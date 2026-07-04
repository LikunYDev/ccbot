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
def _clear_status_msg_info():
    """Reset _status_msg_info between tests so tracking doesn't leak."""
    from ccbot.handlers import message_queue as mq

    mq._status_msg_info.clear()
    yield
    mq._status_msg_info.clear()


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


@pytest.mark.usefixtures("_clear_queue_state")
class TestDrainQueues:
    """drain_queues() lets live workers flush enqueued-but-unsent tasks before
    shutdown_workers() cancels them — the delivery guarantee on restart."""

    @pytest.mark.asyncio
    async def test_waits_for_enqueued_message_to_send(self):
        import asyncio

        from ccbot.handlers.message_queue import (
            drain_queues,
            enqueue_content_message,
            get_or_create_queue,
        )

        bot = AsyncMock()
        sent: list[str] = []

        async def fake_send(*args, **kwargs):
            await asyncio.sleep(0.02)  # simulate a slow network send
            sent.append("content")
            m = MagicMock()
            m.message_id = 1
            return m

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.send_with_fallback", new=fake_send),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            get_or_create_queue(bot, user_id=7, thread_id=42)
            await enqueue_content_message(
                bot,
                user_id=7,
                window_id="@5",
                parts=["hi"],
                content_type="text",
                thread_id=42,
            )
            # drain must block until the slow send actually completes.
            await drain_queues(timeout=5.0)

        assert sent == ["content"]

    @pytest.mark.asyncio
    async def test_no_queues_is_noop(self):
        from ccbot.handlers.message_queue import drain_queues

        await drain_queues()  # returns promptly with nothing queued

    @pytest.mark.asyncio
    async def test_times_out_without_hanging(self):
        import asyncio

        from ccbot.handlers import message_queue as mq
        from ccbot.handlers.message_queue import (
            drain_queues,
            enqueue_content_message,
            get_or_create_queue,
        )

        bot = AsyncMock()

        async def never_send(*args, **kwargs):
            await asyncio.sleep(10)
            return MagicMock()

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.send_with_fallback", new=never_send),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            get_or_create_queue(bot, user_id=7, thread_id=42)
            await enqueue_content_message(
                bot,
                user_id=7,
                window_id="@5",
                parts=["hi"],
                content_type="text",
                thread_id=42,
            )
            try:
                # Returns despite the hung send, instead of blocking forever.
                await drain_queues(timeout=0.05)
            finally:
                for w in list(mq._queue_workers.values()):
                    w.cancel()


@pytest.mark.usefixtures("_clear_queue_state")
class TestContentRetryAndFailureNotice:
    """Deliver-or-loudly-drop: RetryAfter retries a content/interactive_ui
    task in place (bounded), and a content task that is ultimately dropped
    gets a best-effort user-visible notice instead of vanishing silently."""

    @pytest.mark.asyncio
    async def test_retries_retryafter_in_place_then_succeeds_fifo_preserved(self):
        """A single RetryAfter is retried in place and delivers exactly one
        message. A second task enqueued only after the retry has begun must
        still be processed strictly after the first — FIFO is preserved."""
        import asyncio

        from telegram.error import RetryAfter

        from ccbot.handlers.message_queue import (
            enqueue_content_message,
            get_or_create_queue,
        )

        bot = AsyncMock()
        sent: list[str] = []
        call_count = 0
        first_attempt_started = asyncio.Event()

        async def fake_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_attempt_started.set()
                raise RetryAfter(retry_after=0)
            text = args[2]
            sent.append(text)
            m = MagicMock()
            m.message_id = 1
            return m

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.send_with_fallback", new=fake_send),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            queue = get_or_create_queue(bot, user_id=7, thread_id=42)

            await enqueue_content_message(
                bot,
                user_id=7,
                window_id="@5",
                parts=["hello1"],
                content_type="text",
                thread_id=42,
            )
            # Wait until the first send attempt has actually happened (and
            # raised) before enqueuing the second task, so the two are never
            # merged and the second genuinely arrives "after".
            await asyncio.wait_for(first_attempt_started.wait(), timeout=5.0)
            await enqueue_content_message(
                bot,
                user_id=7,
                window_id="@5",
                parts=["hello2"],
                content_type="text",
                thread_id=42,
            )
            await asyncio.wait_for(queue.join(), timeout=5.0)

        assert sent == ["hello1", "hello2"]
        assert call_count == 3  # hello1 fails once then succeeds, then hello2
        bot.send_message.assert_not_called()  # no failure notice — it delivered

    @pytest.mark.asyncio
    async def test_drops_content_after_max_retries_with_error_log_and_notice(
        self, caplog
    ):
        """A send that always raises RetryAfter is retried up to the cap,
        then dropped with an error log and a best-effort failure notice."""
        import asyncio
        import logging

        from telegram.error import RetryAfter

        from ccbot.handlers.message_queue import (
            DELIVERY_FAILURE_NOTICE,
            MAX_CONTENT_RETRY_ATTEMPTS,
            enqueue_content_message,
            get_or_create_queue,
        )

        bot = AsyncMock()
        call_count = 0

        async def always_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RetryAfter(retry_after=0)

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.send_with_fallback", new=always_fail),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            queue = get_or_create_queue(bot, user_id=7, thread_id=42)

            with caplog.at_level(logging.ERROR, logger="ccbot.handlers.message_queue"):
                await enqueue_content_message(
                    bot,
                    user_id=7,
                    window_id="@5",
                    parts=["hello"],
                    content_type="text",
                    thread_id=42,
                )
                await asyncio.wait_for(queue.join(), timeout=5.0)

        assert call_count == MAX_CONTENT_RETRY_ATTEMPTS
        assert any(
            "Giving up on content task" in record.message for record in caplog.records
        )
        bot.send_message.assert_awaited_once_with(
            chat_id=100,
            text=DELIVERY_FAILURE_NOTICE,
            message_thread_id=42,
        )

    @pytest.mark.asyncio
    async def test_merged_batch_survives_transient_retryafter(self):
        """Three mergeable content tasks folded into one send: a RetryAfter
        on the first attempt must not discard the merged batch — join()
        completes and every part is actually delivered."""
        import asyncio

        from telegram.error import RetryAfter

        from ccbot.handlers.message_queue import (
            enqueue_content_message,
            get_or_create_queue,
        )

        bot = AsyncMock()
        sent: list[str] = []
        call_count = 0

        async def fake_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RetryAfter(retry_after=0)
            text = args[2]
            sent.append(text)
            m = MagicMock()
            m.message_id = 1
            return m

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.send_with_fallback", new=fake_send),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            queue = get_or_create_queue(bot, user_id=7, thread_id=42)

            for i in range(3):
                await enqueue_content_message(
                    bot,
                    user_id=7,
                    window_id="@5",
                    parts=[f"part{i}"],
                    content_type="text",
                    thread_id=42,
                )
            await asyncio.wait_for(queue.join(), timeout=5.0)

        assert sent == ["part0", "part1", "part2"]
        assert queue.qsize() == 0
        bot.send_message.assert_not_called()  # delivered — no failure notice

    @pytest.mark.asyncio
    async def test_generic_exception_on_content_notifies_and_worker_continues(self):
        """A non-RetryAfter Exception on a content send is dropped with a
        failure notice, and the worker keeps processing the next task."""
        import asyncio

        from ccbot.handlers.message_queue import (
            DELIVERY_FAILURE_NOTICE,
            enqueue_content_message,
            get_or_create_queue,
        )

        bot = AsyncMock()
        attempted: list[str] = []

        async def flaky_send(*args, **kwargs):
            text = args[2]
            attempted.append(text)
            if text == "boom":
                raise ValueError("kaboom")
            m = MagicMock()
            m.message_id = 1
            return m

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.send_with_fallback", new=flaky_send),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            queue = get_or_create_queue(bot, user_id=7, thread_id=42)

            # Different window_ids so the two tasks are never merged — this
            # pins "worker continues with the next task" as a separate task.
            await enqueue_content_message(
                bot,
                user_id=7,
                window_id="@5",
                parts=["boom"],
                content_type="text",
                thread_id=42,
            )
            await enqueue_content_message(
                bot,
                user_id=7,
                window_id="@6",
                parts=["ok"],
                content_type="text",
                thread_id=42,
            )
            await asyncio.wait_for(queue.join(), timeout=5.0)

        assert attempted == ["boom", "ok"]
        bot.send_message.assert_awaited_once_with(
            chat_id=100,
            text=DELIVERY_FAILURE_NOTICE,
            message_thread_id=42,
        )


