"""Forced-subscription checks, caching, pending actions, and mappings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import ChatMemberRestricted

from app.services.fsub import (
    MembershipStatus,
    PendingActionStore,
    SubscriptionService,
    callback_destination,
    message_destination,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBot:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["creator", "administrator", "member"])
async def test_regular_member_statuses(status: str) -> None:
    service = SubscriptionService("@KayiBots")
    result = await service.check(FakeBot(SimpleNamespace(status=status)), 1)
    assert result.status is MembershipStatus.MEMBER
    assert result.is_member is True


@pytest.mark.asyncio
async def test_status_enum_values_are_supported() -> None:
    from aiogram.enums import ChatMemberStatus

    service = SubscriptionService("@KayiBots")
    result = await service.check(
        FakeBot(SimpleNamespace(status=ChatMemberStatus.MEMBER)), 1
    )
    assert result.status is MembershipStatus.MEMBER


@pytest.mark.asyncio
@pytest.mark.parametrize("is_member", [True, False])
async def test_restricted_membership_respects_is_member(is_member: bool) -> None:
    service = SubscriptionService("@KayiBots")
    member = ChatMemberRestricted.model_construct(
        status="restricted", is_member=is_member
    )
    result = await service.check(FakeBot(member), 1)
    if is_member:
        assert result.status is MembershipStatus.MEMBER
        assert result.detail == "restricted_member"
    else:
        assert result.status is MembershipStatus.NOT_MEMBER


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["left", "kicked"])
async def test_non_member_statuses(status: str) -> None:
    service = SubscriptionService("@KayiBots")
    result = await service.check(FakeBot(SimpleNamespace(status=status)), 1)
    assert result.status is MembershipStatus.NOT_MEMBER


@pytest.mark.asyncio
async def test_user_not_found_is_not_a_member() -> None:
    error = TelegramBadRequest(method=None, message="Bad Request: user not found")
    service = SubscriptionService("@KayiBots")
    result = await service.check(FakeBot(error=error), 1)
    assert result.status is MembershipStatus.NOT_MEMBER


@pytest.mark.asyncio
async def test_other_bad_request_fails_closed() -> None:
    error = TelegramBadRequest(method=None, message="Bad Request: chat not found")
    service = SubscriptionService("@KayiBots")
    result = await service.check(FakeBot(error=error), 1)
    assert result.status is MembershipStatus.UNAVAILABLE
    assert service.cached_count == 0


@pytest.mark.asyncio
async def test_network_failure_fails_closed() -> None:
    error = TelegramNetworkError(method=None, message="network down")
    service = SubscriptionService("@KayiBots")
    result = await service.check(FakeBot(error=error), 1)
    assert result.status is MembershipStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_unexpected_failure_fails_closed() -> None:
    service = SubscriptionService("@KayiBots")
    result = await service.check(FakeBot(error=RuntimeError("surprise")), 1)
    assert result.status is MembershipStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_positive_cache_uses_monotonic_ttl() -> None:
    clock = FakeClock()
    service = SubscriptionService("@KayiBots", cache_ttl=45.0, clock=clock)
    bot = FakeBot(SimpleNamespace(status="member"))
    assert (await service.check(bot, 1)).detail == "member"
    assert (await service.check(bot, 1)).detail == "cache"
    assert len(bot.calls) == 1
    clock.advance(44.9)
    await service.check(bot, 1)
    assert len(bot.calls) == 1
    clock.advance(0.2)
    await service.check(bot, 1)
    assert len(bot.calls) == 2
    await service.check(bot, 1, force=True)
    assert len(bot.calls) == 3


@pytest.mark.asyncio
async def test_failed_check_is_not_cached() -> None:
    clock = FakeClock()
    service = SubscriptionService("@KayiBots", cache_ttl=45.0, clock=clock)
    bot = FakeBot(error=TelegramNetworkError(method=None, message="down"))
    await service.check(bot, 1)
    await service.check(bot, 1)
    assert len(bot.calls) == 2
    assert service.cached_count == 0


@pytest.mark.asyncio
async def test_cache_capacity_and_invalidation() -> None:
    clock = FakeClock()
    service = SubscriptionService("@KayiBots", cache_ttl=100.0, cache_max=2, clock=clock)
    bot = FakeBot(SimpleNamespace(status="member"))
    for user_id in (1, 2, 3):
        await service.check(bot, user_id)
        clock.advance(1)
    assert service.cached_count == 2
    service.invalidate(2)
    assert service.cached_count == 1
    service.clear()
    assert service.cached_count == 0


def test_pending_action_store_ttl_and_capacity() -> None:
    clock = FakeClock()
    store = PendingActionStore(ttl=10.0, max_entries=2, clock=clock)
    store.set(1, "merge")
    store.set(2, "split")
    assert store.get(1) == "merge"
    assert store.pop(1) == "merge"
    assert store.pop(1) is None
    clock.advance(6)
    store.set(3, "rotate")
    # Entry 2 is still alive (age 6), entry 3 is added; entry 2 then expires.
    assert store.count == 2
    clock.advance(5)
    assert store.get(2) is None
    assert store.get(3) == "rotate"


def test_pending_action_capacity_evicts_oldest() -> None:
    clock = FakeClock()
    store = PendingActionStore(ttl=100.0, max_entries=2, clock=clock)
    store.set(1, "a")
    clock.advance(1)
    store.set(2, "b")
    clock.advance(1)
    store.set(3, "c")
    assert store.count == 2
    assert store.get(1) is None
    assert store.get(3) == "c"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("menu:merge", "merge"),
        ("menu:split", "split"),
        ("menu:extract", "extract"),
        ("menu:rotate", "rotate"),
        ("menu:images", "images"),
        ("help:main", "help"),
        ("menu:help", "help"),
        ("help:topic:start", "help_topic:start"),
        ("help:topic:tools", "help_topic:tools"),
        ("help:topic:images", "help_topic:images"),
        ("help:topic:limits", "help_topic:limits"),
        ("help:topic:unknown", "welcome"),
        ("merge:up:1", "welcome"),
        (None, "welcome"),
    ],
)
def test_callback_destination(data, expected) -> None:
    assert callback_destination(data) == expected


def test_message_destination() -> None:
    assert message_destination(SimpleNamespace(text="/start help")) == "help"
    assert message_destination(SimpleNamespace(text="/start")) == "welcome"
    assert message_destination(SimpleNamespace(text="/help")) == "help"
    assert message_destination(SimpleNamespace(text="hello")) == "welcome"
    assert message_destination(
        SimpleNamespace(text=None, document=object())
    ) == "resend"
    assert message_destination(
        SimpleNamespace(text=None, photo=[object()])
    ) == "resend"
