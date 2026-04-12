"""
utils/discord_shim.py
---------------------
Compatibility shim: installs a fake ``fluxer`` module into ``sys.modules``
so that every cog written against the Fluxer API runs transparently on
discord.py — without any changes to the cog files themselves.

MUST be the very first non-stdlib import in run_discord.py.

Covers
------
fluxer.Cog / .command() / .listener()   → commands.Cog + context wrapping
fluxer.Bot                               → DiscordBot (commands.Bot subclass)
fluxer.Message                           → _MessageWrapper (type-hint + runtime)
fluxer.Embed                             → discord.Embed  (identical API)
fluxer.Channel / Guild / GuildMember / Role  → proxy classes with from_data()
fluxer.Permissions.*                     → _Permission descriptors
fluxer.Intents                           → discord.Intents
fluxer.has_permission / checks.has_permission
fluxer.RawReactionActionEvent            → discord.RawReactionActionEvent
bot._channels / ._guilds / ._http        → proxy attributes on DiscordBot
"""

import sys
import types
import discord
from discord.ext import commands
from functools import wraps


# ─── Event argument transformation ───────────────────────────────────────────

def _transform_event(event_name: str, args: tuple) -> tuple:
    """
    Convert discord.py listener arguments so they match what fluxer cogs expect.

    Fluxer listeners receive:
        on_member_join  → raw dict  (we pass the discord.Member; GuildMember.from_data handles both)
        on_message      → fluxer.Message-like object
        on_raw_reaction → already compatible (discord.RawReactionActionEvent)
    """
    if not args:
        return args

    if event_name == "on_member_join":
        # discord.py passes discord.Member; fluxer passes a raw dict.
        # Our GuildMember.from_data() dispatches on isinstance(data, discord.Member).
        return args  # pass-through; from_data() handles it

    if event_name in ("on_message", "on_message_delete"):
        return (_MessageWrapper(args[0]),) + args[1:]

    if event_name == "on_message_edit":
        # before, after
        return tuple(_MessageWrapper(a) for a in args[:2]) + args[2:]

    # on_raw_reaction_add / remove: discord.RawReactionActionEvent already
    # has message_id, emoji, user_id, guild_id, channel_id — no wrapping needed.
    return args


# ─── Author / user wrapper ────────────────────────────────────────────────────

class _AuthorWrapper:
    """
    Wraps discord.Member or discord.User to expose the attribute names that
    the cog code expects from a fluxer user/member object.
    """

    __slots__ = ("_m",)

    def __init__(self, member):
        self._m = member

    @property
    def id(self) -> int:
        return self._m.id

    @property
    def username(self) -> str:
        # fluxer → .username;  discord.py → .name
        return getattr(self._m, "name", "") or ""

    @property
    def display_name(self) -> str:
        return self._m.display_name

    @property
    def mention(self) -> str:
        return self._m.mention

    @property
    def permissions(self):
        """
        fluxer exposes .permissions on the member; discord.py calls it
        .guild_permissions.  Both are discord.Permissions objects with
        identical boolean attributes (manage_roles, administrator, …).
        """
        return getattr(self._m, "guild_permissions", None)

    def __getattr__(self, name):
        return getattr(self._m, name)

    def __repr__(self):
        return f"<_AuthorWrapper id={self._m.id} name={self.username!r}>"


# ─── Message / Context wrapper ────────────────────────────────────────────────

class _MessageWrapper:
    """
    Wraps discord.ext.commands.Context (from command handlers) or
    discord.Message (from listeners) so that cog code sees the same
    attribute shape it uses on fluxer.Message.
    """

    def __init__(self, obj):
        if isinstance(obj, commands.Context):
            self._msg = obj.message
            self._ctx: commands.Context | None = obj
        else:
            self._msg: discord.Message = obj
            self._ctx = None

    # ── fluxer.Message attributes ─────────────────────────────────────────────
    @property
    def guild_id(self):
        g = self._msg.guild
        return g.id if g else None

    @property
    def channel_id(self) -> int:
        return self._msg.channel.id

    @property
    def id(self) -> int:
        return self._msg.id

    @property
    def content(self) -> str:
        return self._msg.content

    @property
    def author(self) -> _AuthorWrapper:
        return _AuthorWrapper(self._msg.author)

    @property
    def channel(self):
        return self._msg.channel

    async def reply(self, content=None, embed=None, **kwargs):
        if self._ctx is not None:
            return await self._ctx.reply(content=content, embed=embed, **kwargs)
        return await self._msg.reply(content=content, embed=embed, **kwargs)

    def __getattr__(self, name):
        return getattr(self._msg, name)

    def __repr__(self):
        return f"<_MessageWrapper id={self._msg.id}>"


