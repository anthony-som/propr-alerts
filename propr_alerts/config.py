"""Runtime configuration, all from the environment.

This bot holds no trading credentials of its own. It reads the copier's
read-only HTTP API with the same basic-auth pair the dashboard uses, and it
talks to Discord with a bot token. Nothing here can place an order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")


def _ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


@dataclass
class Config:
    discord_token: str = ""
    owner_ids: set[int] = field(default_factory=set)

    # The copier's API — the only place trade data comes from.
    copier_url: str = "http://127.0.0.1:8080"
    copier_user: str = "propr"
    copier_password: str = ""

    database: Path = Path("data/alerts.db")
    poll_seconds: float = 3.0

    # A cold start with no saved state would otherwise alert every order that
    # happens to be working right now. Seeding adopts them silently instead.
    seed_alerts: bool = False
    # Say whether a closed trade ended up or down. This is a direction, never a
    # figure — see render.py, which has no access to money at all.
    show_outcome: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            owner_ids=_ids(os.getenv("OWNER_IDS", "")),
            copier_url=os.getenv("COPIER_URL", "http://127.0.0.1:8080").rstrip("/"),
            copier_user=os.getenv("COPIER_USER", os.getenv("UI_USER", "propr")).strip(),
            copier_password=os.getenv(
                "COPIER_PASSWORD", os.getenv("UI_PASSWORD", "")
            ).strip(),
            database=Path(os.getenv("ALERTS_DB", "data/alerts.db")),
            poll_seconds=float(os.getenv("POLL_SECONDS", "3")),
            seed_alerts=os.getenv("SEED_ALERTS", "").lower() in ("1", "true", "yes"),
            show_outcome=os.getenv("SHOW_OUTCOME", "true").lower()
            not in ("0", "false", "no"),
        )

    def missing(self) -> list[str]:
        gaps = []
        if not self.discord_token:
            gaps.append("DISCORD_TOKEN")
        if not self.owner_ids:
            gaps.append("OWNER_IDS")
        return gaps
