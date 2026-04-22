"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json

import pytest

from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import NewMessage, SessionMonitor


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1


class TestTurnEndDispatch:
    """Verify the turn-end callback fires per-batch, not gated on session history."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def _msg(self, content_type: str, tool_use_id: str | None = None) -> NewMessage:
        return NewMessage(
            session_id="s1",
            text="x",
            is_complete=True,
            content_type=content_type,
            tool_use_id=tool_use_id,
        )

    @pytest.mark.asyncio
    async def test_fires_on_plain_text_batch(self, monitor):
        fired: list[str] = []

        async def on_turn_end(sid: str) -> None:
            fired.append(sid)

        async def on_message(_: NewMessage) -> None:
            pass

        monitor.set_message_callback(on_message)
        monitor.set_turn_end_callback(on_turn_end)
        await monitor._dispatch_session_messages("s1", [self._msg("text")])
        assert fired == ["s1"]

    @pytest.mark.asyncio
    async def test_fires_when_batch_pairs_tool_use_with_result(self, monitor):
        fired: list[str] = []

        async def on_turn_end(sid: str) -> None:
            fired.append(sid)

        async def on_message(_: NewMessage) -> None:
            pass

        monitor.set_message_callback(on_message)
        monitor.set_turn_end_callback(on_turn_end)
        await monitor._dispatch_session_messages(
            "s1",
            [
                self._msg("tool_use", tool_use_id="t1"),
                self._msg("tool_result", tool_use_id="t1"),
                self._msg("text"),
            ],
        )
        assert fired == ["s1"]

    @pytest.mark.asyncio
    async def test_defers_when_batch_ends_on_unpaired_tool_use(self, monitor):
        fired: list[str] = []

        async def on_turn_end(sid: str) -> None:
            fired.append(sid)

        async def on_message(_: NewMessage) -> None:
            pass

        monitor.set_message_callback(on_message)
        monitor.set_turn_end_callback(on_turn_end)
        await monitor._dispatch_session_messages(
            "s1", [self._msg("text"), self._msg("tool_use", tool_use_id="t1")]
        )
        assert fired == []

    @pytest.mark.asyncio
    async def test_fires_even_when_session_has_stale_pending_tools(self, monitor):
        """Regression: _pending_tools is session-wide history; must not gate firing.

        Before this fix, one unpaired tool_use from an earlier batch left the
        session's entry in _pending_tools forever and silently blocked the
        turn-end callback. The new gate is per-batch only.
        """
        fired: list[str] = []

        async def on_turn_end(sid: str) -> None:
            fired.append(sid)

        async def on_message(_: NewMessage) -> None:
            pass

        monitor.set_message_callback(on_message)
        monitor.set_turn_end_callback(on_turn_end)
        # Simulate a stuck entry from an earlier batch:
        monitor._pending_tools["s1"] = {"stale-id": {}}  # type: ignore[assignment]
        # Current batch is a clean text-only response:
        await monitor._dispatch_session_messages("s1", [self._msg("text")])
        assert fired == ["s1"]
