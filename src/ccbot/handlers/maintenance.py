"""Periodic local-state maintenance, decoupled from Telegram-facing polling.

A dedicated background loop for hygiene work on ccbot's own state. Kept
separate from status_polling on purpose: that loop's 60s block makes network
calls against Telegram (topic probes) and demonstrably stalls on timeouts;
maintenance is local I/O and must not queue behind it.

Steps (each independently guarded, one failure never blocks the others):
  - session_map sweep: drop entries whose tmux window no longer exists.

Key components: MAINTENANCE_INTERVAL, run_maintenance_once(), maintenance_loop().
"""

import asyncio
import logging

from telegram import Bot

from ..session import session_manager

logger = logging.getLogger(__name__)

# Local-I/O hygiene cadence. Unrelated to status_polling's TOPIC_CHECK_INTERVAL
# (a Telegram API budget) even though the values coincide.
MAINTENANCE_INTERVAL = 60.0  # seconds


async def run_maintenance_once(bot: Bot) -> None:
    """Run every maintenance step, isolating failures per step."""
    try:
        await session_manager.sweep_stale_session_map_entries()
    except Exception as e:
        logger.error("session_map sweep failed: %s", e)


async def maintenance_loop(bot: Bot) -> None:
    """Background task: run maintenance steps every MAINTENANCE_INTERVAL."""
    logger.info("Maintenance loop started (interval: %ss)", MAINTENANCE_INTERVAL)
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL)
        await run_maintenance_once(bot)
