"""
cogs/utility.py
---------------
General-purpose utility commands.

Commands:
    Tags:
        !tag add <name> [caption]  - Create a tag. Attach a WebP/AVIF/GIF/MP4/WebM
                                     image or video to the message to make a media tag.
                                     The caption is optional for media tags.
        !tag remove <name>         - Delete a tag (also removes stored media file)
        !tag list                  - List all tags in this server
        !tag info <name>           - Show tag details (author, created date, type)
        !<name>                    - Trigger a tag directly by name

    General:
        !ping                      - Bot latency check
        !serverinfo                - Info about this server
        !userinfo [@user]          - Info about a user (defaults to yourself)
        !remind <time> <message>   - Set a reminder (e.g. !remind 30m Take a break)
                                     Supports: s (seconds), m (minutes), h (hours)
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone

import aiohttp
import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.utility")

# Embed colour used across this cog
_COLOR = 0x5865F2

# Maximum number of tags per guild, to prevent abuse
_MAX_TAGS = 100

# Where media files are stored on disk
_MEDIA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "tag_media")

# Attachment extensions that are treated as media tags
_ALLOWED_MEDIA_EXTS: frozenset[str] = frozenset({
    # Modern image / animation formats (Discord blog post, March 2025)
    ".webp", ".avif",
    # Classic image formats
    ".png", ".jpg", ".jpeg", ".gif",
    # Video formats
    ".mp4", ".webm", ".mov",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _media_path(guild_id: int, tag_name: str, ext: str) -> str:
    """Return the absolute path where a tag's media file should be stored."""
    guild_dir = os.path.join(_MEDIA_DIR, str(guild_id))
    os.makedirs(guild_dir, exist_ok=True)
    return os.path.join(guild_dir, f"{tag_name}{ext}")


