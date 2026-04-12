"""
cogs/hypixel_updates.py
-----------------------
Monitors Hypixel forums for new / updated SkyBlock posts and sends
formatted embeds to a configured channel.

Sources:
  • skyblock-patch-notes.158   — all posts are SkyBlock, posted by Hypixel Team
  • news-and-announcements.4   — filtered: Hypixel Team only + "skyblock" in title
  • skyblock-alpha/            — filtered: Hypixel Team only (anyone can post here)

Pinned/sticky threads are re-checked every poll for edits; an "Updated"
embed is sent if the content changes.

Extra dependencies (pip install):
    aiohttp>=3.9.0   (already required by fluxer.py)
    beautifulsoup4

Commands (require Manage Guild):
    !hypixel setchannel #channel
    !hypixel status
    !hypixel togglesource <patch_notes|news|alpha>
    !hypixel togglepreview
    !hypixel check
    !hypixel setinterval <minutes>
    !hypixel setpingrole <source> <@role>
    !hypixel clearpingrole <source>
    !hypixel resetseen [source]
    !hypixel help

Settings are stored in guild_settings.json under the "hypixel_updates" key.
"""

import asyncio
import hashlib
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
from bs4 import BeautifulSoup

import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.hypixel_updates")

# ── Constants ─────────────────────────────────────────────────────────────────

HYPIXEL_TEAM_MEMBER_PATH = "/members/hypixel-team.377696/"

SOURCES: Dict[str, dict] = {
    "patch_notes": {
        "url": "https://hypixel.net/forums/skyblock-patch-notes.158/",
        "label": "SkyBlock Patch Notes",
        "emoji": "📋",
        "color": 0x55AAFF,
        "require_hypixel_team": False,
        "require_skyblock_in_title": False,
    },
    "news": {
        "url": "https://hypixel.net/forums/news-and-announcements.4/",
        "label": "News & Announcements",
        "emoji": "📰",
        "color": 0xFFAA00,
        "require_hypixel_team": True,
        "require_skyblock_in_title": True,
    },
    "alpha": {
        "url": "https://hypixel.net/skyblock-alpha/",
        "label": "SkyBlock Alpha",
        "emoji": "🔬",
        "color": 0xAA55FF,
        "require_hypixel_team": True,
        "require_skyblock_in_title": False,
    },
}

SKYBLOCK_KEYWORDS = ["skyblock", "sky block"]

DEFAULT_INTERVAL = 900   # 15 minutes
MIN_INTERVAL     = 300   # 5 minutes
MAX_INTERVAL     = 86400 # 24 hours

_UA = "HypixelUpdateChecker-FluxerBot/1.0 (compatible)"

_DEFAULT_GUILD_DATA = {
    "channel_id": None,
    "post_previews": True,
    "check_interval": DEFAULT_INTERVAL,
    "enabled_sources": {
        "patch_notes": True,
        "news": True,
        "alpha": True,
    },
    "ping_roles": {
        "patch_notes": None,
        "news": None,
        "alpha": None,
    },
    # { thread_id: { "hash": str, "is_sticky": bool } }
    "seen_threads": {
        "patch_notes": {},
        "news": {},
        "alpha": {},
    },
}

# ── Scraping helpers ──────────────────────────────────────────────────────────

def _is_skyblock_title(title: str) -> bool:
    lower = title.lower()
    return any(kw in lower for kw in SKYBLOCK_KEYWORDS)


def _truncate(text: str, limit: int = 450) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + " …"


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


