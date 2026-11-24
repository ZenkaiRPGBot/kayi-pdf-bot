"""Cooperative cancellation shared by handlers and CPU worker threads."""

from __future__ import annotations

import asyncio
import threading

from app.services.errors import JobCancelled


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._async_event: asyncio.Event | None = None

    def _get_async_event(self) -> asyncio.Event:
        if self._async_event is None:
            self._async_event = asyncio.Event()
        return self._async_event

    def cancel(self) -> None:
        self._event.set()
        event = self._async_event
        if event is not None:
            event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise JobCancelled()

    async def wait(self) -> None:
        await self._get_async_event().wait()
