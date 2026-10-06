from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import discord

from propr_alerts.bot import AlertBot, Upload, register_commands
from propr_alerts.config import Config


class Reply:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class OriginalMessage:
    def __init__(self):
        self.replies = []

    async def reply(self, content, **kwargs):
        self.replies.append((content, kwargs))
        return Reply()


class Channel:
    def __init__(self, original):
        self.original = original
        self.fetched = []

    async def fetch_message(self, message_id):
        self.fetched.append(message_id)
        return self.original


class FollowupBot:
    def __init__(self, channel, role="99"):
        self.channel = channel
        self.store = SimpleNamespace(
            subscriptions=self._subscriptions,
        )
        self.role = role
        self.last_followup = {}

    async def _subscriptions(self):
        return {"guild": {"channel": "22", "role": self.role}}

    def get_channel(self, channel_id):
        assert channel_id == 22
        return self.channel


async def test_trade_management_replies_to_the_original_alert():
    original = OriginalMessage()
    channel = Channel(original)

    await AlertBot.broadcast_followup(
        FollowupBot(channel), "BTC:long@t", [("guild", "22", "33")], "✏️ Stop → 63,500"
    )

    assert channel.fetched == [33]
    content, kwargs = original.replies[0]
    # The role is pinged again: an edit is silent, and a reply alone only
    # reaches whoever is already watching.
    assert content == "<@&99> ✏️ Stop → 63,500"
    assert kwargs["mention_author"] is False
    assert kwargs["allowed_mentions"].roles is True
    assert kwargs["allowed_mentions"].everyone is False


async def test_follow_ups_without_a_role_stay_unmentioned():
    original = OriginalMessage()
    channel = Channel(original)

    await AlertBot.broadcast_followup(
        FollowupBot(channel, role=None), "BTC:long@t", [("guild", "22", "33")], "✅ Filled"
    )

    content, kwargs = original.replies[0]
    assert content == "✅ Filled"
    assert kwargs["allowed_mentions"].roles is False


async def test_a_new_follow_up_replaces_the_last_one():
    bot = FollowupBot(Channel(OriginalMessage()))
    known = [("guild", "22", "33")]

    await AlertBot.broadcast_followup(bot, "BTC:long@t", known, "✅ Filled")
    first = bot.last_followup["BTC:long@t"]["guild"]
    await AlertBot.broadcast_followup(bot, "BTC:long@t", known, "✂️ Trimmed 50%")
    second = bot.last_followup["BTC:long@t"]["guild"]

    # One update per trade stays in the channel: the embed already shows
    # where it stands, so the older line has nothing left to say.
    assert first.deleted is True
    assert second is not first and second.deleted is False


async def test_every_command_is_owner_only(tmp_path):
    bot = AlertBot(Config(owner_ids={123}, database=tmp_path / "alerts.db"))
    register_commands(bot)
    try:
        commands = bot.tree.get_commands()
        assert commands
        assert all(command.checks for command in commands)
        assert bot.tree.get_command("subscribe_role") is None
        subscribe = bot.tree.get_command("subscribe")
        assert [parameter.name for parameter in subscribe.parameters] == ["channel", "role"]
        assert subscribe.parameters[1].required is False
    finally:
        for feed in bot.feeds:
            await feed.source.aclose()


async def test_legacy_guild_commands_are_cleared():
    guild = object()

    class Tree:
        def __init__(self):
            self.cleared = []
            self.synced = []

        def clear_commands(self, *, guild):
            self.cleared.append(guild)

        async def sync(self, *, guild):
            self.synced.append(guild)

    class Bot:
        tree = Tree()

    bot = Bot()
    await AlertBot.clear_guild_commands(bot, guild)
    assert bot.tree.cleared == [guild]
    assert bot.tree.synced == [guild]


class Sent:
    """A channel that records every send, as the arguments it was given."""

    def __init__(self, fail=False):
        self.sends = []
        self.fail = fail

    async def send(self, **kwargs):
        if self.fail:
            raise discord.DiscordException("missing permissions")
        self.sends.append(kwargs)


