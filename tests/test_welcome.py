"""Welcome video behaviour and safe text fallback."""

from __future__ import annotations

from dataclasses import replace

import pytest
from aiogram.exceptions import TelegramNetworkError

from app.config import Settings
from app.services.welcome import send_welcome
from app.texts import WELCOME_TEXT


class FakeBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.videos: list[dict] = []
        self.messages: list[dict] = []

    async def send_video(self, **kwargs):
        self.videos.append(kwargs)
        if self.error is not None:
            raise self.error
        return "video"

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return "message"


@pytest.mark.asyncio
async def test_sends_video_when_configured(settings) -> None:
    settings = replace(settings, welcome_video_file_id="video-file-id")
    bot = FakeBot()
    await send_welcome(bot, 1, settings, use_video=True)
    assert len(bot.videos) == 1
    assert bot.videos[0]["video"] == "video-file-id"
    assert bot.messages == []


@pytest.mark.asyncio
async def test_text_only_when_video_is_not_configured(settings) -> None:
    bot = FakeBot()
    await send_welcome(bot, 1, settings, use_video=True)
    assert bot.videos == []
    assert bot.messages[0]["text"] == WELCOME_TEXT


@pytest.mark.asyncio
async def test_video_disabled_for_help_entry(settings) -> None:
    settings = replace(settings, welcome_video_file_id="video-file-id")
    bot = FakeBot()
    await send_welcome(bot, 1, settings, use_video=False)
    assert bot.videos == []
    assert bot.messages[0]["text"] == WELCOME_TEXT


@pytest.mark.asyncio
async def test_telegram_error_falls_back_to_text(settings) -> None:
    settings = replace(settings, welcome_video_file_id="video-file-id")
    bot = FakeBot(TelegramNetworkError(method=None, message="down"))
    await send_welcome(bot, 1, settings)
    assert len(bot.videos) == 1
    assert len(bot.messages) == 1
    assert bot.messages[0]["text"] == WELCOME_TEXT


@pytest.mark.asyncio
async def test_unexpected_video_error_falls_back_to_text(settings) -> None:
    settings = replace(settings, welcome_video_file_id="video-file-id")
    bot = FakeBot(RuntimeError("surprise"))
    await send_welcome(bot, 1, settings)
    assert len(bot.messages) == 1
