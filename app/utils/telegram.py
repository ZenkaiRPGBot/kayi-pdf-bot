"""Small Telegram UI helpers shared by handlers."""

from __future__ import annotations

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from app.utils.logging import get_logger

log = get_logger(__name__)


async def safe_edit_text(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        log.debug("Could not edit message: %s", type(exc).__name__)
    except TelegramAPIError as exc:
        log.debug("Could not edit message: %s", type(exc).__name__)
    try:
        await message.answer(text, reply_markup=reply_markup)
        return False
    except TelegramAPIError:
        return False


async def safe_delete(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def safe_answer_callback(callback, text: str | None = None, *, show_alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramAPIError:
        pass