async def _fetch_html(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(
            url,
            headers={"User-Agent": _UA},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                return await resp.text()
            log.warning("HTTP %s fetching %s", resp.status, url)
    except Exception as exc:
        log.error("Error fetching %s: %s", url, exc)
    return None


_THREAD_URL_RE = re.compile(r"^/threads/[^/]+\.(\d+)/?$")


def _find_container(tag):
    node = tag
    for _ in range(12):
        parent = node.parent
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if parent.name == "div":
            classes = " ".join(parent.get("class", []))
            if "structItem--thread" in classes:
                return parent
        node = parent
    return node


def _parse_thread_list(html: str, source_cfg: dict) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_ids: set = set()

    for link in soup.find_all("a", href=_THREAD_URL_RE):
        href = link.get("href", "")
        m = _THREAD_URL_RE.match(href)
        if not m:
            continue
        thread_id = m.group(1)

        if thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)

        title = link.get_text(strip=True)
        if not title:
            continue

        full_url = "https://hypixel.net" + href
        container = _find_container(link)
        container_text = container.get_text(separator=" ")

        container_data_author = container.get("data-author", "")
        posted_by_hypixel_team = (
            "hypixel team" in container_data_author.lower()
            or bool(container.find("a", href=lambda h: h and HYPIXEL_TEAM_MEMBER_PATH in h))
        )

        if source_cfg["require_hypixel_team"] and not posted_by_hypixel_team:
            continue
        if source_cfg["require_skyblock_in_title"] and not _is_skyblock_title(title):
            continue

        is_sticky  = "Sticky" in container_text
        is_official = "Official" in container_text

        results.append({
            "thread_id": thread_id,
            "title": title,
            "url": full_url,
            "is_sticky": is_sticky,
            "is_official": is_official,
        })

    return results


def _parse_post_content(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    post_body = (
        soup.find("div", class_="bbWrapper")
        or soup.find("article", class_=re.compile(r"message--post"))
        or soup.find("div", class_=re.compile(r"message-body|messageContent"))
    )

    if not post_body:
        candidates = soup.find_all(["div", "article"], class_=re.compile(r"block|content|post"))
        post_body = max(candidates, key=lambda t: len(t.get_text()), default=None)

    if not post_body:
        return {"preview": "", "spoilers": [], "raw_hash": ""}

    spoiler_titles = []
    for btn in post_body.find_all(class_=re.compile(r"bbCodeSpoiler-button-title")):
        label = btn.get_text(strip=True)
        label = re.sub(r"^spoiler\s*:?\s*", "", label, flags=re.IGNORECASE).strip()
        if label and label not in spoiler_titles:
            spoiler_titles.append(label)

    if not spoiler_titles:
        for tag in post_body.find_all(string=re.compile(r"spoiler\s*:", re.IGNORECASE)):
            label = re.sub(r"^spoiler\s*:?\s*", "", str(tag), flags=re.IGNORECASE).strip()
            if label and label not in spoiler_titles:
                spoiler_titles.append(label)

    for tag in post_body.find_all(["blockquote", "img", "figure", "script", "style"]):
        tag.decompose()
    for tag in post_body.find_all(class_=re.compile(r"bbCodeSpoiler-content|js-spoilerTarget")):
        tag.decompose()

    full_text  = post_body.get_text(separator="\n", strip=True)
    lines      = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    clean_text = "\n".join(lines)

    return {
        "preview":  _truncate(clean_text, 450),
        "spoilers": spoiler_titles[:10],
        "raw_hash": _content_hash(clean_text),
    }


# ── Cog ───────────────────────────────────────────────────────────────────────

class HypixelUpdatesCog(fluxer.Cog):
    """Polls Hypixel Forums for new SkyBlock posts and sends embed notifications."""

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()
        self._session: Optional[aiohttp.ClientSession] = None
        self._task:    Optional[asyncio.Task]          = None

    # ── Storage helpers ───────────────────────────────────────────────────────

    def _data(self, guild_id: int) -> dict:
        """Return the full settings dict for a guild, filling in defaults."""
        stored = self.settings.get(guild_id, "hypixel_updates") or {}
        merged = deepcopy(_DEFAULT_GUILD_DATA)
        # Deep-merge stored values over defaults so new keys always exist
        for k, v in stored.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged

    def _save(self, guild_id: int, data: dict) -> None:
        self.settings.set(guild_id, "hypixel_updates", data)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @fluxer.Cog.listener()
    async def on_ready(self) -> None:
        if self._task and not self._task.done():
            return
        self._session = aiohttp.ClientSession()
        await self._seed_all_guilds()
        self._task = asyncio.create_task(self._poll_loop())
        log.info("HypixelUpdates checker started (default interval %ds)", DEFAULT_INTERVAL)

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        log.info("HypixelUpdates checker stopped.")

    # ── Background loop ───────────────────────────────────────────────────────

    # -- Startup seeding ------------------------------------------------------

    async def _seed_all_guilds(self) -> None:
        """On first boot, silently mark all current threads as seen so we do not
        spam old posts the moment the bot starts."""
        for guild in self.bot.guilds:
            try:
                await self._seed_guild(guild.id)
            except Exception:
                log.exception("Error seeding guild %d", guild.id)

    async def _seed_guild(self, guild_id: int) -> None:
        """Scrape current thread listings for every enabled source and record
        all thread IDs as seen -- without sending any notifications."""
        data    = self._data(guild_id)
        enabled = data.get("enabled_sources", {})
        seen    = data.get("seen_threads", {})
        seeded  = False

        for source_key, source_cfg in SOURCES.items():
            if not enabled.get(source_key, True):
                continue

            source_seen = seen.setdefault(source_key, {})
            # Only seed sources that have no history yet
            if source_seen:
                continue

            await asyncio.sleep(2)
            listing_html = await _fetch_html(self._session, source_cfg["url"])
            if not listing_html:
                continue

            threads = _parse_thread_list(listing_html, source_cfg)
            for thread in threads:
                tid = thread["thread_id"]
                if tid not in source_seen:
                    source_seen[tid] = {"hash": "", "is_sticky": thread["is_sticky"]}

            log.info(
                "Seeded %d thread(s) for source '%s' in guild %d (no notifications sent)",
                len(threads), source_key, guild_id,
            )
            seeded = True

        if seeded:
            data["seen_threads"] = seen
            self._save(guild_id, data)

    async def _poll_loop(self) -> None:
        while True:
            # Compute next sleep before doing work so a slow check doesn't skip a cycle
            intervals = []
            for guild in self.bot.guilds:
                d = self._data(guild.id)
                intervals.append(d.get("check_interval", DEFAULT_INTERVAL))
            sleep_for = max(MIN_INTERVAL, min(intervals)) if intervals else DEFAULT_INTERVAL
            await asyncio.sleep(sleep_for)

            try:
                await self._check_all_guilds()
            except Exception:
                log.exception("Unhandled exception in HypixelUpdates poll loop")

    async def _check_all_guilds(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self._check_guild(guild.id)
            except Exception:
                log.exception("Error checking guild %d", guild.id)

    async def _check_guild(self, guild_id: int) -> None:
        data = self._data(guild_id)

        ch_id = data.get("channel_id")
        if not ch_id:
            return
        channel = self.bot._channels.get(ch_id)
        if not channel:
            return

        enabled    = data.get("enabled_sources", {})
        seen       = data.get("seen_threads", {})
        previews   = data.get("post_previews", True)
        ping_roles = data.get("ping_roles", {})

        for source_key, source_cfg in SOURCES.items():
            if not enabled.get(source_key, True):
                continue

            await asyncio.sleep(3)
            listing_html = await _fetch_html(self._session, source_cfg["url"])
            if not listing_html:
                continue

            threads     = _parse_thread_list(listing_html, source_cfg)
            source_seen = seen.get(source_key, {})

            for thread in reversed(threads):
                tid   = thread["thread_id"]
                known = source_seen.get(tid)

                need_content = previews or thread["is_sticky"]
                post_data: dict = {}

                if need_content:
                    await asyncio.sleep(2)
                    thread_html = await _fetch_html(self._session, thread["url"])
                    if thread_html:
                        post_data = _parse_post_content(thread_html)

                new_hash = post_data.get("raw_hash", "")

                if known is None:
                    embed = self._build_embed(thread, source_cfg, post_data, is_update=False)
                    await self._send(channel, embed, ping_roles.get(source_key))
                    source_seen[tid] = {"hash": new_hash, "is_sticky": thread["is_sticky"]}
                    log.info("New post %s (%s) in guild %d", tid, source_key, guild_id)

                elif thread["is_sticky"] and new_hash and new_hash != known.get("hash", ""):
                    embed = self._build_embed(thread, source_cfg, post_data, is_update=True)
                    await self._send(channel, embed, ping_roles.get(source_key))
                    source_seen[tid]["hash"] = new_hash
                    log.info("Updated sticky %s (%s) in guild %d", tid, source_key, guild_id)

            seen[source_key] = source_seen

        data["seen_threads"] = seen
        self._save(guild_id, data)

    # ── Embed builder ─────────────────────────────────────────────────────────

    def _build_embed(
        self,
        thread: dict,
        source_cfg: dict,
        post_data: dict,
        is_update: bool,
    ) -> fluxer.Embed:

        if is_update:
            tag = f"📝 Updated — {source_cfg['label']}"
        elif thread.get("is_sticky"):
            tag = f"📌 Pinned — {source_cfg['label']}"
        else:
            tag = f"{source_cfg['emoji']} New Post — {source_cfg['label']}"

        embed = fluxer.Embed(
            title=f"[{tag}] {thread['title']}",
            color=source_cfg["color"],
        )
        embed.url = thread["url"]

        preview = post_data.get("preview", "")
        if preview:
            embed.description = preview

        spoilers: list = post_data.get("spoilers", [])
        if spoilers:
            embed.add_field(
                name="📂 Sections in this post",
                value="\n".join(f"▸ {s}" for s in spoilers),
                inline=False,
            )

        embed.add_field(
            name="🔗 Read More",
            value=f"[Click to open on Hypixel Forums]({thread['url']})",
            inline=False,
        )

        embed.set_footer(
            text=f"Hypixel SkyBlock Updates • {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}"
        )
        return embed

    async def _send(
        self,
        channel,
        embed: fluxer.Embed,
        role_id: Optional[int] = None,
    ) -> None:
        try:
            content = f"<@&{role_id}>" if role_id else ""
            await channel.send(content=content, embed=embed)
        except Exception as exc:
            log.error("Failed to send embed to channel %s: %s", channel.id, exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_channel(self, guild_id: int, arg: str):
        m = re.search(r"<#(\d+)>", arg)
        if m:
            return self.bot._channels.get(int(m.group(1)))
        name = arg.lstrip("#").strip()
        for ch in self.bot._channels.values():
            if ch.guild_id == guild_id and ch.name == name:
                return ch
        return None

    # ── Command dispatcher ────────────────────────────────────────────────────

    @fluxer.Cog.command(name="hypixel")
    async def hypixel(self, ctx: fluxer.Message, *, args: str = "") -> None:
        """Hypixel SkyBlock update checker commands."""
        if ctx.guild_id is None:
            await ctx.reply(content="This command can only be used inside a server.")
            return

        parts = args.strip().split(None, 1)
        sub   = parts[0].lower() if parts else ""
        rest  = parts[1] if len(parts) > 1 else ""

        if not sub or sub == "help":
            await ctx.reply(embed=self._help_embed())
            return

        await self._gated(ctx, sub, rest)

    @fluxer.checks.has_permission(fluxer.Permissions.MANAGE_GUILD)
    async def _gated(self, ctx: fluxer.Message, sub: str, rest: str) -> None:
        dispatch = {
            "setchannel":    self._cmd_setchannel,
            "status":        self._cmd_status,
            "togglesource":  self._cmd_togglesource,
            "togglepreview": self._cmd_togglepreview,
            "check":         self._cmd_check,
            "setinterval":   self._cmd_setinterval,
            "setpingrole":   self._cmd_setpingrole,
            "clearpingrole": self._cmd_clearpingrole,
            "resetseen":     self._cmd_resetseen,
        }
        handler = dispatch.get(sub)
        if handler is None:
            await ctx.reply(embed=self._help_embed())
            return
        await handler(ctx, rest)

    # ── Sub-command handlers ──────────────────────────────────────────────────

    async def _cmd_setchannel(self, ctx: fluxer.Message, args: str) -> None:
        """Usage: !hypixel setchannel #channel"""
        if not args.strip():
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hypixel setchannel #channel`")
            return
        channel = self._resolve_channel(ctx.guild_id, args.strip())
        if channel is None:
            await ctx.reply(content="Could not find that channel.")
            return
        data = self._data(ctx.guild_id)
        data["channel_id"] = channel.id
        self._save(ctx.guild_id, data)
        await ctx.reply(content=f"✅ Updates will be posted to <#{channel.id}>.")

    async def _cmd_status(self, ctx: fluxer.Message, args: str) -> None:
        data = self._data(ctx.guild_id)

        ch_id      = data.get("channel_id")
        channel    = self.bot._channels.get(ch_id) if ch_id else None
        ch_str     = f"<#{ch_id}>" if channel else "❌ Not set"
        interval   = data.get("check_interval", DEFAULT_INTERVAL)
        previews   = data.get("post_previews", True)
        enabled    = data.get("enabled_sources", {})
        seen       = data.get("seen_threads", {})
        ping_roles = data.get("ping_roles", {})

        source_lines = []
        for k, v in SOURCES.items():
            state      = "✅" if enabled.get(k, True) else "❌"
            source_seen = seen.get(k, {})
            total      = len(source_seen)
            stickies   = sum(
                1 for d in source_seen.values()
                if isinstance(d, dict) and d.get("is_sticky")
            )
            detail = f"({total} seen" + (f", {stickies} pinned" if stickies else "") + ")"
            role_id  = ping_roles.get(k)
            role_str = f" — ping: <@&{role_id}>" if role_id else ""
            source_lines.append(f"{state} **{k}** — {v['label']} {detail}{role_str}")

        embed = fluxer.Embed(title="Hypixel Update Checker — Status", color=0x55AAFF)
        embed.add_field(name="Channel",       value=ch_str,                                     inline=False)
        embed.add_field(name="Check interval", value=f"Every {interval // 60} minutes",          inline=True)
        embed.add_field(name="Post previews",  value="✅ Yes" if previews else "❌ No",           inline=True)
        embed.add_field(name="Sources",        value="\n".join(source_lines),                    inline=False)
        await ctx.reply(embed=embed)

    async def _cmd_togglesource(self, ctx: fluxer.Message, args: str) -> None:
        """Usage: !hypixel togglesource <patch_notes|news|alpha>"""
        source = args.strip().lower()
        if source not in SOURCES:
            valid = ", ".join(f"`{k}`" for k in SOURCES)
            await ctx.reply(content=f"❌ Unknown source. Valid options: {valid}")
            return
        data = self._data(ctx.guild_id)
        data["enabled_sources"][source] = not data["enabled_sources"].get(source, True)
        self._save(ctx.guild_id, data)
        state = "enabled" if data["enabled_sources"][source] else "disabled"
        await ctx.reply(content=f"✅ Source `{source}` is now **{state}**.")

    async def _cmd_togglepreview(self, ctx: fluxer.Message, args: str) -> None:
        data = self._data(ctx.guild_id)
        data["post_previews"] = not data.get("post_previews", True)
        self._save(ctx.guild_id, data)
        state = "enabled" if data["post_previews"] else "disabled"
        await ctx.reply(content=f"✅ Post previews are now **{state}**.")

    async def _cmd_check(self, ctx: fluxer.Message, args: str) -> None:
        await ctx.reply(content="🔍 Running check…")
        try:
            await self._check_all_guilds()
            await ctx.reply(content="✅ Check complete.")
        except Exception as exc:
            await ctx.reply(content=f"❌ Error: {exc}")

    async def _cmd_setinterval(self, ctx: fluxer.Message, args: str) -> None:
        """Usage: !hypixel setinterval <minutes>  (min 5, max 1440)"""
        if not args.strip().isdigit():
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hypixel setinterval <minutes>`")
            return
        minutes = int(args.strip())
        if minutes < 5:
            await ctx.reply(content="❌ Minimum interval is 5 minutes.")
            return
        if minutes > 1440:
            await ctx.reply(content="❌ Maximum interval is 1440 minutes (24 hours).")
            return
        data = self._data(ctx.guild_id)
        data["check_interval"] = minutes * 60
        self._save(ctx.guild_id, data)
        await ctx.reply(content=f"✅ Will check for updates every **{minutes} minutes**.")

    async def _cmd_setpingrole(self, ctx: fluxer.Message, args: str) -> None:
        """Usage: !hypixel setpingrole <source> <@role>"""
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            await ctx.reply(content=f"Usage: `{config.COMMAND_PREFIX}hypixel setpingrole <source> <@role>`")
            return
        source   = parts[0].lower()
        role_arg = parts[1].strip()
        if source not in SOURCES:
            valid = ", ".join(f"`{k}`" for k in SOURCES)
            await ctx.reply(content=f"❌ Unknown source. Valid options: {valid}")
            return
        m = re.fullmatch(r"<@&(\d+)>", role_arg) or re.fullmatch(r"(\d+)", role_arg)
        if not m:
            await ctx.reply(content="❌ Could not parse that as a role. Use a mention or raw role ID.")
            return
        role_id = int(m.group(1))
        data = self._data(ctx.guild_id)
        data["ping_roles"][source] = role_id
        self._save(ctx.guild_id, data)
        await ctx.reply(content=f"✅ <@&{role_id}> will be pinged for **{SOURCES[source]['label']}** updates.")

    async def _cmd_clearpingrole(self, ctx: fluxer.Message, args: str) -> None:
        """Usage: !hypixel clearpingrole <source>"""
        source = args.strip().lower()
        if source not in SOURCES:
            valid = ", ".join(f"`{k}`" for k in SOURCES)
            await ctx.reply(content=f"❌ Unknown source. Valid options: {valid}")
            return
        data = self._data(ctx.guild_id)
        data["ping_roles"][source] = None
        self._save(ctx.guild_id, data)
        await ctx.reply(content=f"✅ Ping role cleared for **{SOURCES[source]['label']}**.")

    async def _cmd_resetseen(self, ctx: fluxer.Message, args: str) -> None:
        """Usage: !hypixel resetseen [source]  — ⚠️ will re-announce old posts!"""
        source = args.strip().lower() or None
        data   = self._data(ctx.guild_id)
        if source:
            if source not in SOURCES:
                valid = ", ".join(f"`{k}`" for k in SOURCES)
                await ctx.reply(content=f"❌ Unknown source. Valid options: {valid}")
                return
            data["seen_threads"][source] = {}
            self._save(ctx.guild_id, data)
            await ctx.reply(content=f"✅ Reset seen threads for `{source}`.")
        else:
            data["seen_threads"] = {k: {} for k in SOURCES}
            self._save(ctx.guild_id, data)
            await ctx.reply(content="✅ Reset seen threads for all sources.")

    # ── Help embed ────────────────────────────────────────────────────────────

    def _help_embed(self) -> fluxer.Embed:
        p = config.COMMAND_PREFIX
        return fluxer.Embed(
            title="📡 Hypixel Update Checker — Help",
            color=0x55AAFF,
            description=(
                f"**Setup**\n"
                f"`{p}hypixel setchannel #channel` — Set the notification channel\n\n"
                f"**Control**\n"
                f"`{p}hypixel togglesource <source>` — Enable/disable a source\n"
                f"`{p}hypixel togglepreview` — Toggle post text previews\n"
                f"`{p}hypixel setinterval <minutes>` — Set poll interval (min 5)\n"
                f"`{p}hypixel check` — Run a check right now\n\n"
                f"**Ping Roles**\n"
                f"`{p}hypixel setpingrole <source> <@role>` — Ping a role on new posts\n"
                f"`{p}hypixel clearpingrole <source>` — Remove the ping role\n\n"
                f"**Diagnostics**\n"
                f"`{p}hypixel status` — Show current config\n"
                f"`{p}hypixel resetseen [source]` — ⚠️ Clears seen history (re-announces!)\n\n"
                f"**Valid sources:** `patch_notes` · `news` · `alpha`\n"
                f"All commands require **Manage Guild** permission."
            ),
        )


async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(HypixelUpdatesCog(bot))