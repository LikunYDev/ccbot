"""Tests for bot.handle_new_message — INTERACTIVE_TOOL_NAMES suppression.

Pane-as-source design: JSONL `tool_use` entries for AskUserQuestion and
ExitPlanMode must NOT trigger a Telegram send from the JSONL path (no retry
loop, no `handle_interactive_ui` call). The pane and status_polling are now
the sole source for these UIs. JSONL is used only to advance the read offset
so we don't reprocess on restart.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot import handle_new_message
from ccbot.session_monitor import NewMessage


@pytest.fixture
def _clear_interactive_state():
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


@pytest.mark.usefixtures("_clear_interactive_state")
class TestJSONLInteractiveToolUseSuppression:
    @pytest.mark.asyncio
    async def test_ask_user_question_tool_use_does_not_call_handle_ui(self):
        """The JSONL `tool_use` entry for AskUserQuestion must NOT call
        handle_interactive_ui — the old retry-and-fetch path is gone. Pane is
        the source. JSONL just advances the read offset."""
        msg = NewMessage(
            session_id="sid-xyz",
            text="AskUserQuestion(question=...)",
            is_complete=True,
            content_type="tool_use",
            tool_use_id="toolu_01",
            tool_name="AskUserQuestion",
        )

        with (
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(7, "@5", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)
            bot = AsyncMock()

            await handle_new_message(msg, bot)

        mock_handle_ui.assert_not_called()
        # No regular content message either — these UIs are pane-only.
        mock_enqueue_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_plan_tool_use_does_not_call_handle_ui(self):
        msg = NewMessage(
            session_id="sid-xyz",
            text="ExitPlanMode(plan=...)",
            is_complete=True,
            content_type="tool_use",
            tool_use_id="toolu_02",
            tool_name="ExitPlanMode",
        )

        with (
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(7, "@5", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)
            bot = AsyncMock()

            await handle_new_message(msg, bot)

        mock_handle_ui.assert_not_called()
        mock_enqueue_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_ask_user_question_tool_use_advances_read_offset(self, tmp_path):
        """Suppressing the JSONL entry must still mark it as processed (advance
        the read offset to the current file size) so a future bot restart
        doesn't reprocess it after the user has already answered."""
        # Create a fake JSONL file so file_path.stat().st_size returns a real value.
        jsonl = tmp_path / "session.jsonl"
        jsonl.write_bytes(b"X" * 4321)

        mock_session = MagicMock()
        mock_session.file_path = jsonl

        msg = NewMessage(
            session_id="sid-xyz",
            text="AskUserQuestion(question=...)",
            is_complete=True,
            content_type="tool_use",
            tool_use_id="toolu_01",
            tool_name="AskUserQuestion",
        )

        with (
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.handle_interactive_ui", new_callable=AsyncMock),
            patch("ccbot.bot.enqueue_content_message", new_callable=AsyncMock),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(7, "@5", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
            mock_sm.update_user_window_offset = MagicMock()
            bot = AsyncMock()

            await handle_new_message(msg, bot)

        mock_sm.update_user_window_offset.assert_called_once_with(7, "@5", 4321)

    @pytest.mark.asyncio
    async def test_non_interactive_tool_use_still_sent_as_content(self):
        """Regression guard: non-INTERACTIVE_TOOL_NAMES tool_use entries
        (Bash, Read, Edit, ...) must still flow through the normal content
        path so users see tool-call notifications."""
        msg = NewMessage(
            session_id="sid-xyz",
            text="Bash(command='ls')",
            is_complete=True,
            content_type="tool_use",
            tool_use_id="toolu_03",
            tool_name="Bash",
        )

        with (
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_enqueue_content,
            patch(
                "ccbot.bot.config",
            ) as mock_config,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(7, "@5", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)
            mock_config.show_tool_calls = True
            bot = AsyncMock()

            await handle_new_message(msg, bot)

        mock_handle_ui.assert_not_called()
        mock_enqueue_content.assert_called()
