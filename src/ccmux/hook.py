"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart/SessionEnd hooks to maintain
a window↔session mapping in ~/.ccmux/session_map.json.
"""

import json
import subprocess
import sys
from pathlib import Path


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

    # Get tmux window name for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane,
    # so we use it to target the correct window (not the active one).
    import os

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

    map_file = Path.home() / ".ccmux" / "session_map.json"
    session_map: dict = {}
    if map_file.exists():
        try:
            session_map = json.loads(map_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if event != "SessionStart":
        return

    session_map[window_name] = {
        "session_id": session_id,
        "cwd": cwd,
    }

    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text(json.dumps(session_map, indent=2))
