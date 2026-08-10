"""Ask Hermes what happened, once per completed exchange, off the reply path.

WHERE THIS RUNS, AND WHY IT CANNOT COST A REPLY. Extraction is enqueued from
``EventProcessor._record_turn``, which the worker reaches *after* the reply has
already been delivered. The worker then writes one row to ``inbound_events``
and moves on; the model call happens here, in a task of its own, through the
same :class:`HermesPriorityGate` that
:class:`~bocco_bridge.reaction_generation.ReactionBankGenerator` and
:class:`~bocco_bridge.motion_invention.MotionInventionGenerator` use. Any user
utterance preempts an extraction in flight, the preempted job is durable and
retried, and every failure path ends in no event recorded — never a failed,
delayed, or duplicated reply.

CONTENTION, since this is now the THIRD consumer of that one background lane.
It is also by far the most frequent: the bank refreshes at startup and on a
persona change, an invention only when somebody asks for one, but extraction
fires once per exchange. Two bounds keep it from monopolising the lane:

* ``delay_seconds`` — a job is not even *available* until this long after the
  exchange. At the measured median 83 s between utterances a 15 s delay puts
  extraction into the gap after the household stops talking, and during a fast
  back-and-forth the jobs simply accumulate instead of firing between
  sentences.
* ``min_interval_seconds`` — a floor between two extraction calls, so a burst
  of queued jobs drains at a rate that always leaves the lane free.

Between two *background* jobs the gate has no priority, so an invention can in
the worst case wait behind one extraction call. That was judged acceptable
rather than fixed with a priority scheme: the invention has already spoken its
「いいね、新しい動きを考えてみるね！」 acknowledgment, so the wait is measured
against a robot that is already visibly busy, and the alternative — a second
ordering mechanism inside the gate — buys a second or two at the cost of a
concurrency primitive nobody can reason about.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import time

from .db import EventDatabase, QueuedEvent
from .event_memory import (
    EventExtractionError,
    EventMemory,
    event_extraction_prompt,
    parse_extracted_event,
)
from .reaction_generation import BackgroundGenerationPreempted, HermesPriorityGate


LOGGER = logging.getLogger(__name__)

# The extraction call is a classification task with a fixed output shape, so it
# is told what it is rather than being run in the robot's voice. Sending the
# persona would ask a character who speaks in 20-character sentences to emit
# JSON, which is the one thing this call must do reliably.
EXTRACTION_INSTRUCTIONS = (
    "You are a careful, conservative record keeper. You answer with JSON only "
    "— no prose, no code fence, no explanation. When in doubt you record "
    "nothing."
)


class EventExtractor:
    """Claim durable extraction jobs and store at most one event per exchange."""

    def __init__(
        self,
        database: EventDatabase,
        hermes: HermesPriorityGate,
        events: EventMemory,
        *,
        poll_seconds: float,
        retry_base_seconds: float,
        max_attempts: int,
        retention: int,
        min_interval_seconds: float = 3.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.hermes = hermes
        self.events = events
        self.poll_seconds = poll_seconds
        self.retry_base_seconds = retry_base_seconds
        self.max_attempts = max_attempts
        self.retention = retention
        self.min_interval_seconds = min_interval_seconds
        self._now = now
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._active_generation: asyncio.Task[str] | None = None
        self._last_call_at = 0.0
        self.ready = False

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        self.hermes.interrupt_background()
        active = self._active_generation
        if active is not None and not active.done():
            active.cancel()

    async def run(self) -> None:
        recovered = await self.database.recover_event_extractions(now=self._now())
        self.ready = True
        if recovered:
            LOGGER.info("event_extraction_queue_recovered job_count=%d", recovered)
        try:
            while not self._stopping.is_set():
                if await self.process_once():
                    continue
                self._wake.clear()
                if self._stopping.is_set():
                    break
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.poll_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            self.ready = False

    async def process_once(self) -> bool:
        event = await self.database.claim_next_event_extraction(now=self._now())
        if event is None:
            return False
        if self._stopping.is_set():
            await self.database.retry_event_extraction(
                event.request_id, "Shutdown", 0, now=self._now()
            )
            return True
        try:
            await self._process(event)
        except asyncio.CancelledError:
            await self.database.retry_event_extraction(
                event.request_id, "CancelledError", 0, now=self._now()
            )
            if self._stopping.is_set():
                return True
            raise
        except Exception as exc:
            # Nothing here may escape into the worker's lane or kill this task.
            await self._give_up(event, exc)
        return True

    async def _process(self, event: QueuedEvent) -> None:
        room_uuid = event.room_uuid
        if not room_uuid:
            await self.database.dead_letter(event.request_id, "MissingRoom")
            return
        try:
            user_text, reply_text, said_at, source_id = self._decode_job(event)
        except ValueError as exc:
            # A job we cannot read is not a job we can retry into working.
            await self.database.dead_letter(event.request_id, type(exc).__name__)
            LOGGER.warning(
                "event_extraction_invalid error_type=%s", type(exc).__name__
            )
            return

        await self._respect_rate_limit()
        try:
            generation = asyncio.create_task(
                self.hermes.background_respond(
                    # Stateless on purpose. The prompt is self-contained and
                    # this is not a conversational turn, so it must never
                    # append to — or be coloured by — the room's own history.
                    conversation=None,
                    text=event_extraction_prompt(user_text, reply_text, said_at),
                    instructions=EXTRACTION_INSTRUCTIONS,
                ),
                name=f"event-extract:{event.request_id}",
            )
            self._active_generation = generation
            self._last_call_at = self._now()
            generated = await generation
        except BackgroundGenerationPreempted:
            await self.database.retry_event_extraction(
                event.request_id,
                "ForegroundPreempted",
                self.poll_seconds,
                now=self._now(),
            )
            LOGGER.info("event_extraction_preempted request_id=%s", source_id)
            return
        finally:
            self._active_generation = None

        try:
            candidate = parse_extracted_event(generated, said_at)
        except EventExtractionError as exc:
            # Output that is not a decision is treated as "no event". Retrying
            # a model that answered in prose usually gets prose again, and an
            # exchange nobody can classify is exactly the exchange that should
            # not produce a row.
            await self.database.complete_event_extraction(
                event.request_id, now=self._now()
            )
            LOGGER.warning(
                "event_extraction_unparsable request_id=%s error=%s",
                source_id,
                str(exc)[:80],
            )
            return

        # Completed before the store write, for the same reason motion
        # invention completes before it performs: a crash between the two
        # costs one un-extracted exchange, whereas completing afterwards lets a
        # restart re-run the model call on work that already succeeded.
        await self.database.complete_event_extraction(
            event.request_id, now=self._now()
        )
        if candidate is None:
            LOGGER.info("event_extraction_rejected request_id=%s", source_id)
            return
        recorded = await self.events.record(
            room_uuid,
            source_id,
            candidate,
            said_at=said_at,
            source_user_text=user_text,
            source_reply_text=reply_text,
            created_at=self._now(),
            retention=self.retention,
        )
        if recorded is None:
            LOGGER.info("event_extraction_duplicate request_id=%s", source_id)
            return
        LOGGER.info(
            "event_recorded request_id=%s event_id=%d kind=%s chars=%d",
            source_id,
            recorded.id,
            recorded.kind,
            len(recorded.text),
        )

    async def _respect_rate_limit(self) -> None:
        """Leave the lane free between calls, and yield to a shutdown."""

        if self.min_interval_seconds <= 0:
            return
        elapsed = self._now() - self._last_call_at
        remaining = self.min_interval_seconds - elapsed
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=remaining)
        except TimeoutError:
            return

    async def _give_up(self, event: QueuedEvent, exc: Exception) -> None:
        """Retry a while, then drop the exchange. Never speak, never crash."""

        if event.attempts < self.max_attempts:
            delay = self._retry_delay(event.attempts)
            await self.database.retry_event_extraction(
                event.request_id, type(exc).__name__, delay, now=self._now()
            )
            LOGGER.warning(
                "event_extraction_retry request_id=%s error_type=%s delay_seconds=%.3f",
                event.request_id,
                type(exc).__name__,
                delay,
            )
            return
        await self.database.complete_event_extraction(
            event.request_id, now=self._now()
        )
        LOGGER.warning(
            "event_extraction_failed request_id=%s error_type=%s",
            event.request_id,
            type(exc).__name__,
        )

    def _retry_delay(self, attempts: int) -> float:
        exponent = min(max(attempts - 1, 0), 6)
        return min(60.0, self.retry_base_seconds * (2**exponent))

    @staticmethod
    def _decode_job(event: QueuedEvent) -> tuple[str, str, float, str]:
        try:
            detail = json.loads(event.event_detail or "")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid event extraction job") from exc
        if not isinstance(detail, dict):
            raise ValueError("invalid event extraction job")
        user_text = detail.get("user_text")
        reply_text = detail.get("reply_text")
        said_at = detail.get("said_at")
        source_id = detail.get("source_request_id")
        if not isinstance(user_text, str) or not user_text.strip():
            raise ValueError("invalid extraction user text")
        if not isinstance(reply_text, str):
            raise ValueError("invalid extraction reply text")
        if isinstance(said_at, bool) or not isinstance(said_at, (int, float)):
            raise ValueError("invalid extraction timestamp")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("invalid extraction source request id")
        return user_text, reply_text, float(said_at), source_id
