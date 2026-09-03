import httpx

from propr_alerts.source import CopierSource


async def test_book_uses_the_copiers_follower_risk_remap():
    def handler(request):
        if request.url.path == "/api/accounts/snapshot":
            return httpx.Response(200, json={
                "leader": {"orders": [{"asset": "BTC"}], "positions": []},
            })
        if request.url.path == "/api/state":
            return httpx.Response(200, json={
                "status": {"risk": {
                    "enabled": True, "leaderPct": "10", "followerPct": "1",
                }},
            })
        return httpx.Response(404)

    source = CopierSource("http://copier", "", "")
    await source._client.aclose()
    source._client = httpx.AsyncClient(
        base_url="http://copier", transport=httpx.MockTransport(handler)
    )
    try:
        book = await source.leader_book()
    finally:
        await source.aclose()

    assert book.orders == [{"asset": "BTC"}]
    assert book.risk_pct == "1"


async def test_book_omits_risk_when_remapping_is_disabled():
    def handler(request):
        if request.url.path == "/api/accounts/snapshot":
            return httpx.Response(200, json={"leader": {}})
        return httpx.Response(200, json={
            "status": {"risk": {"enabled": False, "followerPct": "1"}},
        })

    source = CopierSource("http://copier", "", "")
    await source._client.aclose()
    source._client = httpx.AsyncClient(
        base_url="http://copier", transport=httpx.MockTransport(handler)
    )
    try:
        book = await source.leader_book()
    finally:
        await source.aclose()

    assert book.risk_pct is None
