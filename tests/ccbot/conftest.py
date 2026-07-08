"""Shared fixtures for ccbot unit tests.

Provides factories for building JSONL entries, content blocks,
and sample pane text for terminal parser tests.
"""

import time

import pytest

# ── JSONL entry factories ────────────────────────────────────────────────


@pytest.fixture
def make_jsonl_entry():
    """Factory: build a raw JSONL dict (pre-parse_line)."""

    def _make(
        msg_type: str = "assistant",
        content: list | str = "",
        *,
        timestamp: str | None = None,
        session_id: str = "test-session-id",
        cwd: str = "/tmp/test",
    ) -> dict:
        entry: dict = {
            "type": msg_type,
            "message": {"content": content},
            "sessionId": session_id,
            "cwd": cwd,
        }
        if timestamp:
            entry["timestamp"] = timestamp
        else:
            entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return entry

    return _make


@pytest.fixture
def make_text_block():
    """Factory: build a text content block."""

    def _make(text: str) -> dict:
        return {"type": "text", "text": text}

    return _make


@pytest.fixture
def make_tool_use_block():
    """Factory: build a tool_use content block."""

    def _make(
        tool_id: str = "tool_1",
        name: str = "Read",
        input_data: dict | None = None,
    ) -> dict:
        return {
            "type": "tool_use",
            "id": tool_id,
            "name": name,
            "input": input_data or {},
        }

    return _make


@pytest.fixture
def make_tool_result_block():
    """Factory: build a tool_result content block."""

    def _make(
        tool_use_id: str = "tool_1",
        content: str | list = "result text",
        *,
        is_error: bool = False,
    ) -> dict:
        block: dict = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        return block

    return _make


@pytest.fixture
def make_thinking_block():
    """Factory: build a thinking content block."""

    def _make(thinking: str = "deep thoughts") -> dict:
        return {"type": "thinking", "thinking": thinking}

    return _make


# ── Sample pane text for terminal parser ─────────────────────────────────


@pytest.fixture
def sample_pane_exit_plan():
    return (
        "  Would you like to proceed?\n"
        "  ─────────────────────────────────\n"
        "  Yes     No\n"
        "  ─────────────────────────────────\n"
        "  ctrl-g to edit in vim\n"
    )


@pytest.fixture
def sample_pane_ask_user_multi_tab():
    return "  ←  ☐ Option A\n     ☐ Option B\n     ☐ Option C\n  Enter to select\n"


@pytest.fixture
def sample_pane_ask_user_single_tab():
    return "  ☐ Option A\n  ☐ Option B\n  Enter to select\n"


@pytest.fixture
def sample_pane_permission():
    return "  Do you want to proceed?\n  Some permission details\n  Esc to cancel\n"


_CHROME = (
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  [Opus 4.6] Context: 50%\n"
)


@pytest.fixture
def chrome():
    return _CHROME


@pytest.fixture
def sample_pane_status_line():
    return "Some output text here\nMore output\n✻ Reading file src/main.py\n" + _CHROME


@pytest.fixture
def sample_pane_settings():
    """Realistic Claude Code /model picker as captured from tmux."""
    return (
        " Select model\n"
        " Switch between Claude models. Applies to this session and future Claude Code sessions.\n"
        "\n"
        "   1. Default (recommended)  Opus 4.6 · Most capable for complex work\n"
        " ❯ 2. Sonnet                 Sonnet 4.6 · Best for everyday tasks\n"
        "   3. Haiku                  Haiku 4.5 · Fastest for quick answers\n"
        "\n"
        " Use /fast to turn on Fast mode (Opus 4.6 only).\n"
        "\n"
        " Enter to confirm · Esc to exit\n"
    )


@pytest.fixture
def sample_pane_exit_plan_numbered():
    """Realistic pane showing numbered ExitPlanMode selector (current format).

    The old markers (Would you like to proceed?, ctrl-g to edit in) are NOT
    present — only the ❯ 1. Yes / 2. No selector with plan context above.
    """
    return (
        "  I've written a plan to the plan file.\n"
        "\n"
        "  Here's a summary of what I'll do:\n"
        "  1. Update the config parser\n"
        "  2. Add validation logic\n"
        "\n"
        "  ❯ 1. Yes\n"
        "    2. No\n"
        "\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  [Opus 4.6] Context: 34%\n"
    )


@pytest.fixture
def sample_pane_no_ui():
    return "$ echo hello\nhello\n$\n"


# Bottom chrome with a custom statusLine configured, as captured live
# (2026-07, Claude Code 2.x). The statusline sits below the second separator,
# above the mode indicator.
_CHROME_WITH_STATUSLINE = (
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  ~/ccbot (main) | Fable 5 | ctx: 11% | cost: $4.88\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
)


@pytest.fixture
def chrome_with_statusline():
    return _CHROME_WITH_STATUSLINE


@pytest.fixture
def sample_pane_working_asterisk():
    """Captured live: `*` spinner frame with a Tip hint line between the
    spinner and the separator."""
    return (
        "● Capturing now while it's working:\n"
        "\n"
        "● Running 2 shell commands…\n"
        "  ⎿  $ tmux -L ccbot capture-pane -p -t ccbot:@85 | tail -20\n"
        "\n"
        "* Puttering… (22s · ↓ 270 tokens)\n"
        "  ⎿  Tip: Use /btw to ask a quick side question\n"
        "\n" + _CHROME_WITH_STATUSLINE
    )


@pytest.fixture
def sample_pane_turn_end():
    """Idle pane right after a turn: static turn-end summary line above the
    separator, statusline in the bottom chrome."""
    return (
        "● Done. The fix is in place and tests pass.\n"
        "\n"
        "✻ Cogitated for 1m 12s\n"
        "\n" + _CHROME_WITH_STATUSLINE
    )


@pytest.fixture
def sample_pane_footer_with_task_hud():
    """Captured live: statusline followed by the background-task HUD."""
    return (
        "● Report text here.\n"
        "\n"
        "✻ Waiting for 1 background agent to finish\n"
        "\n"
        "──────────────────────────────────────\n"
        "❯ wait for the render and publish it\n"
        "──────────────────────────────────────\n"
        "  ~/huobi | Opus 4.8 | ctx: 28% | cost: $27.43\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
        "\n"
        "  ● main\n"
        "  ◯ general-purpose  Render interactive system map HTML"
        "                       2m 20s · ↓ 52.4k tokens\n"
    )
