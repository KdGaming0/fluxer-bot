import os

# ── Fluxer bot ────────────────────────────────────────────────────────────────
# Set FLUXER_TOKEN in your environment (e.g. in your systemd service file).
# Never hardcode tokens here.
FLUXER_TOKEN: str   = os.environ.get("FLUXER_TOKEN", "")
BOT_TOKEN: str      = FLUXER_TOKEN   # backwards-compatibility alias
COMMAND_PREFIX: str = os.environ.get("COMMAND_PREFIX", "!")

# ── Discord bot ───────────────────────────────────────────────────────────────
DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "")

# ── OAuth2 client IDs (used to generate invite links on startup) ───────────────
# Find FLUXER_CLIENT_ID in the Fluxer Developer Portal.
# Find DISCORD_CLIENT_ID in the Discord Developer Portal → General Information.
FLUXER_CLIENT_ID: str  = os.environ.get("FLUXER_CLIENT_ID", "")
DISCORD_CLIENT_ID: str = os.environ.get("DISCORD_CLIENT_ID", "")

# ── Welcome cog ───────────────────────────────────────────────────────────────
# Welcome channel is set at runtime via !setwelcome and stored in data/guild_settings.json.
# Colour used on the welcome embed (hex int).
WELCOME_EMBED_COLOR: int = 0x5865F2

# ── Moderation cog ────────────────────────────────────────────────────────────
# Comma-separated words that trigger auto-mod, e.g. "badword1,badword2"
BANNED_WORDS: list[str] = [
    w.strip().lower()
    for w in os.environ.get("BANNED_WORDS", "").split(",")
    if w.strip()
]

# ── Utility cog ───────────────────────────────────────────────────────────────
# Maximum reminder duration in seconds a user can request (default: 24 h).
MAX_REMINDER_SECONDS: int = int(os.environ.get("MAX_REMINDER_SECONDS", 86400))

# ── Modrinth cog ──────────────────────────────────────────────────────────────
# Your user ID — only this user can run !track interval.
OWNER_ID: str = os.environ.get("OWNER_ID", "")

# How often (in seconds) to poll Modrinth for new mod versions. Minimum 60.
MODRINTH_CHECK_INTERVAL: int = int(os.environ.get("MODRINTH_CHECK_INTERVAL", 300))