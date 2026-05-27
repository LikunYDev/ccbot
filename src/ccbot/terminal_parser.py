"""Terminal output parser — detects Claude Code UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, Permission Prompt,
    RestoreCheckpoint) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Status line (spinner characters + working text) by scanning from bottom up.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), strip_pane_chrome(), extract_bash_output().
"""

import re
from dataclasses import dataclass


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans lines top-down: the first line matching any `top` pattern
    marks the start, the first subsequent line matching any `bottom` pattern
    marks the end.  Both boundary lines are included in the extracted content.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient.  This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    # Numbered selector: "❯ 1. [ ] ..." or "  1. [x] ..." (tab bar scrolled off)
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*(?:❯\s+)?\d+\.\s+\["),),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line).
        # Distinguished from the ExitPlanMode numbered fallback by requiring
        # a 3rd option: PermissionPrompt has 3 choices (Yes / Yes,.. / No),
        # ExitPlanMode has 2 (Yes / No). Must come first so 3-option panes
        # aren't swallowed by the 2-option ExitPlanMode fallback.
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(re.compile(r"^\s*3\.\s"),),
        min_gap=2,
    ),
    # Fallback: numbered selector UI (❯ 1. Yes / 2. No) without old markers.
    # Kept last because its top marker is shared with PermissionPrompt and
    # BashApproval numbered prompts — those more-specific patterns must win
    # first when they have the "Do you want to proceed?" / "Bash command"
    # headers. Only reached for bare 2-option numbered selectors.
    UIPattern(
        name="ExitPlanMode",
        top=(re.compile(r"^\s*❯\s+\d+\.\s+Yes"),),
        bottom=(),  # extends to last non-empty line
        min_gap=1,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Feedback",
        top=(re.compile(r"How is Claude doing this session"),),
        bottom=(re.compile(r"0:\s*Dismiss"),),
        min_gap=1,
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]


# ── Post-processing ──────────────────────────────────────────────────────

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Bottom-anchored fallback ──────────────────────────────────────────────
#
# A tall interactive prompt (e.g. a multi-option AskUserQuestion) can push its
# *top* marker above the visible 80×24 pane, so the top-down extractor above
# misses it and the session hangs silently waiting for input it never asked the
# user for. But an interactive prompt is always the last thing on screen —
# Claude is blocked, nothing ever prints below it — so its *bottom* marker is
# always visible. These detectors anchor on that bottom marker and read upward,
# so detection can no longer silently fail on prompt height.
#
# Markers here must be SPECIFIC enough to name the UI unambiguously. The generic
# "Esc to cancel" is intentionally NOT used as an anchor: it is shared by several
# short UIs (permission/settings/bash) whose top marker does not scroll off, so
# the top-down pass already handles them.

# Option/selection structure expected inside a real prompt body. Guards the
# bottom-anchored path against false positives from incidental marker text in
# Claude's normal streaming output.
_RE_OPTION_HINT = re.compile(r"(?:❯|\b\d+[.)]\s|[☐✔☒]|\[[ xX]\])")

# A chrome separator (full-width box rule). An upward read stops here so two
# stacked UIs / prior output never merge into one block.
_RE_SEPARATOR = re.compile(r"^─{5,}$")

# How many trailing non-empty lines may hold the bottom marker. The live prompt
# sits at the very bottom, possibly under a few lines of input-box chrome.
_BOTTOM_ANCHOR_TAIL = 12
# Cap on how far up we read when the top marker is off-screen.
_BOTTOM_ANCHOR_MAX_HEIGHT = 80


@dataclass(frozen=True)
class _BottomAnchor:
    """A bottom marker that alone identifies an interactive UI, read upward."""

    name: str
    marker: tuple[re.Pattern[str], ...]  # specific, unambiguous bottom line
    top_hint: tuple[re.Pattern[str], ...]  # optional top markers to trim to


# Order matters (first match wins), like UI_PATTERNS.
_BOTTOM_ANCHORS: list[_BottomAnchor] = [
    _BottomAnchor(
        name="AskUserQuestion",
        # "Enter to select" is unique to the option selector footer; anchored at
        # line start so prose like "press Enter to select" can't trigger it.
        marker=(re.compile(r"^\s*Enter to select"),),
        top_hint=(
            re.compile(r"^\s*←\s+[☐✔☒]"),
            re.compile(r"^\s*[☐✔☒]"),
            re.compile(r"^\s*(?:❯\s+)?\d+\.\s+\["),
        ),
    ),
    _BottomAnchor(
        name="ExitPlanMode",
        marker=(re.compile(r"^\s*ctrl-g to edit in "),),
        top_hint=(
            re.compile(r"^\s*Would you like to proceed\?"),
            re.compile(r"^\s*Claude has written up a plan"),
        ),
    ),
]


