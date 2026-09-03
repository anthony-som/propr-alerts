"""The diff engine, which is where every alert is decided."""
from propr_alerts.tracker import (
    CANCELLED, CLOSED, FILLED, LEVELS, OPENED, Tracker,
)


def entry(**over):
    order = {
        "orderId": "e1", "asset": "BTC", "type": "limit", "side": "buy",
        "quantity": "0.25", "price": "64250.5", "reduceOnly": False,
        "closePosition": False, "status": "open",
    }
    order.update(over)
    return order


def stop(**over):
    order = {
        "orderId": "s1", "asset": "BTC", "type": "stop_market", "side": "sell",
        "quantity": "0.25", "triggerPrice": "63100", "reduceOnly": True,
        "status": "pending",
    }
    order.update(over)
    return order


def take_profit(**over):
    order = {
        "orderId": "t1", "asset": "BTC", "type": "take_profit_market",
        "side": "sell", "quantity": "0.25", "triggerPrice": "66800",
        "reduceOnly": True, "status": "pending",
    }
    order.update(over)
    return order


def position(**over):
    row = {
        "positionId": "p1", "asset": "BTC", "positionSide": "long",
        "quantity": "0.25", "entryPrice": "64250.5", "leverage": "10",
        "unrealizedPnl": "0",
    }
    row.update(over)
    return row


def kinds(alerts):
    return [a.kind for a in alerts]


def test_a_resting_bracket_opens_one_setup():
    tracker = Tracker()
    alerts = tracker.step([entry(), stop(), take_profit()], [])
    assert kinds(alerts) == [OPENED]
    setup = alerts[0].setup
    assert (setup.asset, setup.side, setup.state) == ("BTC", "long", "working")
    assert setup.entry_price == "64250.5"
    assert setup.stop == "63100"
    assert setup.target == "66800"


def test_the_same_book_twice_is_silent():
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    assert tracker.step([entry(), stop()], []) == []


def test_moving_a_stop_reports_levels():
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    alerts = tracker.step([entry(), stop(triggerPrice="63500")], [])
    assert kinds(alerts) == [LEVELS]
    assert alerts[0].setup.stop == "63500"


def test_moving_entry_stop_and_target_reports_every_changed_order():
    tracker = Tracker()
    tracker.step([entry(), stop(), take_profit()], [])
    alerts = tracker.step([
        entry(price="64500"),
        stop(triggerPrice="63500"),
        take_profit(triggerPrice="67000"),
    ], [])
    assert kinds(alerts) == [LEVELS]
    assert alerts[0].changed == ["entry", "stop", "target"]
    assert alerts[0].setup.entry_price == "64500"
    assert alerts[0].setup.stop == "63500"
    assert alerts[0].setup.target == "67000"


def test_configured_follower_risk_is_carried_by_the_setup():
    tracker = Tracker()
    alerts = tracker.step([entry(), stop()], [], risk_pct="1")
    assert alerts[0].setup.risk_pct == "1"

    revived = Tracker()
    revived.load(tracker.dump())
    assert revived.setups["BTC:long"].risk_pct == "1"


def test_a_fill_is_the_entry_going_away_while_a_position_appears():
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    alerts = tracker.step([stop()], [position(entryPrice="64251")])
    assert kinds(alerts) == [FILLED]
    assert alerts[0].setup.state == "filled"
    assert alerts[0].setup.fill_price == "64251"


def test_a_cancel_is_the_entry_going_away_with_no_position():
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    alerts = tracker.step([], [])
    assert kinds(alerts) == [CANCELLED]


def test_a_close_reports_direction_but_never_a_figure():
    tracker = Tracker()
    tracker.step([entry()], [])
    tracker.step([stop()], [position()])
    tracker.step([stop()], [position(unrealizedPnl="184.20")])
    alerts = tracker.step([], [])
    assert kinds(alerts) == [CLOSED]
    assert alerts[0].setup.outcome == "up"


def test_outcome_can_be_switched_off():
    tracker = Tracker(show_outcome=False)
    tracker.step([entry()], [])
    tracker.step([], [position(unrealizedPnl="-90")])
    alerts = tracker.step([], [])
    assert alerts[0].setup.outcome == ""


def test_a_market_fill_never_seen_as_an_order_still_alerts():
    """Nothing rests on the book, so the position is the first sighting."""
    tracker = Tracker()
    alerts = tracker.step([], [position()])
    assert kinds(alerts) == [FILLED]
    assert alerts[0].setup.entry_type == "market"


def test_a_flagless_stop_protects_the_long_instead_of_opening_a_short():
    """Hyperliquid reports triggers with reduceOnly unset — the copier's bug."""
    tracker = Tracker()
    alerts = tracker.step([entry(), stop(reduceOnly=False)], [])
    assert kinds(alerts) == [OPENED]
    assert len(tracker.setups) == 1
    assert tracker.setups["BTC:long"].stop == "63100"


def test_a_short_is_read_from_the_order_not_the_position_side_field():
    """propr aligns an order's positionSide with its side; deriving is safe."""
    tracker = Tracker()
    alerts = tracker.step(
        [entry(side="sell", positionSide="short", price="100")], []
    )
    assert alerts[0].setup.side == "short"


def test_seeding_adopts_the_book_without_alerting():
    tracker = Tracker()
    assert tracker.step([entry(), stop()], [], seed=True) == []
    assert tracker.setups["BTC:long"].stop == "63100"
    assert tracker.step([entry(), stop()], []) == []


def test_the_same_asset_again_is_a_new_setup():
    tracker = Tracker()
    tracker.step([entry()], [], now="t1")
    tracker.step([], [], now="t2")                      # cancelled
    alerts = tracker.step([entry(orderId="e2")], [], now="t3")
    assert kinds(alerts) == [OPENED]
    assert alerts[0].setup.opened_at == "t3"


def test_state_survives_a_restart():
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    revived = Tracker()
    revived.load(tracker.dump())
    assert revived.step([entry(), stop()], []) == []
    assert revived.setups["BTC:long"].stop == "63100"


def test_a_bracket_that_arrives_after_the_entry_does_alert():
    """The counterpart to a same-tick bracket, which must stay silent."""
    tracker = Tracker()
    assert kinds(tracker.step([entry()], [])) == [OPENED]
    alerts = tracker.step([entry(), stop(), take_profit()], [])
    assert kinds(alerts) == [LEVELS]
    assert (alerts[0].setup.stop, alerts[0].setup.target) == ("63100", "66800")


def test_a_same_tick_bracket_is_carried_by_the_opening_alert():
    tracker = Tracker()
    alerts = tracker.step([entry(), stop(), take_profit()], [])
    assert kinds(alerts) == [OPENED]
    assert alerts[0].setup.stop == "63100"
