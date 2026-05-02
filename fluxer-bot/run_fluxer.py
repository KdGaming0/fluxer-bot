import asyncio
import logging
import os
import sys
import importlib
import json

# ── Platform isolation ──────────────────────────────────────────────────────
# Set this before any import that may transitively import utils.storage,
# so Discord and Fluxer get their own guild_settings JSON files.
os.environ["BOT_PLATFORM"] = "fluxer"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fluxer
import config

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)

# Map IDs → name for logs; pass name into add_cog so it uses the right key
COG_ID_TO_NAME = {
    "welcome":      "WelcomeCog",
    "modrinth":     "ModrinthCog",
    "hypixel_updates": "HypixelUpdatesCog",
    "reddit_monitor":  "RedditMonitorCog",
    "utility":      "UtilityCog",
    "log_upload":   "LogUploadCog",
    "hypixel_monitor": "HypixelMonitorCog",
    "moderation":   "ModerationCog",
    "roles":        "RolesCog",
    "detection":    "DetectionCog",
}


def _get_cog_classes(module):
    classes = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, fluxer.Cog) and obj is not fluxer.Cog:
            classes.append(obj)
    return classes


async def load_cogs():
    import inspect
    cog_dir = os.path.join(os.path.dirname(__file__), "cogs")
    if not os.path.isdir(cog_dir):
        print("Cog directory not found.")
        return
    for filename in os.listdir(cog_dir):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        cog_id = filename[:-3]
        module_name = f"cogs.{cog_id}"
        try:
            module = importlib.import_module(module_name)
            for cls in _get_cog_classes(module):
                instance = cls(bot)
                name = COG_ID_TO_NAME.get(cog_id, cls.__name__)
                await bot.add_cog(instance, name=name)
                print(f"Loaded cog: {name}")
        except Exception as exc:
            print(f"Failed to load cog {cog_id}: {exc}")


async def main():
    global bot
    bot = fluxer.Bot(command_prefix=config.COMMAND_PREFIX, intents=fluxer.Intents.default())
    await load_cogs()
    await bot.start(config.FLUXER_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
