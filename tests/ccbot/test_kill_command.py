"""Tests for kill_command — /kill teardown + registration precedence.

f32/RC14: the bot menu advertised /kill but no CommandHandler existed for
it, so it fell through to forward_command_handler and got literally typed
into the Claude Code session instead of tearing anything down.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import CommandHandler


def _make_update(user_id: int = 1, thread_id: int | None = 42) -> MagicMock:
    """Build a minimal mock Update with a message in a forum topic."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = "/kill"
    update.message.message_thread_id = thread_id
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestKillCommand:
    @pytest.mark.asyncio
    async def test_requires_a_topic(self):
        """/kill outside a named topic replies with an error and does nothing else."""
        update = _make_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=None),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            from ccbot.bot import kill_command

            await kill_command(update, context)

            mock_reply.assert_called_once()
            assert "topic" in mock_reply.call_args.args[1].lower()
            mock_sm.get_window_for_thread.assert_not_called()
            context.bot.delete_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_requires_a_bound_topic(self):
        """/kill on an unbound topic replies with an error and tears down nothing."""
        update = _make_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None

            from ccbot.bot import kill_command

            await kill_command(update, context)

            mock_reply.assert_called_once()
            assert "no session" in mock_reply.call_args.args[1].lower()
            mock_tmux.find_window_by_id.assert_not_called()
            context.bot.delete_forum_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_teardown_kills_window_unbinds_and_deletes_topic(self):
        """Bound topic with a live window: kill window, unbind, clear state,
        then delete the forum topic — mirroring topic_closed_handler's
        teardown plus the topic deletion /kill is responsible for."""
        update = _make_update(user_id=7, thread_id=42)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock) as mock_clear,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_sm.resolve_chat_id.return_value = -100999
            live_window = MagicMock()
            live_window.window_id = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=live_window)
            mock_tmux.kill_window = AsyncMock(return_value=True)

            from ccbot.bot import kill_command

            await kill_command(update, context)

            mock_tmux.kill_window.assert_awaited_once_with("@5")
            mock_sm.unbind_thread.assert_called_once_with(7, 42)
            mock_clear.assert_awaited_once_with(7, 42, context.bot, context.user_data)
            context.bot.delete_forum_topic.assert_awaited_once_with(
                chat_id=-100999, message_thread_id=42
            )
            # No error reply on the happy path.
            mock_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_already_gone_still_unbinds_and_deletes_topic(self):
        """If the tmux window is already gone, /kill must still unbind the
        thread, clear state, and delete the topic (no tmux call to make)."""
        update = _make_update(user_id=7, thread_id=42)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock) as mock_clear,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_sm.resolve_chat_id.return_value = 7
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            mock_tmux.kill_window = AsyncMock()

            from ccbot.bot import kill_command

            await kill_command(update, context)

            mock_tmux.kill_window.assert_not_called()
            mock_sm.unbind_thread.assert_called_once_with(7, 42)
            mock_clear.assert_awaited_once_with(7, 42, context.bot, context.user_data)
            context.bot.delete_forum_topic.assert_awaited_once_with(
                chat_id=7, message_thread_id=42
            )
            mock_reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_forum_topic_failure_reports_manual_cleanup(self):
        """Bots without 'Manage Topics' rights can't delete the topic — the
        session is still killed, but the user must be told to finish by hand."""
        update = _make_update(user_id=7, thread_id=42)
        context = _make_context()
        context.bot.delete_forum_topic = AsyncMock(
            side_effect=RuntimeError("Not enough rights")
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_sm.resolve_chat_id.return_value = -100999
            live_window = MagicMock()
            live_window.window_id = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=live_window)
            mock_tmux.kill_window = AsyncMock(return_value=True)

            from ccbot.bot import kill_command

            await kill_command(update, context)

            # Teardown already happened even though the delete call failed.
            mock_tmux.kill_window.assert_awaited_once_with("@5")
            mock_sm.unbind_thread.assert_called_once_with(7, 42)

            mock_reply.assert_called_once()
            reply_text = mock_reply.call_args.args[1].lower()
            assert "killed" in reply_text
            assert "manually" in reply_text


class TestKillCommandRegistration:
    """f32/RC14: /kill must be wired to kill_command, not the forwarding
    catch-all, and must be registered before it so it takes precedence."""

    def test_kill_wired_to_kill_command_before_forwarding_catchall(self):
        from ccbot.bot import create_bot, forward_command_handler, kill_command

        application = create_bot()
        handlers = application.handlers[0]

        kill_handlers = [
            h
            for h in handlers
            if isinstance(h, CommandHandler) and "kill" in h.commands
        ]
        assert len(kill_handlers) == 1, "expected exactly one /kill CommandHandler"
        assert kill_handlers[0].callback is kill_command

        forward_index = next(
            i
            for i, h in enumerate(handlers)
            if getattr(h, "callback", None) is forward_command_handler
        )
        kill_index = handlers.index(kill_handlers[0])
        assert kill_index < forward_index, (
            "/kill CommandHandler must be registered before the "
            "catch-all command-forwarding handler"
        )
