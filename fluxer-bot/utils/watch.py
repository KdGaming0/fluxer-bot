"""
utils/watch.py
--------------
Shared "is this message in a watched channel?" logic for cogs that keep a
per-guild list of watched channel IDs (ocr_triggers, crash_detect).

A watched ID may be:
    • a plain text channel        — message.channel_id matches directly
    • a forum channel (Discord)   — messages arrive in *threads* whose
                                    parent_id is the forum's ID
    • a category (Discord)        — matches any channel under it, e.g.
                                    ticket channels created on the fly

Forum and category matching rely on discord.py's ``channel.parent_id`` /
``channel.category_id`` attributes. On the Fluxer platform those attributes
don't exist, so the extra checks quietly evaluate to no-ops and only the
direct channel-ID match applies.
"""

from __future__ import annotations


def message_in_watched(message, watched_ids: list) -> bool:
    """True when *message* was posted in (or under) a watched channel ID."""
    if not watched_ids:
        return False
    if message.channel_id in watched_ids:
        return True

    channel = getattr(message, "channel", None)
    if channel is None:
        return False

    # Thread inside a watched forum (or text) channel — Discord only.
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is not None and parent_id in watched_ids:
        return True

    # Channel (or thread) under a watched category — Discord only.
    # discord.Thread exposes category_id via its parent, so tickets and
    # forum posts under a watched category both match.
    category_id = getattr(channel, "category_id", None)
    if category_id is not None and category_id in watched_ids:
        return True

    return False


def is_watchable_channel(channel) -> bool:
    """True for channel objects the watch commands accept.

    Text channels on both platforms; forum channels and categories on
    Discord (the shim's _ChannelProxy sets is_forum_channel/is_category —
    on Fluxer those attributes are absent and default to False).
    """
    return bool(
        getattr(channel, "is_text_channel", False)
        or getattr(channel, "is_forum_channel", False)
        or getattr(channel, "is_category", False)
    )
