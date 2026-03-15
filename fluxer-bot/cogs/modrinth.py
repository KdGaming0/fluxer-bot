"""
cogs/modrinth.py
----------------
Tracks Modrinth mods and posts a rich embed notification when a new version
is released. Polling runs as a background asyncio task — no extra processes
or files needed.

Commands (require Manage Guild permission):
    !track add <id/slug> <#channel> [@role...] [--mc 1.21.4] [--loader fabric]
    !track bulk <#channel> [@role...] [--loader fabric] [--mc 1.21.4] -- <id1> <id2> ...
    !track remove <id/slug>
    !track list
    !track check                              — manual update check (this guild only)
    !track set channel <id> <#channel>
    !track set mc <id> [versions...]
    !track set loader <id> [loader]
    !track set roles <id> [@role...]
    !track set mc-all [versions...]
    !track set loader-all [loader]
    !track set mc-channel <#channel> [versions...]
    !track set loader-channel <#channel> [loader]
    !track set roles-channel <#channel> [@role...]
    !track set channel-channel <#old> <#new>
    !track default loader [loader]
    !track interval <seconds>                 — bot owner only
    !track help

Config additions needed in config.py / environment:
    OWNER_ID                  — user ID allowed to run !track interval
    MODRINTH_CHECK_INTERVAL   — seconds between polls (default 300, minimum 60)

Storage layout in guild_settings.json under key "modrinth":
    {
        "default_loader": "fabric" | null,
        "tracked": {
            "<project_id>": {
                "channel_id":     123,
                "roles":          [456, 789],
                "mc_versions":    ["1.21.4"],
                "loader":         "fabric" | null,
                "last_version_id": "abc123" | null,
                "project_name":   "Sodium"
            }
        }
    }
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import aiohttp

import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.modrinth")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODRINTH_API = "https://api.modrinth.com/v2"
_USER_AGENT = "FluxerBot-ModrinthUpdateChecker/1.0 (fluxer bot)"
_COLOR_UPDATE = 0x1BD96A   # Modrinth green
_COLOR_INFO = 0x5865F2     # Standard bot blue
_MIN_INTERVAL = 60         # seconds

_VALID_LOADERS = frozenset({
    "fabric", "forge", "quilt", "neoforge",
    "liteloader", "modloader", "rift", "minecraft",
})


# ---------------------------------------------------------------------------
# Modrinth API helpers
# ---------------------------------------------------------------------------

async def _api_get(session: aiohttp.ClientSession, path: str, params: dict | None = None):
    """Make a GET request to the Modrinth API. Returns parsed JSON or None."""
    url = f"{_MODRINTH_API}{path}"
    try:
        async with session.get(url, params=params, headers={"User-Agent": _USER_AGENT}) as resp:
            if resp.status == 200:
                return await resp.json()
            log.debug("Modrinth API %s returned HTTP %d", path, resp.status)
    except aiohttp.ClientError as exc:
        log.warning("Modrinth API request failed: %s", exc)
    return None


async def _get_project(session: aiohttp.ClientSession, project_id: str):
    return await _api_get(session, f"/project/{project_id}")


async def _get_versions(
    session: aiohttp.ClientSession,
    project_id: str,
    loaders: list[str] | None = None,
    game_versions: list[str] | None = None,
) -> list | None:
    import json
    params = {}
    if loaders:
        params["loaders"] = json.dumps(loaders)
    if game_versions:
        params["game_versions"] = json.dumps(game_versions)
    return await _api_get(session, f"/project/{project_id}/version", params)


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------

def _parse_channel(arg: str) -> int | None:
    """Extract a channel ID from a <#123> mention or raw integer string."""
    m = re.fullmatch(r"<#(\d+)>", arg) or re.fullmatch(r"(\d+)", arg)
    return int(m.group(1)) if m else None


def _parse_role(arg: str) -> int | None:
    """Extract a role ID from a <@&123> mention or raw integer string."""
    m = re.fullmatch(r"<@&(\d+)>", arg) or re.fullmatch(r"(\d+)", arg)
    return int(m.group(1)) if m else None


