"""Turn snapshots of the leader's book into a stream of alertable events.

The copier already normalises every venue into propr's order shape, so this
only has to diff one tick against the last:

    working orders + open positions  ->  opened / levels / filled / cancelled / closed

Deliberately pure. It performs no I/O, holds no Discord state, and knows
nothing about money — sizes and PnL enter here only as the private signals that
decide *whether* an event happened, and never leave in an event.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional

ENTRY_TYPES = {"market", "limit"}
STOP_TYPES = {"stop_market", "stop_limit"}
TARGET_TYPES = {"take_profit_market", "take_profit_limit"}
CONDITIONAL_TYPES = STOP_TYPES | TARGET_TYPES

OPENED, LEVELS, FILLED, CANCELLED, CLOSED = (
    "opened", "levels", "filled", "cancelled", "closed",
)


def _num(value) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def target_side(order: dict) -> str:
    """Which position an order acts on, derived from the order itself.

    Never read `positionSide` off the order: propr aligns an *order's*
    positionSide with its side, so a sell reads as `short` even when it is
    closing a long. A conditional counts as closing even with `reduceOnly`
    unset, because Hyperliquid routinely reports triggers with the flag off —
    the same rule the copier settled on in `CopyEngine._target_side`.
    """
    closing = bool(
        order.get("reduceOnly")
        or order.get("closePosition")
        or order.get("type") in CONDITIONAL_TYPES
    )
    is_buy = order.get("side") == "buy"
    if closing:
        return "short" if is_buy else "long"
    return "long" if is_buy else "short"


@dataclass
class Setup:
    """One trade idea on one asset and side, from resting order to close."""

    key: str
    asset: str
    side: str                      # long | short
    state: str = "working"         # working | filled | cancelled | closed
    entry_type: str = ""
    entry_price: Optional[str] = None
    fill_price: Optional[str] = None
    stop: Optional[str] = None
    target: Optional[str] = None
    outcome: str = ""              # up | down | "" — direction only, never a figure
    entry_ids: List[str] = field(default_factory=list)
    opened_at: str = ""
    updated_at: str = ""

    @property
    def live(self) -> bool:
        return self.state in ("working", "filled")


@dataclass
class Alert:
    kind: str
    setup: Setup
    # For a LEVELS alert, which of stop/target actually moved.
    changed: List[str] = field(default_factory=list)


class Tracker:
    """Holds the last seen book and reports what changed since."""

    def __init__(self, show_outcome: bool = True) -> None:
        self.setups: Dict[str, Setup] = {}
        self.show_outcome = show_outcome
        self.seeded = False
        # Last seen unrealised PnL per key. Held only so a close can say which
        # way the trade went; it is never rendered and never leaves this class.
        self._last_pnl: Dict[str, str] = {}

    # ------------------------------------------------------------- state
    def dump(self) -> str:
        return json.dumps(
            {"seeded": self.seeded,
             "pnl": self._last_pnl,
             "setups": {k: asdict(v) for k, v in self.setups.items()}}
        )

    def load(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        self.seeded = bool(data.get("seeded"))
        self._last_pnl = dict(data.get("pnl") or {})
        self.setups = {
            key: Setup(**row) for key, row in (data.get("setups") or {}).items()
        }

    # -------------------------------------------------------------- diff
    def step(
        self,
        orders: Iterable[dict],
        positions: Iterable[dict],
        now: str = "",
        seed: bool = False,
    ) -> List[Alert]:
        """Fold one snapshot in and return the alerts it produced.

        `seed` adopts whatever is already on the book without alerting — the
        first tick after a cold start, where every resting order would
        otherwise look brand new.
        """
        entries: Dict[str, List[dict]] = {}
        stops: Dict[str, dict] = {}
        targets: Dict[str, dict] = {}

        for order in orders:
            asset = order.get("asset")
            if not asset:
                continue
            kind = order.get("type")
            key = f"{asset}:{target_side(order)}"
            if kind in ENTRY_TYPES and not (
                order.get("reduceOnly") or order.get("closePosition")
            ):
                entries.setdefault(key, []).append(order)
            elif kind in STOP_TYPES:
                stops.setdefault(key, order)
            elif kind in TARGET_TYPES:
                targets.setdefault(key, order)

        live_positions: Dict[str, dict] = {}
        for position in positions:
            asset, side = position.get("asset"), position.get("positionSide")
            if asset and side and (_num(position.get("quantity")) or 0) != 0:
                key = f"{asset}:{side}"
                live_positions[key] = position
                self._last_pnl[key] = str(position.get("unrealizedPnl") or "")

        alerts: List[Alert] = []
        seen = set(entries) | set(live_positions)
        # Setups born this tick get their levels in the opening alert — the
        # Alert holds the Setup itself, so anything attached before publishing
        # is already in the embed. Reporting them again would double-post every
        # bracket, which is how most entries arrive.
        fresh: set = set()

        for key in sorted(seen):
            setup = self.setups.get(key)
            if setup is None:
                setup = self._new_setup(key, entries.get(key), live_positions.get(key), now)
                self.setups[key] = setup
                fresh.add(key)
                if not seed:
                    # A position with no resting entry means the entry filled
                    # before we ever saw it — a market order, most of the time.
                    alerts.append(Alert(FILLED if setup.state == "filled" else OPENED, setup))
            elif not setup.live:
                # The same asset and side coming back is a new idea, not the
                # old one reopening.
                setup = self._new_setup(key, entries.get(key), live_positions.get(key), now)
                self.setups[key] = setup
                fresh.add(key)
                if not seed:
                    alerts.append(Alert(FILLED if setup.state == "filled" else OPENED, setup))

            moved = self._apply_levels(setup, stops.get(key), targets.get(key))
            if moved and not seed and key not in fresh:
                setup.updated_at = now
                alerts.append(Alert(LEVELS, setup, changed=moved))

            if setup.state == "working" and key in live_positions:
                setup.state = "filled"
                setup.fill_price = _fmt(live_positions[key].get("entryPrice"))
                setup.updated_at = now
                if not seed:
                    alerts.append(Alert(FILLED, setup))

        for key, setup in list(self.setups.items()):
            if not setup.live:
                continue
            if setup.state == "working" and key not in entries and key not in live_positions:
                setup.state = CANCELLED
                setup.updated_at = now
                if not seed:
                    alerts.append(Alert(CANCELLED, setup))
            elif setup.state == "filled" and key not in live_positions:
                setup.state = CLOSED
                setup.updated_at = now
                self.note_outcome(setup, self._last_pnl.pop(key, None))
                if not seed:
                    alerts.append(Alert(CLOSED, setup))

        self._forget_settled()
        self.seeded = True
        return alerts

    # ------------------------------------------------------------ helpers
    def _new_setup(
        self, key: str, entries: Optional[List[dict]], position: Optional[dict], now: str
    ) -> Setup:
        asset, _, side = key.partition(":")
        first = (entries or [{}])[0]
        setup = Setup(
            key=key, asset=asset, side=side,
            entry_type=first.get("type", "") or ("market" if position else ""),
            entry_price=_fmt(first.get("price")),
            opened_at=now, updated_at=now,
        )
        setup.entry_ids = [o.get("orderId") for o in (entries or []) if o.get("orderId")]
        if position is not None:
            setup.state = "filled"
            setup.fill_price = _fmt(position.get("entryPrice"))
        return setup

    def _apply_levels(
        self, setup: Setup, stop: Optional[dict], target: Optional[dict]
    ) -> List[str]:
        """Attach stop/target levels, naming whichever actually moved.

        Levels are kept once seen: when a position closes the exchange cancels
        the survivor, and dropping it would blank the alert at the exact moment
        someone reads it.
        """
        changed: List[str] = []
        for order, attribute in ((stop, "stop"), (target, "target")):
            if order is None:
                continue
            level = _fmt(order.get("triggerPrice") or order.get("price"))
            if level and level != getattr(setup, attribute):
                setattr(setup, attribute, level)
                changed.append(attribute)
        return changed

    def note_outcome(self, setup: Setup, unrealized) -> None:
        """Record which way a trade ended, as a direction and nothing more."""
        if not self.show_outcome:
            return
        value = _num(unrealized)
        if value is not None and value != 0:
            setup.outcome = "up" if value > 0 else "down"

    def _forget_settled(self, keep: int = 200) -> None:
        """Settled setups are kept a while so a late edit still finds them."""
        settled = [k for k, setup in self.setups.items() if not setup.live]
        for key in settled[: max(0, len(settled) - keep)]:
            self.setups.pop(key, None)
            self._last_pnl.pop(key, None)


def _fmt(value) -> Optional[str]:
    """A price for humans: no scientific notation, no trailing zero noise."""
    number = _num(value)
    if number is None:
        return None
    text = format(number.normalize(), "f")
    return text
