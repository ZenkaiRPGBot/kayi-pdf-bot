"""Bounded webhook ingress and update de-duplication."""

from __future__ import annotations

import asyncio

import pytest

from app.services.webhook import (
    IngressStatus,
    UpdateDeduplicator,
    UpdateIngress,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def update(update_id: object = 1) -> dict:
    return {"update_id": update_id, "message": {"text": "hi"}}


@pytest.mark.parametrize("payload", [None, [], "text", 42, object()])
def test_invalid_payload_types(payload) -> None:
    ingress = UpdateIngress()
    assert ingress.ingest(payload) is IngressStatus.INVALID


@pytest.mark.parametrize(
    "update_id", [None, "1", True, False, -1, 1.5, [], {}]
)
def test_invalid_update_ids(update_id) -> None:
    ingress = UpdateIngress()
    assert ingress.ingest({"update_id": update_id}) is IngressStatus.INVALID


def test_accepts_and_deduplicates() -> None:
    ingress = UpdateIngress(queue_size=10)
    assert ingress.ingest(update(1)) is IngressStatus.ACCEPTED
    assert ingress.size == 1
    assert ingress.ingest(update(1)) is IngressStatus.DUPLICATE
    assert ingress.size == 1
    assert ingress.ingest(update(2)) is IngressStatus.ACCEPTED
    assert ingress.size == 2


def test_queue_full_is_not_remembered() -> None:
    ingress = UpdateIngress(queue_size=1)
    assert ingress.ingest(update(1)) is IngressStatus.ACCEPTED
    assert ingress.ingest(update(2)) is IngressStatus.FULL
    assert ingress.queue.get_nowait() == update(1)
    assert ingress.ingest(update(2)) is IngressStatus.ACCEPTED
    assert ingress.ingest(update(2)) is IngressStatus.DUPLICATE


def test_deduplicator_ttl_and_capacity() -> None:
    clock = FakeClock()
    dedup = UpdateDeduplicator(ttl=10.0, max_entries=3, clock=clock)
    for update_id in (1, 2, 3):
        dedup.add(update_id)
        clock.advance(1)
    assert dedup.contains(1) is True
    dedup.add(4)
    assert dedup.size == 3
    assert dedup.contains(1) is False
    clock.advance(20)
    assert dedup.size == 0
    assert dedup.contains(4) is False


def test_queue_properties() -> None:
    ingress = UpdateIngress(queue_size=4)
    assert ingress.capacity == 4
    assert ingress.size == 0
    assert ingress.queue.maxsize == 4
