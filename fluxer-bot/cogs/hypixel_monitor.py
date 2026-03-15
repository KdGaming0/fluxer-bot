"""
cogs/hypixel_monitor.py
-----------------------
Scrapes Hypixel forum categories for mod / tech-support threads and posts
Fluxer embeds to a configured channel.

Extra dependencies (pip install):
    aiohttp>=3.9.0   (already required by fluxer.py)
    beautifulsoup4

Detection tiers
  higher   → immediate notify regardless of threshold (VIP keywords, exact names)
  normal   → +6.0 title / +3.0 body per phrase  (+3.0/+1.5 per single word)
  lower    → +3.0 title / +1.5 body per phrase  (+1.5/+0.75 per single word)
  negative → −4.0 title / −2.0 body  (game-economy terms)

Context boost (+0.5 each, capped at +2.0): help-seeking language, tech terms, question marks.

Commands (require Manage Guild, except !hmonitor cleartasks which needs OWNER_ID):
    !hmonitor quicksetup #channel
    !hmonitor setchannel #channel
    !hmonitor enable / disable / restart
    !hmonitor setinterval <seconds>
    !hmonitor setthreshold <float>
    !hmonitor loaddefaults [merge]
    !hmonitor processedcount / clearprocessed / setmaxprocessed <n>
    !hmonitor status / taskinfo / debugmode <true|false>
    !hmonitor checknow
    !hmonitor testdetect <title>\\n[body]
    !hmonitor tune [limit] [category name]
    !hmonitor cleartasks          (OWNER_ID only)
    !hmonitor category add <url> <name>
    !hmonitor category remove <name>
    !hmonitor category list
    !hmonitor keyword add <tier> <keyword>
    !hmonitor keyword bulkadd <tier> <kw1, kw2, ...>
    !hmonitor keyword remove <tier> <keyword>
    !hmonitor keyword list [tier]
    !hmonitor keyword find <search>
    !hmonitor keyword export
    !hmonitor keyword import [merge] <json>
"""

import asyncio
import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

import fluxer
import config
from utils.storage import GuildSettings
from utils.detection import (
    DEFAULT_KEYWORDS,
    FALSE_POSITIVE_PATTERNS,
    CONTEXT_PATTERNS,
    ScoreResult,
    score_text,
    should_notify,
)

log = logging.getLogger("cog.hypixel_monitor")

# ── Limits ────────────────────────────────────────────────────────────────────
MIN_INTERVAL        = 60
DEFAULT_INTERVAL    = 900
DEFAULT_THRESHOLD   = 3.0
DEFAULT_MAX_PROC    = 1000

# ── Embed colours (hex ints replacing discord.Color) ─────────────────────────
_C_RED    = 0xED4245
_C_ORANGE = 0xE67E22
_C_GOLD   = 0xF1C40F
_C_GREEN  = 0x57F287
_C_BLUE   = 0x5865F2

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

DEFAULT_FORUM_CATEGORIES = [
    {"url": "https://hypixel.net/forums/skyblock.157/",                "name": "SkyBlock General"},
    {"url": "https://hypixel.net/forums/skyblock-community-help.196/", "name": "SkyBlock Community Help"},
]


