"""Read-only client for the copier's API.

Only two endpoints matter: the account snapshot, which carries the leader's
working orders and open positions already normalised into propr's shape, and
health, used to tell "copier is down" apart from "nothing is on the book".

Basic auth is the same pair the dashboard uses. The copier accepts it on every
route, so no token juggling is needed.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional
from urllib.parse import urlencode

import httpx


class SourceError(RuntimeError):
    """The copier could not be read. The caller decides whether to shout."""


@dataclass
class Book:
    orders: List[dict]
    positions: List[dict]
    # The public signal risk after the copier's leader -> follower remap.
    risk_pct: Optional[str] = None


class CopierSource:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        auth = (user, password) if password else None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=auth,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthy(self) -> bool:
        try:
            response = await self._client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def leader_book(self) -> Book:
        """The leader's working orders and open positions, right now."""
        try:
            response = await self._client.get("/api/accounts/snapshot")
        except httpx.HTTPError as exc:
            raise SourceError(f"copier unreachable: {exc}") from exc

        if response.status_code == 409:
            # The engine is stopped. An empty book would read as "everything
            # was cancelled" and fire a wall of alerts, so refuse instead.
            raise SourceError("copier engine is not running")
        if response.status_code in (401, 403):
            raise SourceError("copier rejected the credentials (COPIER_PASSWORD)")
        if response.status_code != 200:
            raise SourceError(f"copier returned {response.status_code}")

        payload = response.json()
        leader = payload.get("leader") or {}
        risk_pct = await self._alert_risk_pct()
        return Book(
            orders=list(leader.get("orders") or []),
            positions=list(leader.get("positions") or []),
            risk_pct=risk_pct,
        )

    async def _alert_risk_pct(self) -> Optional[str]:
        """Read the configured risk remap without exposing account budgets.

        The copier uses e.g. 10% on the small leader account to represent 1%
        on the follower. Its state endpoint is the source of truth for that
        mapping, so alerts stay aligned if the configuration changes.
        """
        try:
            response = await self._client.get("/api/state")
            if response.status_code != 200:
                return None
            status = (response.json() or {}).get("status") or {}
            risk = status.get("risk") or {}
            if not risk.get("enabled"):
                return None
            value = risk.get("followerPct")
            return str(value) if value not in (None, "") else None
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def leader_label(self) -> Optional[str]:
        """Which account is being watched, for `/status`."""
        try:
            response = await self._client.get("/api/state")
            if response.status_code != 200:
                return None
            status = (response.json() or {}).get("status") or {}
            return status.get("leader") or status.get("leaderAccount")
        except (httpx.HTTPError, ValueError):
            return None


# ------------------------------------------------------------------ MEXC
# Futures side codes: which way the order trades, and whether it closes.
MEXC_SIDES = {1: ("buy", False), 2: ("buy", True), 3: ("sell", False), 4: ("sell", True)}
MEXC_MARKET = {5, 6}


def _mexc_asset(symbol: str) -> str:
    return (symbol or "").split("_")[0]            # BTC_USDT -> BTC


def _decimal(value) -> Optional[Decimal]:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def normalise_mexc(
    orders: List[dict],
    stop_orders: List[dict],
    plan_orders: List[dict],
    positions: List[dict],
    fair_prices: Dict[str, str],
) -> Book:
    """MEXC futures responses, in the copier's order and position shape.

    Only what the tracker reads is produced. `quantity` is carried solely so
    the tracker can tell an open position from a flat one, and `unrealizedPnl`
    is only a sign (fair price vs entry) — neither is ever rendered.
    """
    out: List[dict] = []

    def conditional(asset, closer_side, kind, level):
        if level not in (None, "", 0, "0"):
            out.append({"asset": asset, "type": kind, "side": closer_side,
                        "price": None, "triggerPrice": str(level), "reduceOnly": True})

    for order in orders:
        asset = _mexc_asset(order.get("symbol"))
        side, closing = MEXC_SIDES.get(order.get("side"), (None, False))
        if not side:
            continue
        out.append({
            "orderId": str(order.get("orderId") or ""), "asset": asset,
            "type": "market" if order.get("orderType") in MEXC_MARKET else "limit",
            "side": side, "price": order.get("price"), "reduceOnly": closing,
        })
        if not closing:
            # A stop/target attached to the entry itself, before it fills.
            closer = "sell" if side == "buy" else "buy"
            conditional(asset, closer, "stop_market", order.get("stopLossPrice"))
            conditional(asset, closer, "take_profit_market", order.get("takeProfitPrice"))

    for order in stop_orders:                      # TP/SL on a position
        if order.get("state") not in (None, 1):
            continue
        asset = _mexc_asset(order.get("symbol"))
        closer = "sell" if order.get("positionType") == 1 else "buy"
        conditional(asset, closer, "stop_market", order.get("stopLossPrice"))
        conditional(asset, closer, "take_profit_market", order.get("takeProfitPrice"))

    for order in plan_orders:                      # trigger orders
        asset = _mexc_asset(order.get("symbol"))
        side, closing = MEXC_SIDES.get(order.get("side"), (None, False))
        if not side:
            continue
        if not closing:
            # A stop-entry: shown as an entry resting at its trigger.
            out.append({"orderId": str(order.get("id") or ""), "asset": asset,
                        "type": "limit", "side": side,
                        "price": order.get("triggerPrice"), "reduceOnly": False})
            continue
        # Closing a long (sell) on a fall, or a short (buy) on a rise, is a stop.
        falls = order.get("triggerType") == 2
        is_stop = falls == (side == "sell")
        conditional(asset, side, "stop_market" if is_stop else "take_profit_market",
                    order.get("triggerPrice"))

    held: List[dict] = []
    for position in positions:
        entry = _decimal(position.get("holdAvgPrice") or position.get("openAvgPrice"))
        is_long = position.get("positionType") == 1
        fair = _decimal(fair_prices.get(position.get("symbol")))
        pnl = ""
        if entry is not None and fair is not None:
            pnl = str((fair - entry) * (1 if is_long else -1))
        held.append({
            "asset": _mexc_asset(position.get("symbol")),
            "positionSide": "long" if is_long else "short",
            "quantity": str(position.get("holdVol") or 0),
            "entryPrice": str(entry) if entry is not None else None,
            "unrealizedPnl": pnl,
        })
    return Book(orders=out, positions=held)


