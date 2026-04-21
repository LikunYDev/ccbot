"""Unit tests for update_watcher: baseline tracking and upgrade-triggered restart."""

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot import update_watcher
from ccbot.config import config

# Captured before the autouse fixture below stubs it, so resolution-specific
# tests can exercise the real function.
_REAL_RESOLVE = update_watcher._resolve_claude_binary


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    """Fresh module-level state + temp claude_version.json + stub binary resolution."""
    monkeypatch.setattr(config, "claude_version_file", tmp_path / "claude_version.json")
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
    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(config, "auto_restart_enabled", False)
        with patch("subprocess.run") as m:
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        m.assert_not_called()
        assert not config.claude_version_file.exists()

    @pytest.mark.asyncio
    async def test_first_boot_captures_baseline_without_restart(self):
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("1.2.3\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        restart.assert_not_awaited()
        assert json.loads(config.claude_version_file.read_text()) == {
            "installed": "1.2.3"
        }

    @pytest.mark.asyncio
    async def test_unchanged_version_is_noop(self):
        config.claude_version_file.write_text(json.dumps({"installed": "1.2.3"}))
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("1.2.3\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_changed_version_triggers_restart_and_updates_baseline(self):
        config.claude_version_file.write_text(json.dumps({"installed": "1.2.3"}))
        restart = AsyncMock(return_value=True)
        bot = MagicMock()
        with (
            patch("subprocess.run", return_value=_make_completed("1.2.4\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=bot, user_id=7, thread_id=42, window_id="@9"
            )
        restart.assert_awaited_once_with(bot, 7, 42, "@9", "1.2.4")
        assert json.loads(config.claude_version_file.read_text()) == {
            "installed": "1.2.4"
        }

    @pytest.mark.asyncio
    async def test_failed_restart_leaves_baseline_unchanged(self):
        config.claude_version_file.write_text(json.dumps({"installed": "1.2.3"}))
        restart = AsyncMock(return_value=False)
        with (
            patch("subprocess.run", return_value=_make_completed("1.2.4\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        restart.assert_awaited_once()
        assert json.loads(config.claude_version_file.read_text()) == {
            "installed": "1.2.3"
        }

    @pytest.mark.asyncio
    async def test_probe_failure_is_noop(self):
        config.claude_version_file.write_text(json.dumps({"installed": "1.2.3"}))
        restart = AsyncMock()
        with (
            patch("subprocess.run", side_effect=FileNotFoundError()),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        restart.assert_not_awaited()
        assert json.loads(config.claude_version_file.read_text()) == {
            "installed": "1.2.3"
        }

    @pytest.mark.asyncio
    async def test_corrupt_state_file_treated_as_absent(self):
        config.claude_version_file.write_text("{not json")
        restart = AsyncMock(return_value=True)
        with (
            patch("subprocess.run", return_value=_make_completed("1.2.3\n")),
            patch.object(update_watcher, "_restart_topic", restart),
        ):
            await update_watcher.maybe_restart_for_upgrade(
                bot=MagicMock(), user_id=1, thread_id=2, window_id="@0"
            )
        # Corrupt baseline = first boot: capture, do not restart
        restart.assert_not_awaited()
        assert json.loads(config.claude_version_file.read_text()) == {
            "installed": "1.2.3"
        }
