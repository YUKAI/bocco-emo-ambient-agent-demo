"""Pi-local minute scheduler that feeds the bridge's durable event queue."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import logging

from .db import EventDatabase


LOGGER = logging.getLogger(__name__)


class ProactiveScheduler:
    """Queue due schedules; the existing single worker performs every effect."""

    def __init__(
        self,
        database: EventDatabase,
        notify_worker: Callable[[], None],
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.database = database
        self.notify_worker = notify_worker
        self._now = now
        self._stopping = asyncio.Event()
        self.ready = False

    async def check_once(self) -> int:
        return await self._check_at(self._now())

    async def _check_at(self, local_now: datetime) -> int:
        enqueued = await self.database.enqueue_due_schedules(local_now)
        if enqueued:
            self.notify_worker()
            LOGGER.info("schedules_enqueued event_count=%d", enqueued)
        return enqueued

    async def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        self.ready = True
        try:
            while not self._stopping.is_set():
                checked_at = self._now()
                try:
                    await self._check_at(checked_at)
                except Exception as exc:
                    LOGGER.warning(
                        "scheduler_check_failed error_type=%s", type(exc).__name__
                    )
                local_now = self._now()
                if local_now.strftime("%Y-%m-%d %H:%M") != checked_at.strftime(
                    "%Y-%m-%d %H:%M"
                ):
                    continue
                delay = max(
                    0.05,
                    60.0 - local_now.second - (local_now.microsecond / 1_000_000),
                )
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            self.ready = False
