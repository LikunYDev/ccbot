"""Tests for Claude Code session tracking hook."""

import io
import json
import subprocess
import sys

import pytest

from ccbot.hook import _UUID_RE, _is_hook_installed, hook_main


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestIsHookInstalled:
    def test_hook_present(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "ccbot hook", "timeout": 5}
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True

    def test_no_hooks_key(self) -> None:
        assert _is_hook_installed({}) is False

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _is_hook_installed(settings) is False

    def test_full_path_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/ccbot hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_non_session_start_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()


class TestHookMainWritePath:
    """Tests that exercise the session_map write path with tmux mocked.

    These cover behavior the validation tests can't reach because they all
    short-circuit before the tmux query.
    """

    def _run_hook_main_with_tmux(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict,
        *,
        tmux_pane: str,
        tmux_output: str,
    ) -> None:
        """Run hook_main with `subprocess.run` mocked to return `tmux_output`."""

        def fake_run(cmd, *args, **kwargs):
            result = subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=tmux_output, stderr=""
            )
            return result

        monkeypatch.setattr("ccbot.hook.subprocess.run", fake_run)
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setenv("TMUX_PANE", tmux_pane)
        hook_main()

    def test_dedups_grouped_peer_entries_for_same_window_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Grouped tmux sessions share windows, so the hook can fire under
        peer A in one attach and peer B in another — both targeting the
        same window @48. Without dedup the old peer's key (with a now-stale
        session_id) lingers forever and downstream readers must guess which
        is current. Hook must atomically drop other-prefix entries for the
        same window_id when it writes."""
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        session_map_file = tmp_path / "session_map.json"
        session_map_file.write_text(
            json.dumps(
                {
                    "ccbot:@48": {
                        "session_id": "11111111-1111-1111-1111-111111111111",
                        "cwd": "/proj",
                        "window_name": "job",
                    },
                    "ccbot:@49": {  # different window — MUST be preserved
                        "session_id": "22222222-2222-2222-2222-222222222222",
                        "cwd": "/other",
                        "window_name": "other",
                    },
                }
            )
        )

        self._run_hook_main_with_tmux(
            monkeypatch,
            {
                "session_id": "33333333-3333-3333-3333-333333333333",
                "cwd": "/proj",
                "hook_event_name": "SessionStart",
            },
            tmux_pane="%99",
            tmux_output="ccbot-2:@48:job\n",
        )

        result = json.loads(session_map_file.read_text())
        assert result == {
            "ccbot-2:@48": {
                "session_id": "33333333-3333-3333-3333-333333333333",
                "cwd": "/proj",
                "window_name": "job",
            },
            "ccbot:@49": {
                "session_id": "22222222-2222-2222-2222-222222222222",
                "cwd": "/other",
                "window_name": "other",
            },
        }

    def test_overwrite_same_key_does_not_remove_unrelated_entries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """When the hook writes the same key it already had (claude restart
        in the same session), nothing else should be touched. Guards the
        dedup loop against a `k != session_window_key` slip that would
        delete the key it just wrote."""
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        session_map_file = tmp_path / "session_map.json"
        session_map_file.write_text(
            json.dumps(
                {
                    "ccbot:@48": {
                        "session_id": "11111111-1111-1111-1111-111111111111",
                        "cwd": "/proj",
                        "window_name": "job",
                    },
                    "ccbot:@49": {
                        "session_id": "22222222-2222-2222-2222-222222222222",
                        "cwd": "/other",
                        "window_name": "other",
                    },
                }
            )
        )

        self._run_hook_main_with_tmux(
            monkeypatch,
            {
                "session_id": "33333333-3333-3333-3333-333333333333",
                "cwd": "/proj",
                "hook_event_name": "SessionStart",
            },
            tmux_pane="%99",
            tmux_output="ccbot:@48:job\n",
        )

        result = json.loads(session_map_file.read_text())
        assert result == {
            "ccbot:@48": {
                "session_id": "33333333-3333-3333-3333-333333333333",
                "cwd": "/proj",
                "window_name": "job",
            },
            "ccbot:@49": {
                "session_id": "22222222-2222-2222-2222-222222222222",
                "cwd": "/other",
                "window_name": "other",
            },
        }
