import hashlib
import hmac

import httpx
import pytest

from propr_alerts.render import build_embed
from propr_alerts.source import MexcSource, SourceError, normalise_mexc
from propr_alerts.tracker import CLOSED, FILLED, OPENED, Tracker

# Field names and codes from MEXC's futures API docs.
LIMIT_LONG = {"orderId": "7", "symbol": "SOL_USDT", "side": 1, "orderType": 1,
              "price": 142.5, "vol": 900, "state": 2,
              "stopLossPrice": 138, "takeProfitPrice": 155}
POSITION = {"positionId": 1, "symbol": "SOL_USDT", "positionType": 1,
            "holdVol": 900, "holdAvgPrice": 142.5, "state": 1}
POSITION_STOP = {"id": 3, "symbol": "SOL_USDT", "positionType": 1, "state": 1,
                 "stopLossPrice": 140, "takeProfitPrice": 155, "vol": 900}


def kinds(alerts):
    return [a.kind for a in alerts]


def test_a_mexc_trade_from_entry_to_close():
    tracker = Tracker(venue="MEXC")
    opened = tracker.step(**_book([LIMIT_LONG]))
    assert kinds(opened) == [OPENED]
    setup = opened[0].setup
    assert (setup.asset, setup.side, setup.entry_price) == ("SOL", "long", "142.5")
    assert (setup.stop, setup.target) == ("138", "155")

    filled = tracker.step(**_book(stops=[POSITION_STOP], positions=[POSITION],
                                  fair={"SOL_USDT": "150"}))
    assert FILLED in kinds(filled)
    assert setup.stop == "140"                      # moved to breakeven-ish

    closed = tracker.step(**_book())
    assert kinds(closed) == [CLOSED]
    assert setup.outcome == "up"

    embed = build_embed(setup, CLOSED)
    assert embed.title == "LONG SOL · MEXC"
    assert "900" not in embed.description           # no sizes, ever
    assert "hyperliquid" not in embed.description.lower()


def test_plan_orders_split_into_entries_stops_and_targets():
    plans = [
        {"id": 1, "symbol": "BTC_USDT", "side": 3, "triggerPrice": 60000, "triggerType": 2},
        {"id": 2, "symbol": "BTC_USDT", "side": 2, "triggerPrice": 61500, "triggerType": 1},
        {"id": 3, "symbol": "BTC_USDT", "side": 2, "triggerPrice": 55000, "triggerType": 2},
    ]
    setup = Tracker().step(**_book(plans=plans))[0].setup
    assert (setup.side, setup.entry_price) == ("short", "60000")
    assert (setup.stop, setup.target) == ("61500", "55000")


async def test_requests_are_signed_and_errors_are_not_an_empty_book():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"success": False, "code": 602, "message": "bad sig"})

    source = MexcSource("key", "secret", base_url="http://mexc")
    await source._client.aclose()
    source._client = httpx.AsyncClient(base_url="http://mexc",
                                       transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError, match="602"):
        await source.leader_book()
    await source.aclose()

    request = seen[0]
    stamp = request.headers["Request-Time"]
    expected = hmac.new(b"secret", f"key{stamp}{request.url.query.decode()}".encode(),
                        hashlib.sha256).hexdigest()
    assert request.headers["Signature"] == expected
    assert request.url.query.decode() == "page_num=1&page_size=100&states=2"


def _book(orders=(), stops=(), plans=(), positions=(), fair=None):
    book = normalise_mexc(list(orders), list(stops), list(plans), list(positions), fair or {})
    return {"orders": book.orders, "positions": book.positions}
