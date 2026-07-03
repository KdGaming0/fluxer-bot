"""
cogs/crash_detect.py
--------------------
Automatically recognises known crashes in a set of watched channels and
replies with the configured solution — no staff ping needed.

Log sources checked on every message in a watched channel:
    • mclo.gs links      — fetched through the mclo.gs raw API
    • file attachments   — .txt / .log / .crash files
    • inline pastes      — long messages pasted directly into chat

Each known crash is a named *signature*: one or more lines of text that
appear in the log (e.g. a distinctive stack-trace line). A signature
matches when **every** one of its lines is found in the log, so you can
pin a crash down with a few characteristic lines. Whitespace differences
and letter case are ignored.

Commands (require Manage Guild):
    !crash add <name> | <signature> | <solution>  - Add a crash signature
    !crash remove <name>                          - Remove a signature
    !crash list                                   - List configured signatures
    !crash edit <name> | <new solution>           - Change a signature's solution
    !crash show <name>                            - Show full signature + solution
    !crash channel add #channel                   - Watch a channel for logs
    !crash channel remove #channel                - Stop watching a channel
    !crash channel list                           - List watched channels

On Discord, ``channel add`` also accepts forum channels (watches every
post in the forum) and categories (watches every channel under the
category — handy for ticket-bot channels created on the fly). Categories
have no #mention, so reference them by exact name or ID.
    !crash on / off                               - Toggle the whole feature
    !crash status                                 - Show current configuration

The signature may span multiple lines — just paste the lines between the
two `|` separators. Example:

    !crash add sodium-mixin | Mixin apply for mod sodium failed
    sodium.mixins.json:features.render | Your Sodium version is
    incompatible — update Sodium to 0.5.8+ from Modrinth.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import aiohttp
import fluxer
import config
from utils.storage import GuildSettings
from utils.watch import is_watchable_channel, message_in_watched

log = logging.getLogger("cog.crash_detect")

_COLOR_INFO = 0x5865F2
_COLOR_OK   = 0x57F287
_COLOR_WARN = 0xFEE75C

# ── Tunables ──────────────────────────────────────────────────────────────────

# Cap for downloaded logs (bytes) — matches the mclo.gs limit region.
_MAX_LOG_BYTES = 10 * 1024 * 1024

# Only check the first N mclo.gs links / attachments per message.
_MAX_SOURCES_PER_MESSAGE = 3

# Inline message content shorter than this is not scanned as a log paste.
_MIN_INLINE_CHARS = 200

# Maximum number of matched signatures to include in one reply.
_MAX_MATCHES_SHOWN = 3

# File extensions treated as log attachments.
_LOG_EXTS = frozenset({".txt", ".log", ".crash"})

# Matches mclo.gs share links and raw/API links, capturing the paste ID.
_MCLOGS_LINK = re.compile(r"https?://(?:api\.)?mclo\.gs/(?:1/raw/)?([A-Za-z0-9]+)")

_MCLOGS_RAW = "https://api.mclo.gs/1/raw/{}"


# ── Matching helpers ──────────────────────────────────────────────────────────

def _normalise_log(text: str) -> str:
    """Lowercase and collapse whitespace, one log line per output line."""
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"\s+", " ", ln).strip().lower()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def _signature_lines(signature: str) -> list[str]:
    """Split a stored signature into its normalised, non-empty lines."""
    lines = []
    for ln in signature.splitlines():
        ln = re.sub(r"\s+", " ", ln).strip().lower()
        if ln:
            lines.append(ln)
    return lines


def _signature_matches(log_norm: str, signature: str) -> bool:
    """True when every line of *signature* occurs somewhere in the log."""
    lines = _signature_lines(signature)
    if not lines:
        return False
    return all(ln in log_norm for ln in lines)


class CrashDetectCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()

    # =========================================================================
    # Admin commands
    # =========================================================================

    @fluxer.Cog.command(name="crash")
    @fluxer.has_permission(fluxer.Permissions.MANAGE_GUILD)
    async def crash(self, ctx: fluxer.Message) -> None:
        """Manage known crash detection (requires Manage Guild).

        Watches the configured channels for logs (mclo.gs links, .log/.txt
        attachments, or inline pastes) and replies with the stored solution
        when a known crash signature matches.

        Usage:
            !crash add <name> | <signature> | <solution>   Add a signature
            !crash remove <name>                           Remove a signature
            !crash list                                    List signatures
            !crash edit <name> | <new solution>            Edit a solution
            !crash show <name>                             Show signature + solution
            !crash channel add/remove #channel             Manage watched channels
            !crash channel list                            List watched channels
            !crash on / off                                Toggle the feature
            !crash status                                  Show current configuration

        The signature may span multiple lines; the crash matches when ALL
        signature lines are found in the log. Run !crash with no arguments
        for the same overview in Discord.
        """
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        args  = ctx.content.removeprefix(f"{config.COMMAND_PREFIX}crash").strip()
        parts = args.split(None, 1)
        sub   = parts[0].lower() if parts else ""
        rest  = parts[1].strip() if len(parts) > 1 else ""

        if sub == "add":
            await self._sig_add(ctx, rest)
        elif sub == "remove":
            await self._sig_remove(ctx, rest)
        elif sub == "list":
            await self._sig_list(ctx)
        elif sub == "edit":
            await self._sig_edit(ctx, rest)
        elif sub == "show":
            await self._sig_show(ctx, rest)
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
                    title="Known crash commands",
                    description=(
                        f"`{p}crash add <name> | <signature> | <solution>` — Add a signature\n"
                        f"`{p}crash remove <name>` — Remove a signature\n"
                        f"`{p}crash list` — List signatures\n"
                        f"`{p}crash edit <name> | <new solution>` — Edit a solution\n"
                        f"`{p}crash show <name>` — Show full signature + solution\n"
                        f"`{p}crash channel add/remove #channel` — Manage watched channels\n"
                        f"`{p}crash channel list` — List watched channels\n"
                        f"`{p}crash on/off` — Toggle the feature\n"
                        f"`{p}crash status` — Show current configuration\n\n"
                        "Signatures may span multiple lines; the crash matches when "
                        "**all** signature lines are found in an uploaded log "
                        "(mclo.gs link, .log/.txt attachment, or inline paste)."
                    ),
                    color=_COLOR_INFO,
                )
            )

    # ── Signature management ──────────────────────────────────────────────────

    def _get_signatures(self, guild_id: int) -> list:
        return self.settings.get(guild_id, "crash_signatures") or []

    @staticmethod
    def _find_signature(signatures: list, name: str) -> Optional[int]:
        name_l = name.strip().lower()
        for i, sig in enumerate(signatures):
            if sig["name"].lower() == name_l:
                return i
        return None

    async def _sig_add(self, ctx: fluxer.Message, rest: str) -> None:
        p = config.COMMAND_PREFIX
        # name | signature (may contain '|' and newlines) | solution
        if rest.count("|") < 2:
            await ctx.reply(
                f"Usage: `{p}crash add <name> | <signature lines> | <solution>`\n"
                f"Example: `{p}crash add oom | java.lang.OutOfMemoryError | "
                f"Allocate more RAM to the game — 6 GB is recommended for this pack.`"
            )
            return

        name, remainder = (s.strip() for s in rest.split("|", 1))
        signature, solution = (s.strip() for s in remainder.rsplit("|", 1))

        if not name or not signature or not solution:
            await ctx.reply("Name, signature, and solution must all be non-empty.")
            return

        signatures = self._get_signatures(ctx.guild_id)
        if self._find_signature(signatures, name) is not None:
            await ctx.reply(
                f"A signature named **{name}** already exists. "
                f"Use `{p}crash edit {name} | <new solution>` or remove it first."
            )
            return

        signatures.append({"name": name, "signature": signature, "solution": solution})
        self.settings.set(ctx.guild_id, "crash_signatures", signatures)
        log.info("Crash signature %r added in guild %d by %s",
                 name, ctx.guild_id, ctx.author.username)

        sig_preview = signature[:500] + ("…" if len(signature) > 500 else "")
        await ctx.reply(
            embed=fluxer.Embed(title=f"Crash signature added: {name}", color=_COLOR_OK)
            .add_field(name="Signature", value=f"```{sig_preview}```", inline=False)
            .add_field(name="Solution",  value=solution[:1000],        inline=False)
        )

    async def _sig_remove(self, ctx: fluxer.Message, rest: str) -> None:
        if not rest:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}crash remove <name>`")
            return

        signatures = self._get_signatures(ctx.guild_id)
        idx = self._find_signature(signatures, rest)
        if idx is None:
            await ctx.reply(f"No crash signature named **{rest}** found.")
            return

        removed = signatures.pop(idx)
        self.settings.set(ctx.guild_id, "crash_signatures", signatures)

        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Removed crash signature **{removed['name']}**.",
                color=_COLOR_OK,
            )
        )

    async def _sig_list(self, ctx: fluxer.Message) -> None:
        signatures = self._get_signatures(ctx.guild_id)
        if not signatures:
            await ctx.reply(
                f"No crash signatures configured yet. Add one with "
                f"`{config.COMMAND_PREFIX}crash add <name> | <signature> | <solution>`."
            )
            return

        lines = []
        for sig in signatures:
            first_line = sig["signature"].splitlines()[0][:60]
            n_lines = len(_signature_lines(sig["signature"]))
            suffix = f" (+{n_lines - 1} more line{'s' if n_lines > 2 else ''})" if n_lines > 1 else ""
            lines.append(f"**{sig['name']}** — `{first_line}`{suffix}")

        await ctx.reply(
            embed=fluxer.Embed(
                title=f"Known crash signatures — {len(signatures)} total",
                description="\n".join(lines)[:4000],
                color=_COLOR_INFO,
            )
        )

    async def _sig_edit(self, ctx: fluxer.Message, rest: str) -> None:
        p = config.COMMAND_PREFIX
        if "|" not in rest:
            await ctx.reply(f"Usage: `{p}crash edit <name> | <new solution>`")
            return

        name, new_solution = (s.strip() for s in rest.split("|", 1))
        if not name or not new_solution:
            await ctx.reply(f"Usage: `{p}crash edit <name> | <new solution>`")
            return

        signatures = self._get_signatures(ctx.guild_id)
        idx = self._find_signature(signatures, name)
        if idx is None:
            await ctx.reply(f"No crash signature named **{name}** found.")
            return

        signatures[idx]["solution"] = new_solution
        self.settings.set(ctx.guild_id, "crash_signatures", signatures)

        await ctx.reply(
            embed=fluxer.Embed(
                title=f"Solution updated: {signatures[idx]['name']}",
                description=new_solution[:2000],
                color=_COLOR_OK,
            )
        )

    async def _sig_show(self, ctx: fluxer.Message, rest: str) -> None:
        if not rest:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}crash show <name>`")
            return

        signatures = self._get_signatures(ctx.guild_id)
        idx = self._find_signature(signatures, rest)
        if idx is None:
            await ctx.reply(f"No crash signature named **{rest}** found.")
            return

        sig = signatures[idx]
        sig_preview = sig["signature"][:900] + ("…" if len(sig["signature"]) > 900 else "")
        await ctx.reply(
            embed=fluxer.Embed(title=f"Crash signature: {sig['name']}", color=_COLOR_INFO)
            .add_field(name="Signature", value=f"```{sig_preview}```",  inline=False)
            .add_field(name="Solution",  value=sig["solution"][:1000],  inline=False)
        )

    # ── Channel management ────────────────────────────────────────────────────

    def _resolve_channel(self, ctx: fluxer.Message, arg: str):
        match = re.search(r"<#(\d+)>", arg)
        if match:
            return self.bot._channels.get(int(match.group(1)))
        # Raw ID — the only way to reference a category, which has no <#…> mention.
        if arg.strip().isdigit():
            return self.bot._channels.get(int(arg.strip()))
        name = arg.lstrip("#").strip().lower()
        for ch in self.bot._channels.values():
            if ch.guild_id == ctx.guild_id and ch.name.lower() == name:
                return ch
        return None

    async def _channel_cmd(self, ctx: fluxer.Message, rest: str) -> None:
        parts  = rest.split(None, 1)
        action = parts[0].lower() if parts else ""
        arg    = parts[1].strip() if len(parts) > 1 else ""

        channels: list = self.settings.get(ctx.guild_id, "crash_channels") or []

        if action == "list":
            desc = "\n".join(f"<#{cid}>" for cid in channels) if channels else (
                "No channels watched yet — crash detection is inactive until you add one."
            )
            await ctx.reply(
                embed=fluxer.Embed(
                    title=f"Crash detection channels — {len(channels)}",
                    description=desc,
                    color=_COLOR_INFO,
                )
            )
            return

        if action not in ("add", "remove") or not arg:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}crash channel add/remove <#channel | name | ID>` "
                f"or `{config.COMMAND_PREFIX}crash channel list`\n"
                "Forum channels and categories work too (Discord): watching a forum "
                "covers every post in it, watching a category covers every channel "
                "under it (e.g. ticket channels). Reference a category by name or ID."
            )
            return

        channel = self._resolve_channel(ctx, arg)
        if channel is None or not is_watchable_channel(channel):
            await ctx.reply(
                "Could not find that channel. Use a #mention, exact name, or ID — "
                "text channels, forum channels, and categories are supported."
            )
            return

        if getattr(channel, "is_category", False):
            label = f"category **{channel.name}** (all channels under it)"
        elif getattr(channel, "is_forum_channel", False):
            label = f"forum **{channel.name}** (all posts in it)"
        else:
            label = f"**#{channel.name}**"

        if action == "add":
            if channel.id in channels:
                await ctx.reply(f"{label} is already being watched.")
                return
            channels.append(channel.id)
            verb = "now watched for known crashes"
        else:
            if channel.id not in channels:
                await ctx.reply(f"{label} is not being watched.")
                return
            channels.remove(channel.id)
            verb = "no longer watched"

        self.settings.set(ctx.guild_id, "crash_channels", channels)
        await ctx.reply(
            embed=fluxer.Embed(
                description=f"{label} is {verb}.",
                color=_COLOR_OK,
            )
        )

    async def _toggle(self, ctx: fluxer.Message, enable: bool) -> None:
        self.settings.set(ctx.guild_id, "crash_enabled", enable)
        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Known crash detection **{'enabled' if enable else 'disabled'}**.",
                color=_COLOR_OK if enable else _COLOR_WARN,
            )
        )

    async def _status(self, ctx: fluxer.Message) -> None:
        enabled    = self.settings.get(ctx.guild_id, "crash_enabled", default=True)
        channels   = self.settings.get(ctx.guild_id, "crash_channels") or []
        signatures = self._get_signatures(ctx.guild_id)

        channels_str = ", ".join(f"<#{cid}>" for cid in channels) if channels else "None (inactive)"

        await ctx.reply(
            embed=fluxer.Embed(title="Known crash detection status", color=_COLOR_INFO)
            .add_field(name="Status",     value="✅ Enabled" if enabled else "❌ Disabled", inline=True)
            .add_field(name="Signatures", value=str(len(signatures)), inline=True)
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

        if not self.settings.get(message.guild_id, "crash_enabled", default=True):
            return

        channels: list = self.settings.get(message.guild_id, "crash_channels") or []
        if not message_in_watched(message, channels):
            return

        signatures = self._get_signatures(message.guild_id)
        if not signatures:
            return

        sources = await self._collect_log_sources(message)
        if not sources:
            return

        matches: list[dict] = []
        matched_names: set[str] = set()
        for source in sources:
            log_norm = _normalise_log(source)
            for sig in signatures:
                if sig["name"] in matched_names:
                    continue
                if _signature_matches(log_norm, sig["signature"]):
                    matches.append(sig)
                    matched_names.add(sig["name"])

        if not matches:
            return

        log.info(
            "Known crash matched (%s) for %s in guild %d",
            ", ".join(sorted(matched_names)), message.author.username, message.guild_id,
        )
        await self._send_reply(message, matches[:_MAX_MATCHES_SHOWN])

    async def _collect_log_sources(self, message: fluxer.Message) -> list[str]:
        """Gather log text from mclo.gs links, log attachments, and inline pastes."""
        sources: list[str] = []

        # mclo.gs links (however they were pasted — share link or raw API link)
        seen_ids: set[str] = set()
        for paste_id in _MCLOGS_LINK.findall(message.content or ""):
            if paste_id in seen_ids or len(sources) >= _MAX_SOURCES_PER_MESSAGE:
                continue
            seen_ids.add(paste_id)
            raw = await self._fetch_mclogs(paste_id)
            if raw:
                sources.append(raw)

        # Log file attachments
        for attachment in (getattr(message, "attachments", []) or []):
            if len(sources) >= _MAX_SOURCES_PER_MESSAGE:
                break
            filename: str = getattr(attachment, "filename", "") or ""
            _, ext = os.path.splitext(filename.lower())
            if ext not in _LOG_EXTS:
                continue
            size = getattr(attachment, "size", 0) or 0
            if size > _MAX_LOG_BYTES:
                continue
            url: str = getattr(attachment, "url", "") or ""
            if not url:
                continue
            try:
                sources.append(await self._download_text(url))
            except Exception as exc:
                log.warning("Failed to download log attachment %r: %s", filename, exc)

        # Inline paste — only worth scanning if it is reasonably long
        if message.content and len(message.content) >= _MIN_INLINE_CHARS:
            sources.append(message.content)

        return sources

    async def _send_reply(self, message: fluxer.Message, matches: list[dict]) -> None:
        if len(matches) == 1:
            embed = fluxer.Embed(
                title=f"🔧 Known issue detected: {matches[0]['name']}",
                description=matches[0]["solution"][:4000],
                color=_COLOR_INFO,
            )
        else:
            embed = fluxer.Embed(
                title="🔧 Known issues detected",
                description="Your log matches more than one known problem:",
                color=_COLOR_INFO,
            )
            for sig in matches:
                embed.add_field(name=sig["name"], value=sig["solution"][:1000], inline=False)

        embed.set_footer(text="Automatic crash detection — if this doesn't solve it, ask for help below.")

        try:
            await message.reply(embed=embed)
            return
        except Exception as exc:
            log.warning("Crash reply failed, falling back to channel send: %s", exc)

        channel = self.bot._channels.get(message.channel_id)
        if channel:
            try:
                await channel.send(content=message.author.mention, embed=embed)
            except Exception as exc:
                log.error("Failed to send crash detection response: %s", exc)

    # =========================================================================
    # Download helpers
    # =========================================================================

    @staticmethod
    async def _fetch_mclogs(paste_id: str) -> Optional[str]:
        """Fetch the raw content of an mclo.gs paste, or None on failure."""
        url = _MCLOGS_RAW.format(paste_id)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        log.debug("mclo.gs raw fetch for %r returned %d", paste_id, resp.status)
                        return None
                    raw_bytes = await resp.read()
        except Exception as exc:
            log.warning("mclo.gs raw fetch failed for %r: %s", paste_id, exc)
            return None
        return raw_bytes[:_MAX_LOG_BYTES].decode("utf-8", errors="replace")

    @staticmethod
    async def _download_text(url: str) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                raw_bytes = await resp.read()
        return raw_bytes[:_MAX_LOG_BYTES].decode("utf-8", errors="replace")


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(CrashDetectCog(bot))
