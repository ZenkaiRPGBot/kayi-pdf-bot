"""Job admission, cancellation, timeouts, and CPU offloading."""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.services.cancellation import CancelToken
from app.services.errors import (
    JobAlreadyActiveError,
    JobCancelled,
    JobCapacityError,
    ProcessingTimeout,
)
from app.services.jobs import JobManager, run_cpu


@pytest.mark.asyncio
async def test_acquire_and_release() -> None:
    manager = JobManager(max_active_jobs=1, max_queued_jobs=0)
    slot = await manager.acquire(1)
    assert manager.active == 1
    assert manager.is_active(1)
    assert manager.stats == {"active": 1, "queued": 0, "capacity": 1}
    await slot.release()
    assert manager.active == 0
    assert not manager.is_active(1)
    # Releasing twice is harmless.
    await slot.release()
    assert manager.active == 0


@pytest.mark.asyncio
async def test_same_user_cannot_start_two_jobs() -> None:
    manager = JobManager()
    slot = await manager.acquire(7)
    with pytest.raises(JobAlreadyActiveError):
        await manager.acquire(7)
    await slot.release()


@pytest.mark.asyncio
async def test_capacity_is_enforced_without_waiting() -> None:
    manager = JobManager(max_active_jobs=1, max_queued_jobs=0)
    slot = await manager.acquire(1)
    with pytest.raises(JobCapacityError):
        await manager.acquire(2)
    await slot.release()


@pytest.mark.asyncio
async def test_queued_job_times_out() -> None:
    manager = JobManager(
        max_active_jobs=1, max_queued_jobs=1, queue_timeout=0.05
    )
    slot = await manager.acquire(1)
    with pytest.raises(JobCapacityError):
        await manager.acquire(2)
    assert manager.queued == 0
    await slot.release()


@pytest.mark.asyncio
async def test_cancel_queued_job() -> None:
    manager = JobManager(max_active_jobs=1, max_queued_jobs=1, queue_timeout=5.0)
    slot = await manager.acquire(1)
    waiter = asyncio.create_task(manager.acquire(2))
    await asyncio.sleep(0.02)
    assert manager.queued == 1
    assert manager.cancel(2) is True
    with pytest.raises(JobCancelled):
        await waiter
    assert manager.queued == 0
    assert not manager.is_active(2)
    await slot.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_queue_state() -> None:
    manager = JobManager(max_active_jobs=1, max_queued_jobs=1, queue_timeout=5.0)
    slot = await manager.acquire(1)
    waiter = asyncio.create_task(manager.acquire(2))
    await asyncio.sleep(0.02)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert manager.queued == 0
    assert manager.active == 1
    await slot.release()
    assert manager.active == 0
    replacement = await asyncio.wait_for(manager.acquire(3), timeout=1.0)
    await replacement.release()


@pytest.mark.asyncio
async def test_queue_timeout_does_not_leak_a_semaphore_slot() -> None:
    manager = JobManager(max_active_jobs=1, max_queued_jobs=1, queue_timeout=0.02)
    slot = await manager.acquire(1)
    with pytest.raises(JobCapacityError):
        await manager.acquire(2)
    assert manager.queued == 0
    await slot.release()
    replacement = await asyncio.wait_for(manager.acquire(3), timeout=1.0)
    await replacement.release()


@pytest.mark.asyncio
async def test_cancel_active_job_sets_token() -> None:
    manager = JobManager()
    slot = await manager.acquire(3)
    assert manager.cancel(3) is True
    assert slot.token.cancelled
    assert manager.cancel(999) is False
    await slot.release()


@pytest.mark.asyncio
async def test_run_cpu_runs_off_the_event_loop() -> None:
    main_thread = threading.get_ident()
    seen: list[int] = []

    def work() -> str:
        seen.append(threading.get_ident())
        return "done"

    assert await run_cpu(work, timeout=2.0) == "done"
    assert seen and seen[0] != main_thread


@pytest.mark.asyncio
async def test_run_cpu_pre_cancelled_token_does_not_run() -> None:
    called = False

    def work() -> None:
        nonlocal called
        called = True

    token = CancelToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        await run_cpu(work, timeout=2.0, token=token)
    assert called is False


@pytest.mark.asyncio
async def test_run_cpu_timeout_defers_cleanup_until_worker_finishes() -> None:
    cleanup_calls: list[bool] = []

    def slow() -> None:
        import time

        time.sleep(0.15)

    with pytest.raises(ProcessingTimeout) as exc_info:
        await run_cpu(
            slow,
            timeout=0.01,
            cleanup_on_timeout=lambda: cleanup_calls.append(True),
        )
    assert exc_info.value.cleanup_deferred is True
    assert cleanup_calls == []
    await asyncio.sleep(0.35)
    assert cleanup_calls == [True]


@pytest.mark.asyncio
async def test_run_cpu_timeout_cancels_token() -> None:
    token = CancelToken()

    def slow() -> None:
        import time

        time.sleep(0.05)

    with pytest.raises(ProcessingTimeout):
        await run_cpu(slow, timeout=0.01, token=token)
    assert token.cancelled


@pytest.mark.asyncio
async def test_run_cpu_cancelled_task_still_cleans_up() -> None:
    cleanup_calls: list[bool] = []

    def slow() -> None:
        import time

        time.sleep(0.1)

    task = asyncio.create_task(
        run_cpu(
            slow,
            timeout=5.0,
            cleanup_on_timeout=lambda: cleanup_calls.append(True),
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_calls == []
    await asyncio.sleep(0.25)
    assert cleanup_calls == [True]


@pytest.mark.asyncio
async def test_run_cpu_propagates_worker_errors() -> None:
    def fail() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_cpu(fail, timeout=2.0)