# ─── Channel proxy ────────────────────────────────────────────────────────────

class _ChannelProxy:
    """
    Wraps a live discord.abc.GuildChannel or a raw REST/gateway dict.
    Exposes .id / .name / .guild_id / .is_text_channel / .send().
    """

    def __init__(self, channel_or_data, http_wrapper=None):
        is_dict = isinstance(channel_or_data, dict)
        self._ch   = None if is_dict else channel_or_data
        self._data = channel_or_data if is_dict else None
        self._http_wrapper = http_wrapper

        if self._ch is not None:
            self.id           = self._ch.id
            self.name         = self._ch.name
            guild             = getattr(self._ch, "guild", None)
            self.guild_id     = guild.id if guild else None
            self.is_text_channel = isinstance(self._ch, discord.TextChannel)
        else:
            self.id           = int(self._data.get("id", 0))
            self.name         = self._data.get("name", "")
            gid               = self._data.get("guild_id")
            self.guild_id     = int(gid) if gid else None
            # type 0 = GUILD_TEXT
            self.is_text_channel = self._data.get("type", 0) == 0

    async def send(self, content=None, embed=None, **kwargs):
        if self._ch is not None:
            return await self._ch.send(content=content, embed=embed, **kwargs)
        if self._http_wrapper is not None:
            return await self._http_wrapper.send_message(
                self.id, content=content, embed=embed
            )
        raise RuntimeError(
            "_ChannelProxy (from dict) has no live channel or http wrapper"
        )

    @classmethod
    def from_data(cls, data: dict, http_wrapper) -> "_ChannelProxy":
        return cls(data, http_wrapper)

    def __getattr__(self, name):
        if self._ch is not None:
            return getattr(self._ch, name)
        raise AttributeError(
            f"_ChannelProxy (dict-backed, no live channel) has no attribute {name!r}"
        )

    def __repr__(self):
        return f"<_ChannelProxy id={self.id} name={self.name!r}>"


# ─── Guild proxy ──────────────────────────────────────────────────────────────

class _GuildProxy:
    """Wraps discord.Guild, forwarding unknown attribute access."""

    def __init__(self, guild: discord.Guild):
        self._guild      = guild
        self.id          = guild.id
        self.name        = guild.name
        self.member_count = getattr(guild, "member_count", None)

    @classmethod
    def from_data(cls, data: dict, http_wrapper) -> "_GuildDataProxy":
        return _GuildDataProxy(data)

    def __getattr__(self, name):
        return getattr(self._guild, name)

    def __repr__(self):
        return f"<_GuildProxy id={self.id} name={self.name!r}>"


class _GuildDataProxy:
    """Guild built from a raw REST dict (e.g. from http.get_guild())."""

    def __init__(self, data: dict):
        self._data       = data
        self.id          = int(data.get("id", 0))
        self.name        = data.get("name", "")
        self.member_count = (
            data.get("approximate_member_count")
            or data.get("member_count")
        )


# ─── GuildMember proxy ────────────────────────────────────────────────────────

class _MemberUser:
    """User sub-object extracted from a discord.Member."""

    def __init__(self, member: discord.Member):
        self.id           = member.id
        self.username     = member.name
        self.display_name = member.display_name
        self.mention      = member.mention

    def __repr__(self):
        return f"<_MemberUser id={self.id} name={self.username!r}>"


class _GuildMemberProxy:
    """Wraps discord.Member to look like fluxer.GuildMember."""

    def __init__(self, member: discord.Member):
        self._member  = member
        self.guild_id = member.guild.id if member.guild else None
        self.user     = _MemberUser(member)

    @classmethod
    def from_data(cls, data, http_wrapper) -> "_GuildMemberProxy | _GuildMemberDictProxy":
        """
        Accept either:
          • discord.Member  (from discord.py on_member_join)
          • raw dict        (gateway/REST data, unlikely in discord mode)
        """
        if isinstance(data, discord.Member):
            return cls(data)
        return _GuildMemberDictProxy(data)

    def __repr__(self):
        return f"<_GuildMemberProxy guild_id={self.guild_id}>"


class _GuildMemberDictProxy:
    """GuildMember from raw gateway/REST dict data."""

    def __init__(self, data: dict):
        user_data     = data.get("user", {})
        gid           = data.get("guild_id")
        self.guild_id = int(gid) if gid else None
        uid           = user_data.get("id", 0)
        self.user     = type("_RawUser", (), {
            "id":           int(uid),
            "username":     user_data.get("username", ""),
            "display_name": (
                user_data.get("global_name") or user_data.get("username", "")
            ),
            "mention":      f"<@{uid}>",
        })()


# ─── Role proxy ───────────────────────────────────────────────────────────────

