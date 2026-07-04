"""Tests for response_builder.build_response_parts."""

from ccbot.handlers.response_builder import build_response_parts
from ccbot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestBuildResponseParts:
    def test_user_message_has_emoji_prefix(self):
        parts = build_response_parts("hello", is_complete=True, role="user")
        assert len(parts) == 1
        assert "\U0001f464" in parts[0]

    def test_user_message_not_truncated_multi_part(self):
        """A user message beyond the old 3000-char cutoff must be
        paginated in full, never cut with an ellipsis."""
        long_text = "a" * 5000
        parts = build_response_parts(long_text, is_complete=True, role="user")
        assert len(parts) > 1
        joined = "".join(parts)
        assert "…" not in joined
        assert joined.count("a") == 5000
        # Prefix only appears on the first part
        assert parts[0].startswith("\U0001f464")
        assert not parts[1].startswith("\U0001f464")

    def test_thinking_content_keeps_full_text_beyond_500_chars(self):
        """A completed thinking block over the old 500-char cutoff must
        keep its full inner text — the quote stays collapsed by default
        and length is enforced only at the send layer."""
        inner = "x" * 800
        text = f"{EXP_START}{inner}{EXP_END}"
        parts = build_response_parts(text, is_complete=True, content_type="thinking")
        assert len(parts) == 1
        assert inner in parts[0]
        assert "truncated" not in parts[0].lower()

    def test_plain_text_single_part(self):
        parts = build_response_parts("short text", is_complete=True)
        assert len(parts) == 1

    def test_plain_text_multi_part_has_page_suffix(self):
        long_text = "\n".join(f"line {i} " + "padding" * 50 for i in range(200))
        parts = build_response_parts(long_text, is_complete=True)
        assert len(parts) > 1
        assert "1/" in parts[0]

    def test_expandable_quote_stays_atomic(self):
        inner = "thought " * 100
        text = f"{EXP_START}{inner}{EXP_END}"
        parts = build_response_parts(text, is_complete=False, content_type="thinking")
        assert len(parts) == 1

    def test_thinking_has_prefix(self):
        parts = build_response_parts(
            "some thought", is_complete=True, content_type="thinking"
        )
        assert len(parts) == 1
        assert "Thinking" in parts[0]

    def test_assistant_text_no_prefix(self):
        parts = build_response_parts(
            "hello world", is_complete=True, content_type="text", role="assistant"
        )
        assert len(parts) == 1
        assert "\U0001f464" not in parts[0]
        assert "Thinking" not in parts[0]