def _parse_add_opts(args: list[str]) -> tuple[list[int], list[str], str | None]:
    """
    Parse optional arguments shared by !track add and !track bulk:
        [@role...] [--mc ver...] [--loader loader]

    Returns (roles, mc_versions, loader).
    """
    roles: list[int] = []
    mc_versions: list[str] = []
    loader: str | None = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--mc":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                mc_versions.append(args[i])
                i += 1
        elif arg == "--loader":
            i += 1
            if i < len(args):
                loader = args[i].lower()
                i += 1
        else:
            role_id = _parse_role(arg)
            if role_id:
                roles.append(role_id)
            i += 1

    return roles, mc_versions, loader


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------

def _build_update_embed(project: dict, version: dict) -> fluxer.Embed:
    slug = project.get("slug") or project.get("id", "")
    url = f"https://modrinth.com/mod/{slug}/version/{version['id']}"

    loaders_str = ", ".join(version.get("loaders") or []) or "—"

    mc = version.get("game_versions") or []
    if len(mc) > 10:
        mc_str = ", ".join(mc[:10]) + f" (+{len(mc) - 10} more)"
    else:
        mc_str = ", ".join(mc) or "—"

    # Trim changelog to fit embed limits
    changelog = (version.get("changelog") or "").strip()
    if len(changelog) > 900:
        changelog = changelog[:900] + f"…\n\n[View full changelog]({url})"

    version_type = version.get("version_type", "release")
    type_display = version_type.capitalize()

    embed = (
        fluxer.Embed(
            title=f"🆕 {project.get('title', slug)} — {version.get('version_number', '')}",
            color=_COLOR_UPDATE,
        )
        .add_field(
            name="Version Name",
            value=version.get("name") or version.get("version_number", "—"),
            inline=True,
        )
        .add_field(name="Release Type", value=type_display, inline=True)
        .add_field(name="Loaders", value=loaders_str, inline=True)
        .add_field(name="Minecraft Versions", value=mc_str, inline=False)
    )

    if changelog:
        embed.add_field(name="Changelog", value=changelog, inline=False)

    footer_parts = [f"Project ID: {project.get('id', '')}"]
    published = version.get("date_published", "")
    if published:
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            footer_parts.append(dt.strftime("Published %d %b %Y at %H:%M UTC"))
        except ValueError:
            pass
    embed.set_footer(text=" • ".join(footer_parts))

    icon = project.get("icon_url")
    if icon:
        embed.set_thumbnail(url=icon)

    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ModrinthCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    @fluxer.Cog.listener()
    async def on_ready(self) -> None:
        """Start the background polling loop once the bot is connected."""
        if self._task and not self._task.done():
            return  # Already running
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._poll_loop())
        log.info(
            "Modrinth update checker started (interval: %ds)",
            getattr(config, "MODRINTH_CHECK_INTERVAL", 300),
        )

    async def cog_unload(self) -> None:
        """Cancel the background task and close the HTTP session on unload."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        log.info("Modrinth update checker stopped.")

    # ── Background polling loop ────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Periodically check every tracked mod across all guilds."""
        while True:
            interval = max(
                _MIN_INTERVAL,
                int(getattr(config, "MODRINTH_CHECK_INTERVAL", 300)),
            )
            await asyncio.sleep(interval)
            try:
                await self._check_all_guilds()
            except Exception as exc:
                log.exception("Unhandled error in Modrinth poll loop: %s", exc)

    async def _check_all_guilds(self) -> None:
        """Run one full update check pass across every guild the bot is in."""
        for guild in self.bot.guilds:
            try:
                await self._check_guild(guild.id)
            except Exception as exc:
                log.warning("Error checking guild %d: %s", guild.id, exc)

    async def _check_guild(self, guild_id: int) -> None:
        """Check all tracked mods for a single guild and post any new versions."""
        data = self.settings.get(guild_id, "modrinth") or {}
        tracked: dict = data.get("tracked", {})
        if not tracked:
            return

        default_loader: str | None = data.get("default_loader")

        for project_id, entry in list(tracked.items()):
            try:
                updated = await self._check_project(guild_id, project_id, entry, default_loader)
                if updated:
                    # Persist the new last_version_id
                    self.settings.set(guild_id, "modrinth", data)
            except Exception as exc:
                log.warning("Error checking project %s in guild %d: %s", project_id, guild_id, exc)

            # Be polite to the Modrinth API
            await asyncio.sleep(1)

    async def _check_project(
        self,
        guild_id: int,
        project_id: str,
        entry: dict,
        default_loader: str | None,
    ) -> bool:
        """
        Check one project for a new version.
        Posts to the configured channel if a new version is found.
        Returns True if a new version was found (and last_version_id was updated).
        """
        loader = entry.get("loader") or default_loader
        versions = await _get_versions(
            self._session,
            project_id,
            loaders=[loader] if loader else None,
            game_versions=entry.get("mc_versions") or None,
        )

        if not versions:
            return False

        latest = next((v for v in versions if v.get("status") == "listed"), versions[0])

        if latest["id"] == entry.get("last_version_id"):
            return False

        # New version found — fetch full project info then post
        project = await _get_project(self._session, project_id)
        if not project:
            return False

        # Update stored state
        entry["last_version_id"] = latest["id"]
        entry["project_name"] = project.get("title", project_id)

        # Post the notification
        channel = self.bot._channels.get(entry["channel_id"])
        if channel is None:
            log.warning(
                "Notification channel %d not found for project %s in guild %d",
                entry["channel_id"], project_id, guild_id,
            )
            return True  # Still mark as seen so we don't spam on reconnect

        embed = _build_update_embed(project, latest)
        role_mentions = " ".join(f"<@&{rid}>" for rid in entry.get("roles") or [])

        await channel.send(
            content=role_mentions or None,
            embed=embed,
        )
        log.info(
            "Posted update for %s (%s) in guild %d",
            project.get("title", project_id), latest["id"], guild_id,
        )
        return True

    # =========================================================================
    # Permission helpers
    # =========================================================================

    def _has_manage_guild(self, member) -> bool:
        if not member:
            return False
        perms = getattr(member, "permissions", None)
        if perms is None:
            return False
        return getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False)

    def _is_owner(self, user_id: int) -> bool:
        owner_id = getattr(config, "OWNER_ID", None)
        if not owner_id:
            return False
        return str(user_id) == str(owner_id)

    # =========================================================================
    # Command dispatcher
    # =========================================================================

    @fluxer.Cog.command(name="track")
    async def track(self, ctx: fluxer.Message, *, args: str = "") -> None:
        """Dispatcher for all !track sub-commands."""
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        parts = args.strip().split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""

        # Help is public
        if not sub or sub == "help":
            await ctx.reply(embed=self._help_embed())
            return

        # Owner-only gate
        if sub == "interval":
            if not self._is_owner(ctx.author.id):
                await ctx.reply("This command is bot-owner only.")
                return
            await self._cmd_interval(ctx, rest.split())
            return

        # Everything else requires Manage Guild
        guild = self.bot._guilds.get(ctx.guild_id)
        member = None
        if guild:
            try:
                member = await guild.fetch_member(ctx.author.id)
            except Exception:
                pass

        if not self._has_manage_guild(member):
            await ctx.reply("You need the **Manage Guild** permission to use this command.")
            return

        # Dispatch to sub-command handlers
        dispatch = {
            "add":     self._cmd_add,
            "bulk":    self._cmd_bulk,
            "remove":  self._cmd_remove,
            "rm":      self._cmd_remove,
            "delete":  self._cmd_remove,
            "list":    self._cmd_list,
            "check":   self._cmd_check,
            "set":     self._cmd_set,
            "default": self._cmd_default,
        }

        handler = dispatch.get(sub)
        if handler is None:
            await ctx.reply(embed=self._help_embed())
            return

        await handler(ctx, rest.split())

    # =========================================================================
    # Sub-command handlers
    # =========================================================================

    async def _cmd_add(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track add <id> <#channel> [@role...] [--mc ver] [--loader loader]"""
        if len(args) < 2:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}track add <id> <#channel> [@role...] "
                f"[--mc 1.21.4] [--loader fabric]`"
            )
            return

        project_id = args[0]
        channel_id = _parse_channel(args[1])
        if not channel_id:
            await ctx.reply("Please mention a valid channel: `#channel-name`")
            return

        roles, mc_versions, loader = _parse_add_opts(args[2:])

        if loader and loader not in _VALID_LOADERS:
            await ctx.reply(
                f"`{loader}` is not a valid loader. "
                f"Valid options: {', '.join(sorted(_VALID_LOADERS))}"
            )
            return

        await ctx.reply(f"🔍 Looking up `{project_id}` on Modrinth…")

        project = await _get_project(self._session, project_id)
        if not project:
            await ctx.reply(f"Could not find a Modrinth project with ID/slug `{project_id}`.")
            return

        # Resolve the effective loader to record the current latest version
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        default_loader = data.get("default_loader")
        effective_loader = loader or default_loader

        versions = await _get_versions(
            self._session,
            project["id"],
            loaders=[effective_loader] if effective_loader else None,
            game_versions=mc_versions or None,
        )
        latest = next((v for v in (versions or []) if v.get("status") == "listed"), (versions or [None])[0])

        entry = {
            "channel_id":      channel_id,
            "roles":           roles,
            "mc_versions":     mc_versions,
            "loader":          loader,
            "last_version_id": latest["id"] if latest else None,
            "project_name":    project.get("title", project_id),
        }

        if "tracked" not in data:
            data["tracked"] = {}
        data["tracked"][project["id"]] = entry
        self.settings.set(ctx.guild_id, "modrinth", data)

        log.info(
            "Now tracking %s (%s) in guild %d → channel %d",
            project.get("title"), project["id"], ctx.guild_id, channel_id,
        )

        role_str = " ".join(f"<@&{r}>" for r in roles) or "None"
        embed = (
            fluxer.Embed(
                title=f"✅ Now tracking: {project.get('title', project_id)}",
                color=_COLOR_UPDATE,
            )
            .add_field(name="Channel", value=f"<#{channel_id}>", inline=True)
            .add_field(name="Loader Filter", value=loader or effective_loader or "Any", inline=True)
            .add_field(name="MC Versions", value=", ".join(mc_versions) if mc_versions else "Any", inline=True)
            .add_field(name="Ping Roles", value=role_str, inline=False)
            .set_footer(text=f"Project ID: {project['id']} • Current version recorded as baseline")
        )
        if project.get("icon_url"):
            embed.set_thumbnail(url=project["icon_url"])

        await ctx.reply(embed=embed)

    async def _cmd_bulk(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track bulk <#channel> [@role...] [--loader x] [--mc x] -- <id1> <id2> ..."""
        full = " ".join(args)
        sep = " -- "
        if sep not in full:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}track bulk <#channel> [@role...] "
                f"[--loader fabric] [--mc 1.21.4] -- <id1> <id2> …`\n"
                f"Separate options from IDs with ` -- ` (space dash dash space)."
            )
            return

        opts_part, ids_part = full.split(sep, 1)
        opts_args = opts_part.strip().split()
        project_ids = ids_part.strip().split()

        if not project_ids:
            await ctx.reply("No project IDs provided after ` -- `.")
            return

        if not opts_args:
            await ctx.reply("Please provide a channel as the first argument.")
            return

        channel_id = _parse_channel(opts_args[0])
        if not channel_id:
            await ctx.reply("Please mention a valid channel as the first argument.")
            return

        roles, mc_versions, loader = _parse_add_opts(opts_args[1:])

        if loader and loader not in _VALID_LOADERS:
            await ctx.reply(
                f"`{loader}` is not a valid loader. "
                f"Valid options: {', '.join(sorted(_VALID_LOADERS))}"
            )
            return

        await ctx.reply(f"🔍 Adding **{len(project_ids)}** mod(s) — this may take a moment…")

        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        default_loader = data.get("default_loader")
        effective_loader = loader or default_loader

        if "tracked" not in data:
            data["tracked"] = {}

        added: list[str] = []
        failed: list[str] = []

        for pid in project_ids:
            try:
                project = await _get_project(self._session, pid)
                if not project:
                    failed.append(f"`{pid}` — not found on Modrinth")
                    continue

                versions = await _get_versions(
                    self._session,
                    project["id"],
                    loaders=[effective_loader] if effective_loader else None,
                    game_versions=mc_versions or None,
                )
                latest = next(
                    (v for v in (versions or []) if v.get("status") == "listed"),
                    (versions or [None])[0],
                )

                data["tracked"][project["id"]] = {
                    "channel_id":      channel_id,
                    "roles":           roles,
                    "mc_versions":     mc_versions,
                    "loader":          loader,
                    "last_version_id": latest["id"] if latest else None,
                    "project_name":    project.get("title", pid),
                }
                added.append(project.get("title", pid))
            except Exception as exc:
                failed.append(f"`{pid}` — {exc}")

            await asyncio.sleep(0.6)  # Polite pacing

        self.settings.set(ctx.guild_id, "modrinth", data)

        loader_str = loader or effective_loader or "Any"
        mc_str = ", ".join(mc_versions) if mc_versions else "Any"
        role_str = " ".join(f"<@&{r}>" for r in roles) or "None"

        lines = [
            f"✅ Added **{len(added)}** of **{len(project_ids)}** mod(s) to <#{channel_id}>",
            f"Loader: `{loader_str}` · MC: `{mc_str}` · Roles: {role_str}",
        ]
        if added:
            lines.append("\n" + "\n".join(f"  ✓ {n}" for n in added))
        if failed:
            lines.append(f"\n❌ **{len(failed)}** failed:\n" + "\n".join(failed))

        await ctx.reply("\n".join(lines))

    async def _cmd_remove(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track remove <id/slug/name>"""
        if not args:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track remove <project_id>`")
            return

        query = args[0].lower()
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        tracked: dict = data.get("tracked", {})

        # Match by project ID or stored project name
        match_key = None
        for key, entry in tracked.items():
            if key.lower() == query or entry.get("project_name", "").lower() == query:
                match_key = key
                break

        if not match_key:
            await ctx.reply(
                f"No tracked mod matching `{args[0]}`.\n"
                f"Use `{config.COMMAND_PREFIX}track list` to see what's being tracked."
            )
            return

        name = tracked[match_key].get("project_name", match_key)
        del tracked[match_key]
        data["tracked"] = tracked
        self.settings.set(ctx.guild_id, "modrinth", data)

        log.info("Stopped tracking %s in guild %d", name, ctx.guild_id)
        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Stopped tracking **{name}**.",
                color=_COLOR_INFO,
            )
        )

    async def _cmd_list(self, ctx: fluxer.Message, args: list[str] = None) -> None:
        """!track list — show all tracked mods in this server."""
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        tracked: dict = data.get("tracked", {})

        if not tracked:
            await ctx.reply(
                f"No mods are being tracked yet.\n"
                f"Use `{config.COMMAND_PREFIX}track add` to start tracking."
            )
            return

        # Group by channel for a tidy layout
        by_channel: dict[int, list[tuple]] = {}
        for pid, entry in tracked.items():
            cid = entry.get("channel_id")
            by_channel.setdefault(cid, []).append((pid, entry))

        embed = fluxer.Embed(
            title=f"Tracked Mods — {len(tracked)} total",
            color=_COLOR_INFO,
        )

        for channel_id, entries in sorted(by_channel.items(), key=lambda x: x[0]):
            lines = []
            for pid, entry in sorted(entries, key=lambda x: x[1].get("project_name", "").lower()):
                loader = entry.get("loader") or "—"
                mc = ", ".join(entry.get("mc_versions") or []) or "Any"
                roles = " ".join(f"<@&{r}>" for r in entry.get("roles") or []) or "None"
                lines.append(
                    f"**{entry.get('project_name', pid)}** (`{pid}`)\n"
                    f"  Loader: `{loader}` · MC: `{mc}` · Roles: {roles}"
                )
            embed.add_field(
                name=f"#{self._channel_name(channel_id)} ({len(entries)} mod{'s' if len(entries) != 1 else ''})",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        default_loader = data.get("default_loader")
        if default_loader:
            embed.set_footer(text=f"Server default loader: {default_loader}")

        await ctx.reply(embed=embed)

    async def _cmd_check(self, ctx: fluxer.Message, args: list[str] = None) -> None:
        """!track check — manually trigger an update check for this guild."""
        await ctx.reply("🔍 Running manual update check…")
        try:
            await self._check_guild(ctx.guild_id)
            await ctx.reply(
                embed=fluxer.Embed(
                    description="Manual check complete. Any new versions have been posted.",
                    color=_COLOR_INFO,
                )
            )
        except Exception as exc:
            log.exception("Manual check failed in guild %d: %s", ctx.guild_id, exc)
            await ctx.reply(f"Check failed: {exc}")

    async def _cmd_interval(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track interval <seconds> — bot owner only."""
        if not args or not args[0].isdigit():
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track interval <seconds>` (minimum {_MIN_INTERVAL})")
            return

        seconds = int(args[0])
        if seconds < _MIN_INTERVAL:
            await ctx.reply(f"Interval must be at least **{_MIN_INTERVAL}** seconds.")
            return

        # Store globally — we look this up each loop iteration
        config.MODRINTH_CHECK_INTERVAL = seconds
        await ctx.reply(
            embed=fluxer.Embed(
                description=f"Check interval updated to **{seconds}s**. Takes effect from the next tick.",
                color=_COLOR_INFO,
            )
        )

    async def _cmd_default(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track default loader [loader] — set/clear the server-wide default loader."""
        if not args or args[0].lower() != "loader":
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track default loader [loader]`")
            return

        loader = args[1].lower() if len(args) > 1 else None

        if loader and loader not in _VALID_LOADERS:
            await ctx.reply(
                f"`{loader}` is not a valid loader. "
                f"Valid options: {', '.join(sorted(_VALID_LOADERS))}"
            )
            return

        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        data["default_loader"] = loader
        self.settings.set(ctx.guild_id, "modrinth", data)

        msg = (
            f"Server default loader set to `{loader}`."
            if loader
            else "Server default loader cleared."
        )
        await ctx.reply(embed=fluxer.Embed(description=msg, color=_COLOR_INFO))

    # ── !track set <key> ... ──────────────────────────────────────────────

    async def _cmd_set(self, ctx: fluxer.Message, args: list[str]) -> None:
        """Dispatcher for all !track set sub-commands."""
        if not args:
            await ctx.reply(embed=self._help_embed())
            return

        key = args[0].lower()
        rest = args[1:]

        set_dispatch = {
            "channel":         self._set_channel,
            "mc":              self._set_mc,
            "loader":          self._set_loader,
            "roles":           self._set_roles,
            "mc-all":          self._set_mc_all,
            "loader-all":      self._set_loader_all,
            "mc-channel":      self._set_mc_channel,
            "loader-channel":  self._set_loader_channel,
            "roles-channel":   self._set_roles_channel,
            "channel-channel": self._set_channel_channel,
        }

        handler = set_dispatch.get(key)
        if handler is None:
            await ctx.reply(embed=self._help_embed())
            return

        await handler(ctx, rest)

    async def _set_channel(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set channel <project_id> <#channel>"""
        if len(args) < 2:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track set channel <id> <#channel>`")
            return
        pid, channel_arg = args[0], args[1]
        channel_id = _parse_channel(channel_arg)
        if not channel_id:
            await ctx.reply("Please mention a valid channel.")
            return
        data, entry = self._get_entry(ctx.guild_id, pid)
        if entry is None:
            await ctx.reply(f"`{pid}` is not being tracked.")
            return
        entry["channel_id"] = channel_id
        self.settings.set(ctx.guild_id, "modrinth", data)
        await ctx.reply(embed=fluxer.Embed(description=f"Notifications for `{pid}` will now go to <#{channel_id}>.", color=_COLOR_INFO))

    async def _set_mc(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set mc <project_id> [versions...]"""
        if not args:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track set mc <id> [versions...]`")
            return
        pid, versions = args[0], args[1:]
        data, entry = self._get_entry(ctx.guild_id, pid)
        if entry is None:
            await ctx.reply(f"`{pid}` is not being tracked.")
            return
        entry["mc_versions"] = versions
        self.settings.set(ctx.guild_id, "modrinth", data)
        msg = f"MC filter for `{pid}`: `{', '.join(versions)}`" if versions else f"MC filter for `{pid}` cleared."
        await ctx.reply(embed=fluxer.Embed(description=msg, color=_COLOR_INFO))

    async def _set_loader(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set loader <project_id> [loader]"""
        if not args:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track set loader <id> [loader]`")
            return
        pid = args[0]
        loader = args[1].lower() if len(args) > 1 else None
        if loader and loader not in _VALID_LOADERS:
            await ctx.reply(f"`{loader}` is not a valid loader.")
            return
        data, entry = self._get_entry(ctx.guild_id, pid)
        if entry is None:
            await ctx.reply(f"`{pid}` is not being tracked.")
            return
        entry["loader"] = loader
        self.settings.set(ctx.guild_id, "modrinth", data)
        msg = f"Loader filter for `{pid}` set to `{loader}`." if loader else f"Loader filter for `{pid}` cleared."
        await ctx.reply(embed=fluxer.Embed(description=msg, color=_COLOR_INFO))

    async def _set_roles(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set roles <project_id> [@role...]"""
        if not args:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track set roles <id> [@role...]`")
            return
        pid = args[0]
        roles = [r for a in args[1:] if (r := _parse_role(a))]
        data, entry = self._get_entry(ctx.guild_id, pid)
        if entry is None:
            await ctx.reply(f"`{pid}` is not being tracked.")
            return
        entry["roles"] = roles
        self.settings.set(ctx.guild_id, "modrinth", data)
        role_str = " ".join(f"<@&{r}>" for r in roles) or "None"
        await ctx.reply(embed=fluxer.Embed(description=f"Ping roles for `{pid}`: {role_str}", color=_COLOR_INFO))

    async def _set_mc_all(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set mc-all [versions...]"""
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        tracked = data.get("tracked", {})
        if not tracked:
            await ctx.reply("No mods are being tracked.")
            return
        for entry in tracked.values():
            entry["mc_versions"] = args
        self.settings.set(ctx.guild_id, "modrinth", data)
        msg = (
            f"MC filter set to `{', '.join(args)}` for all {len(tracked)} mod(s)."
            if args else f"MC filter cleared for all {len(tracked)} mod(s)."
        )
        await ctx.reply(embed=fluxer.Embed(description=msg, color=_COLOR_INFO))

    async def _set_loader_all(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set loader-all [loader]"""
        loader = args[0].lower() if args else None
        if loader and loader not in _VALID_LOADERS:
            await ctx.reply(f"`{loader}` is not a valid loader.")
            return
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        tracked = data.get("tracked", {})
        if not tracked:
            await ctx.reply("No mods are being tracked.")
            return
        for entry in tracked.values():
            entry["loader"] = loader
        self.settings.set(ctx.guild_id, "modrinth", data)
        msg = (
            f"Loader set to `{loader}` for all {len(tracked)} mod(s)."
            if loader else f"Loader filter cleared for all {len(tracked)} mod(s)."
        )
        await ctx.reply(embed=fluxer.Embed(description=msg, color=_COLOR_INFO))

    async def _set_mc_channel(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set mc-channel <#channel> [versions...]"""
        if not args:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track set mc-channel <#channel> [versions...]`")
            return
        channel_id = _parse_channel(args[0])
        if not channel_id:
            await ctx.reply("Please mention a valid channel.")
            return
        versions = args[1:]
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        affected = [e for e in data.get("tracked", {}).values() if e.get("channel_id") == channel_id]
        if not affected:
            await ctx.reply(f"No mods are posting to <#{channel_id}>.")
            return
        for entry in affected:
            entry["mc_versions"] = versions
        self.settings.set(ctx.guild_id, "modrinth", data)
        msg = (
            f"MC filter set to `{', '.join(versions)}` for {len(affected)} mod(s) in <#{channel_id}>."
            if versions else f"MC filter cleared for {len(affected)} mod(s) in <#{channel_id}>."
        )
        await ctx.reply(embed=fluxer.Embed(description=msg, color=_COLOR_INFO))

    async def _set_loader_channel(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set loader-channel <#channel> [loader]"""
        if not args:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track set loader-channel <#channel> [loader]`")
            return
        channel_id = _parse_channel(args[0])
        if not channel_id:
            await ctx.reply("Please mention a valid channel.")
            return
        loader = args[1].lower() if len(args) > 1 else None
        if loader and loader not in _VALID_LOADERS:
            await ctx.reply(f"`{loader}` is not a valid loader.")
            return
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        affected = [e for e in data.get("tracked", {}).values() if e.get("channel_id") == channel_id]
        if not affected:
            await ctx.reply(f"No mods are posting to <#{channel_id}>.")
            return
        for entry in affected:
            entry["loader"] = loader
        self.settings.set(ctx.guild_id, "modrinth", data)
        msg = (
            f"Loader set to `{loader}` for {len(affected)} mod(s) in <#{channel_id}>."
            if loader else f"Loader filter cleared for {len(affected)} mod(s) in <#{channel_id}>."
        )
        await ctx.reply(embed=fluxer.Embed(description=msg, color=_COLOR_INFO))

    async def _set_roles_channel(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set roles-channel <#channel> [@role...]"""
        if not args:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track set roles-channel <#channel> [@role...]`")
            return
        channel_id = _parse_channel(args[0])
        if not channel_id:
            await ctx.reply("Please mention a valid channel.")
            return
        roles = [r for a in args[1:] if (r := _parse_role(a))]
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        affected = [e for e in data.get("tracked", {}).values() if e.get("channel_id") == channel_id]
        if not affected:
            await ctx.reply(f"No mods are posting to <#{channel_id}>.")
            return
        for entry in affected:
            entry["roles"] = roles
        self.settings.set(ctx.guild_id, "modrinth", data)
        role_str = " ".join(f"<@&{r}>" for r in roles) or "None"
        await ctx.reply(embed=fluxer.Embed(
            description=f"Ping roles for {len(affected)} mod(s) in <#{channel_id}> set to: {role_str}",
            color=_COLOR_INFO,
        ))

    async def _set_channel_channel(self, ctx: fluxer.Message, args: list[str]) -> None:
        """!track set channel-channel <#old> <#new> — move all mods from one channel to another."""
        if len(args) < 2:
            await ctx.reply(f"Usage: `{config.COMMAND_PREFIX}track set channel-channel <#old> <#new>`")
            return
        old_id = _parse_channel(args[0])
        new_id = _parse_channel(args[1])
        if not old_id:
            await ctx.reply("Could not parse the old channel. Please use a #mention.")
            return
        if not new_id:
            await ctx.reply("Could not parse the new channel. Please use a #mention.")
            return
        data = self.settings.get(ctx.guild_id, "modrinth") or {}
        affected = [e for e in data.get("tracked", {}).values() if e.get("channel_id") == old_id]
        if not affected:
            await ctx.reply(f"No mods are posting to <#{old_id}>.")
            return
        for entry in affected:
            entry["channel_id"] = new_id
        self.settings.set(ctx.guild_id, "modrinth", data)
        await ctx.reply(embed=fluxer.Embed(
            description=f"Moved **{len(affected)}** mod(s) from <#{old_id}> to <#{new_id}>.",
            color=_COLOR_INFO,
        ))

    # =========================================================================
    # Helpers
    # =========================================================================

    def _get_entry(self, guild_id: int, pid: str) -> tuple[dict, dict | None]:
        """Return (full modrinth data dict, entry dict or None) for a project ID."""
        data = self.settings.get(guild_id, "modrinth") or {}
        entry = data.get("tracked", {}).get(pid)
        if entry is None:
            # Also try matching by stored project name
            for key, e in data.get("tracked", {}).items():
                if e.get("project_name", "").lower() == pid.lower():
                    entry = e
                    break
        return data, entry

    def _channel_name(self, channel_id: int) -> str:
        channel = self.bot._channels.get(channel_id)
        return channel.name if channel else str(channel_id)

    def _help_embed(self) -> fluxer.Embed:
        p = config.COMMAND_PREFIX
        return fluxer.Embed(
            title="📦 Modrinth Update Tracker — Help",
            color=_COLOR_INFO,
            description=(
                f"**Tracking**\n"
                f"`{p}track add <id> <#ch> [@role...] [--mc ver] [--loader x]`\n"
                f"`{p}track bulk <#ch> [@role...] [--loader x] [--mc ver] -- <id1> <id2> …`\n"
                f"`{p}track remove <id>` · `{p}track list` · `{p}track check`\n\n"

                f"**Per-project settings**\n"
                f"`{p}track set channel <id> <#ch>`\n"
                f"`{p}track set mc <id> [versions…]`\n"
                f"`{p}track set loader <id> [loader]`\n"
                f"`{p}track set roles <id> [@role…]`\n\n"

                f"**Bulk settings**\n"
                f"`{p}track set mc-all [versions…]`\n"
                f"`{p}track set loader-all [loader]`\n"
                f"`{p}track set mc-channel <#ch> [versions…]`\n"
                f"`{p}track set loader-channel <#ch> [loader]`\n"
                f"`{p}track set roles-channel <#ch> [@role…]`\n"
                f"`{p}track set channel-channel <#old> <#new>`\n\n"

                f"**Server defaults**\n"
                f"`{p}track default loader [loader]`\n\n"

                f"**Bot owner only**\n"
                f"`{p}track interval <seconds>` — min {_MIN_INTERVAL}s\n\n"

                f"Valid loaders: {', '.join(sorted(_VALID_LOADERS))}\n"
                f"Use a Modrinth slug (e.g. `sodium`) or full project ID.\n"
                f"All commands require **Manage Guild** permission."
            ),
        )


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(ModrinthCog(bot))