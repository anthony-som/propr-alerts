"""Hyperliquid orders, in the shape the copier actually hands over.

These fixtures are not invented: they are the output of the copier's own
`normalize_order` / `normalize_position` run over the verbatim Hyperliquid API
responses in its `tests/test_hyperliquid.py`. That adapter is the only thing
between the exchange and this bot, so matching its output is what makes the
alerts real rather than plausible.
"""
from propr_alerts.tracker import CLOSED, FILLED, LEVELS, OPENED, Tracker

# A resting Alo buy on NEAR — normalize_order(HL_LIMIT).
HL_LIMIT = {
    "orderId": "534299702396", "asset": "NEAR", "type": "limit", "side": "buy",
    "positionSide": "long", "price": "1.8623", "triggerPrice": None,
    "reduceOnly": False, "quantity": "523.3", "status": "open",
}
# A stop-market protecting a long — normalize_order(HL_STOP). Note `price` is
# None and the level lives on `triggerPrice`.
HL_STOP = {
    "orderId": "534299702400", "asset": "NEAR", "type": "stop_market",
    "side": "sell", "positionSide": "long", "price": None,
    "triggerPrice": "1.7100", "reduceOnly": True, "quantity": "523.3",
    "status": "pending",
}
HL_TAKE_PROFIT = {
    "orderId": "534299702401", "asset": "NEAR", "type": "take_profit_market",
    "side": "sell", "positionSide": "long", "price": None,
    "triggerPrice": "2.0500", "reduceOnly": True, "quantity": "523.3",
    "status": "pending",
}
# normalize_position(HL_POSITION): quantity is unsigned, side derived from szi.
HL_POSITION = {
    "positionId": "hl:NEAR:long", "asset": "NEAR", "positionSide": "long",
    "quantity": "523.3", "entryPrice": "1.8623", "leverage": "20",
    "marginMode": "cross", "unrealizedPnl": "0",
}


def kinds(alerts):
    return [a.kind for a in alerts]


def test_a_hyperliquid_bracket_becomes_one_setup():
    tracker = Tracker()
    alerts = tracker.step([HL_LIMIT, HL_STOP, HL_TAKE_PROFIT], [])
    assert kinds(alerts) == [OPENED]
    setup = alerts[0].setup
    assert (setup.asset, setup.side) == ("NEAR", "long")
    assert setup.entry_price == "1.8623"
    # The levels come off triggerPrice, which is where Hyperliquid puts them.
    assert setup.stop == "1.71"
    assert setup.target == "2.05"


def test_the_whole_life_of_a_hyperliquid_trade():
    tracker = Tracker()
    assert kinds(tracker.step([HL_LIMIT, HL_STOP, HL_TAKE_PROFIT], [])) == [OPENED]

    # Entry fills: the resting limit leaves the book, the position appears.
    filled = tracker.step([HL_STOP, HL_TAKE_PROFIT], [HL_POSITION])
    assert kinds(filled) == [FILLED]
    assert filled[0].setup.fill_price == "1.8623"

    # Stop trailed up.
    moved = tracker.step(
        [{**HL_STOP, "triggerPrice": "1.8000"}, HL_TAKE_PROFIT], [HL_POSITION]
    )
    assert kinds(moved) == [LEVELS]
    assert moved[0].setup.stop == "1.8"

    # Target hit: position gone, exchange cancels the survivor.
    in_profit = {**HL_POSITION, "unrealizedPnl": "412.90"}
    tracker.step([HL_TAKE_PROFIT], [in_profit])
    closed = tracker.step([], [])
    assert kinds(closed) == [CLOSED]
    assert closed[0].setup.outcome == "up"
    assert closed[0].setup.stop == "1.8"          # levels survive the close


def test_a_short_entry_is_read_correctly():
    """A plain sell with no reduceOnly opens a short."""
    tracker = Tracker()
    sell = {**HL_LIMIT, "orderId": "9", "side": "sell", "positionSide": "short"}
    alerts = tracker.step([sell], [])
    assert alerts[0].setup.side == "short"


def test_a_stop_with_no_matching_position_is_ignored_not_alerted():
    """An orphan conditional must never masquerade as a new setup."""
    tracker = Tracker()
    assert tracker.step([HL_STOP], []) == []
    assert tracker.setups == {}
