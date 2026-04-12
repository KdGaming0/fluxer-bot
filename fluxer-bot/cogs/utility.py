"""
cogs/utility.py
---------------
General-purpose utility commands.

Commands:
    Tags:
        !tag add <name> <text>   - Create a tag
        !tag remove <name>       - Delete a tag
        !tag list                - List all tags in this server
        !tag info <name>         - Show tag details (author, created date)
        !<name>                  - Trigger a tag directly by name

    General:
        !ping                    - Bot latency check
        !serverinfo              - Info about this server
        !userinfo [@user]        - Info about a user (defaults to yourself)
        !remind <time> <message> - Set a reminder (e.g. !remind 30m Take a break)
                                   Supports: s (seconds), m (minutes), h (hours)
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.utility")

# Embed colour used across this cog
_COLOR = 0x5865F2

# Maximum number of tags per guild, to prevent abuse
_MAX_TAGS = 100


class UtilityCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()

    # =========================================================================
    # Tag system
    # =========================================================================

    @fluxer.Cog.command(name="tag")
    async def tag(self, ctx: fluxer.Message, *, args: str = "") -> None:
        """Dispatcher for all !tag sub-commands."""
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        parts = args.strip().split(None, 2)  # ["add"|"remove"|..., name, text?]
        subcommand = parts[0].lower() if parts else ""

        if subcommand == "add":
            await self._tag_add(ctx, parts)
        elif subcommand == "remove":
            await self._tag_remove(ctx, parts)
        elif subcommand == "list":
            await self._tag_list(ctx)
        elif subcommand == "info":
            await self._tag_info(ctx, parts)
        else:
            embed = fluxer.Embed(
                title="Tag commands",
                description=(
                    f"`{config.COMMAND_PREFIX}tag add <name> <text>` — Create a tag\n"
                    f"`{config.COMMAND_PREFIX}tag remove <name>` — Delete a tag\n"
                    f"`{config.COMMAND_PREFIX}tag list` — List all tags\n"
                    f"`{config.COMMAND_PREFIX}tag info <name>` — Tag details\n"
                    f"`{config.COMMAND_PREFIX}<name>` — Use a tag directly"
                ),
                color=_COLOR,
            )
            await ctx.reply(embed=embed)

    async def _tag_add(self, ctx: fluxer.Message, parts: list[str]) -> None:
        # parts = ["add", "name", "text with spaces"]
        if len(parts) < 3:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}tag add <name> <text>`\n"
                f"Example: `{config.COMMAND_PREFIX}tag add rules Read #rules before chatting!`"
            )
            return

        name = parts[1].lower()
        text = parts[2]

        # Validate name — letters, digits, hyphens only, max 32 chars
        if not re.fullmatch(r"[a-z0-9\-]{1,32}", name):
            await ctx.reply(
                "Tag names can only contain lowercase letters, digits, and hyphens, "
                "and must be 32 characters or fewer."
            )
            return

        # Don't allow shadowing real commands
        if name in self.bot._commands:
            await ctx.reply(f"**{name}** is a built-in command and cannot be used as a tag name.")
            return

        tags: dict = self.settings.get(ctx.guild_id, "tags") or {}

        if name in tags:
            await ctx.reply(
                f"A tag named **{name}** already exists. "
                f"Remove it first with `{config.COMMAND_PREFIX}tag remove {name}`."
            )
            return

        if len(tags) >= _MAX_TAGS:
            await ctx.reply(f"This server has reached the maximum of {_MAX_TAGS} tags.")
            return

        tags[name] = {
            "text": text,
            "author_id": ctx.author.id,
            "author_name": ctx.author.display_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.settings.set(ctx.guild_id, "tags", tags)
        log.info("Tag '%s' created in guild %d by %s", name, ctx.guild_id, ctx.author.username)

        await ctx.reply(
            embed=fluxer.Embed(
                title="Tag created",
                description=(
                    f"Tag **{name}** is ready.\n"
                    f"Use it with `{config.COMMAND_PREFIX}{name}`."
                ),
                color=_COLOR,
            )
        )

    async def _tag_remove(self, ctx: fluxer.Message, parts: list[str]) -> None:
        if len(parts) < 2:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}tag remove <name>`")
            return

        name = parts[1].lower()
        tags: dict = self.settings.get(ctx.guild_id, "tags") or {}

        if name not in tags:
            await ctx.reply(f"No tag named **{name}** exists on this server.")
            return

        del tags[name]
        self.settings.set(ctx.guild_id, "tags", tags)
        log.info("Tag '%s' deleted in guild %d by %s", name, ctx.guild_id, ctx.author.username)

        await ctx.reply(f"Tag **{name}** has been deleted.")

    async def _tag_list(self, ctx: fluxer.Message) -> None:
        tags: dict = self.settings.get(ctx.guild_id, "tags") or {}

        if not tags:
            await ctx.reply(
                f"No tags yet. Create one with `{config.COMMAND_PREFIX}tag add <name> <text>`."
            )
            return

        # List tag names in alphabetical order, formatted in a code block
        names = sorted(tags.keys())
        tag_list = "  ".join(f"`{n}`" for n in names)

        embed = fluxer.Embed(
            title=f"Tags — {len(tags)} total",
            description=tag_list,
            color=_COLOR,
        ).set_footer(text=f"Use {config.COMMAND_PREFIX}<name> to trigger a tag.")

        await ctx.reply(embed=embed)

    async def _tag_info(self, ctx: fluxer.Message, parts: list[str]) -> None:
        if len(parts) < 2:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}tag info <name>`")
            return

        name = parts[1].lower()
        tags: dict = self.settings.get(ctx.guild_id, "tags") or {}

        if name not in tags:
            await ctx.reply(f"No tag named **{name}** exists on this server.")
            return

        tag = tags[name]
        created_raw = tag.get("created_at", "")
        try:
            created_dt = datetime.fromisoformat(created_raw)
            created_str = created_dt.strftime("%d %b %Y at %H:%M UTC")
        except (ValueError, TypeError):
            created_str = "Unknown"

        embed = (
            fluxer.Embed(title=f"Tag: {name}", color=_COLOR)
            .add_field(name="Content", value=tag["text"], inline=False)
            .add_field(name="Created by", value=tag.get("author_name", "Unknown"), inline=True)
            .add_field(name="Created at", value=created_str, inline=True)
        )
        await ctx.reply(embed=embed)

    # =========================================================================
    # Tag trigger — on_message listener
    # =========================================================================

    @fluxer.Cog.listener()
    async def on_message(self, message: fluxer.Message) -> None:
        """Check every message to see if it calls a tag by name."""
        if message.author.bot:
            return
        if message.guild_id is None:
            return
        if not message.content.startswith(config.COMMAND_PREFIX):
            return

        # Strip prefix and grab the first word as the potential tag name
        body = message.content[len(config.COMMAND_PREFIX):].strip()
        name = body.split()[0].lower() if body.split() else ""

        if not name:
            return

        # Only fire if it's NOT already a registered bot command
        if name in self.bot._commands:
            if hasattr(self.bot, "process_commands"):
                await self.bot.process_commands(message._msg)
            return

        tags: dict = self.settings.get(message.guild_id, "tags") or {}
        if name not in tags:
            if hasattr(self.bot, "process_commands"):
                await self.bot.process_commands(message._msg)
            return

        await message.reply(tags[name]["text"])
        log.debug("Tag '%s' triggered in guild %d", name, message.guild_id)

    # =========================================================================
    # General commands
    # =========================================================================

    @fluxer.Cog.command(name="ping")
    async def ping(self, ctx: fluxer.Message) -> None:
        """Measure round-trip latency.

        Note: edit_message() does not serialize Embed objects in its JSON
        payload, so we measure the time to ctx.reply() directly instead of
        the send-then-edit pattern.
        """
        start = time.monotonic()
        await ctx.reply(
            embed=fluxer.Embed(
                title="Pong!",
                description="Measuring…",
                color=_COLOR,
            )
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        # Send the actual result as a follow-up in the same channel
        channel = self.bot._channels.get(ctx.channel_id)
        if channel:
            await channel.send(
                embed=fluxer.Embed(
                    title="Pong!",
                    description=f"Round-trip: **{elapsed_ms:.0f} ms**",
                    color=_COLOR,
                )
            )

    @fluxer.Cog.command(name="serverinfo")
    async def serverinfo(self, ctx: fluxer.Message) -> None:
        """Show information about this server."""
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        guild = self.bot._guilds.get(ctx.guild_id)
        if guild is None:
            await ctx.reply("Could not retrieve server information.")
            return

        created_str = guild.created_at.strftime("%d %b %Y")
        member_count = guild.member_count or "Unknown"

        embed = (
            fluxer.Embed(title=guild.name or "This Server", color=_COLOR)
            .add_field(name="Server ID", value=str(guild.id), inline=True)
            .add_field(name="Owner ID", value=str(guild.owner_id or "Unknown"), inline=True)
            .add_field(name="Members", value=str(member_count), inline=True)
            .add_field(name="Created", value=created_str, inline=True)
        )

        if guild.icon_url:
            embed.set_thumbnail(url=guild.icon_url)

        # FIX: send as a fresh message so the command text isn't quoted in the reply
        channel = self.bot._channels.get(ctx.channel_id)
        if channel:
            await channel.send(embed=embed)
        else:
            await ctx.reply(embed=embed)

    @fluxer.Cog.command(name="userinfo")
    async def userinfo(self, ctx: fluxer.Message) -> None:
        """Show information about a user. Mention them or leave blank for yourself.

        Usage:  !userinfo
                !userinfo @someone
        """
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        guild = self.bot._guilds.get(ctx.guild_id)

        # Resolve target: mentioned user or the caller
        target_user = ctx.mentions[0] if ctx.mentions else ctx.author

        # Try to fetch full member info from the guild for role/join data
        member = None
        if guild:
            try:
                member = await guild.fetch_member(target_user.id)
            except Exception:
                pass  # Not a member or fetch failed — show user info only

        created_str = target_user.created_at.strftime("%d %b %Y")
        joined_str = "Unknown"
        if member and member.joined_at:
            try:
                joined_dt = datetime.fromisoformat(member.joined_at.replace("Z", "+00:00"))
                joined_str = joined_dt.strftime("%d %b %Y")
            except ValueError:
                pass

        embed = (
            fluxer.Embed(
                title=target_user.display_name,
                description=f"@{target_user.username}",
                color=_COLOR,
            )
            .add_field(name="User ID", value=str(target_user.id), inline=True)
            .add_field(name="Account created", value=created_str, inline=True)
        )

        if member:
            embed.add_field(name="Joined server", value=joined_str, inline=True)
            if member.nick:
                embed.add_field(name="Nickname", value=member.nick, inline=True)

        if target_user.avatar_url:
            embed.set_thumbnail(url=target_user.avatar_url)

        # FIX: same as serverinfo — send fresh to avoid reply quoting the command
        channel = self.bot._channels.get(ctx.channel_id)
        if channel:
            await channel.send(embed=embed)
        else:
            await ctx.reply(embed=embed)

    @fluxer.Cog.command(name="remind")
    async def remind(self, ctx: fluxer.Message, *, args: str = "") -> None:
        """Set a reminder. Bot will reply in this channel after the delay.

        Usage:  !remind 30m Take a break
                !remind 2h Submit the report
                !remind 45s Check the oven

        Time units: s = seconds, m = minutes, h = hours
        Maximum: 24 hours
        """
        if not args:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}remind <time> <message>`\n"
                f"Example: `{config.COMMAND_PREFIX}remind 30m Take a break`\n"
                f"Units: `s` seconds · `m` minutes · `h` hours (max 24h)"
            )
            return

        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            await ctx.reply("Please include both a time and a reminder message.")
            return

        time_str, reminder_text = parts[0], parts[1]
        seconds = self._parse_time(time_str)

        if seconds is None:
            await ctx.reply(
                f"Couldn't parse **{time_str}** as a time.\n"
                f"Use a number followed by `s`, `m`, or `h` — e.g. `30m`, `2h`, `90s`."
            )
            return

        max_seconds = config.MAX_REMINDER_SECONDS
        if seconds > max_seconds:
            max_h = max_seconds // 3600
            await ctx.reply(f"Reminders are capped at {max_h} hours.")
            return

        if seconds < 5:
            await ctx.reply("Minimum reminder time is 5 seconds.")
            return

        # Acknowledge immediately so the user knows it's set
        human_time = self._humanise_seconds(seconds)
        await ctx.reply(
            embed=fluxer.Embed(
                title="Reminder set",
                description=f"I'll remind you in **{human_time}**:\n> {reminder_text}",
                color=_COLOR,
            )
        )

        # Fire and forget — the bot continues normally
        asyncio.create_task(
            self._send_reminder(ctx, reminder_text, seconds)
        )

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    async def _send_reminder(ctx: fluxer.Message, text: str, delay: float) -> None:
        await asyncio.sleep(delay)
        embed = fluxer.Embed(
            title="Reminder",
            description=text,
            color=_COLOR,
        ).set_footer(text=f"Reminder set by {ctx.author.display_name}")
        try:
            await ctx.reply(embed=embed)
        except Exception as exc:
            log.warning("Failed to send reminder: %s", exc)

    @staticmethod
    def _parse_time(s: str) -> float | None:
        """Convert a time string like '30m', '2h', '90s' to seconds."""
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", s.lower())
        if not match:
            return None
        value, unit = float(match.group(1)), match.group(2)
        return value * {"s": 1, "m": 60, "h": 3600}[unit]

    @staticmethod
    def _humanise_seconds(seconds: float) -> str:
        """Turn a number of seconds into a readable string."""
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds} second{'s' if seconds != 1 else ''}"
        if seconds < 3600:
            m = seconds // 60
            return f"{m} minute{'s' if m != 1 else ''}"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h} hour{'s' if h != 1 else ''}"


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(UtilityCog(bot))