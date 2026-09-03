"""Persistence: who is subscribed, what has been posted, and tracker state.

Cloned from the quantum bot's subscription model — one row per guild, with the
channel to post in and an optional role to mention — plus two tables it did not
need: the message ids of every alert, so a setup can be edited in place when it
fills or cancels, and a single-row blob of tracker state, so a restart does not
re-announce every order already on the book.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    guild_id   TEXT PRIMARY KEY,
    channel_id TEXT,
    role_id    TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS alert_messages (
    alert_key  TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (alert_key, guild_id)
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    async def setup(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ------------------------------------------------------ subscriptions
    async def subscribe(
        self,
        guild_id: str,
        channel_id: Optional[str] = None,
        role_id: Optional[str] = None,
    ) -> None:
        """Upsert one guild's subscription, leaving unnamed fields alone."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO subscriptions (guild_id, channel_id, role_id, created_at)"
                " VALUES (?, ?, ?, datetime('now'))"
                " ON CONFLICT(guild_id) DO UPDATE SET"
                "   channel_id = COALESCE(excluded.channel_id, subscriptions.channel_id),"
                "   role_id    = COALESCE(excluded.role_id, subscriptions.role_id)",
                (guild_id, channel_id, role_id),
            )
            await db.commit()

    async def unsubscribe(self, guild_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM subscriptions WHERE guild_id = ?", (guild_id,))
            await db.execute("DELETE FROM alert_messages WHERE guild_id = ?", (guild_id,))
            await db.commit()

    async def subscriptions(self) -> Dict[str, dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT guild_id, channel_id, role_id FROM subscriptions"
                " WHERE channel_id IS NOT NULL"
            )).fetchall()
        return {r["guild_id"]: {"channel": r["channel_id"], "role": r["role_id"]}
                for r in rows}

    async def all_subscriptions(self) -> List[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT guild_id, channel_id, role_id, created_at FROM subscriptions"
                " ORDER BY created_at"
            )).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------- posted messages
    async def remember_message(
        self, alert_key: str, guild_id: str, channel_id: str, message_id: str
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO alert_messages"
                " (alert_key, guild_id, channel_id, message_id) VALUES (?, ?, ?, ?)",
                (alert_key, guild_id, channel_id, message_id),
            )
            await db.commit()

    async def messages_for(self, alert_key: str) -> List[Tuple[str, str, str]]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT guild_id, channel_id, message_id FROM alert_messages"
                " WHERE alert_key = ?",
                (alert_key,),
            )).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    async def forget_messages(self, alert_key: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM alert_messages WHERE alert_key = ?", (alert_key,))
            await db.commit()

    # --------------------------------------------------------- kv (state)
    async def get(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT v FROM kv WHERE k = ?", (key,))).fetchone()
        return row[0] if row else None

    async def put(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )
            await db.commit()
