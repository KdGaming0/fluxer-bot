"""
cogs/welcome.py
---------------
Greets new members with a rich embed.

Setup (run once after adding the bot to your server):
    !setwelcome #your-channel-name

The channel is saved to data/guild_settings.json and survives restarts.
"""

import logging
import re

import fluxer
import config
from utils.storage import GuildSettings

log = logging.getLogger("cog.welcome")


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
            # No argument — clear the current setting
            self.settings.delete(ctx.guild_id, "welcome_channel_id")
            await ctx.reply("Welcome channel cleared. No welcome messages will be sent.")
            return

        # Try to parse a <#ID> channel mention first, then fall back to name
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

        if not channel.is_text_channel():
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
                f"To disable it: `{config.COMMAND_PREFIX}setwelcome` with no argument."
            ),
            color=config.WELCOME_EMBED_COLOR,
        )
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
        display = member.nick or member.user.global_name or member.user.username

        embed = (
            fluxer.Embed(
                title="Welcome!",
                description=(
                    f"Hey **{display}**, welcome to **{guild_name}**!\n"
                    "We're glad to have you here. Feel free to introduce yourself."
                ),
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

    def _find_channel_by_name(self, guild_id: int, name: str) -> fluxer.Channel | None:
        """Find a cached channel by name within a specific guild."""
        for channel in self.bot._channels.values():
            if channel.guild_id == guild_id and channel.name == name:
                return channel
        return None


# ── Extension setup ───────────────────────────────────────────────────────────

async def setup(bot: fluxer.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))