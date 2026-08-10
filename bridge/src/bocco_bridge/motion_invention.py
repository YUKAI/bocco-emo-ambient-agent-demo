"""Invent a motion on request, off the event worker.

The bridge has exactly one event worker, and its priority lanes do not preempt.
Generating a motion inline would therefore stall every other event behind a
model call — the exact defect that was fixed for the reaction banks by moving
generation off the worker, so this follows that pattern rather than repeating
it: the worker only acknowledges and enqueues a durable job, and this generator
drains the job through the same :class:`HermesPriorityGate`, which lets any
foreground reply displace an in-flight invention.

The model returns a compact choreography spec, never a document. The renderer
in :mod:`bocco_bridge.motion_spec` owns every number, and
``validate_motion_document`` is the backstop: nothing that fails it is ever
sent. A generation failure, a malformed spec and a validation failure all
degrade to one spoken line — never a crash, never silence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import time
from typing import Protocol

from .db import EventDatabase, QueuedEvent
from .motion_spec import (
    MotionSpecError,
    parse_motion_spec,
    render_motion_document,
    spec_format_prompt,
)
from .reaction_generation import BackgroundGenerationPreempted, HermesPriorityGate
from .repertoire import MotionRepertoire


LOGGER = logging.getLogger(__name__)

MOTION_INVENTION_ACK_TEXT = "いいね、新しい動きを考えてみるね！"
MOTION_INVENTION_FAILED_TEXT = "ごめんね、新しい動きは思いつかなかった。"
MOTION_INVENTION_READY_TEXT = "できたよ、「{name}」！"
MOTION_INVENTION_REPLAY_TEXT = "「{name}」をもう一度やるね！"
MOTION_INVENTION_EMPTY_TEXT = "まだ覚えている動きはないよ。"
MOTION_INVENTION_LIST_TEXT = "覚えている動きは、{names}だよ。"
MOTION_INVENTION_THEME_MAX_CHARS = 60


class MotionStage(Protocol):
    """The two robot-facing actions an invention needs, owned by the processor.

    Both go through the processor so that echo suppression, the motion budget
    and the existing custom-motion dispatch path keep working exactly as they
    do for every other motion.
    """

    async def speak_aside(self, request_id: str, room_uuid: str, text: str) -> bool: ...

    async def perform_invented_motion(
        self, request_id: str, room_uuid: str, motion_id: int
    ) -> bool: ...


class MotionInventionGenerator:
    """Claim durable invention jobs, render them, perform them, remember them."""

    def __init__(
        self,
        database: EventDatabase,
        hermes: HermesPriorityGate,
        repertoire: MotionRepertoire,
        stage: MotionStage,
        *,
        poll_seconds: float,
        retry_base_seconds: float,
        max_attempts: int,
        retention: int,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.hermes = hermes
        self.repertoire = repertoire
        self.stage = stage
        self.poll_seconds = poll_seconds
        self.retry_base_seconds = retry_base_seconds
        self.max_attempts = max_attempts
        self.retention = retention
        self._now = now
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._active_generation: asyncio.Task[str] | None = None
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
        recovered = await self.database.recover_motion_inventions(now=self._now())
        self.ready = True
        if recovered:
            LOGGER.info("motion_invention_queue_recovered job_count=%d", recovered)
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
        event = await self.database.claim_next_motion_invention(now=self._now())
        if event is None:
            return False
        if self._stopping.is_set():
            await self.database.retry_motion_invention(
                event.request_id, "Shutdown", 0, now=self._now()
            )
            return True
        try:
            await self._process(event)
        except asyncio.CancelledError:
            await self.database.retry_motion_invention(
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
            theme, instructions = self._decode_job(event)
        except ValueError as exc:
            await self.database.dead_letter(event.request_id, type(exc).__name__)
            LOGGER.warning(
                "motion_invention_invalid error_type=%s", type(exc).__name__
            )
            await self.stage.speak_aside(
                event.request_id, room_uuid, MOTION_INVENTION_FAILED_TEXT
            )
            return

        try:
            generation = asyncio.create_task(
                self.hermes.background_respond(
                    # Self-contained prompt: it names its own format and needs
                    # no history, so it never asks Hermes to store anything.
                    conversation=None,
                    text=spec_format_prompt(theme),
                    instructions=instructions,
                ),
                name=f"motion-invention-generate:{event.request_id}",
            )
            self._active_generation = generation
            generated = await generation
        except BackgroundGenerationPreempted:
            await self.database.retry_motion_invention(
                event.request_id,
                "ForegroundPreempted",
                self.poll_seconds,
                now=self._now(),
            )
            LOGGER.info("motion_invention_preempted request_id=%s", event.request_id)
            return
        finally:
            self._active_generation = None

        spec = parse_motion_spec(generated, fallback_name=theme)
        # Rendering validates; an invalid document raises here and never
        # reaches the robot.
        render_motion_document(spec)
        motion = await self.repertoire.remember(
            room_uuid,
            spec.name,
            spec.to_text(),
            event.request_id,
            created_at=self._now(),
            retention=self.retention,
        )
        if motion is None:
            raise MotionSpecError("invented motion could not be remembered")

        # Completed before it is performed, on purpose. A crash between the two
        # costs one performance of a motion the user can simply ask for again;
        # completing afterwards would let a restart regenerate and re-speak the
        # whole thing, which is the worse failure by a wide margin.
        await self.database.complete_motion_invention(
            event.request_id, now=self._now()
        )
        LOGGER.info(
            "motion_invention_ready request_id=%s motion_id=%d beats=%d",
            event.request_id,
            motion.id,
            len(spec.beats),
        )
        await self.stage.speak_aside(
            event.request_id,
            room_uuid,
            MOTION_INVENTION_READY_TEXT.format(name=motion.name),
        )
        await self.stage.perform_invented_motion(
            event.request_id, room_uuid, motion.id
        )

    async def _give_up(self, event: QueuedEvent, exc: Exception) -> None:
        """Retry a while, then answer with a spoken line rather than silence."""

        if event.attempts < self.max_attempts and not isinstance(
            exc, MotionSpecError
        ):
            delay = self._retry_delay(event.attempts)
            await self.database.retry_motion_invention(
                event.request_id, type(exc).__name__, delay, now=self._now()
            )
            LOGGER.warning(
                "motion_invention_retry request_id=%s error_type=%s delay_seconds=%.3f",
                event.request_id,
                type(exc).__name__,
                delay,
            )
            return
        await self.database.complete_motion_invention(
            event.request_id, now=self._now()
        )
        LOGGER.warning(
            "motion_invention_failed request_id=%s error_type=%s",
            event.request_id,
            type(exc).__name__,
        )
        if event.room_uuid:
            await self.stage.speak_aside(
                event.request_id, event.room_uuid, MOTION_INVENTION_FAILED_TEXT
            )

    def _retry_delay(self, attempts: int) -> float:
        exponent = min(max(attempts - 1, 0), 6)
        return min(60.0, self.retry_base_seconds * (2**exponent))

    @staticmethod
    def _decode_job(event: QueuedEvent) -> tuple[str, str]:
        try:
            detail = json.loads(event.event_detail or "")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid motion invention job") from exc
        if not isinstance(detail, dict):
            raise ValueError("invalid motion invention job")
        theme = detail.get("theme", "")
        instructions = detail.get("instructions")
        if not isinstance(theme, str):
            raise ValueError("invalid motion invention theme")
        if not isinstance(instructions, str) or not instructions:
            raise ValueError("invalid instructions")
        return theme[:MOTION_INVENTION_THEME_MAX_CHARS], instructions
