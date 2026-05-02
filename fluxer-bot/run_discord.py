import asyncio
import logging
import os
import sys

# ── Platform isolation ──────────────────────────────────────────────────────
# Set this before any import that may transitively import utils.storage,
# so Discord and Fluxer get their own guild_settings JSON files.
os.environ["BOT_PLATFORM"] = "discord"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import utils.discord_shim
import config

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("cog.utility").setLevel(logging.DEBUG)


async def main():
    bot = utils.discord_shim.CustomFluxerBot(
        command_prefix=config.COMMAND_PREFIX,
        intents=utils.discord_shim.CustomIntents.default(),
    )
    await utils.discord_shim.CustomBot.add_cogs(bot)
    await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
