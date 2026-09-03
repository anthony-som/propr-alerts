"""The Discord bot: subscriptions, fan-out, and the poll loop.

The subscription half is the quantum bot's model, trimmed to one feed: a guild
subscribes one channel, optionally names a role to mention, and owner-only
slash commands manage the list. The trading half is new — every few seconds the
leader's book is diffed and the resulting alerts are posted, then *edited in
place* as the setup fills, cancels or closes, so a channel shows one message per
idea rather than a stack of fragments.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .render import build_embed, update_line
from .source import CopierSource, SourceError
from .store import Store
from .tracker import CANCELLED, CLOSED, FILLED, LEVELS, OPENED, Alert, Setup, Tracker

log = logging.getLogger("propr-alerts")

STATE_KEY = "tracker"
TERMINAL = {CANCELLED, CLOSED}


def alert_key(setup: Setup) -> str:
    """Stable per *idea*: the same asset and side later is a new message."""
    return f"{setup.key}@{setup.opened_at or 'seed'}"


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
        self.source = CopierSource(
            config.copier_url, config.copier_user, config.copier_password
        )
        self.tracker = Tracker(show_outcome=config.show_outcome)
        self.last_error: Optional[str] = None
        self.last_tick: Optional[str] = None
        self.alerts_sent = 0

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        await self.store.setup()
        saved = await self.store.get(STATE_KEY)
        if saved:
            self.tracker.load(saved)
        register_commands(self)
        await self.tree.sync()
        self.poll.change_interval(seconds=self.config.poll_seconds)
        self.poll.start()

    async def on_ready(self) -> None:
        log.info("connected as %s, %d guild(s)", self.user, len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="Hyperliquid"
            )
        )
        for guild in self.guilds:
            await self.sync_guild(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("joined %s (%s)", guild.name, guild.id)
        await self.sync_guild(guild)

    async def sync_guild(self, guild: discord.Guild) -> None:
        """Copy the commands into one guild so they appear immediately.

        A global sync is the right long-term home for them, but Discord can take
        up to an hour to push global commands to clients — long enough to look
        like the bot never came up. A guild sync lands straight away.
        """
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d command(s) to %s", len(synced), guild.name)
        except discord.DiscordException as exc:
            log.warning("command sync failed for %s — %s", guild.name, exc)

    async def close(self) -> None:
        self.poll.cancel()
        await self.source.aclose()
        await super().close()

    # ----------------------------------------------------------- poll loop
    @tasks.loop(seconds=3.0)
    async def poll(self) -> None:
        try:
            book = await self.source.leader_book()
        except SourceError as exc:
            # Never invent alerts from a failed read: an unreachable copier is
            # not an empty book.
            if str(exc) != self.last_error:
                log.warning("source: %s", exc)
            self.last_error = str(exc)
            return

        self.last_error = None
        now = discord.utils.utcnow().isoformat(timespec="seconds")
        self.last_tick = now
        seed = not self.tracker.seeded and not self.config.seed_alerts
        alerts = self.tracker.step(book.orders, book.positions, now=now, seed=seed)

        for alert in alerts:
            try:
                await self.publish(alert)
            except Exception:                      # one bad guild must not stop the rest
                log.exception("publishing %s failed", alert.setup.key)

        if alerts or seed:
            await self.store.put(STATE_KEY, self.tracker.dump())

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
            if kind in (FILLED, CANCELLED, CLOSED, LEVELS):
                await self.broadcast_followup(
                    known, update_line(setup, kind, alert.changed)
                )

        if kind in TERMINAL:
            await self.store.forget_messages(key)
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

    async def broadcast_followup(self, known, line: str) -> None:
        """A short line under the setup, because an edit alone is silent."""
        for _guild_id, channel_id, message_id in known:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                continue
            reference = None
            try:
                reference = await channel.fetch_message(int(message_id))
            except discord.DiscordException:
                pass
            try:
                await channel.send(
                    line,
                    reference=reference,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.DiscordException as exc:
                log.warning("follow-up failed — %s", exc)


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

    @bot.tree.command(name="subscribe", description="Post trade alerts in a channel")
    @is_owner()
    async def subscribe(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        await bot.store.subscribe(str(interaction.guild_id), channel_id=str(channel.id))
        await interaction.followup.send(f"Alerts will post in {channel.mention}.", ephemeral=True)

    @bot.tree.command(name="subscribe_role", description="Role to mention on a new setup")
    @is_owner()
    async def subscribe_role(interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        await bot.store.subscribe(str(interaction.guild_id), role_id=str(role.id))
        await interaction.followup.send(f"{role.mention} will be mentioned.", ephemeral=True)

    @bot.tree.command(name="subscribe_id", description="Subscribe by guild/channel ID")
    @is_owner()
    async def subscribe_id(interaction: discord.Interaction, guild_id: str, channel_id: str):
        await interaction.response.defer(ephemeral=True)
        await bot.store.subscribe(guild_id, channel_id=channel_id)
        await interaction.followup.send(
            f"Subscribed channel `{channel_id}` in guild `{guild_id}`.", ephemeral=True)

    @bot.tree.command(name="unsubscribe", description="Stop alerts in this server")
    async def unsubscribe(interaction: discord.Interaction):
        if not (interaction.user.id in config.owner_ids
                or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message(
                "You need to be an administrator to use this command.", ephemeral=True)
            return
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
        healthy = await bot.source.healthy()
        live = [s for s in bot.tracker.setups.values() if s.live]
        lines = [
            f"copier: {'🟢 reachable' if healthy else '🔴 unreachable'} ({config.copier_url})",
            f"last read: {bot.last_tick or 'never'}",
            f"last error: {bot.last_error or 'none'}",
            f"live setups: {len(live)}",
            f"alerts sent: {bot.alerts_sent}",
        ]
        if live:
            lines.append("")
            lines += [f"• {s.asset} {s.side} — {s.state}" for s in live]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

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
                send_messages=True, embed_links=True,
                read_message_history=True, view_channel=True,
            ),
            scopes=("bot", "applications.commands"),
        )
        await interaction.response.send_message(url, ephemeral=True)

    @bot.tree.command(name="ping", description="Latency check")
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
