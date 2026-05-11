"""Unit tests for build_claude_command — shell command string assembly."""

from unittest.mock import patch

import pytest

from ccbot import tmux_manager as tm
from ccbot.tmux_manager import (
    _parse_group_session_names,
    build_claude_command,
    build_window_shell_cmd,
)


class TestBuildClaudeCommand:
    def test_plain_command(self):
        assert build_claude_command("claude") == "claude"

    def test_preserves_custom_base_command(self):
        # Matches the README pattern `IS_SANDBOX=1 claude`.
        assert build_claude_command("IS_SANDBOX=1 claude") == "IS_SANDBOX=1 claude"

    def test_permission_mode_appended(self):
        assert (
            build_claude_command("claude", permission_mode="auto")
            == "claude --permission-mode auto"
        )

    def test_empty_permission_mode_is_no_op(self):
        assert build_claude_command("claude", permission_mode="") == "claude"

    def test_resume_only(self):
        assert (
            build_claude_command("claude", resume_session_id="abc-123")
            == "claude --resume abc-123"
        )

    def test_permission_mode_precedes_resume(self):
        # Placing --permission-mode before --resume ensures the flag applies
        # to the resumed session (CLI order matters for some claude versions).
        assert (
            build_claude_command(
                "claude", permission_mode="auto", resume_session_id="abc-123"
            )
            == "claude --permission-mode auto --resume abc-123"
        )

    @pytest.mark.parametrize(
        "mode", ["default", "acceptEdits", "plan", "auto", "bypassPermissions"]
    )
    def test_each_mode_roundtrips(self, mode):
        result = build_claude_command("claude", permission_mode=mode)
        assert result == f"claude --permission-mode {mode}"


class TestBuildWindowShellCmd:
    """The window_shell value tmux runs as the pane's primary process.

    Structure: `PATH="<fallback>:$PATH" <inner>; exec <user_shell>`. tmux
    invokes this via `/bin/sh -c`, so shell features (`PATH=`, `;`, `exec`)
    are interpreted by the shell — verified via a tmux probe before the
    refactor landed.
    """

    def test_contains_inner_command_verbatim(self):
        result = build_window_shell_cmd("claude --resume abc", "/bin/zsh")
        assert "claude --resume abc" in result

    def test_prepends_fallback_path(self):
        result = build_window_shell_cmd("claude", "/bin/zsh")
        # The fallback dirs guard against launchd/systemd PATH being
        # stripped; same dirs as update_watcher's binary-resolution helper.
        assert ".local/bin" in result
        assert "/opt/homebrew/bin" in result
        assert "/usr/local/bin" in result
        assert "$PATH" in result

    def test_exec_user_shell_after_claude(self):
        # The trailing `; exec <shell>` keeps a debug shell in the pane after
        # claude exits — otherwise the window would silently disappear.
        result = build_window_shell_cmd("claude", "/bin/zsh")
        assert result.rstrip().endswith("; exec /bin/zsh")

    def test_preserves_env_var_prefix(self):
        # The README documents `CLAUDE_COMMAND=IS_SANDBOX=1 claude`; this
        # should pass through unchanged so the shell interprets the env var.
        result = build_window_shell_cmd("IS_SANDBOX=1 claude", "/bin/zsh")
        assert "IS_SANDBOX=1 claude" in result


class TestUserShell:
    def test_falls_back_to_zsh_when_pwd_lookup_raises(self):
        with patch("ccbot.tmux_manager.pwd.getpwuid", side_effect=KeyError("nope")):
            assert tm._user_shell() == "/bin/zsh"

    def test_falls_back_to_zsh_when_shell_field_empty(self):
        class _Stub:
            pw_shell = ""

        with patch("ccbot.tmux_manager.pwd.getpwuid", return_value=_Stub()):
            assert tm._user_shell() == "/bin/zsh"

    def test_returns_pwd_shell_when_set(self):
        class _Stub:
            pw_shell = "/usr/bin/fish"

        with patch("ccbot.tmux_manager.pwd.getpwuid", return_value=_Stub()):
            assert tm._user_shell() == "/usr/bin/fish"


class TestParseGroupSessionNames:
    def test_grouped_session_returns_all_peers(self):
        output = "ccbot|ccbot\nccbot-2|ccbot\nother|\n"
        assert _parse_group_session_names(output, "ccbot") == {"ccbot", "ccbot-2"}

    def test_ungrouped_session_does_not_match_other_ungrouped_sessions(self):
        output = "ccbot|\nother|\n"
        assert _parse_group_session_names(output, "ccbot") == {"ccbot"}

    def test_missing_configured_session_falls_back_to_literal_name(self):
        output = "other|other\nother-2|other\n"
        assert _parse_group_session_names(output, "ccbot") == {"ccbot"}
