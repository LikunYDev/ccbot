"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import asyncio
import json

import pytest

from ccbot.config import config
from ccbot.monitor_state import TrackedSession
from ccbot.session import WindowState, session_manager
from ccbot.session_monitor import NewMessage, SessionInfo, SessionMonitor


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

    @pytest.mark.asyncio
    async def test_complete_corrupt_line_is_skipped(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """A complete-but-malformed line is skipped and the offset advances
        past it, so subsequent valid lines are still parsed and a bad line
        can never wedge reads forever."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="after corruption")
        jsonl_file.write_text(
            "not-json-but-terminated\n" + json.dumps(entry) + "\n",
            encoding="utf-8",
        )

        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # The corrupt line is skipped; the valid line after it is still read.
        assert len(result) == 1
        assert result[0]["message"]["content"] == "after corruption"

        # Offset advances past the corrupt line all the way to EOF.
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_trailing_partial_line_retried(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """A line with no trailing newline (file tail, likely mid-write) is
        retried next cycle — the offset does not advance past it."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="complete")
        complete_line = json.dumps(entry) + "\n"
        partial_line = '{"type": "assistant", "message": {"conte'  # no newline
        jsonl_file.write_text(complete_line + partial_line, encoding="utf-8")

        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Only the complete line is parsed; the partial tail is not consumed.
        assert len(result) == 1
        assert result[0]["message"]["content"] == "complete"

        # Offset stops at the start of the partial line, not past it.
        assert session.last_byte_offset == len(complete_line.encode("utf-8"))


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


class TestSessionMapFromWindowStates:
    """SessionMonitor derives its window->session map from
    session_manager.window_states — the reconciled authority (hook events
    with pins applied) — instead of re-parsing session_map.json itself
    (review findings f16/f47, root cause RC2).
    """

    @pytest.fixture
    def monitor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.fixture(autouse=True)
    def _isolate_window_states(self):
        """Snapshot/restore the singleton's window_states around each test
        to avoid cross-test pollution."""
        original = session_manager.window_states
        session_manager.window_states = {}
        yield
        session_manager.window_states = original

    @pytest.mark.asyncio
    async def test_load_current_session_map_reads_window_states(self, monitor):
        session_manager.window_states = {
            "@5": WindowState(session_id="sid-1"),
            "@28": WindowState(session_id="sid-28"),
            "@9": WindowState(session_id=""),  # not yet detected -- excluded
        }

        current_map = await monitor._load_current_session_map()

        assert current_map == {"@5": "sid-1", "@28": "sid-28"}

    @pytest.mark.asyncio
    async def test_load_current_session_map_ignores_session_map_json_on_disk(
        self, monitor, tmp_path
    ):
        """The monitor no longer parses session_map.json directly; only
        session_manager.window_states (populated by
        session_manager.load_session_map()) matters."""
        (tmp_path / "session_map.json").write_text(
            json.dumps({"ccbot:@5": {"session_id": "totally-different-sid"}}),
            encoding="utf-8",
        )
        session_manager.window_states = {"@5": WindowState(session_id="sid-1")}

        current_map = await monitor._load_current_session_map()

        assert current_map == {"@5": "sid-1"}

    @pytest.mark.asyncio
    async def test_resume_pin_stays_active_even_when_hook_reports_old_sid(
        self, monitor, tmp_path
    ):
        """Regression (f16/f47, RC2): after a resume, window_states holds the
        pinned (real) session_id while session_map.json on disk may still
        only report the pre-resume hook session_id. Before this fix, the
        monitor parsed session_map.json itself and never saw the pinned sid,
        so active_session_ids never contained it and the topic went
        permanently silent.
        """
        (tmp_path / "session_map.json").write_text(
            json.dumps({"ccbot:@5": {"session_id": "hook-uuid"}}),
            encoding="utf-8",
        )
        session_manager.window_states = {
            "@5": WindowState(session_id="orig-uuid", pinned_over="hook-uuid"),
        }

        current_map = await monitor._load_current_session_map()

        assert current_map == {"@5": "orig-uuid"}
        # check_for_updates treats orig-uuid (not hook-uuid) as active.
        assert "orig-uuid" in set(current_map.values())
        assert "hook-uuid" not in set(current_map.values())

    @pytest.mark.asyncio
    async def test_cleanup_all_stale_sessions_uses_window_states(
        self, monitor, tmp_path
    ):
        session_manager.window_states = {"@28": WindowState(session_id="sid-28")}
        monitor.state.update_session(
            TrackedSession(
                session_id="sid-28",
                file_path=str(tmp_path / "sid-28.jsonl"),
                last_byte_offset=0,
            )
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="stale",
                file_path=str(tmp_path / "stale.jsonl"),
                last_byte_offset=0,
            )
        )

        await monitor._cleanup_all_stale_sessions()

        assert monitor.state.get_session("sid-28") is not None
        assert monitor.state.get_session("stale") is None


