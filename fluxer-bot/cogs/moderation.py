"""
cogs/moderation.py
------------------
Moderation commands and automated protection systems.

Commands (all require the relevant Fluxer permission):
    Member actions:
        !kick @user [reason]             - Kick a member
        !ban @user [reason]              - Ban a member
        !unban <user_id> [reason]        - Unban by user ID
        !timeout @user <time> [reason]   - Timeout a member for a duration
        !untimeout @user                 - Remove a timeout early
        !purge <count>                   - Delete last N messages (max 100)

    Mod log:
        !setmodlog #channel              - Set the mod log channel
        !setmodlog                       - Clear the mod log channel

    Auto-mod (banned words):
        !automod addword <word>          - Add a banned word
        !automod removeword <word>       - Remove a banned word
        !automod list                    - List banned words
        !automod on                      - Enable auto-mod
        !automod off                     - Disable auto-mod

    Honeypot:
        !honeypot create <name>          - Create and designate a honeypot channel
        !honeypot set #channel           - Designate an existing channel
        !honeypot remove                 - Remove the honeypot designation
        !honeypot logchannel #channel    - Set a dedicated channel for honeypot alerts
        !honeypot logchannel             - Clear the honeypot log channel
        !honeypot role @role             - Set a role to ping on honeypot alerts
        !honeypot role                   - Clear the ping role
        !honeypot muterole @role         - Set the role applied to honeypot offenders
        !honeypot muterole               - Clear the honeypot mute role

Automatic protections (no commands needed):
    - Banned word filter
    - Cross-channel duplicate detection (45 s window)
    - Honeypot channel enforcement
"""

import asyncio
import logging
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.moderation")

_COLOR_BAD = 0xED4245   # red — punitive actions
_COLOR_WARN = 0xFEE75C  # yellow — warnings
_COLOR_OK = 0x57F287    # green — confirmations
_COLOR_INFO = 0x5865F2  # blue — informational

# How long (seconds) to track cross-channel duplicate messages
_DUPE_WINDOW = 45

# How long to timeout a user who triggers the duplicate spam threshold (seconds)
_DUPE_TIMEOUT_SECONDS = 15 * 60  # 15 minutes

# Number of distinct channels the same content must appear in within the window
# before escalating to a timeout (≥ this value → timeout)
_DUPE_TIMEOUT_THRESHOLD = 3

# Minimum message length to bother tracking for duplicates
_DUPE_MIN_LENGTH = 8

# Common words/phrases that are too short or generic to track
_DUPE_SKIP = frozenset({
    "yes", "no", "ok", "okay", "yep", "nope", "nah", "yeah", "yea",
    "hi", "hey", "hello", "bye", "cya", "lol", "lmao", "lmfao", "rofl",
    "omg", "wtf", "gg", "rip", "f", "o", "xd", "haha", "hehe",
    "thanks", "thank you", "ty", "np", "no problem", "yw", "welcome",
    "sure", "yup", "cool", "nice", "good", "great", "wow", "damn",
})


class _DupeEntry(NamedTuple):
    channel_id: int
    timestamp: float


class ModerationCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()

        # In-memory duplicate tracker:
        # { guild_id: { user_id: { normalised_content: [_DupeEntry, ...] } } }
        self._dupe_tracker: dict[int, dict[int, dict[str, list[_DupeEntry]]]] = (
            defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        )

    # =========================================================================
    # Helpers — mod log
    # =========================================================================

    async def _post_mod_log(self, guild_id: int, embed: fluxer.Embed) -> None:
        """Post an embed to the mod log channel if one is configured."""
        channel_id = self.settings.get(guild_id, "modlog_channel_id")
        if channel_id is None:
            return

        channel = self.bot._channels.get(channel_id)
        if channel is None:
            log.warning("Mod log channel %d not found in guild %d", channel_id, guild_id)
            return

        try:
            await channel.send(embed=embed)
        except Exception as exc:
            log.error("Failed to post mod log: %s", exc)

    def _mod_embed(
        self,
        *,
        action: str,
        target: str,
        moderator: str,
        reason: str | None = None,
        color: int = _COLOR_BAD,
        extra_fields: list[tuple[str, str]] | None = None,
    ) -> fluxer.Embed:
        """Build a standardised mod-log embed."""
        embed = (
            fluxer.Embed(title=action, color=color)
            .add_field(name="User", value=target, inline=True)
            .add_field(name="Moderator", value=moderator, inline=True)
        )

        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)

        if extra_fields:
            for name, value in extra_fields:
                embed.add_field(name=name, value=value, inline=True)

        embed.set_footer(text=datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"))
        return embed

    # =========================================================================
    # Helpers — member resolution / parsing
    # =========================================================================

    async def _resolve_member(
        self, guild_id: int, user_id: int
    ) -> fluxer.GuildMember | None:
        guild = self.bot._guilds.get(guild_id)
        if guild is None:
            return None

        try:
            return await guild.fetch_member(user_id)
        except Exception:
            return None

    @staticmethod
    def _parse_time(s: str) -> float | None:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", s.lower())
        if not match:
            return None

        value, unit = float(match.group(1)), match.group(2)
        return value * {"s": 1, "m": 60, "h": 3600}[unit]

    @staticmethod
    def _iso_from_now(seconds: float) -> str:
        dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return dt.isoformat()

    @staticmethod
    def _normalise(text: str) -> str:
        """Normalise message content for duplicate comparison.

        Lowercases, strips accents, removes punctuation and extra whitespace.
        """
        text = text.lower()
        text = unicodedata.normalize("NFD", text)
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        text = re.sub(r"[^\w\s]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # =========================================================================
    # Commands — mod log setup
    # =========================================================================

    @fluxer.Cog.command(name="setmodlog")
    @fluxer.has_permission(fluxer.Permissions.MANAGE_GUILD)
    async def setmodlog(self, ctx: fluxer.Message) -> None:
        """Set or clear the mod log channel.

        Usage:  !setmodlog #channel
                !setmodlog              <- clears the mod log
        """
        args = ctx.content.removeprefix(f"{config.COMMAND_PREFIX}setmodlog").strip()

        if not args:
            self.settings.delete(ctx.guild_id, "modlog_channel_id")
            await ctx.reply("Mod log channel cleared.")
            return

        channel = None
        match = re.search(r"<#(\d+)>", args)
        if match:
            channel = self.bot._channels.get(int(match.group(1)))
        else:
            for ch in self.bot._channels.values():
                if ch.guild_id == ctx.guild_id and ch.name == args.lstrip("#"):
                    channel = ch
                    break

        if channel is None or not channel.is_text_channel:
            await ctx.reply("Could not find that text channel.")
            return

        self.settings.set(ctx.guild_id, "modlog_channel_id", channel.id)

        await ctx.reply(
            embed=fluxer.Embed(
                title="Mod log set",
                description=f"Moderation events will be logged in **#{channel.name}**.",
                color=_COLOR_OK,
            )
        )

    # =========================================================================
    # Commands — member actions
    # =========================================================================

    @fluxer.Cog.command(name="kick")
    @fluxer.has_permission(fluxer.Permissions.KICK_MEMBERS)
    async def kick(self, ctx: fluxer.Message) -> None:
        """Kick a member.

        Usage:  !kick @user [reason]
        """
        if not ctx.mentions:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}kick @user [reason]`")
            return

        target_user = ctx.mentions[0]
        reason_raw = ctx.content.split(None, 2)
        reason = reason_raw[2].strip() if len(reason_raw) > 2 else None
        reason = re.sub(r"<@!?\d+>", "", reason).strip() if reason else None

        member = await self._resolve_member(ctx.guild_id, target_user.id)
        if member is None:
            await ctx.reply("That user is not in this server.")
            return

        try:
            await member.kick(reason=reason)
        except Exception as exc:
            await ctx.reply(f"Failed to kick: {exc}")
            return

        embed = self._mod_embed(
            action="Member Kicked",
            target=f"{target_user.display_name} ({target_user.id})",
            moderator=ctx.author.display_name,
            reason=reason or "No reason provided",
        )

        await ctx.reply(embed=embed)
        await self._post_mod_log(ctx.guild_id, embed)
        log.info("Kicked %s from guild %d", target_user.username, ctx.guild_id)

    @fluxer.Cog.command(name="ban")
    @fluxer.has_permission(fluxer.Permissions.BAN_MEMBERS)
    async def ban(self, ctx: fluxer.Message) -> None:
        """Ban a member.

        Usage:  !ban @user [reason]
        """
        if not ctx.mentions:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}ban @user [reason]`")
            return

        target_user = ctx.mentions[0]
        reason_raw = ctx.content.split(None, 2)
        reason = reason_raw[2].strip() if len(reason_raw) > 2 else None
        reason = re.sub(r"<@!?\d+>", "", reason).strip() if reason else None

        member = await self._resolve_member(ctx.guild_id, target_user.id)
        if member is None:
            await ctx.reply("That user is not in this server.")
            return

        try:
            await member.ban(reason=reason)
        except Exception as exc:
            await ctx.reply(f"Failed to ban: {exc}")
            return

        embed = self._mod_embed(
            action="Member Banned",
            target=f"{target_user.display_name} ({target_user.id})",
            moderator=ctx.author.display_name,
            reason=reason or "No reason provided",
        )

        await ctx.reply(embed=embed)
        await self._post_mod_log(ctx.guild_id, embed)
        log.info("Banned %s from guild %d", target_user.username, ctx.guild_id)

    @fluxer.Cog.command(name="unban")
    @fluxer.has_permission(fluxer.Permissions.BAN_MEMBERS)
    async def unban(self, ctx: fluxer.Message) -> None:
        """Unban a user by ID.

        Usage:  !unban <user_id> [reason]
        """
        parts = ctx.content.split(None, 3)
        if len(parts) < 2 or not parts[1].isdigit():
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}unban <user_id> [reason]`")
            return

        user_id = int(parts[1])
        reason = parts[2].strip() if len(parts) > 2 else None

        guild = self.bot._guilds.get(ctx.guild_id)
        if guild is None:
            await ctx.reply("Could not resolve guild.")
            return

        try:
            await guild.unban(user_id, reason=reason)
        except Exception as exc:
            await ctx.reply(f"Failed to unban: {exc}")
            return

        embed = self._mod_embed(
            action="Member Unbanned",
            target=str(user_id),
            moderator=ctx.author.display_name,
            reason=reason or "No reason provided",
            color=_COLOR_OK,
        )

        await ctx.reply(embed=embed)
        await self._post_mod_log(ctx.guild_id, embed)

    @fluxer.Cog.command(name="timeout")
    @fluxer.has_permission(fluxer.Permissions.MODERATE_MEMBERS)
    async def timeout(self, ctx: fluxer.Message) -> None:
        """Timeout a member for a duration.

        Usage:  !timeout @user <time> [reason]
        Example: !timeout @user 30m Spamming
        Units: s = seconds, m = minutes, h = hours
        """
        if not ctx.mentions:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}timeout @user <time> [reason]`\n"
                f"Example: `{config.COMMAND_PREFIX}timeout @user 30m Spamming`"
            )
            return

        target_user = ctx.mentions[0]

        # Content after mention
        content_clean = re.sub(r"<@!?\d+>", "", ctx.content).strip()
        parts = content_clean.split(None, 2)

        # parts[0] = command name, parts[1] = time, parts[2] = reason (optional)
        if len(parts) < 2:
            await ctx.reply("Please include a duration, e.g. `30m`.")
            return

        seconds = self._parse_time(parts[1])
        if seconds is None:
            await ctx.reply(
                f"Couldn't parse **{parts[1]}** as a time. "
                f"Use `s`, `m`, or `h` — e.g. `30m`, `2h`."
            )
            return

        reason = parts[2].strip() if len(parts) > 2 else None

        member = await self._resolve_member(ctx.guild_id, target_user.id)
        if member is None:
            await ctx.reply("That user is not in this server.")
            return

        try:
            # Fluxer expects the timeout date as a positional argument.
            await member.timeout(self._iso_from_now(seconds), reason=reason)
        except Exception as exc:
            await ctx.reply(f"Failed to apply timeout: {exc}")
            return

        # Human-readable duration
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        duration_str = (
            f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
        ).strip()

        embed = self._mod_embed(
            action="Member Timed Out",
            target=f"{target_user.display_name} ({target_user.id})",
            moderator=ctx.author.display_name,
            reason=reason or "No reason provided",
            extra_fields=[("Duration", duration_str)],
        )

        await ctx.reply(embed=embed)
        await self._post_mod_log(ctx.guild_id, embed)

        log.info(
            "Timed out %s for %s in guild %d",
            target_user.username,
            duration_str,
            ctx.guild_id,
        )

    @fluxer.Cog.command(name="untimeout")
    @fluxer.has_permission(fluxer.Permissions.MODERATE_MEMBERS)
    async def untimeout(self, ctx: fluxer.Message) -> None:
        """Remove a timeout from a member.

        Usage:  !untimeout @user
        """
        if not ctx.mentions:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}untimeout @user`")
            return

        target_user = ctx.mentions[0]
        member = await self._resolve_member(ctx.guild_id, target_user.id)
        if member is None:
            await ctx.reply("That user is not in this server.")
            return

        try:
            # Passing None removes the timeout.
            await member.timeout(None, reason=f"Timeout removed by {ctx.author.display_name}")
        except Exception as exc:
            await ctx.reply(f"Failed to remove timeout: {exc}")
            return

        embed = self._mod_embed(
            action="Timeout Removed",
            target=f"{target_user.display_name} ({target_user.id})",
            moderator=ctx.author.display_name,
            color=_COLOR_OK,
        )

        await ctx.reply(embed=embed)
        await self._post_mod_log(ctx.guild_id, embed)

    @fluxer.Cog.command(name="purge")
    @fluxer.has_permission(fluxer.Permissions.MANAGE_MESSAGES)
    async def purge(self, ctx: fluxer.Message) -> None:
        """Delete the last N messages in this channel (max 100).

        Usage:  !purge <count>
        """
        parts = ctx.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}purge <number>`")
            return

        count = min(int(parts[1]), 100)
        channel = self.bot._channels.get(ctx.channel_id)

        if channel is None:
            await ctx.reply("Could not resolve this channel.")
            return

        try:
            messages = await channel.fetch_messages(limit=count + 1)  # +1 to include the command
            ids = [m.id for m in messages]
            if ids:
                await channel.delete_messages(ids)
        except Exception as exc:
            await ctx.reply(f"Failed to purge messages: {exc}")
            return

        embed = self._mod_embed(
            action="Messages Purged",
            target=f"#{channel.name}",
            moderator=ctx.author.display_name,
            reason=f"{count} message(s) deleted",
            color=_COLOR_INFO,
        )

        await self._post_mod_log(ctx.guild_id, embed)

        confirm = await channel.send(
            embed=fluxer.Embed(
                description=f"Deleted {count} message(s).",
                color=_COLOR_OK,
            )
        )

        await asyncio.sleep(5)

        try:
            await confirm.delete()
        except Exception:
            pass

    # =========================================================================
    # Commands — auto-mod word filter
    # =========================================================================

    @fluxer.Cog.command(name="automod")
    @fluxer.has_permission(fluxer.Permissions.MANAGE_GUILD)
    async def automod(self, ctx: fluxer.Message) -> None:
        """Manage the automatic word filter.

        Usage:
            !automod addword <word>
            !automod removeword <word>
            !automod list
            !automod on
            !automod off
        """
        args = ctx.content.removeprefix(f"{config.COMMAND_PREFIX}automod").strip()
        parts = args.split(None, 1)
        sub = parts[0].lower() if parts else ""

        if sub == "addword":
            await self._automod_addword(ctx, parts)
        elif sub == "removeword":
            await self._automod_removeword(ctx, parts)
        elif sub == "list":
            await self._automod_list(ctx)
        elif sub in ("on", "off"):
            await self._automod_toggle(ctx, sub == "on")
        else:
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Auto-mod commands",
                    description=(
                        f"`{config.COMMAND_PREFIX}automod addword <word>` — Add banned word\n"
                        f"`{config.COMMAND_PREFIX}automod removeword <word>` — Remove banned word\n"
                        f"`{config.COMMAND_PREFIX}automod list` — Show banned words\n"
                        f"`{config.COMMAND_PREFIX}automod on/off` — Toggle filter"
                    ),
                    color=_COLOR_INFO,
                )
            )

    async def _automod_addword(self, ctx: fluxer.Message, parts: list[str]) -> None:
        if len(parts) < 2:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}automod addword <word>`")
            return

        word = parts[1].strip().lower()
        words: list = self.settings.get(ctx.guild_id, "banned_words") or []

        if word in words:
            await ctx.reply(f"**{word}** is already on the banned word list.")
            return

        words.append(word)
        self.settings.set(ctx.guild_id, "banned_words", words)

        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Added **{word}** to the banned word list.",
                color=_COLOR_OK,
            )
        )

    async def _automod_removeword(self, ctx: fluxer.Message, parts: list[str]) -> None:
        if len(parts) < 2:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}automod removeword <word>`")
            return

        word = parts[1].strip().lower()
        words: list = self.settings.get(ctx.guild_id, "banned_words") or []

        if word not in words:
            await ctx.reply(f"**{word}** is not on the banned word list.")
            return

        words.remove(word)
        self.settings.set(ctx.guild_id, "banned_words", words)

        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Removed **{word}** from the banned word list.",
                color=_COLOR_OK,
            )
        )

    async def _automod_list(self, ctx: fluxer.Message) -> None:
        words: list = self.settings.get(ctx.guild_id, "banned_words") or []

        if not words:
            await ctx.reply("The banned word list is empty.")
            return

        await ctx.reply(
            embed=fluxer.Embed(
                title=f"Banned words — {len(words)} total",
                description="  ".join(f"`{w}`" for w in sorted(words)),
                color=_COLOR_INFO,
            )
        )

    async def _automod_toggle(self, ctx: fluxer.Message, enable: bool) -> None:
        self.settings.set(ctx.guild_id, "automod_enabled", enable)
        state = "enabled" if enable else "disabled"

        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Auto-mod word filter **{state}**.",
                color=_COLOR_OK if enable else _COLOR_WARN,
            )
        )

    # =========================================================================
    # Commands — honeypot
    # =========================================================================

    @fluxer.Cog.command(name="honeypot")
    @fluxer.has_permission(fluxer.Permissions.MANAGE_CHANNELS)
    async def honeypot(self, ctx: fluxer.Message) -> None:
        """Manage the honeypot channel.

        Usage:
            !honeypot create <name>       - Create a new channel and designate it
            !honeypot set #channel        - Designate an existing channel
            !honeypot remove              - Remove the honeypot designation
            !honeypot logchannel #channel - Set alert channel
            !honeypot role @role          - Set ping role for alerts
            !honeypot muterole @role      - Set role to apply on trigger
        """
        args = ctx.content.removeprefix(f"{config.COMMAND_PREFIX}honeypot").strip()
        parts = args.split(None, 1)
        sub = parts[0].lower() if parts else ""

        if sub == "create":
            await self._honeypot_create(ctx, parts)
        elif sub == "set":
            await self._honeypot_set(ctx, parts)
        elif sub == "remove":
            await self._honeypot_remove(ctx)
        elif sub == "logchannel":
            await self._honeypot_logchannel(ctx, parts)
        elif sub == "role":
            await self._honeypot_role(ctx, parts)
        elif sub == "muterole":
            await self._honeypot_muterole(ctx, parts)
        elif sub == "mode":
            await self._honeypot_mode(ctx, parts)
        else:
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Honeypot commands",
                    description=(
                        f"`{config.COMMAND_PREFIX}honeypot create <n>` — Create channel\n"
                        f"`{config.COMMAND_PREFIX}honeypot set #channel` — Use existing channel\n"
                        f"`{config.COMMAND_PREFIX}honeypot remove` — Remove designation\n"
                        f"`{config.COMMAND_PREFIX}honeypot logchannel #channel` — Set dedicated alert channel\n"
                        f"`{config.COMMAND_PREFIX}honeypot logchannel` — Clear alert channel\n"
                        f"`{config.COMMAND_PREFIX}honeypot role @role` — Set role to ping on alerts\n"
                        f"`{config.COMMAND_PREFIX}honeypot role` — Clear ping role\n"
                        f"`{config.COMMAND_PREFIX}honeypot muterole @role` — Set role to apply on trigger\n"
                        f"`{config.COMMAND_PREFIX}honeypot muterole` — Clear applied role\n"
                        f"`{config.COMMAND_PREFIX}honeypot mode role` — Use role punishment\n"
                        f"`{config.COMMAND_PREFIX}honeypot mode timeout` — Use Discord timeout"
                    ),
                    color=_COLOR_INFO,
                )
            )

    async def _honeypot_create(self, ctx: fluxer.Message, parts: list[str]) -> None:
        if len(parts) < 2:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}honeypot create <channel-name>`"
            )
            return

        name = parts[1].strip().lower().replace(" ", "-")
        guild = self.bot._guilds.get(ctx.guild_id)

        if guild is None:
            await ctx.reply("Could not resolve guild.")
            return

        try:
            data = await self.bot._http.create_guild_channel(
                ctx.guild_id,
                name=name,
                type=0,  # text channel
                topic="⚠️ Restricted channel.",
            )
            channel = fluxer.Channel.from_data(data, self.bot._http)
            self.bot._channels[channel.id] = channel
        except Exception as exc:
            await ctx.reply(f"Failed to create channel: {exc}")
            return

        await self._designate_honeypot(ctx, channel)

    async def _honeypot_set(self, ctx: fluxer.Message, parts: list[str]) -> None:
        if len(parts) < 2:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}honeypot set #channel`"
            )
            return

        channel = None
        match = re.search(r"<#(\d+)>", parts[1])

        if match:
            channel = self.bot._channels.get(int(match.group(1)))
        else:
            name = parts[1].lstrip("#").strip()
            for ch in self.bot._channels.values():
                if ch.guild_id == ctx.guild_id and ch.name == name:
                    channel = ch
                    break

        if channel is None or not channel.is_text_channel:
            await ctx.reply("Could not find that text channel.")
            return

        await self._designate_honeypot(ctx, channel)

    async def _honeypot_remove(self, ctx: fluxer.Message) -> None:
        old_id = self.settings.get(ctx.guild_id, "honeypot_channel_id")

        if old_id is None:
            await ctx.reply("No honeypot channel is currently set.")
            return

        self.settings.delete(ctx.guild_id, "honeypot_channel_id")

        await ctx.reply(
            embed=fluxer.Embed(
                description="Honeypot designation removed. The channel itself was not deleted.",
                color=_COLOR_OK,
            )
        )

    async def _honeypot_logchannel(self, ctx: fluxer.Message, parts: list[str]) -> None:
        """Set or clear the dedicated honeypot alert log channel.

        Usage:  !honeypot logchannel #channel
                !honeypot logchannel              <- clears it
        """
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg:
            self.settings.delete(ctx.guild_id, "honeypot_log_channel_id")
            await ctx.reply(
                embed=fluxer.Embed(
                    description="Honeypot log channel cleared. Alerts will fall back to the general mod log.",
                    color=_COLOR_OK,
                )
            )
            return

        channel = None
        match = re.search(r"<#(\d+)>", arg)

        if match:
            channel = self.bot._channels.get(int(match.group(1)))
        else:
            name = arg.lstrip("#").strip()
            for ch in self.bot._channels.values():
                if ch.guild_id == ctx.guild_id and ch.name == name:
                    channel = ch
                    break

        if channel is None or not channel.is_text_channel:
            await ctx.reply("Could not find that text channel.")
            return

        self.settings.set(ctx.guild_id, "honeypot_log_channel_id", channel.id)

        log.info(
            "Honeypot log channel set to #%s (%d) in guild %d",
            channel.name,
            channel.id,
            ctx.guild_id,
        )

        ping_role_id = self.settings.get(ctx.guild_id, "honeypot_ping_role_id")
        role_note = (
            f"\nRole <@&{ping_role_id}> will be pinged on each alert."
            if ping_role_id
            else f"\nNo ping role set yet — use `{config.COMMAND_PREFIX}honeypot role @role` to add one."
        )

        await ctx.reply(
            embed=fluxer.Embed(
                title="Honeypot log channel set",
                description=f"Honeypot alerts will be posted in **#{channel.name}**.{role_note}",
                color=_COLOR_OK,
            )
        )

    async def _honeypot_role(self, ctx: fluxer.Message, parts: list[str]) -> None:
        """Set or clear the role to ping when the honeypot is triggered.

        Usage:  !honeypot role @role
                !honeypot role              <- clears it
        """
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg:
            self.settings.delete(ctx.guild_id, "honeypot_ping_role_id")
            await ctx.reply(
                embed=fluxer.Embed(
                    description="Honeypot ping role cleared. No role will be pinged on alerts.",
                    color=_COLOR_OK,
                )
            )
            return

        match = re.fullmatch(r"<@&(\d+)>", arg) or re.fullmatch(r"(\d+)", arg)

        if not match:
            await ctx.reply(
                "Could not parse that as a role. Use a mention (`@Role`) or a raw role ID."
            )
            return

        role_id = int(match.group(1))
        self.settings.set(ctx.guild_id, "honeypot_ping_role_id", role_id)

        log.info("Honeypot ping role set to %d in guild %d", role_id, ctx.guild_id)

        log_channel_id = self.settings.get(ctx.guild_id, "honeypot_log_channel_id")
        channel_note = (
            f"\nAlerts will appear in <#{log_channel_id}>."
            if log_channel_id
            else f"\nNo log channel set yet — use `{config.COMMAND_PREFIX}honeypot logchannel #channel` to add one."
        )

        await ctx.reply(
            embed=fluxer.Embed(
                title="Honeypot ping role set",
                description=f"<@&{role_id}> will be pinged whenever the honeypot is triggered.{channel_note}",
                color=_COLOR_OK,
            )
        )

    async def _honeypot_muterole(self, ctx: fluxer.Message, parts: list[str]) -> None:
        """Set or clear the role applied to users who trigger the honeypot.

        Usage:  !honeypot muterole @role
                !honeypot muterole              <- clears it
        """
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg:
            self.settings.delete(ctx.guild_id, "honeypot_mute_role_id")
            await ctx.reply(
                embed=fluxer.Embed(
                    description="Honeypot mute role cleared. Users will no longer receive a role on trigger.",
                    color=_COLOR_OK,
                )
            )
            return

        match = re.fullmatch(r"<@&(\d+)>", arg) or re.fullmatch(r"(\d+)", arg)

        if not match:
            await ctx.reply(
                "Could not parse that as a role. Use a mention (`@Role`) or a raw role ID."
            )
            return

        role_id = int(match.group(1))
        self.settings.set(ctx.guild_id, "honeypot_mute_role_id", role_id)

        log.info("Honeypot mute role set to %d in guild %d", role_id, ctx.guild_id)

        await ctx.reply(
            embed=fluxer.Embed(
                title="Honeypot mute role set",
                description=(
                    f"<@&{role_id}> will be applied whenever someone triggers the honeypot.\n\n"
                    "Make sure my bot role is above this role and I have **Manage Roles**."
                ),
                color=_COLOR_OK,
            )
        )

    async def _honeypot_mode(self, ctx: fluxer.Message, parts: list[str]) -> None:
        """Set whether the honeypot applies a role or uses Discord timeout.

        Usage:  !honeypot mode role
                !honeypot mode timeout
        """
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        if arg not in {"role", "timeout"}:
            current = self.settings.get(ctx.guild_id, "honeypot_action_mode") or "role"
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Honeypot mode",
                    description=(
                        f"Current mode: **{current}**\n\n"
                        f"`{config.COMMAND_PREFIX}honeypot mode role` — Apply configured mute role\n"
                        f"`{config.COMMAND_PREFIX}honeypot mode timeout` — Apply Discord timeout"
                    ),
                    color=_COLOR_INFO,
                )
            )
            return

        self.settings.set(ctx.guild_id, "honeypot_action_mode", arg)
        log.info("Honeypot action mode set to %s in guild %d", arg, ctx.guild_id)

        await ctx.reply(
            embed=fluxer.Embed(
                title="Honeypot mode set",
                description=f"Honeypot punishment mode is now **{arg}**.",
                color=_COLOR_OK,
            )
        )

    async def _post_honeypot_log(
        self,
        guild_id: int,
        embed: fluxer.Embed,
        ping_content: str,
    ) -> None:
        """Post honeypot alert to dedicated log channel, falling back to mod log."""
        channel_id = (
            self.settings.get(guild_id, "honeypot_log_channel_id")
            or self.settings.get(guild_id, "modlog_channel_id")
        )

        if channel_id is None:
            log.debug("No honeypot log or mod log channel configured for guild %d", guild_id)
            return

        channel = self.bot._channels.get(channel_id)

        if channel is None:
            log.warning("Honeypot log channel %d not found in guild %d", channel_id, guild_id)
            return

        try:
            if ping_content:
                await channel.send(content=ping_content)
            await channel.send(embed=embed)
        except Exception as exc:
            log.error("Failed to post honeypot log: %s", exc)

    async def _designate_honeypot(
        self,
        ctx: fluxer.Message,
        channel: fluxer.Channel,
    ) -> None:
        """Save the honeypot channel and post the warning message inside it."""
        self.settings.set(ctx.guild_id, "honeypot_channel_id", channel.id)

        log.info(
            "Honeypot set to #%s (%d) in guild %d",
            channel.name,
            channel.id,
            ctx.guild_id,
        )

        warning_embed = fluxer.Embed(
            title="⚠️ Restricted Channel",
            description=(
                "**Do not write in this channel.**\n\n"
                "This is a restricted area. Any message sent here will result in "
                "a restricted role being applied until a moderator reviews your case.\n\n"
                "If you ended up here by accident, please leave without typing anything."
            ),
            color=_COLOR_WARN,
        ).set_footer(text="This warning was posted automatically by the moderation bot.")

        try:
            await channel.send(embed=warning_embed)
        except Exception as exc:
            log.warning("Could not post honeypot warning: %s", exc)

        await ctx.reply(
            embed=fluxer.Embed(
                title="Honeypot set",
                description=(
                    f"**#{channel.name}** is now the honeypot channel.\n"
                    f"Set the role to apply with `{config.COMMAND_PREFIX}honeypot muterole @role`."
                ),
                color=_COLOR_OK,
            )
        )

    # =========================================================================
    # Listener — on_message (all auto-mod checks)
    # =========================================================================

    @fluxer.Cog.listener()
    async def on_message(self, message: fluxer.Message) -> None:
        """Run all automatic moderation checks in priority order."""
        if message.author.bot:
            return

        if message.guild_id is None:
            return

        # 1. Honeypot — highest priority, check first
        if await self._check_honeypot(message):
            return

        # 2. Banned word filter
        if await self._check_banned_words(message):
            return

        # 3. Cross-channel duplicate detection
        await self._check_duplicates(message)

    # ── Auto-mod: honeypot ────────────────────────────────────────────────────

    async def _check_honeypot(self, message: fluxer.Message) -> bool:
        """Return True if message was in the honeypot and action was taken."""
        honeypot_id = self.settings.get(message.guild_id, "honeypot_channel_id")

        if honeypot_id is None or message.channel_id != honeypot_id:
            return False

        log.info(
            "Honeypot triggered by %s (guild %d)",
            message.author.username,
            message.guild_id,
        )

        try:
            await message.delete()
        except Exception as exc:
            log.warning("Could not delete honeypot message: %s", exc)

        action_mode = self.settings.get(message.guild_id, "honeypot_action_mode") or "role"

        mute_role_id = self.settings.get(message.guild_id, "honeypot_mute_role_id")
        role_applied = False
        timeout_applied = False

        if action_mode == "timeout":
            member = await self._resolve_member(message.guild_id, message.author.id)
            timeout_until = self._iso_from_now(_HONEYPOT_TIMEOUT_DAYS * 86400)

            if member:
                try:
                    await member.timeout(
                        timeout_until,
                        reason="Honeypot channel triggered",
                    )
                    timeout_applied = True
                except Exception as exc:
                    log.error("Could not timeout honeypot offender: %s", exc)
            else:
                log.warning(
                    "Could not resolve member %d in guild %d for honeypot timeout",
                    message.author.id,
                    message.guild_id,
                )

        else:
            if mute_role_id:
                try:
                    await self.bot._http.add_guild_member_role(
                        message.guild_id,
                        message.author.id,
                        mute_role_id,
                    )
                    role_applied = True
                except Exception as exc:
                    log.error("Could not apply honeypot mute role: %s", exc)
            else:
                log.warning(
                    "Honeypot triggered in guild %d, but no honeypot mute role is configured",
                    message.guild_id,
                )

        # Warn in the honeypot channel.
        channel = self.bot._channels.get(message.channel_id)

        if channel:
            try:
                warn_embed = fluxer.Embed(
                    title="User Restricted",
                    description=(
                        f"{message.author.mention} has been restricted for writing in a restricted channel.\n"
                        "A moderator will review this."
                    ),
                    color=_COLOR_BAD,
                )
                await channel.send(embed=warn_embed)
            except Exception as exc:
                log.warning("Could not send honeypot warning message: %s", exc)

        # Truncate long messages but keep enough for context.
        raw_content = message.content.strip()
        display_content = raw_content[:400] if raw_content else "_(empty message)_"

        if len(raw_content) > 400:
            display_content += f"\n_…({len(raw_content) - 400} more characters)_"

        honeypot_channel = self.bot._channels.get(message.channel_id)
        channel_ref = (
            f"<#{message.channel_id}>"
            if honeypot_channel
            else f"channel `{message.channel_id}`"
        )

        restore_hint = (
            f"Remove role <@&{mute_role_id}> from {message.author.mention}"
            if action_mode == "role" and mute_role_id
            else f"`{config.COMMAND_PREFIX}untimeout <@{message.author.id}>`\n"
                 f"or by ID: `{config.COMMAND_PREFIX}untimeout {message.author.id}`"
            if action_mode == "timeout"
            else f"Set a role first with `{config.COMMAND_PREFIX}honeypot muterole @role`"
        )

        action_status = (
            f"✅ Applied role <@&{mute_role_id}>"
            if action_mode == "role" and role_applied and mute_role_id
            else "✅ Applied Discord timeout"
            if action_mode == "timeout" and timeout_applied
            else "⚠️ Failed or not configured — check permissions and role hierarchy"
        )

        mod_log_embed = self._mod_embed(
            action="🍯 Honeypot Triggered",
            target=(
                f"{message.author.mention} — **{message.author.display_name}**\n"
                f"ID: `{message.author.id}`"
            ),
            moderator="Auto-mod",
            reason="Sent a message in the designated honeypot channel",
            color=_COLOR_BAD,
            extra_fields=[
                ("Channel", channel_ref),
                ("Mode", action_mode),
                ("Action Applied", action_status),
                ("Message Content", display_content),
                ("⬆️ To Restore", restore_hint),
            ],
        )

        ping_role_id = self.settings.get(message.guild_id, "honeypot_ping_role_id")
        ping_content = f"<@&{ping_role_id}>" if ping_role_id else ""

        await self._post_honeypot_log(message.guild_id, mod_log_embed, ping_content)

        log.info(
            "Honeypot mod-log posted for user %d in guild %d (mode=%s, role_applied=%s, timeout_applied=%s)",
            message.author.id,
            message.guild_id,
            action_mode,
            role_applied,
            timeout_applied,
        )

        return True

    # ── Auto-mod: banned words ────────────────────────────────────────────────

    async def _check_banned_words(self, message: fluxer.Message) -> bool:
        """Return True if a banned word was found and action was taken."""
        if not self.settings.get(message.guild_id, "automod_enabled", default=False):
            return False

        words: list = self.settings.get(message.guild_id, "banned_words") or []

        if not words:
            return False

        content_lower = message.content.lower()
        triggered = next((w for w in words if w in content_lower), None)

        if triggered is None:
            return False

        try:
            await message.delete()
        except Exception as exc:
            log.warning("Could not delete banned word message: %s", exc)

        channel = self.bot._channels.get(message.channel_id)

        if channel:
            try:
                await channel.send(
                    embed=fluxer.Embed(
                        description=(
                            f"{message.author.mention} your message was removed "
                            "because it contained a banned word."
                        ),
                        color=_COLOR_WARN,
                    )
                )
            except Exception:
                pass

        embed = self._mod_embed(
            action="Banned Word Detected",
            target=f"{message.author.display_name} ({message.author.id})",
            moderator="Auto-mod",
            reason=f"Triggered word: `{triggered}`",
            color=_COLOR_WARN,
            extra_fields=[("Channel", f"<#{message.channel_id}>")],
        )

        await self._post_mod_log(message.guild_id, embed)
        return True

    # ── Auto-mod: cross-channel duplicate detection ───────────────────────────

    async def _check_duplicates(self, message: fluxer.Message) -> None:
        """Detect the same message being sent across multiple channels rapidly."""
        content = message.content.strip()

        # Skip very short messages and common phrases.
        if len(content) < _DUPE_MIN_LENGTH:
            return

        if self._normalise(content) in _DUPE_SKIP:
            return

        normed = self._normalise(content)

        if not normed:
            return

        now = time.monotonic()
        guild_id = message.guild_id
        user_id = message.author.id

        # Clean up entries outside the window.
        user_entries = self._dupe_tracker[guild_id][user_id]
        user_entries[normed] = [
            e for e in user_entries[normed]
            if now - e.timestamp <= _DUPE_WINDOW
        ]

        # Check if this channel is already in the list, so sending the same
        # thing twice in the same channel does not double-count.
        seen_channels = {e.channel_id for e in user_entries[normed]}

        if message.channel_id not in seen_channels:
            user_entries[normed].append(_DupeEntry(message.channel_id, now))

        distinct_channels = {e.channel_id for e in user_entries[normed]}
        count = len(distinct_channels)

        if count < 2:
            return

        try:
            await message.delete()
        except Exception as exc:
            log.warning("Could not delete duplicate message: %s", exc)

        channel = self.bot._channels.get(message.channel_id)

        if count >= _DUPE_TIMEOUT_THRESHOLD:
            # Escalate to timeout.
            member = await self._resolve_member(guild_id, user_id)

            if member:
                try:
                    # Fluxer expects the timeout date as a positional argument.
                    await member.timeout(
                        self._iso_from_now(_DUPE_TIMEOUT_SECONDS),
                        reason="Cross-channel spam (automated)",
                    )
                except Exception as exc:
                    log.error("Could not timeout duplicate spammer: %s", exc)

            if channel:
                try:
                    await channel.send(
                        embed=fluxer.Embed(
                            description=(
                                f"{message.author.mention} you have been muted for 15 minutes "
                                f"for sending the same message across {count} channels."
                            ),
                            color=_COLOR_BAD,
                        )
                    )
                except Exception:
                    pass

            embed = self._mod_embed(
                action="Cross-Channel Spam — Timeout Applied",
                target=f"{message.author.display_name} ({user_id})",
                moderator="Auto-mod",
                reason=f"Same message sent in {count} channels within {_DUPE_WINDOW}s",
                extra_fields=[("Duration", "15 minutes")],
            )

            # Clear tracker for this user+content to reset after punishment.
            user_entries.pop(normed, None)

        else:
            # First offence — warn only.
            if channel:
                try:
                    await channel.send(
                        embed=fluxer.Embed(
                            description=(
                                f"{message.author.mention} please do not send the same message "
                                "in multiple channels."
                            ),
                            color=_COLOR_WARN,
                        )
                    )
                except Exception:
                    pass

            embed = self._mod_embed(
                action="Cross-Channel Duplicate Detected",
                target=f"{message.author.display_name} ({user_id})",
                moderator="Auto-mod",
                reason=f"Same message seen in {count} channels within {_DUPE_WINDOW}s",
                color=_COLOR_WARN,
            )

        await self._post_mod_log(guild_id, embed)


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))