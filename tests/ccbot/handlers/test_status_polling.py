"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.status_polling import update_status_message


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import (
        _interactive_enqueued,
        _interactive_last_name,
        _interactive_mode,
        _interactive_msgs,
    )

    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_enqueued.clear()
    _interactive_last_name.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_enqueued.clear()
    _interactive_last_name.clear()


def _simulate_worker_dispatched(
    user_id: int, thread_id: int, ui_name: str, msg_id: int = 12345
):
    """Mutate the interactive-UI state dicts as if the message_queue worker
    had picked up an `interactive_ui` task and successfully delivered it.
    Use between poll cycles in tests to model the async dispatch the test
    harness does not actually run."""
    from ccbot.handlers.interactive_ui import (
        _interactive_enqueued,
        _interactive_last_name,
        _interactive_msgs,
    )

    ikey = (user_id, thread_id)
    _interactive_enqueued.discard(ikey)
    _interactive_msgs[ikey] = msg_id
    _interactive_last_name[ikey] = ui_name


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → enqueues an interactive_ui task on
        the per-user message queue. The worker (tested separately) is what
        ultimately calls handle_interactive_ui."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue.assert_called_once_with(mock_bot, 1, window_id, thread_id=42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no enqueue_interactive_ui call, just status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue.assert_not_called()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerInteractiveUIDetection:
    """Pane-as-source design: status poller detects ANY interactive UI in the
    pane and enqueues a delivery task on the per-user message queue. The
    poller no longer special-cases AskUserQuestion/ExitPlanMode by deferring
    to JSONL — pane is the trigger as well as the content source."""

    @pytest.mark.asyncio
    async def test_exit_plan_enqueues_regardless_of_session(
        self, mock_bot: AsyncMock, sample_pane_exit_plan_numbered: str
    ):
        """ExitPlanMode in pane → enqueue. The previous 'defer to JSONL when
        session_id is set' branch is gone — pane is the source of truth."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue,
            patch(
                "ccbot.handlers.status_polling.set_interactive_mode",
            ) as mock_set_mode,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value=sample_pane_exit_plan_numbered
            )

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_set_mode.assert_called_once_with(1, window_id, 42)
            mock_enqueue.assert_called_once_with(mock_bot, 1, window_id, thread_id=42)

    @pytest.mark.asyncio
    async def test_permission_prompt_enqueues(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        """PermissionPrompt in pane → enqueue."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue.assert_called_once_with(mock_bot, 1, window_id, thread_id=42)

    @pytest.mark.asyncio
    async def test_ask_user_question_enqueues_after_simulated_bot_restart(
        self,
        mock_bot: AsyncMock,
        sample_pane_ask_user_single_tab: str,
    ):
        """Regression for the byte-offset-past-tool_use silent failure:
        clean interactive state (as if bot just restarted) + pane shows
        AskUserQuestion → the poller must enqueue. JSONL is irrelevant."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value=sample_pane_ask_user_single_tab
            )

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_enqueue.assert_called_once_with(mock_bot, 1, window_id, thread_id=42)


@pytest.mark.usefixtures("_clear_interactive_state")
class TestPaneAsSourceInteractiveUI:
    """Pane-as-source state machine: status_polling enqueues an interactive_ui
    task whenever the pane shows a UI that is new or has morphed (different
    UI name than last delivered). Once enqueued, repeat polls are no-ops
    until the worker dispatches and pane state changes again.
    """

    WIN = "@5"
    USER = 1
    THREAD = 42

    @pytest.fixture
    def mock_window(self):
        w = MagicMock()
        w.window_id = self.WIN
        return w

    async def _poll(self, mock_bot, pane_text, mock_window):
        """Drive one poll cycle and return the patched enqueue_interactive_ui
        mock so tests can assert whether (and how) a delivery was scheduled."""
        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)

            await update_status_message(
                mock_bot,
                user_id=self.USER,
                window_id=self.WIN,
                thread_id=self.THREAD,
            )
            return mock_enqueue

    @pytest.mark.asyncio
    async def test_first_poll_enqueues_once(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
    ):
        """Pane shows ExitPlanMode for the first time → exactly one enqueue.
        Mode is set, enqueued flag is set (worker hasn't dispatched yet)."""
        from ccbot.handlers.interactive_ui import (
            _interactive_enqueued,
            _interactive_mode,
            _interactive_msgs,
        )

        enqueue = await self._poll(
            mock_bot, sample_pane_exit_plan_numbered, mock_window
        )

        enqueue.assert_called_once_with(
            mock_bot, self.USER, self.WIN, thread_id=self.THREAD
        )
        assert _interactive_mode[(self.USER, self.THREAD)] == self.WIN
        assert (self.USER, self.THREAD) in _interactive_enqueued
        # Worker hasn't run in this test harness — msg_id stays unset.
        assert (self.USER, self.THREAD) not in _interactive_msgs

    @pytest.mark.asyncio
    async def test_repeated_polls_same_ui_enqueue_once(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
    ):
        """Idempotency: across three polls of the same UI, exactly one
        enqueue. The in-flight `_interactive_enqueued` flag blocks the second
        cycle; after the (simulated) worker dispatches, the
        `last_name == content.name` check blocks the third."""
        # Cycle 1: enqueue
        enqueue1 = await self._poll(
            mock_bot, sample_pane_exit_plan_numbered, mock_window
        )
        enqueue1.assert_called_once()

        # Cycle 2: enqueued flag still set, worker hasn't dispatched → no-op
        enqueue2 = await self._poll(
            mock_bot, sample_pane_exit_plan_numbered, mock_window
        )
        enqueue2.assert_not_called()

        # Simulate worker dispatching the task (sets msg_id, last_name; clears flag)
        _simulate_worker_dispatched(self.USER, self.THREAD, "ExitPlanMode")

        # Cycle 3: msg_id set + last_name matches → no-op
        enqueue3 = await self._poll(
            mock_bot, sample_pane_exit_plan_numbered, mock_window
        )
        enqueue3.assert_not_called()

    @pytest.mark.asyncio
    async def test_morph_re_enqueues(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
        sample_pane_permission: str,
    ):
        """The observed production bug, in the new model.

        Cycle 1: ExitPlanMode in pane → enqueue. Worker dispatches.
        Cycle 2: pane morphed to PermissionPrompt → re-enqueue so the worker
        can edit the Telegram message with the new UI content."""
        enqueue1 = await self._poll(
            mock_bot, sample_pane_exit_plan_numbered, mock_window
        )
        enqueue1.assert_called_once()
        _simulate_worker_dispatched(self.USER, self.THREAD, "ExitPlanMode")

        enqueue2 = await self._poll(mock_bot, sample_pane_permission, mock_window)
        enqueue2.assert_called_once_with(
            mock_bot, self.USER, self.WIN, thread_id=self.THREAD
        )

    @pytest.mark.asyncio
    async def test_permission_still_showing_no_duplicate_send(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_permission: str,
    ):
        """msg_id set + last_name matches current UI → no re-enqueue. Guards
        against Telegram API flooding when the user is just looking at a
        prompt and not acting on it."""
        # Simulate previous successful delivery
        _simulate_worker_dispatched(self.USER, self.THREAD, "PermissionPrompt")

        enqueue = await self._poll(mock_bot, sample_pane_permission, mock_window)
        enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_bash_approval_after_exit_plan_re_enqueues(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
    ):
        """Pane morph from ExitPlanMode to BashApproval must re-enqueue —
        same class of bug as the ExitPlanMode → PermissionPrompt case."""
        bash_approval_pane = (
            "  Bash command\n"
            "  This command requires approval\n"
            "  ls /\n"
            "  Esc to cancel\n"
        )
        enqueue1 = await self._poll(
            mock_bot, sample_pane_exit_plan_numbered, mock_window
        )
        enqueue1.assert_called_once()
        _simulate_worker_dispatched(self.USER, self.THREAD, "ExitPlanMode")

        enqueue2 = await self._poll(mock_bot, bash_approval_pane, mock_window)
        enqueue2.assert_called_once_with(
            mock_bot, self.USER, self.WIN, thread_id=self.THREAD
        )

    @pytest.mark.asyncio
    async def test_ui_gone_clears_state(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
        sample_pane_no_ui: str,
    ):
        """When mode is set (UI was previously delivered) but pane no longer
        shows a UI, clear_interactive_msg must be called to remove the
        Telegram message and reset all state."""
        # Simulate a previous successful delivery so all flags are populated.
        await self._poll(mock_bot, sample_pane_exit_plan_numbered, mock_window)
        _simulate_worker_dispatched(self.USER, self.THREAD, "ExitPlanMode")

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.status_polling.clear_interactive_msg",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_no_ui)

            await update_status_message(
                mock_bot,
                user_id=self.USER,
                window_id=self.WIN,
                thread_id=self.THREAD,
            )
            mock_clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_full_lifecycle_plan_prompt_dismiss_another_prompt(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
        sample_pane_permission: str,
        sample_pane_no_ui: str,
    ):
        """Full lifecycle through the pane-as-source state machine:
        ExitPlanMode delivered → morphs to PermissionPrompt (re-enqueue) →
        user dismisses (clear) → fresh PermissionPrompt later (enqueue again).
        """
        from ccbot.handlers.interactive_ui import (
            _interactive_enqueued,
            _interactive_last_name,
            _interactive_mode,
            _interactive_msgs,
        )

        # Step 1: ExitPlanMode → enqueue + dispatch
        h = await self._poll(mock_bot, sample_pane_exit_plan_numbered, mock_window)
        h.assert_called_once()
        _simulate_worker_dispatched(self.USER, self.THREAD, "ExitPlanMode")

        # Step 2: Pane morphs to PermissionPrompt → re-enqueue
        h = await self._poll(mock_bot, sample_pane_permission, mock_window)
        h.assert_called_once()
        _simulate_worker_dispatched(
            self.USER, self.THREAD, "PermissionPrompt", msg_id=500
        )

        # Step 3: dismiss → pane idle → clear
        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.status_polling.clear_interactive_msg",
                new_callable=AsyncMock,
                side_effect=lambda *_args, **_kwargs: (
                    _interactive_mode.pop((self.USER, self.THREAD), None),
                    _interactive_msgs.pop((self.USER, self.THREAD), None),
                    _interactive_last_name.pop((self.USER, self.THREAD), None),
                    _interactive_enqueued.discard((self.USER, self.THREAD)),
                ),
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_no_ui)
            await update_status_message(
                mock_bot,
                user_id=self.USER,
                window_id=self.WIN,
                thread_id=self.THREAD,
            )
        assert (self.USER, self.THREAD) not in _interactive_mode
        assert (self.USER, self.THREAD) not in _interactive_msgs
        assert (self.USER, self.THREAD) not in _interactive_last_name

        # Step 4: fresh PermissionPrompt later → enqueue again
        h = await self._poll(mock_bot, sample_pane_permission, mock_window)
        h.assert_called_once()

    @pytest.mark.asyncio
    async def test_stale_mode_for_different_window_does_not_block_enqueue(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_permission: str,
    ):
        """If `_interactive_mode` is set for a DIFFERENT window (left over
        from another topic), polling this window with a PermissionPrompt must
        clear the stale state and still enqueue the new UI."""
        from ccbot.handlers.interactive_ui import _interactive_mode

        _interactive_mode[(self.USER, self.THREAD)] = "@99"  # different window

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_enqueue,
            patch(
                "ccbot.handlers.status_polling.clear_interactive_msg",
                new_callable=AsyncMock,
            ) as mock_clear,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)

            await update_status_message(
                mock_bot,
                user_id=self.USER,
                window_id=self.WIN,
                thread_id=self.THREAD,
            )
            mock_clear.assert_called_once()
            mock_enqueue.assert_called_once_with(
                mock_bot, self.USER, self.WIN, thread_id=self.THREAD
            )
