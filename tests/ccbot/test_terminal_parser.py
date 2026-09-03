"""Tests for terminal_parser — regex-based detection of Claude Code UI elements."""

import pytest

from ccbot.terminal_parser import (
    build_degraded_prompt,
    extract_bash_output,
    extract_interactive_content,
    has_interactive_footer,
    is_interactive_ui,
    is_turn_end_status,
    parse_chrome_footer,
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
            ("*", "Puttering… (22s · ↓ 270 tokens)", "Puttering… (22s · ↓ 270 tokens)"),
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

    def test_tip_line_between_spinner_and_chrome(
        self, sample_pane_working_asterisk: str
    ):
        """Regression: a `⎿ Tip: …` hint line between the spinner and the
        separator must be skipped, not treated as 'no status'."""
        assert (
            parse_status_line(sample_pane_working_asterisk)
            == "Puttering… (22s · ↓ 270 tokens)"
        )

    def test_mode_hint_line_skipped(self, chrome: str):
        """A ⏵⏵ hint line above the separator is skipped too."""
        pane = f"output\n✻ Doing work\n⏵⏵ accept edits on\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_content_line_between_spinner_and_chrome_blocks(self, chrome: str):
        """A regular content line above the separator still means no status —
        the ·-bullet false-positive fix must survive the hint-skipping."""
        pane = f"✻ Doing work\nsome regular output\n{chrome}"
        assert parse_status_line(pane) is None

    def test_turn_end_summary_parses(self, sample_pane_turn_end: str):
        """The static turn-end line parses as a status (classification is
        is_turn_end_status's job, not the parser's)."""
        assert parse_status_line(sample_pane_turn_end) == "Cogitated for 1m 12s"

    def test_todo_hud_between_spinner_and_chrome(self, chrome: str):
        """Regression: the todo-list HUD under the spinner (first line
        `⎿`-prefixed, continuations indented with ◼/◻/… glyphs) must be
        skipped, and the scan must reach over its full height."""
        pane = (
            "· Wave 3: building the identity foundation… (39m 42s · ↓ 90.9k tokens)\n"
            "  ⎿  ◼ Wave 3: identity — lineage spawn guard (#85)\n"
            "     ◻ Wave 4: surface unparseable [repeat::] in sync status (#82)\n"
            "     ◻ Wave 5: migration safety (#81)\n"
            "     ◻ Wave 6: GM window aligned (#84) and decluttered (#83)\n"
            "     ◻ Final: full suite, integration review, push branch\n"
            "      … +3 completed\n"
            "\n" + chrome
        )
        assert parse_status_line(pane) == (
            "Wave 3: building the identity foundation… (39m 42s · ↓ 90.9k tokens)"
        )

    def test_queued_message_echo_skipped(self, chrome: str):
        """Regression: a queued user message echoed above the separator
        (`  ❯ Where are we`) must not hide the spinner."""
        pane = (
            "✻ Wave 2: fixing the sync engine… (32m 12s · ↓ 71.9k tokens)\n"
            "  ⎿  ◼ Wave 2: sync engine — adopt (#79), re-date wedge (#80)\n"
            "     ◻ Wave 3: identity — lineage spawn guard (#85)\n"
            "      … +1 pending, 2 completed\n"
            "\n"
            "  ❯ Where are we\n"
            "\n" + chrome
        )
        assert parse_status_line(pane) == (
            "Wave 2: fixing the sync engine… (32m 12s · ↓ 71.9k tokens)"
        )

    def test_content_line_below_hud_still_blocks(self, chrome: str):
        """A regular content line between the HUD and the separator still
        stops the scan — skippable lines widen the reach, not the guard."""
        pane = f"✻ Doing work\n  ⎿  ◼ some task\nsome regular output\n{chrome}"
        assert parse_status_line(pane) is None

    def test_wrapped_tip_continuation_skipped(self, chrome: str):
        """Regression: at 80 columns the Tip hint wraps, and its continuation
        line (indented under the hint text, no glyph) must not stop the
        scan. Captured live from an 80x24 pane."""
        pane = (
            "· Actioning… (6m 43s · ↓ 27.7k tokens)\n"
            "  ⎿  Tip: Use /btw to ask a quick side question without "
            "interrupting Claude's\n"
            "     current work\n"
            "\n" + chrome
        )
        assert parse_status_line(pane) == "Actioning… (6m 43s · ↓ 27.7k tokens)"

    def test_labeled_separator_recognized(self):
        """Regression: with ultracode on, the separator carries a
        right-aligned label ("──── ultracode ─") and is still the chrome
        separator."""
        pane = (
            "✻ Cogitated for 1m 12s\n"
            "\n" + "─" * 68 + " ultracode ─\n"
            "❯ \n" + "─" * 80 + "\n"
            "  ~/ccbot (main) | Fable 5.1 | ctx: 7% | cost: $1.50\n"
        )
        assert parse_status_line(pane) == "Cogitated for 1m 12s"

    def test_rule_followed_by_text_is_not_a_separator(self):
        """The label sits inside the rule (dashes on both sides); a rule
        followed by trailing text is output, not chrome."""
        pane = "· Working… (3s)\n" + "─" * 30 + " not chrome\n"
        assert parse_status_line(pane) is None


# ── is_turn_end_status ───────────────────────────────────────────────────


class TestIsTurnEndStatus:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("Cogitated for 1m 12s", True, id="minutes_seconds"),
            pytest.param("Churned for 9m 58s", True, id="churned"),
            pytest.param("Worked for 45s", True, id="seconds_only"),
            pytest.param("Baked for 1h 2m 3s", True, id="hours"),
            pytest.param(
                "Puttering… (22s · ↓ 270 tokens)", False, id="live_with_stats"
            ),
            pytest.param(
                "Waiting for 1 background agent to finish", False, id="waiting_prose"
            ),
            pytest.param("Reading file src/main.py", False, id="tool_status"),
            pytest.param("", False, id="empty"),
        ],
    )
    def test_classification(self, text: str, expected: bool):
        assert is_turn_end_status(text) is expected


