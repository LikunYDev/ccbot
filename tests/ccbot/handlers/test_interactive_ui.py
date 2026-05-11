"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    handle_interactive_ui,
)
from ccbot.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


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
        _interactive_mode,
        _interactive_msgs,
    )

    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_enqueued.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_enqueued.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs["chat_id"] == 100
        assert call_kwargs.kwargs["message_thread_id"] == 42
        assert call_kwargs.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager"),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_exit_plan_numbered_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_exit_plan_numbered: str
    ):
        """New numbered ExitPlanMode format triggers keyboard send."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value=sample_pane_exit_plan_numbered
            )
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_exit_plan_old_format_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_exit_plan: str
    ):
        """Old ExitPlanMode format still triggers keyboard send (backward compat)."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_exit_plan)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs["reply_markup"] is not None


@pytest.mark.usefixtures("_clear_interactive_state")
class TestInteractiveEnqueuedFlag:
    """The `_interactive_enqueued` set is the linchpin that prevents
    status_polling from enqueueing duplicate `interactive_ui` tasks while a
    previous task is still in the worker's queue. These tests pin the
    contract before the rewrite touches status_polling."""

    def test_mark_is_idempotent(self):
        """Marking twice for the same ikey leaves the flag set exactly once."""
        from ccbot.handlers.interactive_ui import (
            _interactive_enqueued,
            is_interactive_enqueued,
            mark_interactive_enqueued,
        )

        mark_interactive_enqueued(7, 42)
        mark_interactive_enqueued(7, 42)
        assert is_interactive_enqueued(7, 42) is True
        # Internal: keyed by (user_id, thread_id or 0); de-dup via set semantics
        assert (7, 42) in _interactive_enqueued
        assert sum(1 for k in _interactive_enqueued if k == (7, 42)) == 1

    def test_clear_is_idempotent(self):
        """Clearing when the flag is not set must not raise."""
        from ccbot.handlers.interactive_ui import (
            clear_interactive_enqueued,
            is_interactive_enqueued,
        )

        # Never set; clear should be a no-op.
        clear_interactive_enqueued(7, 42)
        assert is_interactive_enqueued(7, 42) is False

    def test_mark_then_clear_round_trip(self):
        from ccbot.handlers.interactive_ui import (
            clear_interactive_enqueued,
            is_interactive_enqueued,
            mark_interactive_enqueued,
        )

        mark_interactive_enqueued(7, 42)
        assert is_interactive_enqueued(7, 42) is True
        clear_interactive_enqueued(7, 42)
        assert is_interactive_enqueued(7, 42) is False

    def test_thread_id_none_treated_as_zero(self):
        """thread_id=None and thread_id=0 must collapse to the same key, matching
        the convention used by _interactive_mode / _interactive_msgs."""
        from ccbot.handlers.interactive_ui import (
            is_interactive_enqueued,
            mark_interactive_enqueued,
        )

        mark_interactive_enqueued(7, None)
        assert is_interactive_enqueued(7, 0) is True
        assert is_interactive_enqueued(7, None) is True

    def test_different_ikeys_are_independent(self):
        from ccbot.handlers.interactive_ui import (
            clear_interactive_enqueued,
            is_interactive_enqueued,
            mark_interactive_enqueued,
        )

        mark_interactive_enqueued(7, 42)
        mark_interactive_enqueued(7, 99)
        clear_interactive_enqueued(7, 42)
        assert is_interactive_enqueued(7, 42) is False
        assert is_interactive_enqueued(7, 99) is True

    def test_last_name_round_trip(self):
        from ccbot.handlers.interactive_ui import (
            get_interactive_last_name,
            set_interactive_last_name,
        )

        assert get_interactive_last_name(7, 42) is None
        set_interactive_last_name(7, "ExitPlanMode", 42)
        assert get_interactive_last_name(7, 42) == "ExitPlanMode"
        set_interactive_last_name(7, "PermissionPrompt", 42)
        assert get_interactive_last_name(7, 42) == "PermissionPrompt"

    def test_last_name_thread_id_none_treated_as_zero(self):
        from ccbot.handlers.interactive_ui import (
            get_interactive_last_name,
            set_interactive_last_name,
        )

        set_interactive_last_name(7, "AskUserQuestion", None)
        assert get_interactive_last_name(7, 0) == "AskUserQuestion"

    @pytest.mark.asyncio
    async def test_handle_interactive_ui_records_last_name(
        self, mock_bot: AsyncMock, sample_pane_exit_plan: str
    ):
        """On a successful send, handle_interactive_ui must record the UI name
        in _interactive_last_name so status_polling can detect morphs."""
        from ccbot.handlers.interactive_ui import get_interactive_last_name

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_exit_plan)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=7, window_id=window_id, thread_id=42
            )

        assert result is True
        assert get_interactive_last_name(7, 42) == "ExitPlanMode"

    @pytest.mark.asyncio
    async def test_clear_interactive_msg_clears_last_name(self, mock_bot: AsyncMock):
        """clear_interactive_msg must also wipe _interactive_last_name so the
        next UI in this topic doesn't get classified as a morph of the old."""
        from ccbot.handlers.interactive_ui import (
            _interactive_msgs,
            clear_interactive_msg,
            get_interactive_last_name,
            set_interactive_last_name,
        )

        set_interactive_last_name(7, "ExitPlanMode", 42)
        _interactive_msgs[(7, 42)] = 999

        with patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            await clear_interactive_msg(7, mock_bot, 42)

        assert get_interactive_last_name(7, 42) is None

    @pytest.mark.asyncio
    async def test_clear_interactive_msg_clears_enqueued_flag(
        self, mock_bot: AsyncMock
    ):
        """When the UI is dismissed and clear_interactive_msg runs, the
        `_interactive_enqueued` flag must also be cleared so the next render
        of a UI in the same topic can enqueue again."""
        from ccbot.handlers.interactive_ui import (
            _interactive_msgs,
            clear_interactive_msg,
            is_interactive_enqueued,
            mark_interactive_enqueued,
        )

        # Simulate: a UI was previously delivered (msg_id set) and a poll-time
        # enqueue had been recorded but not yet cleared by the worker.
        mark_interactive_enqueued(7, 42)
        _interactive_msgs[(7, 42)] = 12345

        with patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm:
            mock_sm.resolve_chat_id.return_value = 100
            await clear_interactive_msg(7, mock_bot, 42)

        assert is_interactive_enqueued(7, 42) is False


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)
