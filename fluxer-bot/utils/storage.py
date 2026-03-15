"""
utils/storage.py
----------------
Lightweight persistent storage for per-guild bot settings.

All cogs share one JSON file (data/guild_settings.json).
Data is nested by guild ID, so each cog's keys never clash:

    {
        "123456789": {
            "welcome_channel_id": 987654321,
            "banned_words": ["badword"],
            ...
        }
    }

Usage:
    from utils.storage import GuildSettings

    settings = GuildSettings()

    # Read
    channel_id = settings.get(guild_id, "welcome_channel_id")

    # Write (saves to disk immediately)
    settings.set(guild_id, "welcome_channel_id", channel.id)

    # Delete a key
    settings.delete(guild_id, "welcome_channel_id")
"""

import json
import logging
import os
from typing import Any

log = logging.getLogger("utils.storage")

# Path to the data file, relative to the project root
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_DATA_FILE = os.path.join(_DATA_DIR, "guild_settings.json")


class GuildSettings:
    """Thread-safe (single-process) persistent key-value store per guild."""

    def __init__(self) -> None:
        os.makedirs(_DATA_DIR, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ── Private ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load settings from disk, or start with an empty store."""
        if not os.path.exists(_DATA_FILE):
            log.info("No settings file found — starting fresh at %s", _DATA_FILE)
            return
        try:
            with open(_DATA_FILE, encoding="utf-8") as f:
                self._data = json.load(f)
            log.info("Loaded guild settings from %s", _DATA_FILE)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Failed to load settings file: %s — starting fresh", exc)

    def _save(self) -> None:
        """Write the current state to disk."""
        try:
            with open(_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except OSError as exc:
            log.error("Failed to save settings file: %s", exc)

    def _guild_key(self, guild_id: int) -> str:
        return str(guild_id)

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, guild_id: int, key: str, default: Any = None) -> Any:
        """Return the value for *key* in this guild, or *default* if absent."""
        return self._data.get(self._guild_key(guild_id), {}).get(key, default)

    def set(self, guild_id: int, key: str, value: Any) -> None:
        """Set *key* to *value* for this guild and persist to disk."""
        gk = self._guild_key(guild_id)
        if gk not in self._data:
            self._data[gk] = {}
        self._data[gk][key] = value
        self._save()

    def delete(self, guild_id: int, key: str) -> None:
        """Remove *key* from this guild's settings (no-op if absent)."""
        gk = self._guild_key(guild_id)
        if gk in self._data and key in self._data[gk]:
            del self._data[gk][key]
            self._save()

    def get_all(self, guild_id: int) -> dict[str, Any]:
        """Return a copy of all settings for this guild."""
        return dict(self._data.get(self._guild_key(guild_id), {}))