# ── parse_chrome_footer ──────────────────────────────────────────────────


class TestParseChromeFooter:
    def test_custom_statusline(self, sample_pane_turn_end: str):
        assert (
            parse_chrome_footer(sample_pane_turn_end)
            == "~/ccbot (main) | Fable 5 | ctx: 11% | cost: $4.88"
        )

    def test_statusline_while_working(self, sample_pane_working_asterisk: str):
        """Footer is present mid-turn too — extraction is layout-based."""
        assert (
            parse_chrome_footer(sample_pane_working_asterisk)
            == "~/ccbot (main) | Fable 5 | ctx: 11% | cost: $4.88"
        )

    def test_task_hud_below_statusline_excluded(
        self, sample_pane_footer_with_task_hud: str
    ):
        """● / ◯ background-task HUD lines below the statusline are not part
        of the footer."""
        assert (
            parse_chrome_footer(sample_pane_footer_with_task_hud)
            == "~/huobi | Opus 4.8 | ctx: 28% | cost: $27.43"
        )

    def test_default_chrome_line(self, chrome: str):
        """Without a custom statusLine the default model/context line is the
        footer — whatever is rendered there is returned verbatim."""
        pane = f"some output\n{chrome}"
        assert parse_chrome_footer(pane) == "[Opus 4.6] Context: 50%"

    def test_mode_indicator_only_returns_none(self):
        pane = (
            "output\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
        )
        assert parse_chrome_footer(pane) is None

    def test_multiline_statusline_preserved(self):
        pane = (
            "output\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  line one of statusline\n"
            "  line two of statusline\n"
            "  ⏵⏵ auto mode on\n"
        )
        assert (
            parse_chrome_footer(pane)
            == "line one of statusline\nline two of statusline"
        )

    def test_no_second_separator_returns_none(self):
        pane = "output\n──────────────────────────────────────\n❯ \n"
        assert parse_chrome_footer(pane) is None

    def test_empty_pane_returns_none(self):
        assert parse_chrome_footer("") is None

    def test_labeled_separator_recognized(self):
        """Regression: the ultracode label on the first separator must not
        hide the statusline below the second."""
        pane = (
            "output\n" + "─" * 68 + " ultracode ─\n"
            "❯ \n" + "─" * 80 + "\n"
            "  ~/ccbot (main) | Fable 5.1 | ctx: 7% | cost: $1.50\n"
            "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
        )
        assert (
            parse_chrome_footer(pane)
            == "~/ccbot (main) | Fable 5.1 | ctx: 7% | cost: $1.50"
        )


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

    def test_exit_plan_mode_numbered_selector_excludes_trailing_chrome(
        self, sample_pane_exit_plan_numbered: str
    ):
        """The bare-numbered ExitPlanMode fallback has no bottom marker, so
        it used to extend to the last non-empty line of the WHOLE pane —
        swallowing the standing chrome (prompt box, status bar) below the
        dialog. It must stop at the dialog itself instead."""
        result = extract_interactive_content(sample_pane_exit_plan_numbered)
        assert result is not None
        assert "Context:" not in result.content
        assert "─" not in result.content

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

    def test_ask_user_multi_tab_excludes_trailing_chrome(
        self, sample_pane_ask_user_multi_tab: str, chrome: str
    ):
        """The multi-tab AskUserQuestion pattern has no bottom marker either
        (the footer varies per tab), so it must stop at its own "Enter to
        select" footer rather than at the last non-empty line of the pane,
        which would otherwise pull in the standing chrome below it."""
        pane = sample_pane_ask_user_multi_tab + chrome
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content
        assert "Context:" not in result.content
        assert "─" not in result.content

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


