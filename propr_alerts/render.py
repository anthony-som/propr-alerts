"""Discord rendering. Prices and levels only — never an amount.

The no-money rule is structural rather than a habit of writing: everything here
takes a `Setup`, and a `Setup` carries no quantity, no notional, no balance and
no PnL figure. There is nothing in scope to leak. `tests/test_render.py` feeds
in a book stuffed with sizes and asserts none of them reach the output.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Sequence

import discord

from .tracker import CANCELLED, CLOSED, FILLED, LEVELS, OPENED, Setup

COLOURS = {
    OPENED: 0x5865F2,     # blurple — a setup is posted, nothing has happened
    LEVELS: 0x5865F2,
    FILLED: 0x2ECC71,
    CANCELLED: 0x9AA0A6,
    CLOSED: 0x95A5A6,
}
CLOSED_COLOURS = {"up": 0x2ECC71, "down": 0xED4245}

STATUS_LINE = {
    "working": "⏳ Working",
    "filled": "✅ Filled",
    "cancelled": "🚫 Cancelled",
    "closed": "🏁 Closed",
}

PROMO_LINE = (
    "[Follow exactly on Propr](https://app.propr.xyz/r/RkWBtYVD) · "
    "[Follow exactly on Hyperliquid](https://app.hyperliquid.xyz/join/QIKO) · "
    "[Follow @qikoCrypto on X](https://x.com/qikoCrypto)"
)


def title(setup: Setup) -> str:
    """`LONG BTC` — direction and ticker, nothing else."""
    return f"{setup.side.upper()} {setup.asset}"


def _decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _price(value: str | None) -> str:
    if not value:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Thousands separators, and only as many decimals as the price actually has.
    text = f"{number:,.8f}".rstrip("0").rstrip(".")
    return text or "0"


def entry_label(setup: Setup) -> str:
    if setup.fill_price:
        return f"{_price(setup.fill_price)} (filled)"
    if setup.entry_type == "market":
        return "market"
    if setup.entry_price:
        return f"{_price(setup.entry_price)} ({setup.entry_type or 'limit'})"
    return setup.entry_type or "—"


def status_label(setup: Setup) -> str:
    label = STATUS_LINE.get(setup.state, setup.state)
    if setup.state == "closed" and setup.outcome:
        label += " in profit" if setup.outcome == "up" else " at a loss"
    return label


def stop_distance_pct(setup: Setup) -> str:
    """How far the stop sits from the entry, as a percentage of the entry.

    Rounded to the nearest half percent, because the exact figure is noise: a
    stop 1.79% away and one 1.83% away are the same trade. Anything under a
    quarter percent would round to nothing, so it reads as `<0.5%` instead.
    """
    base = _decimal(setup.fill_price or setup.entry_price)
    stop = _decimal(setup.stop)
    if base is None or stop is None or base == 0:
        return "—"
    pct = abs(base - stop) / base * 100
    rounded = (pct * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2
    if rounded == 0:
        return "<0.5%"
    return f"{rounded.normalize():f}%"


def risk_pct(setup: Setup) -> str:
    """Configured account risk after the leader-to-follower remap."""
    value = _decimal(setup.risk_pct)
    if value is None:
        return "—"
    return f"{value.normalize():f}%"


def body(setup: Setup) -> str:
    """The three levels, then where the trade stands.

    Written as lines rather than embed fields: fields sit side by side and wrap
    badly on a phone, and the levels are what a reader is scanning for.
    """
    return "\n".join([
        f"**Entry** {entry_label(setup)}",
        f"**Stop** {_price(setup.stop)}",
        f"**Target** {_price(setup.target)}",
        f"**Risk** {risk_pct(setup)}",
        f"**Stop distance** {stop_distance_pct(setup)}",
        "",
        status_label(setup),
        "",
        PROMO_LINE,
    ])


def build_embed(setup: Setup, kind: str) -> discord.Embed:
    colour = COLOURS.get(kind, 0x5865F2)
    if kind == CLOSED and setup.outcome:
        colour = CLOSED_COLOURS.get(setup.outcome, colour)

    embed = discord.Embed(
        title=title(setup),
        description=body(setup),
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Levels only • No sizes")
    return embed


def update_line(setup: Setup, kind: str, changed: Sequence[str] = ()) -> str:
    """The short follow-up posted under a setup, so an edit is never missed.

    It is a reply to the original embed, which already carries the ticker,
    direction and levels — so this says only what changed, and never restates
    the order.
    """
    if kind == FILLED:
        return "✅ Filled"
    if kind == CANCELLED:
        return f"🚫 Cancelled {setup.asset} {setup.side.upper()}"
    if kind == CLOSED:
        if setup.outcome:
            return f"🏁 Closed {'in profit' if setup.outcome == 'up' else 'at a loss'}"
        return "🏁 Closed"
    if kind == LEVELS:
        moves = []
        if "entry" in changed:
            moves.append(f"Entry → {_price(setup.entry_price)}")
        if "stop" in changed:
            moves.append(f"Stop → {_price(setup.stop)}")
        if "target" in changed:
            moves.append(f"Target → {_price(setup.target)}")
        return "✏️ " + (" · ".join(moves) if moves else "Levels updated")
    return "Updated"
