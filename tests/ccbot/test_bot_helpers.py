"""Unit tests for pure helpers exposed from bot.py."""

from pathlib import Path

import pytest

from ccbot.bot import _bind_outcome_message, _resolve_browser_start_path


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestResolveBrowserStartPath:
    def test_unset_returns_cwd(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        monkeypatch.setattr(live_config, "default_dir", "")
        assert _resolve_browser_start_path() == str(Path.cwd())

    def test_existing_dir_returned(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        target = tmp_path / "obsidian"
        target.mkdir()
        monkeypatch.setattr(live_config, "default_dir", str(target))
        assert _resolve_browser_start_path() == str(target.resolve())

    def test_tilde_expansion(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        monkeypatch.setenv("HOME", str(tmp_path))
        sub = tmp_path / "notes"
        sub.mkdir()
        monkeypatch.setattr(live_config, "default_dir", "~/notes")
        assert _resolve_browser_start_path() == str(sub.resolve())

    def test_nonexistent_path_falls_back_to_cwd(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        monkeypatch.setattr(
            live_config, "default_dir", str(tmp_path / "does-not-exist")
        )
        assert _resolve_browser_start_path() == str(Path.cwd())

    def test_path_to_file_falls_back_to_cwd(self, monkeypatch, tmp_path):
        from ccbot.config import config as live_config

        f = tmp_path / "a-file"
        f.write_text("hi")
        monkeypatch.setattr(live_config, "default_dir", str(f))
        assert _resolve_browser_start_path() == str(Path.cwd())


class TestBindOutcomeMessage:
    """f65 / RC29: fresh windows must not claim success when the
    SessionStart hook never registered — WindowState.session_id would stay
    empty forever and every Claude reply would be silently dropped at
    routing, even though outbound sends still work."""

    MSG = "Created window 'foo' at /tmp/foo"

    def test_hook_ok_fresh_shows_created(self):
        result = _bind_outcome_message(self.MSG, hook_ok=True, resumed=False)
        assert result == f"✅ {self.MSG}\n\nCreated. Send messages here."

    def test_hook_ok_resumed_shows_resumed(self):
        result = _bind_outcome_message(self.MSG, hook_ok=True, resumed=True)
        assert result == f"✅ {self.MSG}\n\nResumed. Send messages here."

    def test_hook_failed_fresh_shows_warning(self):
        result = _bind_outcome_message(self.MSG, hook_ok=False, resumed=False)
        assert result.startswith(f"⚠️ {self.MSG}\n\n")
        assert "did not register" in result
        assert "ccbot hook --install" in result
        assert "/restart" in result
        # Must not claim the false "Created. Send messages here." success.
        assert "Created. Send messages here." not in result

    def test_hook_failed_resumed_still_shows_resumed(self):
        # Resume windows have their WindowState.session_id manually pinned
        # by the caller even when the hook times out (see the
        # resume-override logic in _create_and_bind_window), so routing
        # works either way and the normal "Resumed" text stays truthful.
        result = _bind_outcome_message(self.MSG, hook_ok=False, resumed=True)
        assert result == f"✅ {self.MSG}\n\nResumed. Send messages here."
