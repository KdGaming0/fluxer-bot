"""
bot.py
------
Master launcher.

Reads which tokens are set, prints invite links for every configured
platform, then starts each bot as an independent sub-process:

    run_fluxer.py   — uses the real fluxer package
    run_discord.py  — installs the fluxer shim first, then loads the same cogs

Running them as separate processes keeps their ``sys.modules`` caches
completely independent, which is required for the shim to work correctly.

Usage
-----
    python bot.py

Both bots' stdout/stderr are streamed to this console, prefixed with
[Fluxer] or [Discord] so you can tell them apart.

Environment variables
---------------------
    FLUXER_TOKEN       — Fluxer bot token
    DISCORD_TOKEN      — Discord bot token
    FLUXER_CLIENT_ID   — Fluxer application client ID  (for invite link)
    DISCORD_CLIENT_ID  — Discord application client ID (for invite link)
    COMMAND_PREFIX     — command prefix (default: !)
"""

import asyncio
import logging
import sys
import os

# Force line-buffered stdout so child output isn't swallowed when running
# under systemd (which uses a pipe, not a TTY, causing Python to block-buffer).
sys.stdout.reconfigure(line_buffering=True)

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("launcher")

# ── Permission integer used for the Discord invite link ───────────────────────
# Covers everything this bot needs.  Adjust after you know your exact needs.
_DISCORD_PERMISSIONS = (
    0x00000002   # kick_members
    | 0x00000004   # ban_members
    | 0x00000010   # manage_channels
    | 0x00000020   # manage_guild
    | 0x00000040   # add_reactions
    | 0x00000400   # view_channel
    | 0x00000800   # send_messages
    | 0x00002000   # manage_messages
    | 0x00004000   # embed_links
    | 0x00010000   # read_message_history
    | 0x10000000   # manage_roles
    | 0x10000000000  # moderate_members (timeout)
)


def _print_invite_links() -> None:
    """Log invite URLs for every configured platform."""
    if config.FLUXER_CLIENT_ID:
        log.info(
            "┌─ Fluxer invite ──────────────────────────────────────────────────"
        )
        log.info(
            "│  https://fluxer.gg/oauth2/authorize"
            "?client_id=%s&permissions=8&scope=bot",
            config.FLUXER_CLIENT_ID,
        )
        log.info("└──────────────────────────────────────────────────────────────────")
    else:
        log.warning("FLUXER_CLIENT_ID not set — Fluxer invite link unavailable.")

    if config.DISCORD_CLIENT_ID:
        log.info(
            "┌─ Discord invite ─────────────────────────────────────────────────"
        )
        log.info(
            "│  https://discord.com/api/oauth2/authorize"
            "?client_id=%s&permissions=%d&scope=bot",
            config.DISCORD_CLIENT_ID,
            _DISCORD_PERMISSIONS,
        )
        log.info("└──────────────────────────────────────────────────────────────────")
    else:
        log.warning("DISCORD_CLIENT_ID not set — Discord invite link unavailable.")


async def _stream_process(proc: asyncio.subprocess.Process, prefix: str) -> None:
    """Read and print all output from a sub-process."""
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(f"{prefix} {line.decode(errors='replace')}", end="", flush=True)
    await proc.wait()


async def _run_bot(script: str, prefix: str) -> None:
    """Start a bot sub-process and stream its output until it exits."""
    log.info("Starting %s (script: %s)", prefix.strip("[]"), script)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",  # force unbuffered stdout/stderr in the child process
        os.path.join(os.path.dirname(__file__), script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ,
    )
    await _stream_process(proc, prefix)
    log.warning("%s process exited (code %s).", prefix.strip("[]"), proc.returncode)


async def main() -> None:
    _print_invite_links()

    tasks = []

    if config.FLUXER_TOKEN:
        tasks.append(_run_bot("run_fluxer.py", "[Fluxer] "))
    else:
        log.warning("FLUXER_TOKEN not set — Fluxer bot will not start.")

    if config.DISCORD_TOKEN:
        tasks.append(_run_bot("run_discord.py", "[Discord]"))
    else:
        log.warning("DISCORD_TOKEN not set — Discord bot will not start.")

    if not tasks:
        log.error(
            "No bot tokens configured.  "
            "Set FLUXER_TOKEN and/or DISCORD_TOKEN and try again."
        )
        return

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())