class _RoleProxy:
    """Wraps discord.Role or a raw API dict."""

    def __init__(self, role: discord.Role | None = None, data: dict | None = None):
        self._role = role
        if role is not None:
            self.id       = role.id
            self.position = role.position
        else:
            self.id       = int(data.get("id", 0))
            self.position = data.get("position", 0)

    @classmethod
    def from_data(cls, data, http_wrapper, guild_id=None) -> "_RoleProxy":
        if isinstance(data, discord.Role):
            return cls(role=data)
        return cls(data=data)

    def __getattr__(self, name):
        if self._role is not None:
            return getattr(self._role, name)
        raise AttributeError(f"_RoleProxy (dict-backed) has no attribute {name!r}")


# ─── HTTP wrapper ─────────────────────────────────────────────────────────────

class _HttpWrapper:
    """
    Translates fluxer HTTP method names → discord.py HTTPClient method names.

    If any method raises AttributeError, the discord.py client version may
    have a slightly different name — check discord.py docs for the release
    you're running.
    """

    def __init__(self, http):
        self._http = http

    # ── Read ──────────────────────────────────────────────────────────────────
    async def get_guild(self, guild_id: int):
        return await self._http.get_guild(guild_id)

    async def get_channel(self, channel_id: int):
        return await self._http.get_channel(channel_id)

    async def get_guild_roles(self, guild_id: int):
        # discord.py HTTPClient: get_roles(guild_id)
        return await self._http.get_roles(guild_id)

    # ── Write ─────────────────────────────────────────────────────────────────
    async def add_guild_member_role(
        self, guild_id: int, user_id: int, role_id: int, reason: str | None = None
    ):
        # discord.py HTTPClient: add_role(guild_id, user_id, role_id)
        return await self._http.add_role(guild_id, user_id, role_id, reason=reason)

    async def remove_guild_member_role(
        self, guild_id: int, user_id: int, role_id: int, reason: str | None = None
    ):
        # discord.py HTTPClient: remove_role(guild_id, user_id, role_id)
        return await self._http.remove_role(guild_id, user_id, role_id, reason=reason)

    async def create_guild_channel(self, guild_id: int, **options):
        # discord.py: create_channel(guild_id, channel_type, *, reason, **options)
        channel_type = options.pop("type", 0)
        reason       = options.pop("reason", None)
        return await self._http.create_channel(
            guild_id, channel_type, reason=reason, **options
        )

    async def send_message(self, channel_id: int, content=None, embed=None):
        payload: dict = {}
        if content is not None:
            payload["content"] = content
        if embed is not None:
            payload["embeds"] = [embed.to_dict()]
        return await self._http.send_message(channel_id, **payload)


# ─── Bot cache proxies ───────────────────────────────────────────────────────

class _ChannelCache:
    """
    Proxy that makes ``bot._channels`` behave like the dict the cogs expect.
    Keys are int channel IDs; values are _ChannelProxy instances.
    """

    def __init__(self, bot: "DiscordBot"):
        self._bot = bot

    def get(self, channel_id: int) -> _ChannelProxy | None:
        ch = self._bot.get_channel(int(channel_id))
        return _ChannelProxy(ch) if ch is not None else None

    def values(self):
        for guild in self._bot.guilds:
            for ch in guild.channels:
                yield _ChannelProxy(ch)


class _GuildCache:
    """
    Proxy that makes ``bot._guilds`` behave like the dict the cogs expect.
    Keys are int guild IDs; values are _GuildProxy instances.
    """

    def __init__(self, bot: "DiscordBot"):
        self._bot = bot

    def get(self, guild_id: int) -> _GuildProxy | None:
        g = self._bot.get_guild(int(guild_id))
        return _GuildProxy(g) if g is not None else None


# ─── Custom Bot ───────────────────────────────────────────────────────────────

