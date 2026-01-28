"""Session monitoring service for Claude Code sessions.

Polls Claude Code session files and detects new assistant messages.
Emits both intermediate (streaming) and complete messages to enable
real-time Telegram updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

from .config import config
from .monitor_state import MonitorState, TrackedSession
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import read_cwd_from_jsonl

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    """Information about a Claude Code session."""

    session_id: str
    file_path: Path
    file_mtime: float
    project_path: str


@dataclass
class NewMessage:
    """A new assistant message detected by the monitor."""

    session_id: str
    project_path: str
    text: str
    uuid: str | None
    is_complete: bool  # True when stop_reason is set (final message)
    msg_id: str | None = None  # API message ID (same across streaming chunks)
    content_type: str = "text"  # "text" or "thinking"
    tool_use_id: str | None = None


class SessionMonitor:
    """Monitors Claude Code sessions for new assistant messages.

    Reads new JSONL lines immediately on mtime change (no stability wait),
    emitting both intermediate and complete assistant messages.
    """

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        self.projects_path = projects_path if projects_path is not None else config.claude_projects_path
        self.poll_interval = poll_interval if poll_interval is not None else config.monitor_poll_interval

        self.state = MonitorState(
            state_file=state_file or config.monitor_state_file
        )
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        # Per-session pending tool_use state carried across poll cycles
        self._pending_tools: dict[str, dict[str, str]] = {}  # session_id -> pending

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def _get_active_cwds(self) -> set[str]:
        """Get normalized cwds of all active tmux windows."""
        cwds = set()
        for w in tmux_manager.list_windows():
            try:
                cwds.add(str(Path(w.cwd).resolve()))
            except (OSError, ValueError):
                cwds.add(w.cwd)
        return cwds

    def scan_projects(self) -> list[SessionInfo]:
        """Scan projects that have active tmux windows."""
        active_cwds = self._get_active_cwds()
        if not active_cwds:
            return []

        sessions = []

        if not self.projects_path.exists():
            return sessions

        for project_dir in self.projects_path.iterdir():
            if not project_dir.is_dir():
                continue

            index_file = project_dir / "sessions-index.json"
            original_path = ""
            indexed_ids: set[str] = set()

            if index_file.exists():
                try:
                    index_data = json.loads(index_file.read_text())
                    entries = index_data.get("entries", [])
                    original_path = index_data.get("originalPath", "")

                    for entry in entries:
                        session_id = entry.get("sessionId", "")
                        full_path = entry.get("fullPath", "")
                        file_mtime = entry.get("fileMtime", 0)
                        project_path = entry.get("projectPath", original_path)

                        if not session_id or not full_path:
                            continue

                        try:
                            norm_pp = str(Path(project_path).resolve())
                        except (OSError, ValueError):
                            norm_pp = project_path
                        if norm_pp not in active_cwds:
                            continue

                        indexed_ids.add(session_id)
                        file_path = Path(full_path)
                        if file_path.exists():
                            sessions.append(SessionInfo(
                                session_id=session_id,
                                file_path=file_path,
                                file_mtime=file_mtime,
                                project_path=project_path,
                            ))

                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(f"Error reading index {index_file}: {e}")

            # Pick up un-indexed .jsonl files
            try:
                for jsonl_file in project_dir.glob("*.jsonl"):
                    session_id = jsonl_file.stem
                    if session_id in indexed_ids:
                        continue

                    # Determine project_path for this file
                    file_project_path = original_path
                    if not file_project_path:
                        file_project_path = read_cwd_from_jsonl(jsonl_file)
                    if not file_project_path:
                        dir_name = project_dir.name
                        if dir_name.startswith("-"):
                            file_project_path = dir_name.replace("-", "/")

                    try:
                        norm_fp = str(Path(file_project_path).resolve())
                    except (OSError, ValueError):
                        norm_fp = file_project_path

                    if norm_fp not in active_cwds:
                        continue

                    try:
                        file_mtime = jsonl_file.stat().st_mtime
                    except OSError:
                        continue
                    sessions.append(SessionInfo(
                        session_id=session_id,
                        file_path=jsonl_file,
                        file_mtime=file_mtime,
                        project_path=file_project_path,
                    ))
            except OSError as e:
                logger.debug(f"Error scanning jsonl files in {project_dir}: {e}")

        return sessions

    def _read_new_lines(self, session: TrackedSession, file_path: Path) -> list[dict]:
        """Read new lines from a session file.

        Detects file truncation (e.g. after /clear) and resets line count.
        """
        new_entries = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                # Detect file truncation: if we expect more lines than exist,
                # reset and re-read from the beginning
                current_total = sum(1 for _ in f)
                f.seek(0)

                if current_total < session.last_line_count:
                    logger.info(
                        "File truncated for session %s "
                        "(had %d lines, now %d). Resetting.",
                        session.session_id,
                        session.last_line_count,
                        current_total,
                    )
                    session.last_line_count = 0

                for _ in range(session.last_line_count):
                    f.readline()
                line_count = session.last_line_count
                for line in f:
                    line_count += 1
                    data = TranscriptParser.parse_line(line)
                    if data:
                        new_entries.append(data)
                session.last_line_count = line_count
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
        return new_entries

    async def check_for_updates(self) -> list[NewMessage]:
        """Check all sessions for new assistant messages.

        Reads immediately on mtime change. Emits both intermediate
        (stop_reason=null) and complete messages.
        """
        new_messages = []
        sessions = self.scan_projects()

        for session_info in sessions:
            try:
                actual_mtime = session_info.file_path.stat().st_mtime
                tracked = self.state.get_session(session_info.session_id)

                if tracked is None:
                    tracked = TrackedSession(
                        session_id=session_info.session_id,
                        file_path=str(session_info.file_path),
                        last_mtime=actual_mtime,
                        last_line_count=self._count_lines(session_info.file_path),
                        project_path=session_info.project_path,
                    )
                    self.state.update_session(tracked)
                    logger.info(f"Started tracking session: {session_info.session_id}")
                    continue

                if actual_mtime <= tracked.last_mtime:
                    continue

                # Read immediately — no stability wait
                new_entries = self._read_new_lines(tracked, session_info.file_path)

                if new_entries:
                    logger.debug(
                        f"Read {len(new_entries)} new entries for "
                        f"session {session_info.session_id}"
                    )

                # Parse new entries using the shared logic, carrying over pending tools
                carry = self._pending_tools.get(session_info.session_id, {})
                parsed_entries, remaining = TranscriptParser.parse_entries(
                    new_entries, pending_tools=carry,
                )
                if remaining:
                    self._pending_tools[session_info.session_id] = remaining
                else:
                    self._pending_tools.pop(session_info.session_id, None)

                for entry in parsed_entries:
                    if not entry.text or entry.role == "user":
                        continue
                    new_messages.append(NewMessage(
                        session_id=session_info.session_id,
                        project_path=session_info.project_path,
                        text=entry.text,
                        uuid=None,
                        is_complete=True,
                        content_type=entry.content_type,
                        tool_use_id=entry.tool_use_id,
                    ))

                tracked.last_mtime = actual_mtime
                tracked.project_path = session_info.project_path
                self.state.update_session(tracked)

            except OSError as e:
                logger.debug(f"Error processing session {session_info.session_id}: {e}")

        self.state.save_if_dirty()
        return new_messages

    def _count_lines(self, file_path: Path) -> int:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    async def _monitor_loop(self) -> None:
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        # Deferred import to avoid circular dependency (cached once)
        from .session import session_manager

        while self._running:
            try:
                # Load hook-based session map updates
                session_manager.load_session_map()

                new_messages = await self.check_for_updates()

                for msg in new_messages:
                    status = "complete" if msg.is_complete else "streaming"
                    preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
                    logger.info(
                        "[%s] session=%s: %s", status, msg.session_id, preview
                    )
                    if self._message_callback:
                        try:
                            await self._message_callback(msg)
                        except Exception as e:
                            logger.error(f"Message callback error: {e}")

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(self.poll_interval)

        logger.info("Session monitor stopped")

    def start(self) -> None:
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.state.save()
        logger.info("Session monitor stopped and state saved")
