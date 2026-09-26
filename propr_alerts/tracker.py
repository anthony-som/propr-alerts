"""Turn snapshots of the leader's book into a stream of alertable events.

The copier already normalises every venue into propr's order shape, so this
only has to diff one tick against the last:

    working orders + open positions  ->  opened / levels / filled / trimmed /
                                         added / cancelled / closed

Deliberately pure. It performs no I/O and holds no Discord state. Quantities,
balances and PnL never leave it; the only account-level figure retained on a
setup is the configured public risk percentage.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional

ENTRY_TYPES = {"market", "limit"}
STOP_TYPES = {"stop_market", "stop_limit"}
TARGET_TYPES = {"take_profit_market", "take_profit_limit"}
CONDITIONAL_TYPES = STOP_TYPES | TARGET_TYPES

OPENED, LEVELS, FILLED, TRIMMED, ADDED, CANCELLED, CLOSED = (
    "opened", "levels", "filled", "trimmed", "added", "cancelled", "closed",
)

# A size move smaller than this is a rounding remainder from a partial fill,
# not a decision anyone made. Hyperliquid takes fees and funding out of margin
# rather than out of the position, so a real trim is never this small.
MIN_SIZE_MOVE = 3      # percent


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
    risk_pct: Optional[str] = None
    closed_pct: Optional[str] = None   # how much of the position has been taken off
    outcome: str = ""              # up | down | "" — direction only, never a figure
    venue: str = ""                # "" is the copier's Hyperliquid leader
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
    # For a TRIMMED alert, how much of the position went in this one move.
    pct: str = ""


class Tracker:
    """Holds the last seen book and reports what changed since."""

    def __init__(self, show_outcome: bool = True, venue: str = "") -> None:
        self.venue = venue
        self.setups: Dict[str, Setup] = {}
        self.show_outcome = show_outcome
        self.seeded = False
        # Last seen unrealised PnL per key. Held only so a close can say which
        # way the trade went; it is never rendered and never leaves this class.
        self._last_pnl: Dict[str, str] = {}
        # Position sizes per key — `last` seen, `base` last reported on, and the
        # `peak` it reached. They stay in here for the same reason PnL does: what
        # leaves is a percentage, which says how a trade is being managed without
        # saying how big it is.
        self._size: Dict[str, Dict[str, str]] = {}
        # Conditionals seen leaving the book, against the size they left at.
        # A removal is judged a tick later, so this has to survive the gap.
        self._gone: Dict[str, Dict[str, str]] = {}

    # ------------------------------------------------------------- state
    def dump(self) -> str:
        return json.dumps(
            {"seeded": self.seeded,
             "pnl": self._last_pnl,
             "size": self._size,
             "gone": self._gone,
             "setups": {k: asdict(v) for k, v in self.setups.items()}}
        )

    def load(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        self.seeded = bool(data.get("seeded"))
        self._last_pnl = dict(data.get("pnl") or {})
        self._size = {
            key: dict(row) for key, row in (data.get("size") or {}).items()
        }
        self._gone = {
            key: dict(row) for key, row in (data.get("gone") or {}).items()
        }
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
        risk_pct: Optional[str] = None,
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
                setup = self._new_setup(
                    key, entries.get(key), live_positions.get(key), now, risk_pct
                )
                self.setups[key] = setup
                fresh.add(key)
                if not seed:
                    # A position with no resting entry means the entry filled
                    # before we ever saw it — a market order, most of the time.
                    alerts.append(Alert(FILLED if setup.state == "filled" else OPENED, setup))
            elif not setup.live:
                # The same asset and side coming back is a new idea, not the
                # old one reopening.
                setup = self._new_setup(
                    key, entries.get(key), live_positions.get(key), now, risk_pct
                )
                self.setups[key] = setup
                fresh.add(key)
                if not seed:
                    alerts.append(Alert(FILLED if setup.state == "filled" else OPENED, setup))

            if risk_pct is not None:
                setup.risk_pct = _fmt(risk_pct)

            moved = self._apply_levels(
                setup, entries.get(key), stops.get(key), targets.get(key)
            )
            moved += self._apply_removals(
                setup, key, stops.get(key), targets.get(key), live_positions.get(key)
            )
            if moved and not seed and key not in fresh:
                setup.updated_at = now
                alerts.append(Alert(LEVELS, setup, changed=moved))

            just_filled = False
            if setup.state == "working" and key in live_positions:
                setup.state = "filled"
                setup.fill_price = _fmt(live_positions[key].get("entryPrice"))
                setup.updated_at = now
                just_filled = True
                if not seed:
                    alerts.append(Alert(FILLED, setup))

            # Sizing is folded in even when nothing is announced, so a trim is
            # always measured against a baseline the alert actually reported.
            if key in live_positions:
                sized = self._fold_size(setup, key, live_positions[key], now)
                if sized and not seed and not just_filled and key not in fresh:
                    alerts.append(sized)

        for key, setup in list(self.setups.items()):
            if not setup.live:
                continue
            if setup.state == "working" and key not in entries and key not in live_positions:
                setup.state = CANCELLED
                setup.updated_at = now
                self._size.pop(key, None)
                self._gone.pop(key, None)
                if not seed:
                    alerts.append(Alert(CANCELLED, setup))
            elif setup.state == "filled" and key not in live_positions:
                setup.state = CLOSED
                setup.updated_at = now
                self._size.pop(key, None)
                self._gone.pop(key, None)
                self.note_outcome(setup, self._last_pnl.pop(key, None))
                if not seed:
                    alerts.append(Alert(CLOSED, setup))

        self._forget_settled()
        self.seeded = True
        return alerts

    # ------------------------------------------------------------ helpers
    def _new_setup(
        self,
        key: str,
        entries: Optional[List[dict]],
        position: Optional[dict],
        now: str,
        risk_pct: Optional[str],
    ) -> Setup:
        asset, _, side = key.partition(":")
        self._size.pop(key, None)          # a reused key starts sizing afresh
        self._gone.pop(key, None)
        first = (entries or [{}])[0]
        setup = Setup(
            key=key, asset=asset, side=side,
            entry_type=first.get("type", "") or ("market" if position else ""),
            entry_price=_fmt(first.get("price")),
            risk_pct=_fmt(risk_pct), venue=self.venue,
            opened_at=now, updated_at=now,
        )
        setup.entry_ids = [o.get("orderId") for o in (entries or []) if o.get("orderId")]
        if position is not None:
            setup.state = "filled"
            setup.fill_price = _fmt(position.get("entryPrice"))
        return setup

    def _apply_levels(
        self,
        setup: Setup,
        entries: Optional[List[dict]],
        stop: Optional[dict],
        target: Optional[dict],
    ) -> List[str]:
        """Attach order levels, naming whichever actually moved.

        A level absent from this tick is left alone rather than cleared: when a
        position closes the exchange cancels the survivor, and dropping it here
        would blank the alert at the exact moment someone reads it. Clearing a
        level that was genuinely pulled is `_apply_removals`, which can tell the
        two apart.
        """
        changed: List[str] = []
        if setup.state == "working" and entries:
            entry = entries[0]
            level = _fmt(entry.get("price"))
            if level and setup.entry_price is not None and level != setup.entry_price:
                setup.entry_price = level
                setup.entry_type = entry.get("type", "") or setup.entry_type
                changed.append("entry")
            setup.entry_ids = [
                order.get("orderId") for order in entries if order.get("orderId")
            ]
        for order, attribute in ((stop, "stop"), (target, "target")):
            if order is None:
                continue
            level = _fmt(order.get("triggerPrice") or order.get("price"))
            if level and level != getattr(setup, attribute):
                setattr(setup, attribute, level)
                changed.append(attribute)
        return changed

    def _apply_removals(
        self,
        setup: Setup,
        key: str,
        stop: Optional[dict],
        target: Optional[dict],
        position: Optional[dict],
    ) -> List[str]:
        """Report a stop or target that has been taken off the book.

        Judged a tick late, for two reasons. A cancel-and-replace that straddles
        a poll boundary would otherwise read as a removal chased by a new order,
        when a follower only needs the move. And a conditional that vanished
        because it *filled* has to be told apart from one that was pulled —
        which the position size settles, since it shrinks in the first case and
        holds in the second. A fill is left to the trim alert to report.

        A close never reaches here: the exchange cancels the surviving bracket
        order as the position goes, but the key drops out of `live_positions` in
        the same tick, so the setup is not folded at all.
        """
        current = _size_of(position)
        previous = (self._size.get(key) or {}).get("last", current)
        pending = self._gone.setdefault(key, {})
        removed: List[str] = []

        for order, attribute in ((stop, "stop"), (target, "target")):
            if order is not None or getattr(setup, attribute) is None:
                pending.pop(attribute, None)     # back on the book, or never on it
                continue
            if attribute not in pending:
                # Note the size it was last protecting and decide next tick.
                pending[attribute] = previous
                continue
            if pending.pop(attribute) == current:
                removed.append(f"{attribute}_gone")
            setattr(setup, attribute, None)

        if not pending:
            self._gone.pop(key, None)
        return removed

    def _fold_size(
        self, setup: Setup, key: str, position: dict, now: str
    ) -> Optional[Alert]:
        """Fold this tick's position size in and report a trim or an add.

        A change has to hold still for a tick before it is announced. A limit
        entry, and a take-profit worked in pieces, both walk the size across
        several polls, and reporting each step would turn one decision into a
        stream of alerts.

        What comes out is a percentage of the position and never a quantity:
        the sizes themselves stay in `self._size`, out of reach of the Setup
        and so out of reach of the renderer.
        """
        current = _num(position.get("quantity"))
        current = abs(current) if current is not None else None
        if not current:
            return None

        sizes = self._size.get(key)
        if sizes is None:
            # First sighting: this is the size to measure everything against.
            self._size[key] = dict.fromkeys(("last", "base", "peak"), str(current))
            return None

        last, base = _num(sizes.get("last")), _num(sizes.get("base"))
        peak = max(_num(sizes.get("peak")) or current, current)
        sizes.update(last=str(current), peak=str(peak))

        # How much of the largest this position ever was has now been taken off.
        off = _percent(peak - current, peak)
        setup.closed_pct = str(off) if off >= MIN_SIZE_MOVE else None

        if not base or current == base or current != last:
            return None                     # nothing new, or still moving
        moved = _percent(abs(current - base), base)
        if moved < MIN_SIZE_MOVE:
            return None

        sizes["base"] = str(current)
        setup.updated_at = now
        if current > base:
            # Adding moves the average entry, and that is the number on show.
            setup.fill_price = _fmt(position.get("entryPrice")) or setup.fill_price
            return Alert(ADDED, setup)
        return Alert(TRIMMED, setup, pct=str(moved))

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
            self._size.pop(key, None)
            self._gone.pop(key, None)


def _size_of(position: Optional[dict]) -> str:
    """A position's size, spelled the way the sizing dictionaries spell it."""
    if not position:
        return ""
    quantity = _num(position.get("quantity"))
    return str(abs(quantity)) if quantity else ""


def _percent(part: Decimal, whole: Decimal) -> int:
    """A share of a position, to the nearest whole percent.

    Whole percents on purpose: a trim is a decision, not a measurement, and a
    figure like `49.7%` invites someone to reconstruct the size behind it.
    """
    if not whole:
        return 0
    share = (part / whole * 100).to_integral_value(rounding=ROUND_HALF_UP)
    return int(max(Decimal(0), min(Decimal(100), share)))


def _fmt(value) -> Optional[str]:
    """A price for humans: no scientific notation, no trailing zero noise."""
    number = _num(value)
    if number is None:
        return None
    text = format(number.normalize(), "f")
    return text
