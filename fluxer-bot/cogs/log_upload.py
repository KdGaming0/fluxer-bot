"""
cogs/log_upload.py
------------------
Automatically detects Minecraft crash reports and log files posted in chat —
either as a .txt / .log file attachment or as a long inline paste ��� uploads
them to mclo.gs, deletes the original message, and posts a clean embed with
the mclo.gs link, detected software info, and any identified problems.

Commands (require Manage Guild):
    !logupload on          - Enable auto-upload in this server (default: off)
    !logupload off         - Disable auto-upload in this server
    !logupload channel #ch - Only process logs posted in a specific channel
                             (leave blank to allow any channel)
    !logupload channel     - Clear channel restriction
    !logupload status      - Show current settings

Detection triggers
------------------
Inline text messages:
    • >= LOG_UPLOAD_MIN_LINES lines  AND contains at least one strong MC signal.
    • >= LOG_UPLOAD_MIN_CHARS chars  AND contains at least one strong MC signal.

File attachments:
    • Extension in ALLOWED_EXTS (.txt, .log, .crash, .md)  AND the downloaded
      content contains at least one strong MC signal.
    • File size is capped at MAX_FILE_BYTES before uploading to stay within
      mclo.gs limits.

Strong MC signals (any one is sufficient):
    • "---- Minecraft Crash Report ----"
    • "---- Crash Report ----"          (some launchers use this shorter form)
    • "#@!@# Game crashed! #@!@#"
    • "[xx:xx:xx] [" — bracketed timestamp + thread (log line pattern)
    • "at net.minecraft."               (Java stack trace)
    • "at com.mojang."
    • "net.minecraft.client.main.Main"
    • "Exception in thread \""
    • "java.lang.OutOfMemoryError"
    • "FML" or "Forge Mod Loader" lines
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import aiohttp
import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.log_upload")

# ── Tunables ──────────────────────────────────────────────────────────────────

# Inline message must have at least this many lines to be considered
LOG_UPLOAD_MIN_LINES: int = 15

# … OR at least this many characters
LOG_UPLOAD_MIN_CHARS: int = 1000

# File attachments larger than this (bytes) are truncated before upload.
# mclo.gs hard limit is 10 MiB; we keep well below that.
MAX_FILE_BYTES: int = 8 * 1024 * 1024   # 8 MiB

# File extensions that we will try to process as logs
ALLOWED_EXTS: frozenset[str] = frozenset({".txt", ".log", ".crash", ".md"})

# mclo.gs endpoints
_MCLOGS_UPLOAD  = "https://api.mclo.gs/1/log"
_MCLOGS_ANALYSE = "https://api.mclo.gs/1/analyse"

# Embed colour
_COLOR = 0x5865F2
_COLOR_ERR = 0xED4245

# ── Strong signal patterns ────────────────────────────────────────────────────
# Any single match is sufficient to treat the content as a Minecraft log.

_STRONG_SIGNALS: list[re.Pattern] = [
    re.compile(r"----\s*(?:Minecraft\s+)?Crash Report\s*----", re.I),
    re.compile(r"#@!@#\s*Game crashed!", re.I),
    # Standard log line: [HH:MM:SS] [ThreadName/LEVEL]:
    re.compile(r"\[\d{2}:\d{2}:\d{2}\]\s*\["),
    re.compile(r"\bat\s+net\.minecraft\.", re.I),
    re.compile(r"\bat\s+com\.mojang\.", re.I),
    re.compile(r"net\.minecraft\.client\.main\.Main", re.I),
    re.compile(r'Exception in thread\s+"'),
    re.compile(r"java\.lang\.OutOfMemoryError", re.I),
    re.compile(r"Forge Mod Loader|FML\b", re.I),
    # Fabric/Quilt loader lines
    re.compile(r"\[Fabric Loader\]|\[fabric-loader\]", re.I),
    # Common launcher log headers
    re.compile(r"MultiMC version:|Prism Launcher|ATLauncher|CurseForge.*launcher", re.I),
]


def _looks_like_mc_log(content: str) -> bool:
    """Return True if *content* contains at least one strong Minecraft log signal."""
    for pattern in _STRONG_SIGNALS:
        if pattern.search(content):
            return True
    return False


def _inline_qualifies(content: str) -> bool:
    """Return True if an inline message should be processed as a log paste."""
    if len(content) < LOG_UPLOAD_MIN_CHARS and content.count("\n") < LOG_UPLOAD_MIN_LINES:
        return False
    return _looks_like_mc_log(content)


class LogUploadCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()

    # =========================================================================
    # Admin commands
    # =========================================================================

    @fluxer.Cog.command(name="logupload")
    @fluxer.has_permission(fluxer.Permissions.MANAGE_GUILD)
    async def logupload(self, ctx: fluxer.Message, *, args: str = "") -> None:
        """Dispatcher for !logupload sub-commands."""
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        parts  = args.strip().split(None, 1)
        sub    = parts[0].lower() if parts else ""
        rest   = parts[1].strip() if len(parts) > 1 else ""

        if sub == "on":
            await self._subcmd_on(ctx)
        elif sub == "off":
            await self._subcmd_off(ctx)
        elif sub == "channel":
            await self._subcmd_channel(ctx, rest)
        elif sub == "status":
            await self._subcmd_status(ctx)
        else:
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Log Upload commands",
                    description=(
                        f"`{config.COMMAND_PREFIX}logupload on` — Enable auto log upload\n"
                        f"`{config.COMMAND_PREFIX}logupload off` — Disable auto log upload\n"
                        f"`{config.COMMAND_PREFIX}logupload channel #ch` — Restrict to one channel\n"
                        f"`{config.COMMAND_PREFIX}logupload channel` — Allow any channel\n"
                        f"`{config.COMMAND_PREFIX}logupload status` — Show current settings\n\n"
                        "When enabled, Minecraft crash reports and log pastes are automatically "
                        "uploaded to **mclo.gs** and the raw paste is deleted."
                    ),
                    color=_COLOR,
                )
            )

    async def _subcmd_on(self, ctx: fluxer.Message) -> None:
        self.settings.set(ctx.guild_id, "logupload_enabled", True)
        log.info("Log upload enabled in guild %d by %s", ctx.guild_id, ctx.author.username)
        await ctx.reply(
            embed=fluxer.Embed(
                title="Log upload enabled",
                description=(
                    "Minecraft crash reports and log pastes will now be automatically "
                    "uploaded to **mclo.gs**.\n\n"
                    f"To restrict to one channel: "
                    f"`{config.COMMAND_PREFIX}logupload channel #channel-name`"
                ),
                color=_COLOR,
            )
        )

    async def _subcmd_off(self, ctx: fluxer.Message) -> None:
        self.settings.set(ctx.guild_id, "logupload_enabled", False)
        log.info("Log upload disabled in guild %d by %s", ctx.guild_id, ctx.author.username)
        await ctx.reply(
            embed=fluxer.Embed(
                title="Log upload disabled",
                description="Automatic log detection is now off for this server.",
                color=_COLOR,
            )
        )

    async def _subcmd_channel(self, ctx: fluxer.Message, args: str) -> None:
        """Set or clear the channel restriction."""
        if not args:
            self.settings.delete(ctx.guild_id, "logupload_channel_id")
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Channel restriction cleared",
                    description="Log upload will now trigger in **any** channel.",
                    color=_COLOR,
                )
            )
            return

        import re as _re
        channel = None
        m = _re.search(r"<#(\d+)>", args)
        if m:
            channel = self.bot._channels.get(int(m.group(1)))
        else:
            for ch in self.bot._channels.values():
                if ch.guild_id == ctx.guild_id and ch.name == args.lstrip("#"):
                    channel = ch
                    break

        if channel is None:
            await ctx.reply(f"Could not find that channel. Try mentioning it: `#channel-name`")
            return

        self.settings.set(ctx.guild_id, "logupload_channel_id", channel.id)
        await ctx.reply(
            embed=fluxer.Embed(
                title="Channel restriction set",
                description=f"Log upload will only trigger in **#{channel.name}**.",
                color=_COLOR,
            )
        )

    async def _subcmd_status(self, ctx: fluxer.Message) -> None:
        enabled    = self.settings.get(ctx.guild_id, "logupload_enabled", default=False)
        channel_id = self.settings.get(ctx.guild_id, "logupload_channel_id")

        channel_str = f"<#{channel_id}>" if channel_id else "Any channel"
        status_str  = "✅ Enabled" if enabled else "❌ Disabled"

        await ctx.reply(
            embed=fluxer.Embed(
                title="Log Upload status",
                color=_COLOR,
            )
            .add_field(name="Status",  value=status_str,  inline=True)
            .add_field(name="Channel", value=channel_str, inline=True)
        )

    # =========================================================================
    # Listener
    # =========================================================================

    @fluxer.Cog.listener()
    async def on_message(self, message: fluxer.Message) -> None:
        """Check every message for Minecraft log content."""
        if message.author.bot:
            return
        if message.guild_id is None:
            return

        # ── Ignore commands ───────────────────────────────────────────────────
        # Prevents false positives when running commands (e.g. !tag add ... Prism Launcher)
        if message.content and message.content.startswith(config.COMMAND_PREFIX):
            return

        # ── Check feature is enabled for this guild ───────────────────────────
        if not self.settings.get(message.guild_id, "logupload_enabled", default=False):
            return

        # ── Check channel restriction ─────────────────────────────────────────
        allowed_channel = self.settings.get(message.guild_id, "logupload_channel_id")
        if allowed_channel and message.channel_id != allowed_channel:
            return

        # ── Try to get log content ────────────────────────────────────────────
        content: Optional[str] = None
        source_label: str      = "inline paste"

        attachments = getattr(message, "attachments", []) or []

        if attachments:
            # Process the first qualifying attachment
            for attachment in attachments:
                filename: str = getattr(attachment, "filename", "") or ""
                import os as _os
                _, ext = _os.path.splitext(filename.lower())
                if ext not in ALLOWED_EXTS:
                    continue

                file_size = getattr(attachment, "size", 0) or 0
                if file_size > MAX_FILE_BYTES:
                    log.debug(
                        "Attachment '%s' (%d bytes) exceeds cap — skipping",
                        filename, file_size,
                    )
                    continue

                url: str = getattr(attachment, "url", "") or ""
                if not url:
                    continue

                try:
                    raw = await self._download_text(url)
                except Exception as exc:
                    log.warning("Failed to download attachment '%s': %s", filename, exc)
                    continue

                if not _looks_like_mc_log(raw):
                    continue

                content      = raw
                source_label = f"file: `{filename}`"
                break  # stop after the first valid attachment

        elif message.content and _inline_qualifies(message.content):
            content      = message.content
            source_label = "inline paste"

        if content is None:
            return

        # ── Upload to mclo.gs ─────────────────────────────────────────────────
        log.info(
            "Uploading log from %s in guild %d (%s, %d chars)",
            message.author.username, message.guild_id, source_label, len(content),
        )

        upload_result = await self._upload_log(content)

        if upload_result is None:
            # Upload failed — don't delete the original, just bail silently
            # (error already logged inside _upload_log)
            return

        mclog_url: str = upload_result.get("url", "")
        errors: int    = upload_result.get("errors", 0)

        if not mclog_url:
            log.error("mclo.gs response missing 'url' field: %s", upload_result)
            return

        # ── Fetch insights to surface detected problems ───────────────────────
        insights = await self._fetch_insights(upload_result.get("id", ""))

        # ── Delete original message ───────────────────────────────────────────
        try:
            await message.delete()
            log.info("Deleted original log paste from %s", message.author.username)
        except Exception as exc:
            log.warning(
                "Could not delete original message (missing Manage Messages?): %s", exc
            )
            # Continue anyway — the embed will still be useful even alongside the raw paste

        # ── Post the result embed ─────────────────────────────────────────────
        embed = self._build_result_embed(
            author_mention = message.author.mention,
            author_name    = message.author.display_name,
            mclog_url      = mclog_url,
            errors         = errors,
            source_label   = source_label,
            insights       = insights,
            upload_result  = upload_result,
        )

        channel = self.bot._channels.get(message.channel_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except Exception as exc:
                log.error("Failed to send log upload embed: %s", exc)

    # =========================================================================
    # mclo.gs API helpers
    # =========================================================================

    @staticmethod
    async def _download_text(url: str) -> str:
        """Download a URL and return its content as a UTF-8 string."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                raw_bytes = await resp.read()
        # Truncate if oversized (shouldn't happen due to size check, but be safe)
        return raw_bytes[:MAX_FILE_BYTES].decode("utf-8", errors="replace")

    @staticmethod
    async def _upload_log(content: str) -> Optional[dict]:
        """
        Upload *content* to mclo.gs and return the parsed JSON response,
        or None if the upload failed.
        """
        payload = json.dumps({"content": content})
        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _MCLOGS_UPLOAD,
                    data=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json(content_type=None)
        except Exception as exc:
            log.error("mclo.gs upload request failed: %s", exc)
            return None

        if not data.get("success"):
            log.error("mclo.gs upload error: %s", data.get("error", "(no error field)"))
            return None

        return data

    @staticmethod
    async def _fetch_insights(log_id: str) -> Optional[dict]:
        """
        Fetch insights for an already-uploaded log from mclo.gs.
        Returns the analysis dict or None on failure.
        """
        if not log_id:
            return None

        url = f"https://api.mclo.gs/1/log/{log_id}?insights=1"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json(content_type=None)
        except Exception as exc:
            log.warning("mclo.gs insights fetch failed: %s", exc)
            return None

        if not data.get("success"):
            return None

        return data.get("content", {}).get("insights")

    # =========================================================================
    # Embed builder
    # =========================================================================

    @staticmethod
    def _build_result_embed(
        *,
        author_mention: str,
        author_name: str,
        mclog_url: str,
        errors: int,
        source_label: str,
        insights: Optional[dict],
        upload_result: dict,
    ) -> fluxer.Embed:
        """Build the embed that replaces the raw log paste."""

        # ── Title line: software detection ───────────────────────────────────
        # Insights endpoint returns a flat dict with `information` list.
        software_title = ""
        if insights:
            information: list = insights.get("information") or []
            for info in information:
                label = (info.get("label") or "").lower()
                if "minecraft" in label or "version" in label or "software" in label:
                    software_title = info.get("message", "")
                    break

        title = software_title if software_title else "Minecraft Log"

        # ── Description ───────────────────────────────────────────────────────
        description = (
            f"{author_mention} your message was detected as a Minecraft crash report or log file. "
            f"It has been automatically uploaded to mclo.gs to keep the chat clean and readable.\n\n"
            f"🔗 **[View your log on mclo.gs]({mclog_url})**"
        )

        embed = fluxer.Embed(title=title, description=description, color=_COLOR)

        # ── Error count field ─────────────────────────────────────────────────
        err_str = f"{errors:,}" if errors else "None detected"
        embed.add_field(name="Errors in log", value=err_str, inline=True)
        embed.add_field(name="Source",         value=source_label, inline=True)

        # ── Detected problems from insights ───────────────────────────────────
        if insights:
            problems: list = insights.get("problems") or []
            if problems:
                # Show up to 3 problems to keep the embed readable
                problem_lines = []
                for problem in problems[:3]:
                    msg = problem.get("message", "")
                    if not msg:
                        continue
                    solutions = problem.get("solutions") or []
                    if solutions:
                        sol = solutions[0].get("message", "")
                        problem_lines.append(f"**Problem:** {msg}\n↳ {sol}")
                    else:
                        problem_lines.append(f"**Problem:** {msg}")

                if problem_lines:
                    embed.add_field(
                        name=f"⚠️ Detected problems ({min(len(problems), 3)} of {len(problems)})",
                        value="\n\n".join(problem_lines),
                        inline=False,
                    )

        embed.set_footer(text=f"Uploaded by {author_name}  •  Logs expire after 90 days")
        return embed


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(LogUploadCog(bot))