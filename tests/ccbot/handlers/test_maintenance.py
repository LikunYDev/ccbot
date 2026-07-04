"""Tests for maintenance — hook-failure notices and divergence detection."""

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import ccbot.handlers.maintenance as maintenance
from ccbot.config import config


@pytest.fixture(autouse=True)
def _reset_module_state():
    maintenance._hook_failures_offset = None
    maintenance._divergence.clear()
    yield
    maintenance._hook_failures_offset = None
    maintenance._divergence.clear()


def _fake_session_manager(bindings, window_states):
    sm = SimpleNamespace()
    sm.iter_thread_bindings = lambda: iter(bindings)
    sm.window_states = window_states
    sm.resolve_chat_id = lambda user_id, thread_id=None: 100
    return sm


class TestHookFailureNotices:
    @pytest.mark.asyncio
    async def test_first_run_skips_history_then_tails_new_lines(
        self, tmp_path, monkeypatch
    ):
        """A restart must not replay historical failures; only lines appended
        after the first check are surfaced, and only to topics bound to the
        failure's cwd."""
        monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
        failures = tmp_path / "hook_failures.jsonl"
        failures.write_text(
            json.dumps({"ts": 1.0, "cwd": "/proj", "session_id": "old", "reason": "x"})
            + "\n"
        )
        bot = AsyncMock()
        sm = _fake_session_manager(
            bindings=[(1, 42, "@41"), (1, 43, "@50")],
            window_states={
                "@41": SimpleNamespace(session_id="sid-a", cwd="/proj"),
                "@50": SimpleNamespace(session_id="sid-b", cwd="/other"),
            },
        )

        with patch("ccbot.handlers.maintenance.session_manager", sm):
            await maintenance._check_hook_failures(bot)  # first run: seek EOF
            bot.send_message.assert_not_called()

            with open(failures, "a") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": 2.0,
                            "cwd": "/proj",
                            "session_id": "44444444-4444-4444-4444-444444444444",
                            "reason": "no unique claude window",
                        }
                    )
                    + "\n"
                )
            await maintenance._check_hook_failures(bot)

        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["message_thread_id"] == 42  # /proj topic only
        assert "no unique claude window" in kwargs["text"]
        assert "44444444" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_processed_lines_are_not_resent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
        failures = tmp_path / "hook_failures.jsonl"
        failures.write_text("")
        bot = AsyncMock()
        sm = _fake_session_manager(
            bindings=[(1, 42, "@41")],
            window_states={"@41": SimpleNamespace(session_id="s", cwd="/proj")},
        )

        with patch("ccbot.handlers.maintenance.session_manager", sm):
            await maintenance._check_hook_failures(bot)
            with open(failures, "a") as f:
                f.write(json.dumps({"cwd": "/proj", "reason": "r"}) + "\n")
            await maintenance._check_hook_failures(bot)
            await maintenance._check_hook_failures(bot)

        assert bot.send_message.call_count == 1


class TestDivergenceDetection:
    def _setup(self, tmp_path, monkeypatch, *, bindings=None, window_states=None):
        """Project dir with a frozen tracked transcript and a fresh sibling."""
        proj = tmp_path / "proj"
        proj.mkdir()
        tracked = proj / "sid-a.jsonl"
        tracked.write_text("{}\n")
        stale = time.time() - 600
        os.utime(tracked, (stale, stale))
        candidate = proj / "sid-b.jsonl"
        candidate.write_text("{}\n")

        monitor_state_file = tmp_path / "monitor_state.json"
        monitor_state_file.write_text(
            json.dumps(
                {
                    "tracked_sessions": {
                        "sid-a": {
                            "session_id": "sid-a",
                            "file_path": str(tracked),
                            "last_byte_offset": 0,
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(config, "monitor_state_file", monitor_state_file)

        sm = _fake_session_manager(
            bindings=bindings or [(1, 42, "@41")],
            window_states=window_states
            or {"@41": SimpleNamespace(session_id="sid-a", cwd="/proj")},
        )
        return candidate, sm

    @staticmethod
    def _grow(path):
        with open(path, "a") as f:
            f.write("{}\n")

    @pytest.mark.asyncio
    async def test_notice_after_two_growth_ticks_then_once(self, tmp_path, monkeypatch):
        candidate, sm = self._setup(tmp_path, monkeypatch)
        bot = AsyncMock()

        with patch("ccbot.handlers.maintenance.session_manager", sm):
            await maintenance._check_divergence(bot)  # tick 1: observe
            bot.send_message.assert_not_called()

            self._grow(candidate)
            await maintenance._check_divergence(bot)  # tick 2: growth -> notice
            bot.send_message.assert_called_once()

            self._grow(candidate)
            await maintenance._check_divergence(bot)  # tick 3: no duplicate
            bot.send_message.assert_called_once()

        kwargs = bot.send_message.call_args.kwargs
        assert "sid-b" in kwargs["text"]
        assert kwargs["message_thread_id"] == 42
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.callback_data == "rp:@41:sid-b"

    @pytest.mark.asyncio
    async def test_no_notice_without_growth(self, tmp_path, monkeypatch):
        """A static sibling file (e.g. an old finished session) never fires."""
        _candidate, sm = self._setup(tmp_path, monkeypatch)
        bot = AsyncMock()

        with patch("ccbot.handlers.maintenance.session_manager", sm):
            await maintenance._check_divergence(bot)
            await maintenance._check_divergence(bot)
            await maintenance._check_divergence(bot)

        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ambiguous_directory_suppresses_notice(self, tmp_path, monkeypatch):
        """Two bound windows on one directory: the growing sibling can't be
        attributed to either, so no notice (same refusal as the hook)."""
        candidate, _ = self._setup(tmp_path, monkeypatch)
        sm = _fake_session_manager(
            bindings=[(1, 42, "@41"), (1, 43, "@42")],
            window_states={
                "@41": SimpleNamespace(session_id="sid-a", cwd="/proj"),
                "@42": SimpleNamespace(session_id="sid-c", cwd="/proj"),
            },
        )
        bot = AsyncMock()

        with patch("ccbot.handlers.maintenance.session_manager", sm):
            await maintenance._check_divergence(bot)
            self._grow(candidate)
            await maintenance._check_divergence(bot)
            self._grow(candidate)
            await maintenance._check_divergence(bot)

        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_recently_active_tracked_file_clears_episode(
        self, tmp_path, monkeypatch
    ):
        """A tracked transcript that is still writing is not stale, no matter
        what its siblings do (long thinking turns must never false-alarm)."""
        candidate, sm = self._setup(tmp_path, monkeypatch)
        # Tracked file wrote just now.
        tracked = tmp_path / "proj" / "sid-a.jsonl"
        now = time.time()
        os.utime(tracked, (now, now))
        bot = AsyncMock()

        with patch("ccbot.handlers.maintenance.session_manager", sm):
            await maintenance._check_divergence(bot)
            self._grow(candidate)
            await maintenance._check_divergence(bot)

        bot.send_message.assert_not_called()
        assert maintenance._divergence == {}
