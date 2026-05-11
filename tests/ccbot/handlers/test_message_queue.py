"""Tests for message_queue — status stats stripping for dedup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.message_queue import _strip_status_stats


class TestStripStatusStats:
    @pytest.mark.parametrize(
        ("input_text", "expected"),
        [
            pytest.param(
                "Thinking… (45s · ↓ 2.5k tokens · thought for 25s)",
                "Thinking…",
                id="seconds_only",
            ),
            pytest.param(
                "Enchanting… (2m 9s · ↓ 8.1k tokens · thought for 49s)",
                "Enchanting…",
                id="minutes_and_seconds",
            ),
            pytest.param(
                "Working… (1h 2m 3s · ↓ 50k tokens)",
                "Working…",
                id="hours_minutes_seconds",
            ),
            pytest.param(
                "Just text without stats",
                "Just text without stats",
                id="no_parenthetical",
            ),
            pytest.param(
                "Idle…",
                "Idle…",
                id="no_stats",
            ),
            pytest.param(
                "Germinating… (30s · ↓ 897 tokens · thought for 2s) Esc to interrupt",
                "Germinating…",
                id="with_trailing_esc",
            ),
            pytest.param(
                "Thinking… (2m 9s · ↓ 8.1k tokens) Esc to interrupt",
                "Thinking…",
                id="minutes_with_trailing_esc",
            ),
        ],
    )
    def test_strip_status_stats(self, input_text: str, expected: str):
        assert _strip_status_stats(input_text) == expected


@pytest.fixture
def _clear_queue_state():
    """Reset module-level queue dicts between tests so workers from previous
    tests don't leak."""
    from ccbot.handlers import message_queue as mq

    mq._message_queues.clear()
    mq._queue_workers.clear()
    mq._queue_locks.clear()
    mq._group_process_locks.clear()
    yield
    mq._message_queues.clear()
    mq._queue_workers.clear()
    mq._queue_locks.clear()
    mq._group_process_locks.clear()


@pytest.fixture
def _clear_enqueued_flag():
    """Reset _interactive_enqueued between tests."""
    from ccbot.handlers.interactive_ui import _interactive_enqueued

    _interactive_enqueued.clear()
    yield
    _interactive_enqueued.clear()


