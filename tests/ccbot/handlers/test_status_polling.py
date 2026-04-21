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
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


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
        """Poller captures Settings pane → handle_interactive_ui sends keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
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
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
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

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → handle_interactive_ui
        → bot.send_message with keyboard.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            # Verify bot.send_message was called with keyboard
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert call_kwargs["message_thread_id"] == 42
            keyboard = call_kwargs["reply_markup"]
            assert keyboard is not None
            # Verify the message text contains model picker content
            assert "Select model" in call_kwargs["text"]


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerExitPlanDetection:
    """Simulate the status poller detecting ExitPlanMode UI (numbered format)."""

    @pytest.mark.asyncio
    async def test_exit_plan_with_session_defers_to_jsonl(
        self, mock_bot: AsyncMock, sample_pane_exit_plan_numbered: str
    ):
        """ExitPlanMode with active session → set_interactive_mode, no handle_interactive_ui."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        mock_ws = MagicMock()
        mock_ws.session_id = "uuid-xxx"

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.set_interactive_mode",
            ) as mock_set_mode,
            patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value=sample_pane_exit_plan_numbered
            )
            mock_sm.get_window_state.return_value = mock_ws

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_set_mode.assert_called_once_with(1, window_id, 42)
            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_plan_without_session_sends_immediately(
        self, mock_bot: AsyncMock, sample_pane_exit_plan_numbered: str
    ):
        """ExitPlanMode with no session_id → handle_interactive_ui called (fallback)."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        mock_ws = MagicMock()
        mock_ws.session_id = ""

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value=sample_pane_exit_plan_numbered
            )
            mock_sm.get_window_state.return_value = mock_ws
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_permission_prompt_always_sends_immediately(
        self, mock_bot: AsyncMock, sample_pane_permission: str
    ):
        """PermissionPrompt → handle_interactive_ui called regardless of session_id."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        mock_ws = MagicMock()
        mock_ws.session_id = "uuid-xxx"

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)
            mock_sm.get_window_state.return_value = mock_ws
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStickyInteractiveModeRescue:
    """Regression coverage for the 'ExitPlanMode → PermissionPrompt transition'
    bug: when _interactive_mode was set via the JSONL-deferred branch but no
    Telegram message was ever sent, a subsequent PermissionPrompt in the same
    pane must still be forwarded — the sticky mode flag must not swallow it.
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
        """Drive one poll cycle and return the patched handle_interactive_ui
        mock plus set_interactive_mode mock."""
        mock_ws = MagicMock()
        mock_ws.session_id = "uuid-xxx"
        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
            mock_sm.get_window_state.return_value = mock_ws
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot,
                user_id=self.USER,
                window_id=self.WIN,
                thread_id=self.THREAD,
            )
            return mock_handle_ui

    @pytest.mark.asyncio
    async def test_exit_plan_then_permission_transition_sends(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
        sample_pane_permission: str,
    ):
        """The observed production bug.

        Cycle 1: ExitPlanMode pane → _interactive_mode set, no send (JSONL path).
        Cycle 2: pane morphs to PermissionPrompt → must send; _interactive_mode
        is already set for this window but _interactive_msgs is empty.
        """
        from ccbot.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        # Cycle 1: JSONL-handled UI — mode-only, no send
        handle_ui = await self._poll(
            mock_bot, sample_pane_exit_plan_numbered, mock_window
        )
        handle_ui.assert_not_called()
        assert _interactive_mode[(self.USER, self.THREAD)] == self.WIN
        assert (self.USER, self.THREAD) not in _interactive_msgs

        # Cycle 2: pane morphed to PermissionPrompt — fix must fire
        handle_ui = await self._poll(mock_bot, sample_pane_permission, mock_window)
        handle_ui.assert_called_once_with(mock_bot, self.USER, self.WIN, self.THREAD)

    @pytest.mark.asyncio
    async def test_exit_plan_then_still_exit_plan_stays_silent(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
    ):
        """AskUserQuestion/ExitPlanMode must remain JSONL-only, even if mode is
        already set but no msg exists — otherwise the poll path starts
        double-sending."""
        from ccbot.handlers.interactive_ui import _interactive_mode

        # Cycle 1
        await self._poll(mock_bot, sample_pane_exit_plan_numbered, mock_window)
        # Cycle 2: same UI still there
        handle_ui = await self._poll(
            mock_bot, sample_pane_exit_plan_numbered, mock_window
        )
        handle_ui.assert_not_called()
        assert _interactive_mode[(self.USER, self.THREAD)] == self.WIN

    @pytest.mark.asyncio
    async def test_permission_still_showing_no_duplicate_send(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_permission: str,
    ):
        """Once a PermissionPrompt is sent (mode + msg both set), subsequent
        polls while the same prompt is visible must NOT re-send — preventing
        Telegram API flooding."""
        from ccbot.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        # Simulate that a previous poll already forwarded the PermissionPrompt:
        # both mode and msg are populated (as the real handle_interactive_ui
        # would have done).
        _interactive_mode[(self.USER, self.THREAD)] = self.WIN
        _interactive_msgs[(self.USER, self.THREAD)] = 12345

        # Next poll: same prompt still visible — must not resend
        handle_ui = await self._poll(mock_bot, sample_pane_permission, mock_window)
        handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_bash_approval_rescued_after_mode_set(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
    ):
        """Same class of bug for BashApproval — pane morphed from an
        ExitPlanMode into a Bash command approval prompt."""
        bash_approval_pane = (
            "  Bash command\n"
            "  This command requires approval\n"
            "  ls /\n"
            "  Esc to cancel\n"
        )
        # Cycle 1: mode-only
        await self._poll(mock_bot, sample_pane_exit_plan_numbered, mock_window)
        # Cycle 2: rescue
        handle_ui = await self._poll(mock_bot, bash_approval_pane, mock_window)
        handle_ui.assert_called_once_with(mock_bot, self.USER, self.WIN, self.THREAD)

    @pytest.mark.asyncio
    async def test_ui_gone_clears_mode(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_exit_plan_numbered: str,
        sample_pane_no_ui: str,
    ):
        """When mode is set but UI disappears, mode must be cleared (fall-through
        to status check)."""
        from ccbot.handlers.interactive_ui import _interactive_mode

        await self._poll(mock_bot, sample_pane_exit_plan_numbered, mock_window)
        assert (self.USER, self.THREAD) in _interactive_mode

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
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
            patch("ccbot.handlers.status_polling.session_manager"),
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
        """End-to-end sequence matching the real log trace:
        ExitPlanMode (mode-only) → PermissionPrompt (rescue sends) → user
        dismisses (UI gone, mode cleared) → another PermissionPrompt arrives
        later and must be sent via the normal path."""
        from ccbot.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        # Step 1: ExitPlanMode — mode-only
        h = await self._poll(mock_bot, sample_pane_exit_plan_numbered, mock_window)
        h.assert_not_called()

        # Step 2: PermissionPrompt rescues
        h = await self._poll(mock_bot, sample_pane_permission, mock_window)
        h.assert_called_once()
        _interactive_msgs[(self.USER, self.THREAD)] = 500

        # Step 3: dismiss → pane goes idle
        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
            patch("ccbot.handlers.status_polling.session_manager"),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_no_ui)
            await update_status_message(
                mock_bot,
                user_id=self.USER,
                window_id=self.WIN,
                thread_id=self.THREAD,
            )
        # clear_interactive_msg popped both dicts
        assert (self.USER, self.THREAD) not in _interactive_mode
        assert (self.USER, self.THREAD) not in _interactive_msgs

        # Step 4: fresh PermissionPrompt arrives later — normal path
        h = await self._poll(mock_bot, sample_pane_permission, mock_window)
        h.assert_called_once()

    @pytest.mark.asyncio
    async def test_different_window_mode_does_not_block_rescue(
        self,
        mock_bot: AsyncMock,
        mock_window: MagicMock,
        sample_pane_permission: str,
    ):
        """If _interactive_mode is set for a DIFFERENT window and we poll this
        window with a PermissionPrompt, the existing stale-mode cleanup branch
        should kick in and the normal path should send the prompt. Regression
        guard: ensure the new rescue guard does not fire in this branch."""
        from ccbot.handlers.interactive_ui import _interactive_mode

        _interactive_mode[(self.USER, self.THREAD)] = "@99"  # different window

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.clear_interactive_msg",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_permission)
            mock_ws = MagicMock()
            mock_ws.session_id = "uuid-xxx"
            mock_sm.get_window_state.return_value = mock_ws
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot,
                user_id=self.USER,
                window_id=self.WIN,
                thread_id=self.THREAD,
            )
            mock_clear.assert_called_once()
            mock_handle_ui.assert_called_once_with(
                mock_bot, self.USER, self.WIN, self.THREAD
            )
