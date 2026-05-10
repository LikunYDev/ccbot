# Pinned Default Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `CCBOT_DEFAULT_DIR` env var so the new-session directory browser opens at a configurable pinned path instead of the bot's `cwd`.

**Architecture:** One new field on `Config` (`default_dir`), plus a small private helper in `bot.py` that returns either the configured path (if it exists and is a directory) or `Path.cwd()`. Both directory-browser entry sites call the helper. `CLAUDE_PERMISSION_MODE=auto` is already supported and needs no code change.

**Tech Stack:** Python 3, python-telegram-bot, pytest, ruff, pyright, uv.

**Spec:** [`docs/superpowers/specs/2026-05-08-default-dir-design.md`](../specs/2026-05-08-default-dir-design.md)

---

## File Structure

- Modify: `src/ccbot/config.py` — add `self.default_dir` field.
- Modify: `src/ccbot/bot.py` — add private `_resolve_browser_start_path()` helper; call it at the two existing directory-browser entry sites.
- Modify: `tests/ccbot/test_config.py` — new `TestConfigDefaultDir` class.
- Create: `tests/ccbot/test_bot_helpers.py` — focused tests for the helper. (Keeping it in its own file because `bot.py` is heavy to import in tests; the helper itself is pure.)

The helper goes in `bot.py` (private, single consumer in the module). If it grows a second consumer outside `bot.py`, lift to `utils.py` later — not now.

---

## Task 1: Add `default_dir` field to `Config`

