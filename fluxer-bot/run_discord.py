"""
run_discord.py
--------------
Discord bot entry point.

Launched automatically by bot.py (master launcher), or directly:
    python run_discord.py

CRITICAL: ``utils.discord_shim`` is imported first — before anything else.
This installs a fake ``fluxer`` module into ``sys.modules`` so that every
cog file works on discord.py without any code changes.
"""

# ── MUST be the very first non-stdlib import ──────────────────────────────────
import utils.discord_shim                   # installs sys.modules["fluxer"]
from utils.discord_shim import DiscordBot   # our commands.Bot subclass
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import logging
import sys

import discord
import config

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Show debug output from the utility cog so tag/command dispatch is visible
logging.getLogger("cog.utility").setLevel(logging.DEBUG)
log = logging.getLogger("discord_bot")

# ── Bot instance ──────────────────────────────────────────────────────────────
intents = discord.Intents.all()

bot = DiscordBot(
    command_prefix=config.COMMAND_PREFIX,
    intents=intents,
)


# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s", bot.user)
    log.info("Command prefix : %s", config.COMMAND_PREFIX)

    if config.DISCORD_CLIENT_ID:
        perms = discord.Permissions(
            ban_members=True,
            kick_members=True,
            manage_messages=True,
            manage_channels=True,
            manage_guild=True,
            manage_roles=True,
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            add_reactions=True,
            moderate_members=True,
        )
        url = (
            f"https://discord.com/api/oauth2/authorize"
            f"?client_id={config.DISCORD_CLIENT_ID}"
            f"&permissions={perms.value}"
            f"&scope=bot"
        )
        log.info("Invite: %s", url)


@bot.event
async def on_guild_available(guild: discord.Guild) -> None:
    log.info(
        "Guild available: %s (%d) — total: %d", guild.name, guild.id, len(bot.guilds)
    )


# ── Entry point ───────────────────────────────────────────────────────────────
async def main() -> None:
    if not config.DISCORD_TOKEN:
        log.error(
            "DISCORD_TOKEN environment variable is not set. "
            "Export it before running the bot."
        )
        sys.exit(1)

    await bot.load_cogs()
    await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())