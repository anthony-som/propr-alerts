"""The subscription table — the part cloned from the quantum bot."""
import pytest

from propr_alerts.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "alerts.db")
    await s.setup()
    return s


async def test_a_guild_subscribes_a_channel(store):
    await store.subscribe("g1", channel_id="c1")
    assert await store.subscriptions() == {"g1": {"channel": "c1", "role": None}}


async def test_setting_a_role_keeps_the_channel(store):
    """The two commands are separate, so neither may clobber the other."""
    await store.subscribe("g1", channel_id="c1")
    await store.subscribe("g1", role_id="r1")
    assert await store.subscriptions() == {"g1": {"channel": "c1", "role": "r1"}}


async def test_a_guild_with_no_channel_is_not_a_destination(store):
    await store.subscribe("g1", role_id="r1")
    assert await store.subscriptions() == {}
    assert len(await store.all_subscriptions()) == 1


async def test_unsubscribing_forgets_the_posted_messages_too(store):
    await store.subscribe("g1", channel_id="c1")
    await store.remember_message("BTC:long@t1", "g1", "c1", "m1")
    await store.unsubscribe("g1")
    assert await store.subscriptions() == {}
    assert await store.messages_for("BTC:long@t1") == []


async def test_messages_are_tracked_per_guild_for_editing(store):
    await store.remember_message("BTC:long@t1", "g1", "c1", "m1")
    await store.remember_message("BTC:long@t1", "g2", "c2", "m2")
    assert sorted(await store.messages_for("BTC:long@t1")) == [
        ("g1", "c1", "m1"), ("g2", "c2", "m2"),
    ]
    await store.forget_messages("BTC:long@t1")
    assert await store.messages_for("BTC:long@t1") == []


async def test_reposting_the_same_alert_replaces_the_message_id(store):
    await store.remember_message("BTC:long@t1", "g1", "c1", "m1")
    await store.remember_message("BTC:long@t1", "g1", "c1", "m9")
    assert await store.messages_for("BTC:long@t1") == [("g1", "c1", "m9")]


async def test_tracker_state_round_trips(store):
    assert await store.get("tracker") is None
    await store.put("tracker", '{"seeded": true}')
    await store.put("tracker", '{"seeded": false}')
    assert await store.get("tracker") == '{"seeded": false}'
