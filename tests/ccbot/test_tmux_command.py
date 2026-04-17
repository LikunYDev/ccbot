"""Unit tests for build_claude_command — shell command string assembly."""

import pytest

from ccbot.tmux_manager import build_claude_command


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
