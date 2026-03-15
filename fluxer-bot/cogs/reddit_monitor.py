"""
cogs/reddit_monitor.py
----------------------
Monitors configured subreddits for mod / tech-support posts and posts
Fluxer embeds to a configured channel.

Extra dependencies (pip install):
    asyncpraw>=7.7.0

Detection tiers — identical to HypixelMonitor:
  higher   → immediate notify regardless of threshold
  normal   → +6.0 title / +3.0 body per phrase  (÷2 for single words)
  lower    → +3.0 title / +1.5 body per phrase  (÷2 for single words)
  negative → −4.0 title / −2.0 body

Context boost (+0.5 each, capped at +2.0).

Commands (require Manage Guild, except !rmonitor cleartasks which needs OWNER_ID):
    !rmonitor quicksetup #channel
    !rmonitor setcreds <client_id> <client_secret> <user_agent>
    !rmonitor setchannel #channel
    !rmonitor enable / disable / restart
    !rmonitor setinterval <seconds>
    !rmonitor setthreshold <float>
    !rmonitor addsub <n> / remsub <n> / listsubs
    !rmonitor setflair [flair text]
    !rmonitor loaddefaults [merge]
    !rmonitor processedcount / clearprocessed / setmaxprocessed <n>
    !rmonitor status / taskinfo / debugmode <true|false>
    !rmonitor checknow
    !rmonitor testdetect <title>\\n[body]
    !rmonitor tune <subreddit> [limit]
    !rmonitor cleartasks          (OWNER_ID only)
    !rmonitor keyword add <tier> <keyword>
    !rmonitor keyword bulkadd <tier> <kw1, kw2, ...>
    !rmonitor keyword remove <tier> <keyword>
    !rmonitor keyword list [tier]
    !rmonitor keyword find <search>
    !rmonitor keyword export
    !rmonitor keyword import [merge] <json>

Credentials are stored in guild_settings.json under the rm_ prefix.
Run !rmonitor setcreds in a private channel — the message is deleted immediately.
"""

import asyncio
import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional

import asyncpraw
import asyncpraw.models

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

log = logging.getLogger("cog.reddit_monitor")

# ── Limits ────────────────────────────────────────────────────────────────────
MIN_INTERVAL      = 60
DEFAULT_INTERVAL  = 900
DEFAULT_THRESHOLD = 3.0
DEFAULT_MAX_PROC  = 1000

# ── Embed colours ─────────────────────────────────────────────────────────────
_C_RED    = 0xED4245
_C_ORANGE = 0xE67E22
_C_GOLD   = 0xF1C40F
_C_GREEN  = 0x57F287
_C_BLUE   = 0x5865F2

