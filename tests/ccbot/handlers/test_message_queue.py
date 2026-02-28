"""Tests for message_queue — status stats stripping for dedup."""

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
