"""Unit tests for pure helpers exposed from bot.py."""

from pathlib import Path

import pytest

from ccbot.bot import _resolve_browser_start_path


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
