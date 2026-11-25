"""Tests for /ping, /usage, Render hosting metadata, and command registration."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from app.bot import build_services, create_dispatcher
from app.config import ConfigError, Settings, load_settings
from app.handlers.usage import (
    build_usage_card_text,
    is_owner,
    ping_handler,
    ping_refresh_callback,
    register_bot_commands,
    usage_handler,
    usage_refresh_callback,
)


def _settings(**overrides) -> Settings:
    defaults = {
        "bot_token": "123:test",
        "owner_id": 986380678,
        "render_email": "owner@example.com",
        "render_service_name": "kayi-pdf-bot",
        "render_repo": "ZenkaiRPGBot/kayi-pdf-bot",
        "render_used_hours": 10.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_is_owner_fail_closed() -> None:
    assert is_owner(986380678, _settings(owner_id=986380678)) is True
    assert is_owner(111111111, _settings(owner_id=986380678)) is False
    # Zero owner_id must deny everyone rather than opening /usage publicly
    assert is_owner(986380678, _settings(owner_id=0)) is False
    assert is_owner(None, _settings(owner_id=986380678)) is False


def test_render_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("RENDER_EMAIL", "render@example.com")
    monkeypatch.setenv("RENDER_SERVICE_NAME", "custom-pdf")
    monkeypatch.setenv("RENDER_REPO", "Org/repo")
    monkeypatch.setenv("RENDER_USED_HOURS", "12.5")
    settings = load_settings()
    assert settings.render_email == "render@example.com"
    assert settings.render_service_name == "custom-pdf"
    assert settings.render_repo == "Org/repo"
    assert settings.render_used_hours == 12.5

    monkeypatch.setenv("RENDER_USED_HOURS", "-1")
    with pytest.raises(ConfigError):
        load_settings()


@pytest.mark.asyncio
async def test_ping_handler_and_refresh() -> None:
    bot = MagicMock()
    bot.get_me = AsyncMock()

    msg = MagicMock()
    msg.reply = AsyncMock()

    await ping_handler(msg, bot)
    msg.reply.assert_called_once()
    text = msg.reply.call_args[0][0]
    markup = msg.reply.call_args[1]["reply_markup"]
    assert "<b>Pong!</b> ⚡" in text
    assert "Bot Responsiveness:" in text
    assert "Telegram API Latency:" in text
    assert "Uptime:" in text
    payload = json.loads(markup.model_dump_json(exclude_none=True))
    assert payload["inline_keyboard"][0][0]["callback_data"] == "ping:refresh"
    assert payload["inline_keyboard"][0][0]["style"] == "primary"

    cb = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    await ping_refresh_callback(cb, bot)
    cb.message.edit_text.assert_called_once()
    cb.answer.assert_called_once_with("Ping refreshed", show_alert=False)


@pytest.mark.asyncio
async def test_usage_handler_permissions_and_metrics(tmp_path) -> None:
    settings = _settings(temp_root=tmp_path / "pdf")
    services = build_services(settings)
    bot = MagicMock()
    bot.get_me = AsyncMock()

    # Non-owner blocked
    other_msg = MagicMock()
    other_msg.from_user = SimpleNamespace(id=22222)
    other_msg.reply = AsyncMock()
    other_msg.answer = AsyncMock()
    await usage_handler(other_msg, bot, settings, services.jobs, services.workspaces)
    other_msg.reply.assert_called_once_with("Access restricted to bot owner.")
    other_msg.answer.assert_not_called()

    # Zero OWNER_ID fail-closed
    unconfigured = _settings(owner_id=0, temp_root=tmp_path / "pdf")
    await usage_handler(other_msg, bot, unconfigured, services.jobs, services.workspaces)
    assert other_msg.reply.call_count == 2
    other_msg.answer.assert_not_called()

    # Owner allowed + real metrics rendered
    owner_msg = MagicMock()
    owner_msg.from_user = SimpleNamespace(id=986380678)
    owner_msg.answer = AsyncMock()
    await usage_handler(owner_msg, bot, settings, services.jobs, services.workspaces)
    owner_msg.answer.assert_called_once()
    text = owner_msg.answer.call_args[0][0]
    markup = owner_msg.answer.call_args[1]["reply_markup"]

    assert "Kayi PDF — Host &amp; Service Metrics" in text
    assert "owner@example.com" in text
    assert "kayi-pdf-bot" in text
    assert "ZenkaiRPGBot/kayi-pdf-bot" in text
    assert "750h" in text
    assert "Process CPU:" in text
    assert "Process RAM:" in text
    assert "Temp Storage:" in text
    assert "Active Jobs:" in text
    assert "Queued Jobs:" in text
    payload = json.loads(markup.model_dump_json(exclude_none=True))
    assert payload["inline_keyboard"][0][0]["callback_data"] == "usage:refresh"
    assert payload["inline_keyboard"][0][0]["style"] == "primary"


@pytest.mark.asyncio
async def test_usage_refresh_callback_owner_and_denied(tmp_path) -> None:
    settings = _settings(temp_root=tmp_path / "pdf")
    services = build_services(settings)
    bot = MagicMock()
    bot.get_me = AsyncMock()

    bad_cb = MagicMock()
    bad_cb.from_user = SimpleNamespace(id=123)
    bad_cb.message = MagicMock()
    bad_cb.message.edit_text = AsyncMock()
    bad_cb.answer = AsyncMock()
    await usage_refresh_callback(bad_cb, bot, settings, services.jobs, services.workspaces)
    bad_cb.message.edit_text.assert_not_called()
    bad_cb.answer.assert_called_once_with(
        "Access restricted to bot owner.", show_alert=True
    )

    ok_cb = MagicMock()
    ok_cb.from_user = SimpleNamespace(id=986380678)
    ok_cb.message = MagicMock()
    ok_cb.message.edit_text = AsyncMock()
    ok_cb.answer = AsyncMock()
    await usage_refresh_callback(ok_cb, bot, settings, services.jobs, services.workspaces)
    ok_cb.message.edit_text.assert_called_once()
    ok_cb.answer.assert_called_once_with("Metrics refreshed", show_alert=False)


@pytest.mark.asyncio
async def test_register_bot_commands_separates_public_and_owner() -> None:
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()

    await register_bot_commands(bot, _settings(owner_id=986380678))
    assert bot.set_my_commands.call_count == 2

    public_call, owner_call = bot.set_my_commands.call_args_list
    public_cmds = [c.command for c in public_call[0][0]]
    assert public_cmds == ["start", "help", "ping", "cancel"]
    assert "usage" not in public_cmds
    assert isinstance(public_call[1]["scope"], BotCommandScopeDefault)

    owner_cmds = [c.command for c in owner_call[0][0]]
    assert owner_cmds == ["start", "help", "ping", "cancel", "usage"]
    assert isinstance(owner_call[1]["scope"], BotCommandScopeChat)
    assert owner_call[1]["scope"].chat_id == 986380678

    bot.set_my_commands.reset_mock()
    await register_bot_commands(bot, _settings(owner_id=0))
    assert bot.set_my_commands.call_count == 1
    assert isinstance(bot.set_my_commands.call_args[1]["scope"], BotCommandScopeDefault)