async def _download_to_disk(url: str, dest_path: str) -> None:
    """Download *url* and write the bytes to *dest_path*."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)


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
                    f"`{config.COMMAND_PREFIX}tag add <name> [caption]` — Create a tag\n"
                    f"  • Attach a WebP/AVIF/GIF/MP4/WebM file to make a **media tag**\n"
                    f"  • Or just provide text for a plain **text tag**\n"
                    f"`{config.COMMAND_PREFIX}tag remove <name>` — Delete a tag\n"
                    f"`{config.COMMAND_PREFIX}tag list` — List all tags\n"
                    f"`{config.COMMAND_PREFIX}tag info <name>` — Tag details\n"
                    f"`{config.COMMAND_PREFIX}<name>` — Use a tag directly"
                ),
                color=_COLOR,
            )
            await ctx.reply(embed=embed)

    async def _tag_add(self, ctx: fluxer.Message, parts: list[str]) -> None:
        """
        Create a tag.

        Text tag:   !tag add <name> <text>
        Media tag:  !tag add <name> [optional caption]  + attached file
        """
        # ── Validate we have at least a name ─────────────────────────────────
        # parts = ["add", "name", ...]  — need at least index 1
        has_name = len(parts) >= 2

        # Grab the raw attachments list from the underlying message object.
        # _MessageWrapper.__getattr__ falls through to the discord.Message /
        # fluxer.Message so .attachments is always available.
        attachments = getattr(ctx, "attachments", []) or []
        has_attachment = bool(attachments)

        if not has_name:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}tag add <name> [caption]`\n"
                f"For a media tag, attach a file (WebP, AVIF, GIF, MP4, WebM, …) "
                f"to the same message.\n"
                f"Example: `{config.COMMAND_PREFIX}tag add myclip` ← with an attachment\n"
                f"Example: `{config.COMMAND_PREFIX}tag add rules Read the rules channel!`"
            )
            return

        # If there's no attachment and no text either, bail out
        if not has_attachment and len(parts) < 3:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}tag add <name> <text>`\n"
                f"To make a media tag, attach a file to the same message.\n"
                f"Example: `{config.COMMAND_PREFIX}tag add rules Read #rules before chatting!`"
            )
            return

        name = parts[1].lower()
        caption = parts[2].strip() if len(parts) >= 3 else ""

        # ── Validate name ────────────────────────────────────────────────────
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

        # ── Branch: media tag vs text tag ────────────────────────────────────
        if has_attachment:
            attachment = attachments[0]

            # attachment.filename exists on both discord.py (Attachment.filename)
            # and Fluxer (mirrors the same attribute name)
            original_filename: str = getattr(attachment, "filename", "") or ""
            _, ext = os.path.splitext(original_filename.lower())

            if ext not in _ALLOWED_MEDIA_EXTS:
                allowed = ", ".join(sorted(_ALLOWED_MEDIA_EXTS))
                await ctx.reply(
                    f"Unsupported file type `{ext or '(none)'}`. "
                    f"Allowed: {allowed}"
                )
                return

            # Attachment URL: discord.py → attachment.url; Fluxer → same
            attachment_url: str = getattr(attachment, "url", "") or ""
            if not attachment_url:
                await ctx.reply("Could not read the attachment URL. Please try again.")
                return

            dest_path = _media_path(ctx.guild_id, name, ext)

            try:
                await _download_to_disk(attachment_url, dest_path)
            except Exception as exc:
                log.exception("Failed to download attachment for tag '%s': %s", name, exc)
                await ctx.reply(
                    f"Failed to download the attachment: `{exc}`\n"
                    f"The file has not been saved."
                )
                return

            tags[name] = {
                "type": "media",
                "media_path": dest_path,
                "filename": f"{name}{ext}",  # clean name for re-upload
                "caption": caption,
                "author_id": ctx.author.id,
                "author_name": ctx.author.display_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.settings.set(ctx.guild_id, "tags", tags)
            log.info(
                "Media tag '%s' created in guild %d by %s (file: %s)",
                name, ctx.guild_id, ctx.author.username, dest_path,
            )

            description = (
                f"Media tag **{name}** is ready (`{ext}`).\n"
                f"Use it with `{config.COMMAND_PREFIX}{name}`."
            )
            if caption:
                description += f"\nCaption: {caption}"

            await ctx.reply(
                embed=fluxer.Embed(
                    title="Media tag created",
                    description=description,
                    color=_COLOR,
                )
            )

        else:
            # Plain text tag — original behaviour
            text = parts[2]

            tags[name] = {
                "type": "text",
                "text": text,
                "author_id": ctx.author.id,
                "author_name": ctx.author.display_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.settings.set(ctx.guild_id, "tags", tags)
            log.info(
                "Text tag '%s' created in guild %d by %s",
                name, ctx.guild_id, ctx.author.username,
            )

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

        tag = tags[name]

        # Delete the media file from disk if this is a media tag
        if tag.get("type") == "media":
            media_path: str = tag.get("media_path", "")
            if media_path and os.path.isfile(media_path):
                try:
                    os.remove(media_path)
                    log.info("Deleted media file: %s", media_path)
                except OSError as exc:
                    log.warning("Could not delete media file %s: %s", media_path, exc)

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

        # Annotate media tags with a 🖼 indicator
        names = sorted(tags.keys())
        tag_list = "  ".join(
            f"`{n}` 🖼" if tags[n].get("type") == "media" else f"`{n}`"
            for n in names
        )

        embed = fluxer.Embed(
            title=f"Tags — {len(tags)} total",
            description=tag_list,
            color=_COLOR,
        ).set_footer(text=f"🖼 = media tag  •  Use {config.COMMAND_PREFIX}<name> to trigger a tag.")

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

        tag_type = tag.get("type", "text")

        if tag_type == "media":
            filename   = tag.get("filename", "unknown")
            _, ext     = os.path.splitext(filename)
            media_path = tag.get("media_path", "")
            on_disk    = "✅ on disk" if os.path.isfile(media_path) else "❌ file missing"
            caption    = tag.get("caption") or "*(none)*"

            embed = (
                fluxer.Embed(title=f"Tag: {name}", color=_COLOR)
                .add_field(name="Type",       value=f"Media (`{ext}`)",      inline=True)
                .add_field(name="File",        value=f"`{filename}` {on_disk}", inline=True)
                .add_field(name="Caption",     value=caption,                inline=False)
                .add_field(name="Created by",  value=tag.get("author_name", "Unknown"), inline=True)
                .add_field(name="Created at",  value=created_str,            inline=True)
            )
        else:
            embed = (
                fluxer.Embed(title=f"Tag: {name}", color=_COLOR)
                .add_field(name="Type",        value="Text",                 inline=True)
                .add_field(name="Content",     value=tag.get("text", ""),    inline=False)
                .add_field(name="Created by",  value=tag.get("author_name", "Unknown"), inline=True)
                .add_field(name="Created at",  value=created_str,            inline=True)
            )

        await ctx.reply(embed=embed)

    # =========================================================================
    # Tag trigger — on_message listener
    # =========================================================================

    @fluxer.Cog.listener()
    async def on_message(self, message: fluxer.Message) -> None:
        """Check every message to see if it calls a tag by name."""
        log.debug("on_message fired: guild=%s author=%s content=%r",
                  message.guild_id, message.author.username, message.content)

        if message.author.bot:
            log.debug("on_message: ignoring bot message")
            return
        if message.guild_id is None:
            log.debug("on_message: ignoring DM")
            return
        if not message.content.startswith(config.COMMAND_PREFIX):
            return

        # Strip prefix and grab the first word as the potential tag name
        body = message.content[len(config.COMMAND_PREFIX):].strip()
        name = body.split()[0].lower() if body.split() else ""
        log.debug("on_message: prefix matched, name=%r", name)

        if not name:
            return

        # If it's a real command, do nothing — the framework dispatches it
        if name in self.bot._commands:
            log.debug("on_message: '%s' is a real command, skipping", name)
            return

        tags: dict = self.settings.get(message.guild_id, "tags") or {}
        log.debug("on_message: tags in guild=%s", list(tags.keys()))
        if name not in tags:
            log.debug("on_message: '%s' not a tag, skipping", name)
            return

        tag = tags[name]
        tag_type = tag.get("type", "text")

        if tag_type == "media":
            media_path: str = tag.get("media_path", "")
            filename: str   = tag.get("filename", "media")
            caption: str    = tag.get("caption", "")

            if not media_path or not os.path.isfile(media_path):
                log.warning("Tag '%s' media file missing at %s", name, media_path)
                await message.reply(
                    f"⚠️ The media file for tag **{name}** is missing. "
                    f"An admin may need to recreate it."
                )
                return

            try:
                # fluxer.File and discord.File share the same constructor:
                # File(fp, filename=...) where fp is a binary file-like object.
                with open(media_path, "rb") as fp:
                    media_file = fluxer.File(fp, filename=filename)
                    await message.reply(
                        content=caption if caption else None,
                        file=media_file,
                    )
            except Exception as exc:
                log.exception("Failed to send media for tag '%s': %s", name, exc)
                await message.reply(f"⚠️ Could not send the media for tag **{name}**: `{exc}`")

        else:
            await message.reply(tag.get("text", ""))

        log.info(
            "Tag '%s' triggered in guild %d (type=%s)",
            name, message.guild_id, tag_type,
        )

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