"""Outer middleware enforcing the Kayi Bots channel subscription."""

from __future__ import annotations

import re

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import Settings
from app.keyboards.main import subscribe_keyboard
from app.services.fsub import (
    MembershipStatus,
    PendingActionStore,
    SubscriptionService,
    callback_destination,
    message_destination,
)
from app.texts import fsub_text
from app.utils.logging import get_logger

log = get_logger(__name__)

_COMMAND_RE = re.compile(r"^/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?$")


def _command(event: TelegramObject) -> str:
    if not isinstance(event, Message) or not event.text:
        return ""
    first = event.text.strip().split(maxsplit=1)[0]
    if not _COMMAND_RE.match(first):
        return ""
    return first.split("@", 1)[0].lower()


def is_exempt(event: TelegramObject) -> bool:
    if isinstance(event, Message):
        return _command(event) in {"/start", "/cancel"}
    if isinstance(event, CallbackQuery):
        return (event.data or "") == "fsub:verify"
    return False


class SubscriptionMiddleware(BaseMiddleware):
    def __init__(
        self,
        subscription: SubscriptionService,
        pending: PendingActionStore,
        settings: Settings,
    ) -> None:
        self.subscription = subscription
        self.pending = pending
        self.settings = settings

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if is_exempt(event):
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        result = await self.subscription.check(data["bot"], user.id)
        if result.status is MembershipStatus.MEMBER:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            self.pending.set(user.id, callback_destination(event.data))
        else:
            self.pending.set(user.id, message_destination(event))
        await self._send_prompt(event, data["bot"], result.status)
        return None

    async def _send_prompt(self, event: TelegramObject, bot, status: MembershipStatus) -> None:
        text = fsub_text(status)
        markup = subscribe_keyboard(self.settings)
        try:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except Exception:
                    pass
                chat_id = (
                    event.message.chat.id
                    if event.message is not None
                    else event.from_user.id
                )
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
            elif isinstance(event, Message):
                await event.answer(text, reply_markup=markup)
        except Exception as exc:
            log.warning("Could not send subscription prompt: %s", type(exc).__name__)
