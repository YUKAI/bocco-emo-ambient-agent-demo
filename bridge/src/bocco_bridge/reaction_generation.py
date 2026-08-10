"""Preemptible, durable reaction-bank generation outside the event worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import json
import logging
import time
from typing import Protocol

from .db import EventDatabase, QueuedEvent
from .reactions import (
    REACTION_EVENT_KEYS,
    parse_reaction_phrases,
    reaction_generation_prompt,
)


LOGGER = logging.getLogger(__name__)


class HermesBackend(Protocol):
    async def respond(
        self, conversation: str | None, text: str, instructions: str
    ) -> str: ...


class BackgroundGenerationPreempted(Exception):
    """A foreground Hermes request displaced background generation."""


class ForegroundHermesClient:
    """Expose normal Hermes methods through the foreground side of a gate."""

    def __init__(self, gate: HermesPriorityGate) -> None:
        self._gate = gate

    async def respond(
        self, conversation: str | None, text: str, instructions: str
    ) -> str:
        return await self._gate.foreground_respond(
            conversation, text, instructions
        )

    async def respond_stream(
        self, conversation: str | None, text: str, instructions: str
    ) -> AsyncIterator[str]:
        async for delta in self._gate.foreground_respond_stream(
            conversation, text, instructions
        ):
            yield delta


class HermesPriorityGate:
    """Bound Hermes to one call and let foreground work preempt background."""

    def __init__(self, backend: HermesBackend) -> None:
        self._backend = backend
        self._condition = asyncio.Condition()
        self._active: str | None = None
        self._foreground_waiters = 0
        self._background_call: asyncio.Task[str] | None = None
        self.foreground = ForegroundHermesClient(self)

    def interrupt_background(self) -> None:
        """Cancel an in-flight bank call without touching foreground work."""

        task = self._background_call
        if task is not None and not task.done():
            task.cancel()

    @asynccontextmanager
    async def _foreground_turn(self) -> AsyncIterator[None]:
        acquired = False
        async with self._condition:
            self._foreground_waiters += 1
            try:
                self.interrupt_background()
                await self._condition.wait_for(lambda: self._active is None)
                self._active = "foreground"
                acquired = True
            finally:
                self._foreground_waiters -= 1
        try:
            yield
        finally:
            if acquired:
                async with self._condition:
                    self._active = None
                    self._condition.notify_all()

    async def foreground_respond(
        self, conversation: str | None, text: str, instructions: str
    ) -> str:
        async with self._foreground_turn():
            return await self._backend.respond(conversation, text, instructions)

    async def foreground_respond_stream(
        self, conversation: str | None, text: str, instructions: str
    ) -> AsyncIterator[str]:
        stream_method = getattr(self._backend, "respond_stream", None)
        if stream_method is None:
            raise AttributeError("Hermes backend has no streaming method")
        async with self._foreground_turn():
            stream = stream_method(conversation, text, instructions)
            try:
                async for delta in stream:
                    yield delta
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()

    async def background_respond(
        self, conversation: str | None, text: str, instructions: str
    ) -> str:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._active is None and self._foreground_waiters == 0
            )
            call = asyncio.create_task(
                self._backend.respond(conversation, text, instructions),
                name="reaction-bank-hermes-call",
            )
            self._active = "background"
            self._background_call = call

        preempted = False
        try:
            return await call
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            preempted = True
        finally:
            async with self._condition:
                if self._background_call is call:
                    self._background_call = None
                if self._active == "background":
                    self._active = None
                self._condition.notify_all()
        if preempted:
            raise BackgroundGenerationPreempted
        raise AssertionError("unreachable background generation state")


class ReactionBankGenerator:
    """Claim durable refresh jobs and publish each completed entry separately."""

    def __init__(
        self,
        database: EventDatabase,
        hermes: HermesPriorityGate,
        *,
        poll_seconds: float,
        retry_base_seconds: float,
        stateless_conversation: bool = False,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.hermes = hermes
        self.stateless_conversation = stateless_conversation
        self.poll_seconds = poll_seconds
        self.retry_base_seconds = retry_base_seconds
        self._now = now
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._active_generation: asyncio.Task[str] | None = None
        self.ready = False

    def _conversation(self, persona_hash: str) -> str | None:
        """Name the bank's Hermes conversation, or nothing when stateless.

        Every reaction-bank prompt is self-contained — one fixed prompt per
        event key against a persona already carried in ``instructions`` — so
        this conversation only ever accumulated. It is the second of the two
        stored conversations that grew without bound, and dropping it costs no
        continuity at all.
        """

        if self.stateless_conversation:
            return None
        return f"bocco-reaction-bank:{persona_hash[:16]}"

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
        self._runner = asyncio.current_task()
        recovered = await self.database.recover_reaction_bank_refreshes(
            now=self._now()
        )
        self.ready = True
        if recovered:
            LOGGER.info("reaction_bank_queue_recovered job_count=%d", recovered)
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
            self._runner = None

    async def process_once(self) -> bool:
        event = await self.database.claim_next_reaction_bank_refresh(
            now=self._now()
        )
        if event is None:
            return False
        if self._stopping.is_set():
            await self.database.retry_reaction_bank_refresh(
                event.request_id, "Shutdown", 0, now=self._now()
            )
            return True
        try:
            await self._process(event)
        except asyncio.CancelledError:
            await self.database.retry_reaction_bank_refresh(
                event.request_id, "CancelledError", 0, now=self._now()
            )
            if self._stopping.is_set():
                return True
            raise
        except Exception as exc:
            delay = self._retry_delay(event.attempts)
            await self.database.retry_reaction_bank_refresh(
                event.request_id,
                type(exc).__name__,
                delay,
                now=self._now(),
            )
            LOGGER.warning(
                "reaction_bank_refresh_retry error_type=%s delay_seconds=%.3f",
                type(exc).__name__,
                delay,
            )
        return True

    async def _process(self, event: QueuedEvent) -> None:
        try:
            persona_hash, instructions, completed = self._decode_job(event)
        except ValueError as exc:
            await self.database.dead_letter(event.request_id, type(exc).__name__)
            LOGGER.warning(
                "reaction_bank_refresh_invalid error_type=%s", type(exc).__name__
            )
            return

        for event_key in REACTION_EVENT_KEYS:
            if event_key in completed:
                continue
            try:
                generation = asyncio.create_task(
                    self.hermes.background_respond(
                        conversation=self._conversation(persona_hash),
                        # The composed persona also rides in ``instructions``;
                        # it is repeated in the request text because this call
                        # is a writing brief, not a conversational turn, and
                        # the brief has to name the voice it is written for.
                        text=reaction_generation_prompt(
                            event_key, persona=instructions
                        ),
                        instructions=instructions,
                    ),
                    name=f"reaction-bank-generate:{event_key}",
                )
                self._active_generation = generation
                generated = await generation
                phrases = parse_reaction_phrases(generated)
            except BackgroundGenerationPreempted:
                await self.database.retry_reaction_bank_refresh(
                    event.request_id,
                    "ForegroundPreempted",
                    self.poll_seconds,
                    now=self._now(),
                )
                LOGGER.info(
                    "reaction_bank_refresh_preempted persona_hash_prefix=%s",
                    persona_hash[:12],
                )
                return
            except Exception as exc:
                delay = self._retry_delay(event.attempts)
                await self.database.retry_reaction_bank_refresh(
                    event.request_id,
                    type(exc).__name__,
                    delay,
                    now=self._now(),
                )
                LOGGER.warning(
                    "reaction_bank_refresh_retry event_key=%s error_type=%s delay_seconds=%.3f",
                    event_key,
                    type(exc).__name__,
                    delay,
                )
                return
            finally:
                self._active_generation = None

            published = await self.database.publish_reaction_bank_entry(
                event.request_id,
                persona_hash,
                event_key,
                phrases,
                generated_at=self._now(),
            )
            if not published:
                LOGGER.info(
                    "reaction_bank_refresh_superseded persona_hash_prefix=%s",
                    persona_hash[:12],
                )
                return
            completed.add(event_key)

        finished = await self.database.complete_reaction_bank_refresh(
            event.request_id, now=self._now()
        )
        if finished:
            LOGGER.info(
                "reaction_bank_refresh_completed persona_hash_prefix=%s refreshed=%d total=%d",
                persona_hash[:12],
                len(completed),
                len(REACTION_EVENT_KEYS),
            )

    def _retry_delay(self, attempts: int) -> float:
        exponent = min(max(attempts - 1, 0), 6)
        return min(60.0, self.retry_base_seconds * (2**exponent))

    @staticmethod
    def _decode_job(event: QueuedEvent) -> tuple[str, str, set[str]]:
        try:
            detail = json.loads(event.event_detail or "")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid reaction bank job") from exc
        if not isinstance(detail, dict):
            raise ValueError("invalid reaction bank job")
        persona_hash = detail.get("persona_hash")
        instructions = detail.get("instructions")
        completed_keys = detail.get("completed_keys", [])
        if not isinstance(persona_hash, str) or not persona_hash:
            raise ValueError("invalid persona hash")
        if not isinstance(instructions, str) or not instructions:
            raise ValueError("invalid instructions")
        if not isinstance(completed_keys, list) or not all(
            isinstance(item, str) for item in completed_keys
        ):
            raise ValueError("invalid reaction bank progress")
        return persona_hash, instructions, set(completed_keys)
