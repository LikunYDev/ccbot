"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart/SessionEnd hooks to maintain
a window↔session mapping in ~/.ccmux/session_map.json.

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Validate session_id looks like a UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_SESSION_MAP_FILE = Path.home() / ".ccmux" / "session_map.json"


def hook_main() -> None:
    """Process a Claude Code hook event from stdin."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        return

    # Validate session_id format
    if not _UUID_RE.match(session_id):
        return

    # Validate cwd is an absolute path (if provided)
    if cwd and not os.path.isabs(cwd):
        return

    if event != "SessionStart":
        return

    # Get tmux window name for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane.
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        return

    result = subprocess.run(
        ["tmux", "display-message", "-t", pane_id, "-p", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    window_name = result.stdout.strip()
    if not window_name:
        return

    # Read-modify-write with file locking to prevent concurrent hook races
    map_file = _SESSION_MAP_FILE
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass

                session_map[window_name] = {
                    "session_id": session_id,
                    "cwd": cwd,
                }

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError:
        pass
