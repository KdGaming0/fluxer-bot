"""
cogs/welcome.py
---------------
Greets new members with a rich embed.

Commands:
    !setwelcome #channel              - Set the welcome channel
    !setwelcome                       - Clear the welcome channel
    !setwelcome msg <message>         - Set a custom welcome message
                                        Use {user} for mention, {name} for display name,
                                        {server} for server name, {count} for member count
    !setwelcome msg                   - Reset to default message
    !setwelcome getstarted <text>     - Set the "Get started" embed field
                                        Supports channel mentions e.g. <#123456>
    !setwelcome getstarted            - Reset to default
    !setwelcome help <text>           - Set the "Need help?" embed field
    !setwelcome help                  - Reset to default
    !setwelcome preview               - Preview the current welcome embed
"""

import logging
import re

import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.welcome")

DEFAULT_MESSAGE = (
    "Hey **{name}**, welcome to **{server}**!\n"
    "We're glad to have you here. Feel free to introduce yourself."
)

DEFAULT_GETSTARTED = "Read the rules and grab some roles to unlock channels."
DEFAULT_HELP = f"Use `{config.COMMAND_PREFIX}help` to see available commands."


class WelcomeCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _get_guild_name(self, guild_id: int) -> str:
        """Return guild name from cache, falling back to an HTTP fetch."""
        guild = self.bot._guilds.get(guild_id)
        if guild and guild.name:
            return guild.name
        try:
            data = await self.bot._http.get_guild(guild_id)
            if data and data.get("name"):
                full_guild = fluxer.Guild.from_data(data, self.bot._http)
                self.bot._guilds[guild_id] = full_guild
                return full_guild.name
        except Exception as exc:
            log.warning("Could not fetch guild %d from API: %s", guild_id, exc)
        return "the server"

    async def _get_member_count(self, guild_id: int) -> int | str:
        """Return member count from cache, falling back to an HTTP fetch."""
        guild = self.bot._guilds.get(guild_id)
        if guild and guild.member_count is not None:
            return guild.member_count
        try:
            data = await self.bot._http.get_guild(guild_id)
            if data:
                return (
                    data.get("approximate_member_count")
                    or data.get("member_count")
                    or "?"
                )
        except Exception as exc:
            log.warning("Could not fetch member count for guild %d: %s", guild_id, exc)
        return "?"

    def _format_message(self, template: str, user, guild_name: str, member_count) -> str:
        """Replace placeholders in the welcome message template."""
        return (
            template
            .replace("{user}", user.mention)
            .replace("{name}", user.display_name)
            .replace("{server}", str(guild_name))
            .replace("{count}", str(member_count))
        )

    def _find_channel_by_name(self, guild_id: int, name: str) -> fluxer.Channel | None:
        for channel in self.bot._channels.values():
            if channel.guild_id == guild_id and channel.name == name:
                return channel
        return None

    def _build_embed(self, description: str, getstarted: str, help_text: str) -> fluxer.Embed:
        """Construct the welcome embed from its three text parts."""
        embed = fluxer.Embed(
            title="Welcome!",
            description=description,
            color=config.WELCOME_EMBED_COLOR,
        )
        if getstarted:
            embed.add_field(name="Get started", value=getstarted, inline=False)
        if help_text:
            embed.add_field(name="Need help?", value=help_text, inline=False)
        return embed

    # =========================================================================
    # Main command dispatcher
    # =========================================================================

    @fluxer.Cog.command(name="setwelcome")
    async def setwelcome(self, ctx: fluxer.Message) -> None:
        """Dispatcher for all !setwelcome sub-commands."""
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        raw = ctx.content.strip()
        args_str = raw[len(f"{config.COMMAND_PREFIX}setwelcome"):].strip()
        tokens = args_str.split(None, 1)
        sub = tokens[0].lower() if tokens else ""
        rest = tokens[1].strip() if len(tokens) > 1 else ""

        if sub == "preview":
            await self._subcmd_preview(ctx)
        elif sub == "msg":
            await self._subcmd_msg(ctx, rest)
        elif sub == "getstarted":
            await self._subcmd_getstarted(ctx, rest)
        elif sub == "help":
            await self._subcmd_help(ctx, rest)
        else:
            # No recognised sub-command — treat the whole string as a channel argument
            await self._subcmd_channel(ctx, args_str)

    # =========================================================================
    # Sub-command handlers
    # =========================================================================

    async def _subcmd_channel(self, ctx: fluxer.Message, args: str) -> None:
        """Set or clear the welcome channel."""
        if not args:
            self.settings.delete(ctx.guild_id, "welcome_channel_id")
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Welcome disabled",
                    description="Welcome channel cleared. No welcome messages will be sent.",
                    color=config.WELCOME_EMBED_COLOR,
                )
            )
            return

        channel = None
        match = re.search(r"<#(\d+)>", args)
        if match:
            channel = self.bot._channels.get(int(match.group(1)))
        else:
            channel = self._find_channel_by_name(ctx.guild_id, args.lstrip("#"))

        if channel is None:
            await ctx.reply(
                f"Could not find that channel. "
                f"Try: `{config.COMMAND_PREFIX}setwelcome #channel-name`"
            )
            return

        if not channel.is_text_channel:
            await ctx.reply("The welcome channel must be a text channel.")
            return

        self.settings.set(ctx.guild_id, "welcome_channel_id", channel.id)
        log.info(
            "Welcome channel set to #%s (%d) in guild %d",
            channel.name, channel.id, ctx.guild_id,
        )

        await ctx.reply(
            embed=fluxer.Embed(
                title="Welcome channel set",
                description=(
                    f"New members will be welcomed in **#{channel.name}**.\n\n"
                    f"`{config.COMMAND_PREFIX}setwelcome msg <text>` — set the welcome message\n"
                    f"`{config.COMMAND_PREFIX}setwelcome getstarted <text>` — set the Get started field\n"
                    f"`{config.COMMAND_PREFIX}setwelcome help <text>` — set the Need help? field\n"
                    f"`{config.COMMAND_PREFIX}setwelcome preview` — preview the embed\n\n"
                    f"Placeholders: `{{user}}` `{{name}}` `{{server}}` `{{count}}`"
                ),
                color=config.WELCOME_EMBED_COLOR,
            )
        )

    async def _subcmd_msg(self, ctx: fluxer.Message, text: str) -> None:
        """Set or reset the main welcome message body."""
        if not text:
            self.settings.delete(ctx.guild_id, "welcome_message")
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Welcome message reset",
                    description=f"Reset to default:\n\n{DEFAULT_MESSAGE}",
                    color=config.WELCOME_EMBED_COLOR,
                )
            )
            return

        self.settings.set(ctx.guild_id, "welcome_message", text)
        log.info("Welcome message updated in guild %d", ctx.guild_id)

        await ctx.reply(
            embed=fluxer.Embed(
                title="Welcome message updated",
                description=(
                    f"{text}\n\n"
                    f"Run `{config.COMMAND_PREFIX}setwelcome preview` to see the full embed."
                ),
                color=config.WELCOME_EMBED_COLOR,
            )
        )

    async def _subcmd_getstarted(self, ctx: fluxer.Message, text: str) -> None:
        """Set or reset the 'Get started' embed field.

        Supports channel mentions — write <#CHANNEL_ID> and they render
        as clickable links inside the embed.
        """
        if not text:
            self.settings.delete(ctx.guild_id, "welcome_getstarted")
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Get started field reset",
                    description=f"Reset to default:\n\n{DEFAULT_GETSTARTED}",
                    color=config.WELCOME_EMBED_COLOR,
                )
            )
            return

        self.settings.set(ctx.guild_id, "welcome_getstarted", text)
        log.info("Welcome getstarted field updated in guild %d", ctx.guild_id)

        await ctx.reply(
            embed=fluxer.Embed(
                title="Get started field updated",
                description=(
                    f"{text}\n\n"
                    f"Run `{config.COMMAND_PREFIX}setwelcome preview` to see the full embed."
                ),
                color=config.WELCOME_EMBED_COLOR,
            )
        )

    async def _subcmd_help(self, ctx: fluxer.Message, text: str) -> None:
        """Set or reset the 'Need help?' embed field."""
        if not text:
            self.settings.delete(ctx.guild_id, "welcome_help")
            await ctx.reply(
                embed=fluxer.Embed(
                    title="Need help? field reset",
                    description=f"Reset to default:\n\n{DEFAULT_HELP}",
                    color=config.WELCOME_EMBED_COLOR,
                )
            )
            return

        self.settings.set(ctx.guild_id, "welcome_help", text)
        log.info("Welcome help field updated in guild %d", ctx.guild_id)

        await ctx.reply(
            embed=fluxer.Embed(
                title="Need help? field updated",
                description=(
                    f"{text}\n\n"
                    f"Run `{config.COMMAND_PREFIX}setwelcome preview` to see the full embed."
                ),
                color=config.WELCOME_EMBED_COLOR,
            )
        )

    async def _subcmd_preview(self, ctx: fluxer.Message) -> None:
        """Preview the full welcome embed using the caller as the test member."""
        guild_name = await self._get_guild_name(ctx.guild_id)
        member_count = await self._get_member_count(ctx.guild_id)

        template = self.settings.get(ctx.guild_id, "welcome_message") or DEFAULT_MESSAGE
        description = self._format_message(template, ctx.author, guild_name, member_count)

        getstarted = self.settings.get(ctx.guild_id, "welcome_getstarted") or DEFAULT_GETSTARTED
        help_text = self.settings.get(ctx.guild_id, "welcome_help") or DEFAULT_HELP

        embed = self._build_embed(description, getstarted, help_text)
        embed.set_footer(text="This is a preview using your account.")

        await ctx.reply(embed=embed)

    # =========================================================================
    # Listener
    # =========================================================================

    @fluxer.Cog.listener()
    async def on_member_join(self, data: dict) -> None:
        """Send a welcome embed when a new member joins."""
        member = fluxer.GuildMember.from_data(data, self.bot._http)
        guild_id = member.guild_id

        if guild_id is None:
            return

        channel_id = self.settings.get(guild_id, "welcome_channel_id")
        if channel_id is None:
            log.debug("No welcome channel set for guild %d — skipping", guild_id)
            return

        channel = self.bot._channels.get(channel_id)
        if channel is None:
            log.warning(
                "Welcome channel %d no longer exists in guild %d", channel_id, guild_id
            )
            return

        guild_name = await self._get_guild_name(guild_id)
        member_count = await self._get_member_count(guild_id)

        template = self.settings.get(guild_id, "welcome_message") or DEFAULT_MESSAGE
        description = self._format_message(template, member.user, guild_name, member_count)

        getstarted = self.settings.get(guild_id, "welcome_getstarted") or DEFAULT_GETSTARTED
        help_text = self.settings.get(guild_id, "welcome_help") or DEFAULT_HELP

        embed = self._build_embed(description, getstarted, help_text)

        await channel.send(embed=embed)
        log.info("Sent welcome message for %s in guild %s", member.user.username, guild_name)


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))