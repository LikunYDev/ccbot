"""Shared utility functions used across multiple CCBot modules.

Provides:
  - ccbot_dir(): resolve config directory from CCBOT_DIR env var.
  - atomic_write_json(): crash-safe JSON file writes via temp+rename.
  - read_cwd_from_jsonl(): extract the cwd field from the first JSONL entry.
  - supervise_loop(): restart a background loop coroutine if it crashes or
    exits unexpectedly, instead of silently killing monitoring forever.
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Coroutine

CCBOT_DIR_ENV = "CCBOT_DIR"

logger = logging.getLogger(__name__)


def ccbot_dir() -> Path:
    """Resolve config directory from CCBOT_DIR env var or default ~/.ccbot."""
    raw = os.environ.get(CCBOT_DIR_ENV, "")
    return Path(raw) if raw else Path.home() / ".ccbot"


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory, then renames it
    to the target path. This prevents data corruption if the process
    is interrupted mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    # Write to temp file in same directory (same filesystem for atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_cwd_from_jsonl(file_path: str | Path) -> str:
    """Read the cwd field from the first JSONL entry that has one.

    Shared by session.py and session_monitor.py.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    cwd = data.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return ""


async def supervise_loop(
    name: str,
    factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    should_run: Callable[[], bool] | None = None,
    restart_delay: float = 5.0,
) -> None:
    """Run a background loop coroutine, restarting it if it crashes or exits.

    `factory` is called to produce a fresh coroutine each attempt (the loop
    coroutine itself is single-use, so it can't just be awaited twice).

    - Normal return: if `should_run` is given and now returns False, this is
      an intended stop — exit quietly. Otherwise the loop exited on its own,
      which is unexpected — log and restart after `restart_delay`.
    - `asyncio.CancelledError`: re-raised immediately, no restart (this is
      how callers cancel the supervisor task to stop supervision).
    - Any other exception: logged with traceback and restarted after
      `restart_delay`.
    """
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s crashed; restarting in %.0fs", name, restart_delay)
            await asyncio.sleep(restart_delay)
            continue

        if should_run is not None and not should_run():
            return

        logger.error("%s exited unexpectedly; restarting in %.0fs", name, restart_delay)
        await asyncio.sleep(restart_delay)