class DiscordBot(commands.Bot):
    """
    commands.Bot subclass that exposes the fluxer-style attributes
    ``_channels``, ``_guilds``, and ``_http`` that cogs access via ``self.bot``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__channel_cache: _ChannelCache | None = None
        self.__guild_cache:   _GuildCache   | None = None
        self.__http_wrapper:  _HttpWrapper  | None = None

    @property
    def _channels(self) -> _ChannelCache:
        if self.__channel_cache is None:
            self.__channel_cache = _ChannelCache(self)
        return self.__channel_cache

    @property
    def _guilds(self) -> _GuildCache:
        if self.__guild_cache is None:
            self.__guild_cache = _GuildCache(self)
        return self.__guild_cache

    @property
    def _http(self) -> _HttpWrapper:  # type: ignore[override]
        # discord.py uses self.http internally; _http is free for our use.
        if self.__http_wrapper is None:
            self.__http_wrapper = _HttpWrapper(self.http)
        return self.__http_wrapper

    @property
    def _commands(self) -> dict:
        """Fluxer-style _commands dict — maps command name → Command object."""
        return {name: cmd for name, cmd in self.all_commands.items()}

    async def on_command_error(self, ctx, error) -> None:
        """Suppress CommandNotFound so tag triggers in on_message can handle unknown prefixed words."""
        if isinstance(error, commands.CommandNotFound):
            return
        raise error

    async def load_cogs(self) -> None:
        """Auto-discover and load every cog module in the cogs/ directory."""
        import os, logging
        log = logging.getLogger("discord_bot")
        cogs_dir = os.path.join(os.path.dirname(__file__), "..", "cogs")
        for filename in sorted(os.listdir(cogs_dir)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            module_name = f"cogs.{filename[:-3]}"
            try:
                await self.load_extension(module_name)
                log.info("Loaded cog: %s", module_name)
            except Exception:
                log.exception("Failed to load cog: %s", module_name)


# ─── Permissions system ───────────────────────────────────────────────────────

class _Permission:
    """Carries the discord.py ``has_permissions`` kwarg for one permission."""
    __slots__ = ("kwarg",)

    def __init__(self, kwarg: str):
        self.kwarg = kwarg


class Permissions:
    """Maps fluxer.Permissions.X constants to discord.py kwarg names."""
    MANAGE_GUILD     = _Permission("manage_guild")
    MANAGE_ROLES     = _Permission("manage_roles")
    BAN_MEMBERS      = _Permission("ban_members")
    KICK_MEMBERS     = _Permission("kick_members")
    MANAGE_MESSAGES  = _Permission("manage_messages")
    MANAGE_CHANNELS  = _Permission("manage_channels")
    MODERATE_MEMBERS = _Permission("moderate_members")
    ADMINISTRATOR    = _Permission("administrator")


def has_permission(perm: _Permission):
    """Drop-in for ``@fluxer.has_permission(fluxer.Permissions.X)``."""
    return commands.has_permissions(**{perm.kwarg: True})


class _Checks:
    """Namespace for ``@fluxer.checks.has_permission(...)``."""
    @staticmethod
    def has_permission(perm: _Permission):
        return commands.has_permissions(**{perm.kwarg: True})


# ─── Shim Cog base class ─────────────────────────────────────────────────────

class _ShimCog(commands.Cog):
    """
    Replacement for ``fluxer.Cog``.

    Provides class-level ``command()`` and ``listener()`` that produce
    discord.py-compatible Command objects and listener-marked functions
    while transparently wrapping the command context.
    """

    def __init__(self, bot=None):
        # discord.py Cog.__init__ takes no arguments; store bot ourselves.
        super().__init__()
        if bot is not None:
            self.bot = bot

    @staticmethod
    def command(**kwargs):
        """Replacement for ``@fluxer.Cog.command(name=...)``.

        Wraps the handler so ``ctx`` is always a ``_MessageWrapper``
        regardless of which discord.py context object is passed in.
        """
        def decorator(func):
            @wraps(func)
            async def wrapper(self, ctx, *args, **kw):
                return await func(self, _MessageWrapper(ctx), *args, **kw)
            return commands.command(**kwargs)(wrapper)
        return decorator

    @staticmethod
    def listener():
        """Replacement for ``@fluxer.Cog.listener()``.

        Wraps the handler to transform discord.py event arguments into
        the shapes that fluxer cogs expect, then delegates to
        ``commands.Cog.listener()`` for proper registration.
        """
        def decorator(func):
            event_name = func.__name__

            @wraps(func)
            async def wrapper(self, *args, **kw):
                new_args = _transform_event(event_name, args)
                return await func(self, *new_args, **kw)

            return commands.Cog.listener()(wrapper)
        return decorator


# ─── Assemble and install the fake `fluxer` module ───────────────────────────

_shim = types.ModuleType("fluxer")
_shim.__doc__ = "discord.py compatibility shim — installed by utils/discord_shim.py"

_shim.Cog                    = _ShimCog
_shim.Bot                    = DiscordBot
_shim.Message                = _MessageWrapper          # type-hint target only
_shim.Embed                  = discord.Embed            # identical API to fluxer.Embed
_shim.Channel                = _ChannelProxy
_shim.Guild                  = _GuildProxy
_shim.GuildMember            = _GuildMemberProxy
_shim.Role                   = _RoleProxy
_shim.Permissions            = Permissions
_shim.Intents                = discord.Intents
_shim.has_permission         = has_permission
_shim.checks                 = _Checks()
_shim.RawReactionActionEvent = discord.RawReactionActionEvent

sys.modules["fluxer"] = _shim