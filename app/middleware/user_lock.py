"""Serialises handlers per user while allowing /cancel to break through."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


def _is_cancel(event: TelegramObject) -> bool:
    return isinstance(event, Message) and bool(
        event.text and event.text.split(maxsplit=1)[0].split("@")[0].lower() == "/cancel"
    )


def _is_verify(event: TelegramObject) -> bool:
    return isinstance(event, CallbackQuery) and (event.data or "") == "fsub:verify"


class _LockEntry:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


class UserLockManager:
    """Keyed locks with reference counts so idle users do not leak memory."""

    def __init__(self) -> None:
        self._entries: dict[int, _LockEntry] = {}
        self._guard = asyncio.Lock()

    async def _get(self, user_id: int) -> _LockEntry:
        async with self._guard:
            entry = self._entries.get(user_id)
            if entry is None:
                entry = _LockEntry()
                self._entries[user_id] = entry
            entry.refs += 1
            return entry

    async def _put(self, user_id: int, entry: _LockEntry) -> None:
        async with self._guard:
            entry.refs -= 1
            if entry.refs <= 0 and self._entries.get(user_id) is entry:
                self._entries.pop(user_id, None)

    @asynccontextmanager
    async def acquire(self, user_id: int):
        entry = await self._get(user_id)
        try:
            async with entry.lock:
                yield
        finally:
            await self._put(user_id, entry)

    async def try_acquire(
        self, user_id: int, timeout: float = 1.0
    ) -> _LockEntry | None:
        entry = await self._get(user_id)
        try:
            await asyncio.wait_for(entry.lock.acquire(), timeout=timeout)
            return entry
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await self._put(user_id, entry)
            raise
        except Exception:
            await self._put(user_id, entry)
            raise

    async def release(self, user_id: int, entry: _LockEntry) -> None:
        if entry.lock.locked():
            entry.lock.release()
        await self._put(user_id, entry)

    @property
    def tracked_users(self) -> int:
        return len(self._entries)


class UserSerializationMiddleware(BaseMiddleware):
    """Holds a per-user lock for every handler except /cancel and verify.

    Long PDF work runs in the handler while this lock is held.  A second
    callback from the same user gets an immediate answer instead of waiting
    behind the CPU job; a second upload waits its turn so FSM data cannot be
    modified concurrently.
    """

    def __init__(self, locks: UserLockManager) -> None:
        self.locks = locks

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if _is_cancel(event) or _is_verify(event):
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            try:
                entry = await self.locks.try_acquire(user.id, timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    await event.answer(
                        "A task is in progress. Wait for it to finish or use /cancel.",
                        show_alert=True,
                    )
                except Exception:
                    pass
                return None
            if entry is None:
                return None
            try:
                return await handler(event, data)
            finally:
                await self.locks.release(user.id, entry)
        async with self.locks.acquire(user.id):
            return await handler(event, data)
