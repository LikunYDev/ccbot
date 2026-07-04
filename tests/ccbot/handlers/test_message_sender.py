"""Tests for message_sender.edit_with_fallback.

Mirrors send_with_fallback: try MarkdownV2 first, fall back to plain text
(sentinels stripped) on a non-RetryAfter failure, re-raise RetryAfter from
either attempt, and return a bool instead of the Message/None that
send_with_fallback returns (there's nothing new to hand back on an edit).
"""

import logging

import pytest
from telegram.error import RetryAfter
from unittest.mock import AsyncMock

from ccbot.handlers.message_sender import edit_with_fallback


class TestEditWithFallback:
    @pytest.mark.asyncio
    async def test_returns_true_on_first_attempt_success(self):
        bot = AsyncMock()

        result = await edit_with_fallback(bot, chat_id=100, message_id=5, text="hi")

        assert result is True
        bot.edit_message_text.assert_awaited_once()
        _, kwargs = bot.edit_message_text.call_args
        assert kwargs["chat_id"] == 100
        assert kwargs["message_id"] == 5
        assert kwargs["parse_mode"] == "MarkdownV2"

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_on_markdown_failure(self):
        bot = AsyncMock()
        calls = []

        async def fake_edit(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ValueError("bad markdown entities")
            return None

        bot.edit_message_text = AsyncMock(side_effect=fake_edit)

        result = await edit_with_fallback(bot, chat_id=100, message_id=5, text="hi")

        assert result is True
        assert len(calls) == 2
        # First attempt used MarkdownV2, second (fallback) did not.
        assert calls[0]["parse_mode"] == "MarkdownV2"
        assert "parse_mode" not in calls[1]
        assert calls[1]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_reraises_retryafter_from_first_attempt(self):
        bot = AsyncMock()
        bot.edit_message_text = AsyncMock(side_effect=RetryAfter(retry_after=1))

        with pytest.raises(RetryAfter):
            await edit_with_fallback(bot, chat_id=100, message_id=5, text="hi")

        bot.edit_message_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reraises_retryafter_from_second_attempt(self):
        bot = AsyncMock()
        calls = []

        async def fake_edit(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ValueError("bad markdown entities")
            raise RetryAfter(retry_after=1)

        bot.edit_message_text = AsyncMock(side_effect=fake_edit)

        with pytest.raises(RetryAfter):
            await edit_with_fallback(bot, chat_id=100, message_id=5, text="hi")

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_returns_false_and_logs_error_when_both_attempts_fail(self, caplog):
        bot = AsyncMock()
        bot.edit_message_text = AsyncMock(side_effect=ValueError("nope"))

        with caplog.at_level(logging.ERROR, logger="ccbot.handlers.message_sender"):
            result = await edit_with_fallback(bot, chat_id=100, message_id=5, text="hi")

        assert result is False
        assert bot.edit_message_text.await_count == 2
        assert any(
            "Failed to edit message" in record.message for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_strips_sentinels_in_plain_fallback(self):
        from ccbot.transcript_parser import TranscriptParser

        bot = AsyncMock()
        calls = []
        sentinel_text = (
            f"{TranscriptParser.EXPANDABLE_QUOTE_START}"
            "hidden"
            f"{TranscriptParser.EXPANDABLE_QUOTE_END}"
        )

        async def fake_edit(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ValueError("bad markdown entities")
            return None

        bot.edit_message_text = AsyncMock(side_effect=fake_edit)

        result = await edit_with_fallback(
            bot, chat_id=100, message_id=5, text=sentinel_text
        )

        assert result is True
        assert calls[1]["text"] == "hidden"