**Files:**
- Modify: `src/ccbot/config.py` (insert after the `show_hidden_dirs` block, ~line 130)
- Test: `tests/ccbot/test_config.py` (append a new test class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/ccbot/test_config.py`:

```python
@pytest.mark.usefixtures("_base_env")
class TestConfigDefaultDir:
    def test_default_is_empty(self, monkeypatch):
        monkeypatch.delenv("CCBOT_DEFAULT_DIR", raising=False)
        cfg = Config()
        assert cfg.default_dir == ""

    def test_set_value(self, monkeypatch):
        monkeypatch.setenv("CCBOT_DEFAULT_DIR", "/Users/lkyao/obsidian")
        cfg = Config()
        assert cfg.default_dir == "/Users/lkyao/obsidian"

    def test_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("CCBOT_DEFAULT_DIR", "  /tmp/foo  ")
        cfg = Config()
        assert cfg.default_dir == "/tmp/foo"

    def test_empty_string_is_no_op(self, monkeypatch):
        monkeypatch.setenv("CCBOT_DEFAULT_DIR", "")
        cfg = Config()
        assert cfg.default_dir == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/ccbot/test_config.py::TestConfigDefaultDir -v`

Expected: 4 failures with `AttributeError: 'Config' object has no attribute 'default_dir'`.

- [ ] **Step 3: Add the field to `Config`**

In `src/ccbot/config.py`, find the `show_hidden_dirs` block (around line 127-130):

```python
        # Show hidden (dot) directories in directory browser
        self.show_hidden_dirs = (
            os.getenv("CCBOT_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )
```

Insert immediately after it:

```python
        # Pinned starting directory for the new-session directory browser.
        # Empty / unset / nonexistent path falls back to Path.cwd() at use time.
        self.default_dir: str = os.getenv("CCBOT_DEFAULT_DIR", "").strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/ccbot/test_config.py::TestConfigDefaultDir -v`

Expected: 4 passed.

- [ ] **Step 5: Run full config test file**

Run: `uv run python -m pytest tests/ccbot/test_config.py -v`

Expected: all tests pass (no regressions).

- [ ] **Step 6: Commit**

```bash
git add src/ccbot/config.py tests/ccbot/test_config.py
git commit -m "$(cat <<'EOF'
feat: add CCBOT_DEFAULT_DIR config field

Reads CCBOT_DEFAULT_DIR env var into Config.default_dir for use by
the directory browser entry path. Empty / unset stays as fallback
to Path.cwd() at the call site.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `_resolve_browser_start_path` helper in `bot.py` (TDD via pure helper test file)

**Files:**
- Modify: `src/ccbot/bot.py` (add helper near top, after imports)
- Create: `tests/ccbot/test_bot_helpers.py`

The helper is intentionally pure (no telegram/tmux deps) so the test file imports it directly. We test the helper in isolation; the call-site wiring is covered by Task 3 plus manual verification.

- [ ] **Step 1: Write the failing tests**

Create `tests/ccbot/test_bot_helpers.py`:

```python
"""Unit tests for pure helpers exposed from bot.py."""

from pathlib import Path

import pytest

from ccbot.bot import _resolve_browser_start_path


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestResolveBrowserStartPath:
    def test_unset_returns_cwd(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CCBOT_DEFAULT_DIR", raising=False)
        # _resolve_browser_start_path reads from the live `config` singleton,
        # so reload it via a fresh import path. Easiest: monkeypatch the
        # attribute on the imported config instance.
        from ccbot.config import config as live_config

        monkeypatch.setattr(live_config, "default_dir", "")
        assert _resolve_browser_start_path() == str(Path.cwd())

    def test_existing_dir_returned(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        target = tmp_path / "obsidian"
        target.mkdir()
        monkeypatch.setattr(live_config, "default_dir", str(target))
        assert _resolve_browser_start_path() == str(target.resolve())

    def test_tilde_expansion(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        # tmp_path is guaranteed to exist; build a "~"-style path that
        # resolves to it by setting HOME.
        monkeypatch.setenv("HOME", str(tmp_path))
        sub = tmp_path / "notes"
        sub.mkdir()
        monkeypatch.setattr(live_config, "default_dir", "~/notes")
        assert _resolve_browser_start_path() == str(sub.resolve())

    def test_nonexistent_path_falls_back_to_cwd(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        monkeypatch.setattr(
            live_config, "default_dir", str(tmp_path / "does-not-exist")
        )
        assert _resolve_browser_start_path() == str(Path.cwd())

    def test_path_to_file_falls_back_to_cwd(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        f = tmp_path / "a-file"
        f.write_text("hi")
        monkeypatch.setattr(live_config, "default_dir", str(f))
        assert _resolve_browser_start_path() == str(Path.cwd())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/ccbot/test_bot_helpers.py -v`

Expected: `ImportError: cannot import name '_resolve_browser_start_path' from 'ccbot.bot'`.

- [ ] **Step 3: Add the helper to `bot.py`**

In `src/ccbot/bot.py`, insert immediately after the existing `logger = logging.getLogger(__name__)` line (currently at line 141):

```python
def _resolve_browser_start_path() -> str:
    """Return the directory the new-session browser should open at.

    Uses ``config.default_dir`` if set and pointing to an existing
    directory; otherwise falls back to the bot process's cwd.
    """
    pinned = config.default_dir
    if pinned:
        candidate = Path(pinned).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved.is_dir():
            return str(resolved)
        logger.warning(
            "CCBOT_DEFAULT_DIR=%r is not a directory; falling back to cwd",
            pinned,
        )
    return str(Path.cwd())
```

(`logger` is already defined at line 141 — confirmed via `grep -n "^logger = " src/ccbot/bot.py`. Do not re-declare it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/ccbot/test_bot_helpers.py -v`

Expected: 5 passed.

- [ ] **Step 5: Run lint/type checks**

Run:
```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/ccbot/bot.py
```

Expected: all green. If `ruff format --check` fails, run `uv run ruff format src/ tests/` and re-run.

- [ ] **Step 6: Commit**

```bash
git add src/ccbot/bot.py tests/ccbot/test_bot_helpers.py
git commit -m "$(cat <<'EOF'
feat: add _resolve_browser_start_path helper

Pure helper that returns config.default_dir if it exists as a
directory, otherwise Path.cwd(). Will be wired into the directory
browser entry sites in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Wire the helper into both directory-browser entry sites

**Files:**
- Modify: `src/ccbot/bot.py` at line ~921 and line ~1521

These two sites both currently do `start_path = str(Path.cwd())`. They become `start_path = _resolve_browser_start_path()`.

There is no clean unit-testable boundary at these sites (they're inside async telegram handler functions with heavy context). We rely on (a) Task 2's unit tests for the helper and (b) the manual smoke test in the verification step.

- [ ] **Step 1: Verify the two call sites still match before editing**

Run: `grep -n 'start_path = str(Path.cwd())' src/ccbot/bot.py`

Expected output: exactly two lines (around 921 and 1521). If a different count is returned, stop and reconcile against the spec before continuing — the codebase has drifted.

- [ ] **Step 2: Replace both occurrences**

In `src/ccbot/bot.py`, both at line ~921 and line ~1521, change:

```python
        start_path = str(Path.cwd())
```

to:

```python
        start_path = _resolve_browser_start_path()
```

The Edit tool's `replace_all=true` is safe here because the literal `str(Path.cwd())` does not appear elsewhere in `bot.py` for this purpose (verify with the grep in Step 1).

- [ ] **Step 3: Run grep to confirm zero call sites left**

Run: `grep -n 'start_path = str(Path.cwd())' src/ccbot/bot.py`

Expected: no output (exit code 1).

Then: `grep -n '_resolve_browser_start_path()' src/ccbot/bot.py`

Expected: 3 matches — the function definition plus two call sites.

- [ ] **Step 4: Run lint/type checks and full test suite**

Run:
```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/ccbot/
uv run python -m pytest
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/ccbot/bot.py
git commit -m "$(cat <<'EOF'
feat: use pinned default dir for new-session directory browser

Replaces hardcoded Path.cwd() at the two directory-browser entry
sites with _resolve_browser_start_path(), which honors
CCBOT_DEFAULT_DIR when set.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Update README with the new env var

**Files:**
- Modify: `README.md`

The README already documents `CLAUDE_PERMISSION_MODE`. Add a one-row entry for `CCBOT_DEFAULT_DIR` next to it.

- [ ] **Step 1: Locate the env var documentation in README.md**

Run: `grep -n 'CLAUDE_PERMISSION_MODE\|CCBOT_SHOW_HIDDEN_DIRS' README.md`

Inspect the surrounding region to identify the env-var table or list. The README already has uncommitted changes on this branch (per `git status`); leave those alone and only add the new row/line in the same style as existing entries.

- [ ] **Step 2: Add the row/line**

Add an entry in the same format as `CCBOT_SHOW_HIDDEN_DIRS`:

```
CCBOT_DEFAULT_DIR  -  Pinned starting directory for the new-session directory browser. Empty/unset uses cwd. Nonexistent paths fall back to cwd with a warning.
```

(Match the surrounding markdown table or list syntax exactly.)

- [ ] **Step 3: Commit only the new line**

`git diff README.md` to confirm only the one new line/row is added (the rest of the unstaged README changes stay unstaged).

```bash
git add -p README.md   # accept only the new entry hunk; reject other hunks
git commit -m "$(cat <<'EOF'
docs: document CCBOT_DEFAULT_DIR env var

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If `git add -p` is awkward, an alternative: stash the other README changes, edit, commit, then pop.

---

## Verification (Manual — do this once after all tasks land)

The user will run this against a real bot — not part of the automated suite.

- [ ] **Step 1: Update `~/.ccbot/.env`**

Add or update:
```
CCBOT_DEFAULT_DIR=/Users/lkyao/obsidian
CLAUDE_PERMISSION_MODE=auto
```

- [ ] **Step 2: Restart the service**

Run: `./scripts/restart-macos.sh`

- [ ] **Step 3: Trigger the new-session flow**

In Telegram, create a new topic and send any message. There should be no unbound windows for the cleanest test (run `tmux list-windows -t ccbot` to confirm; kill any stale unbound ones if needed).

- [ ] **Step 4: Confirm the directory browser opens at the pinned path**

The browser message should show `Current: ~/obsidian` (or `/Users/lkyao/obsidian`).

- [ ] **Step 5: Confirm `--permission-mode auto` is passed**

After picking a directory, confirm the new tmux window's command line includes `--permission-mode auto`. Run:

```bash
tmux list-panes -t ccbot -a -F '#{window_name} #{pane_current_command} #{pane_start_command}'
```

The `pane_start_command` of the just-created window should contain `claude --permission-mode auto`.

- [ ] **Step 6: Sanity check fallback**

Set `CCBOT_DEFAULT_DIR=/totally/bogus/path`, restart, trigger a new topic. Browser should open at `cwd` (not crash), and the service log should contain a "is not a directory; falling back to cwd" warning.

After verifying, restore `CCBOT_DEFAULT_DIR=/Users/lkyao/obsidian`.

---

## Pre-merge gates

Per `CLAUDE.md`:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/ccbot/
uv run python -m pytest
```

All four must pass before declaring the work done.
