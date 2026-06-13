"""Unit tests for update_watcher: version tracking, one-time update/failure
notices, and in-place restart."""

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot import update_watcher
from ccbot.config import config

# Captured before the autouse fixture below stubs it, so resolution-specific
# tests can exercise the real function.
_REAL_RESOLVE = update_watcher._resolve_claude_binary


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Fresh module-level version cache + stub binary resolution."""
    monkeypatch.setattr(config, "auto_restart_enabled", True)
    # Pretend the binary is always resolvable; tests that specifically
    # exercise resolution override this.
    monkeypatch.setattr(
        update_watcher, "_resolve_claude_binary", lambda: "/fake/bin/claude"
    )
    update_watcher.reset_state_for_tests()
    yield
    update_watcher.reset_state_for_tests()


def _make_completed(stdout: str = "1.2.3 (Claude Code)\n", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["claude", "--version"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


class TestCurrentClaudeVersion:
    @pytest.mark.asyncio
    async def test_parses_dotted_version(self):
        with patch(
            "subprocess.run", return_value=_make_completed("1.2.3 (Claude Code)\n")
        ):
            assert await update_watcher.current_claude_version() == "1.2.3"

    @pytest.mark.asyncio
    async def test_caches_within_ttl(self):
        with patch("subprocess.run", return_value=_make_completed("1.2.3\n")) as m:
            await update_watcher.current_claude_version()
            await update_watcher.current_claude_version()
            await update_watcher.current_claude_version()
        assert m.call_count == 1

    @pytest.mark.asyncio
    async def test_force_bypasses_cache(self):
        with patch("subprocess.run", return_value=_make_completed("1.2.3\n")) as m:
            await update_watcher.current_claude_version()
            await update_watcher.current_claude_version(force=True)
        assert m.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_on_nonzero_exit(self):
        with patch("subprocess.run", return_value=_make_completed("", returncode=1)):
            assert await update_watcher.current_claude_version() is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unparseable(self):
        with patch("subprocess.run", return_value=_make_completed("no version here\n")):
            assert await update_watcher.current_claude_version() is None

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=5)

        with patch("subprocess.run", side_effect=raise_timeout):
            assert await update_watcher.current_claude_version() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_binary_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert await update_watcher.current_claude_version() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_resolution_fails(self, monkeypatch):
        """When shutil.which fails for both PATH and fallbacks, return None."""
        monkeypatch.setattr(update_watcher, "_resolve_claude_binary", lambda: None)
        with patch("subprocess.run") as m:
            assert await update_watcher.current_claude_version() is None
        m.assert_not_called()


class TestResolveClaudeBinary:
    """Binary-path resolution with fallback to common install locations."""

    def test_uses_path_lookup_when_available(self, monkeypatch):
        monkeypatch.setattr(config, "claude_command", "claude")

        def which(cmd, path=None):
            # First call: standard PATH lookup succeeds
            return "/custom/bin/claude" if path is None else None

        with patch("shutil.which", side_effect=which):
            assert _REAL_RESOLVE() == "/custom/bin/claude"

    def test_falls_back_to_known_install_dirs(self, monkeypatch):
        monkeypatch.setattr(config, "claude_command", "claude")

        def which(cmd, path=None):
            # PATH lookup fails; fallback PATH (the curated list) succeeds
            if path is None:
                return None
            assert ".local/bin" in path  # curated list must include it
            return "/Users/someone/.local/bin/claude"

        with patch("shutil.which", side_effect=which):
            resolved = _REAL_RESOLVE()
        assert resolved == "/Users/someone/.local/bin/claude"

    def test_returns_none_when_all_lookups_fail(self, monkeypatch):
        monkeypatch.setattr(config, "claude_command", "claude")
        with patch("shutil.which", return_value=None):
            assert _REAL_RESOLVE() is None


class TestMaybeNotifyUpdateOrFailure:
    """Turn-end notifier: one-time update + failure notices, never auto-restart."""

    @staticmethod
    def _stub(monkeypatch, *, launch_version: str, pane_text: str | None = None):
        """Stub session_manager (observable window_state) + pane capture."""
        from ccbot import session
        from ccbot import tmux_manager as tm

        ws = MagicMock(
            claude_launch_version=launch_version,
            update_notified_version="",
            failure_notified=False,
        )
        sm = MagicMock()
        sm.get_window_state.return_value = ws
        sm.set_claude_launch_version = MagicMock()
        sm._save_state = MagicMock()
        monkeypatch.setattr(session, "session_manager", sm)
        monkeypatch.setattr(
            tm.tmux_manager, "capture_pane", AsyncMock(return_value=pane_text)
        )
        return sm, ws

    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(config, "auto_restart_enabled", False)
        enqueue = AsyncMock()
        with (
            patch("subprocess.run") as m,
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        m.assert_not_called()
        enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_matching_version_clean_pane_no_notice(self, monkeypatch):
        self._stub(monkeypatch, launch_version="2.1.118", pane_text="❯ ready")
        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_version_drift_notifies_once(self, monkeypatch):
        # The notice fires once and never re-nags the same drift on later turns.
        _sm, ws = self._stub(monkeypatch, launch_version="2.1.117")
        enqueue = AsyncMock()
        bot = MagicMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=bot, user_id=7, thread_id=42, window_id="@9"
            )
            await update_watcher.maybe_notify_update_or_failure(
                bot=bot, user_id=7, thread_id=42, window_id="@9"
            )
        assert enqueue.await_count == 1
        text = enqueue.await_args.kwargs["text"]
        assert "ℹ️" in text and "2.1.118" in text and "2.1.117" in text
        assert "/restart" in text
        # marker advanced so it never re-nags; no restart attempted
        assert ws.update_notified_version == "2.1.118"

    @pytest.mark.asyncio
    async def test_backfill_from_pane_then_notifies(self, monkeypatch):
        # Pre-existing window (no recorded launch_version): backfill from the
        # pane's version string; mismatch with current → one update notice.
        sm, _ws = self._stub(monkeypatch, launch_version="")
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="2.1.116"),
        )
        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        sm.set_claude_launch_version.assert_called_once_with("@0", "2.1.116")
        assert enqueue.await_count == 1
        assert "ℹ️" in enqueue.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_backfill_matches_current_no_notice(self, monkeypatch):
        sm, _ws = self._stub(monkeypatch, launch_version="")
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="2.1.118"),
        )
        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        sm.set_claude_launch_version.assert_called_once_with("@0", "2.1.118")
        enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backfill_wrapper_shell_descendant_then_notifies(self, monkeypatch):
        # window_shell hides claude's version behind the wrapper shell; backfill
        # walks the process tree to the real running version, then notifies.
        sm, _ws = self._stub(monkeypatch, launch_version="")
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="zsh"),
        )
        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_pid",
            AsyncMock(return_value=12345),
        )
        monkeypatch.setattr(
            update_watcher,
            "_find_version_descendant",
            AsyncMock(return_value="2.1.116"),
            raising=False,
        )
        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        sm.set_claude_launch_version.assert_called_once_with("@0", "2.1.116")
        assert enqueue.await_count == 1

    @pytest.mark.asyncio
    async def test_failure_signature_notifies_once(self, monkeypatch):
        # Version matches (no update notice), but the pane shows a fatal error
        # (a revoked/unavailable model) → one failure notice, deduped after.
        _sm, ws = self._stub(
            monkeypatch,
            launch_version="2.1.118",
            pane_text="There's an issue with the selected model (claude-fable-5).",
        )
        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        assert enqueue.await_count == 1
        text = enqueue.await_args.kwargs["text"]
        assert "⚠️" in text and "/restart" in text
        assert ws.failure_notified is True

    @pytest.mark.asyncio
    async def test_failure_clears_resets_flag(self, monkeypatch):
        # A previously-notified failure that has since cleared resets the flag,
        # so a future recurrence notifies once again.
        _sm, ws = self._stub(
            monkeypatch, launch_version="2.1.118", pane_text="❯ all good"
        )
        ws.failure_notified = True
        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        enqueue.assert_not_awaited()
        assert ws.failure_notified is False

    @pytest.mark.asyncio
    async def test_capture_failure_preserves_failure_flag(self, monkeypatch):
        # capture_pane returning None means "couldn't inspect the pane", which
        # must NOT be treated as "clean" — otherwise a transient tmux hiccup
        # resets the flag and the notice flaps/re-fires next turn.
        _sm, ws = self._stub(monkeypatch, launch_version="2.1.118", pane_text=None)
        ws.failure_notified = True
        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        enqueue.assert_not_awaited()
        assert ws.failure_notified is True  # preserved, not reset

    @pytest.mark.asyncio
    async def test_failure_signature_outside_tail_is_ignored(self, monkeypatch):
        # A signature far up in scrollback (e.g. conversation text) must NOT
        # trigger — only the tail of the pane (the live banner region) is
        # scanned. Push the phrase above the last _FAILURE_SCAN_LINES lines.
        pane = "there's an issue with the selected model in this code\n" + "\n".join(
            f"line {i}" for i in range(update_watcher._FAILURE_SCAN_LINES + 5)
        )
        _sm, ws = self._stub(monkeypatch, launch_version="2.1.118", pane_text=pane)
        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            await update_watcher.maybe_notify_update_or_failure(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        enqueue.assert_not_awaited()
        assert ws.failure_notified is False


class TestWaitForClaudeInPane:
    """Health-check polling that replaces wait_for_session_map_entry."""

    @pytest.mark.asyncio
    async def test_returns_true_when_pane_command_matches_claude(self, monkeypatch):
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        with patch(
            "ccbot.tmux_manager.tmux_manager.get_pane_current_command",
            new=AsyncMock(return_value="claude"),
        ):
            ok, observed = await update_watcher._wait_for_claude_in_pane("@9", 0.1)
        assert ok is True
        assert observed == "claude"

    @pytest.mark.asyncio
    async def test_returns_true_when_pane_shows_version_string(self, monkeypatch):
        # claude renames its own process to its version (e.g. "2.1.118"),
        # so the regex must accept that shape.
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        with patch(
            "ccbot.tmux_manager.tmux_manager.get_pane_current_command",
            new=AsyncMock(return_value="2.1.118"),
        ):
            ok, observed = await update_watcher._wait_for_claude_in_pane("@9", 0.1)
        assert ok is True
        assert observed == "2.1.118"

    @pytest.mark.asyncio
    async def test_accepts_node(self, monkeypatch):
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        with patch(
            "ccbot.tmux_manager.tmux_manager.get_pane_current_command",
            new=AsyncMock(return_value="node"),
        ):
            ok, _ = await update_watcher._wait_for_claude_in_pane("@9", 0.1)
        assert ok is True

    @pytest.mark.asyncio
    async def test_healthy_when_wrapper_shell_has_claude_descendant(self, monkeypatch):
        # With `window_shell='PATH=... claude ...; exec zsh'`, tmux runs the
        # command via `sh -c`. The shell is the pane_pid; claude runs as its
        # child. `pane_current_command` reports the shell, NOT claude — but
        # claude IS running. The health check must accept this case instead
        # of timing out with a false-negative "claude didn't start".
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        with (
            patch(
                "ccbot.tmux_manager.tmux_manager.get_pane_current_command",
                new=AsyncMock(return_value="zsh"),
            ),
            patch(
                "ccbot.tmux_manager.tmux_manager.get_pane_pid",
                new=AsyncMock(return_value=12345),
                create=True,
            ),
            patch(
                "ccbot.update_watcher._has_claude_descendant",
                new=AsyncMock(return_value=True),
                create=True,
            ),
        ):
            ok, observed = await update_watcher._wait_for_claude_in_pane("@9", 0.1)
        assert ok is True
        assert observed == "zsh"

    @pytest.mark.asyncio
    async def test_returns_false_when_pane_stays_zsh_without_claude_child(
        self, monkeypatch
    ):
        # Real failure: shell is the pane_pid (wrapper) AND no claude descendant
        # exists — claude never actually started (binary missing, crashed, etc.).
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        with (
            patch(
                "ccbot.tmux_manager.tmux_manager.get_pane_current_command",
                new=AsyncMock(return_value="zsh"),
            ),
            patch(
                "ccbot.tmux_manager.tmux_manager.get_pane_pid",
                new=AsyncMock(return_value=12345),
                create=True,
            ),
            patch(
                "ccbot.update_watcher._has_claude_descendant",
                new=AsyncMock(return_value=False),
                create=True,
            ),
        ):
            ok, observed = await update_watcher._wait_for_claude_in_pane("@9", 0.05)
        assert ok is False
        assert observed == "zsh"

    @pytest.mark.asyncio
    async def test_returns_false_when_pane_vanishes(self, monkeypatch):
        # Pane gone entirely (window_shell exited and `; exec zsh` chain
        # didn't kick in, or window was killed externally).
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        with patch(
            "ccbot.tmux_manager.tmux_manager.get_pane_current_command",
            new=AsyncMock(return_value=None),
        ):
            ok, _ = await update_watcher._wait_for_claude_in_pane("@9", 0.1)
        assert ok is False

    @pytest.mark.asyncio
    async def test_eventually_becomes_claude(self, monkeypatch):
        # Realistic case: pane briefly shows the wrapper shell before claude
        # starts, then transitions. The poll loop should accept it.
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        responses = ["sh", "sh", "node", "claude"]
        mock = AsyncMock(side_effect=responses)
        with patch(
            "ccbot.tmux_manager.tmux_manager.get_pane_current_command", new=mock
        ):
            ok, observed = await update_watcher._wait_for_claude_in_pane("@9", 0.5)
        assert ok is True
        assert observed == "node"  # first match wins


class TestHasClaudeDescendant:
    """Walks `ps -axo pid,ppid,comm` output to find a claude-like descendant."""

    @staticmethod
    def _ps_output(rows: list[tuple[int, int, str]]) -> str:
        return "\n".join(f"{pid} {ppid} {comm}" for pid, ppid, comm in rows) + "\n"

    @pytest.mark.asyncio
    async def test_returns_true_when_direct_child_is_claude(self):
        # wrapper shell (pid=100) with claude as its direct child (pid=101).
        output = self._ps_output(
            [(1, 0, "init"), (100, 1, "zsh"), (101, 100, "claude")]
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert await update_watcher._has_claude_descendant(100) is True

    @pytest.mark.asyncio
    async def test_returns_true_when_descendant_uses_version_string(self):
        # claude renames its own process to its version (e.g. "2.1.118"), so
        # ps may show the version string instead of "claude".
        output = self._ps_output([(100, 1, "zsh"), (101, 100, "2.1.118")])
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert await update_watcher._has_claude_descendant(100) is True

    @pytest.mark.asyncio
    async def test_returns_true_when_grandchild_matches(self):
        # Claude may spawn through an intermediate process; BFS must descend.
        output = self._ps_output(
            [(100, 1, "zsh"), (101, 100, "sh"), (102, 101, "claude")]
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert await update_watcher._has_claude_descendant(100) is True

    @pytest.mark.asyncio
    async def test_strips_path_prefix_from_comm(self):
        # macOS `ps -o comm=` returns the full executable path; basename the
        # string so `/Users/alice/.local/bin/claude` still matches "claude".
        output = self._ps_output(
            [(100, 1, "zsh"), (101, 100, "/Users/alice/.local/bin/claude")]
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert await update_watcher._has_claude_descendant(100) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_descendant_matches(self):
        output = self._ps_output(
            [(100, 1, "zsh"), (101, 100, "cat"), (102, 100, "less")]
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert await update_watcher._has_claude_descendant(100) is False

    @pytest.mark.asyncio
    async def test_returns_false_when_pid_has_no_children(self):
        output = self._ps_output([(1, 0, "init"), (100, 1, "zsh")])
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert await update_watcher._has_claude_descendant(100) is False

    @pytest.mark.asyncio
    async def test_ignores_unrelated_claude_processes(self):
        # A claude process elsewhere in the tree must NOT make an unrelated
        # pane look healthy — the scan is rooted at the given pid.
        output = self._ps_output(
            [
                (1, 0, "init"),
                (100, 1, "zsh"),  # the pane we care about
                (101, 100, "cat"),
                (200, 1, "zsh"),  # unrelated pane
                (201, 200, "claude"),  # unrelated claude
            ]
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert await update_watcher._has_claude_descendant(100) is False

    @pytest.mark.asyncio
    async def test_returns_false_on_ps_failure(self):
        failed = subprocess.CompletedProcess(
            args=["ps"], returncode=1, stdout="", stderr="ps: boom"
        )
        with patch("subprocess.run", return_value=failed):
            assert await update_watcher._has_claude_descendant(100) is False

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=5.0),
        ):
            assert await update_watcher._has_claude_descendant(100) is False

    @pytest.mark.asyncio
    async def test_tolerates_malformed_lines(self):
        # Real ps output sometimes has blank lines / trailing whitespace.
        # Parser must skip them without crashing.
        output = "\n".join(
            ["", "100 1 zsh", "   ", "not-a-pid xxx claude", "101 100 claude"]
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert await update_watcher._has_claude_descendant(100) is True


class TestRestartTopicInPlace:
    """End-to-end behavior of restart_topic_in_place (respawn-pane + health check)."""

    def _setup_session_manager(self, monkeypatch, *, sid="sid-abc", cwd="/tmp"):
        from ccbot import session

        ws = MagicMock(
            session_id=sid,
            cwd=cwd,
            window_name="my-topic",
            update_notified_version="2.1.117",
            failure_notified=True,
        )
        sm = MagicMock()
        sm.get_window_state.return_value = ws
        sm.get_display_name.return_value = "my-topic"
        sm._save_state = MagicMock()
        sm.set_claude_launch_version = MagicMock()
        monkeypatch.setattr(session, "session_manager", sm)
        return sm, ws

    @pytest.mark.asyncio
    async def test_success_path_acks_resets_markers(self, monkeypatch):
        sm, ws = self._setup_session_manager(monkeypatch)
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)

        from ccbot import tmux_manager as tm

        respawn = AsyncMock(return_value=True)
        monkeypatch.setattr(tm.tmux_manager, "respawn_pane", respawn)
        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="2.1.118"),
        )

        enqueue = AsyncMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            result = await update_watcher.restart_topic_in_place(
                bot=MagicMock(),
                user_id=1,
                thread_id=2,
                window_id="@9",
            )

        assert result is True
        # In-place: same window reused, --resume'd with the original sid.
        respawn.assert_awaited_once()
        assert respawn.await_args.args[0] == "@9"
        assert respawn.await_args.kwargs.get("resume_session_id") == "sid-abc"
        # Baseline pinned to current (single save); pending notices cleared.
        assert ws.claude_launch_version == "2.1.118"
        assert ws.update_notified_version == ""
        assert ws.failure_notified is False
        # Exactly one message enqueued, and it's the success ack.
        assert enqueue.await_count == 1
        kwargs = enqueue.await_args.kwargs
        assert "♻️" in kwargs["text"]
        assert "2.1.118" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_failed_version_probe_clears_baseline(self, monkeypatch):
        # If the version probe fails during /restart, the launch baseline must
        # be CLEARED (not left at the stale pre-restart version), otherwise the
        # next turn-end emits a bogus "update available" notice for the version
        # we just restarted onto.
        sm, ws = self._setup_session_manager(monkeypatch)
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)

        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager, "respawn_pane", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="claude"),
        )

        enqueue = AsyncMock()
        with (
            patch("subprocess.run", side_effect=FileNotFoundError()),  # probe → None
            patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue),
        ):
            result = await update_watcher.restart_topic_in_place(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@9"
            )

        assert result is True
        assert ws.claude_launch_version == ""  # cleared, not stale "2.1.117"
        assert ws.update_notified_version == ""
        assert ws.failure_notified is False
        # Ack falls back to the generic label when the version is unknown.
        assert "the latest version" in enqueue.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_health_check_failure_warns_and_returns_false(self, monkeypatch):
        sm, _ws = self._setup_session_manager(monkeypatch)
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_TIMEOUT", 0.05)

        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager, "respawn_pane", AsyncMock(return_value=True)
        )
        # Silent-failure symptom: pane's foreground process is the wrapper
        # shell (zsh) AND claude is not among its descendants — claude never
        # started (missing binary, crashed immediately, etc.).
        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="zsh"),
        )
        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_pid",
            AsyncMock(return_value=99999),
        )
        monkeypatch.setattr(
            update_watcher,
            "_has_claude_descendant",
            AsyncMock(return_value=False),
        )

        enqueue = AsyncMock()
        with patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue):
            result = await update_watcher.restart_topic_in_place(
                bot=MagicMock(),
                user_id=1,
                thread_id=2,
                window_id="@9",
            )

        assert result is False
        # Warning enqueued, NOT the cheerful ack.
        assert enqueue.await_count == 1
        kwargs = enqueue.await_args.kwargs
        assert "⚠️" in kwargs["text"]
        assert "zsh" in kwargs["text"]
        # Baseline must NOT be re-saved on failure, so the user can retry.
        sm._save_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_respawn_failure_warns_and_returns_false(self, monkeypatch):
        sm, _ws = self._setup_session_manager(monkeypatch)
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager, "respawn_pane", AsyncMock(return_value=False)
        )

        enqueue = AsyncMock()
        with patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue):
            result = await update_watcher.restart_topic_in_place(
                bot=MagicMock(),
                user_id=1,
                thread_id=2,
                window_id="@9",
            )

        assert result is False
        # The user gets a warning (unlike create_window's silent failure path).
        assert enqueue.await_count == 1
        assert "⚠️" in enqueue.await_args.kwargs["text"]
        sm._save_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_cwd_skips_restart(self, monkeypatch):
        # Defensive path: window state has no cwd → can't relaunch.
        self._setup_session_manager(monkeypatch, cwd="")
        from ccbot import tmux_manager as tm

        respawn = AsyncMock()
        monkeypatch.setattr(tm.tmux_manager, "respawn_pane", respawn)

        result = await update_watcher.restart_topic_in_place(
            bot=MagicMock(),
            user_id=1,
            thread_id=2,
            window_id="@9",
        )

        assert result is False
        respawn.assert_not_awaited()
