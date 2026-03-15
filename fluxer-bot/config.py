import os

# ── Bot core ──────────────────────────────────────────────────────────────────
# Set BOT_TOKEN in your environment (e.g. in your systemd service file).
# Never hardcode your token here.
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
COMMAND_PREFIX: str = os.environ.get("COMMAND_PREFIX", "!")

# ── Welcome cog ───────────────────────────────────────────────────────────────
# Welcome channel is set at runtime via !setwelcome and stored in data/guild_settings.json.
# Colour used on the welcome embed (hex int).
WELCOME_EMBED_COLOR: int = 0x5865F2

# ── Moderation cog (used later) ───────────────────────────────────────────────
# Comma-separated words that trigger auto-mod, e.g. "badword1,badword2"
BANNED_WORDS: list[str] = [
    w.strip().lower()
    for w in os.environ.get("BANNED_WORDS", "").split(",")
    if w.strip()
]

# ── Utility cog (used later) ──────────────────────────────────────────────────
# Maximum reminder duration in seconds a user can request (default: 24 h).
MAX_REMINDER_SECONDS: int = int(os.environ.get("MAX_REMINDER_SECONDS", 86400))