class MexcSource:
    """Your MEXC futures account, read directly with a read-only API key.

    The copier has no MEXC adapter, so this signs its own requests. Give the
    key read permission only: every call here is a GET, and nothing can trade.
    """

    BASE_URL = "https://contract.mexc.com"
    # ponytail: one page of 100 per list, paginate if you ever rest more than that.
    PAGE = {"page_num": 1, "page_size": 100}

    def __init__(self, api_key: str, api_secret: str, risk_pct: Optional[str] = None,
                 base_url: str = BASE_URL) -> None:
        self.base_url = base_url
        self._key, self._secret = api_key, api_secret
        self.risk_pct = risk_pct or None
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthy(self) -> bool:
        try:
            return (await self._client.get("/api/v1/contract/ping")).status_code == 200
        except httpx.HTTPError:
            return False

    async def _get(self, path: str, params: Optional[dict] = None, signed: bool = True):
        query = urlencode(sorted((params or {}).items()))
        headers = {}
        if signed:
            stamp = str(int(time.time() * 1000))
            signature = hmac.new(self._secret.encode(), f"{self._key}{stamp}{query}".encode(),
                                 hashlib.sha256).hexdigest()
            headers = {"ApiKey": self._key, "Request-Time": stamp,
                       "Signature": signature, "Content-Type": "application/json"}
        try:
            response = await self._client.get(f"{path}?{query}" if query else path,
                                              headers=headers)
        except httpx.HTTPError as exc:
            raise SourceError(f"mexc unreachable: {exc}") from exc
        if response.status_code != 200:
            raise SourceError(f"mexc returned {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("mexc returned non-JSON") from exc
        if not payload.get("success"):
            # A rejected key must not read as an empty book.
            raise SourceError(f"mexc error {payload.get('code')}: {payload.get('message')}")
        return payload.get("data")

    async def leader_book(self) -> Book:
        orders = await self._get("/api/v1/private/order/list/history_orders",
                                 {"states": 2, **self.PAGE})
        stops = await self._get("/api/v1/private/stoporder/list/orders",
                                {"is_finished": 0, **self.PAGE})
        plans = await self._get("/api/v1/private/planorder/list/orders",
                                {"states": 1, **self.PAGE})
        positions = [p for p in (await self._get("/api/v1/private/position/open_positions") or [])
                     if (_decimal(p.get("holdVol")) or 0) > 0]
        fair: Dict[str, str] = {}
        for symbol in {p.get("symbol") for p in positions}:
            try:
                data = await self._get(f"/api/v1/contract/fair_price/{symbol}", signed=False)
                fair[symbol] = (data or {}).get("fairPrice")
            except SourceError:
                pass                               # only costs the profit/loss wording
        book = normalise_mexc(_rows(orders), _rows(stops), _rows(plans), positions, fair)
        book.risk_pct = self.risk_pct
        return book

    async def leader_label(self) -> Optional[str]:
        return "MEXC futures"


def _rows(data) -> List[dict]:
    """MEXC lists come back bare or wrapped in a page object."""
    if isinstance(data, dict):
        data = data.get("resultList") or data.get("data") or []
    return list(data or [])
