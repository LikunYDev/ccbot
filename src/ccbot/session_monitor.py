"""Session monitoring service — watches JSONL files for new messages.

Runs an async polling loop that:
  1. Reads session_manager.window_states (the reconciled window->session
     authority: hook events with manual pins applied) to know which
     sessions to watch.
  2. Detects window->session changes (new/changed/deleted windows) and cleans up.
  3. Reads new JSONL lines from each session file using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NewMessage objects to a callback.

Delivery contract: read -> dispatch -> observe success -> THEN commit. A
session's byte offset is only persisted once every message in its batch has
been handed to the message callback without raising — never at read time.
While a batch is awaiting that outcome the session is held in `_inflight`,
which also backpressures `check_for_updates` (no further reads for that
session until the batch settles). A callback exception or crash in that
window no longer loses the batch: the next poll cycle re-reads and
re-dispatches it from the same offset. This is an at-least-once contract
(a mid-batch failure can redeliver messages sent before the failure) —
duplicates are preferred over silent loss. Bounded retries (3) stop a
poison batch from wedging a session's reads forever; the offset is then
committed anyway and the drop is logged.

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Key classes: SessionMonitor, NewMessage, SessionInfo.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiofiles

from .config import config
from .handlers.interactive_ui import INTERACTIVE_TOOL_NAMES
from .monitor_state import MonitorState, TrackedSession
from .tmux_manager import tmux_manager
from .transcript_parser import ParsedEntry, TranscriptParser
from .utils import read_cwd_from_jsonl, supervise_loop

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    """Information about a Claude Code session."""

    session_id: str
    file_path: Path


@dataclass
class NewMessage:
    """A new message detected by the monitor."""

    session_id: str
    text: str
    is_complete: bool  # True when stop_reason is set (final message)
    content_type: str = "text"  # "text" or "thinking"
    tool_use_id: str | None = None
    role: str = "assistant"  # "user" or "assistant"
    tool_name: str | None = None  # For tool_use messages, the tool name
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images


@dataclass
class _PendingCommit:
    """Offset/carry state needed to commit a session's read once its batch
    of NewMessages has been durably delivered.

    Held in-memory only, between `check_for_updates` returning an
    undelivered batch and `_dispatch_and_commit` settling it — never
    persisted.
    """

    offset_after: int  # Byte offset to persist once delivery succeeds
    # offset_before: already restored in-memory by check_for_updates; kept
    # here for reference/debugging.
    offset_before: int
    carry_before: dict[str, Any]  # _pending_tools[session_id] snapshot pre-parse


class SessionMonitor:
    """Monitors Claude Code sessions for new assistant messages.

    Uses simple async polling with aiofiles for non-blocking I/O.
    Emits both intermediate and complete assistant messages.
    """

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        self.projects_path = (
            projects_path if projects_path is not None else config.claude_projects_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        # Fires once per session after a dispatch batch completes with no
        # pending tool_use — i.e. the turn has ended and the topic is idle.
        self._turn_end_callback: Callable[[str], Awaitable[None]] | None = None
        # Fire-and-forget callback tasks (one per session group per poll cycle)
        self._callback_tasks: set[asyncio.Task[None]] = set()
        # Per-session pending tool_use state carried across poll cycles
        self._pending_tools: dict[str, dict[str, Any]] = {}  # session_id -> pending
        # Track last known window_id -> session_id map (from session_manager
        # .window_states) for detecting changes
        self._last_session_map: dict[str, str] = {}  # window_id -> session_id
        # In-memory mtime cache for quick file change detection (not persisted)
        self._file_mtimes: dict[str, float] = {}  # session_id -> last_seen_mtime
        # Sessions whose last-read batch is still awaiting delivery ACK.
        # check_for_updates skips these (backpressure), so a session never
        # has two dispatch batches racing each other.
        self._inflight: set[str] = set()
        # Consecutive delivery-failure count per session, for the bounded
        # (3-attempt) poison-batch escape hatch. Reset on any success.
        self._delivery_failures: dict[str, int] = {}
        # scan_projects() fallback-glob miss cache for _recover_missing_sessions:
        # session_id -> monotonic time before which it should NOT be re-globbed.
        # Bounds the cost of a truly-gone session_id to one glob per 60s
        # instead of one per ~2s poll cycle.
        self._glob_miss_until: dict[str, float] = {}

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def set_turn_end_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a callback fired when a session's turn ends with no pending tool_use."""
        self._turn_end_callback = callback

    async def _get_active_cwds(self) -> set[str]:
        """Get normalized cwds of all active tmux windows."""
        cwds = set()
        windows = await tmux_manager.list_windows()
        for w in windows:
            try:
                cwds.add(str(Path(w.cwd).resolve()))
            except (OSError, ValueError):
                cwds.add(w.cwd)
        return cwds

    async def scan_projects(self) -> list[SessionInfo]:
        """Scan projects that have active tmux windows."""
        active_cwds = await self._get_active_cwds()
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
                    async with aiofiles.open(index_file, "r") as f:
                        content = await f.read()
                    index_data = json.loads(content)
                    entries = index_data.get("entries", [])
                    original_path = index_data.get("originalPath", "")

                    for entry in entries:
                        session_id = entry.get("sessionId", "")
                        full_path = entry.get("fullPath", "")
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
                            sessions.append(
                                SessionInfo(
                                    session_id=session_id,
                                    file_path=file_path,
                                )
                            )

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
                        file_project_path = await asyncio.to_thread(
                            read_cwd_from_jsonl, jsonl_file
                        )
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

                    sessions.append(
                        SessionInfo(
                            session_id=session_id,
                            file_path=jsonl_file,
                        )
                    )
            except OSError as e:
                logger.debug(f"Error scanning jsonl files in {project_dir}: {e}")

        return sessions

    def _recover_missing_sessions(
        self, sessions: list[SessionInfo], active_session_ids: set[str]
    ) -> None:
        """Recover active sessions that scan_projects() silently dropped.

        scan_projects() gates every candidate session on its recorded
        project path resolving to a CURRENTLY active tmux window cwd. If
        the project directory is renamed/moved after the session started
        (or the sessions-index's recorded path just drifts), that gate
        drops the session from every future scan — forever, with no log
        (review f59/RC27). Recover it two ways, appending recovered
        sessions directly into `sessions` (mutated in place) so Phase 1
        reads them normally:

          (a) Already tracked (in monitor_state) with a file that still
              exists on disk: the file location is already known, so the
              cwd gate is irrelevant — synthesize a SessionInfo straight
              from the tracked record.
          (b) Not tracked, or its file vanished: fall back to a one-level
              glob under the projects root for `*/<session_id>.jsonl`. A
              hit means the transcript file still exists somewhere, just
              not reachable through the normal (indexed or un-indexed)
              scan; log a warning since this silent-drop failure mode is
              otherwise invisible. A miss is cached for 60s (monotonic) so
              a truly-gone session_id is not re-globbed every ~2s poll.
        """
        missing_ids = active_session_ids - {s.session_id for s in sessions}
        if not missing_ids:
            return

        now = time.monotonic()
        for session_id in missing_ids:
            tracked = self.state.get_session(session_id)
            if tracked is not None and Path(tracked.file_path).exists():
                sessions.append(
                    SessionInfo(
                        session_id=session_id, file_path=Path(tracked.file_path)
                    )
                )
                continue

            retry_at = self._glob_miss_until.get(session_id)
            if retry_at is not None and now < retry_at:
                continue

            match = next(self.projects_path.glob(f"*/{session_id}.jsonl"), None)
            if match is not None:
                logger.warning(
                    "session %s found via fallback glob; project dir no "
                    "longer matches its window cwd",
                    session_id,
                )
                sessions.append(SessionInfo(session_id=session_id, file_path=match))
                self._glob_miss_until.pop(session_id, None)
            else:
                self._glob_miss_until[session_id] = now + 60.0

    async def _read_new_lines(
        self, session: TrackedSession, file_path: Path
    ) -> list[dict]:
        """Read new lines from a session file using byte offset for efficiency.

        Detects file truncation (e.g. after /clear) and resets offset.
        Recovers from corrupted offsets (mid-line) by scanning to next line.

        A line that fails to parse is either corrupt (it ends with a newline,
        so it's a complete-but-malformed record) or partial (no trailing
        newline — the file tail, likely mid-write). Corrupt lines are logged
        and skipped with the offset advanced past them, so one bad line can
        never wedge reads forever; partial lines keep the existing
        break-and-retry-next-cycle behavior.
        """
        new_entries = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                # Get file size to detect truncation
                await f.seek(0, 2)  # Seek to end
                file_size = await f.tell()

                # Detect file truncation: if offset is beyond file size, reset
                if session.last_byte_offset > file_size:
                    logger.info(
                        "File truncated for session %s "
                        "(offset %d > size %d). Resetting.",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = 0

                # Seek to last read position for incremental reading
                await f.seek(session.last_byte_offset)

                # Detect corrupted offset: if we're mid-line (not at '{'),
                # scan forward to the next line start. This can happen if
                # the state file was manually edited or corrupted.
                if session.last_byte_offset > 0:
                    first_char = await f.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in session %s (mid-line), "
                            "scanning to next line",
                            session.last_byte_offset,
                            session.session_id,
                        )
                        await f.readline()  # Skip rest of partial line
                        session.last_byte_offset = await f.tell()
                        return []
                    await f.seek(session.last_byte_offset)  # Reset for normal read

                # Read only new lines from the offset.
                # Track safe_offset: only advance past lines that parsed
                # successfully. A non-empty line that fails JSON parsing is
                # likely a partial write; stop and retry next cycle.
                safe_offset = session.last_byte_offset
                async for line in f:
                    data = TranscriptParser.parse_line(line)
                    if data is not None:
                        new_entries.append(data)
                        safe_offset = await f.tell()
                    elif not line.strip():
                        # Empty line — safe to skip
                        safe_offset = await f.tell()
                    elif line.endswith("\n"):
                        # Complete line that failed to parse — permanently
                        # corrupt, not a race with an in-progress write.
                        # Skip and advance past it so it can't wedge reads.
                        logger.warning(
                            "Skipping corrupt JSONL line in session %s",
                            session.session_id,
                        )
                        safe_offset = await f.tell()
                    else:
                        # No trailing newline — likely the file tail
                        # mid-write. Don't advance offset; retry next cycle.
                        logger.debug(
                            "Partial JSONL line in session %s, will retry next cycle",
                            session.session_id,
                        )
                        break

                session.last_byte_offset = safe_offset

        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
        return new_entries

    def _entries_to_messages(
        self, session_id: str, parsed_entries: list[ParsedEntry]
    ) -> list[NewMessage]:
        """Convert parsed transcript entries into deliverable NewMessages.

        Applies the same show_user_messages/show_thinking/show_tools
        filtering used for every live poll batch. Shared by the normal
        `check_for_updates` path and the final-drain path in
        `_detect_and_cleanup_changes` so both apply identical rules.
        """
        session_messages: list[NewMessage] = []
        for entry in parsed_entries:
            if not entry.text and not entry.image_data:
                continue
            # Skip user messages unless show_user_messages is enabled
            if entry.role == "user" and not config.show_user_messages:
                continue
            # Skip thinking messages unless show_thinking is enabled
            if entry.content_type == "thinking" and not config.show_thinking:
                continue
            # Skip tool messages unless show_tools is enabled
            # Exception: interactive tools (AskUserQuestion, ExitPlanMode) must pass through
            if (
                entry.content_type in ("tool_use", "tool_result")
                and not config.show_tools
            ):
                if not (
                    entry.content_type == "tool_use"
                    and entry.tool_name in INTERACTIVE_TOOL_NAMES
                ):
                    continue
            session_messages.append(
                NewMessage(
                    session_id=session_id,
                    text=entry.text,
                    is_complete=True,
                    content_type=entry.content_type,
                    tool_use_id=entry.tool_use_id,
                    role=entry.role,
                    tool_name=entry.tool_name,
                    image_data=entry.image_data,
                )
            )
        return session_messages

    async def check_for_updates(
        self, active_session_ids: set[str]
    ) -> tuple[list[NewMessage], dict[str, _PendingCommit]]:
        """Check all sessions for new assistant messages.

        Before collecting, `_recover_missing_sessions` adds back any active
        session_id that `scan_projects()` failed to surface (its project
        dir no longer matches a live tmux cwd) so it keeps being read
        instead of silently going dark.

        Uses a collect → read → parse pipeline:
          1. Collect: identify sessions that need reading (mtime/size changed).
             A session with a dispatch still in flight (`_inflight`) is
             skipped — backpressure that also guarantees a session never has
             two dispatch batches racing each other.
          2. Read: parallel async file reads via asyncio.gather
          3. Parse: sequential per-session parsing (safe — _pending_tools keyed by session)

        A session whose batch contains no NewMessages (filtered content,
        bookkeeping-only entries) has its offset committed immediately, as
        before. A session that DID produce messages is not committed here:
        the in-memory offset is reverted to its pre-read value and the
        advanced offset is handed back (per session, via the returned dict)
        so the caller can commit it only once the batch is durably
        delivered — see the module docstring's delivery contract. Because a
        failure partway through a batch causes the whole batch to be
        re-dispatched next cycle, messages already delivered before the
        failure can be redelivered: at-least-once is the chosen contract,
        since silent loss is worse than a duplicate.

        Args:
            active_session_ids: Set of session IDs currently in session_map

        Returns:
            Tuple of (new messages, pending-commit info keyed by session_id
            for every session whose batch is awaiting a delivery ACK).
        """
        new_messages: list[NewMessage] = []
        pending_commits: dict[str, _PendingCommit] = {}

        # Scan projects to get available session files
        sessions = await self.scan_projects()

        # Recover any active session_id the scan above silently dropped
        # (e.g. its project dir was renamed/moved) — see
        # _recover_missing_sessions for why this can't just be logged once
        # and ignored.
        self._recover_missing_sessions(sessions, active_session_ids)

        # Phase 1: Collect — identify sessions needing reads
        to_read: list[tuple[SessionInfo, TrackedSession, float, int]] = []

        for session_info in sessions:
            if session_info.session_id not in active_session_ids:
                continue
            if session_info.session_id in self._inflight:
                # A previous batch for this session hasn't been ACKed yet;
                # never read further ahead of an undelivered batch.
                continue
            try:
                tracked = self.state.get_session(session_info.session_id)

                if tracked is None:
                    # For a newly-noticed session, default the offset to end
                    # of file (avoids re-processing old messages). But if the
                    # SessionStart hook recorded the transcript's size at
                    # session start (WindowState.session_start_size), seed
                    # there instead: a reply that landed within this poll
                    # cycle's window — or before the monitor ever noticed the
                    # session, e.g. after losing monitor_state — would
                    # otherwise fall inside [session_start_size, file_size)
                    # and be silently skipped (review f17/RC38). This is a
                    # deliberate at-least-once tradeoff: it replays from
                    # session start when monitor state was lost. For a
                    # resumed session the transcript already contains its
                    # full history at SessionStart, so start_size ≈ current
                    # size and nothing replays.
                    try:
                        file_size = session_info.file_path.stat().st_size
                        current_mtime = session_info.file_path.stat().st_mtime
                    except OSError:
                        file_size = 0
                        current_mtime = 0.0

                    from .session import session_manager

                    seed_offset = file_size
                    for ws in session_manager.window_states.values():
                        if (
                            ws.session_id == session_info.session_id
                            and ws.session_start_size >= 0
                        ):
                            seed_offset = min(ws.session_start_size, file_size)
                            break

                    tracked = TrackedSession(
                        session_id=session_info.session_id,
                        file_path=str(session_info.file_path),
                        last_byte_offset=seed_offset,
                    )
                    self.state.update_session(tracked)
                    self._file_mtimes[session_info.session_id] = current_mtime
                    logger.info(f"Started tracking session: {session_info.session_id}")
                    continue

                # Check mtime + file size to see if file has changed
                try:
                    st = session_info.file_path.stat()
                    current_mtime = st.st_mtime
                    current_size = st.st_size
                except OSError:
                    continue

                last_mtime = self._file_mtimes.get(session_info.session_id, 0.0)
                if (
                    current_mtime <= last_mtime
                    and current_size <= tracked.last_byte_offset
                ):
                    continue

                to_read.append(
                    (session_info, tracked, current_mtime, tracked.last_byte_offset)
                )

            except OSError as e:
                logger.debug(f"Error collecting session {session_info.session_id}: {e}")

        if not to_read:
            self.state.save_if_dirty()
            return new_messages, pending_commits

        # Phase 2: Read — parallel file reads
        read_results: list[list[dict[str, Any]] | BaseException] = await asyncio.gather(
            *(
                self._read_new_lines(tracked, si.file_path)
                for si, tracked, _mtime, _offset_before in to_read
            ),
            return_exceptions=True,
        )

        # Phase 3: Parse — sequential per session
        for (session_info, tracked, current_mtime, offset_before), result in zip(
            to_read, read_results, strict=True
        ):
            if isinstance(result, BaseException):
                logger.warning(
                    "Error reading session %s: %s", session_info.session_id, result
                )
                continue

            new_entries: list[dict[str, Any]] = result
            self._file_mtimes[session_info.session_id] = current_mtime

            if new_entries:
                logger.debug(
                    "Read %d new entries for session %s",
                    len(new_entries),
                    session_info.session_id,
                )

            # Parse new entries using the shared logic, carrying over pending tools
            carry = self._pending_tools.get(session_info.session_id, {})
            carry_before = dict(carry)
            parsed_entries, remaining = TranscriptParser.parse_entries(
                new_entries,
                pending_tools=carry,
            )
            if remaining:
                self._pending_tools[session_info.session_id] = remaining
            else:
                self._pending_tools.pop(session_info.session_id, None)

            session_messages = self._entries_to_messages(
                session_info.session_id, parsed_entries
            )

            if session_messages:
                # Undelivered batch: hold the offset commit until the batch
                # is dispatched and ACKed. Revert the in-memory offset now
                # so a crash or dropped process before that ACK re-reads
                # (and re-emits) these same messages next cycle instead of
                # losing them.
                offset_after = tracked.last_byte_offset
                tracked.last_byte_offset = offset_before
                pending_commits[session_info.session_id] = _PendingCommit(
                    offset_after=offset_after,
                    offset_before=offset_before,
                    carry_before=carry_before,
                )
                new_messages.extend(session_messages)
            else:
                # Nothing to deliver — safe to commit the read now.
                self.state.update_session(tracked)

        self.state.save_if_dirty()
        return new_messages, pending_commits

    async def _load_current_session_map(self) -> dict[str, str]:
        """Return window_id -> session_id from the reconciled authority.

        session_manager.window_states is the reconciled authority: it is
        built from hook events (session_map.json) with manual pins
        (WindowState.pinned_over) applied on top, via
        session_manager.load_session_map(). The monitor no longer parses
        session_map.json itself, so a resumed session's pinned session_id
        (which may differ from what the hook currently reports) is tracked
        correctly instead of silently filtered out.
        """
        from .session import session_manager

        return {
            wid: ws.session_id
            for wid, ws in session_manager.window_states.items()
            if ws.session_id
        }

    async def _cleanup_all_stale_sessions(self) -> None:
        """Clean up all tracked sessions not in current session_map (used on startup)."""
        current_map = await self._load_current_session_map()
        active_session_ids = set(current_map.values())

        stale_sessions = []
        for session_id in self.state.tracked_sessions.keys():
            if session_id not in active_session_ids:
                stale_sessions.append(session_id)

        if stale_sessions:
            logger.info(
                f"[Startup cleanup] Removing {len(stale_sessions)} stale sessions"
            )
            for session_id in stale_sessions:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
            self.state.save_if_dirty()

    async def _final_drain_session(self, session_id: str) -> None:
        """Deliver any unread trailing lines before a session's tracking is removed.

        `_detect_and_cleanup_changes` removes a session from tracking the
        moment it observes the window's session_id change (/clear, resume)
        or the window's deletion — in the same poll cycle it observes it.
        Without this, any lines Claude appended to the OLD jsonl since the
        last read are permanently lost: nothing will ever read that offset
        range again once tracking is gone.

        Performs one last read + parse + dispatch, inline (awaited, not
        fire-and-forget) so removal happens strictly after delivery is
        attempted. A delivery failure here is logged (by
        `_dispatch_session_messages`) and is not retried — the session is
        dying either way; one honest attempt is all there is.

        Skips (rather than reads) a session still in `_inflight`: its
        offset was reverted to a pre-read value pending that batch's
        delivery ACK (see `check_for_updates`), so reading here now would
        re-read the same range and race a second concurrent dispatch
        against the first — exactly the double-dispatch `_inflight`
        backpressure exists to prevent. That in-flight batch will still be
        delivered on its own; only lines appended after its read (a
        narrower window than the bug this drain fixes) could be missed.
        """
        if session_id in self._inflight:
            logger.info(
                "Final drain skipped for session %s: a previous batch is "
                "still in flight",
                session_id,
            )
            return
        tracked = self.state.get_session(session_id)
        if tracked is None:
            return
        file_path = Path(tracked.file_path)
        if not file_path.exists():
            return

        new_entries = await self._read_new_lines(tracked, file_path)
        if not new_entries:
            return

        carry = self._pending_tools.get(session_id, {})
        parsed_entries, _remaining = TranscriptParser.parse_entries(
            new_entries, pending_tools=carry
        )
        session_messages = self._entries_to_messages(session_id, parsed_entries)
        if not session_messages:
            return

        logger.info(
            "Final drain: delivering %d trailing message(s) for session %s "
            "before removing tracking",
            len(session_messages),
            session_id,
        )
        await self._dispatch_session_messages(session_id, session_messages)

    async def _detect_and_cleanup_changes(self) -> dict[str, str]:
        """Detect session_map changes and cleanup replaced/removed sessions.

        Returns current session_map for further processing.
        """
        current_map = await self._load_current_session_map()

        sessions_to_remove: set[str] = set()

        # Check for window session changes (window exists in both, but session_id changed)
        for window_id, old_session_id in self._last_session_map.items():
            new_session_id = current_map.get(window_id)
            if new_session_id and new_session_id != old_session_id:
                logger.info(
                    "Window '%s' session changed: %s -> %s",
                    window_id,
                    old_session_id,
                    new_session_id,
                )
                sessions_to_remove.add(old_session_id)

        # Check for deleted windows (window in old map but not in current)
        old_windows = set(self._last_session_map.keys())
        current_windows = set(current_map.keys())
        deleted_windows = old_windows - current_windows

        for window_id in deleted_windows:
            old_session_id = self._last_session_map[window_id]
            logger.info(
                "Window '%s' deleted, removing session %s",
                window_id,
                old_session_id,
            )
            sessions_to_remove.add(old_session_id)

        # Perform cleanup
        if sessions_to_remove:
            for session_id in sessions_to_remove:
                await self._final_drain_session(session_id)
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
            self.state.save_if_dirty()

        # Update last known map
        self._last_session_map = current_map

        return current_map

    async def _dispatch_session_messages(
        self, session_id: str, messages: list[NewMessage]
    ) -> bool:
        """Dispatch messages for one session sequentially.

        Returns True only if every message callback in this batch completed
        without raising. On the first callback exception, logs it and stops
        dispatching the rest of the batch — preserving delivery order,
        since a later message must never be delivered ahead of one that
        failed — and returns False so the caller (`_dispatch_and_commit`)
        knows to retry the whole batch next cycle instead of committing it.

        Fires the turn-end callback after a fully-delivered batch, unless
        the batch ended on an unpaired tool_use — a conservative signal
        that Claude is mid-tool-call right now.

        (Earlier versions gated on `session_id not in self._pending_tools`,
        but `_pending_tools` accumulates unresolved tools across the whole
        session history; a single canceled or unmatched tool_use left in it
        would silently block the callback forever.)
        """
        last_unpaired_tool_use_id: str | None = None
        for msg in messages:
            try:
                if self._message_callback:
                    await self._message_callback(msg)
            except Exception as e:
                logger.error("Message callback error (session %s): %s", session_id, e)
                return False
            if msg.content_type == "tool_use" and msg.tool_use_id:
                last_unpaired_tool_use_id = msg.tool_use_id
            elif msg.content_type == "tool_result" and msg.tool_use_id:
                if last_unpaired_tool_use_id == msg.tool_use_id:
                    last_unpaired_tool_use_id = None

        turn_ended = last_unpaired_tool_use_id is None
        if self._turn_end_callback and turn_ended:
            try:
                await self._turn_end_callback(session_id)
            except Exception as e:
                logger.error("Turn-end callback error (session %s): %s", session_id, e)
        return True

    def _commit_offset(self, session_id: str, offset: int) -> None:
        """Persist `offset` as the session's last_byte_offset (the ACK commit)."""
        tracked = self.state.get_session(session_id)
        if tracked is None:
            # Session was cleaned up (window closed/changed) while its batch
            # was in flight — nothing left to persist for it.
            return
        tracked.last_byte_offset = offset
        self.state.update_session(tracked)
        self.state.save_if_dirty()

    async def _dispatch_and_commit(
        self,
        session_id: str,
        messages: list[NewMessage],
        commit: _PendingCommit | None,
    ) -> None:
        """Deliver one session's undelivered batch, then commit or retry.

        Runs as a fire-and-forget task from `_monitor_loop` (one per session
        per poll cycle). `session_id` is added to `_inflight` for the
        duration so `check_for_updates` will not read ahead of this batch
        (also prevents two dispatch tasks for the same session racing each
        other).

        On successful delivery: persists `commit.offset_after`, resetting
        the failure counter. On failure: restores `_pending_tools` to its
        pre-parse snapshot so the next cycle's re-parse starts from
        identical pairing state, and increments the failure counter. After
        3 consecutive failures for a session, the batch is dropped (offset
        committed anyway, error logged) so a poison batch cannot wedge the
        session's reads forever.
        """
        self._inflight.add(session_id)
        try:
            delivered = await self._dispatch_session_messages(session_id, messages)

            if commit is None:
                logger.error(
                    "No pending commit info for session %s; offset not persisted",
                    session_id,
                )
                return

            if delivered:
                self._commit_offset(session_id, commit.offset_after)
                self._delivery_failures.pop(session_id, None)
                return

            if commit.carry_before:
                self._pending_tools[session_id] = commit.carry_before
            else:
                self._pending_tools.pop(session_id, None)

            failures = self._delivery_failures.get(session_id, 0) + 1
            if failures >= 3:
                logger.error(
                    "Dropping %d message(s) for session %s after 3 failed "
                    "delivery attempts",
                    len(messages),
                    session_id,
                )
                self._commit_offset(session_id, commit.offset_after)
                self._delivery_failures.pop(session_id, None)
            else:
                self._delivery_failures[session_id] = failures
        finally:
            self._inflight.discard(session_id)

    async def _monitor_loop(self) -> None:
        """Background loop for checking session updates.

        Uses simple async polling with aiofiles for non-blocking I/O.
        """
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        # Deferred import to avoid circular dependency (cached once)
        from .session import session_manager

        # Populate the reconciled authority (window_states) before it is
        # read below — otherwise startup cleanup and the initial
        # _last_session_map would see an empty map and wrongly treat every
        # tracked session as stale.
        await session_manager.load_session_map()
        # Clean up all stale sessions on startup
        await self._cleanup_all_stale_sessions()
        # Initialize last known session_map
        self._last_session_map = await self._load_current_session_map()

        while self._running:
            try:
                # Load hook-based session map updates
                await session_manager.load_session_map()

                # Detect session_map changes and cleanup replaced/removed sessions
                current_map = await self._detect_and_cleanup_changes()
                active_session_ids = set(current_map.values())

                # Check for new messages (all I/O is async)
                new_messages, pending_commits = await self.check_for_updates(
                    active_session_ids
                )

                if new_messages and self._message_callback:
                    # Group messages by session_id for concurrent dispatch
                    groups: dict[str, list[NewMessage]] = {}
                    for msg in new_messages:
                        status = "complete" if msg.is_complete else "streaming"
                        preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
                        logger.info(
                            "[%s] session=%s: %s", status, msg.session_id, preview
                        )
                        groups.setdefault(msg.session_id, []).append(msg)

                    for session_id, msgs in groups.items():
                        task = asyncio.create_task(
                            self._dispatch_and_commit(
                                session_id, msgs, pending_commits.get(session_id)
                            )
                        )
                        self._callback_tasks.add(task)
                        task.add_done_callback(self._callback_tasks.discard)

            except Exception:
                logger.exception("Monitor loop error")

            await asyncio.sleep(self.poll_interval)

        logger.info("Session monitor stopped")

    def start(self) -> None:
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(
            supervise_loop(
                "session monitor",
                self._monitor_loop,
                should_run=lambda: self._running,
            )
        )

    def _stop_poll_loop(self) -> None:
        """Stop the background poll loop task. Idempotent."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def drain_callbacks(self, timeout: float = 5.0) -> None:
        """Stop the poll loop and AWAIT in-flight dispatch tasks before shutdown.

        Byte offsets now advance on delivery ACK, not on read (see the
        module docstring's delivery contract), so an in-flight batch that
        gets interrupted here is no longer at risk of being silently
        skipped after a restart — the offset was never advanced past it,
        so the next start simply re-reads and re-dispatches it. Awaiting
        the outstanding dispatch tasks here instead avoids that redundant
        replay: already-read messages get their chance to be delivered
        (and their offset committed) before we shut down, so a ccbot
        restart doesn't needlessly redeliver them. Call before stop().
        """
        self._stop_poll_loop()
        pending = list(self._callback_tasks)
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "drain_callbacks: timed out awaiting %d in-flight dispatch task(s)",
                len(pending),
            )

    def stop(self) -> None:
        self._stop_poll_loop()
        # Cancel any outstanding fire-and-forget callback tasks. (post_shutdown
        # calls drain_callbacks() first to *deliver* them; this is the backstop
        # for any that remain or for a stop() without a prior drain.)
        for task in list(self._callback_tasks):
            task.cancel()
        self._callback_tasks.clear()
        self.state.save()
        logger.info("Session monitor stopped and state saved")
