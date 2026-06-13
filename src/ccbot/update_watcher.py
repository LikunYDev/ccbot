"""Notify (don't auto-restart) when Claude Code updates or a session breaks.

Design: a running session works fine on its old version, and a remote user must
never have in-flight work killed by surprise. So version drift earns a single
heads-up — never a restart — and the user runs /restart at a clean point. The
same turn-end hook also surfaces a session stuck on a fatal error (e.g. a
revoked model) once. Restarts, when they happen, are IN PLACE (respawn-pane,
same @window_id) so they cause no window-id / session_map churn.

Invoked from SessionMonitor at the natural turn-end moment for a topic — after
all new JSONL entries are dispatched and no tool_use is pending. Per-window
state (`WindowState.claude_launch_version`, `update_notified_version`,
`failure_notified`) dedupes notices so they never re-nag and survive a restart.

Key functions:
  - current_claude_version: throttled (5 min) subprocess probe of `claude --version`.
  - maybe_notify_update_or_failure: per-topic turn-end hook; sends one-time notices.
  - restart_topic_in_place: in-place respawn + --resume, triggered by /restart.
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


async def restart_topic_in_place(
    bot: "Bot",
    user_id: int,
    thread_id: int,
    window_id: str,
    new_version: str | None = None,
) -> bool:
    """Restart the topic's Claude session IN PLACE (respawn-pane, --resume).

    Reuses the same tmux window, so the @window_id is unchanged: no rebind, no
    orphaned session_map entry, no display-name leak — the whole class of
    window-id churn that kill+create caused on every upgrade. Only the Claude
    session_id rolls, and the resume-override re-pins the original sid so the
    monitor keeps routing to this topic. The respawn uses the user's current
    settings.json default model (no explicit --model), so /restart also clears
    a stale model pin (e.g. a now-revoked model).

    User-initiated (via /restart) or offered after a one-time update/failure
    notice. On failure to relaunch, enqueues a `⚠️` warning and returns False.
    """
    from .handlers.message_queue import enqueue_content_message
    from .session import session_manager
    from .tmux_manager import tmux_manager

    state = session_manager.get_window_state(window_id)
    cwd = state.cwd
    sid = state.session_id or None

    if not cwd:
        logger.warning(
            "restart_in_place: missing cwd for window %s; skipping", window_id
        )
        return False

    # Probe the installed version once: used both for the ack label and to
    # re-baseline this window so the next turn-end sees a match (no re-notify).
    current = await current_claude_version()
    version_label = new_version or current or "the latest version"

    logger.info(
        "in-place restart: respawning window %s (user=%d, thread=%d, sid=%s)",
        window_id,
        user_id,
        thread_id,
        sid,
    )
    ok = await tmux_manager.respawn_pane(window_id, cwd, resume_session_id=sid)
    if not ok:
        warn = (
            "⚠️ Restart failed: could not respawn the session in this window. "
            "You may need to recreate the topic."
        )
        await enqueue_content_message(
            bot=bot,
            user_id=user_id,
            window_id=window_id,
            parts=[warn],
            content_type="text",
            text=warn,
            thread_id=thread_id,
        )
        return False

    # Health check: confirm claude actually started in the pane. The hook is a
    # downstream consequence of claude starting — the pane process check is the
    # more direct signal and lets us detect failures faster.
    healthy, observed_cmd = await _wait_for_claude_in_pane(
        window_id, _RESTART_HEALTH_TIMEOUT
    )
    if not healthy:
        logger.error(
            "in-place restart: claude did not start in window %s after %.1fs "
            "(last pane_current_command=%r); user-visible failure",
            window_id,
            _RESTART_HEALTH_TIMEOUT,
            observed_cmd,
        )
        warn = (
            f"⚠️ Restart failed: claude is not running in the window "
            f"(pane shows {observed_cmd or 'nothing'})."
        )
        await enqueue_content_message(
            bot=bot,
            user_id=user_id,
            window_id=window_id,
            parts=[warn],
            content_type="text",
            text=warn,
            thread_id=thread_id,
        )
        return False

    # claude is running. For --resume, the SessionStart hook (when it fires)
    # reports a *new* session_id but messages keep writing to the original
    # JSONL — pin window_state to the resumed sid so routing is stable
    # regardless of hook timing. window_id is unchanged, so no rebind needed.
    if sid and state.session_id != sid:
        logger.info(
            "in-place restart resume override: window %s session_id %s -> %s",
            window_id,
            state.session_id,
            sid,
        )
        state.session_id = sid

    # Reset this window's baselines: it is now current, and any pending
    # update/failure notice is resolved.
    state.update_notified_version = ""
    state.failure_notified = False
    session_manager._save_state()
    if current:
        session_manager.set_claude_launch_version(window_id, current)

    ack = f"♻️ Restarted on {version_label}, resumed your session."
    await enqueue_content_message(
        bot=bot,
        user_id=user_id,
        window_id=window_id,
        parts=[ack],
        content_type="text",
        text=ack,
        thread_id=thread_id,
    )
    return True


# Pane signatures that mean the session is stuck on something only a restart
# fixes (e.g. a now-unavailable / revoked model). Lower-cased substring match.
# Extension point: add new fatal banners here.
_FAILURE_SIGNATURES: tuple[str, ...] = (
    "issue with the selected model",
    "may not exist or you may not have access",
)


async def maybe_notify_update_or_failure(
    bot: "Bot",
    user_id: int,
    thread_id: int,
    window_id: str,
) -> None:
    """At turn-end, send a ONE-TIME notice if Claude Code updated or the session
    looks broken — but never restart on its own.

    A running session works fine on its old version, and a remote user must not
    have in-flight work killed by surprise; so version drift only earns a single
    heads-up, and the user runs /restart at a clean point. The same channel also
    surfaces a session stuck on a fatal error (e.g. a revoked model) once, with
    the same /restart affordance. Both notices are deduped per-window via
    persisted markers so they never re-nag (and survive a ccbot restart).

    No-op when disabled (CCBOT_AUTO_RESTART=false) or when the version probe
    fails. Backfills a missing launch version from the pane process name.
    """
    from .handlers.message_queue import enqueue_content_message
    from .session import session_manager
    from .tmux_manager import tmux_manager

    if not config.auto_restart_enabled:
        return

    state = session_manager.get_window_state(window_id)

    # --- 1. Version drift → one notice per new version ---
    current = await current_claude_version()
    if current is not None:
        launch = state.claude_launch_version
        if not launch:
            # Backfill: recover the running claude version from the pane. claude
            # renames its own process to its version string, so a stable claude
            # pane shows e.g. "2.1.118". When the pane's foreground process is
            # a wrapper shell (window_shell form), walk the process tree since
            # the shell hides claude's version. Only when both signals miss do
            # we default to current — a silent migration that won't spuriously
            # notify.
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

        if current != launch and state.update_notified_version != current:
            notice = (
                f"ℹ️ Claude Code updated to {current} — this session is still on "
                f"{launch} (your work is unaffected). Send /restart at a clean "
                f"point to pick it up."
            )
            await enqueue_content_message(
                bot=bot,
                user_id=user_id,
                window_id=window_id,
                parts=[notice],
                content_type="text",
                text=notice,
                thread_id=thread_id,
            )
            state.update_notified_version = current
            session_manager._save_state()
            logger.info(
                "Notified update for window %s: %s -> %s (user=%d, thread=%d)",
                window_id,
                launch,
                current,
                user_id,
                thread_id,
            )

    # --- 2. Session stuck on a fatal error → one notice until it clears ---
    pane = await tmux_manager.capture_pane(window_id)
    sig = None
    if pane:
        low = pane.lower()
        sig = next((s for s in _FAILURE_SIGNATURES if s in low), None)
    if sig and not state.failure_notified:
        notice = (
            "⚠️ This session looks stuck — the pane shows a fatal error "
            "(likely the selected model is unavailable). Send /restart to "
            "relaunch it on your current default model."
        )
        await enqueue_content_message(
            bot=bot,
            user_id=user_id,
            window_id=window_id,
            parts=[notice],
            content_type="text",
            text=notice,
            thread_id=thread_id,
        )
        state.failure_notified = True
        session_manager._save_state()
        logger.info("Notified failure for window %s (sig=%r)", window_id, sig)
    elif not sig and state.failure_notified:
        state.failure_notified = False
        session_manager._save_state()


def reset_state_for_tests() -> None:
    """Reset all module-level state. Tests only."""
    global _last_check_mono, _last_version
    _last_check_mono = 0.0
    _last_version = None
