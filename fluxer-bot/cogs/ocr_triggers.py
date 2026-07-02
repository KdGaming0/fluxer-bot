"""
cogs/ocr_triggers.py
--------------------
Runs OCR (via the ``tesseract`` binary) on every image posted in a set of
watched support channels and replies with a configured message when the
extracted text contains a configured trigger string.

Typical use case: users screenshot a crash GUI that explicitly tells them
NOT to send a screenshot. The GUI text is detected and the bot replies
telling them to use the log-upload button instead. The original message is
left untouched — the bot only replies underneath it.

Commands (require Manage Guild):
    !ocr add <trigger text> | <reply>   - Add a trigger with its custom reply
    !ocr remove <number>                - Remove a trigger (number from !ocr list)
    !ocr list                           - List all configured triggers
    !ocr edit <number> | <new reply>    - Change the reply for a trigger
    !ocr show <number>                  - Show a trigger's full text and reply
    !ocr channel add #channel           - Watch a channel for images
    !ocr channel remove #channel        - Stop watching a channel
    !ocr channel list                   - List watched channels
    !ocr on / off                       - Toggle the whole feature
    !ocr status                         - Show current configuration

Matching is fuzzy: OCR output is noisy, so trigger strings are compared
case-insensitively, with punctuation stripped, and a sliding-window
similarity check (ratio >= 0.8) in addition to plain substring matching.

Requires the ``tesseract`` binary on PATH (Debian/Ubuntu: ``apt install
tesseract-ocr``, Arch: ``pacman -S tesseract tesseract-data-eng``).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
from typing import Optional

import aiohttp
import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.ocr_triggers")

_COLOR_INFO = 0x5865F2
_COLOR_OK   = 0x57F287
_COLOR_WARN = 0xFEE75C

# ── Tunables ──────────────────────────────────────────────────────────────────

# Skip images larger than this (bytes) — screenshots are far smaller.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Only OCR the first N image attachments of a message.
_MAX_IMAGES_PER_MESSAGE = 3

# Kill tesseract if it takes longer than this (seconds).
_OCR_TIMEOUT_SECONDS = 30

# How many tesseract processes may run at the same time.
_OCR_CONCURRENCY = 2

# Similarity ratio required for a fuzzy (non-substring) trigger match.
_FUZZY_THRESHOLD = 0.75

# Only fuzzy-scan the first N characters of OCR output (screenshots are short).
_MAX_FUZZY_HAYSTACK = 8000

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


# ── Text normalisation / fuzzy matching ───────────────────────────────────────

def _normalise(text: str) -> str:
    """Lowercase, strip everything except letters/digits, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fuzzy_contains(haystack: str, needle: str) -> bool:
    """Return True if *needle* appears in *haystack*, allowing OCR noise.

    Both arguments must already be normalised. Tries a plain substring match
    first, then compares with all whitespace stripped (OCR frequently merges
    or splits words), sliding a character window across the haystack and
    accepting any window with a similarity ratio >= _FUZZY_THRESHOLD.
    """
    if not needle:
        return False
    if needle in haystack:
        return True

    h = haystack.replace(" ", "")[:_MAX_FUZZY_HAYSTACK]
    n = needle.replace(" ", "")
    if not n or not h:
        return False
    if n in h:
        return True

    ln = len(n)
    if len(h) <= ln:
        return difflib.SequenceMatcher(None, h, n).ratio() >= _FUZZY_THRESHOLD

    step = max(1, ln // 8)
    for i in range(0, len(h) - ln + 1, step):
        window = h[i : i + ln]
        if difflib.SequenceMatcher(None, window, n).ratio() >= _FUZZY_THRESHOLD:
            return True
    return False


def _is_image_attachment(attachment) -> bool:
    content_type: str = getattr(attachment, "content_type", "") or ""
    if content_type.startswith("image/"):
        return True
    filename: str = getattr(attachment, "filename", "") or ""
    _, ext = os.path.splitext(filename.lower())
    return ext in _IMAGE_EXTS


class OcrTriggersCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()
        self._ocr_sem = asyncio.Semaphore(_OCR_CONCURRENCY)

    # =========================================================================
    # Admin commands
    # =========================================================================

    @fluxer.Cog.command(name="ocr")
    @fluxer.has_permission(fluxer.Permissions.MANAGE_GUILD)
    async def ocr(self, ctx: fluxer.Message) -> None:
        """Manage screenshot OCR detection (requires Manage Guild).

        Runs OCR on every image posted in the watched channels and replies
        with a configured message when a trigger string is found in the
        image text.

        Usage:
            !ocr add <trigger text> | <reply>   Add a trigger + custom reply
            !ocr remove <number>                Remove a trigger
            !ocr list                           List all triggers
            !ocr edit <number> | <new reply>    Change a trigger's reply
            !ocr show <number>                  Show full trigger + reply
            !ocr channel add/remove #channel    Manage watched channels
            !ocr channel list                   List watched channels
            !ocr on / off                       Toggle the feature
            !ocr status                         Show current configuration

        Run !ocr with no arguments for the same overview in Discord.
        """
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        args  = ctx.content.removeprefix(f"{config.COMMAND_PREFIX}ocr").strip()
        parts = args.split(None, 1)
        sub   = parts[0].lower() if parts else ""
        rest  = parts[1].strip() if len(parts) > 1 else ""

        if sub == "add":
            await self._trigger_add(ctx, rest)
        elif sub == "remove":
            await self._trigger_remove(ctx, rest)
        elif sub == "list":
            await self._trigger_list(ctx)
        elif sub == "edit":
            await self._trigger_edit(ctx, rest)
        elif sub == "show":
            await self._trigger_show(ctx, rest)
        elif sub == "channel":
            await self._channel_cmd(ctx, rest)
        elif sub in ("on", "off"):
            await self._toggle(ctx, sub == "on")
        elif sub == "status":
            await self._status(ctx)
        else:
            p = config.COMMAND_PREFIX
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Screenshot OCR commands",
                    description=(
                        f"`{p}ocr add <trigger text> | <reply>` — Add a trigger\n"
                        f"`{p}ocr remove <number>` — Remove a trigger\n"
                        f"`{p}ocr list` — List all triggers\n"
                        f"`{p}ocr edit <number> | <new reply>` — Edit a trigger's reply\n"
                        f"`{p}ocr show <number>` — Show full trigger + reply\n"
                        f"`{p}ocr channel add/remove #channel` — Manage watched channels\n"
                        f"`{p}ocr channel list` — List watched channels\n"
                        f"`{p}ocr on/off` — Toggle the feature\n"
                        f"`{p}ocr status` — Show current configuration\n\n"
                        "Every image posted in a watched channel is OCR'd; if the "
                        "extracted text contains a trigger string, the bot replies "
                        "with that trigger's configured message."
                    ),
                    color=_COLOR_INFO,
                )
            )

    # ── Trigger management ────────────────────────────────────────────────────

    async def _trigger_add(self, ctx: fluxer.Message, rest: str) -> None:
        if "|" not in rest:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}ocr add <trigger text> | <reply>`\n"
                f"Example: `{config.COMMAND_PREFIX}ocr add A screenshot of this GUI "
                f"tells us nothing | Please read the crash screen: click the button to "
                f"upload your logs and paste the generated message here.`"
            )
            return

        trigger_text, reply_text = (s.strip() for s in rest.split("|", 1))
        if not trigger_text or not reply_text:
            await ctx.reply("Both the trigger text and the reply must be non-empty.")
            return

        triggers: list = self.settings.get(ctx.guild_id, "ocr_triggers") or []
        if any(_normalise(t["trigger"]) == _normalise(trigger_text) for t in triggers):
            await ctx.reply("That trigger already exists. Use `ocr edit` to change its reply.")
            return

        triggers.append({"trigger": trigger_text, "reply": reply_text})
        self.settings.set(ctx.guild_id, "ocr_triggers", triggers)
        log.info("OCR trigger added in guild %d by %s", ctx.guild_id, ctx.author.username)

        await ctx.reply(
            embed=fluxer.Embed(
                title=f"OCR trigger #{len(triggers)} added",
                description=f"**Trigger:** {trigger_text}\n**Reply:** {reply_text}",
                color=_COLOR_OK,
            )
        )

    def _resolve_trigger_index(self, ctx: fluxer.Message, arg: str) -> Optional[int]:
        """Turn a user-supplied trigger reference (list number) into a 0-based index."""
        triggers: list = self.settings.get(ctx.guild_id, "ocr_triggers") or []
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(triggers):
                return idx
        return None

    async def _trigger_remove(self, ctx: fluxer.Message, rest: str) -> None:
        idx = self._resolve_trigger_index(ctx, rest)
        if idx is None:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}ocr remove <number>` — see "
                f"`{config.COMMAND_PREFIX}ocr list` for numbers."
            )
            return

        triggers: list = self.settings.get(ctx.guild_id, "ocr_triggers") or []
        removed = triggers.pop(idx)
        self.settings.set(ctx.guild_id, "ocr_triggers", triggers)

        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Removed trigger: **{removed['trigger'][:200]}**",
                color=_COLOR_OK,
            )
        )

    async def _trigger_list(self, ctx: fluxer.Message) -> None:
        triggers: list = self.settings.get(ctx.guild_id, "ocr_triggers") or []
        if not triggers:
            await ctx.reply(
                f"No OCR triggers configured yet. Add one with "
                f"`{config.COMMAND_PREFIX}ocr add <trigger> | <reply>`."
            )
            return

        lines = []
        for i, t in enumerate(triggers, start=1):
            trig = t["trigger"][:60] + ("…" if len(t["trigger"]) > 60 else "")
            repl = t["reply"][:80] + ("…" if len(t["reply"]) > 80 else "")
            lines.append(f"**{i}.** `{trig}`\n↳ {repl}")

        await ctx.reply(
            embed=fluxer.Embed(
                title=f"OCR triggers — {len(triggers)} total",
                description="\n\n".join(lines)[:4000],
                color=_COLOR_INFO,
            )
        )

    async def _trigger_edit(self, ctx: fluxer.Message, rest: str) -> None:
        if "|" not in rest:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}ocr edit <number> | <new reply>`"
            )
            return

        num_part, new_reply = (s.strip() for s in rest.split("|", 1))
        idx = self._resolve_trigger_index(ctx, num_part)
        if idx is None or not new_reply:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}ocr edit <number> | <new reply>` — see "
                f"`{config.COMMAND_PREFIX}ocr list` for numbers."
            )
            return

        triggers: list = self.settings.get(ctx.guild_id, "ocr_triggers") or []
        triggers[idx]["reply"] = new_reply
        self.settings.set(ctx.guild_id, "ocr_triggers", triggers)

        await ctx.reply(
            embed=fluxer.Embed(
                title=f"OCR trigger #{idx + 1} updated",
                description=f"**Trigger:** {triggers[idx]['trigger'][:200]}\n**New reply:** {new_reply}",
                color=_COLOR_OK,
            )
        )

    async def _trigger_show(self, ctx: fluxer.Message, rest: str) -> None:
        idx = self._resolve_trigger_index(ctx, rest)
        if idx is None:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}ocr show <number>`")
            return

        triggers: list = self.settings.get(ctx.guild_id, "ocr_triggers") or []
        t = triggers[idx]
        await ctx.reply(
            embed=fluxer.Embed(title=f"OCR trigger #{idx + 1}", color=_COLOR_INFO)
            .add_field(name="Trigger text", value=t["trigger"][:1000], inline=False)
            .add_field(name="Reply",        value=t["reply"][:1000],   inline=False)
        )

    # ── Channel management ────────────────────────────────────────────────────

    def _resolve_channel(self, ctx: fluxer.Message, arg: str):
        match = re.search(r"<#(\d+)>", arg)
        if match:
            return self.bot._channels.get(int(match.group(1)))
        name = arg.lstrip("#").strip()
        for ch in self.bot._channels.values():
            if ch.guild_id == ctx.guild_id and ch.name == name:
                return ch
        return None

    async def _channel_cmd(self, ctx: fluxer.Message, rest: str) -> None:
        parts  = rest.split(None, 1)
        action = parts[0].lower() if parts else ""
        arg    = parts[1].strip() if len(parts) > 1 else ""

        channels: list = self.settings.get(ctx.guild_id, "ocr_channels") or []

        if action == "list":
            desc = "\n".join(f"<#{cid}>" for cid in channels) if channels else (
                "No channels watched yet — OCR is inactive until you add one."
            )
            await ctx.reply(
                embed=fluxer.Embed(
                    title=f"OCR watched channels — {len(channels)}",
                    description=desc,
                    color=_COLOR_INFO,
                )
            )
            return

        if action not in ("add", "remove") or not arg:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}ocr channel add/remove #channel` "
                f"or `{config.COMMAND_PREFIX}ocr channel list`"
            )
            return

        channel = self._resolve_channel(ctx, arg)
        if channel is None or not channel.is_text_channel:
            await ctx.reply("Could not find that text channel.")
            return

        if action == "add":
            if channel.id in channels:
                await ctx.reply(f"**#{channel.name}** is already being watched.")
                return
            channels.append(channel.id)
            verb = "now watched for screenshots"
        else:
            if channel.id not in channels:
                await ctx.reply(f"**#{channel.name}** is not being watched.")
                return
            channels.remove(channel.id)
            verb = "no longer watched"

        self.settings.set(ctx.guild_id, "ocr_channels", channels)
        await ctx.reply(
            embed=fluxer.Embed(
                description=f"**#{channel.name}** is {verb}.",
                color=_COLOR_OK,
            )
        )

    async def _toggle(self, ctx: fluxer.Message, enable: bool) -> None:
        self.settings.set(ctx.guild_id, "ocr_enabled", enable)
        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Screenshot OCR detection **{'enabled' if enable else 'disabled'}**.",
                color=_COLOR_OK if enable else _COLOR_WARN,
            )
        )

    async def _status(self, ctx: fluxer.Message) -> None:
        enabled  = self.settings.get(ctx.guild_id, "ocr_enabled", default=True)
        channels = self.settings.get(ctx.guild_id, "ocr_channels") or []
        triggers = self.settings.get(ctx.guild_id, "ocr_triggers") or []

        channels_str = ", ".join(f"<#{cid}>" for cid in channels) if channels else "None (inactive)"

        await ctx.reply(
            embed=fluxer.Embed(title="Screenshot OCR status", color=_COLOR_INFO)
            .add_field(name="Status",   value="✅ Enabled" if enabled else "❌ Disabled", inline=True)
            .add_field(name="Triggers", value=str(len(triggers)), inline=True)
            .add_field(name="Watched channels", value=channels_str, inline=False)
        )

    # =========================================================================
    # Listener
    # =========================================================================

    @fluxer.Cog.listener()
    async def on_message(self, message: fluxer.Message) -> None:
        if message.author.bot:
            return
        if message.guild_id is None:
            return
        if message.content and message.content.startswith(config.COMMAND_PREFIX):
            return

        if not self.settings.get(message.guild_id, "ocr_enabled", default=True):
            return

        channels: list = self.settings.get(message.guild_id, "ocr_channels") or []
        if message.channel_id not in channels:
            return

        triggers: list = self.settings.get(message.guild_id, "ocr_triggers") or []
        if not triggers:
            return

        attachments = getattr(message, "attachments", []) or []
        images = [a for a in attachments if _is_image_attachment(a)]
        if not images:
            return

        for attachment in images[:_MAX_IMAGES_PER_MESSAGE]:
            size = getattr(attachment, "size", 0) or 0
            if size > _MAX_IMAGE_BYTES:
                continue

            url: str = getattr(attachment, "url", "") or ""
            if not url:
                continue

            try:
                image_bytes = await self._download(url)
            except Exception as exc:
                log.warning("Failed to download image attachment: %s", exc)
                continue

            text = await self._run_ocr(image_bytes)
            if not text:
                continue

            ocr_norm = _normalise(text)
            log.debug("OCR extracted %d chars from image in channel %d",
                      len(text), message.channel_id)

            for trig in triggers:
                if _fuzzy_contains(ocr_norm, _normalise(trig["trigger"])):
                    log.info(
                        "OCR trigger matched (%r) for %s in guild %d",
                        trig["trigger"][:60], message.author.username, message.guild_id,
                    )
                    await self._send_reply(message, trig["reply"])
                    return  # one reply per message is enough

    async def _send_reply(self, message: fluxer.Message, reply_text: str) -> None:
        embed = fluxer.Embed(description=reply_text, color=_COLOR_WARN)
        try:
            await message.reply(embed=embed)
            return
        except Exception as exc:
            log.warning("OCR reply failed, falling back to channel send: %s", exc)

        channel = self.bot._channels.get(message.channel_id)
        if channel:
            try:
                await channel.send(content=message.author.mention, embed=embed)
            except Exception as exc:
                log.error("Failed to send OCR trigger response: %s", exc)

    # =========================================================================
    # OCR helpers
    # =========================================================================

    @staticmethod
    async def _download(url: str) -> bytes:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                return await resp.read()

    async def _run_ocr(self, image_bytes: bytes) -> Optional[str]:
        """Run tesseract on raw image bytes and return the extracted text."""
        async with self._ocr_sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tesseract", "stdin", "stdout",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                log.error(
                    "tesseract binary not found — install it "
                    "(e.g. `apt install tesseract-ocr` or `pacman -S tesseract`)"
                )
                return None

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(image_bytes), timeout=_OCR_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                log.warning("tesseract timed out after %ds", _OCR_TIMEOUT_SECONDS)
                return None

            if proc.returncode != 0:
                log.warning(
                    "tesseract exited with code %d: %s",
                    proc.returncode,
                    stderr.decode(errors="replace")[:200],
                )
                return None

            return stdout.decode("utf-8", errors="replace")


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(OcrTriggersCog(bot))