def _try_extract_from_bottom(
    lines: list[str], anchor: _BottomAnchor
) -> InteractiveUIContent | None:
    """Detect a UI by its bottom marker alone, reading upward.

    Fallback for tall prompts whose top marker has scrolled out of the visible
    pane. The marker must appear within the last ``_BOTTOM_ANCHOR_TAIL``
    non-empty lines. Content is captured upward to the top hint if it is still
    visible, else to a chrome separator or a bounded floor.
    """
    bottom_idx: int | None = None
    seen_nonempty = 0
    for i in range(len(lines) - 1, -1, -1):
        if any(p.search(lines[i]) for p in anchor.marker):
            bottom_idx = i
            break
        if lines[i].strip():
            seen_nonempty += 1
            if seen_nonempty >= _BOTTOM_ANCHOR_TAIL:
                break
    if bottom_idx is None:
        return None

    floor = max(0, bottom_idx - _BOTTOM_ANCHOR_MAX_HEIGHT)
    top_idx = floor
    for i in range(bottom_idx - 1, floor - 1, -1):
        if any(p.search(lines[i]) for p in anchor.top_hint):
            top_idx = i
            break
        if _RE_SEPARATOR.match(lines[i].strip()):
            top_idx = i + 1
            break

    block = lines[top_idx : bottom_idx + 1]
    # Guard: a real prompt has option/selection structure in the captured block.
    if not any(_RE_OPTION_HINT.search(ln) for ln in block):
        return None

    content = "\n".join(block).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=anchor.name)


# ── Never-silent backstop ──────────────────────────────────────────────────
#
# If a known interactive footer is visible but no pattern (top-down or
# bottom-anchored) matched, a prompt still exists and the user must not be left
# in silence. ``build_degraded_prompt`` surfaces the visible region plus a note
# so the session never hangs unseen — the invariant that makes pane-as-source
# safe rather than merely usually-correct.

_FOOTER_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*Enter to select"),
    re.compile(r"^\s*Enter to confirm"),
    re.compile(r"^\s*Enter to continue"),
    re.compile(r"^\s*Esc to (?:cancel|exit)"),
    re.compile(r"^\s*ctrl-g to edit in "),
)

_DEGRADED_NOTE = (
    "⚠️ Claude is asking something I couldn't fully parse. Open the session to "
    "see the full prompt, or use the keys below."
)
# Cap on how many visible lines a degraded prompt shows.
_DEGRADED_MAX_LINES = 30


def has_interactive_footer(pane_text: str) -> bool:
    """True if a known interactive footer is in the last visible lines.

    Used by the never-silent backstop to detect that a prompt exists even when
    full extraction failed (e.g. an unrecognized future UI).
    """
    if not pane_text:
        return False
    nonempty = [ln for ln in pane_text.strip().split("\n") if ln.strip()]
    for ln in nonempty[-_BOTTOM_ANCHOR_TAIL:]:
        if any(p.search(ln) for p in _FOOTER_MARKERS):
            return True
    return False


def build_degraded_prompt(pane_text: str) -> InteractiveUIContent | None:
    """Best-effort content when a footer is visible but no pattern matched.

    Returns the visible UI region (chrome stripped) plus a note, named
    ``"UnknownPrompt"``. Returns None when no interactive footer is present.
    """
    if not has_interactive_footer(pane_text):
        return None
    lines = strip_pane_chrome(pane_text.strip().split("\n"))
    block = [ln for ln in lines if ln.strip()][-_DEGRADED_MAX_LINES:]
    body = _shorten_separators("\n".join(block)).rstrip()
    return InteractiveUIContent(
        content=f"{_DEGRADED_NOTE}\n\n{body}", name="UnknownPrompt"
    )


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern top-down in declaration order (first match wins),
    then falls back to bottom-anchored detection for tall prompts whose top
    marker has scrolled out of the visible pane. Returns None if no
    recognizable interactive UI is found.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result
    # Fallback: top marker off-screen — anchor on the always-visible bottom.
    for anchor in _BOTTOM_ANCHORS:
        result = _try_extract_from_bottom(lines, anchor)
        if result:
            return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) appears immediately above
    the chrome separator (a full line of ``─`` characters).  We locate
    the separator first, then check the line just above it — this avoids
    false positives from ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Find the chrome separator: topmost ──── line in the last 10 lines
    chrome_idx: int | None = None
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            chrome_idx = i
            break

    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Check lines just above the separator (skip blanks, up to 4 lines)
    for i in range(chrome_idx - 1, max(chrome_idx - 5, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First non-empty line above separator isn't a spinner → no status
        return None
    return None


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured tmux pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage settings tab.

    The /usage modal shows a Settings overlay with a "Usage" tab containing
    progress bars and reset times.  This parser looks for the Settings header
    line, then collects all content until "Esc to cancel".

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Find the Settings header that indicates we're in the usage modal
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            # The usage tab header line
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
