"""
cogs/welcome.py
---------------
Greets new members with a rich embed.

Setup (run once after adding the bot to your server):
    !setwelcome #your-channel-name

Commands:
    !setwelcome #channel     - Set the welcome channel
    !setwelcome              - Clear the welcome channel
    !setwelcomemsg <message> - Set a custom welcome message
                               Use {user} for mention, {name} for display name,
                               {server} for server name, {count} for member count
    !setwelcomemsg           - Reset to default message
    !welcomepreview          - Preview the current welcome message
"""

import logging
import re

import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.welcome")

DEFAULT_MESSAGE = "Hey **{name}**, welcome to **{server}**!\nWe're glad to have you here. Feel free to introduce yourself."


class WelcomeCog(fluxer.Cog):

    def __init__(self, bot: fluxer.Bot) -> None:
        super().__init__(bot)
        self.settings = GuildSettings()

    # ── Commands ──────────────────────────────────────────────────────────────

    @fluxer.Cog.command(name="setwelcome")
    async def setwelcome(self, ctx: fluxer.Message) -> None:
        """Set the welcome channel for this server.

        Usage:  !setwelcome #channel-name
                !setwelcome          <- clears the welcome channel
        """
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        args = ctx.content.removeprefix(f"{config.COMMAND_PREFIX}setwelcome").strip()

        if not args:
            self.settings.delete(ctx.guild_id, "welcome_channel_id")
            await ctx.reply("Welcome channel cleared. No welcome messages will be sent.")
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

        embed = fluxer.Embed(
            title="Welcome channel set",
            description=(
                f"New members will be welcomed in **#{channel.name}**.\n\n"
                f"To change it: `{config.COMMAND_PREFIX}setwelcome #other-channel`\n"
                f"To disable it: `{config.COMMAND_PREFIX}setwelcome` with no argument.\n\n"
                f"To customize the message: `{config.COMMAND_PREFIX}setwelcomemsg <message>`\n"
                f"Available placeholders: `{{user}}` `{{name}}` `{{server}}` `{{count}}`"
            ),
            color=config.WELCOME_EMBED_COLOR,
        )
        await ctx.reply(embed=embed)

    @fluxer.Cog.command(name="setwelcomemsg")
    async def setwelcomemsg(self, ctx: fluxer.Message) -> None:
        """Set a custom welcome message.

        Usage:  !setwelcomemsg Hey {user}, welcome to {server}! You are member #{count}.
                !setwelcomemsg       <- resets to default message

        Placeholders:
            {user}   - Mentions the user e.g. @John
            {name}   - Display name of the user
            {server} - Server name
            {count}  - Current member count
        """
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        args = ctx.content.removeprefix(f"{config.COMMAND_PREFIX}setwelcomemsg").strip()

        if not args:
            self.settings.delete(ctx.guild_id, "welcome_message")
            embed = fluxer.Embed(
                title="Welcome message reset",
                description=f"The welcome message has been reset to the default:\n\n_{DEFAULT_MESSAGE}_",
                color=config.WELCOME_EMBED_COLOR,
            )
            await ctx.reply(embed=embed)
            return

        self.settings.set(ctx.guild_id, "welcome_message", args)
        log.info("Welcome message updated in guild %d", ctx.guild_id)

        embed = fluxer.Embed(
            title="Welcome message updated",
            description=(
                f"New welcome message:\n\n_{args}_\n\n"
                f"Use `{config.COMMAND_PREFIX}welcomepreview` to preview it."
            ),
            color=config.WELCOME_EMBED_COLOR,
        )
        await ctx.reply(embed=embed)

    @fluxer.Cog.command(name="welcomepreview")
    async def welcomepreview(self, ctx: fluxer.Message) -> None:
        """Preview the current welcome message using yourself as the test user."""
        if ctx.guild_id is None:
            await ctx.reply("This command can only be used inside a server.")
            return

        guild = self.bot._guilds.get(ctx.guild_id)
        guild_name = guild.name if guild else "the server"
        member_count = len(guild.members) if guild and hasattr(guild, "members") else "?"

        template = self.settings.get(ctx.guild_id, "welcome_message") or DEFAULT_MESSAGE
        description = self._format_message(template, ctx.author, guild_name, member_count)

        embed = fluxer.Embed(
            title="👋 Welcome message preview",
            description=description,
            color=config.WELCOME_EMBED_COLOR,
        )
        embed.set_footer(text="This is a preview using your account.")
        await ctx.reply(embed=embed)

    # ── Listeners ─────────────────────────────────────────────────────────────

    @fluxer.Cog.listener()
    async def on_member_join(self, data: dict) -> None:
        """Called when a new member joins a guild."""
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
                "Welcome channel ID %d no longer exists in guild %d", channel_id, guild_id
            )
            return

        guild = self.bot._guilds.get(guild_id)
        guild_name = guild.name if guild else "the server"
        member_count = len(guild.members) if guild and hasattr(guild, "members") else "?"
        display = member.nick or member.user.global_name or member.user.username

        template = self.settings.get(guild_id, "welcome_message") or DEFAULT_MESSAGE
        description = self._format_message(template, member.user, guild_name, member_count)

        embed = (
            fluxer.Embed(
                title="Welcome!",
                description=description,
                color=config.WELCOME_EMBED_COLOR,
            )
            .add_field(
                name="Get started",
                value="Read the rules and grab some roles to unlock channels.",
                inline=False,
            )
            .add_field(
                name="Need help?",
                value=f"Use `{config.COMMAND_PREFIX}help` to see available commands.",
                inline=False,
            )
            .set_footer(text=f"User ID: {member.user.id}")
        )

        await channel.send(embed=embed)
        log.info(
            "Sent welcome message for %s in guild %s", member.user.username, guild_name
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _format_message(self, template: str, user, guild_name: str, member_count) -> str:
        """Replace placeholders in the welcome message template."""
        display = getattr(user, "global_name", None) or getattr(user, "username", str(user))
        mention = f"<@{user.id}>" if hasattr(user, "id") else display
        return (
            template
            .replace("{user}", mention)
            .replace("{name}", display)
            .replace("{server}", guild_name)
            .replace("{count}", str(member_count))
        )

    def _find_channel_by_name(self, guild_id: int, name: str) -> fluxer.Channel | None:
        """Find a cached channel by name within a specific guild."""
        for channel in self.bot._channels.values():
            if channel.guild_id == guild_id and channel.name == name:
                return channel
        return None


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))