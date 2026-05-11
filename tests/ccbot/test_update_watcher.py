"""Unit tests for update_watcher: per-window version tracking + upgrade restart."""

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


class TestMaybeRestartForUpgrade:
    """Per-window upgrade detection: compare current vs WindowState.claude_launch_version."""

    @staticmethod
    def _stub_session_manager(monkeypatch, *, launch_version: str):
        """Patch session_manager so window_state reads/writes are observable."""
        from ccbot import session

        ws = MagicMock(claude_launch_version=launch_version)
        sm = MagicMock()
        sm.get_window_state.return_value = ws
        sm.set_claude_launch_version = MagicMock()
        monkeypatch.setattr(session, "session_manager", sm)
        return sm, ws

    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(config, "auto_restart_enabled", False)
        with patch("subprocess.run") as m:
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        m.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_failure_is_noop(self, monkeypatch):
        # Version probe failed → no state read, no restart.
        from ccbot import session

        sm = MagicMock()
        monkeypatch.setattr(session, "session_manager", sm)
        restart = AsyncMock()
        with (
            patch("subprocess.run", side_effect=FileNotFoundError()),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        restart.assert_not_awaited()
        sm.get_window_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_launch_version_is_noop(self, monkeypatch):
        self._stub_session_manager(monkeypatch, launch_version="2.1.118")
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mismatched_launch_version_triggers_restart(self, monkeypatch):
        # Per-window upgrade: this window was launched with old version,
        # installed is newer → restart this specific window.
        self._stub_session_manager(monkeypatch, launch_version="2.1.117")
        restart = AsyncMock(return_value=True)
        bot = MagicMock()
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=bot, user_id=7, thread_id=42, window_id="@9"
            )
        restart.assert_awaited_once_with(bot, 7, 42, "@9", "2.1.118")

    @pytest.mark.asyncio
    async def test_failed_restart_leaves_state_unchanged(self, monkeypatch):
        # On failure _restart_topic returns False; we must NOT advance
        # claude_launch_version on the old window — next turn-end retries.
        sm, _ws = self._stub_session_manager(monkeypatch, launch_version="2.1.117")
        restart = AsyncMock(return_value=False)
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        restart.assert_awaited_once()
        # Backfill path was NOT taken (launch_version was non-empty), so
        # set_claude_launch_version must not have been called from this fn.
        sm.set_claude_launch_version.assert_not_called()

    @pytest.mark.asyncio
    async def test_backfill_from_pane_version_triggers_restart(self, monkeypatch):
        # Pre-existing window (no recorded launch_version): backfill from
        # the pane's process name, which is the running version string.
        # Mismatch with current installed → restart fires.
        # This is the exact case the user reported: pane shows 2.1.116
        # while installed is 2.1.118.
        sm, _ws = self._stub_session_manager(monkeypatch, launch_version="")
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="2.1.116"),
        )
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        sm.set_claude_launch_version.assert_called_once_with("@0", "2.1.116")
        restart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_backfill_from_pane_version_matches_no_restart(self, monkeypatch):
        # Backfill reads version that already matches current → silent
        # migration, no restart.
        sm, _ws = self._stub_session_manager(monkeypatch, launch_version="")
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="2.1.118"),
        )
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        sm.set_claude_launch_version.assert_called_once_with("@0", "2.1.118")
        restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backfill_with_ambiguous_pane_defaults_to_current(self, monkeypatch):
        # Pane shows "claude" (transient state, not a version string) — we
        # can't tell the running version, so default to current. Silent
        # migration, no restart. Avoids spurious restart on first turn-end
        # after deploy.
        sm, _ws = self._stub_session_manager(monkeypatch, launch_version="")
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="claude"),
        )
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        sm.set_claude_launch_version.assert_called_once_with("@0", "2.1.118")
        restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backfill_with_no_pane_defaults_to_current(self, monkeypatch):
        # Pane vanished (window dead) — same fallback: assume current,
        # don't restart.
        sm, _ws = self._stub_session_manager(monkeypatch, launch_version="")
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value=None),
        )
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        sm.set_claude_launch_version.assert_called_once_with("@0", "2.1.118")
        restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backfill_reads_version_from_wrapper_shell_descendant(
        self, monkeypatch
    ):
        # Reproduces the marrige-proposal scenario: window was created via
        # `window_shell`, so pane_current_command reports the wrapper shell
        # (zsh) rather than the running claude's version string. Backfill
        # must walk the process tree to discover the actual running version
        # — otherwise it defaults to current and silently silences the
        # upgrade signal forever.
        sm, _ws = self._stub_session_manager(monkeypatch, launch_version="")
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
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("2.1.118\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        # Backfill must pin to the OBSERVED running version (2.1.116), not
        # the fresh installed current (2.1.118). Mismatch then triggers the
        # restart that the wrapper-shell pane would otherwise hide.
        sm.set_claude_launch_version.assert_called_once_with("@0", "2.1.116")
        restart.assert_awaited_once()


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


class TestRestartTopic:
    """End-to-end behavior of _restart_topic with the health check in place."""

    def _setup_session_manager(self, monkeypatch, *, sid="sid-abc", cwd="/tmp"):
        from ccbot import session

        ws = MagicMock(session_id=sid, cwd=cwd, window_name="my-topic")
        sm = MagicMock()
        sm.get_window_state.return_value = ws
        sm.get_display_name.return_value = "my-topic"
        sm.bind_thread = MagicMock()
        sm._save_state = MagicMock()
        sm.set_claude_launch_version = MagicMock()
        monkeypatch.setattr(session, "session_manager", sm)
        return sm, ws

    @pytest.mark.asyncio
    async def test_success_path_acks_and_returns_true(self, monkeypatch):
        sm, ws = self._setup_session_manager(monkeypatch)
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)

        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager, "kill_window", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            tm.tmux_manager,
            "create_window",
            AsyncMock(return_value=(True, "ok", "my-topic", "@9-new")),
        )
        monkeypatch.setattr(
            tm.tmux_manager,
            "get_pane_current_command",
            AsyncMock(return_value="2.1.118"),
        )

        enqueue = AsyncMock()
        with patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue):
            result = await update_watcher._restart_topic(
                bot=MagicMock(),
                user_id=1,
                thread_id=2,
                old_wid="@9",
                new_version="2.1.118",
            )

        assert result is True
        # Override block reassigns sid → save called.
        sm._save_state.assert_called()
        # New window's launch version pinned to the upgraded version, so the
        # next turn-end on this window sees a match instead of looping.
        sm.set_claude_launch_version.assert_called_once_with("@9-new", "2.1.118")
        # Exactly one message enqueued, and it's the success ack.
        assert enqueue.await_count == 1
        kwargs = enqueue.await_args.kwargs
        assert "♻️" in kwargs["text"]
        assert "2.1.118" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_health_check_failure_warns_and_returns_false(self, monkeypatch):
        sm, ws = self._setup_session_manager(monkeypatch)
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_INTERVAL", 0.01)
        monkeypatch.setattr(update_watcher, "_RESTART_HEALTH_TIMEOUT", 0.05)

        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager, "kill_window", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            tm.tmux_manager,
            "create_window",
            AsyncMock(return_value=(True, "ok", "my-topic", "@9-new")),
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
            result = await update_watcher._restart_topic(
                bot=MagicMock(),
                user_id=1,
                thread_id=2,
                old_wid="@9",
                new_version="2.1.118",
            )

        assert result is False
        # Warning enqueued, NOT the cheerful ack.
        assert enqueue.await_count == 1
        kwargs = enqueue.await_args.kwargs
        assert "⚠️" in kwargs["text"]
        assert "zsh" in kwargs["text"]
        # Must NOT have run the resume override (sid pinning).
        # Override happens only on success — saved exactly once during
        # bind_thread? bind_thread is mocked, so _save_state should not be
        # called from _restart_topic itself on the failure path.
        sm._save_state.assert_not_called()
        # And the new window's launch_version must NOT be pinned, so the
        # next turn-end can retry the upgrade.
        sm.set_claude_launch_version.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_window_failure_returns_false_no_enqueue(self, monkeypatch):
        sm, _ws = self._setup_session_manager(monkeypatch)
        from ccbot import tmux_manager as tm

        monkeypatch.setattr(
            tm.tmux_manager, "kill_window", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            tm.tmux_manager,
            "create_window",
            AsyncMock(return_value=(False, "tmux died", "", "")),
        )

        enqueue = AsyncMock()
        with patch("ccbot.handlers.message_queue.enqueue_content_message", new=enqueue):
            result = await update_watcher._restart_topic(
                bot=MagicMock(),
                user_id=1,
                thread_id=2,
                old_wid="@9",
                new_version="2.1.118",
            )

        assert result is False
        enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_cwd_skips_restart(self, monkeypatch):
        # Defensive path: window state has no cwd → can't recreate.
        self._setup_session_manager(monkeypatch, cwd="")
        from ccbot import tmux_manager as tm

        kill = AsyncMock()
        monkeypatch.setattr(tm.tmux_manager, "kill_window", kill)

        result = await update_watcher._restart_topic(
            bot=MagicMock(),
            user_id=1,
            thread_id=2,
            old_wid="@9",
            new_version="2.1.118",
        )

        assert result is False
        kill.assert_not_awaited()
