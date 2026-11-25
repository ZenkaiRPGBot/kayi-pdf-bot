"""Forced-subscription checks, positive caching, and pending destinations."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import ChatMemberRestricted

from app.utils.logging import get_logger

log = get_logger(__name__)


class MembershipStatus(str, Enum):
    MEMBER = "member"
    NOT_MEMBER = "not_member"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MembershipResult:
    status: MembershipStatus
    detail: str = ""

    @property
    def is_member(self) -> bool:
        return self.status is MembershipStatus.MEMBER


class SubscriptionService:
    """Positive membership cache with a strict 45-second default TTL."""

    def __init__(
        self,
        channel: str,
        *,
        cache_ttl: float = 45.0,
        cache_max: int = 10_000,
        clock=time.monotonic,
    ) -> None:
        self.channel = channel
        self.cache_ttl = cache_ttl
        self.cache_max = cache_max
        self._clock = clock
        self._cache: dict[int, float] = {}

    def _cached(self, user_id: int) -> bool:
        expires = self._cache.get(user_id)
        if expires is None:
            return False
        if expires <= self._clock():
            self._cache.pop(user_id, None)
            return False
        return True

    def _store(self, user_id: int) -> None:
        now = self._clock()
        for key, expiry in list(self._cache.items()):
            if expiry <= now:
                self._cache.pop(key, None)
        self._cache[user_id] = now + self.cache_ttl
        if len(self._cache) > self.cache_max:
            # Expiring entries first, then the nearest expiry.
            for key, _ in sorted(self._cache.items(), key=lambda item: item[1])[
                : len(self._cache) - self.cache_max
            ]:
                self._cache.pop(key, None)

    def invalidate(self, user_id: int) -> None:
        self._cache.pop(user_id, None)

    def clear(self) -> None:
        self._cache.clear()

    @property
    def cached_count(self) -> int:
        return len(self._cache)

    async def check(self, bot, user_id: int, *, force: bool = False) -> MembershipResult:
        if not force and self._cached(user_id):
            return MembershipResult(MembershipStatus.MEMBER, "cache")
        try:
            member = await bot.get_chat_member(
                chat_id=self.channel,
                user_id=user_id,
            )
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if any(
                marker in text
                for marker in (
                    "user not found",
                    "participant_id_invalid",
                    "user_not_participant",
                )
            ):
                self.invalidate(user_id)
                log.info(
                    "FSUB_DENY user_id=%s status=not_found channel=%s",
                    user_id,
                    self.channel,
                )
                return MembershipResult(MembershipStatus.NOT_MEMBER, "not_found")
            log.warning(
                "FSUB_TG_ERROR user_id=%s channel=%s error=%s",
                user_id,
                self.channel,
                type(exc).__name__,
            )
            return MembershipResult(MembershipStatus.UNAVAILABLE, "bad_request")
        except (
            TelegramRetryAfter,
            TelegramNetworkError,
            TelegramServerError,
            TelegramUnauthorizedError,
            TimeoutError,
        ) as exc:
            if isinstance(exc, TimeoutError):
                log.warning(
                    "FSUB_TIMEOUT user_id=%s channel=%s timeout=%ss",
                    user_id,
                    self.channel,
                    getattr(exc, "timeout", "unknown"),
                )
            else:
                log.warning(
                    "FSUB_TG_ERROR user_id=%s channel=%s error=%s",
                    user_id,
                    self.channel,
                    type(exc).__name__,
                )
            return MembershipResult(MembershipStatus.UNAVAILABLE, "telegram_error")
        except Exception as exc:  # fail closed, but keep the bot alive
            log.error(
                "FSUB_UNEXPECTED_ERROR user_id=%s channel=%s error=%s",
                user_id,
                self.channel,
                type(exc).__name__,
            )
            return MembershipResult(MembershipStatus.UNAVAILABLE, "unexpected")

        raw_status = getattr(member, "status", "")
        status = getattr(raw_status, "value", raw_status)
        status = str(status).lower()
        if status in {"creator", "administrator", "member"}:
            self._store(user_id)
            log.info(
                "FSUB_ALLOW user_id=%s status=%s channel=%s",
                user_id,
                status,
                self.channel,
            )
            return MembershipResult(MembershipStatus.MEMBER, status)
        if status == "restricted":
            # aiogram exposes is_member on restricted members.  A restricted
            # user who is not a member must not pass the gate.
            if isinstance(member, ChatMemberRestricted):
                is_member = bool(member.is_member)
            else:
                is_member = bool(getattr(member, "is_member", False))
            if is_member:
                self._store(user_id)
                log.info(
                    "FSUB_ALLOW user_id=%s status=restricted_member channel=%s",
                    user_id,
                    self.channel,
                )
                return MembershipResult(MembershipStatus.MEMBER, "restricted_member")
            self.invalidate(user_id)
            log.info(
                "FSUB_DENY user_id=%s status=restricted channel=%s",
                user_id,
                self.channel,
            )
            return MembershipResult(MembershipStatus.NOT_MEMBER, "restricted")
        if status in {"left", "kicked"}:
            self.invalidate(user_id)
            log.info(
                "FSUB_DENY user_id=%s status=%s channel=%s",
                user_id,
                status,
                self.channel,
            )
            return MembershipResult(MembershipStatus.NOT_MEMBER, status)
        log.warning(
            "FSUB_UNKNOWN_STATUS user_id=%s channel=%s status=%s",
            user_id,
            self.channel,
            status or "empty",
        )
        return MembershipResult(MembershipStatus.UNAVAILABLE, "unknown_status")


@dataclass(frozen=True, slots=True)
class PendingAction:
    action: str
    created_at: float


class PendingActionStore:
    """In-memory destinations used to restore the user's intent after FSub."""

    def __init__(
        self,
        *,
        ttl: float = 900.0,
        max_entries: int = 10_000,
        clock=time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._items: dict[int, PendingAction] = {}

    def set(self, user_id: int, action: str) -> None:
        self._prune()
        self._items[user_id] = PendingAction(action=action, created_at=self._clock())
        if len(self._items) > self.max_entries:
            oldest = sorted(self._items.items(), key=lambda item: item[1].created_at)
            for key, _ in oldest[: len(self._items) - self.max_entries]:
                self._items.pop(key, None)

    def get(self, user_id: int) -> str | None:
        self._prune()
        item = self._items.get(user_id)
        return item.action if item is not None else None

    def pop(self, user_id: int) -> str | None:
        self._prune()
        item = self._items.pop(user_id, None)
        return item.action if item is not None else None

    def clear(self) -> None:
        self._items.clear()

    @property
    def count(self) -> int:
        return len(self._items)

    def _prune(self) -> None:
        now = self._clock()
        for key, item in list(self._items.items()):
            if now - item.created_at > self.ttl:
                self._items.pop(key, None)


def callback_destination(callback_data: str | None) -> str:
    """Map a protected callback to the best destination we can restore."""

    data = (callback_data or "").strip().lower()
    if data == "menu:merge":
        return "merge"
    if data == "menu:split":
        return "split"
    if data == "menu:extract":
        return "extract"
    if data == "menu:rotate":
        return "rotate"
    if data == "menu:images":
        return "images"
    if data == "help:main" or data == "menu:help":
        return "help"
    if data.startswith("help:topic:"):
        topic = data[len("help:topic:") :]
        if topic in {"start", "tools", "images", "limits"}:
            return f"help_topic:{topic}"
    return "welcome"


def message_destination(event) -> str:
    text = (getattr(event, "text", None) or "").strip().lower()
    if text.startswith("/start"):
        # /start itself is exempt; this branch keeps the mapping explicit.
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1] == "help":
            return "help"
        return "welcome"
    if text.startswith("/help"):
        return "help"
    if getattr(event, "document", None) is not None:
        return "resend"
    if getattr(event, "photo", None) is not None:
        return "resend"
    return "welcome"
