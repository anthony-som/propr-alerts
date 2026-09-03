"""The no-money rule, enforced end to end.

A book is fed through the tracker with sizes, leverage and a PnL figure on it,
every resulting alert is rendered, and the rendered text is searched for those
numbers. Any of them surfacing is the bug this file exists to catch.
"""
from propr_alerts.render import build_embed, update_line
from propr_alerts.tracker import CLOSED, FILLED, OPENED, Setup, Tracker

MONEY = ("0.37", "23750", "9814.7", "184.2", "184.20", "12.5")


def book_with_money():
    order = {
        "orderId": "e1", "asset": "ETH", "type": "limit", "side": "buy",
        "quantity": "0.37", "price": "3125.5", "reduceOnly": False,
        "notional": "23750", "status": "open",
    }
    stop = {
        "orderId": "s1", "asset": "ETH", "type": "stop_market", "side": "sell",
        "quantity": "0.37", "triggerPrice": "2990", "reduceOnly": True,
    }
    position = {
        "asset": "ETH", "positionSide": "long", "quantity": "0.37",
        "entryPrice": "3125.5", "leverage": "12.5", "unrealizedPnl": "184.20",
        "notional": "23750",
    }
    return order, stop, position


def rendered(alerts):
    out = []
    for alert in alerts:
        embed = build_embed(alert.setup, alert.kind)
        out.append(embed.title or "")
        out.append(embed.description or "")
        out.append((embed.footer.text or "") if embed.footer else "")
        out.append(update_line(alert.setup, alert.kind))
    return "\n".join(out)


def test_no_size_leverage_or_pnl_reaches_discord():
    order, stop, position = book_with_money()
    tracker = Tracker()
    text = rendered(tracker.step([order, stop], []))
    text += rendered(tracker.step([stop], [position]))
    text += rendered(tracker.step([], []))

    for figure in MONEY:
        assert figure not in text, f"{figure} leaked into the alert"


def test_prices_and_levels_do_reach_discord():
    order, stop, position = book_with_money()
    tracker = Tracker()
    text = rendered(tracker.step([order, stop], []))
    assert "3,125.5" in text          # entry, thousands-separated
    assert "2,990" in text            # stop
    assert "ETH" in text
    assert "LONG" in text


def test_the_title_is_the_direction_and_ticker_alone():
    setup = Setup(key="BTC:short", asset="BTC", side="short")
    assert build_embed(setup, OPENED).title == "SHORT BTC"


def test_the_body_carries_entry_stop_target_then_status():
    setup = Setup(
        key="BTC:short", asset="BTC", side="short", state="working",
        entry_type="limit", entry_price="64250.5", stop="65100", target="61800",
    )
    lines = build_embed(setup, OPENED).description.splitlines()
    assert lines[0] == "**Entry** 64,250.5 (limit)"
    assert lines[1] == "**Stop** 65,100"
    assert lines[2] == "**Target** 61,800"
    assert lines[-1] == "⏳ Working"


def test_a_missing_level_renders_as_a_dash_not_none():
    setup = Setup(key="SOL:long", asset="SOL", side="long", entry_type="market")
    lines = build_embed(setup, FILLED).description.splitlines()
    assert lines[1] == "**Stop** —"
    assert lines[2] == "**Target** —"


def test_a_filled_setup_shows_the_fill_price_as_the_entry():
    setup = Setup(
        key="BTC:long", asset="BTC", side="long", state="filled",
        entry_type="limit", entry_price="64250.5", fill_price="64251",
    )
    body = build_embed(setup, FILLED).description
    assert "**Entry** 64,251 (filled)" in body
    assert "✅ Filled" in body


def test_a_closed_setup_says_how_it_ended_without_a_figure():
    setup = Setup(key="BTC:long", asset="BTC", side="long", state="closed", outcome="up")
    body = build_embed(setup, CLOSED).description
    assert "🏁 Closed in profit" in body


def test_a_closed_trade_says_direction_only():
    setup = Setup(key="BTC:long", asset="BTC", side="long", state="closed", outcome="down")
    line = update_line(setup, CLOSED)
    assert "at a loss" in line
    assert not any(character.isdigit() for character in line)
