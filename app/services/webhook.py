"""Bounded webhook ingress and duplicate-update suppression."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from enum import Enum

from app.utils.logging import get_logger

log = get_logger(__name__)


class IngressStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    FULL = "full"
    INVALID = "invalid"


class UpdateDeduplicator:
    """Small TTL/LRU set of Telegram update IDs."""

    def __init__(
        self,
        *,
        ttl: float = 600.0,
        max_entries: int = 2000,
        clock=time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._items: OrderedDict[int, float] = OrderedDict()

    def contains(self, update_id: int) -> bool:
        self._prune()
        return update_id in self._items

    def add(self, update_id: int) -> None:
        self._prune()
        self._items[update_id] = self._clock()
        self._items.move_to_end(update_id)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def _prune(self) -> None:
        cutoff = self._clock() - self.ttl
        while self._items:
            update_id, seen_at = next(iter(self._items.items()))
            if seen_at > cutoff:
                break
            self._items.pop(update_id, None)

    @property
    def size(self) -> int:
        self._prune()
        return len(self._items)


class UpdateIngress:
    """Queue accepted updates only; full queue means Telegram should retry."""

    def __init__(
        self,
        *,
        queue_size: int = 100,
        dedup_ttl: float = 600.0,
        dedup_max: int = 2000,
        clock=time.monotonic,
    ) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_size)
        self.dedup = UpdateDeduplicator(
            ttl=dedup_ttl, max_entries=dedup_max, clock=clock
        )

    def ingest(self, payload: object) -> IngressStatus:
        if not isinstance(payload, dict):
            return IngressStatus.INVALID
        update_id = payload.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
            return IngressStatus.INVALID
        if self.dedup.contains(update_id):
            return IngressStatus.DUPLICATE
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("Webhook queue full; asking Telegram to retry")
            return IngressStatus.FULL
        self.dedup.add(update_id)
        return IngressStatus.ACCEPTED

    @property
    def size(self) -> int:
        return self.queue.qsize()

    @property
    def capacity(self) -> int:
        return self.queue.maxsize
