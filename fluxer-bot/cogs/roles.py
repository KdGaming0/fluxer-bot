"""
cogs/roles.py
-------------
Reaction-role system. Users click a reaction on a dedicated embed to
get (or remove) a role — no commands needed for end users.

Admin setup commands:
    !reactionrole create <title>              - Post a new reaction-role embed in this channel
    !reactionrole add <message_id> <emoji> <role name>
                                              - Add an emoji→role pair to an existing embed
    !reactionrole remove <message_id> <emoji> - Remove an emoji→role pair from an embed
    !reactionrole delete <message_id>         - Delete the embed and all its mappings
    !reactionrole list                        - List all active reaction-role messages in this server

All setup commands require the caller to have Manage Roles permission.

Storage layout in guild_settings.json:
    {
        "reaction_roles": {
            "<message_id>": {
                "channel_id": 123,
                "title": "Pick your roles",
                "pairs": {
                    "<emoji>": <role_id>,
                    ...
                }
            }
        }
    }
"""

import logging

import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.roles")

_COLOR = 0x5865F2


class RolesCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()

    # =========================================================================
    # Permission helper
    # =========================================================================

    def _has_manage_roles(self, member) -> bool:
        """Return True if the member has Manage Roles permission."""
        if not member:
            return False
        perms = getattr(member, "permissions", None)
        if perms is None:
            return False
        return getattr(perms, "manage_roles", False) or getattr(perms, "administrator", False)

    def _bot_can_assign(self, guild, role) -> bool:
        """Return True if the bot's highest role is above the target role."""
        bot_member = guild.get_member(self.bot.user.id) if guild else None
        if not bot_member:
            return True  # Can't verify; attempt anyway
        bot_top = max(
            (r.position for r in getattr(bot_member, "roles", []) if hasattr(r, "position")),
            default=0,
        )
        return role.position < bot_top

    # =========================================================================
    # Embed builder
    # =========================================================================

    def _build_embed(self, title: str, pairs: dict) -> fluxer.Embed:
        """Build the reaction-role embed from the current pairs dict."""
        if pairs:
            lines = [f"{emoji}  —  <@&{role_id}>" for emoji, role_id in pairs.items()]
            description = "\n".join(lines)
        else:
            description = "_No roles configured yet. Use_ `!reactionrole add` _to add some._"

        return fluxer.Embed(
            title=title,
            description=description,
            color=_COLOR,
        ).set_footer(text="React below to assign yourself a role. React again to remove it.")

    # =========================================================================
    # Main command dispatcher
    # =========================================================================

    @fluxer.Cog.command(name="reactionrole")
    @fluxer.checks.has_permission(fluxer.Permissions.MANAGE_ROLES)
    async def reactionrole(self, ctx: fluxer.Message, *, args: str = "") -> None:
        """Dispatcher for all !reactionrole sub-commands."""
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        parts = args.strip().split(None, 3)
        subcommand = parts[0].lower() if parts else ""
        guild = self.bot._guilds.get(ctx.guild_id)

        if subcommand == "create":
            await self._rr_create(ctx, parts)
        elif subcommand == "add":
            await self._rr_add(ctx, parts, guild)
        elif subcommand == "remove":
            await self._rr_remove(ctx, parts)
        elif subcommand == "delete":
            await self._rr_delete(ctx, parts)
        elif subcommand == "list":
            await self._rr_list(ctx)
        else:
            await self._rr_help(ctx)

    # =========================================================================
    # Sub-command implementations
    # =========================================================================

    async def _rr_help(self, ctx: fluxer.Message) -> None:
        p = config.COMMAND_PREFIX
        embed = fluxer.Embed(
            title="Reaction Role commands",
            description=(
                f"`{p}reactionrole create <title>` — Post a new reaction-role embed\n"
                f"`{p}reactionrole add <msg_id> <emoji> <role>` — Add an emoji→role pair\n"
                f"`{p}reactionrole remove <msg_id> <emoji>` — Remove an emoji→role pair\n"
                f"`{p}reactionrole delete <msg_id>` — Delete the embed entirely\n"
                f"`{p}reactionrole list` — List all active reaction-role messages\n\n"
                "All commands require **Manage Roles** permission."
            ),
            color=_COLOR,
        )
        await ctx.reply(embed=embed)

    async def _rr_create(self, ctx: fluxer.Message, parts: list[str]) -> None:
        """Post a new (empty) reaction-role embed in the current channel."""
        # parts = ["create", "title word1", "word2", ...]  — rejoin everything after "create"
        title = " ".join(parts[1:]).strip() if len(parts) > 1 else "React to get a role"

        embed = self._build_embed(title, {})
        sent = await ctx.channel.send(embed=embed)

        # Persist the new message
        rr_data: dict = self.settings.get(ctx.guild_id, "reaction_roles") or {}
        rr_data[str(sent.id)] = {
            "channel_id": ctx.channel.id,
            "title": title,
            "pairs": {},
        }
        self.settings.set(ctx.guild_id, "reaction_roles", rr_data)

        log.info(
            "Reaction-role embed created (msg %d) in guild %d by %s",
            sent.id, ctx.guild_id, ctx.author.username,
        )

        await ctx.reply(
            embed=fluxer.Embed(
                title="Reaction-role embed created",
                description=(
                    f"Message ID: `{sent.id}`\n\n"
                    f"Add roles with:\n"
                    f"`{config.COMMAND_PREFIX}reactionrole add {sent.id} 🎮 Gamers`"
                ),
                color=_COLOR,
            )
        )

    async def _rr_add(self, ctx: fluxer.Message, parts: list[str], guild) -> None:
        """Add an emoji→role pair to an existing reaction-role message.

        Usage: !reactionrole add <message_id> <emoji> <role name or mention>
        parts = ["add", "message_id", "emoji", "role name..."]
        """
        if len(parts) < 4:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}reactionrole add <message_id> <emoji> <role name>`\n"
                f"Example: `{config.COMMAND_PREFIX}reactionrole add 1234567890 🎮 Gamers`"
            )
            return

        msg_id_str = parts[1]
        emoji = parts[2]
        role_query = parts[3].strip()

        # Validate message ID
        rr_data: dict = self.settings.get(ctx.guild_id, "reaction_roles") or {}
        if msg_id_str not in rr_data:
            await ctx.reply(
                f"No reaction-role embed found with ID `{msg_id_str}`.\n"
                f"Use `{config.COMMAND_PREFIX}reactionrole list` to see active embeds."
            )
            return

        # Resolve the role
        role = self._find_role(guild, role_query)
        if role is None:
            await ctx.reply(f"Could not find a role matching **{role_query}**.")
            return

        # Safety: bot hierarchy check
        if not self._bot_can_assign(guild, role):
            await ctx.reply(
                f"I can't assign **{role.name}** because it's higher than my own highest role.\n"
                "Move my role above it in Server Settings → Roles first."
            )
            return

        entry = rr_data[msg_id_str]
        pairs: dict = entry.get("pairs", {})

        if emoji in pairs:
            await ctx.reply(
                f"{emoji} is already mapped to <@&{pairs[emoji]}> on that embed.\n"
                f"Remove it first with `{config.COMMAND_PREFIX}reactionrole remove {msg_id_str} {emoji}`"
            )
            return

        # Add the pair and update the embed
        pairs[emoji] = role.id
        entry["pairs"] = pairs
        rr_data[msg_id_str] = entry
        self.settings.set(ctx.guild_id, "reaction_roles", rr_data)

        # Edit the live embed
        channel = self.bot._channels.get(entry["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(int(msg_id_str))
                await message.edit(embed=self._build_embed(entry["title"], pairs))
                await message.add_reaction(emoji)
            except Exception as exc:
                log.warning("Failed to update reaction-role embed: %s", exc)

        log.info(
            "Added %s → role %d to reaction-role msg %s in guild %d",
            emoji, role.id, msg_id_str, ctx.guild_id,
        )

        await ctx.reply(
            embed=fluxer.Embed(
                title="Role added",
                description=f"{emoji} is now mapped to **{role.name}**.",
                color=_COLOR,
            )
        )

    async def _rr_remove(self, ctx: fluxer.Message, parts: list[str]) -> None:
        """Remove an emoji→role pair from a reaction-role message.

        Usage: !reactionrole remove <message_id> <emoji>
        """
        if len(parts) < 3:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}reactionrole remove <message_id> <emoji>`"
            )
            return

        msg_id_str = parts[1]
        emoji = parts[2]

        rr_data: dict = self.settings.get(ctx.guild_id, "reaction_roles") or {}
        if msg_id_str not in rr_data:
            await ctx.reply(f"No reaction-role embed found with ID `{msg_id_str}`.")
            return

        entry = rr_data[msg_id_str]
        pairs: dict = entry.get("pairs", {})

        if emoji not in pairs:
            await ctx.reply(f"{emoji} is not mapped on that embed.")
            return

        del pairs[emoji]
        entry["pairs"] = pairs
        rr_data[msg_id_str] = entry
        self.settings.set(ctx.guild_id, "reaction_roles", rr_data)

        # Update the live embed and remove the bot's reaction
        channel = self.bot._channels.get(entry["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(int(msg_id_str))
                await message.edit(embed=self._build_embed(entry["title"], pairs))
                await message.clear_reaction(emoji)
            except Exception as exc:
                log.warning("Failed to update reaction-role embed: %s", exc)

        log.info(
            "Removed %s from reaction-role msg %s in guild %d",
            emoji, msg_id_str, ctx.guild_id,
        )

        await ctx.reply(
            embed=fluxer.Embed(
                title="Role removed",
                description=f"{emoji} has been removed from the embed.",
                color=_COLOR,
            )
        )

    async def _rr_delete(self, ctx: fluxer.Message, parts: list[str]) -> None:
        """Delete a reaction-role embed entirely and clear its stored mappings."""
        if len(parts) < 2:
            await ctx.reply(
                f"Usage: `{config.COMMAND_PREFIX}reactionrole delete <message_id>`"
            )
            return

        msg_id_str = parts[1]
        rr_data: dict = self.settings.get(ctx.guild_id, "reaction_roles") or {}

        if msg_id_str not in rr_data:
            await ctx.reply(f"No reaction-role embed found with ID `{msg_id_str}`.")
            return

        entry = rr_data.pop(msg_id_str)
        self.settings.set(ctx.guild_id, "reaction_roles", rr_data)

        # Try to delete the actual message
        channel = self.bot._channels.get(entry["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(int(msg_id_str))
                await message.delete()
            except Exception as exc:
                log.warning("Could not delete reaction-role message %s: %s", msg_id_str, exc)

        log.info(
            "Deleted reaction-role embed %s in guild %d by %s",
            msg_id_str, ctx.guild_id, ctx.author.username,
        )

        await ctx.reply(
            embed=fluxer.Embed(
                title="Embed deleted",
                description=f"Reaction-role embed `{msg_id_str}` and all its mappings have been removed.",
                color=_COLOR,
            )
        )

    async def _rr_list(self, ctx: fluxer.Message) -> None:
        """List all active reaction-role embeds in this server."""
        rr_data: dict = self.settings.get(ctx.guild_id, "reaction_roles") or {}

        if not rr_data:
            await ctx.reply(
                f"No reaction-role embeds set up yet.\n"
                f"Create one with `{config.COMMAND_PREFIX}reactionrole create <title>`."
            )
            return

        lines = []
        for msg_id, entry in rr_data.items():
            pair_count = len(entry.get("pairs", {}))
            channel = self.bot._channels.get(entry["channel_id"])
            channel_name = f"#{channel.name}" if channel else f"channel {entry['channel_id']}"
            lines.append(
                f"**{entry['title']}** — `{msg_id}`\n"
                f"  {channel_name} · {pair_count} role{'s' if pair_count != 1 else ''}"
            )

        embed = fluxer.Embed(
            title=f"Reaction-role embeds — {len(rr_data)} total",
            description="\n\n".join(lines),
            color=_COLOR,
        )
        await ctx.reply(embed=embed)

    # =========================================================================
    # Reaction listeners — the core user-facing logic
    # =========================================================================

    @fluxer.Cog.listener()
    async def on_reaction_add(self, data: dict) -> None:
        """Assign a role when a user adds a reaction to a reaction-role message."""
        user_id = data.get("user_id")
        guild_id = data.get("guild_id")
        message_id = str(data.get("message_id", ""))
        emoji_data = data.get("emoji", {})
        emoji = emoji_data.get("name", "")  # Works for both unicode and custom emoji names

        # Ignore bot reactions
        if data.get("member", {}).get("user", {}).get("bot"):
            return
        if guild_id is None:
            return

        rr_data: dict = self.settings.get(guild_id, "reaction_roles") or {}
        if message_id not in rr_data:
            return

        pairs: dict = rr_data[message_id].get("pairs", {})
        role_id = pairs.get(emoji)
        if role_id is None:
            return

        guild = self.bot._guilds.get(guild_id)
        if guild is None:
            return

        try:
            await guild.add_role(user_id, role_id)
            log.debug(
                "Assigned role %d to user %d via reaction %s in guild %d",
                role_id, user_id, emoji, guild_id,
            )
        except Exception as exc:
            log.warning("Failed to assign role %d to user %d: %s", role_id, user_id, exc)

    @fluxer.Cog.listener()
    async def on_reaction_remove(self, data: dict) -> None:
        """Remove a role when a user removes their reaction from a reaction-role message."""
        user_id = data.get("user_id")
        guild_id = data.get("guild_id")
        message_id = str(data.get("message_id", ""))
        emoji_data = data.get("emoji", {})
        emoji = emoji_data.get("name", "")

        if guild_id is None:
            return

        rr_data: dict = self.settings.get(guild_id, "reaction_roles") or {}
        if message_id not in rr_data:
            return

        pairs: dict = rr_data[message_id].get("pairs", {})
        role_id = pairs.get(emoji)
        if role_id is None:
            return

        guild = self.bot._guilds.get(guild_id)
        if guild is None:
            return

        try:
            await guild.remove_role(user_id, role_id)
            log.debug(
                "Removed role %d from user %d via reaction %s in guild %d",
                role_id, user_id, emoji, guild_id,
            )
        except Exception as exc:
            log.warning("Failed to remove role %d from user %d: %s", role_id, user_id, exc)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _find_role(self, guild, query: str):
        """Resolve a role by mention, ID, or name (case-insensitive)."""
        if guild is None:
            return None

        roles = getattr(guild, "roles", [])

        # Mention format: <@&123456>
        if query.startswith("<@&") and query.endswith(">"):
            role_id = int(query[3:-1])
            return next((r for r in roles if r.id == role_id), None)

        # Raw ID
        if query.isdigit():
            role_id = int(query)
            return next((r for r in roles if r.id == role_id), None)

        # Name (case-insensitive) - strip leading @ if user typed @Role Name
        name = query.lstrip("@").strip()
        return next((r for r in roles if r.name.lower() == name.lower()), None)


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(RolesCog(bot))