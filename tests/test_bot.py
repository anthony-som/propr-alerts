from propr_alerts.bot import AlertBot, register_commands
from propr_alerts.config import Config


class OriginalMessage:
    def __init__(self):
        self.replies = []

    async def reply(self, content, **kwargs):
        self.replies.append((content, kwargs))


class Channel:
    def __init__(self, original):
        self.original = original
        self.fetched = []

    async def fetch_message(self, message_id):
        self.fetched.append(message_id)
        return self.original


async def test_trade_management_replies_to_the_original_alert():
    original = OriginalMessage()
    channel = Channel(original)

    class Bot:
        def get_channel(self, channel_id):
            assert channel_id == 22
            return channel

    await AlertBot.broadcast_followup(Bot(), [("guild", "22", "33")], "✏️ Stop → 63,500")

    assert channel.fetched == [33]
    assert original.replies[0][0] == "✏️ Stop → 63,500"
    assert original.replies[0][1]["mention_author"] is False


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
