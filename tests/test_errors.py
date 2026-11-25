"""Global update error replies and contextual, secret-free logging."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.types import ErrorEvent

from app.bot import _error_context, handle_error
from app.services.errors import FileValidationError


class FakeBot:
    def __init__(self, *, failing: bool = False) -> None:
        self.messages: list[dict] = []
        self.failing = failing

    async def send_message(self, chat_id, text, **kwargs):
        if self.failing:
            raise RuntimeError("telegram down")
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})


class FakeCallback:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=9)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=100))
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))


def message_event(exception: Exception) -> ErrorEvent:
    message = SimpleNamespace(
        chat=SimpleNamespace(id=100),
        from_user=SimpleNamespace(id=7),
    )
    update = SimpleNamespace(callback_query=None, message=message)
    return ErrorEvent.model_construct(update=update, exception=exception)


def callback_event(exception: Exception) -> tuple[ErrorEvent, FakeCallback]:
    callback = FakeCallback()
    update = SimpleNamespace(callback_query=callback, message=None)
    return ErrorEvent.model_construct(update=update, exception=exception), callback


def test_error_context_reads_message_and_callback_updates() -> None:
    assert _error_context(message_event(RuntimeError())) == ("message", 7, 100)
    event, _ = callback_event(RuntimeError())
    assert _error_context(event) == ("callback_query", 9, 100)
    other = ErrorEvent.model_construct(
        update=SimpleNamespace(callback_query=None, message=None),
        exception=RuntimeError(),
    )
    assert _error_context(other) == ("other", None, None)


@pytest.mark.asyncio
async def test_expected_error_uses_the_user_safe_message(caplog) -> None:
    bot = FakeBot()
    event = message_event(FileValidationError())

    with caplog.at_level("ERROR", logger="app.bot"):
        await handle_error(event, bot)

    assert bot.messages == [
        {"chat_id": 100, "text": FileValidationError.user_message}
    ]
    assert "message update error" in caplog.text
    assert "user=7" in caplog.text
    assert "chat=100" in caplog.text
    assert "FileValidationError" in caplog.text


@pytest.mark.asyncio
async def test_unexpected_error_uses_the_generic_message(caplog) -> None:
    bot = FakeBot()
    event = message_event(RuntimeError("secret detail"))

    with caplog.at_level("ERROR", logger="app.bot"):
        await handle_error(event, bot)

    assert bot.messages == [
        {
            "chat_id": 100,
            "text": "Something went wrong. Please try again or use /cancel.",
        }
    ]
    assert "RuntimeError" in caplog.text
    assert "user=7" in caplog.text


@pytest.mark.asyncio
async def test_callback_error_is_answered_with_an_alert() -> None:
    bot = FakeBot()
    event, callback = callback_event(FileValidationError())

    await handle_error(event, bot)

    assert callback.answers == [(FileValidationError.user_message[:190], True)]
    assert bot.messages == []


@pytest.mark.asyncio
async def test_error_reply_failure_is_swallowed() -> None:
    bot = FakeBot(failing=True)
    await handle_error(message_event(RuntimeError("boom")), bot)
    assert bot.messages == []


@pytest.mark.asyncio
async def test_unknown_update_kind_is_logged_without_a_reply(caplog) -> None:
    bot = FakeBot()
    event = ErrorEvent.model_construct(
        update=SimpleNamespace(callback_query=None, message=None),
        exception=RuntimeError("boom"),
    )

    with caplog.at_level("ERROR", logger="app.bot"):
        await handle_error(event, bot)

    assert bot.messages == []
    assert "other update error" in caplog.text
