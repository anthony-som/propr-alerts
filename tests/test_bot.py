from propr_alerts.bot import AlertBot


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
