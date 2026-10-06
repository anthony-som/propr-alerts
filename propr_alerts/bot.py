"""The Discord bot: subscriptions, fan-out, and the poll loop.

The subscription half is the quantum bot's model, trimmed to one feed: a guild
subscribes one channel, optionally names a role to mention, and owner-only
slash commands manage the list. The trading half is new — every few seconds the
leader's book is diffed and the resulting alerts are posted, then *edited in
place* as the setup fills, is trimmed or added to, cancels or closes, so a
channel shows one message per idea rather than a stack of fragments.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .render import announcement_embed, attachment_name, build_embed, update_line
from .source import CopierSource, MexcSource, SourceError
from .store import Store
from .tracker import (
    ADDED, CANCELLED, CLOSED, FILLED, LEVELS, OPENED, TRIMMED,
    Alert, Setup, Tracker,
)

log = logging.getLogger("propr-alerts")

STATE_KEY = "tracker"
TERMINAL = {CANCELLED, CLOSED}


def alert_key(setup: Setup) -> str:
    """Stable per *idea*: the same asset and side later is a new message."""
    key = f"{setup.key}@{setup.opened_at or 'seed'}"
    return f"{setup.venue}:{key}" if setup.venue else key


@dataclass
class Feed:
    """One account being watched: where its book comes from, and its diff."""

    name: str
    state_key: str
    source: Union[CopierSource, MexcSource]
    tracker: Tracker
    last_error: Optional[str] = None
    last_tick: Optional[str] = None


class Upload(NamedTuple):
    """An attachment held in memory, ready to be sent more than once."""
    name: str
    data: bytes
    is_image: bool


async def load_upload(attachment: discord.Attachment) -> Upload:
    """Read an upload once, up front.

    A `discord.File` wraps a stream that is spent by the first `send`, so the
    same File cannot be handed to a second channel — only the first server
    would get the chart. Holding the bytes lets each channel get its own File.
    """
    content_type = attachment.content_type or ""
    return Upload(
        name=attachment_name(attachment.filename),
        data=await attachment.read(),
        is_image=content_type.startswith("image/"),
    )


class AlertBot(discord.Client):
    """A plain client with a command tree.

    Not `commands.Bot`: that carries the prefix-command machinery, which needs
    the privileged message-content intent and warns on every boot without it.
    This bot only ever posts and answers slash commands, so it asks for nothing
    privileged.
    """

    def __init__(self, config: Config) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.config = config
        self.store = Store(config.database)
        self.feeds = [Feed(
            "Hyperliquid", STATE_KEY,
            CopierSource(config.copier_url, config.copier_user, config.copier_password),
            Tracker(show_outcome=config.show_outcome),
        )]
        if config.mexc_api_key:
            self.feeds.append(Feed(
                "MEXC", f"{STATE_KEY}:mexc",
                MexcSource(config.mexc_api_key, config.mexc_api_secret, config.mexc_risk_pct),
                Tracker(show_outcome=config.show_outcome, venue="MEXC"),
            ))
        self.alerts_sent = 0
        # alert key → guild → the latest follow-up reply, deleted by the next.
        # ponytail: in memory, so a restart mid-trade leaves one old reply up;
        # keep it in the store's kv table if that ever matters.
        self.last_followup: Dict[str, Dict[str, discord.Message]] = {}

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        await self.store.setup()
        for feed in self.feeds:
            saved = await self.store.get(feed.state_key)
            if saved:
                feed.tracker.load(saved)
        register_commands(self)
        await self.tree.sync()
        self.poll.change_interval(seconds=self.config.poll_seconds)
        self.poll.start()

    async def on_ready(self) -> None:
        log.info("connected as %s, %d guild(s)", self.user, len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name=" + ".join(feed.name for feed in self.feeds)
            )
        )
        # Older versions copied every global command into each guild, which
        # made Discord show two of everything once global propagation caught
        # up. Remove those legacy guild-scoped copies; global commands remain.
        for guild in self.guilds:
            await self.clear_guild_commands(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("joined %s (%s)", guild.name, guild.id)

    async def clear_guild_commands(self, guild: discord.Guild) -> None:
        """Delete legacy guild copies so only global commands are displayed."""
        try:
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
        except discord.DiscordException as exc:
            log.warning("command cleanup failed for %s — %s", guild.name, exc)

    async def close(self) -> None:
        self.poll.cancel()
        for feed in self.feeds:
            await feed.source.aclose()
        await super().close()

    # ----------------------------------------------------------- poll loop
    @tasks.loop(seconds=3.0)
    async def poll(self) -> None:
        for feed in self.feeds:
            await self.poll_feed(feed)

    async def poll_feed(self, feed: Feed) -> None:
        try:
            book = await feed.source.leader_book()
        except SourceError as exc:
            # Never invent alerts from a failed read: an unreachable source is
            # not an empty book.
            if str(exc) != feed.last_error:
                log.warning("%s source: %s", feed.name, exc)
            feed.last_error = str(exc)
            return

        feed.last_error = None
        now = discord.utils.utcnow().isoformat(timespec="seconds")
        feed.last_tick = now
        seed = not feed.tracker.seeded and not self.config.seed_alerts
        alerts = feed.tracker.step(
            book.orders,
            book.positions,
            now=now,
            seed=seed,
            risk_pct=book.risk_pct,
        )

        for alert in alerts:
            try:
                await self.publish(alert)
            except Exception:                      # one bad guild must not stop the rest
                log.exception("publishing %s failed", alert.setup.key)

        if alerts or seed:
            await self.store.put(feed.state_key, feed.tracker.dump())

    @poll.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    # ------------------------------------------------------------- fan-out
    async def publish(self, alert: Alert) -> None:
        setup, kind = alert.setup, alert.kind
        key = alert_key(setup)
        embed = build_embed(setup, kind)
        known = await self.store.messages_for(key)

        if kind == OPENED or not known:
            await self.broadcast_new(key, embed, mention=True)
        else:
            await self.edit_all(key, known, embed)
            # Every transition gets a reply, moved levels included — an edit on
            # its own is silent, and a moved stop is exactly what a follower
            # needs to see.
            if kind in (FILLED, CANCELLED, CLOSED, LEVELS, TRIMMED, ADDED):
                await self.broadcast_followup(
                    key, known, update_line(setup, kind, alert.changed, alert.pct)
                )

        if kind in TERMINAL:
            await self.store.forget_messages(key)
            self.last_followup.pop(key, None)
        self.alerts_sent += 1

    async def broadcast_new(self, key: str, embed: discord.Embed, mention: bool) -> None:
        for guild_id, details in (await self.store.subscriptions()).items():
            channel = self.get_channel(int(details["channel"]))
            if channel is None:
                log.warning("guild %s: channel %s not visible", guild_id, details["channel"])
                continue
            content = f"<@&{details['role']}>" if (mention and details.get("role")) else None
            try:
                sent = await channel.send(content=content, embed=embed)
            except discord.DiscordException as exc:
                log.warning("guild %s: send failed — %s", guild_id, exc)
                continue
            await self.store.remember_message(
                key, guild_id, str(channel.id), str(sent.id)
            )

    async def broadcast_manual(
        self,
        text: str = "",
        heading: str = "",
        upload: Optional[Upload] = None,
        mention: bool = False,
        plain: bool = False,
    ) -> Tuple[int, List[str]]:
        """Post something you wrote to every subscribed channel.

        The alert path builds its own message out of a `Setup`; this one
        carries whatever you hand it — a PnL card, a chart, a note — through
        the same fan-out, so a manual post lands in the same channels wearing
        the same branding. Nothing is remembered afterwards: there is no setup
        for it to belong to, so it is never edited or replied to later.

        Returns how many channels took it, and the guilds that did not.
        """
        sent, failed = 0, []
        for guild_id, details in (await self.store.subscriptions()).items():
            channel = self.get_channel(int(details["channel"]))
            if channel is None:
                log.warning("guild %s: channel %s not visible", guild_id, details["channel"])
                failed.append(guild_id)
                continue

            lines = []
            if mention and details.get("role"):
                lines.append(f"<@&{details['role']}>")
            if plain:
                if heading:
                    lines.append(f"**{heading}**")
                if text:
                    lines.append(text)

            kwargs: dict = {
                # A role ping is the one mention a manual post may make; an
                # @everyone typed into the message body must not go through.
                "allowed_mentions": discord.AllowedMentions(
                    everyone=False, users=False, roles=mention
                ),
            }
            if lines:
                kwargs["content"] = "\n".join(lines)
            if upload:
                kwargs["file"] = discord.File(io.BytesIO(upload.data), filename=upload.name)
            if not plain:
                kwargs["embed"] = announcement_embed(
                    text=text,
                    heading=heading,
                    # A non-image attachment has nothing to show inside the
                    # embed, so it rides along as a plain file instead.
                    image_filename=upload.name if upload and upload.is_image else "",
                )

            try:
                await channel.send(**kwargs)
            except discord.DiscordException as exc:
                log.warning("guild %s: manual send failed — %s", guild_id, exc)
                failed.append(guild_id)
                continue
            sent += 1
        return sent, failed

    async def edit_all(self, key: str, known, embed: discord.Embed) -> None:
        for guild_id, channel_id, message_id in known:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(embed=embed)
            except discord.DiscordException as exc:
                log.warning("guild %s: edit failed — %s", guild_id, exc)

    async def broadcast_followup(self, key: str, known, line: str) -> None:
        """Reply to each original setup, pinging the subscribed role.

        An edit alone is silent, and a reply on its own only notifies whoever
        is already watching the thread — but a filled entry or a moved stop is
        news for everyone the opening alert pinged, so the role rides along
        here too.

        Each reply replaces the setup's previous one, so a busy trade leaves
        one update in the channel rather than a stack of them. Nothing is lost:
        the embed has already been edited to where the trade stands now.
        """
        subscriptions = await self.store.subscriptions()
        replies = self.last_followup.setdefault(key, {})
        for guild_id, channel_id, message_id in known:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                continue
            role = subscriptions.get(guild_id, {}).get("role")
            try:
                original = await channel.fetch_message(int(message_id))
                sent = await original.reply(
                    f"<@&{role}> {line}" if role else line,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, users=False, roles=bool(role)
                    ),
                )
            except discord.DiscordException as exc:
                # The old reply stays up, and tracked, rather than leave the
                # setup with no update under it at all.
                log.warning("guild %s: follow-up failed — %s", guild_id, exc)
                continue
            stale, replies[guild_id] = replies.get(guild_id), sent
            if stale:
                try:
                    await stale.delete()
                except discord.DiscordException as exc:
                    log.warning("guild %s: removing old follow-up failed — %s", guild_id, exc)


# --------------------------------------------------------------- commands
def register_commands(bot: AlertBot) -> None:
    config = bot.config

    def is_owner():
        async def predicate(interaction: discord.Interaction) -> bool:
            return interaction.user.id in config.owner_ids
        return app_commands.check(predicate)

    @bot.tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        message = ("You don't have permission to use this command."
                   if isinstance(error, app_commands.CheckFailure) else f"❌ {error}")
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @bot.tree.command(name="subscribe", description="Set the alert channel and mention role")
    @is_owner()
    async def subscribe(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: Optional[discord.Role] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        await bot.store.subscribe(
            str(interaction.guild_id),
            channel_id=str(channel.id),
            role_id=str(role.id) if role else None,
        )
        role_text = f" and mention {role.mention}" if role else ""
        await interaction.followup.send(
            f"Alerts will post in {channel.mention}{role_text}.", ephemeral=True
        )

    @bot.tree.command(name="subscribe_id", description="Subscribe by guild/channel ID")
    @is_owner()
    async def subscribe_id(interaction: discord.Interaction, guild_id: str, channel_id: str):
        await interaction.response.defer(ephemeral=True)
        await bot.store.subscribe(guild_id, channel_id=channel_id)
        await interaction.followup.send(
            f"Subscribed channel `{channel_id}` in guild `{guild_id}`.", ephemeral=True)

    @bot.tree.command(name="unsubscribe", description="Stop alerts in this server")
    @is_owner()
    async def unsubscribe(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await bot.store.unsubscribe(str(interaction.guild_id))
        await interaction.followup.send("This server has been unsubscribed.", ephemeral=True)

    @bot.tree.command(name="admin_unsubscribe", description="Unsubscribe a guild by ID")
    @is_owner()
    async def admin_unsubscribe(interaction: discord.Interaction, guild_id: str):
        await interaction.response.defer(ephemeral=True)
        await bot.store.unsubscribe(guild_id)
        await interaction.followup.send(f"Guild `{guild_id}` unsubscribed.", ephemeral=True)

    @bot.tree.command(name="servers", description="List subscribed servers")
    @is_owner()
    async def servers(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await bot.store.all_subscriptions()
        if not rows:
            await interaction.followup.send("No subscriptions yet.", ephemeral=True)
            return
        lines = []
        for row in rows:
            guild = bot.get_guild(int(row["guild_id"]))
            channel = bot.get_channel(int(row["channel_id"])) if row["channel_id"] else None
            role = f" · <@&{row['role_id']}>" if row["role_id"] else ""
            lines.append(
                f"**{guild.name if guild else row['guild_id']}** — "
                f"{channel.mention if channel else '`no channel`'}{role}"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @bot.tree.command(name="status", description="Bot and copier health")
    @is_owner()
    async def status(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lines = []
        for feed in bot.feeds:
            healthy = await feed.source.healthy()
            live = [s for s in feed.tracker.setups.values() if s.live]
            lines += [
                f"**{feed.name}**: {'🟢 reachable' if healthy else '🔴 unreachable'}"
                f" ({feed.source.base_url})",
                f"last read: {feed.last_tick or 'never'}",
                f"last error: {feed.last_error or 'none'}",
                f"live setups: {len(live)}",
            ]
            lines += [
                f"• {s.asset} {s.side} — {s.state}"
                + (f" ({s.closed_pct}% closed)" if s.closed_pct else "")
                for s in live
            ]
            lines.append("")
        lines.append(f"alerts sent: {bot.alerts_sent}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @bot.tree.command(
        name="announce", description="Post your own message or chart to every server"
    )
    @app_commands.describe(
        message="What to say. Type \\n for a line break.",
        image="A chart, PnL card or screenshot to post",
        heading="Optional title above the message",
        mention="Ping the subscribed role (off by default)",
        plain="Post as a plain message instead of the branded embed",
        preview="Show it to you only, without sending it anywhere",
    )
    @is_owner()
    async def announce(
        interaction: discord.Interaction,
        message: Optional[app_commands.Range[str, 1, 3500]] = None,
        image: Optional[discord.Attachment] = None,
        heading: Optional[app_commands.Range[str, 1, 200]] = None,
        mention: bool = False,
        plain: bool = False,
        preview: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)
        if not message and not image:
            await interaction.followup.send(
                "Give me a `message`, an `image`, or both.", ephemeral=True)
            return

        # A slash-command box cannot hold a real newline, so the escape that
        # can be typed into one is honoured here.
        text = (message or "").replace("\\n", "\n")
        upload = None
        if image:
            try:
                upload = await load_upload(image)
            except discord.DiscordException as exc:
                await interaction.followup.send(
                    f"❌ Couldn't read that attachment — {exc}", ephemeral=True)
                return

        if preview:
            # Rendered exactly as the servers will see it, minus the ping, so a
            # chart can be checked before it is broadcast.
            kwargs: dict = {"ephemeral": True}
            if upload:
                kwargs["file"] = discord.File(io.BytesIO(upload.data), filename=upload.name)
            if plain:
                kwargs["content"] = "\n".join(
                    part for part in (f"**{heading}**" if heading else "", text) if part
                ) or "(image only)"
            else:
                kwargs["embed"] = announcement_embed(
                    text=text,
                    heading=heading or "",
                    image_filename=upload.name if upload and upload.is_image else "",
                )
            await interaction.followup.send(**kwargs)
            return

        sent, failed = await bot.broadcast_manual(
            text=text,
            heading=heading or "",
            upload=upload,
            mention=mention,
            plain=plain,
        )
        note = f"Posted to {sent} server{'' if sent == 1 else 's'}."
        if failed:
            note += f" {len(failed)} couldn't be reached: {', '.join(failed)}"
        if not sent and not failed:
            note = "Nowhere to post — no server has subscribed a channel yet."
        await interaction.followup.send(note, ephemeral=True)

    @bot.tree.command(name="preview", description="Post a sample alert here")
    @is_owner()
    async def preview(interaction: discord.Interaction):
        sample = Setup(
            key="BTC:long", asset="BTC", side="long", state="working",
            entry_type="limit", entry_price="64250.5", stop="63100", target="66800",
        )
        await interaction.response.send_message(
            embed=build_embed(sample, OPENED), ephemeral=True)

    @bot.tree.command(name="invite", description="Invite link for this bot")
    @is_owner()
    async def invite(interaction: discord.Interaction):
        url = discord.utils.oauth_url(
            bot.user.id,
            permissions=discord.Permissions(
                send_messages=True, embed_links=True, attach_files=True,
                read_message_history=True, view_channel=True,
            ),
            scopes=("bot", "applications.commands"),
        )
        await interaction.response.send_message(url, ephemeral=True)

    @bot.tree.command(name="ping", description="Latency check")
    @is_owner()
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message(
            f"🏓 {round(bot.latency * 1000)}ms", ephemeral=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # One line per poll would be a line every few seconds, for ever.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.from_env()
    missing = config.missing()
    if missing:
        raise SystemExit(f"Missing required environment: {', '.join(missing)}")
    bot = AlertBot(config)
    asyncio.run(bot.start(config.discord_token))


if __name__ == "__main__":
    main()