# ── session-resume cost prompt ────────────────────────────────────────────


class TestResumeSessionPrompt:
    """Claude Code's daemon/resume flow asks whether to resume a large session
    from a summary or in full. Previously unrecognized — fell to the degraded
    UnknownPrompt backstop."""

    PANE = (
        " Resuming the full session will consume a substantial portion of your\n"
        " usage limits. We recommend resuming from a summary.\n"
        "\n"
        " ❯ 1. Resume from summary (recommended)\n"
        "   2. Resume full session as-is\n"
        "   3. Don't ask me again\n"
        "\n"
        " Enter to confirm · Esc to cancel\n"
    )

    def test_full_prompt_extracts(self):
        result = extract_interactive_content(self.PANE)
        assert result is not None
        assert result.name == "ResumeSession"
        assert "Resuming the full session" in result.content
        assert "Resume from summary (recommended)" in result.content
        assert "Don't ask me again" in result.content

    def test_description_scrolled_off_still_extracts(self):
        pane = "\n".join(self.PANE.split("\n")[3:])
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ResumeSession"
        assert "Resume full session as-is" in result.content

    def test_selection_on_other_option_still_extracts(self):
        pane = (
            "   1. Resume from summary (recommended)\n"
            " ❯ 2. Resume full session as-is\n"
            "   3. Don't ask me again\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ResumeSession"

    def test_start_new_session_variant(self):
        pane = " ❯ 1. Resume from summary (recommended)\n   2. Start new session\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ResumeSession"

    def test_is_interactive(self):
        assert is_interactive_ui(self.PANE) is True


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

    def test_degraded_trims_scrollback_above_separator(self):
        """Unrelated output above the dialog's chrome separator must not be
        included — the dialog is the message, not an appendix to scrollback."""
        pane = (
            "line of earlier assistant prose\n"
            "more earlier prose that is not part of the dialog\n"
            "──────────────────────────────\n"
            "Some brand-new dialog we don't have a pattern for\n"
            "  1. option one\n"
            "  2. option two\n"
            "  3. option three\n"
            "  4. option four\n"
            "  5. option five\n"
            "Esc to cancel\n"
            "\n"
            "──────────────────────────────\n"
            " ❯\n"
            "──────────────────────────────\n"
            "  status line\n"
        )
        degraded = build_degraded_prompt(pane)
        assert degraded is not None
        assert "brand-new dialog" in degraded.content
        assert "option two" in degraded.content
        assert "earlier prose" not in degraded.content

    def test_degraded_without_separator_keeps_tail_behavior(self):
        """No separator above the dialog: fall back to the last-N-lines tail
        (never worse than before)."""
        pane = (
            "some previous output\n"
            "Some brand-new dialog we don't have a pattern for\n"
            "Esc to cancel\n"
        )
        degraded = build_degraded_prompt(pane)
        assert degraded is not None
        assert "brand-new dialog" in degraded.content
        assert "some previous output" in degraded.content

    def test_degraded_separator_below_dialog_falls_back(self):
        """A separator *below* the dialog (stray chrome) must not trim the
        dialog away — the footer check rejects the trimmed region."""
        pane = (
            "Some brand-new dialog we don't have a pattern for\n"
            "Esc to cancel\n"
            "──────────────────────────────\n"
            "❯ input line\n"
        )
        degraded = build_degraded_prompt(pane)
        assert degraded is not None
        assert "brand-new dialog" in degraded.content


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

    def test_labeled_separator_strips(self):
        lines = [
            "some output",
            "─" * 68 + " ultracode ─",
            "❯",
            "─" * 80,
            "  statusline",
        ]
        assert strip_pane_chrome(lines) == ["some output"]


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