class TestScanProjectsFallbackRecovery:
    """scan_projects() gates every session on its recorded project path
    matching a live tmux window's CURRENT cwd. If the project directory is
    renamed/moved after the session started (or the sessions-index path
    drifts), scan_projects silently drops the session from every future
    scan, forever, with no log — check_for_updates must recover it instead
    (review f59/RC27).
    """

    @pytest.fixture
    def monitor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.fixture(autouse=True)
    def _isolate_window_states(self):
        """Snapshot/restore the singleton's window_states around each test
        to avoid cross-test pollution."""
        original = session_manager.window_states
        session_manager.window_states = {}
        yield
        session_manager.window_states = original

    @pytest.mark.asyncio
    async def test_tracked_session_with_existing_file_is_still_read(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """(a) Already tracked with a file that still exists on disk: the
        session is recovered directly from the tracked record — the cwd
        gate is irrelevant since the file location is already known."""
        jsonl_file = tmp_path / "moved-sid.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="hello from moved dir")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        monitor.state.update_session(
            TrackedSession(
                session_id="moved-sid",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        async def fake_scan_projects():
            # Project dir renamed/moved: no active tmux cwd matches it
            # anymore, so the normal scan can no longer find this session.
            return []

        monitor.scan_projects = fake_scan_projects  # type: ignore[method-assign]

        messages, commits = await monitor.check_for_updates({"moved-sid"})

        assert [m.text for m in messages] == ["hello from moved dir"]
        assert "moved-sid" in commits

    @pytest.mark.asyncio
    async def test_untracked_session_recovered_via_fallback_glob(
        self, monitor, tmp_path, make_jsonl_entry, caplog
    ):
        """(b) Not tracked at all: recovered via a one-level glob under the
        projects root for `*/<session_id>.jsonl`, and the recovery is
        logged as a warning since this failure mode is otherwise silent
        and permanent."""
        project_dir = tmp_path / "projects" / "-renamed-project-dir"
        project_dir.mkdir(parents=True)
        jsonl_file = project_dir / "glob-sid.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="hello via glob")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        async def fake_scan_projects():
            return []

        monitor.scan_projects = fake_scan_projects  # type: ignore[method-assign]

        with caplog.at_level("WARNING"):
            messages, commits = await monitor.check_for_updates({"glob-sid"})

        # A brand-new (untracked) session's first poll only seeds tracking
        # at EOF (existing behavior); it starts reading from there on the
        # next cycle — see TestNewSessionOffsetSeeding.
        assert messages == []
        assert commits == {}
        assert monitor.state.get_session("glob-sid") is not None
        assert any(
            "fallback glob" in record.getMessage() and "glob-sid" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_glob_miss_is_cached_and_not_retried_immediately(
        self, monitor, tmp_path
    ):
        """A session_id that is neither tracked nor found by the glob is
        cached for 60s so a truly-gone session isn't re-globbed every poll."""

        async def fake_scan_projects():
            return []

        monitor.scan_projects = fake_scan_projects  # type: ignore[method-assign]

        messages, commits = await monitor.check_for_updates({"gone-sid"})
        assert messages == []
        assert commits == {}
        assert "gone-sid" in monitor._glob_miss_until

        # Materializing the file after the miss was cached must not be
        # picked up until the TTL elapses (glob is skipped entirely).
        project_dir = tmp_path / "projects" / "-late-project-dir"
        project_dir.mkdir(parents=True)
        (project_dir / "gone-sid.jsonl").write_text("", encoding="utf-8")

        messages, commits = await monitor.check_for_updates({"gone-sid"})
        assert monitor.state.get_session("gone-sid") is None


class TestNewSessionOffsetSeeding:
    """Regression for f17/RC38: seeding a newly-noticed session's offset at
    current EOF silently skips a reply that landed in the transcript before
    the monitor's first poll (or before it ever noticed the session — e.g.
    monitor_state was lost). The hook records the transcript size at
    SessionStart (WindowState.session_start_size); check_for_updates must
    seed from there instead so that reply is still delivered.
    """

    @pytest.fixture
    def monitor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.fixture(autouse=True)
    def _isolate_window_states(self):
        """Snapshot/restore the singleton's window_states around each test
        to avoid cross-test pollution."""
        original = session_manager.window_states
        session_manager.window_states = {}
        yield
        session_manager.window_states = original

    @pytest.mark.asyncio
    async def test_first_poll_delivers_reply_already_beyond_start_size(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        jsonl_file = tmp_path / "new-sid.jsonl"
        preamble = make_jsonl_entry(msg_type="assistant", content="before start")
        jsonl_file.write_text(json.dumps(preamble) + "\n", encoding="utf-8")
        start_size = jsonl_file.stat().st_size

        # The reply lands before the monitor ever polls this session.
        reply = make_jsonl_entry(msg_type="assistant", content="fast reply")
        with jsonl_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(reply) + "\n")

        session_manager.window_states = {
            "@1": WindowState(session_id="new-sid", session_start_size=start_size),
        }

        async def fake_scan_projects():
            return [SessionInfo(session_id="new-sid", file_path=jsonl_file)]

        monitor.scan_projects = fake_scan_projects  # type: ignore[method-assign]

        # First call only starts tracking (seeds the offset); it never reads
        # in the same cycle it starts tracking a session.
        messages, commits = await monitor.check_for_updates({"new-sid"})
        assert messages == []
        assert commits == {}
        assert monitor.state.get_session("new-sid").last_byte_offset == start_size

        # Next poll cycle reads from the seeded offset (start_size), not
        # from EOF (which would have skipped the reply too).
        messages, commits = await monitor.check_for_updates({"new-sid"})
        assert [m.text for m in messages] == ["fast reply"]
        assert "new-sid" in commits

    @pytest.mark.asyncio
    async def test_unknown_start_size_still_seeds_at_eof(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """No matching window_state (or session_start_size == -1) preserves
        the prior default behavior: seed at current EOF."""
        jsonl_file = tmp_path / "new-sid.jsonl"
        preamble = make_jsonl_entry(msg_type="assistant", content="already there")
        jsonl_file.write_text(json.dumps(preamble) + "\n", encoding="utf-8")

        session_manager.window_states = {
            "@1": WindowState(session_id="new-sid"),  # session_start_size == -1
        }

        async def fake_scan_projects():
            return [SessionInfo(session_id="new-sid", file_path=jsonl_file)]

        monitor.scan_projects = fake_scan_projects  # type: ignore[method-assign]

        messages, commits = await monitor.check_for_updates({"new-sid"})

        assert messages == []
        assert commits == {}
        assert monitor.state.get_session("new-sid").last_byte_offset == (
            jsonl_file.stat().st_size
        )

    @pytest.mark.asyncio
    async def test_start_size_beyond_current_file_size_is_clamped(
        self, monitor, tmp_path
    ):
        """A start_size larger than the file's current size (e.g. the file
        was truncated/replaced) must not seed an offset past EOF."""
        jsonl_file = tmp_path / "new-sid.jsonl"
        jsonl_file.write_text("", encoding="utf-8")

        session_manager.window_states = {
            "@1": WindowState(session_id="new-sid", session_start_size=999),
        }

        async def fake_scan_projects():
            return [SessionInfo(session_id="new-sid", file_path=jsonl_file)]

        monitor.scan_projects = fake_scan_projects  # type: ignore[method-assign]

        await monitor.check_for_updates({"new-sid"})

        assert monitor.state.get_session("new-sid").last_byte_offset == 0


class TestFinalDrainOnSessionChange:
    """Trailing unread lines in the OLD jsonl are delivered before its
    tracking is removed when a window's session_id changes underneath it
    (/clear, resume) — review f50/RC23.

    Before this fix, `_detect_and_cleanup_changes` removed the old
    session's tracking the instant it observed the session_id flip,
    without checking whether any lines had been appended to the old file
    since the last read — silently losing them forever.
    """

    @pytest.fixture
    def monitor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.fixture(autouse=True)
    def _isolate_window_states(self):
        """Snapshot/restore the singleton's window_states around each test
        to avoid cross-test pollution."""
        original = session_manager.window_states
        session_manager.window_states = {}
        yield
        session_manager.window_states = original

    @pytest.mark.asyncio
    async def test_drains_trailing_lines_before_removing_old_session(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        old_file = tmp_path / "old-sid.jsonl"
        already_read = make_jsonl_entry(msg_type="assistant", content="already read")
        old_file.write_text(json.dumps(already_read) + "\n", encoding="utf-8")

        monitor.state.update_session(
            TrackedSession(
                session_id="old-sid",
                file_path=str(old_file),
                last_byte_offset=old_file.stat().st_size,
            )
        )

        # A trailing line lands after the last read but before the window's
        # session_id flips (e.g. /clear racing the final write).
        trailing = make_jsonl_entry(msg_type="assistant", content="trailing unread")
        with old_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trailing) + "\n")

        delivered: list[NewMessage] = []

        async def on_message(msg: NewMessage) -> None:
            delivered.append(msg)

        monitor.set_message_callback(on_message)

        # Poll cycle N: window @1 -> old-sid.
        session_manager.window_states = {"@1": WindowState(session_id="old-sid")}
        monitor._last_session_map = await monitor._load_current_session_map()

        # Poll cycle N+1: window @1 -> new-sid (e.g. after /clear).
        session_manager.window_states = {"@1": WindowState(session_id="new-sid")}

        current_map = await monitor._detect_and_cleanup_changes()

        assert current_map == {"@1": "new-sid"}
        # The trailing line was delivered...
        assert [m.text for m in delivered] == ["trailing unread"]
        # ...strictly before tracking was torn down.
        assert monitor.state.get_session("old-sid") is None

    @pytest.mark.asyncio
    async def test_drains_trailing_lines_before_removing_deleted_window_session(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Same guarantee for the window-deleted path, not just the
        session-changed path."""
        old_file = tmp_path / "old-sid.jsonl"
        old_file.write_text("", encoding="utf-8")

        monitor.state.update_session(
            TrackedSession(
                session_id="old-sid",
                file_path=str(old_file),
                last_byte_offset=0,
            )
        )

        trailing = make_jsonl_entry(msg_type="assistant", content="final words")
        with old_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trailing) + "\n")

        delivered: list[NewMessage] = []

        async def on_message(msg: NewMessage) -> None:
            delivered.append(msg)

        monitor.set_message_callback(on_message)

        # Poll cycle N: window @1 exists, bound to old-sid.
        session_manager.window_states = {"@1": WindowState(session_id="old-sid")}
        monitor._last_session_map = await monitor._load_current_session_map()

        # Poll cycle N+1: window @1 is gone entirely (topic/window closed).
        session_manager.window_states = {}

        current_map = await monitor._detect_and_cleanup_changes()

        assert current_map == {}
        assert [m.text for m in delivered] == ["final words"]
        assert monitor.state.get_session("old-sid") is None


class TestDrainCallbacks:
    """Graceful shutdown delivers in-flight dispatch tasks. Offsets now
    advance on delivery ACK rather than on read, so this is no longer
    required to prevent loss — but draining still avoids needlessly
    redelivering already-read messages after a restart."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_awaits_in_flight_dispatch(self, monitor):
        delivered = asyncio.Event()

        async def slow_dispatch():
            await asyncio.sleep(0.02)
            delivered.set()

        task = asyncio.create_task(slow_dispatch())
        monitor._callback_tasks.add(task)
        task.add_done_callback(monitor._callback_tasks.discard)

        await monitor.drain_callbacks()

        assert delivered.is_set()  # in-flight message was delivered, not dropped
        assert task.done()

    @pytest.mark.asyncio
    async def test_no_pending_is_noop(self, monitor):
        # Should return promptly with nothing queued.
        await monitor.drain_callbacks()

    @pytest.mark.asyncio
    async def test_times_out_without_hanging(self, monitor):
        async def never():
            await asyncio.sleep(10)

        task = asyncio.create_task(never())
        monitor._callback_tasks.add(task)
        try:
            await monitor.drain_callbacks(timeout=0.05)  # returns despite the hang
        finally:
            task.cancel()


class TestDeliveryCommitContract:
    """Offsets commit only after a batch is durably delivered (f18/RC7).

    `check_for_updates` reverts the in-memory offset for any session that
    produced messages and hands the advance back via its pending-commits
    dict; `_dispatch_and_commit` is the only thing that actually persists
    it, and only once `_dispatch_session_messages` reports every callback
    succeeded. This is at-least-once delivery: a failure re-reads and
    re-dispatches the identical batch next cycle instead of losing it.
    """

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.fixture
    def session_file(self, tmp_path, monitor):
        """A tracked session backed by a real (initially empty) JSONL file."""
        jsonl_file = tmp_path / "s1.jsonl"
        jsonl_file.write_text("", encoding="utf-8")

        async def fake_scan_projects() -> list[SessionInfo]:
            return [SessionInfo(session_id="s1", file_path=jsonl_file)]

        monitor.scan_projects = fake_scan_projects  # type: ignore[method-assign]
        return jsonl_file

    async def _seed_and_append(self, monitor, session_file, make_jsonl_entry, text):
        """Start tracking (offset seeded to EOF), then append one assistant entry."""
        # First call just seeds tracking at end-of-file (no pre-existing content).
        messages, commits = await monitor.check_for_updates({"s1"})
        assert messages == []
        assert commits == {}

        entry = make_jsonl_entry(msg_type="assistant", content=text)
        with session_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    @pytest.mark.asyncio
    async def test_failed_callback_does_not_persist_offset_and_is_redelivered(
        self, monitor, session_file, make_jsonl_entry
    ):
        await self._seed_and_append(monitor, session_file, make_jsonl_entry, "hello")

        messages, commits = await monitor.check_for_updates({"s1"})
        assert [m.text for m in messages] == ["hello"]
        assert "s1" in commits

        async def raising_callback(_msg: NewMessage) -> None:
            raise RuntimeError("boom")

        monitor.set_message_callback(raising_callback)
        await monitor._dispatch_and_commit("s1", messages, commits["s1"])

        # Offset was NOT advanced past the undelivered batch.
        assert monitor.state.get_session("s1").last_byte_offset == 0
        assert "s1" not in monitor._inflight

        # Next cycle re-reads and re-emits the identical message.
        messages2, commits2 = await monitor.check_for_updates({"s1"})
        assert [m.text for m in messages2] == ["hello"]
        assert "s1" in commits2

    @pytest.mark.asyncio
    async def test_successful_callback_persists_offset_and_is_not_reread(
        self, monitor, session_file, make_jsonl_entry
    ):
        await self._seed_and_append(monitor, session_file, make_jsonl_entry, "hello")

        messages, commits = await monitor.check_for_updates({"s1"})
        assert len(messages) == 1

        delivered: list[NewMessage] = []

        async def ok_callback(msg: NewMessage) -> None:
            delivered.append(msg)

        monitor.set_message_callback(ok_callback)
        await monitor._dispatch_and_commit("s1", messages, commits["s1"])

        assert [m.text for m in delivered] == ["hello"]
        expected_offset = session_file.stat().st_size
        assert monitor.state.get_session("s1").last_byte_offset == expected_offset
        assert "s1" not in monitor._inflight
        assert monitor._delivery_failures.get("s1") is None

        # No new content and offset already at EOF: nothing to re-read.
        messages2, commits2 = await monitor.check_for_updates({"s1"})
        assert messages2 == []
        assert commits2 == {}

    @pytest.mark.asyncio
    async def test_three_failures_drop_batch_and_advance_offset(
        self, monitor, session_file, make_jsonl_entry, caplog
    ):
        await self._seed_and_append(monitor, session_file, make_jsonl_entry, "hello")
        expected_offset = session_file.stat().st_size

        async def raising_callback(_msg: NewMessage) -> None:
            raise RuntimeError("boom")

        monitor.set_message_callback(raising_callback)

        for attempt in range(1, 4):
            messages, commits = await monitor.check_for_updates({"s1"})
            assert [m.text for m in messages] == ["hello"], f"attempt {attempt}"
            with caplog.at_level("ERROR"):
                await monitor._dispatch_and_commit("s1", messages, commits["s1"])

        # After the 3rd failure the batch is dropped: offset committed anyway,
        # the failure counter reset, and the drop logged.
        assert monitor.state.get_session("s1").last_byte_offset == expected_offset
        assert monitor._delivery_failures.get("s1") is None
        assert any(
            "Dropping" in record.getMessage() and "s1" in record.getMessage()
            for record in caplog.records
        )

        # The dropped batch is gone for good: nothing left to re-read.
        messages, commits = await monitor.check_for_updates({"s1"})
        assert messages == []
        assert commits == {}

    @pytest.mark.asyncio
    async def test_inflight_session_is_not_reread_until_settled(
        self, monitor, session_file, make_jsonl_entry
    ):
        await self._seed_and_append(monitor, session_file, make_jsonl_entry, "hello")

        messages, commits = await monitor.check_for_updates({"s1"})
        assert len(messages) == 1

        started = asyncio.Event()
        release = asyncio.Event()

        async def stalling_callback(_msg: NewMessage) -> None:
            started.set()
            await release.wait()

        monitor.set_message_callback(stalling_callback)
        task = asyncio.create_task(
            monitor._dispatch_and_commit("s1", messages, commits["s1"])
        )
        await started.wait()
        assert "s1" in monitor._inflight

        # A poll cycle that lands while the batch is still in flight must not
        # re-read (and re-emit) it — nor race a second dispatch task for it.
        messages2, commits2 = await monitor.check_for_updates({"s1"})
        assert messages2 == []
        assert "s1" not in commits2

        release.set()
        await task
        assert "s1" not in monitor._inflight
