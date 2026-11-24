"""Bounded job admission, cooperative cancellation, and CPU offloading."""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass, field
from typing import Any, Callable

from app.services.cancellation import CancelToken
from app.services.errors import (
    JobAlreadyActiveError,
    JobCancelled,
    JobCapacityError,
    ProcessingTimeout,
)
from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class JobSlot:
    manager: "JobManager"
    user_id: int
    token: CancelToken
    _released: bool = field(default=False, repr=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self.manager.release(self.user_id, self.token)


class JobManager:
    """One active CPU job, with a small bounded waiting queue.

    ``active + queued`` is checked under a lock before a job is admitted, so
    requests beyond ``max_active_jobs + max_queued_jobs`` are rejected rather
    than growing memory or disk use without bound.
    """

    def __init__(
        self,
        *,
        max_active_jobs: int = 1,
        max_queued_jobs: int = 2,
        queue_timeout: float = 120.0,
    ) -> None:
        self.max_active_jobs = max_active_jobs
        self.max_queued_jobs = max_queued_jobs
        self.queue_timeout = queue_timeout
        self._semaphore = asyncio.Semaphore(max_active_jobs)
        self._lock = asyncio.Lock()
        self._active = 0
        self._queued = 0
        self._tokens: dict[int, CancelToken] = {}

    @property
    def active(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return self._queued

    @property
    def capacity(self) -> int:
        return self.max_active_jobs + self.max_queued_jobs

    def is_active(self, user_id: int) -> bool:
        return user_id in self._tokens

    async def acquire(self, user_id: int) -> JobSlot:
        async with self._lock:
            if user_id in self._tokens:
                raise JobAlreadyActiveError()
            if self._active + self._queued >= self.capacity:
                raise JobCapacityError()
            token = CancelToken()
            self._tokens[user_id] = token
            self._queued += 1

        acquire_task = asyncio.create_task(self._semaphore.acquire())
        cancel_task = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
                {acquire_task, cancel_task},
                timeout=self.queue_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            acquired = (
                acquire_task in done
                and not acquire_task.cancelled()
                and acquire_task.exception() is None
                and acquire_task.result() is True
            )
            if cancel_task in done and token.cancelled:
                if acquired:
                    self._semaphore.release()
                async with self._lock:
                    self._queued = max(0, self._queued - 1)
                    if self._tokens.get(user_id) is token:
                        self._tokens.pop(user_id, None)
                raise JobCancelled()
            if acquired:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
                async with self._lock:
                    self._queued = max(0, self._queued - 1)
                    self._active += 1
                return JobSlot(manager=self, user_id=user_id, token=token)
            # Timed out (or the semaphore task failed unexpectedly).
            if not acquired:
                acquire_task.cancel()
                await asyncio.gather(acquire_task, return_exceptions=True)
                if _acquired(acquire_task):
                    # The acquire completed in the race window after wait().
                    self._semaphore.release()
                async with self._lock:
                    self._queued = max(0, self._queued - 1)
                    if self._tokens.get(user_id) is token:
                        self._tokens.pop(user_id, None)
                raise JobCapacityError()
        except (JobCancelled, JobCapacityError):
            raise
        except asyncio.CancelledError:
            cancel_task.cancel()
            acquire_task.cancel()
            await asyncio.gather(cancel_task, acquire_task, return_exceptions=True)
            if _acquired(acquire_task):
                self._semaphore.release()
            async with self._lock:
                self._queued = max(0, self._queued - 1)
                if self._tokens.get(user_id) is token:
                    self._tokens.pop(user_id, None)
            raise
        except Exception:
            cancel_task.cancel()
            acquire_task.cancel()
            await asyncio.gather(cancel_task, acquire_task, return_exceptions=True)
            if _acquired(acquire_task):
                self._semaphore.release()
            async with self._lock:
                self._queued = max(0, self._queued - 1)
                if self._tokens.get(user_id) is token:
                    self._tokens.pop(user_id, None)
            raise
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
            if not acquire_task.done():
                acquire_task.cancel()

    async def release(self, user_id: int, token: CancelToken) -> None:
        async with self._lock:
            if self._tokens.get(user_id) is token:
                self._tokens.pop(user_id, None)
            self._active = max(0, self._active - 1)
        self._semaphore.release()

    def cancel(self, user_id: int) -> bool:
        token = self._tokens.get(user_id)
        if token is None:
            return False
        token.cancel()
        return True

    @property
    def stats(self) -> dict[str, int]:
        return {
            "active": self._active,
            "queued": self._queued,
            "capacity": self.capacity,
        }


def _acquired(task: asyncio.Task[bool]) -> bool:
    return (
        task.done()
        and not task.cancelled()
        and task.exception() is None
        and task.result() is True
    )


def _consume_exception(future: asyncio.Future[Any]) -> None:
    try:
        future.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _cleanup_after_future(future: asyncio.Future[Any], cleanup: Callable[[], None]) -> None:
    _consume_exception(future)
    try:
        cleanup()
    except Exception:
        log.exception("Deferred workspace cleanup failed")


async def run_cpu(
    func: Callable[..., Any],
    *args: Any,
    timeout: float,
    token: CancelToken | None = None,
    cleanup_on_timeout: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Run a blocking function off the event loop with a hard wait limit.

    Python threads cannot be killed.  On timeout the worker is asked to stop
    cooperatively, and (when supplied) workspace cleanup is deferred until the
    thread actually finishes so it cannot delete a file still being written.
    """

    if token is not None:
        token.raise_if_cancelled()
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError as exc:
        if token is not None:
            token.cancel()
        if cleanup_on_timeout is not None:
            future.add_done_callback(
                functools.partial(_cleanup_after_future, cleanup=cleanup_on_timeout)
            )
            raise ProcessingTimeout(cleanup_deferred=True) from exc
        _consume_exception(future)
        raise ProcessingTimeout() from exc
    except asyncio.CancelledError:
        if token is not None:
            token.cancel()
        if cleanup_on_timeout is not None:
            future.add_done_callback(
                functools.partial(_cleanup_after_future, cleanup=cleanup_on_timeout)
            )
        else:
            _consume_exception(future)
        raise
