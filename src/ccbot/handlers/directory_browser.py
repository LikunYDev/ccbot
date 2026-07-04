"""Directory browser and window picker UI for session creation.

Provides UIs in Telegram for:
  - Window picker: list unbound tmux windows for quick binding
  - Directory browser: navigate directory hierarchies to create new sessions
  - Session picker: resume an existing Claude session found in a directory

These three share one mutually-exclusive state machine (only one is active
at a time per topic). That state is keyed by Telegram thread_id, not
user_id: browsing/picking in one topic can never clobber another topic's
in-progress flow or silently discard its pending first message (see
RC13 / f31 / f33 — the old per-user keys let a second topic's browse
overwrite the first topic's state and drop its queued message).

Key components:
  - DIRS_PER_PAGE: Number of directories shown per page
  - Field-name constants (STATE_KEY, BROWSE_PATH_KEY, ...) for the keys
    inside each thread's per-thread state dict
  - get_browse_state / set_browse_state / clear_browse_state: the only
    sanctioned way to read/write/drop a thread's state — callers (bot.py)
    should never touch the underlying ``browse_by_thread`` dict directly
  - build_window_picker: Build unbound window picker UI
  - build_directory_browser: Build directory browser UI
  - build_session_picker: Build session picker UI
"""

import os
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..session import ClaudeSession

from ..config import config
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)

# Directories per page in directory browser
DIRS_PER_PAGE = 6

# user_data key holding the per-thread state dict: dict[thread_id, dict]
BROWSE_BY_THREAD_KEY = "browse_by_thread"

# Field names inside each thread's entry in browse_by_thread (see
# get_browse_state / set_browse_state / clear_browse_state below).
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_WINDOW = "selecting_window"
STATE_SELECTING_SESSION = "selecting_session"
BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path
UNBOUND_WINDOWS_KEY = "unbound_windows"  # Cache of window_ids for window picker
SESSIONS_KEY = "cached_sessions"  # Cache of ClaudeSession list
SELECTED_PATH_KEY = "selected_path"  # Directory chosen before the session picker
PENDING_TEXT_KEY = "pending_text"  # First message text queued for this topic


def get_browse_state(user_data: dict | None, thread_id: int | None) -> dict:
    """Return thread_id's browse/picker state dict (``{}`` if none is set).

    Every field for a topic's in-progress directory-browser/window-picker/
    session-picker flow — state, path, page, cached dirs/windows/sessions,
    and any pending first message — lives in one dict keyed by thread_id,
    so a flow in one topic can never see or clobber another topic's flow.
    """
    if user_data is None:
        return {}
    return user_data.get(BROWSE_BY_THREAD_KEY, {}).get(thread_id, {})


def set_browse_state(
    user_data: dict | None, thread_id: int | None, fields: dict
) -> None:
    """Merge ``fields`` into thread_id's browse/picker state dict.

    Only thread_id's entry is created/updated; other threads' entries (and
    any fields not named in ``fields``, e.g. a pending message queued by an
    earlier step) are left untouched.
    """
    if user_data is None:
        return
    by_thread = user_data.setdefault(BROWSE_BY_THREAD_KEY, {})
    by_thread.setdefault(thread_id, {}).update(fields)


def clear_browse_state(user_data: dict | None, thread_id: int | None) -> None:
    """Drop thread_id's entire browse/picker state, all fields at once.

    Other threads' entries are left untouched.
    """
    if user_data is None:
        return
    by_thread = user_data.get(BROWSE_BY_THREAD_KEY)
    if by_thread is not None:
        by_thread.pop(thread_id, None)


def build_window_picker(
    windows: list[tuple[str, str, str]],
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build window picker UI for unbound tmux windows.

    Args:
        windows: List of (window_id, window_name, cwd) tuples.

    Returns: (text, keyboard, window_ids) where window_ids is the ordered list for caching.
    """
    window_ids = [wid for wid, _, _ in windows]

    lines = [
        "*Bind to Existing Window*\n",
        "These windows are running but not bound to any topic.",
        "Pick one to attach it here, or start a new session.\n",
    ]
    for _wid, name, cwd in windows:
        display_cwd = cwd.replace(str(Path.home()), "~")
        lines.append(f"• `{name}` — {display_cwd}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(windows), 2):
        row = []
        for j in range(min(2, len(windows) - i)):
            name = windows[i + j][1]
            display = name[:12] + "…" if len(name) > 13 else name
            row.append(
                InlineKeyboardButton(
                    f"🖥 {display}", callback_data=f"{CB_WIN_BIND}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("➕ New Session", callback_data=CB_WIN_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_WIN_CANCEL),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons), window_ids


def build_directory_browser(
    current_path: str, page: int = 0
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    path = Path(current_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path.cwd()

    try:
        subdirs = sorted(
            [
                d.name
                for d in path.iterdir()
                if d.is_dir()
                and (config.show_hidden_dirs or not d.name.startswith("."))
            ]
        )
    except (PermissionError, OSError):
        subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    # Allow going up unless at filesystem root
    if path != path.parent:
        action_row.append(InlineKeyboardButton("..", callback_data=CB_DIR_UP))
    action_row.append(InlineKeyboardButton("Select", callback_data=CB_DIR_CONFIRM))
    action_row.append(InlineKeyboardButton("Cancel", callback_data=CB_DIR_CANCEL))
    buttons.append(action_row)

    display_path = str(path).replace(str(Path.home()), "~")
    if not subdirs:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\n_(No subdirectories)_"
    else:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\nTap a folder to enter, or select current directory"

    return text, InlineKeyboardMarkup(buttons), subdirs


def _relative_time(file_path: str) -> str:
    """Format file mtime as a human-readable relative time string."""
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return ""
    delta = int(time.time() - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def build_session_picker(
    sessions: list[ClaudeSession],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build session picker UI for resuming an existing Claude session.

    Args:
        sessions: List of ClaudeSession objects (sorted by recency).

    Returns: (text, keyboard).
    """
    lines = [
        "*Resume Session?*\n",
        "Existing sessions found in this directory.\n",
    ]
    for i, s in enumerate(sessions):
        display = s.name or s.summary
        display = display[:40] + "…" if len(display) > 40 else display
        rel = _relative_time(s.file_path)
        time_str = f" ({rel})" if rel else ""
        lines.append(f"{i + 1}. {display} — {s.message_count} msgs{time_str}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(sessions), 2):
        row = []
        for j in range(min(2, len(sessions) - i)):
            s = sessions[i + j]
            label = s.name or s.summary
            label = label[:14] + "…" if len(label) > 14 else label
            row.append(
                InlineKeyboardButton(
                    f"▶ {label}", callback_data=f"{CB_SESSION_SELECT}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("➕ New Session", callback_data=CB_SESSION_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_SESSION_CANCEL),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)
