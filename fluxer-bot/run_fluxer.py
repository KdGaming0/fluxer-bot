"""
run_fluxer.py
-------------
Fluxer bot entry point.

Launched automatically by bot.py (master launcher), or directly:
    python run_fluxer.py

This is essentially the original bot.py, renamed so that bot.py can be
repurposed as the master launcher for both platforms.
"""

import asyncio
import logging
import os
import sys

import fluxer
import config

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fluxer_bot")

# ── Bot instance ──────────────────────────────────────────────────────────────
intents = fluxer.Intents.all()

bot = fluxer.Bot(
    command_prefix=config.COMMAND_PREFIX,
    intents=intents,
)


# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s", bot.user)
    log.info("Command prefix : %s", config.COMMAND_PREFIX)
    if config.FLUXER_CLIENT_ID:
        log.info(
            "Invite : https://fluxer.gg/oauth2/authorize"
            "?client_id=%s&permissions=8&scope=bot",
            config.FLUXER_CLIENT_ID,
        )


@bot.event
async def on_guild_join(guild: fluxer.Guild) -> None:
    log.info(
        "Guild available: %s (%d) — total: %d", guild, guild.id, len(bot.guilds)
    )


# ── Cog auto-loader ───────────────────────────────────────────────────────────
async def load_cogs() -> None:
    """Discover and load every module inside the cogs/ directory."""
    cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")

    for filename in sorted(os.listdir(cogs_dir)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue

        module_name = f"cogs.{filename[:-3]}"
        try:
            await bot.load_extension(module_name)
            log.info("Loaded cog: %s", module_name)
        except Exception:
            log.exception("Failed to load cog: %s", module_name)


# ── Entry point ───────────────────────────────────────────────────────────────
async def main() -> None:
    if not config.FLUXER_TOKEN:
        log.error(
            "FLUXER_TOKEN environment variable is not set. "
            "Export it before running the bot."
        )
        sys.exit(1)

    await load_cogs()
    await bot.start(config.FLUXER_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())