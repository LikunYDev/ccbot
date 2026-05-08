# Default Directory for New-Topic Session Creation

## Goal

When ccbot creates a new Claude Code session for an unbound Telegram topic,
allow the directory browser to start at a configurable, pinned directory
instead of the bot process's current working directory. Pair this with the
existing `CLAUDE_PERMISSION_MODE=auto` support so a fresh new-topic session
launches Claude in `auto` permission mode at the pinned location.

## Motivation

The user's primary workflow is editing notes under `/Users/lkyao/obsidian`.
Today, the directory browser starts at `Path.cwd()`, which is wherever the
bot process happened to be launched (typically the repo root for dev,
`$HOME` under launchd). The user must navigate up and back down every time
they create a new topic. They also want every new session to default to
`auto` permission mode so file edits don't gate on prompts.

## Non-Goals

- Changing the in-window directory navigation (`..`, page nav) — those
  continue to work normally and can navigate above the pinned start path.
- Per-user pinned directories — single global env var only.
- Per-topic remembered start paths — out of scope.
- Adding any UI to configure the pinned dir — env var only.
- Changing the window picker flow (the picker for already-unbound tmux
  windows) — only the directory browser entry path is affected, which is
  what runs when there are no unbound windows or after the user clicks
  "New Session" from the picker.

## Design

### Configuration

Add one new env var:

| Var | Default | Effect |
|---|---|---|
| `CCBOT_DEFAULT_DIR` | unset (empty string) | Path to start the directory browser at when creating a new session. Empty / unset / nonexistent path falls back to `Path.cwd()`. |

The user's `.env` (in `~/.ccbot/.env` or repo `.env`) will set:

```
CCBOT_DEFAULT_DIR=/Users/lkyao/obsidian
CLAUDE_PERMISSION_MODE=auto
```

`CLAUDE_PERMISSION_MODE=auto` is already supported (`config.py:77-84`,
applied at `tmux_manager.py:594` via `build_window_shell_cmd`); no code
change required for that piece.

### Code Changes

**`src/ccbot/config.py`** — add a new attribute on `Config`:

```python
# Optional pinned starting directory for the new-session directory
# browser. Empty / unset / nonexistent path falls back to Path.cwd().
self.default_dir: str = os.getenv("CCBOT_DEFAULT_DIR", "").strip()
```

Place near the other `CCBOT_*` browse-related settings (e.g. just below
`show_hidden_dirs`).

**`src/ccbot/bot.py`** — extract a small helper to resolve the start path,
and call it at both sites that currently use `str(Path.cwd())` to seed the
directory browser:

- `bot.py:921` (unbound topic, no unbound windows path)
- `bot.py:1521` (window picker → "New Session" button transitions to the
  directory browser; same pattern)

Helper behavior:

1. If `config.default_dir` is non-empty:
   - Expand `~` and resolve.
   - If it exists and `is_dir()`, return as `str`.
   - Otherwise log a warning and fall through.
2. Return `str(Path.cwd())`.

The helper goes in `bot.py` (kept private to that module) to avoid widening
`config.py`'s surface. If a second consumer appears later we can move it.

### Failure Modes

- **Pinned path doesn't exist or isn't a directory**: log a warning at
  `Config` init time (or first use) and silently fall back to `Path.cwd()`.
  We don't crash the bot or refuse to create new sessions over a stale
  config value.
- **Pinned path is not readable**: `build_directory_browser` already
  catches `PermissionError, OSError` and renders an empty subdir list. The
  user can navigate up via `..` from there.

### Behavior Summary

After this change, for the user's setup:

- Send a message to a new topic with no unbound windows → directory
  browser opens at `/Users/lkyao/obsidian` (instead of the bot's cwd).
- Pick a folder → if existing sessions exist, session picker shows;
  otherwise window is created and `claude --permission-mode auto` is
  launched in that directory.
- All other flows (bound topics, window picker, resume) unchanged.

## Testing

Add tests under `tests/ccbot/test_config.py`:

1. `CCBOT_DEFAULT_DIR` unset → `config.default_dir == ""`.
2. `CCBOT_DEFAULT_DIR=/some/path` set → `config.default_dir == "/some/path"`.
3. Whitespace stripped.

Add tests for the bot helper (in an existing or new `tests/ccbot/test_bot.py`
section, depending on how testable that module is — we'll inspect during
implementation):

1. `default_dir` empty → returns `str(Path.cwd())`.
2. `default_dir` points to a real directory (use `tmp_path`) → returns it.
3. `default_dir` points to a nonexistent path → falls back to `Path.cwd()`.
4. `default_dir` points to a file (not a dir) → falls back to `Path.cwd()`.

If `bot.py` proves hard to import in isolation, we'll extract the helper
into a small utility (e.g. `bot_helpers.py` or `utils.py`) so it's directly
testable. We'll prefer the simpler in-`bot.py` placement first.

## Verification

Manual smoke test once implemented:

1. Set `CCBOT_DEFAULT_DIR=/Users/lkyao/obsidian` and
   `CLAUDE_PERMISSION_MODE=auto` in `~/.ccbot/.env`.
2. Restart the service (`./scripts/restart-macos.sh`).
3. Create a new Telegram topic with no unbound windows; send a message.
4. Confirm the directory browser opens rooted at `/Users/lkyao/obsidian`.
5. Pick a directory → confirm `claude --permission-mode auto` is the
   command run in the new tmux window (visible via `tmux list-windows`
   or by inspecting the pane's first line).

Pre-merge gates (per `CLAUDE.md`):

- `uv run ruff check src/ tests/`
- `uv run ruff format --check src/ tests/`
- `uv run pyright src/ccbot/`
- `uv run python -m pytest`
