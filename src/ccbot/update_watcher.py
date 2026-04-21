"""Auto-restart Claude Code in bound topics when the installed version changes.

Invoked from SessionMonitor at the natural turn-end moment for a topic — after
all new JSONL entries are dispatched and no tool_use is pending. Compares the
locally installed `claude --version` against a persisted baseline; on change,
kills and recreates the tmux window with `--resume` so the topic picks up the
new version without losing its Claude session.

Key functions:
  - current_claude_version: throttled (5 min) subprocess probe of `claude --version`.
  - maybe_restart_for_upgrade: per-topic hook called after a turn ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import config
from .utils import atomic_write_json

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

_VERSION_CACHE_TTL = 300.0  # seconds
_SUBPROCESS_TIMEOUT = 5.0
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")

_last_check_mono: float = 0.0
_last_version: str | None = None
_cached_baseline: str | None = None
_baseline_loaded: bool = False
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


def _load_baseline() -> str | None:
    """Read the persisted baseline from disk, cached in memory after first read."""
    global _cached_baseline, _baseline_loaded
    if _baseline_loaded:
        return _cached_baseline
    _baseline_loaded = True
    path = config.claude_version_file
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("claude_version.json unreadable (%s); treating as absent", e)
        return None
    baseline = data.get("installed")
    if isinstance(baseline, str) and baseline:
        _cached_baseline = baseline
    return _cached_baseline


def _save_baseline(version: str) -> None:
    global _cached_baseline, _baseline_loaded
    atomic_write_json(config.claude_version_file, {"installed": version})
    _cached_baseline = version
    _baseline_loaded = True


async def _restart_topic(
    bot: "Bot",
    user_id: int,
    thread_id: int,
    old_wid: str,
    new_version: str,
) -> bool:
    """Kill the old tmux window, recreate with --resume, rebind the topic."""
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

    hook_timeout = 15.0 if sid else 5.0
    hook_ok = await session_manager.wait_for_session_map_entry(
        new_wid, timeout=hook_timeout
    )

    if sid:
        ws = session_manager.get_window_state(new_wid)
        if not hook_ok:
            logger.warning(
                "auto-restart: hook timed out for window %s; "
                "manually setting session_id=%s cwd=%s",
                new_wid,
                sid,
                cwd,
            )
            ws.session_id = sid
            ws.cwd = cwd
            ws.window_name = new_wname
            session_manager._save_state()
        elif ws.session_id != sid:
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

    No-op when auto-restart is disabled, when the version probe fails, or when
    the installed version matches the persisted baseline. On first-ever call
    (no baseline on disk), captures the baseline without restarting.
    """
    if not config.auto_restart_enabled:
        return
    baseline = _load_baseline()
    current = await current_claude_version()
    if current is None:
        return
    if baseline is None:
        _save_baseline(current)
        logger.info("captured initial Claude version baseline: %s", current)
        return
    if current == baseline:
        return
    logger.info(
        "Claude version changed: %s -> %s (user=%d, thread=%d, wid=%s)",
        baseline,
        current,
        user_id,
        thread_id,
        window_id,
    )
    if await _restart_topic(bot, user_id, thread_id, window_id, current):
        _save_baseline(current)


def reset_state_for_tests() -> None:
    """Reset all module-level state. Tests only."""
    global _last_check_mono, _last_version, _cached_baseline, _baseline_loaded
    _last_check_mono = 0.0
    _last_version = None
    _cached_baseline = None
    _baseline_loaded = False