# ─────────────────────────────────────────────────────────────────────────────
class HypixelMonitorCog(fluxer.Cog):
    """Monitor Hypixel Forums for mod-related questions and technical help."""

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()
        self._tasks:      Dict[int, asyncio.Task]          = {}
        self._sessions:   Dict[int, aiohttp.ClientSession] = {}
        self._task_locks: Dict[int, asyncio.Lock]          = {}
        self._proc_locks: Dict[int, asyncio.Lock]          = {}

    # ── Storage helpers ───────────────────────────────────────────────────────

    def _get(self, guild_id: int, key: str, default=None):
        return self.settings.get(guild_id, f"hm_{key}", default)

    def _set(self, guild_id: int, key: str, value) -> None:
        self.settings.set(guild_id, f"hm_{key}", value)

    def _del(self, guild_id: int, key: str) -> None:
        self.settings.delete(guild_id, f"hm_{key}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @fluxer.Cog.listener()
    async def on_guild_join(self, guild: fluxer.Guild) -> None:
        if self._get(guild.id, "enabled", False):
            await self._ensure_task(guild)
            log.info("HypixelMonitor resuming task for guild %d (%s)", guild.id, guild)

    async def cog_unload(self) -> None:
        log.info("Shutting down HypixelMonitor…")
        tasks = list(self._tasks.values())
        for t in tasks:
            if not t.cancelled():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for s in self._sessions.values():
            await s.close()
        self._sessions.clear()
        self._task_locks.clear()
        self._proc_locks.clear()

    # ── HTTP session ──────────────────────────────────────────────────────────

    async def _get_session(self, guild_id: int) -> aiohttp.ClientSession:
        s = self._sessions.get(guild_id)
        if s and not s.closed:
            return s
        s = aiohttp.ClientSession(
            headers={"User-Agent": _UA},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._sessions[guild_id] = s
        return s

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _reply(self, ctx: fluxer.Message, text: str = "", embed: fluxer.Embed = None) -> None:
        if embed is not None:
            await ctx.reply(content=text if text else " ", embed=embed)
        else:
            await ctx.reply(content=text or None)

    def _resolve_channel(self, guild_id: int, arg: str):
        m = re.search(r"<#(\d+)>", arg)
        if m:
            return self.bot._channels.get(int(m.group(1)))
        name = arg.lstrip("#").strip()
        for ch in self.bot._channels.values():
            if ch.guild_id == guild_id and ch.name == name:
                return ch
        return None

    def _pagify(self, text: str, max_len: int = 1900) -> List[str]:
        pages, current = [], ""
        for line in text.split("\n"):
            candidate = (current + "\n" + line) if current else line
            if len(candidate) > max_len:
                pages.append(current)
                current = line
            else:
                current = candidate
        if current:
            pages.append(current)
        return pages or [""]

    async def _send_pages(self, ctx: fluxer.Message, text: str) -> None:
        pages = self._pagify(text)
        await ctx.reply(content=pages[0])
        ch = self.bot._channels.get(ctx.channel_id)
        for page in pages[1:]:
            if ch:
                await ch.send(content=page)

    # ── Detection ─────────────────────────────────────────────────────────────

    @staticmethod
    def _score_text(title: str, body: str, keywords: Dict[str, List[str]]) -> ScoreResult:
        return score_text(title, body, keywords)

    def _should_notify(self, thread_data: dict, detect: ScoreResult, guild_id: int) -> bool:
        threshold = self._get(guild_id, "threshold") or DEFAULT_THRESHOLD
        return should_notify(
            thread_data.get("title", ""),
            thread_data.get("content", ""),
            detect,
            threshold,
        )

    # ── Notification ──────────────────────────────────────────────────────────

    async def _notify(self, guild_id: int, thread: dict, detect: ScoreResult) -> None:
        ch_id = self._get(guild_id, "notify_channel_id")
        if not ch_id:
            return
        channel = self.bot._channels.get(ch_id)
        if not channel:
            return

        score = detect.score
        if detect.immediate:
            confidence, color = "🔴 HIGH (Immediate)", _C_RED
        elif score >= 6.0:
            confidence, color = "🟠 HIGH",   _C_ORANGE
        elif score >= 3.0:
            confidence, color = "🟡 MEDIUM", _C_GOLD
        else:
            confidence, color = "🟢 LOW",    _C_GREEN

        content = thread.get("content", "") or ""
        embed = (
            fluxer.Embed(
                title=(thread.get("title", "Unknown"))[:256],
                description=((content[:500] + "…") if len(content) > 500 else content or "No preview"),
                color=color,
            )
            .add_field(name="Confidence", value=confidence,                    inline=True)
            .add_field(name="Score",      value=f"{score:.1f}",                inline=True)
            .add_field(name="Category",   value=thread.get("category", "?"),   inline=True)
        )

        for tier in ("higher", "normal"):
            vals = detect.matches.get(tier, [])
            if vals:
                embed.add_field(
                    name=f"{tier.title()} Keywords",
                    value=", ".join(vals[:6]) + ("…" if len(vals) > 6 else ""),
                    inline=False,
                )
        if detect.matches.get("negative"):
            embed.add_field(
                name="⚠️ Negative Indicators",
                value=", ".join(detect.matches["negative"][:4]),
                inline=False,
            )

        embed.set_footer(text=f"by {thread.get('author', '?')} • Hypixel Forums")

        if thread.get("url"):
            embed.url = thread["url"]

        try:
            await channel.send(embed=embed)
        except Exception as exc:
            log.exception("Failed to send notification: %s", exc)

    # ── Processed-ID helpers ──────────────────────────────────────────────────

    def _proc_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._proc_locks:
            self._proc_locks[guild_id] = asyncio.Lock()
        return self._proc_locks[guild_id]

    async def _add_processed(self, guild_id: int, thread_id: str) -> None:
        async with self._proc_lock(guild_id):
            processed = self._get(guild_id, "processed_ids") or []
            maxp = self._get(guild_id, "max_processed") or DEFAULT_MAX_PROC
            if thread_id not in processed:
                processed.append(thread_id)
            if len(processed) > maxp:
                processed = processed[-maxp:]
            self._set(guild_id, "processed_ids", processed)

    async def _is_processed(self, guild_id: int, thread_id: str) -> bool:
        processed = self._get(guild_id, "processed_ids") or []
        return thread_id in processed

    # ── Debug helper ──────────────────────────────────────────────────────────

    async def _debug(self, guild_id: int, msg: str) -> None:
        if not self._get(guild_id, "debug", False):
            return
        ch_id = self._get(guild_id, "notify_channel_id")
        if ch_id and (ch := self.bot._channels.get(ch_id)):
            try:
                await ch.send(content=msg)
            except Exception:
                pass

    # ── Forum scraping ────────────────────────────────────────────────────────

    async def _get_thread_content(self, session: aiohttp.ClientSession, url: str) -> str:
        try:
            async with session.get(url) as r:
                if r.status == 200:
                    soup = BeautifulSoup(await r.text(), "html.parser")
                    el = (
                        soup.select_one(".message-body .message-userContent")
                        or soup.select_one(".message--post .message-body")
                    )
                    if el:
                        return re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        except Exception as exc:
            log.warning("Content fetch failed %s: %s", url, exc)
        return ""

    async def _get_recent_threads(
        self, session: aiohttp.ClientSession, category: dict
    ) -> List[dict]:
        threads = []
        try:
            async with session.get(category["url"]) as r:
                if r.status != 200:
                    return threads
                soup = BeautifulSoup(await r.text(), "html.parser")
                for item in soup.select(".structItem--thread"):
                    try:
                        cls = " ".join(item.get("class", []))
                        m   = re.search(r"js-threadListItem-(\d+)", cls)
                        if not m:
                            continue
                        tid      = m.group(1)
                        title_el = item.select_one(".structItem-title")
                        if not title_el:
                            continue
                        title = title_el.get_text(strip=True)
                        a     = title_el.select_one("a")
                        if not a:
                            continue
                        url    = urljoin("https://hypixel.net", a["href"])
                        auth_el = (
                            item.select_one(".structItem-minor .username")
                            or item.select_one(".username")
                        )
                        author = auth_el.get_text(strip=True) if auth_el else "Unknown"
                        threads.append({
                            "id": tid, "title": title, "url": url,
                            "author": author, "category": category["name"],
                            "content": "",
                        })
                    except Exception as exc:
                        log.warning("Thread parse error: %s", exc)
        except Exception as exc:
            log.error("Category fetch error (%s): %s", category["name"], exc)
        return threads

    # ── Monitoring loop ───────────────────────────────────────────────────────

    async def _monitor_guild(self, guild_id: int) -> None:
        log.info("HypixelMonitor started: guild %d", guild_id)
        try:
            while True:
                try:
                    if not self._get(guild_id, "enabled", False):
                        log.info("Monitoring disabled, stopping: guild %d", guild_id)
                        break

                    cats = self._get(guild_id, "forum_categories") or DEFAULT_FORUM_CATEGORIES
                    if not cats:
                        await self._debug(guild_id, "⚠️ Monitor alive — no forum categories configured.")
                    else:
                        await self._check_categories(guild_id, cats)

                    interval = max(
                        self._get(guild_id, "interval") or DEFAULT_INTERVAL,
                        MIN_INTERVAL,
                    )
                    await asyncio.sleep(interval)

                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("Loop error: guild %d", guild_id)
                    await self._debug(guild_id, "❌ Monitor error — retrying in 60 s…")
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Fatal error: guild %d", guild_id)
        finally:
            await self._cleanup(guild_id)

    async def _check_categories(self, guild_id: int, cats: List[dict]) -> None:
        keywords = self._get(guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        session  = await self._get_session(guild_id)
        notified = 0
        checked  = 0

        for cat in cats:
            try:
                threads = await self._get_recent_threads(session, cat)
                for thread in threads:
                    checked += 1
                    if await self._is_processed(guild_id, thread["id"]):
                        continue
                    if not thread["content"]:
                        thread["content"] = await self._get_thread_content(session, thread["url"])
                    detect = self._score_text(thread["title"], thread["content"], keywords)
                    if self._should_notify(thread, detect, guild_id):
                        await self._notify(guild_id, thread, detect)
                        notified += 1
                        log.info("Notified: %s in %s (guild %d)", thread["id"], cat["name"], guild_id)
                    await self._add_processed(guild_id, thread["id"])
            except Exception:
                log.exception("Category error (%s): guild %d", cat["name"], guild_id)

        if notified == 0:
            await self._debug(
                guild_id,
                f"✅ Monitor alive — checked {checked} threads across {len(cats)} category/ies. "
                "No matches this cycle.",
            )

    # ── Task management ───────────────────────────────────────────────────────

    async def _cleanup(self, guild_id: int) -> None:
        self._tasks.pop(guild_id, None)
        s = self._sessions.pop(guild_id, None)
        if s:
            try:
                await s.close()
            except Exception:
                pass
        self._task_locks.pop(guild_id, None)
        self._proc_locks.pop(guild_id, None)

    def _get_task_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._task_locks:
            self._task_locks[guild_id] = asyncio.Lock()
        return self._task_locks[guild_id]

    async def _ensure_task(self, guild) -> None:
        guild_id = guild.id
        async with self._get_task_lock(guild_id):
            t = self._tasks.get(guild_id)
            if t and not t.done():
                return
            if t:
                await self._cleanup(guild_id)
            if not self._get(guild_id, "enabled", False):
                return
            self._tasks[guild_id] = asyncio.create_task(self._monitor_guild(guild_id))

    async def _stop_task(self, guild_id: int) -> None:
        async with self._get_task_lock(guild_id):
            t = self._tasks.get(guild_id)
            if t and not t.cancelled():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            await self._cleanup(guild_id)

    # =========================================================================
    # Command dispatcher
    # =========================================================================

    @fluxer.Cog.command(name="hmonitor")
    async def hmonitor(self, ctx: fluxer.Message, *, args: str = "") -> None:
        """Dispatcher for all !hmonitor sub-commands."""
        if ctx.guild_id is None:
            await ctx.reply(content="This command can only be used inside a server.")
            return

        parts = args.strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""
        rest  = parts[1] if len(parts) > 1 else ""

        if not sub or sub == "help":
            await self._reply(ctx, embed=self._help_embed())
            return

        if sub == "cleartasks":
            if str(ctx.author.id) != str(getattr(config, "OWNER_ID", "")):
                await ctx.reply(content="This command is bot-owner only.")
                return
            await self._cmd_cleartasks(ctx)
            return

        await self._gated(ctx, sub, rest)

    @fluxer.checks.has_permission(fluxer.Permissions.MANAGE_GUILD)
    async def _gated(self, ctx: fluxer.Message, sub: str, rest: str) -> None:
        dispatch = {
            "quicksetup":      self._cmd_quicksetup,
            "setchannel":      self._cmd_setchannel,
            "enable":          self._cmd_enable,
            "disable":         self._cmd_disable,
            "restart":         self._cmd_restart,
            "setinterval":     self._cmd_setinterval,
            "setthreshold":    self._cmd_setthreshold,
            "loaddefaults":    self._cmd_loaddefaults,
            "processedcount":  self._cmd_processedcount,
            "clearprocessed":  self._cmd_clearprocessed,
            "setmaxprocessed": self._cmd_setmaxprocessed,
            "status":          self._cmd_status,
            "taskinfo":        self._cmd_taskinfo,
            "debugmode":       self._cmd_debugmode,
            "checknow":        self._cmd_checknow,
            "testdetect":      self._cmd_testdetect,
            "tune":            self._cmd_tune,
            "category":        self._dispatch_category,
            "keyword":         self._dispatch_keyword,
        }
        handler = dispatch.get(sub)
        if handler is None:
            await self._reply(ctx, embed=self._help_embed())
            return
        await handler(ctx, rest)

    # =========================================================================
    # Sub-command handlers
    # =========================================================================

    async def _cmd_quicksetup(self, ctx: fluxer.Message, args: str) -> None:
        if not args.strip():
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor quicksetup #channel`")
            return
        channel = self._resolve_channel(ctx.guild_id, args.strip())
        if channel is None:
            await ctx.reply(content="Could not find that channel.")
            return
        self._set(ctx.guild_id, "notify_channel_id", channel.id)
        self._set(ctx.guild_id, "keywords", deepcopy(DEFAULT_KEYWORDS))
        self._set(ctx.guild_id, "forum_categories", deepcopy(DEFAULT_FORUM_CATEGORIES))
        await self._reply(ctx, embed=fluxer.Embed(
            title="✅ Quick setup complete!",
            description=(
                f"📢 Channel: <#{channel.id}>\n"
                f"🔑 Default keywords loaded\n"
                f"📂 Default forum categories loaded\n\n"
                f"▶️ Run `{config.COMMAND_PREFIX}hmonitor enable` to start."
            ),
            color=_C_GREEN,
        ))

    async def _cmd_setchannel(self, ctx: fluxer.Message, args: str) -> None:
        channel = self._resolve_channel(ctx.guild_id, args.strip())
        if channel is None:
            await ctx.reply(content="Could not find that channel.")
            return
        self._set(ctx.guild_id, "notify_channel_id", channel.id)
        await ctx.reply(content=f"Notification channel set to <#{channel.id}>.")

    async def _cmd_enable(self, ctx: fluxer.Message, args: str) -> None:
        if self._get(ctx.guild_id, "enabled", False):
            await ctx.reply(content="Already enabled. Use `disable` to stop.")
            return
        self._set(ctx.guild_id, "enabled", True)
        guild = self.bot._guilds.get(ctx.guild_id)
        if guild:
            await self._ensure_task(guild)
        await ctx.reply(content="✅ Monitoring enabled.")

    async def _cmd_disable(self, ctx: fluxer.Message, args: str) -> None:
        self._set(ctx.guild_id, "enabled", False)
        await self._stop_task(ctx.guild_id)
        await ctx.reply(content="⏹ Monitoring disabled.")

    async def _cmd_restart(self, ctx: fluxer.Message, args: str) -> None:
        await self._stop_task(ctx.guild_id)
        await asyncio.sleep(1)
        self._set(ctx.guild_id, "enabled", True)
        guild = self.bot._guilds.get(ctx.guild_id)
        if guild:
            await self._ensure_task(guild)
        await ctx.reply(content="♻️ Monitoring task restarted.")

    async def _cmd_setinterval(self, ctx: fluxer.Message, args: str) -> None:
        if not args.strip().isdigit():
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor setinterval <seconds>`")
            return
        seconds = int(args.strip())
        if seconds < MIN_INTERVAL:
            await ctx.reply(content=f"Minimum interval is {MIN_INTERVAL}s.")
            return
        self._set(ctx.guild_id, "interval", seconds)
        await ctx.reply(content=f"Interval set to {seconds}s.")

    async def _cmd_setthreshold(self, ctx: fluxer.Message, args: str) -> None:
        try:
            threshold = float(args.strip())
        except ValueError:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor setthreshold <1.0-10.0>`")
            return
        if not 1.0 <= threshold <= 10.0:
            await ctx.reply(content="Threshold must be between 1.0 and 10.0.")
            return
        self._set(ctx.guild_id, "threshold", threshold)
        await ctx.reply(content=f"Threshold set to {threshold}.")

    async def _cmd_loaddefaults(self, ctx: fluxer.Message, args: str) -> None:
        merge = args.strip().lower() in ("true", "merge", "1", "yes")
        if merge:
            kw = self._get(ctx.guild_id, "keywords") or {"higher": [], "normal": [], "lower": [], "negative": []}
            for tier, defaults in DEFAULT_KEYWORDS.items():
                kw[tier] = list(set(kw.get(tier, [])) | set(defaults))
            self._set(ctx.guild_id, "keywords", kw)
            await ctx.reply(content="Default keywords merged.")
        else:
            self._set(ctx.guild_id, "keywords", deepcopy(DEFAULT_KEYWORDS))
            await ctx.reply(content="Default keywords loaded (previous keywords replaced).")

        kw = self._get(ctx.guild_id, "keywords") or {}
        counts = ", ".join(f"{t}: {len(kw.get(t, []))}" for t in ("higher", "normal", "lower", "negative"))
        await ctx.reply(content=f"Keyword counts — {counts}")

    async def _cmd_processedcount(self, ctx: fluxer.Message, args: str) -> None:
        ids = self._get(ctx.guild_id, "processed_ids") or []
        await ctx.reply(content=f"Stored processed IDs: {len(ids)}")

    async def _cmd_clearprocessed(self, ctx: fluxer.Message, args: str) -> None:
        self._set(ctx.guild_id, "processed_ids", [])
        await ctx.reply(content="✅ Processed IDs cleared.")

    async def _cmd_setmaxprocessed(self, ctx: fluxer.Message, args: str) -> None:
        if not args.strip().isdigit():
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor setmaxprocessed <n>`")
            return
        n = int(args.strip())
        if n < 10:
            await ctx.reply(content="Must be at least 10.")
            return
        self._set(ctx.guild_id, "max_processed", n)
        await ctx.reply(content=f"Max processed IDs set to {n}.")

    async def _cmd_status(self, ctx: fluxer.Message, args: str) -> None:
        gid   = ctx.guild_id
        en    = self._get(gid, "enabled", False)
        cats  = self._get(gid, "forum_categories") or []
        ch_id = self._get(gid, "notify_channel_id")
        iv    = self._get(gid, "interval") or DEFAULT_INTERVAL
        thr   = self._get(gid, "threshold") or DEFAULT_THRESHOLD
        maxp  = self._get(gid, "max_processed") or DEFAULT_MAX_PROC
        kw    = self._get(gid, "keywords") or {}
        dbg   = self._get(gid, "debug", False)
        ids   = self._get(gid, "processed_ids") or []

        task = self._tasks.get(gid)
        task_st = (
            "🟢 Running" if (task and not task.done())
            else "🔴 Stopped (task ended)" if task
            else "🔴 Not running"
        )

        ch = self.bot._channels.get(ch_id) if ch_id else None
        await ctx.reply(content=(
            f"**HypixelMonitor Status**\n"
            f"Enabled: `{en}` | Task: {task_st}\n"
            f"Channel: {f'<#{ch.id}>' if ch else '*(not set)*'}\n"
            f"Categories: {len(cats)} | Interval: {iv}s | Threshold: {thr}\n"
            f"Debug: `{dbg}` | Processed IDs: {len(ids)}/{maxp}\n"
            f"Keywords — higher: {len(kw.get('higher', []))}, "
            f"normal: {len(kw.get('normal', []))}, "
            f"lower: {len(kw.get('lower', []))}, "
            f"negative: {len(kw.get('negative', []))}"
        ))

    async def _cmd_taskinfo(self, ctx: fluxer.Message, args: str) -> None:
        task = self._tasks.get(ctx.guild_id)
        if not task:
            await ctx.reply(content="❌ No task exists for this guild.")
            return
        lines = [
            f"Task done: {'yes' if task.done() else 'no'}",
            f"Task cancelled: {'yes' if task.cancelled() else 'no'}",
        ]
        if task.done():
            try:
                exc = task.exception()
                lines.append(f"Exception: {type(exc).__name__}: {exc}" if exc else "Completed normally")
            except asyncio.InvalidStateError:
                lines.append("State unknown")
        lines.append(f"Has session: {'yes' if ctx.guild_id in self._sessions else 'no'}")
        await ctx.reply(content="\n".join(lines))

    async def _cmd_debugmode(self, ctx: fluxer.Message, args: str) -> None:
        val = args.strip().lower() in ("true", "1", "yes", "on")
        self._set(ctx.guild_id, "debug", val)
        await ctx.reply(content=f"Debug mode: `{val}`")

    async def _cmd_checknow(self, ctx: fluxer.Message, args: str) -> None:
        cats = self._get(ctx.guild_id, "forum_categories") or DEFAULT_FORUM_CATEGORIES
        if not cats:
            await ctx.reply(content="❌ No forum categories configured.")
            return
        await ctx.reply(content="🔍 Running check…")
        try:
            await self._check_categories(ctx.guild_id, cats)
            await ctx.reply(content="✅ Manual check done.")
        except Exception as exc:
            await ctx.reply(content=f"❌ Error: {exc}")

    async def _cmd_testdetect(self, ctx: fluxer.Message, args: str) -> None:
        title, _, body = args.partition("\n")
        kw     = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        detect = self._score_text(title.strip(), body.strip(), kw)
        lines  = [
            f"**Immediate**: {detect.immediate}",
            f"**Score**: {detect.score}  (context boost: +{detect.context_boost})",
            "**Matches by tier:**",
        ]
        for tier, vals in detect.matches.items():
            lines.append(f"  {tier}: {', '.join(vals) if vals else '*(none)*'}")
        if detect.breakdown:
            lines.append("**Scoring breakdown (first 15):**")
            for kw_name, (tier, pts) in list(detect.breakdown.items())[:15]:
                lines.append(f"  `{kw_name}` [{tier}] → {pts:+.1f}")
        await ctx.reply(content="\n".join(lines))

    async def _cmd_tune(self, ctx: fluxer.Message, args: str) -> None:
        parts = args.strip().split()
        limit = 10
        cat_name = None
        if parts and parts[-1].isdigit():
            limit = int(parts[-1])
            cat_name = " ".join(parts[:-1]) or None
        else:
            cat_name = " ".join(parts) or None

        cats = self._get(ctx.guild_id, "forum_categories") or DEFAULT_FORUM_CATEGORIES
        if not cats:
            await ctx.reply(content="No categories configured.")
            return

        if cat_name:
            cat = next((c for c in cats if c["name"].lower() == cat_name.lower()), None)
            if not cat:
                names = ", ".join(f"`{c['name']}`" for c in cats)
                await ctx.reply(content=f"Category not found. Available: {names}")
                return
            test_cats = [cat]
        else:
            test_cats = [cats[0]]

        kw      = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        session = await self._get_session(ctx.guild_id)

        await ctx.reply(content=f"🔍 Fetching up to {limit} threads from **{test_cats[0]['name']}**…")
        try:
            rows = []
            for cat in test_cats:
                threads = await self._get_recent_threads(session, cat)
                for thread in threads[:limit]:
                    if not thread["content"]:
                        thread["content"] = await self._get_thread_content(session, thread["url"])
                    detect = self._score_text(thread["title"], thread["content"], kw)
                    would_notify = self._should_notify(thread, detect, ctx.guild_id)
                    top_kws = ", ".join(
                        (detect.matches.get("higher") or [])[:2] +
                        (detect.matches.get("normal") or [])[:3]
                    ) or "—"
                    rows.append((thread["title"][:48], detect.score, "✓" if would_notify else "✗", top_kws[:30]))

            header = f"{'Title':<50} {'Score':<6} {'Notify':<7} Top keywords\n" + "─" * 85
            body_t = "\n".join(f"{t:<50} {s:<6.1f} {n:<7} {k}" for t, s, n, k in rows)
            await self._send_pages(ctx, f"```\n{header}\n{body_t}\n```")
        except Exception as exc:
            await ctx.reply(content=f"Error: {exc}")

    async def _cmd_cleartasks(self, ctx: fluxer.Message) -> None:
        cancelled = 0
        for t in self._tasks.values():
            if not t.cancelled():
                t.cancel()
                cancelled += 1
        await asyncio.sleep(2)
        for gid in list(self._tasks.keys()):
            await self._cleanup(gid)
        await ctx.reply(content=f"Cancelled {cancelled} task(s). Restarting…")
        for guild in self.bot.guilds:
            if self._get(guild.id, "enabled", False):
                await self._ensure_task(guild)
        await ctx.reply(content="✅ Tasks restarted for enabled guilds.")

    # ── Category sub-dispatcher ───────────────────────────────────────────────

    async def _dispatch_category(self, ctx: fluxer.Message, rest: str) -> None:
        parts = rest.strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""
        arg   = parts[1] if len(parts) > 1 else ""

        if sub == "add":
            await self._cat_add(ctx, arg)
        elif sub == "remove":
            await self._cat_remove(ctx, arg)
        elif sub == "list":
            await self._cat_list(ctx)
        else:
            await ctx.reply(content=(
                f"`{config.COMMAND_PREFIX}hmonitor category add <url> <name>`\n"
                f"`{config.COMMAND_PREFIX}hmonitor category remove <name>`\n"
                f"`{config.COMMAND_PREFIX}hmonitor category list`"
            ))

    async def _cat_add(self, ctx: fluxer.Message, arg: str) -> None:
        parts = arg.split(None, 1)
        if len(parts) < 2:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor category add <url> <name>`")
            return
        url, name = parts[0], parts[1].strip()
        cats = self._get(ctx.guild_id, "forum_categories") or deepcopy(DEFAULT_FORUM_CATEGORIES)
        if any(c["url"] == url or c["name"] == name for c in cats):
            await ctx.reply(content="A category with that URL or name already exists.")
            return
        cats.append({"url": url, "name": name})
        self._set(ctx.guild_id, "forum_categories", cats)
        await ctx.reply(content=f"Added category: **{name}**")

    async def _cat_remove(self, ctx: fluxer.Message, arg: str) -> None:
        name = arg.strip()
        if not name:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor category remove <name>`")
            return
        cats = self._get(ctx.guild_id, "forum_categories") or deepcopy(DEFAULT_FORUM_CATEGORIES)
        before = len(cats)
        cats = [c for c in cats if c["name"] != name]
        if len(cats) == before:
            await ctx.reply(content="No category with that name found.")
            return
        self._set(ctx.guild_id, "forum_categories", cats)
        await ctx.reply(content=f"Removed category: **{name}**")

    async def _cat_list(self, ctx: fluxer.Message) -> None:
        cats = self._get(ctx.guild_id, "forum_categories") or DEFAULT_FORUM_CATEGORIES
        if not cats:
            await ctx.reply(content="No categories configured.")
            return
        lines = ["**Monitored categories**"]
        for c in cats:
            lines.append(f"• **{c['name']}** — {c['url']}")
        await self._send_pages(ctx, "\n".join(lines))

    # ── Keyword sub-dispatcher ────────────────────────────────────────────────

    async def _dispatch_keyword(self, ctx: fluxer.Message, rest: str) -> None:
        parts = rest.strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""
        arg   = parts[1] if len(parts) > 1 else ""

        if sub == "add":
            await self._kw_add(ctx, arg)
        elif sub == "bulkadd":
            await self._kw_bulkadd(ctx, arg)
        elif sub == "remove":
            await self._kw_remove(ctx, arg)
        elif sub == "list":
            await self._kw_list(ctx, arg)
        elif sub == "find":
            await self._kw_find(ctx, arg)
        elif sub == "export":
            await self._kw_export(ctx)
        elif sub == "import":
            await self._kw_import(ctx, arg)
        else:
            await ctx.reply(content=(
                f"`{config.COMMAND_PREFIX}hmonitor keyword add <tier> <keyword>`\n"
                f"`{config.COMMAND_PREFIX}hmonitor keyword bulkadd <tier> <kw1, kw2, ...>`\n"
                f"`{config.COMMAND_PREFIX}hmonitor keyword remove <tier> <keyword>`\n"
                f"`{config.COMMAND_PREFIX}hmonitor keyword list [tier]`\n"
                f"`{config.COMMAND_PREFIX}hmonitor keyword find <search>`\n"
                f"`{config.COMMAND_PREFIX}hmonitor keyword export`\n"
                f"`{config.COMMAND_PREFIX}hmonitor keyword import [merge] <json>`\n"
                f"Valid tiers: `higher`, `normal`, `lower`, `negative`"
            ))

    _VALID_TIERS = frozenset({"higher", "normal", "lower", "negative"})

    async def _kw_add(self, ctx: fluxer.Message, arg: str) -> None:
        parts = arg.split(None, 1)
        if len(parts) < 2:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor keyword add <tier> <keyword>`")
            return
        tier, keyword = parts[0].lower(), parts[1].strip()
        if tier not in self._VALID_TIERS:
            await ctx.reply(content=f"Invalid tier. Use: {', '.join(sorted(self._VALID_TIERS))}")
            return
        kw = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        if keyword in kw[tier]:
            await ctx.reply(content="That keyword is already in this tier.")
            return
        kw[tier].append(keyword)
        self._set(ctx.guild_id, "keywords", kw)
        await ctx.reply(content=f"Added to **{tier}**: `{keyword}`")

    async def _kw_bulkadd(self, ctx: fluxer.Message, arg: str) -> None:
        parts = arg.split(None, 1)
        if len(parts) < 2:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor keyword bulkadd <tier> <kw1, kw2, ...>`")
            return
        tier, raw = parts[0].lower(), parts[1]
        if tier not in self._VALID_TIERS:
            await ctx.reply(content=f"Invalid tier. Use: {', '.join(sorted(self._VALID_TIERS))}")
            return
        new_kws = [k.strip() for k in raw.split(",") if k.strip()]
        if not new_kws:
            await ctx.reply(content="No keywords found — separate them with commas.")
            return
        kw = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        added, skipped = [], []
        for nk in new_kws:
            if nk in kw[tier]:
                skipped.append(nk)
            else:
                kw[tier].append(nk)
                added.append(nk)
        self._set(ctx.guild_id, "keywords", kw)
        lines = []
        if added:
            lines.append(f"✅ Added ({len(added)}): {', '.join(f'`{k}`' for k in added)}")
        if skipped:
            lines.append(f"⏭ Already present ({len(skipped)}): {', '.join(f'`{k}`' for k in skipped)}")
        await ctx.reply(content="\n".join(lines))

    async def _kw_remove(self, ctx: fluxer.Message, arg: str) -> None:
        parts = arg.split(None, 1)
        if len(parts) < 2:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor keyword remove <tier> <keyword>`")
            return
        tier, keyword = parts[0].lower(), parts[1].strip()
        if tier not in self._VALID_TIERS:
            await ctx.reply(content=f"Invalid tier. Use: {', '.join(sorted(self._VALID_TIERS))}")
            return
        kw = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        if keyword not in kw[tier]:
            await ctx.reply(content="Keyword not found in that tier.")
            return
        kw[tier].remove(keyword)
        self._set(ctx.guild_id, "keywords", kw)
        await ctx.reply(content=f"Removed from **{tier}**: `{keyword}`")

    async def _kw_list(self, ctx: fluxer.Message, arg: str) -> None:
        tier_filter = arg.strip().lower() or "all"
        kw = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        tiers = ("higher", "normal", "lower", "negative") if tier_filter == "all" else (tier_filter,)
        if any(t not in self._VALID_TIERS for t in tiers):
            await ctx.reply(content=f"Invalid tier. Use: {', '.join(sorted(self._VALID_TIERS))}, or `all`")
            return
        lines = []
        for t in tiers:
            vals = kw.get(t, [])
            lines.append(f"**{t.title()}** ({len(vals)})")
            for v in vals:
                lines.append(f"  • {v}")
        await self._send_pages(ctx, "\n".join(lines))

    async def _kw_find(self, ctx: fluxer.Message, arg: str) -> None:
        search = arg.strip().lower()
        if not search:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hmonitor keyword find <search>`")
            return
        kw = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        found = [
            f"**{tier}**: `{k}`"
            for tier in ("higher", "normal", "lower", "negative")
            for k in kw.get(tier, [])
            if search in k.lower()
        ]
        await ctx.reply(content="\n".join(found) if found else f"No keywords matching `{search}` found.")

    async def _kw_export(self, ctx: fluxer.Message) -> None:
        kw   = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        data = json.dumps(kw, indent=2)
        await self._send_pages(ctx, f"```json\n{data}\n```")

    async def _kw_import(self, ctx: fluxer.Message, arg: str) -> None:
        parts = arg.strip().split(None, 1)
        merge = False
        if parts and parts[0].lower() in ("true", "merge"):
            merge = True
            json_str = parts[1] if len(parts) > 1 else ""
        else:
            json_str = arg.strip()

        if not json_str:
            await ctx.reply(content=(
                f"Usage: `{config.COMMAND_PREFIX}hmonitor keyword import [merge] <json>`\n"
                f"Get the JSON from `{config.COMMAND_PREFIX}hmonitor keyword export`."
            ))
            return

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            await ctx.reply(content=f"Failed to parse JSON: {exc}")
            return

        if not all(k in self._VALID_TIERS for k in data):
            await ctx.reply(content="JSON must only contain keys: higher, normal, lower, negative.")
            return

        if merge:
            kw = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
            for tier, vals in data.items():
                kw[tier] = list(set(kw.get(tier, [])) | set(vals))
            self._set(ctx.guild_id, "keywords", kw)
            await ctx.reply(content="✅ Keywords merged.")
        else:
            self._set(ctx.guild_id, "keywords", data)
            await ctx.reply(content="✅ Keywords replaced.")

    # ── Help embed ────────────────────────────────────────────────────────────

    def _help_embed(self) -> fluxer.Embed:
        p = config.COMMAND_PREFIX
        return fluxer.Embed(
            title="🔍 HypixelMonitor — Help",
            color=_C_BLUE,
            description=(
                f"**Setup**\n"
                f"`{p}hmonitor quicksetup #channel` · `{p}hmonitor setchannel #channel`\n\n"
                f"**Control**\n"
                f"`{p}hmonitor enable` · `{p}hmonitor disable` · `{p}hmonitor restart`\n\n"
                f"**Tuning**\n"
                f"`{p}hmonitor setinterval <s>` · `{p}hmonitor setthreshold <1.0-10.0>`\n"
                f"`{p}hmonitor loaddefaults [merge]` · `{p}hmonitor debugmode <true|false>`\n\n"
                f"**Categories**\n"
                f"`{p}hmonitor category add <url> <name>`\n"
                f"`{p}hmonitor category remove <name>` · `{p}hmonitor category list`\n\n"
                f"**Keywords**\n"
                f"`{p}hmonitor keyword add <tier> <kw>` · `{p}hmonitor keyword bulkadd <tier> <kw1, kw2>`\n"
                f"`{p}hmonitor keyword remove <tier> <kw>` · `{p}hmonitor keyword list [tier]`\n"
                f"`{p}hmonitor keyword find <search>` · `{p}hmonitor keyword export/import`\n\n"
                f"**Diagnostics**\n"
                f"`{p}hmonitor status` · `{p}hmonitor taskinfo` · `{p}hmonitor checknow`\n"
                f"`{p}hmonitor testdetect <title>\\n[body]` · `{p}hmonitor tune [limit] [category]`\n"
                f"`{p}hmonitor processedcount` · `{p}hmonitor clearprocessed` · `{p}hmonitor setmaxprocessed <n>`\n\n"
                f"**Tiers**: `higher` (immediate) · `normal` · `lower` · `negative`\n"
                f"All commands require **Manage Guild** permission."
            ),
        )


async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(HypixelMonitorCog(bot))