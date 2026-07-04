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


class TestHookMainCwdFallback:
    """SessionStart under a daemon-hosted claude (--bg-pty-host) runs with
    TMUX/TMUX_PANE stripped. The hook falls back to matching the session cwd
    against live panes on ccbot's socket, and only re-points a window entry
    that a prior in-pane fire already created — never creates or re-purposes
    one from a cwd guess."""

    SESSION_MAP = {
        "ccbot:@41": {
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

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        *,
        panes: str,
        source: str = "clear",
        seed_map: dict | None = None,
        ps_output: str = "",
    ) -> dict | None:
        """Run hook_main without TMUX_PANE; tmux list-panes returns `panes`,
        ps (used to filter idle shells on ambiguity) returns `ps_output`."""

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=ps_output, stderr=""
                )
            assert cmd[:2] == ["tmux", "-L"], cmd
            assert "list-panes" in cmd
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=panes, stderr=""
            )

        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        if seed_map is not None:
            (tmp_path / "session_map.json").write_text(json.dumps(seed_map))
        monkeypatch.setattr("ccbot.hook.subprocess.run", fake_run)
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        payload = {
            "session_id": "33333333-3333-3333-3333-333333333333",
            "cwd": "/proj",
            "hook_event_name": "SessionStart",
            "source": source,
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.delenv("TMUX", raising=False)
        hook_main()
        map_file = tmp_path / "session_map.json"
        return json.loads(map_file.read_text()) if map_file.exists() else None

    def test_unique_cwd_match_repoints_existing_entry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        result = self._run(
            monkeypatch,
            tmp_path,
            panes=("ccbot\t@41\tjob\t/proj\t100\nccbot\t@49\tother\t/other\t200\n"),
            seed_map=self.SESSION_MAP,
        )
        assert result is not None
        assert result["ccbot:@41"]["session_id"] == (
            "33333333-3333-3333-3333-333333333333"
        )
        # Unrelated entry untouched
        assert result["ccbot:@49"] == self.SESSION_MAP["ccbot:@49"]

    def test_no_prior_entry_refuses_to_bind(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """An outside-tmux claude in a directory matching some live pane must
        not create a mapping for that window."""
        seed = {"ccbot:@49": self.SESSION_MAP["ccbot:@49"]}
        result = self._run(
            monkeypatch,
            tmp_path,
            panes="ccbot\t@41\tjob\t/proj\t100\n",
            seed_map=seed,
        )
        assert result == seed

    def test_idle_shell_in_same_dir_filtered_out(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A bare shell parked in the project directory (no claude below it)
        must not block resolution — only the window running a claude client
        counts."""
        result = self._run(
            monkeypatch,
            tmp_path,
            panes=("7\t@29\tzsh\t/proj\t100\nccbot\t@41\tjob\t/proj\t200\n"),
            # pane 100 is an idle zsh; pane 200 has a claude child (201)
            ps_output=("100 1 zsh\n200 1 zsh\n201 200 claude\n"),
            seed_map=self.SESSION_MAP,
        )
        assert result is not None
        assert result["ccbot:@41"]["session_id"] == (
            "33333333-3333-3333-3333-333333333333"
        )

    def test_ambiguous_claude_windows_refuse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Two live windows on the same directory, both running claude — the
        window cannot be named, so the map must stay untouched."""
        result = self._run(
            monkeypatch,
            tmp_path,
            panes=("ccbot\t@41\tjob\t/proj\t100\nccbot\t@42\tjob-2\t/proj\t200\n"),
            ps_output=("100 1 zsh\n101 100 claude\n200 1 zsh\n201 200 claude\n"),
            seed_map=self.SESSION_MAP,
        )
        assert result == self.SESSION_MAP

    def test_startup_source_never_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A fresh `claude` launched outside tmux fires source=startup; the
        fallback is reserved for continuations (clear/compact/resume)."""
        result = self._run(
            monkeypatch,
            tmp_path,
            panes="ccbot\t@41\tjob\t/proj\t100\n",
            source="startup",
            seed_map=self.SESSION_MAP,
        )
        assert result == self.SESSION_MAP


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
