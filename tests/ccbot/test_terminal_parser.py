"""Tests for terminal_parser — regex-based detection of Claude Code UI elements."""

import pytest

from ccbot.terminal_parser import (
    build_degraded_prompt,
    extract_bash_output,
    extract_interactive_content,
    has_interactive_footer,
    is_interactive_ui,
    parse_status_line,
    strip_pane_chrome,
)

# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task", "Working on task"),
            ("✻", "  Reading file  ", "Reading file"),
            ("✽", "Thinking deeply", "Thinking deeply"),
            ("✶", "Analyzing code", "Analyzing code"),
            ("✳", "Processing input", "Processing input"),
            ("✢", "Building project", "Building project"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py"


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Claude has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Claude has written up a plan" in result.content

    def test_exit_plan_mode_numbered_selector(
        self, sample_pane_exit_plan_numbered: str
    ):
        """New numbered ❯ 1. Yes / 2. No format is detected as ExitPlanMode."""
        result = extract_interactive_content(sample_pane_exit_plan_numbered)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "❯" in result.content
        assert "Yes" in result.content

    def test_exit_plan_mode_old_format_still_works(self, sample_pane_exit_plan: str):
        """Backward compat: old ExitPlanMode format still detected."""
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_permission_prompt(self, sample_pane_permission: str):
        result = extract_interactive_content(sample_pane_permission)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Do you want to proceed?" in result.content

    def test_permission_prompt_three_option_numbered_not_misclassified(self):
        """Regression: a PermissionPrompt with 3-option numbered selector
        (❯ 1. Yes / 2. Yes,... / 3. No) must not be swallowed by the
        ExitPlanMode numbered fallback. The 'Do you want to proceed?' header
        is the specific marker and must win.
        """
        pane = (
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. Yes, and don't ask again for: launchctl list *\n"
            "   3. No\n"
            "\n"
            " Esc to cancel · Tab to amend · ctrl+e to explain\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "PermissionPrompt"

    def test_permission_prompt_three_option_without_header(self):
        """Even without the 'Do you want to proceed?' header, a 3-option
        numbered selector should be classified as PermissionPrompt (min_gap=2
        filter); only bare 2-option Yes/No falls through to ExitPlanMode.
        """
        pane = " ❯ 1. Yes\n   2. Yes, allow access to foo/\n   3. No\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "PermissionPrompt"

    def test_exit_plan_numbered_two_option_still_exit_plan(self):
        """Fallback ordering invariant: a bare 2-option ❯ 1. Yes / 2. No
        pane (no other markers) still classifies as ExitPlanMode."""
        pane = "  Here's my plan.\n\n  ❯ 1. Yes\n    2. No\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● claude-sonnet-4-20250514\n"
            "  ○ claude-opus-4-20250514\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ claude-sonnet-4-20250514\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_min_gap_too_small_returns_none(self):
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None


# ── bottom-anchored detection (tall prompts, top marker off-screen) ───────


class TestBottomAnchoredDetection:
    def test_ask_user_top_marker_scrolled_off(self):
        """Incident repro (2026-05-27): a tall AskUserQuestion whose tab/checkbox
        header has scrolled above the 80×24 viewport. The top-down extractor
        misses it (no top marker visible); the bottom-anchored fallback must
        still detect it via the always-visible 'Enter to select' footer."""
        pane = (
            'When you say "escalations" are over-engineered, which do you mean?\n'
            "❯ 1. The note tier ladder\n"
            "  2. Cross-scope forced-preview\n"
            "  3. The auto-archive sweep\n"
            "  4. Something else\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.7] Context: 41%\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "The note tier ladder" in result.content

    def test_no_false_positive_on_prose_with_marker(self):
        """A line starting 'Enter to select' but lacking option structure
        (no ❯/number/checkbox) must NOT be detected as a prompt."""
        pane = (
            "Here is how the model picker works in general.\n"
            "Enter to select is the phrase shown in its footer.\n"
        )
        assert extract_interactive_content(pane) is None

    def test_top_down_still_wins_when_top_visible(self):
        """When the top marker is visible the normal top-down path handles it;
        the fallback must not change the outcome."""
        pane = "  ☐ Option A\n  ☐ Option B\n  Enter to select\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "AskUserQuestion"


# ── never-silent backstop (footer present, no pattern matched) ────────────


class TestDegradedBackstop:
    def test_has_footer_true_for_known_footers(self):
        assert has_interactive_footer("body\nEnter to select · Esc to cancel\n")
        assert has_interactive_footer("plan text\nctrl-g to edit in vim\n")
        assert has_interactive_footer("stuff\nEsc to cancel\n")

    def test_has_footer_false_for_plain_output(self, sample_pane_no_ui: str):
        assert has_interactive_footer(sample_pane_no_ui) is False
        assert has_interactive_footer("") is False

    def test_degraded_prompt_for_unknown_ui(self):
        """An unrecognized dialog with a known footer but no matching top marker
        yields a degraded view (note + visible body), never silence."""
        pane = (
            "Some brand-new dialog we don't have a pattern for\n"
            "  with a couple of lines\n"
            "Esc to cancel\n"
        )
        # No pattern should match this (no recognized top marker).
        assert extract_interactive_content(pane) is None
        degraded = build_degraded_prompt(pane)
        assert degraded is not None
        assert degraded.name == "UnknownPrompt"
        assert "brand-new dialog" in degraded.content
        assert "couldn't fully parse" in degraded.content

    def test_degraded_none_without_footer(self, sample_pane_no_ui: str):
        assert build_degraded_prompt(sample_pane_no_ui) is None


# ── is_interactive_ui ────────────────────────────────────────────────────


class TestIsInteractiveUI:
    def test_true_when_ui_present(self, sample_pane_exit_plan: str):
        assert is_interactive_ui(sample_pane_exit_plan) is True

    def test_false_when_no_ui(self, sample_pane_no_ui: str):
        assert is_interactive_ui(sample_pane_no_ui) is False

    def test_settings_is_interactive(self, sample_pane_settings: str):
        assert is_interactive_ui(sample_pane_settings) is True

    def test_false_for_empty_string(self):
        assert is_interactive_ui("") is False


# ── strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")
