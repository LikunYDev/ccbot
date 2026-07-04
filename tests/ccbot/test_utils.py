"""Tests for ccbot.utils: ccbot_dir, atomic_write_json, read_cwd_from_jsonl,
supervise_loop.
"""

import asyncio
import json
from pathlib import Path

import pytest

from ccbot.utils import (
    atomic_write_json,
    ccbot_dir,
    parse_group_session_names,
    read_cwd_from_jsonl,
    supervise_loop,
)


class TestCcbotDir:
    def test_returns_env_var_path(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CCBOT_DIR", "/custom/config")
        assert ccbot_dir() == Path("/custom/config")

    def test_returns_default_without_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CCBOT_DIR", raising=False)
        assert ccbot_dir() == Path.home() / ".ccbot"


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path: Path):
        target = tmp_path / "data.json"
        atomic_write_json(target, {"key": "value"})
        result = json.loads(target.read_text(encoding="utf-8"))
        assert result == {"key": "value"}

    def test_creates_parent_directories(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c" / "data.json"
        atomic_write_json(target, [1, 2, 3])
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == [1, 2, 3]

    def test_round_trip(self, tmp_path: Path):
        data = {"users": [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]}
        target = tmp_path / "round_trip.json"
        atomic_write_json(target, data)
        assert json.loads(target.read_text(encoding="utf-8")) == data

    def test_no_temp_files_left_on_success(self, tmp_path: Path):
        target = tmp_path / "clean.json"
        atomic_write_json(target, {"ok": True})
        remaining = list(tmp_path.glob(".*tmp*"))
        assert remaining == []


class TestReadCwdFromJsonl:
    def test_cwd_in_first_entry(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text(json.dumps({"cwd": "/home/user/project"}) + "\n")
        assert read_cwd_from_jsonl(f) == "/home/user/project"

    def test_cwd_in_second_entry(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "init"}),
            json.dumps({"cwd": "/found/here"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        assert read_cwd_from_jsonl(f) == "/found/here"

    def test_no_cwd_returns_empty(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "init"}),
            json.dumps({"type": "message", "text": "hello"}),
        ]
        f.write_text("\n".join(lines) + "\n")
        assert read_cwd_from_jsonl(f) == ""

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert read_cwd_from_jsonl(tmp_path / "nonexistent.jsonl") == ""


class TestParseGroupSessionNames:
    """Moved from test_tmux_command.py alongside the function's move to
    utils.py (shared by tmux_manager.py and hook.py)."""

    def test_grouped_session_returns_all_peers(self):
        output = "ccbot|ccbot\nccbot-2|ccbot\nother|\n"
        assert parse_group_session_names(output, "ccbot") == {"ccbot", "ccbot-2"}

    def test_ungrouped_session_does_not_match_other_ungrouped_sessions(self):
        output = "ccbot|\nother|\n"
        assert parse_group_session_names(output, "ccbot") == {"ccbot"}

    def test_missing_configured_session_falls_back_to_literal_name(self):
        output = "other|other\nother-2|other\n"
        assert parse_group_session_names(output, "ccbot") == {"ccbot"}


class TestSuperviseLoop:
    @pytest.mark.asyncio
    async def test_restarts_factory_that_raises(self):
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

        task = asyncio.create_task(
            supervise_loop("test loop", factory, restart_delay=0.01)
        )
        # Give it time to crash and restart at least twice.
        while calls < 2:
            await asyncio.sleep(0.01)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert calls >= 2

    @pytest.mark.asyncio
    async def test_exits_quietly_when_should_run_false(self):
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            # Simulate the loop noticing the stop flag and returning normally.

        result_holder: list[bool] = [False]

        def should_run() -> bool:
            return result_holder[0]

        await asyncio.wait_for(
            supervise_loop(
                "test loop",
                factory,
                should_run=should_run,
                restart_delay=0.01,
            ),
            timeout=1.0,
        )

        assert calls == 1

    @pytest.mark.asyncio
    async def test_propagates_cancelled_error_promptly(self):
        started = asyncio.Event()

        async def factory():
            started.set()
            await asyncio.sleep(10)

        task = asyncio.create_task(
            supervise_loop("test loop", factory, restart_delay=5.0)
        )
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
