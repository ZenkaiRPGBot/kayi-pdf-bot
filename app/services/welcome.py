"""Cinematic welcome with a safe text fallback."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.keyboards.main import main_menu_keyboard
from app.texts import WELCOME_TEXT
from app.utils.logging import get_logger

log = get_logger(__name__)


async def send_welcome(
    bot: Bot,
    chat_id: int,
    settings: Settings,
    *,
    use_video: bool = True,
) -> None:
    """Send the configured welcome video, falling back to a text menu.

    A ``file_id`` is only usable by a bot that has access to the media, so a
    failed video send must not leave the user without a menu.
    """

    if use_video and settings.welcome_video_file_id:
        try:
            await bot.send_video(
                chat_id=chat_id,
                video=settings.welcome_video_file_id,
                caption=WELCOME_TEXT,
                reply_markup=main_menu_keyboard(settings),
            )
            return
        except TelegramAPIError as exc:
            log.warning(
                "Welcome video could not be sent (%s); using text fallback",
                type(exc).__name__,
            )
        except Exception as exc:
            log.warning(
                "Welcome video failed unexpectedly (%s); using text fallback",
                type(exc).__name__,
            )
    await bot.send_message(
        chat_id=chat_id,
        text=WELCOME_TEXT,
        reply_markup=main_menu_keyboard(settings),
    )
