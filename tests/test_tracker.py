"""The diff engine, which is where every alert is decided."""
from propr_alerts.tracker import (
    ADDED, CANCELLED, CLOSED, FILLED, LEVELS, OPENED, TRIMMED, Tracker,
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


def test_a_partial_close_is_reported_as_a_percentage():
    """Halving a position is management, and it used to pass in silence."""
    tracker = Tracker()
    tracker.step([entry()], [])
    tracker.step([stop()], [position(quantity="0.25")])
    # The size has to settle before it is announced, so the tick it moves on
    # is silent and the next one carries the alert.
    assert kinds(tracker.step([stop()], [position(quantity="0.125")])) == []
    alerts = tracker.step([stop()], [position(quantity="0.125")])
    assert kinds(alerts) == [TRIMMED]
    assert alerts[0].pct == "50"
    assert alerts[0].setup.closed_pct == "50"
    assert alerts[0].setup.state == "filled"       # the rest is still running


def test_a_second_trim_counts_from_the_largest_the_position_was():
    tracker = Tracker()
    tracker.step([stop()], [position(quantity="0.25")])
    for quantity in ("0.125", "0.125", "0.0625"):
        tracker.step([stop()], [position(quantity=quantity)])
    alerts = tracker.step([stop()], [position(quantity="0.0625")])
    assert kinds(alerts) == [TRIMMED]
    assert alerts[0].pct == "50"                   # half of what was left
    assert alerts[0].setup.closed_pct == "75"      # three quarters of the peak


def test_an_entry_filling_in_pieces_is_not_a_stream_of_adds():
    """A limit entry walks the size up over several polls; that is one fill."""
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    alerts = kinds(tracker.step([entry(), stop()], [position(quantity="0.1")]))
    for quantity in ("0.18", "0.25"):
        alerts += kinds(tracker.step([entry(), stop()], [position(quantity=quantity)]))
    assert alerts == [FILLED]


def test_a_rounding_remainder_is_not_a_trim():
    tracker = Tracker()
    tracker.step([stop()], [position(quantity="0.25")])
    tracker.step([stop()], [position(quantity="0.2496")])
    assert tracker.step([stop()], [position(quantity="0.2496")]) == []


def test_adding_to_a_position_reports_the_new_average_entry():
    tracker = Tracker()
    tracker.step([stop()], [position(quantity="0.25", entryPrice="64000")])
    tracker.step([stop()], [position(quantity="0.5", entryPrice="63800")])
    alerts = tracker.step([stop()], [position(quantity="0.5", entryPrice="63800")])
    assert kinds(alerts) == [ADDED]
    assert alerts[0].setup.fill_price == "63800"
    assert alerts[0].setup.closed_pct is None


def test_a_full_close_after_a_trim_still_closes():
    tracker = Tracker()
    tracker.step([stop()], [position(quantity="0.25")])
    tracker.step([stop()], [position(quantity="0.125")])
    tracker.step([stop()], [position(quantity="0.125")])
    assert kinds(tracker.step([], [])) == [CLOSED]


def test_sizing_survives_a_restart():
    tracker = Tracker()
    tracker.step([stop()], [position(quantity="0.25")])
    revived = Tracker()
    revived.load(tracker.dump())
    revived.step([stop()], [position(quantity="0.125")])
    alerts = revived.step([stop()], [position(quantity="0.125")])
    assert kinds(alerts) == [TRIMMED]
    assert alerts[0].pct == "50"


def test_the_same_asset_again_starts_sizing_from_scratch():
    tracker = Tracker()
    tracker.step([stop()], [position(quantity="1")], now="t1")
    tracker.step([], [], now="t2")                              # closed
    tracker.step([stop()], [position(quantity="0.1")], now="t3")   # new, smaller
    assert tracker.step([stop()], [position(quantity="0.1")], now="t4") == []


def test_seeding_adopts_the_position_size_without_alerting():
    tracker = Tracker()
    assert tracker.step([stop()], [position(quantity="0.25")], seed=True) == []
    tracker.step([stop()], [position(quantity="0.125")])
    assert kinds(tracker.step([stop()], [position(quantity="0.125")])) == [TRIMMED]


def test_a_pulled_stop_is_reported_and_stops_being_advertised():
    """The alert used to keep claiming protection that was no longer there."""
    tracker = Tracker()
    tracker.step([entry(), stop(), take_profit()], [])
    tracker.step([stop(), take_profit()], [position()])
    # Judged a tick late, so a cancel-and-replace is not two alerts.
    assert tracker.step([take_profit()], [position()]) == []
    alerts = tracker.step([take_profit()], [position()])
    assert kinds(alerts) == [LEVELS]
    assert alerts[0].changed == ["stop_gone"]
    assert alerts[0].setup.stop is None
    assert alerts[0].setup.target == "66800"           # the survivor is untouched
    assert tracker.step([take_profit()], [position()]) == []      # said once


def test_a_stop_pulled_before_the_entry_fills_is_reported_too():
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    tracker.step([entry()], [])
    alerts = tracker.step([entry()], [])
    assert kinds(alerts) == [LEVELS]
    assert alerts[0].changed == ["stop_gone"]


def test_cancelling_and_re_adding_reads_as_a_move_not_a_removal():
    """The two legs can straddle a poll; a follower only needs the move."""
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    tracker.step([stop()], [position()])
    tracker.step([], [position()])                     # gone for one tick
    alerts = tracker.step([stop(orderId="s2", triggerPrice="63500")], [position()])
    assert kinds(alerts) == [LEVELS]
    assert alerts[0].changed == ["stop"]
    assert alerts[0].setup.stop == "63500"


def test_re_adding_at_the_same_price_says_nothing():
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    tracker.step([stop()], [position()])
    tracker.step([], [position()])
    assert tracker.step([stop(orderId="s2")], [position()]) == []


def test_a_target_that_filled_is_a_trim_and_never_a_removal():
    """The order leaves the book either way; only the size tells them apart."""
    tracker = Tracker()
    tracker.step([entry(), stop(), take_profit()], [])
    tracker.step([stop(), take_profit()], [position(quantity="0.25")])
    tracker.step([stop()], [position(quantity="0.125")])
    alerts = tracker.step([stop()], [position(quantity="0.125")])
    assert kinds(alerts) == [TRIMMED]                  # no LEVELS alongside it
    assert alerts[0].setup.target is None              # but it stops being shown


def test_a_close_never_reports_the_survivor_as_removed():
    tracker = Tracker()
    tracker.step([entry(), stop(), take_profit()], [])
    tracker.step([stop(), take_profit()], [position()])
    assert kinds(tracker.step([], [])) == [CLOSED]
    assert tracker.setups["BTC:long"].stop == "63100"  # the final embed still reads


def test_a_pending_removal_survives_a_restart():
    tracker = Tracker()
    tracker.step([entry(), stop()], [])
    tracker.step([stop()], [position()])
    tracker.step([], [position()])                     # noticed, not yet judged
    revived = Tracker()
    revived.load(tracker.dump())
    alerts = revived.step([], [position()])
    assert kinds(alerts) == [LEVELS]
    assert alerts[0].changed == ["stop_gone"]


def test_seeding_adopts_a_missing_level_without_alerting():
    tracker = Tracker()
    tracker.step([entry(), stop()], [], seed=True)
    tracker.step([entry()], [], seed=True)
    assert tracker.step([entry()], [], seed=True) == []
