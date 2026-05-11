"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Periodically probes topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins); cleans up deleted topics (kills tmux window
    + unbinds thread)

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import logging
import time

from telegram import Bot
from telegram.error import BadRequest

from ..session import session_manager
from ..terminal_parser import extract_interactive_content, parse_status_line
from ..tmux_manager import tmux_manager
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_last_name,
    get_interactive_msg_id,
    get_interactive_window,
    is_interactive_enqueued,
    mark_interactive_enqueued,
    set_interactive_mode,
)
from .cleanup import clear_topic_state
from .message_queue import (
    enqueue_interactive_ui,
    enqueue_status_update,
    get_message_queue,
)

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe interval
TOPIC_CHECK_INTERVAL = 60.0  # seconds


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    # Pane-as-source state machine: the pane content is the single source of
    # truth for whether an interactive UI is visible. status_polling enqueues
    # an `interactive_ui` task on the per-user message queue whenever the
    # pane first shows a UI, or the UI has morphed in place (different name
    # than what we last delivered). The worker drains the queue FIFO so any
    # text/thinking already in flight lands first.
    content = extract_interactive_content(pane_text)
    interactive_window = get_interactive_window(user_id, thread_id)

    if content is not None:
        if content.name == "Feedback":
            # Auto-dismiss feedback survey by pressing "0" (Dismiss)
            await tmux_manager.send_keys(window_id, "0", enter=False, literal=False)
            logger.info("Auto-dismissed feedback survey in window %s", window_id)
            return

        # Stale mode pointing at another window — clear before enqueueing this one.
        if interactive_window is not None and interactive_window != window_id:
            await clear_interactive_msg(user_id, bot, thread_id)

        if is_interactive_enqueued(user_id, thread_id):
            # Worker will dispatch shortly; don't double-enqueue.
            return

        # No `await` between the gate reads below and `mark_interactive_enqueued`
        # — atomic from the event loop's POV, so the worker cannot clear the
        # flag mid-check and cause a duplicate enqueue.
        msg_id = get_interactive_msg_id(user_id, thread_id)
        last_name = get_interactive_last_name(user_id, thread_id)
        if msg_id is None or last_name != content.name:
            # First delivery for this UI, or pane morphed to a new UI type.
            set_interactive_mode(user_id, window_id, thread_id)
            mark_interactive_enqueued(user_id, thread_id)
            await enqueue_interactive_ui(bot, user_id, window_id, thread_id=thread_id)
        return

    # No interactive UI in pane.
    if interactive_window == window_id:
        # UI was delivered and is now gone — delete the Telegram message
        # and reset state. Fall through to the status-line check below.
        await clear_interactive_msg(user_id, bot, thread_id)
    elif interactive_window is not None:
        # Stale mode for a different window — clean it up.
        await clear_interactive_msg(user_id, bot, thread_id)

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    status_line = parse_status_line(pane_text)

    if status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    while True:
        try:
            # Periodic topic existence probe
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for user_id, thread_id, wid in list(
                    session_manager.iter_thread_bindings()
                ):
                    try:
                        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
                        # reopen_forum_topic is a silent probe:
                        # - open topic → BadRequest("Topic_not_modified")
                        # - deleted topic → BadRequest("Topic_id_invalid")
                        await bot.reopen_forum_topic(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        err = str(e)
                        if "not_modified" in err.lower():
                            # Topic exists and is open — no-op
                            continue
                        if "thread not found" in err or "Topic_id_invalid" in err:
                            # Topic deleted — kill window, unbind, and clean up
                            w = await tmux_manager.find_window_by_id(wid)
                            if w:
                                await tmux_manager.kill_window(w.window_id)
                            session_manager.unbind_thread(user_id, thread_id)
                            await clear_topic_state(user_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: killed window_id '%s' and "
                                "unbound thread %d for user %d",
                                wid,
                                thread_id,
                                user_id,
                            )
                        else:
                            logger.debug(
                                "Topic probe error for %s: %s",
                                wid,
                                e,
                            )
                    except Exception as e:
                        logger.debug(
                            "Topic probe error for %s: %s",
                            wid,
                            e,
                        )

            for user_id, thread_id, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Clean up stale bindings (window no longer exists)
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        session_manager.unbind_thread(user_id, thread_id)
                        await clear_topic_state(user_id, thread_id, bot)
                        logger.info(
                            "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                            user_id,
                            thread_id,
                            wid,
                        )
                        continue

                    queue = get_message_queue(user_id, thread_id)
                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    skip_status = queue is not None and not queue.empty()
                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
