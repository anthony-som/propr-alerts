"""Read-only client for the copier's API.

Only two endpoints matter: the account snapshot, which carries the leader's
working orders and open positions already normalised into propr's shape, and
health, used to tell "copier is down" apart from "nothing is on the book".

Basic auth is the same pair the dashboard uses. The copier accepts it on every
route, so no token juggling is needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import httpx


class SourceError(RuntimeError):
    """The copier could not be read. The caller decides whether to shout."""


@dataclass
class Book:
    orders: List[dict]
    positions: List[dict]


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
        return Book(
            orders=list(leader.get("orders") or []),
            positions=list(leader.get("positions") or []),
        )

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