# ─────────────────────────────────────────────────────────────────────────────
class RedditMonitorCog(fluxer.Cog):
    """Monitor subreddits for mod-related questions and technical help."""

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()
        self._tasks:          Dict[int, asyncio.Task]      = {}
        self._reddit_clients: Dict[int, asyncpraw.Reddit]  = {}
        self._task_locks:     Dict[int, asyncio.Lock]      = {}
        self._proc_locks:     Dict[int, asyncio.Lock]      = {}

    # ── Storage helpers ───────────────────────────────────────────────────────

    def _get(self, guild_id: int, key: str, default=None):
        return self.settings.get(guild_id, f"rm_{key}", default)

    def _set(self, guild_id: int, key: str, value) -> None:
        self.settings.set(guild_id, f"rm_{key}", value)

    def _del(self, guild_id: int, key: str) -> None:
        self.settings.delete(guild_id, f"rm_{key}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @fluxer.Cog.listener()
    async def on_guild_join(self, guild: fluxer.Guild) -> None:
        if self._get(guild.id, "enabled", False):
            await self._ensure_task(guild)
            log.info("RedditMonitor resuming task for guild %d (%s)", guild.id, guild)

    async def cog_unload(self) -> None:
        log.info("Shutting down RedditMonitor…")
        tasks = list(self._tasks.values())
        for t in tasks:
            if not t.cancelled():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for reddit in self._reddit_clients.values():
            try:
                await reddit.close()
            except Exception:
                pass
        self._reddit_clients.clear()
        self._task_locks.clear()
        self._proc_locks.clear()

    # ── Reddit client ─────────────────────────────────────────────────────────

    async def _get_reddit(self, guild_id: int) -> Optional[asyncpraw.Reddit]:
        if guild_id in self._reddit_clients:
            return self._reddit_clients[guild_id]
        cid    = self._get(guild_id, "reddit_client_id")
        secret = self._get(guild_id, "reddit_client_secret")
        ua     = self._get(guild_id, "reddit_user_agent")
        if not (cid and secret and ua):
            return None
        try:
            reddit = asyncpraw.Reddit(client_id=cid, client_secret=secret, user_agent=ua)
            self._reddit_clients[guild_id] = reddit
            return reddit
        except Exception as exc:
            log.exception("Reddit client creation failed: %s", exc)
            return None

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

    def _should_notify(
        self,
        submission: asyncpraw.models.Submission,
        detect: ScoreResult,
        guild_id: int,
    ) -> bool:
        threshold = self._get(guild_id, "threshold") or DEFAULT_THRESHOLD
        return should_notify(
            submission.title or "",
            getattr(submission, "selftext", "") or "",
            detect,
            threshold,
        )

    # ── Notification ──────────────────────────────────────────────────────────

    async def _notify(
        self,
        guild_id: int,
        submission: asyncpraw.models.Submission,
        detect: ScoreResult,
    ) -> None:
        ch_id = self._get(guild_id, "notify_channel_id")
        if not ch_id:
            return
        channel = self.bot._channels.get(ch_id)
        if not channel:
            return

        score   = detect.score
        created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)

        if detect.immediate:
            confidence, color = "🔴 HIGH (Immediate)", _C_RED
        elif score >= 6.0:
            confidence, color = "🟠 HIGH",   _C_ORANGE
        elif score >= 3.0:
            confidence, color = "🟡 MEDIUM", _C_GOLD
        else:
            confidence, color = "🟢 LOW",    _C_GREEN

        selftext = getattr(submission, "selftext", "") or ""
        embed = (
            fluxer.Embed(
                title=(submission.title or "")[:256],
                description=((selftext[:500] + "…") if len(selftext) > 500 else selftext or "No text"),
                color=color,
            )
            .add_field(name="Confidence", value=confidence,                                  inline=True)
            .add_field(name="Score",      value=f"{score:.1f}",                              inline=True)
            .add_field(name="Subreddit",  value=f"r/{submission.subreddit.display_name}",    inline=True)
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

        embed.set_footer(text=f"u/{submission.author} • {submission.id} • {created.strftime('%d %b %Y %H:%M UTC')}")

        if submission.permalink:
            embed.url = f"https://reddit.com{submission.permalink}"

        try:
            await channel.send(embed=embed)
        except Exception as exc:
            log.exception("Failed to send notification: %s", exc)

    # ── Processed-ID helpers ──────────────────────────────────────────────────

    def _proc_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._proc_locks:
            self._proc_locks[guild_id] = asyncio.Lock()
        return self._proc_locks[guild_id]

    async def _add_processed(self, guild_id: int, post_id: str) -> None:
        async with self._proc_lock(guild_id):
            processed = self._get(guild_id, "processed_ids") or []
            maxp = self._get(guild_id, "max_processed") or DEFAULT_MAX_PROC
            if post_id not in processed:
                processed.append(post_id)
            if len(processed) > maxp:
                processed = processed[-maxp:]
            self._set(guild_id, "processed_ids", processed)

    async def _is_processed(self, guild_id: int, post_id: str) -> bool:
        processed = self._get(guild_id, "processed_ids") or []
        return post_id in processed

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

    # ── Monitoring loop ───────────────────────────────────────────────────────

    async def _monitor_guild(self, guild_id: int) -> None:
        log.info("RedditMonitor started: guild %d", guild_id)
        try:
            reddit = await self._get_reddit(guild_id)
            if reddit is None:
                log.warning("No Reddit credentials for guild %d — stopping", guild_id)
                return

            while True:
                try:
                    if not self._get(guild_id, "enabled", False):
                        log.info("Monitoring disabled: guild %d", guild_id)
                        break

                    subs = self._get(guild_id, "subreddits") or []
                    if not subs:
                        await self._debug(guild_id, "⚠️ Monitor alive — no subreddits configured.")
                    else:
                        await self._check_subreddits(guild_id, reddit, subs)

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

    async def _check_subreddits(
        self,
        guild_id: int,
        reddit: asyncpraw.Reddit,
        subreddits: List[str],
    ) -> None:
        keywords     = self._get(guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        flair_filter = self._get(guild_id, "flair_filter")
        notified     = 0
        checked      = 0

        for sub_name in subreddits:
            try:
                sub = await reddit.subreddit(sub_name)
                async for submission in sub.new(limit=25):
                    checked += 1
                    if await self._is_processed(guild_id, submission.id):
                        continue

                    if flair_filter:
                        flair = getattr(submission, "link_flair_text", None) or ""
                        if flair_filter.lower() not in flair.lower():
                            await self._add_processed(guild_id, submission.id)
                            continue

                    title  = submission.title or ""
                    body   = getattr(submission, "selftext", "") or ""
                    detect = self._score_text(title, body, keywords)
                    await self._add_processed(guild_id, submission.id)
                    if self._should_notify(submission, detect, guild_id):
                        await self._notify(guild_id, submission, detect)
                        notified += 1
                        log.info("Notified: %s in r/%s (guild %d)", submission.id, sub_name, guild_id)

            except Exception:
                log.exception("Subreddit error (%s): guild %d", sub_name, guild_id)

        if notified == 0:
            await self._debug(
                guild_id,
                f"✅ Monitor alive — checked {checked} posts across "
                f"{len(subreddits)} subreddit(s). No matches this cycle.",
            )

    # ── Task management ───────────────────────────────────────────────────────

    async def _cleanup(self, guild_id: int) -> None:
        self._tasks.pop(guild_id, None)
        reddit = self._reddit_clients.pop(guild_id, None)
        if reddit:
            try:
                await reddit.close()
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

    @fluxer.Cog.command(name="rmonitor")
    async def rmonitor(self, ctx: fluxer.Message, *, args: str = "") -> None:
        """Dispatcher for all !rmonitor sub-commands."""
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
            "setcreds":        self._cmd_setcreds,
            "setchannel":      self._cmd_setchannel,
            "enable":          self._cmd_enable,
            "disable":         self._cmd_disable,
            "restart":         self._cmd_restart,
            "setinterval":     self._cmd_setinterval,
            "setthreshold":    self._cmd_setthreshold,
            "addsub":          self._cmd_addsub,
            "remsub":          self._cmd_remsub,
            "listsubs":        self._cmd_listsubs,
            "setflair":        self._cmd_setflair,
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
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor quicksetup #channel`")
            return
        channel = self._resolve_channel(ctx.guild_id, args.strip())
        if channel is None:
            await ctx.reply(content="Could not find that channel.")
            return
        self._set(ctx.guild_id, "notify_channel_id", channel.id)
        self._set(ctx.guild_id, "keywords", deepcopy(DEFAULT_KEYWORDS))
        await self._reply(ctx, embed=fluxer.Embed(
            title="✅ Quick setup complete!",
            description=(
                f"📢 Channel: <#{channel.id}>\n"
                f"🔑 Default keywords loaded\n\n"
                f"**Next steps:**\n"
                f"• Set credentials: `{config.COMMAND_PREFIX}rmonitor setcreds <id> <secret> <user_agent>`\n"
                f"• Add subreddits: `{config.COMMAND_PREFIX}rmonitor addsub HypixelSkyblock`\n"
                f"• Start: `{config.COMMAND_PREFIX}rmonitor enable`"
            ),
            color=_C_GREEN,
        ))

    async def _cmd_setcreds(self, ctx: fluxer.Message, args: str) -> None:
        parts = args.strip().split(None, 2)
        if len(parts) < 3:
            await ctx.reply(content=(
                f"Usage: `{config.COMMAND_PREFIX}rmonitor setcreds <client_id> <client_secret> <user_agent>`\n"
                "⚠️ Run this in a private channel — the message will be deleted immediately."
            ))
            return

        client_id, client_secret, user_agent = parts[0], parts[1], parts[2]
        self._set(ctx.guild_id, "reddit_client_id",     client_id)
        self._set(ctx.guild_id, "reddit_client_secret", client_secret)
        self._set(ctx.guild_id, "reddit_user_agent",    user_agent)

        old = self._reddit_clients.pop(ctx.guild_id, None)
        if old:
            try:
                await old.close()
            except Exception:
                pass

        try:
            await ctx.delete()
        except Exception:
            pass

        await self.bot._channels.get(ctx.channel_id).send(
            content="✅ Reddit credentials saved (original message deleted for safety)."
        ) if self.bot._channels.get(ctx.channel_id) else None

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
        if not await self._get_reddit(ctx.guild_id):
            await ctx.reply(content=(
                "❌ Reddit credentials not configured.\n"
                f"Run `{config.COMMAND_PREFIX}rmonitor setcreds` first."
            ))
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
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor setinterval <seconds>`")
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
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor setthreshold <1.0-10.0>`")
            return
        if not 1.0 <= threshold <= 10.0:
            await ctx.reply(content="Threshold must be between 1.0 and 10.0.")
            return
        self._set(ctx.guild_id, "threshold", threshold)
        await ctx.reply(content=f"Threshold set to {threshold}.")

    async def _cmd_addsub(self, ctx: fluxer.Message, args: str) -> None:
        sub = args.strip().lstrip("r/").strip()
        if not sub:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor addsub HypixelSkyblock`")
            return
        subs = self._get(ctx.guild_id, "subreddits") or []
        if sub in subs:
            await ctx.reply(content="Already monitoring that subreddit.")
            return
        subs.append(sub)
        self._set(ctx.guild_id, "subreddits", subs)
        await ctx.reply(content=f"Added: r/{sub}")

    async def _cmd_remsub(self, ctx: fluxer.Message, args: str) -> None:
        sub = args.strip().lstrip("r/").strip()
        if not sub:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor remsub HypixelSkyblock`")
            return
        subs = self._get(ctx.guild_id, "subreddits") or []
        if sub not in subs:
            await ctx.reply(content="That subreddit isn't in the list.")
            return
        subs.remove(sub)
        self._set(ctx.guild_id, "subreddits", subs)
        await ctx.reply(content=f"Removed: r/{sub}")

    async def _cmd_listsubs(self, ctx: fluxer.Message, args: str = "") -> None:
        subs = self._get(ctx.guild_id, "subreddits") or []
        if not subs:
            await ctx.reply(content="No subreddits configured.")
            return
        await ctx.reply(content="**Monitored subreddits**\n" + "\n".join(f"• r/{s}" for s in subs))

    async def _cmd_setflair(self, ctx: fluxer.Message, args: str) -> None:
        flair = args.strip() or None
        self._set(ctx.guild_id, "flair_filter", flair)
        if flair:
            await ctx.reply(content=f"Flair filter set to: `{flair}`")
        else:
            await ctx.reply(content="Flair filter cleared — all flairs will be checked.")

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
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor setmaxprocessed <n>`")
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
        subs  = self._get(gid, "subreddits") or []
        ch_id = self._get(gid, "notify_channel_id")
        iv    = self._get(gid, "interval") or DEFAULT_INTERVAL
        thr   = self._get(gid, "threshold") or DEFAULT_THRESHOLD
        maxp  = self._get(gid, "max_processed") or DEFAULT_MAX_PROC
        kw    = self._get(gid, "keywords") or {}
        dbg   = self._get(gid, "debug", False)
        ids   = self._get(gid, "processed_ids") or []
        flair = self._get(gid, "flair_filter")
        creds = self._get(gid, "reddit_client_id")

        task = self._tasks.get(gid)
        task_st = (
            "🟢 Running" if (task and not task.done())
            else "🔴 Stopped (task ended)" if task
            else "🔴 Not running"
        )

        ch = self.bot._channels.get(ch_id) if ch_id else None
        await ctx.reply(content=(
            f"**RedditMonitor Status**\n"
            f"Enabled: `{en}` | Task: {task_st}\n"
            f"Channel: {f'<#{ch.id}>' if ch else '*(not set)*'}\n"
            f"Subreddits: {', '.join(subs) if subs else '*(none)*'}\n"
            f"Interval: {iv}s | Threshold: {thr} | Flair filter: `{flair or 'none'}`\n"
            f"Debug: `{dbg}` | Processed IDs: {len(ids)}/{maxp}\n"
            f"Reddit API: {'✅ Configured' if creds else '❌ Not configured'}\n"
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
        lines.append(f"Has Reddit client: {'yes' if ctx.guild_id in self._reddit_clients else 'no'}")
        await ctx.reply(content="\n".join(lines))

    async def _cmd_debugmode(self, ctx: fluxer.Message, args: str) -> None:
        val = args.strip().lower() in ("true", "1", "yes", "on")
        self._set(ctx.guild_id, "debug", val)
        await ctx.reply(content=f"Debug mode: `{val}`")

    async def _cmd_checknow(self, ctx: fluxer.Message, args: str) -> None:
        reddit = await self._get_reddit(ctx.guild_id)
        if reddit is None:
            await ctx.reply(content="❌ Reddit credentials not configured.")
            return
        subs = self._get(ctx.guild_id, "subreddits") or []
        if not subs:
            await ctx.reply(content="❌ No subreddits configured.")
            return
        await ctx.reply(content="🔍 Running check…")
        try:
            await self._check_subreddits(ctx.guild_id, reddit, subs)
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
        if not parts:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor tune <subreddit> [limit]`")
            return

        limit = 10
        if len(parts) > 1 and parts[-1].isdigit():
            limit = int(parts[-1])
            sub_name = parts[0].lstrip("r/")
        else:
            sub_name = parts[0].lstrip("r/")

        reddit = await self._get_reddit(ctx.guild_id)
        if not reddit:
            await ctx.reply(content="Reddit credentials not configured.")
            return

        kw = self._get(ctx.guild_id, "keywords") or deepcopy(DEFAULT_KEYWORDS)
        await ctx.reply(content=f"🔍 Fetching up to {limit} posts from r/{sub_name}…")

        try:
            rows = []
            sr   = await reddit.subreddit(sub_name)
            async for submission in sr.new(limit=limit):
                title  = submission.title or ""
                body   = getattr(submission, "selftext", "") or ""
                detect = self._score_text(title, body, kw)
                would_notify = self._should_notify(submission, detect, ctx.guild_id)
                top_kws = ", ".join(
                    (detect.matches.get("higher") or [])[:2] +
                    (detect.matches.get("normal") or [])[:3]
                ) or "—"
                rows.append((title[:48], detect.score, "✓" if would_notify else "✗", top_kws[:30]))

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
                f"`{config.COMMAND_PREFIX}rmonitor keyword add <tier> <keyword>`\n"
                f"`{config.COMMAND_PREFIX}rmonitor keyword bulkadd <tier> <kw1, kw2, ...>`\n"
                f"`{config.COMMAND_PREFIX}rmonitor keyword remove <tier> <keyword>`\n"
                f"`{config.COMMAND_PREFIX}rmonitor keyword list [tier]`\n"
                f"`{config.COMMAND_PREFIX}rmonitor keyword find <search>`\n"
                f"`{config.COMMAND_PREFIX}rmonitor keyword export`\n"
                f"`{config.COMMAND_PREFIX}rmonitor keyword import [merge] <json>`\n"
                f"Valid tiers: `higher`, `normal`, `lower`, `negative`"
            ))

    _VALID_TIERS = frozenset({"higher", "normal", "lower", "negative"})

    async def _kw_add(self, ctx: fluxer.Message, arg: str) -> None:
        parts = arg.split(None, 1)
        if len(parts) < 2:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor keyword add <tier> <keyword>`")
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
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor keyword bulkadd <tier> <kw1, kw2, ...>`")
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
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor keyword remove <tier> <keyword>`")
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
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}rmonitor keyword find <search>`")
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
                f"Usage: `{config.COMMAND_PREFIX}rmonitor keyword import [merge] <json>`\n"
                f"Get the JSON from `{config.COMMAND_PREFIX}rmonitor keyword export`."
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
            title="🤖 RedditMonitor — Help",
            color=_C_BLUE,
            description=(
                f"**Setup**\n"
                f"`{p}rmonitor quicksetup #channel`\n"
                f"`{p}rmonitor setcreds <id> <secret> <user_agent>` *(use a private channel)*\n"
                f"`{p}rmonitor setchannel #channel`\n\n"
                f"**Subreddits**\n"
                f"`{p}rmonitor addsub <n>` · `{p}rmonitor remsub <n>` · `{p}rmonitor listsubs`\n"
                f"`{p}rmonitor setflair [flair text]`\n\n"
                f"**Control**\n"
                f"`{p}rmonitor enable` · `{p}rmonitor disable` · `{p}rmonitor restart`\n\n"
                f"**Tuning**\n"
                f"`{p}rmonitor setinterval <s>` · `{p}rmonitor setthreshold <1.0-10.0>`\n"
                f"`{p}rmonitor loaddefaults [merge]` · `{p}rmonitor debugmode <true|false>`\n\n"
                f"**Keywords**\n"
                f"`{p}rmonitor keyword add <tier> <kw>` · `{p}rmonitor keyword bulkadd <tier> <kw1, kw2>`\n"
                f"`{p}rmonitor keyword remove <tier> <kw>` · `{p}rmonitor keyword list [tier]`\n"
                f"`{p}rmonitor keyword find <search>` · `{p}rmonitor keyword export/import`\n\n"
                f"**Diagnostics**\n"
                f"`{p}rmonitor status` · `{p}rmonitor taskinfo` · `{p}rmonitor checknow`\n"
                f"`{p}rmonitor testdetect <title>\\n[body]` · `{p}rmonitor tune <subreddit> [limit]`\n"
                f"`{p}rmonitor processedcount` · `{p}rmonitor clearprocessed` · `{p}rmonitor setmaxprocessed <n>`\n\n"
                f"**Tiers**: `higher` (immediate) · `normal` · `lower` · `negative`\n"
                f"All commands require **Manage Guild** permission."
            ),
        )


async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(RedditMonitorCog(bot))