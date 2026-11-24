"""Last-resort callback handler for stale or unknown buttons."""

from aiogram import Router
from aiogram.types import CallbackQuery

from app.texts import expired_text
from app.utils.telegram import safe_answer_callback

router = Router(name="fallback")


@router.callback_query()
async def stale_callback_fallback(callback: CallbackQuery) -> None:
    await safe_answer_callback(callback, expired_text(), show_alert=True)
