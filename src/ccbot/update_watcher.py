"""Auto-restart Claude Code in bound topics when the installed version changes.

Invoked from SessionMonitor at the natural turn-end moment for a topic — after
all new JSONL entries are dispatched and no tool_use is pending. Compares the
locally installed `claude --version` against the version recorded for THIS
window when it was launched (`WindowState.claude_launch_version`); on change,
kills and recreates the tmux window with `--resume` so the topic picks up the
new version without losing its Claude session.

Per-window comparison (vs. a single global baseline) ensures every still-old
session triggers its own restart on upgrade — independent sessions do not
silence each other.

Key functions:
  - current_claude_version: throttled (5 min) subprocess probe of `claude --version`.
  - maybe_restart_for_upgrade: per-topic hook called after a turn ends.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import config

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

_VERSION_CACHE_TTL = 300.0  # seconds
_SUBPROCESS_TIMEOUT = 5.0
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
# Backfill helper: claude renames its own process to its version string
# (e.g. "2.1.118"), so a stable pane shows the running version directly.
_VERSION_ONLY_RE = re.compile(r"^\d+\.\d+\.\d+$")

# `pane_current_command` patterns that indicate claude is actually running.
# claude renames its own process to its version string (`2.1.118` etc.), so we
# accept either the literal name, the underlying `node` runtime, or any
# version-shaped string. Also covers a handful of variant install names.
_CLAUDE_PROCESS_RE = re.compile(r"^(claude|node|\d+\.\d+\.\d+)")

# `pane_current_command` values that mean "a wrapper shell is the pane_pid and
# claude may be a child process". When tmux's `new-window <cmd>` form runs the
# window_shell via `/bin/sh -c`, the shell stays as pane_pid throughout
# claude's lifetime — so the health check falls back to a process-tree scan.
_WRAPPER_SHELL_RE = re.compile(r"^(sh|zsh|bash|fish|dash|ksh)$")

# Health-check budget: how long to poll pane_current_command after restart
# before declaring claude failed to start. Resume can take several seconds for
# large transcripts, so the upper bound matches the previous hook-wait timeout.
_RESTART_HEALTH_TIMEOUT = 15.0
_RESTART_HEALTH_INTERVAL = 0.5

_last_check_mono: float = 0.0
_last_version: str | None = None
_lock = asyncio.Lock()


def _parse_version(output: str) -> str | None:
    m = _VERSION_RE.search(output)
    return m.group(1) if m else None


# Common install locations we'll check if `config.claude_command` isn't on
# PATH. Covers the native installer (~/.local/bin), Homebrew, and manual
# /usr/local installs. Order is "most likely first" — the native installer
# is Anthropic's default per https://code.claude.com/docs/en/setup.md.
_FALLBACK_BIN_DIRS: tuple[str, ...] = (
    str(Path.home() / ".local" / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def _resolve_claude_binary() -> str | None:
    """Resolve `config.claude_command` to an invocable path.

    `shutil.which` handles both PATH lookup and absolute-path validation. If
    that fails (common when ccbot runs under a service manager with a
    stripped-down PATH that omits `~/.local/bin`), retry against a curated
    list of well-known Claude Code install locations.
    """
    cmd = config.claude_command
    resolved = shutil.which(cmd)
    if resolved:
        return resolved
    fallback_path = os.pathsep.join(_FALLBACK_BIN_DIRS)
    return shutil.which(cmd, path=fallback_path)


async def current_claude_version(force: bool = False) -> str | None:
    """Installed claude version, cached 5 min. Returns None on any failure."""
    global _last_check_mono, _last_version
    async with _lock:
        now = time.monotonic()
        if (
            not force
            and _last_version is not None
            and now - _last_check_mono < _VERSION_CACHE_TTL
        ):
            return _last_version
        binary = _resolve_claude_binary()
        if binary is None:
            logger.warning(
                "Could not locate claude binary "
                "(config.claude_command=%r, PATH=%s). "
                "Set CLAUDE_COMMAND to an absolute path or add the install "
                "directory to PATH in the service manager config.",
                config.claude_command,
                os.environ.get("PATH", ""),
            )
            return None
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [binary, "--version"],
                timeout=_SUBPROCESS_TIMEOUT,
                capture_output=True,
                text=True,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("Could not run `%s --version`: %s", binary, e)
            return None
        if result.returncode != 0:
            logger.warning(
                "`%s --version` exited %d: %s",
                binary,
                result.returncode,
                result.stderr.strip()[:200],
            )
            return None
        version = _parse_version(result.stdout)
        if not version:
            logger.warning(
                "Could not parse version from `%s --version` output: %r",
                binary,
                result.stdout[:200],
            )
            return None
        _last_check_mono = now
        _last_version = version
        return version


async def _build_process_tree() -> dict[int, list[tuple[int, str]]] | None:
    """Return a ppid → [(pid, comm), ...] map built from `ps -axo pid,ppid,ucomm`.

    `ucomm` (user-visible command) exposes the proctitle claude sets via
    node's `process.title` — on macOS that's the version string `2.1.118`,
    not the kernel `p_comm` (`claude`). On Linux, `ucomm` aliases `comm`
    and still tracks setproctitle updates via `prctl(PR_SET_NAME)`.

    Basename-normalized so `/Users/foo/.local/bin/claude` still matches
    `claude`. Returns None on any probe failure so callers distinguish
    "can't tell" from "definitely no match".
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ps", "-axo", "pid=,ppid=,ucomm="],
            timeout=_SUBPROCESS_TIMEOUT,
            capture_output=True,
            text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("ps failed during process-tree probe: %s", e)
        return None
    if result.returncode != 0:
        return None

    children: dict[int, list[tuple[int, str]]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            child_pid = int(parts[0])
            parent_pid = int(parts[1])
        except ValueError:
            continue
        comm = os.path.basename(parts[2])
        children.setdefault(parent_pid, []).append((child_pid, comm))
    return children


def _find_matching_descendant_comm(
    tree: dict[int, list[tuple[int, str]]],
    root: int,
    pattern: re.Pattern[str],
) -> str | None:
    """BFS from `root`, returning the first descendant comm matching `pattern`."""
    stack = [root]
    seen = {root}
    while stack:
        cur = stack.pop()
        for child_pid, child_comm in tree.get(cur, ()):
            if pattern.match(child_comm):
                return child_comm
            if child_pid not in seen:
                seen.add(child_pid)
                stack.append(child_pid)
    return None


async def _has_claude_descendant(pid: int) -> bool:
    """True if any descendant process of `pid` looks like claude.

    The `window_shell` wrapper (`sh -c 'PATH=... claude ...; exec zsh'`)
    leaves the shell as the pane's direct foreground process while claude
    runs as a child — so `pane_current_command` alone under-reports success.

    Returns False on any probe failure so the surrounding poll loop can
    re-check rather than declaring a false-positive success.
    """
    tree = await _build_process_tree()
    if tree is None:
        return False
    return _find_matching_descendant_comm(tree, pid, _CLAUDE_PROCESS_RE) is not None


async def _find_version_descendant(pid: int) -> str | None:
    """Return the first descendant comm that is exactly a version string.

    claude renames its own process to its version (`2.1.118`). In a
    wrapper-shell pane the shell hides this, so walk the tree to recover
    the actually-running version — used by the backfill path so upgraded
    installations still trigger a restart when the pane lags behind.
    """
    tree = await _build_process_tree()
    if tree is None:
        return None
    return _find_matching_descendant_comm(tree, pid, _VERSION_ONLY_RE)


async def _wait_for_claude_in_pane(window_id: str, timeout: float) -> tuple[bool, str]:
    """Poll the pane until its current command looks like claude, or timeout.

    Returns (healthy, last_observed_command). Two signals count as healthy:
      1. The pane's foreground process itself matches `_CLAUDE_PROCESS_RE`
         (claude was exec'd directly into the pane).
      2. The foreground process is a wrapper shell (sh/zsh/bash/...) and
         claude is one of its descendants. This is the common case because
         tmux's `new-window <cmd>` runs the command via `sh -c`, so the
         shell stays as pane_pid while claude runs as a child.
    """
    from .tmux_manager import tmux_manager

    deadline = asyncio.get_event_loop().time() + timeout
    last = ""
    while asyncio.get_event_loop().time() < deadline:
        cmd = await tmux_manager.get_pane_current_command(window_id)
        if cmd is None:
            return False, last
        last = cmd
        if _CLAUDE_PROCESS_RE.match(cmd):
            return True, cmd
        if _WRAPPER_SHELL_RE.match(cmd):
            pane_pid = await tmux_manager.get_pane_pid(window_id)
            if pane_pid is not None and await _has_claude_descendant(pane_pid):
                return True, cmd
        await asyncio.sleep(_RESTART_HEALTH_INTERVAL)
    return False, last


async def _restart_topic(
    bot: "Bot",
    user_id: int,
    thread_id: int,
    old_wid: str,
    new_version: str,
) -> bool:
    """Kill the old tmux window, recreate with --resume, rebind the topic.

    On success: enqueues `♻️ … restarted` ack and returns True (caller saves
    the new version baseline). On failure (claude doesn't actually start in
    the pane within the health-check budget): enqueues a `⚠️` warning and
    returns False, so the caller leaves the baseline alone and we'll retry on
    the next turn-end.
    """
    from .handlers.message_queue import enqueue_content_message
    from .session import session_manager
    from .tmux_manager import tmux_manager

    state = session_manager.get_window_state(old_wid)
    cwd = state.cwd
    sid = state.session_id or None
    old_wname = state.window_name or session_manager.get_display_name(old_wid)

    if not cwd:
        logger.warning("auto-restart: missing cwd for window %s; skipping", old_wid)
        return False

    logger.info(
        "auto-restart: killing window %s (user=%d, thread=%d) for upgrade to %s (sid=%s)",
        old_wid,
        user_id,
        thread_id,
        new_version,
        sid,
    )
    await tmux_manager.kill_window(old_wid)

    ok, msg, new_wname, new_wid = await tmux_manager.create_window(
        cwd,
        window_name=old_wname or None,
        resume_session_id=sid,
    )
    if not ok:
        logger.error("auto-restart: create_window failed: %s", msg)
        return False

    session_manager.bind_thread(user_id, thread_id, new_wid, new_wname)

    # Health check: confirm claude actually started in the pane. We don't
    # block on the SessionStart hook here because (a) for resume sessions we
    # already know the sid and override window_state below, and (b) the hook
    # is a downstream consequence of claude starting — the pane process check
    # is the more direct signal and lets us detect failures faster.
    healthy, observed_cmd = await _wait_for_claude_in_pane(
        new_wid, _RESTART_HEALTH_TIMEOUT
    )
    if not healthy:
        logger.error(
            "auto-restart: claude did not start in window %s after %.1fs "
            "(last pane_current_command=%r); user-visible failure",
            new_wid,
            _RESTART_HEALTH_TIMEOUT,
            observed_cmd,
        )
        warn = (
            f"⚠️ Auto-restart to {new_version} failed: claude is not running "
            f"in the window (pane shows {observed_cmd or 'nothing'}). "
            f"Recreate the topic to retry."
        )
        await enqueue_content_message(
            bot=bot,
            user_id=user_id,
            window_id=new_wid,
            parts=[warn],
            content_type="text",
            text=warn,
            thread_id=thread_id,
        )
        return False

    # claude is running. For --resume, the SessionStart hook (when it fires)
    # will report a *new* session_id but messages continue writing to the
    # original JSONL — manually pin window_state to the resumed sid so the
    # monitor routes messages back to this topic regardless of hook timing.
    if sid:
        ws = session_manager.get_window_state(new_wid)
        if ws.session_id != sid:
            logger.info(
                "auto-restart resume override: window %s session_id %s -> %s",
                new_wid,
                ws.session_id,
                sid,
            )
        ws.session_id = sid
        ws.cwd = cwd
        ws.window_name = new_wname
        session_manager._save_state()

    # Pin the new version onto the new window so the next turn-end sees a
    # match instead of looping the restart. Each window owns its own version
    # — no global baseline to share.
    session_manager.set_claude_launch_version(new_wid, new_version)

    ack = f"♻️ Claude Code upgraded to {new_version}. Session restarted."
    await enqueue_content_message(
        bot=bot,
        user_id=user_id,
        window_id=new_wid,
        parts=[ack],
        content_type="text",
        text=ack,
        thread_id=thread_id,
    )
    return True


async def maybe_restart_for_upgrade(
    bot: "Bot",
    user_id: int,
    thread_id: int,
    window_id: str,
) -> None:
    """Restart the topic's Claude session if the installed version changed.

    Compares the live installed version against the version recorded for this
    specific window when it launched. Per-window comparison ensures every
    still-old session triggers its own upgrade — they don't silence each other.

    No-op when auto-restart is disabled or when the version probe fails. For
    windows with no recorded launch version (pre-feature, or capture failed
    at creation), backfills via the pane's process name (claude renames itself
    to its version string) and falls back to current on ambiguity.
    """
    from .session import session_manager
    from .tmux_manager import tmux_manager

    if not config.auto_restart_enabled:
        return
    current = await current_claude_version()
    if current is None:
        return

    state = session_manager.get_window_state(window_id)
    launch = state.claude_launch_version
    if not launch:
        # Backfill: recover the running claude version from the pane. claude
        # renames its own process to its version string, so a stable claude
        # pane shows e.g. "2.1.118". When the pane's foreground process is
        # a wrapper shell (window_shell form), walk the process tree since
        # the shell hides claude's version. Only when both signals miss do
        # we default to current — a silent migration that won't spuriously
        # restart.
        pane_cmd = await tmux_manager.get_pane_current_command(window_id)
        observed: str | None = None
        if pane_cmd and _VERSION_ONLY_RE.match(pane_cmd):
            observed = pane_cmd
        elif pane_cmd and _WRAPPER_SHELL_RE.match(pane_cmd):
            pane_pid = await tmux_manager.get_pane_pid(window_id)
            if pane_pid is not None:
                observed = await _find_version_descendant(pane_pid)
        if observed:
            launch = observed
            logger.info(
                "Backfilled claude_launch_version for window %s from pane: %s",
                window_id,
                launch,
            )
        else:
            launch = current
            logger.info(
                "Backfilled claude_launch_version for window %s to current %s "
                "(pane=%r, no version-string match)",
                window_id,
                launch,
                pane_cmd,
            )
        session_manager.set_claude_launch_version(window_id, launch)

    if current == launch:
        return
    logger.info(
        "Claude version changed for window %s: %s -> %s (user=%d, thread=%d)",
        window_id,
        launch,
        current,
        user_id,
        thread_id,
    )
    await _restart_topic(bot, user_id, thread_id, window_id, current)
    # _restart_topic owns updating the new window's launch_version on success;
    # on failure, the old window's state is unchanged so we'll retry next turn.


def reset_state_for_tests() -> None:
    """Reset all module-level state. Tests only."""
    global _last_check_mono, _last_version
    _last_check_mono = 0.0
    _last_version = None