def manual_bot(subscriptions, channels):
    class Store:
        async def subscriptions(self):
            return subscriptions

    class Bot:
        store = Store()

        def get_channel(self, channel_id):
            return channels.get(channel_id)

    return Bot()


async def test_announce_reaches_every_subscribed_channel():
    one, two = Sent(), Sent()
    bot = manual_bot(
        {"g1": {"channel": "11", "role": "99"}, "g2": {"channel": "22", "role": None}},
        {11: one, 22: two},
    )

    sent, failed = await AlertBot.broadcast_manual(
        bot, text="August closed +18R", mention=True)

    assert (sent, failed) == (2, [])
    # Only the guild that named a role gets a ping; the other is silent.
    assert one.sends[0]["content"] == "<@&99>"
    assert "content" not in two.sends[0]
    assert "August closed +18R" in one.sends[0]["embed"].description


async def test_announce_gives_each_channel_its_own_copy_of_the_image():
    """One File is spent by the first send, so a shared one would arrive blank."""
    one, two = Sent(), Sent()
    bot = manual_bot(
        {"g1": {"channel": "11"}, "g2": {"channel": "22"}}, {11: one, 22: two})
    upload = Upload(name="pnl.png", data=b"\x89PNG chart", is_image=True)

    await AlertBot.broadcast_manual(bot, text="", upload=upload)

    files = [one.sends[0]["file"], two.sends[0]["file"]]
    assert files[0] is not files[1]
    assert [f.fp.read() for f in files] == [b"\x89PNG chart", b"\x89PNG chart"]
    assert one.sends[0]["embed"].image.url == "attachment://pnl.png"


async def test_a_non_image_attachment_is_not_shown_inside_the_embed():
    channel = Sent()
    bot = manual_bot({"g1": {"channel": "11"}}, {11: channel})

    await AlertBot.broadcast_manual(
        bot, text="Statement", upload=Upload("august.pdf", b"%PDF", is_image=False))

    assert channel.sends[0]["file"].filename == "august.pdf"
    assert not channel.sends[0]["embed"].image.url


async def test_announce_never_lets_an_at_everyone_through():
    channel = Sent()
    bot = manual_bot({"g1": {"channel": "11", "role": "99"}}, {11: channel})

    await AlertBot.broadcast_manual(bot, text="@everyone read this", mention=True)

    mentions = channel.sends[0]["allowed_mentions"]
    assert mentions.everyone is False and mentions.roles is True


async def test_plain_announce_skips_the_embed():
    channel = Sent()
    bot = manual_bot({"g1": {"channel": "11"}}, {11: channel})

    await AlertBot.broadcast_manual(bot, text="quick note", heading="Heads up", plain=True)

    assert "embed" not in channel.sends[0]
    assert channel.sends[0]["content"] == "**Heads up**\nquick note"


async def test_one_unreachable_channel_does_not_stop_the_rest():
    good, bad = Sent(), Sent(fail=True)
    bot = manual_bot(
        {"g1": {"channel": "11"}, "g2": {"channel": "22"}, "g3": {"channel": "33"}},
        {11: good, 22: bad},          # 33 is a channel the bot cannot see
    )

    sent, failed = await AlertBot.broadcast_manual(bot, text="hello")

    assert sent == 1
    assert sorted(failed) == ["g2", "g3"]
    assert len(good.sends) == 1


async def test_the_invite_asks_for_attach_files(tmp_path):
    """`/announce` posts charts, which a bot without Attach Files cannot."""
    bot = AlertBot(Config(owner_ids={1}, database=tmp_path / "alerts.db"))
    register_commands(bot)

    class Response:
        sent = None

        def is_done(self):
            return False

        async def send_message(self, content, **kwargs):
            Response.sent = content

    try:
        with patch.object(discord.Client, "user", SimpleNamespace(id=42)):
            await bot.tree.get_command("invite").callback(
                SimpleNamespace(response=Response()))
    finally:
        for feed in bot.feeds:
            await feed.source.aclose()

    granted = parse_qs(urlparse(Response.sent).query)["permissions"][0]
    assert discord.Permissions(int(granted)).attach_files
