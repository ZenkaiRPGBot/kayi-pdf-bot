"""Configuration parsing and safety checks."""

from __future__ import annotations

import re

import pytest

from app.config import ConfigError, Settings, load_settings

SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def test_default_limits_and_properties() -> None:
    settings = Settings()
    assert settings.max_file_size_bytes == 20 * 1024 * 1024
    assert settings.max_output_size_bytes == 45 * 1024 * 1024
    assert settings.max_total_input_bytes == 60 * 1024 * 1024
    assert settings.max_total_output_bytes == 60 * 1024 * 1024
    assert settings.webhook_path == "/telegram/webhook"
    assert settings.fsub_channel_id == "@KayiBots"
    assert settings.max_active_jobs == 1
    assert settings.processing_timeout == 60.0


def test_webhook_url_requires_base_url() -> None:
    assert Settings().webhook_url == ""
    settings = Settings(public_base_url="https://example.com/")
    assert settings.public_base_url == "https://example.com"
    assert settings.webhook_url == "https://example.com/telegram/webhook"
    assert settings.webhook_configured is False
    assert Settings(
        public_base_url="https://example.com", webhook_secret="abc"
    ).webhook_configured is True


def test_webhook_secret_validation() -> None:
    with pytest.raises(ConfigError):
        Settings(webhook_secret="bad secret with spaces")
    with pytest.raises(ConfigError):
        Settings(webhook_secret="x" * 257)


def test_webhook_path_must_start_with_slash() -> None:
    with pytest.raises(ConfigError):
        Settings(webhook_path="telegram/webhook")


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ConfigError):
        Settings(max_files_merge=1)
    with pytest.raises(ConfigError):
        Settings(max_files_merge=11)
    with pytest.raises(ConfigError):
        Settings(max_split_outputs=2, split_individual_max=3)
    with pytest.raises(ConfigError):
        Settings(processing_timeout=0)
    with pytest.raises(ConfigError):
        Settings(max_active_jobs=0)


def test_load_settings_requires_token() -> None:
    with pytest.raises(ConfigError):
        load_settings(require_token=True)


def test_load_settings_generates_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    settings = load_settings(require_token=False)
    assert settings.bot_token == "123:abc"
    assert settings.webhook_secret
    assert SECRET_RE.match(settings.webhook_secret)
    assert "secret" not in repr(settings)


def test_load_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("WEBHOOK_SECRET", "known_secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kayi.example/")
    monkeypatch.setenv("FSUB_CHANNEL", "KayiBots")
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "5")
    monkeypatch.setenv("ENABLE_SELF_PING", "yes")
    monkeypatch.setenv("PING_URL", "https://kayi.example/ping")
    settings = load_settings()
    assert settings.public_base_url == "https://kayi.example"
    assert settings.fsub_channel == "@KayiBots"
    assert settings.max_file_size_mb == 5
    assert settings.enable_self_ping is True
    assert settings.ping_url == "https://kayi.example/ping"


def test_empty_fsub_invite_url_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("FSUB_INVITE_URL", "")
    assert load_settings().fsub_invite_url == "https://t.me/KayiBots"


def test_render_external_url_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://kayi-pdf.onrender.com")
    settings = load_settings()
    assert settings.public_base_url == "https://kayi-pdf.onrender.com"
    assert settings.webhook_url.endswith("/telegram/webhook")


def test_public_base_url_takes_precedence_over_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://custom.example")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://render.example")
    assert load_settings().public_base_url == "https://custom.example"


def test_invalid_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "many")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "20")
    monkeypatch.setenv("ENABLE_SELF_PING", "sometimes")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.delenv("ENABLE_SELF_PING")
    monkeypatch.setenv("PUBLIC_BASE_URL", "ftp://example.com")
    with pytest.raises(ConfigError):
        load_settings()
