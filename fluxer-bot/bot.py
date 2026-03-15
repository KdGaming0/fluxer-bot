import asyncio
import importlib
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
log = logging.getLogger("bot")

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
    log.info("Connected to %d guild(s)", len(bot.guilds))
    log.info("Command prefix: %s", config.COMMAND_PREFIX)


# ── Cog auto-loader ───────────────────────────────────────────────────────────
async def load_cogs() -> None:
    """Discover and load every module inside the cogs/ directory.

    Each cog file must expose an  async def setup(bot)  function that adds
    its cog(s) to the bot.  To disable a cog temporarily, prefix its file
    with an underscore (e.g. _welcome.py) — it will be skipped.
    """
    cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")

    for filename in sorted(os.listdir(cogs_dir)):
        # Only load non-private .py files (skip __init__.py and _anything.py)
        if not filename.endswith(".py"):
            continue
        if filename.startswith("_"):
            continue

        module_name = f"cogs.{filename[:-3]}"  # e.g. "cogs.welcome"
        try:
            await bot.load_extension(module_name)
            log.info("Loaded cog: %s", module_name)
        except Exception:
            log.exception("Failed to load cog: %s", module_name)


# ── Entry point ───────────────────────────────────────────────────────────────
async def main() -> None:
    if not config.BOT_TOKEN:
        log.error(
            "BOT_TOKEN environment variable is not set. "
            "Export it before running the bot."
        )
        sys.exit(1)

    await load_cogs()
    await bot.run(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())