@pytest.mark.usefixtures("_clear_queue_state", "_clear_enqueued_flag")
class TestInteractiveUITask:
    """The pane-as-source design routes interactive UI delivery through the
    per-user message queue. These tests pin the contract: enqueue creates a
    properly-shaped task, the worker dispatches it via handle_interactive_ui,
    and the in-flight enqueue flag is cleared on dispatch."""

    @pytest.mark.asyncio
    async def test_enqueue_interactive_ui_puts_task_on_queue(self):
        from ccbot.handlers.message_queue import (
            enqueue_interactive_ui,
            get_message_queue,
        )

        bot = AsyncMock()
        # session_manager.resolve_chat_id is invoked when the worker starts.
        # Patch it so the worker doesn't blow up; we won't await any sends here.
        with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100

            await enqueue_interactive_ui(bot, user_id=7, window_id="@5", thread_id=42)

            q = get_message_queue(7, 42)
            assert q is not None
            task = q.get_nowait()
            q.task_done()

        assert task.task_type == "interactive_ui"
        assert task.window_id == "@5"
        assert task.thread_id == 42

    @pytest.mark.asyncio
    async def test_process_interactive_ui_task_dispatches_and_clears_flag(self):
        """Direct unit test on the new worker branch: must call
        handle_interactive_ui with the task's window/thread and must clear
        the in-flight enqueue flag so the next pane render can re-enqueue."""
        from ccbot.handlers.interactive_ui import (
            is_interactive_enqueued,
            mark_interactive_enqueued,
        )
        from ccbot.handlers.message_queue import (
            MessageTask,
            _process_interactive_ui_task,
        )

        bot = AsyncMock()
        mark_interactive_enqueued(7, 42)
        task = MessageTask(
            task_type="interactive_ui",
            window_id="@5",
            thread_id=42,
        )

        with patch(
            "ccbot.handlers.message_queue.handle_interactive_ui",
            new_callable=AsyncMock,
        ) as mock_handle_ui:
            mock_handle_ui.return_value = True
            await _process_interactive_ui_task(bot, user_id=7, task=task)

        mock_handle_ui.assert_awaited_once_with(bot, 7, "@5", 42)
        assert is_interactive_enqueued(7, 42) is False

    @pytest.mark.asyncio
    async def test_process_interactive_ui_clears_flag_even_on_failure(self):
        """If handle_interactive_ui returns False (pane race), the flag must
        still be cleared so the next 1-second poll can re-enqueue."""
        from ccbot.handlers.interactive_ui import (
            is_interactive_enqueued,
            mark_interactive_enqueued,
        )
        from ccbot.handlers.message_queue import (
            MessageTask,
            _process_interactive_ui_task,
        )

        bot = AsyncMock()
        mark_interactive_enqueued(7, 42)
        task = MessageTask(
            task_type="interactive_ui",
            window_id="@5",
            thread_id=42,
        )

        with patch(
            "ccbot.handlers.message_queue.handle_interactive_ui",
            new_callable=AsyncMock,
        ) as mock_handle_ui:
            mock_handle_ui.return_value = False
            await _process_interactive_ui_task(bot, user_id=7, task=task)

        assert is_interactive_enqueued(7, 42) is False

    @pytest.mark.asyncio
    async def test_worker_dispatches_interactive_ui_task_in_fifo_order(self):
        """End-to-end through the worker: text content enqueued first, then an
        interactive_ui task. Worker must process content before UI — this is
        the ordering guarantee that replaces today's queue.join() barrier in
        bot.py."""
        import asyncio

        from ccbot.handlers.message_queue import (
            enqueue_content_message,
            enqueue_interactive_ui,
            get_or_create_queue,
        )

        bot = AsyncMock()
        call_order: list[str] = []

        async def fake_send(*args, **kwargs):
            call_order.append("content")
            sent = MagicMock()
            sent.message_id = 1
            return sent

        async def fake_handle_ui(*args, **kwargs):
            call_order.append("interactive_ui")
            return True

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new=fake_send,
            ),
            patch(
                "ccbot.handlers.message_queue.handle_interactive_ui",
                new=fake_handle_ui,
            ),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            queue = get_or_create_queue(bot, user_id=7, thread_id=42)

            await enqueue_content_message(
                bot,
                user_id=7,
                window_id="@5",
                parts=["hello"],
                content_type="text",
                thread_id=42,
            )
            await enqueue_interactive_ui(bot, user_id=7, window_id="@5", thread_id=42)

            # Drain via queue.join — worker runs in background, processes FIFO.
            await asyncio.wait_for(queue.join(), timeout=5.0)

        assert call_order == ["content", "interactive_ui"]

    @pytest.mark.asyncio
    async def test_interactive_ui_task_not_dropped_during_flood_control(self):
        """Worker must NOT drop interactive_ui tasks during flood control —
        dropping bypasses _process_interactive_ui_task's `finally`, leaks the
        `_interactive_enqueued` flag, and silently breaks every subsequent
        status_polling re-enqueue attempt for this topic."""
        import asyncio
        import time

        from ccbot.handlers.interactive_ui import (
            is_interactive_enqueued,
            mark_interactive_enqueued,
        )
        from ccbot.handlers.message_queue import (
            _flood_until,
            enqueue_interactive_ui,
            get_or_create_queue,
        )

        bot = AsyncMock()
        handle_calls: list[tuple] = []

        async def fake_handle_ui(*args, **kwargs):
            handle_calls.append(args)
            return True

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.handle_interactive_ui",
                new=fake_handle_ui,
            ),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            queue = get_or_create_queue(bot, user_id=7, thread_id=42)
            # Simulate a very short flood-control window
            _flood_until[(7, 42)] = time.monotonic() + 0.1

            mark_interactive_enqueued(7, 42)
            await enqueue_interactive_ui(bot, user_id=7, window_id="@5", thread_id=42)
            await asyncio.wait_for(queue.join(), timeout=5.0)

        # The task was waited and processed (not silently dropped).
        assert len(handle_calls) == 1
        # And the in-flight flag was cleared so the next poll can re-enqueue.
        assert is_interactive_enqueued(7, 42) is False
