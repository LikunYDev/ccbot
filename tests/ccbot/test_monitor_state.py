"""Unit tests for MonitorState and TrackedSession persistence."""

import json

import pytest

from ccbot.monitor_state import MonitorState, TrackedSession


class TestTrackedSession:
    def test_to_dict_from_dict_roundtrip(self):
        original = TrackedSession(
            session_id="sess-1",
            file_path="/tmp/test.jsonl",
            last_byte_offset=42,
        )
        restored = TrackedSession.from_dict(original.to_dict())
        assert restored.session_id == "sess-1"
        assert restored.file_path == "/tmp/test.jsonl"
        assert restored.last_byte_offset == 42

    def test_from_dict_missing_fields_uses_defaults(self):
        session = TrackedSession.from_dict({})
        assert session.session_id == ""
        assert session.file_path == ""
        assert session.last_byte_offset == 0


class TestMonitorStateLoad:
    def test_load_missing_file(self, tmp_path):
        state = MonitorState(state_file=tmp_path / "missing.json")
        state.load()
        assert state.tracked_sessions == {}

    def test_load_valid_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        data = {
            "tracked_sessions": {
                "s1": {
                    "session_id": "s1",
                    "file_path": "/a.jsonl",
                    "last_byte_offset": 100,
                }
            }
        }
        state_file.write_text(json.dumps(data))
        state = MonitorState(state_file=state_file)
        state.load()
        assert "s1" in state.tracked_sessions
        assert state.tracked_sessions["s1"].last_byte_offset == 100

    def test_load_corrupt_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{invalid json!!!")
        state = MonitorState(state_file=state_file)
        state.load()
        assert state.tracked_sessions == {}


class TestMonitorStateLoadInvalidEntries:
    """f49/RC22 regression: a corrupt/missing session_id, file_path, or
    last_byte_offset must be skipped (with a warning), not silently
    defaulted — a defaulted last_byte_offset of 0 replays a session's
    entire history into its topic after a state-file glitch."""

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param(
                {"session_id": "s1", "file_path": "/a.jsonl"},
                id="missing-offset",
            ),
            pytest.param(
                {"session_id": "s1", "file_path": "/a.jsonl", "last_byte_offset": -1},
                id="negative-offset",
            ),
            pytest.param(
                {
                    "session_id": "s1",
                    "file_path": "/a.jsonl",
                    "last_byte_offset": "100",
                },
                id="string-offset",
            ),
            pytest.param(
                {"file_path": "/a.jsonl", "last_byte_offset": 10},
                id="missing-session-id",
            ),
            pytest.param(
                {"session_id": "", "file_path": "/a.jsonl", "last_byte_offset": 10},
                id="empty-session-id",
            ),
            pytest.param(
                {"session_id": "s1", "last_byte_offset": 10},
                id="missing-file-path",
            ),
            pytest.param(
                {"session_id": "s1", "file_path": "", "last_byte_offset": 10},
                id="empty-file-path",
            ),
            pytest.param("not-a-dict", id="entry-not-a-dict"),
        ],
    )
    def test_invalid_entry_skipped_with_warning(self, tmp_path, caplog, entry):
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps({"tracked_sessions": {"bad": entry}}), encoding="utf-8"
        )
        state = MonitorState(state_file=state_file)

        with caplog.at_level("WARNING", logger="ccbot.monitor_state"):
            state.load()

        assert state.tracked_sessions == {}
        assert any(
            "bad" in record.getMessage() and "invalid" in record.getMessage().lower()
            for record in caplog.records
        )

    def test_valid_entries_load_alongside_skipped_invalid_ones(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "tracked_sessions": {
                        "good": {
                            "session_id": "good",
                            "file_path": "/good.jsonl",
                            "last_byte_offset": 42,
                        },
                        "bad": {"session_id": "bad", "file_path": "/bad.jsonl"},
                    }
                }
            ),
            encoding="utf-8",
        )
        state = MonitorState(state_file=state_file)
        state.load()

        assert set(state.tracked_sessions) == {"good"}
        assert state.tracked_sessions["good"].last_byte_offset == 42

    def test_zero_offset_is_valid(self, tmp_path):
        """last_byte_offset == 0 is a legitimate value (brand-new tracking),
        not itself a sign of corruption."""
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "tracked_sessions": {
                        "s1": {
                            "session_id": "s1",
                            "file_path": "/a.jsonl",
                            "last_byte_offset": 0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        state = MonitorState(state_file=state_file)
        state.load()

        assert "s1" in state.tracked_sessions
        assert state.tracked_sessions["s1"].last_byte_offset == 0

    def test_boolean_offset_is_invalid(self, tmp_path):
        """JSON `true`/`false` decode to Python bool, a subclass of int —
        must not be accepted as a byte offset."""
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "tracked_sessions": {
                        "s1": {
                            "session_id": "s1",
                            "file_path": "/a.jsonl",
                            "last_byte_offset": True,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        state = MonitorState(state_file=state_file)
        state.load()

        assert state.tracked_sessions == {}


class TestMonitorStateSave:
    def test_save_writes_via_atomic_write(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state = MonitorState(state_file=state_file)
        state.update_session(
            TrackedSession(session_id="s1", file_path="/a.jsonl", last_byte_offset=10)
        )
        calls: list[tuple] = []

        def fake_write(path, data, indent=2):
            calls.append((path, data))

        monkeypatch.setattr("ccbot.utils.atomic_write_json", fake_write)
        state.save()
        assert len(calls) == 1
        path, data = calls[0]
        assert path == state_file
        assert "s1" in data["tracked_sessions"]
        assert data["tracked_sessions"]["s1"]["last_byte_offset"] == 10


class TestMonitorStateOperations:
    @pytest.fixture
    def state(self, tmp_path) -> MonitorState:
        return MonitorState(state_file=tmp_path / "state.json")

    @pytest.mark.parametrize(
        "key, expected_found",
        [
            pytest.param("s1", True, id="existing"),
            pytest.param("nonexistent", False, id="missing"),
        ],
    )
    def test_get_session(self, state, key, expected_found):
        session = TrackedSession(session_id="s1", file_path="/a.jsonl")
        state.tracked_sessions["s1"] = session
        result = state.get_session(key)
        if expected_found:
            assert result is session
        else:
            assert result is None

    def test_update_session_adds_new(self, state):
        session = TrackedSession(session_id="s1", file_path="/a.jsonl")
        state.update_session(session)
        assert state.tracked_sessions["s1"] is session

    def test_update_session_sets_dirty(self, state):
        state.update_session(TrackedSession(session_id="s1", file_path="/a.jsonl"))
        assert state._dirty is True

    def test_remove_session_deletes(self, state):
        state.tracked_sessions["s1"] = TrackedSession(
            session_id="s1", file_path="/a.jsonl"
        )
        state.remove_session("s1")
        assert "s1" not in state.tracked_sessions

    def test_remove_session_missing_no_error(self, state):
        state.remove_session("nonexistent")
        assert state.tracked_sessions == {}


class TestSaveIfDirty:
    def test_dirty_saves(self, tmp_path, monkeypatch):
        state = MonitorState(state_file=tmp_path / "state.json")
        state.update_session(TrackedSession(session_id="s1", file_path="/a.jsonl"))
        saved: list[bool] = []

        def fake_write(*_args, **_kwargs):
            saved.append(True)

        monkeypatch.setattr("ccbot.utils.atomic_write_json", fake_write)
        state.save_if_dirty()
        assert len(saved) == 1

    def test_not_dirty_skips_save(self, tmp_path, monkeypatch):
        state = MonitorState(state_file=tmp_path / "state.json")
        saved: list[bool] = []

        def fake_write(*_args, **_kwargs):
            saved.append(True)

        monkeypatch.setattr("ccbot.utils.atomic_write_json", fake_write)
        state.save_if_dirty()
        assert len(saved) == 0