@pytest.mark.usefixtures("_clear_status_msg_info")
class TestConvertStatusToContentRace:
    """f53: `_convert_status_to_content` must not pop `_status_msg_info`
    until the outstanding edit has actually resolved. Popping it up front
    (before awaiting the edit) lets a concurrent `enqueue_status_update`
    dedup read see nothing mid-flight, skip the dedup, and resurrect a
    duplicate status message that never gets cleared."""

    @pytest.mark.asyncio
    async def test_entry_stays_visible_until_edit_resolves(self):
        """Simulate a slow in-flight edit with an asyncio.Event and assert
        the tracking entry is still readable by a concurrent caller while
        the edit is outstanding, then confirmed popped once it succeeds."""
        import asyncio

        from ccbot.handlers import message_queue as mq

        bot = AsyncMock()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_edit(*args, **kwargs):
            entered.set()
            await release.wait()
            return True

        skey = (7, 42)
        mq._status_msg_info[skey] = (11, "@5", "Thinking…")

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.edit_with_fallback", new=slow_edit),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            task = asyncio.create_task(
                mq._convert_status_to_content(bot, 7, 42, "@5", "new content")
            )
            await asyncio.wait_for(entered.wait(), timeout=5.0)

            # Mid-flight: a concurrent enqueue_status_update dedup read must
            # still see the entry (this IS that read, since it uses .get()).
            assert mq._status_msg_info.get(skey) == (11, "@5", "Thinking…")

            release.set()
            result = await asyncio.wait_for(task, timeout=5.0)

        assert result == 11
        # Consumed: popped only after the edit actually resolved.
        assert skey not in mq._status_msg_info

    @pytest.mark.asyncio
    async def test_pops_entry_on_total_edit_failure(self):
        """When both edit attempts fail (edit_with_fallback returns False),
        the tracked message is dead either way, so the entry must still be
        popped — the caller sends a fresh message."""
        from ccbot.handlers import message_queue as mq

        bot = AsyncMock()
        skey = (7, 42)
        mq._status_msg_info[skey] = (11, "@5", "Thinking…")

        async def failing_edit(*args, **kwargs):
            return False

        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.edit_with_fallback", new=failing_edit),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            result = await mq._convert_status_to_content(
                bot, 7, 42, "@5", "new content"
            )

        assert result is None
        assert skey not in mq._status_msg_info

    @pytest.mark.asyncio
    async def test_pops_entry_on_different_window_delete(self):
        """Stored status belongs to a different window: the old status is
        deleted (not converted) and the entry must still be popped."""
        from ccbot.handlers import message_queue as mq

        bot = AsyncMock()
        skey = (7, 42)
        mq._status_msg_info[skey] = (11, "@5", "Thinking…")

        with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            result = await mq._convert_status_to_content(
                bot, 7, 42, "@6", "new content"
            )

        assert result is None
        assert skey not in mq._status_msg_info
        bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=11)
