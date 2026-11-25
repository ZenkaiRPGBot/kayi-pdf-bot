"""Forced-subscription and per-user serialization middleware."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from app.config import Settings
from app.middleware.fsub import SubscriptionMiddleware, is_exempt
from app.middleware.user_lock import UserLockManager, UserSerializationMiddleware
from app.services.fsub import (
    MembershipResult,
    MembershipStatus,
    PendingActionStore,
)


def make_user(user_id: int = 1) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def make_message(text: str, user_id: int = 1) -> Message:
    return Message.model_construct(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=make_user(user_id),
        text=text,
    )


def make_callback(data: str, user_id: int = 1) -> CallbackQuery:
    return CallbackQuery.model_construct(
        id="cb-1",
        from_user=make_user(user_id),
        chat_instance="chat-1",
        data=data,
        message=None,
    )


class SpySubscription:
    def __init__(self, status: MembershipStatus = MembershipStatus.MEMBER) -> None:
        self.status = status
        self.calls: list[int] = []

    async def check(self, bot, user_id: int, *, force: bool = False):
        self.calls.append(user_id)
        return MembershipResult(self.status)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start", True),
        ("/start help", True),
        ("/start@KayiPDFBot", True),
        ("/cancel", True),
        ("/cancel@KayiPDFBot now", True),
        ("/usage", False),
        ("/render", False),
        ("/status", False),
        ("/stats", False),
        ("/restart", False),
        ("/restart@KayiPDFBot maintenance", False),
        ("/help", False),
        ("/privacy", False),
        ("hello", False),
        ("", False),
    ],
)
def test_message_exemptions(text: str, expected: bool) -> None:
    assert is_exempt(make_message(text)) is expected


@pytest.mark.parametrize(
    "text",
    ["/usage", "/render", "/status", "/stats", "/restart", "/restart maintenance"],
)
def test_owner_commands_bypass_the_subscription_gate_for_the_owner(text: str) -> None:
    assert is_exempt(make_message(text, user_id=7), owner_id=7) is True
    assert is_exempt(make_message(text, user_id=8), owner_id=7) is False


def test_callback_exemption() -> None:
    assert is_exempt(make_callback("fsub:verify")) is True
    assert is_exempt(make_callback("menu:merge")) is False


def test_non_message_events_are_not_exempt() -> None:
    assert is_exempt(object()) is False


@pytest.mark.asyncio
async def test_member_passes_through() -> None:
    subscription = SpySubscription(MembershipStatus.MEMBER)
    middleware = SubscriptionMiddleware(
        subscription,  # type: ignore[arg-type]
        PendingActionStore(),
        Settings(),
    )
    called = False

    async def handler(event, data):
        nonlocal called
        called = True
        return "ok"

    result = await middleware(handler, make_message("hello"), {"bot": object()})
    assert result == "ok"
    assert called is True
    assert subscription.calls == [1]


@pytest.mark.asyncio
async def test_non_member_is_blocked_and_prompted(monkeypatch) -> None:
    subscription = SpySubscription(MembershipStatus.NOT_MEMBER)
    pending = PendingActionStore()
    middleware = SubscriptionMiddleware(
        subscription,  # type: ignore[arg-type]
        pending,
        Settings(),
    )
    prompts: list[tuple[str, object]] = []

    async def fake_answer(self, text, reply_markup=None, **kwargs):
        prompts.append((text, reply_markup))
        return self

    monkeypatch.setattr(Message, "answer", fake_answer)
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    result = await middleware(handler, make_message("hello"), {"bot": object()})
    assert result is None
    assert called is False
    assert pending.get(1) == "welcome"
    assert prompts and "Join Kayi Bots to Continue" in prompts[0][0]
    assert prompts[0][1] is not None


@pytest.mark.asyncio
async def test_unavailable_check_fails_closed(monkeypatch) -> None:
    subscription = SpySubscription(MembershipStatus.UNAVAILABLE)
    pending = PendingActionStore()
    middleware = SubscriptionMiddleware(
        subscription,  # type: ignore[arg-type]
        pending,
        Settings(),
    )
    prompts: list[str] = []

    async def fake_answer(self, text, reply_markup=None, **kwargs):
        prompts.append(text)
        return self

    monkeypatch.setattr(Message, "answer", fake_answer)

    async def handler(event, data):
        raise AssertionError("handler must not run")

    await middleware(handler, make_message("hello"), {"bot": object()})
    assert prompts and "Membership Check Unavailable" in prompts[0]


@pytest.mark.asyncio
async def test_start_bypasses_subscription_check(monkeypatch) -> None:
    subscription = SpySubscription(MembershipStatus.NOT_MEMBER)
    middleware = SubscriptionMiddleware(
        subscription,  # type: ignore[arg-type]
        PendingActionStore(),
        Settings(),
    )
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    await middleware(handler, make_message("/start"), {"bot": object()})
    assert called is True
    assert subscription.calls == []


@pytest.mark.asyncio
async def test_owner_telemetry_bypasses_but_non_owner_is_prompted(monkeypatch) -> None:
    subscription = SpySubscription(MembershipStatus.NOT_MEMBER)
    middleware = SubscriptionMiddleware(
        subscription,  # type: ignore[arg-type]
        PendingActionStore(),
        Settings(owner_id=7),
    )
    calls: list[int] = []
    prompts: list[str] = []

    async def handler(event, data):
        calls.append(event.from_user.id)

    async def fake_answer(self, text, reply_markup=None, **kwargs):
        prompts.append(text)
        return self

    monkeypatch.setattr(Message, "answer", fake_answer)

    await middleware(handler, make_message("/usage", user_id=7), {"bot": object()})
    assert calls == [7]
    assert subscription.calls == []
    assert prompts == []

    await middleware(handler, make_message("/usage", user_id=8), {"bot": object()})
    assert calls == [7]
    assert subscription.calls == [8]
    assert prompts and "Join Kayi Bots to Continue" in prompts[0]


@pytest.mark.asyncio
async def test_blocked_callback_sets_pending_and_answers(monkeypatch) -> None:
    subscription = SpySubscription(MembershipStatus.NOT_MEMBER)
    pending = PendingActionStore()
    middleware = SubscriptionMiddleware(
        subscription,  # type: ignore[arg-type]
        pending,
        Settings(),
    )
    sent: list[dict] = []
    answers: list[tuple] = []

    async def fake_callback_answer(self, text=None, show_alert=False, **kwargs):
        answers.append((text, show_alert))

    async def fake_send_message(self, *args, **kwargs):
        sent.append(kwargs)
        return None

    class FakeBot:
        send_message = fake_send_message

    monkeypatch.setattr(CallbackQuery, "answer", fake_callback_answer)
    await middleware(
        lambda event, data: None,
        make_callback("menu:merge"),
        {"bot": FakeBot()},
    )
    assert pending.get(1) == "merge"
    assert answers == [(None, False)]
    assert sent and "Join Kayi Bots to Continue" in sent[0]["text"]


@pytest.mark.asyncio
async def test_events_without_a_user_pass_through() -> None:
    middleware = SubscriptionMiddleware(
        SpySubscription(),  # type: ignore[arg-type]
        PendingActionStore(),
        Settings(),
    )
    event = Message.model_construct(message_id=1, text="hello", from_user=None)
    called = False

    async def handler(inner_event, data):
        nonlocal called
        called = True
        return "ok"

    assert await middleware(handler, event, {"bot": object()}) == "ok"
    assert called is True


@pytest.mark.asyncio
async def test_user_lock_serializes_handlers() -> None:
    locks = UserLockManager()
    middleware = UserSerializationMiddleware(locks)
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def first(event, data):
        order.append("first-start")
        started.set()
        await release.wait()
        order.append("first-end")

    async def second(event, data):
        order.append("second")

    task = asyncio.create_task(
        middleware(first, make_message("hello", user_id=5), {})
    )
    await started.wait()
    second_task = asyncio.create_task(
        middleware(second, make_message("hello", user_id=5), {})
    )
    await asyncio.sleep(0.01)
    assert order == ["first-start"]
    release.set()
    await asyncio.gather(task, second_task)
    assert order == ["first-start", "first-end", "second"]
    assert locks.tracked_users == 0


@pytest.mark.asyncio
async def test_cancel_bypasses_user_lock() -> None:
    locks = UserLockManager()
    middleware = UserSerializationMiddleware(locks)
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    async with locks.acquire(1):
        await middleware(handler, make_message("/cancel"), {})
    assert called is True


@pytest.mark.asyncio
async def test_verify_bypasses_user_lock() -> None:
    locks = UserLockManager()
    middleware = UserSerializationMiddleware(locks)
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    async with locks.acquire(1):
        await middleware(handler, make_callback("fsub:verify"), {})
    assert called is True


@pytest.mark.asyncio
async def test_busy_callback_is_answered_immediately(monkeypatch) -> None:
    locks = UserLockManager()
    middleware = UserSerializationMiddleware(locks, )
    answers: list[str] = []

    async def fake_answer(self, text=None, show_alert=False, **kwargs):
        answers.append(text or "")

    monkeypatch.setattr(CallbackQuery, "answer", fake_answer)
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    async with locks.acquire(1):
        await middleware(handler, make_callback("menu:merge"), {})
    assert called is False
    assert answers and "task is in progress" in answers[0]


@pytest.mark.asyncio
async def test_user_lock_manager_reference_counting() -> None:
    locks = UserLockManager()
    async with locks.acquire(1):
        assert locks.tracked_users == 1
    assert locks.tracked_users == 0
    entry = await locks.try_acquire(2, timeout=0.1)
    assert entry is not None
    assert locks.tracked_users == 1
    await locks.release(2, entry)
    assert locks.tracked_users == 0

    held = await locks.try_acquire(3, timeout=0.1)
    assert held is not None
    with pytest.raises(asyncio.TimeoutError):
        await locks.try_acquire(3, timeout=0.01)
    await locks.release(3, held)
    assert locks.tracked_users == 0
