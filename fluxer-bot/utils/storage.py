"""
utils/storage.py
----------------
Simple atomic guild-level key/value JSON store.

Typical usage:
    settings = GuildSettings()
    val = settings.get(guild_id, "mykey")          # returns dict|list|str|int|None
    settings.set(guild_id, "mykey", {"foo": 1})    # atomic write
    settings.delete(guild_id, "mykey")

Guild data is keyed by a synthetic string ``guild_id:key`` so every
caller can share the same flat JSON file safely.

Backwards-compatible note
~~~~~~~~~~~~~~~~~~~~~~~~~
Older versions of the bot wrote the JSON directly instead of writing to
a temp file and replacing.  If the write was interrupted, the file became
corrupt / empty; json.load then raised JSONDecodeError on the next
startup and the entire settings store was silently reset to empty —
causing tracked mod versions to look 'new' and triggering duplicate
update notifications after every restart.  The current implementation
avoids that by doing atomic writes (write to temp → fsync → rename).

Platform isolation note
~~~~~~~~~~~~~~~~~~~~~~~
When running both Discord and Fluxer side-by-side as separate OS
processes, each process gets its own backing file so they cannot
overwrite each other's settings (tags, modrinth state, hypixel state,
etc.).  The filename is determined by the ``BOT_PLATFORM`` env var.
"""
from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from typing import Any

_log = logging.getLogger("utils.storage")


# ============================================================================
# Paths
# ============================================================================
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Discord and Fluxer run in separate processes but load the same cogs.
# Use platform-specific filenames to prevent cross-process races.
_PLATFORM_SUFFIX = os.environ.get("BOT_PLATFORM", "")
if _PLATFORM_SUFFIX:
    _PLATFORM_SUFFIX = f"_{_PLATFORM_SUFFIX}"
_DATA_FILE = os.path.join(_DATA_DIR, f"guild_settings{_PLATFORM_SUFFIX}.json")


# ============================================================================
# GuildSettings
# ============================================================================
class GuildSettings:
    """Atomic guild-level JSON store.

    Safe for concurrent access *within* the same process.  When running
    both Discord and Fluxer side-by-side, each process gets its own
    backing file so they cannot overwrite each other.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _guild_key(guild_id: int, key: str) -> str:
        return f"{guild_id}:{key}"

    def _load(self) -> None:
        try:
            with open(_DATA_FILE, "r", encoding="utf-8") as fp:
                self._data = json.load(fp)
        except FileNotFoundError:
            self._data = {}
        except json.JSONDecodeError:
            # Corrupt file — archive it and start fresh
            _log.warning("Corrupt settings file %s; backing up and resetting", _DATA_FILE)
            corrupt_path = _DATA_FILE + ".corrupt"
            try:
                os.replace(_DATA_FILE, corrupt_path)
            except OSError:
                pass
            self._data = {}

    def _save(self) -> None:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._data, fp, indent=2, ensure_ascii=False)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, _DATA_FILE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self, guild_id: int, key: str, default: Any = None) -> Any:
        return deepcopy(self._data.get(self._guild_key(guild_id, key), default))

    def set(self, guild_id: int, key: str, value: Any) -> None:
        self._data[self._guild_key(guild_id, key)] = deepcopy(value)
        self._save()

    def delete(self, guild_id: int, key: str) -> None:
        gk = self._guild_key(guild_id, key)
        if gk in self._data:
            del self._data[gk]
            self._save()
