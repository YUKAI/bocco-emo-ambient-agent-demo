from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bocco_bridge.app import BridgeRuntime, RuntimeDependencies
from bocco_bridge.config import BridgeConfig
from bocco_bridge.db import EventDatabase
from bocco_bridge.events import EventProcessor, EventWorker
from bocco_bridge.reaction_generation import (
    BackgroundGenerationPreempted,
    HermesPriorityGate,
    ReactionBankGenerator,
)
from bocco_bridge.reactions import (
    DEFAULT_REACTION_PHRASES,
    REACTION_EVENT_KEYS,
    REACTION_PHRASE_COUNT,
    composed_persona_hash,
)
from runtime.fakes import FakeBocco, FakeInboundEvent, FakeParser
from timeouts import LIVENESS_TIMEOUT


class ManualClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ControlledHermes:
    def __init__(
        self,
        *,
        block_background_at: set[int] | None = None,
        background_failures: int = 0,
        block_first_foreground: bool = False,
        invalid_background_response: bool = False,
    ) -> None:
        self.block_background_at = set(block_background_at or ())
        self.background_failures = background_failures
        self.block_first_foreground = block_first_foreground
        self.invalid_background_response = invalid_background_response
        self.background_started: asyncio.Queue[int] = asyncio.Queue()
        self.foreground_started: asyncio.Queue[int] = asyncio.Queue()
        self.foreground_release = asyncio.Event()
        self.calls: list[tuple[str, str, str]] = []
        self.conversations: list[str | None] = []
        self.cancelled_background_calls: list[int] = []
        self.active = 0
        self.max_active = 0
        self._background_count = 0
        self._foreground_count = 0

    async def respond(
        self, conversation: str | None, text: str, instructions: str
    ) -> str:
        # A stateless reaction-bank call carries no conversation at all; the
        # foreground reply path always names its room.
        self.conversations.append(conversation)
        background = conversation is None or conversation.startswith(
            "bocco-reaction-bank:"
        )
        if background:
            index = self._background_count
            self._background_count += 1
            await self.background_started.put(index)
        else:
            index = self._foreground_count
            self._foreground_count += 1
            await self.foreground_started.put(index)

        self.calls.append(("background" if background else "foreground", text, instructions))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if background and self.background_failures:
                self.background_failures -= 1
                raise RuntimeError("Hermes is not ready")
            if background and index in self.block_background_at:
                await asyncio.Future()
            if not background and self.block_first_foreground and index == 0:
                await self.foreground_release.wait()
            if background:
                if self.invalid_background_response:
                    return "not json"
                phrases = tuple(
                    f"反応{item}" for item in range(REACTION_PHRASE_COUNT)
                )
                return json.dumps(phrases, ensure_ascii=False)
            return "ユーザーへの返事です。"
        except asyncio.CancelledError:
            if background:
                self.cancelled_background_calls.append(index)
            raise
        finally:
            self.active -= 1

    @property
    def background_calls(self) -> list[tuple[str, str, str]]:
        return [call for call in self.calls if call[0] == "background"]


class FakeRunner:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def setup(self) -> None:
        pass

    async def cleanup(self) -> None:
        pass


class FakeSite:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def start(self) -> None:
        pass


class ReactionGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = BridgeConfig(
            webhook_secret="secret",
            database_path=Path(self.temporary.name) / "state.db",
            tunnel_enabled=False,
            motions_enabled=False,
            stream_sentences=False,
            worker_poll_seconds=0.1,
            worker_retry_base_seconds=1.0,
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        self.clock = ManualClock()
        self.bocco = FakeBocco()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def runtime_parts(
        self,
        hermes: ControlledHermes,
        *,
        stateless_conversation: bool = False,
    ) -> tuple[HermesPriorityGate, ReactionBankGenerator, EventWorker]:
        gate = HermesPriorityGate(hermes)
        generator = ReactionBankGenerator(
            self.database,
            gate,
            poll_seconds=self.config.worker_poll_seconds,
            retry_base_seconds=self.config.worker_retry_base_seconds,
            stateless_conversation=stateless_conversation,
            now=self.clock,
        )
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            gate.foreground,
            now=self.clock,
            reaction_choice=lambda phrases: phrases[0],
        )
        return gate, generator, EventWorker(self.config, self.database, processor)

    async def enqueue_refresh(
        self,
        trigger: str,
        *,
        persona_hash: str = "persona-a",
        instructions: str = "persona-a-instructions",
    ) -> None:
        self.assertTrue(
            await self.database.enqueue_reaction_bank_refresh(
                trigger,
                persona_hash,
                instructions,
                "room-1",
                now=self.clock(),
            )
        )

    async def next_background_call(self, hermes: ControlledHermes) -> int:
        return await asyncio.wait_for(hermes.background_started.get(), timeout=LIVENESS_TIMEOUT)

    async def test_bank_stores_a_named_conversation_only_while_persistent(
        self,
    ) -> None:
        named_hermes = ControlledHermes()
        _, named, _ = self.runtime_parts(named_hermes)
        await self.enqueue_refresh("named-bank")
        self.assertTrue(await named.process_once())

        self.assertEqual(
            set(named_hermes.conversations), {"bocco-reaction-bank:persona-a"}
        )

        stateless_hermes = ControlledHermes()
        _, stateless, _ = self.runtime_parts(
            stateless_hermes, stateless_conversation=True
        )
        await self.enqueue_refresh("stateless-bank", persona_hash="persona-b")
        self.assertTrue(await stateless.process_once())

        # Every bank prompt is self-contained, so nothing is stored server-side.
        self.assertEqual(set(stateless_hermes.conversations), {None})
        self.assertEqual(
            len(stateless_hermes.background_calls), len(REACTION_EVENT_KEYS)
        )

    async def test_recording_and_accel_finish_while_bank_call_is_still_running(
        self,
    ) -> None:
        hermes = ControlledHermes(block_background_at={0})
        gate, generator, worker = self.runtime_parts(hermes)
        await self.enqueue_refresh("nonblocking-physical")
        generation = asyncio.create_task(generator.process_once())
        self.assertEqual(await self.next_background_call(hermes), 0)

        await self.database.enqueue(
            FakeInboundEvent(
                request_id="recording-user-event",
                event_type="recording.finished",
                speech_text=None,
                message_id=None,
                message_media=None,
            )
        )
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="accel-user-event",
                event_type="accel.detected",
                speech_text=None,
                message_id=None,
                message_media=None,
                event_detail="lift",
            )
        )

        started_at = self.clock()
        self.assertTrue(await worker.process_once())
        self.assertTrue(await worker.process_once())
        self.assertEqual(self.clock(), started_at)
        self.assertFalse(generation.done())
        self.assertEqual(
            self.bocco.sent,
            [("room-1", DEFAULT_REACTION_PHRASES["lift"][0])],
        )

        gate.interrupt_background()
        self.assertTrue(await asyncio.wait_for(generation, timeout=LIVENESS_TIMEOUT))
        for request_id in ("recording-user-event", "accel-user-event"):
            event = await self.database.get_event(request_id)
            assert event is not None
            self.assertEqual(event.status, "completed")

    async def test_message_preempts_bank_call_before_foreground_hermes_starts(
        self,
    ) -> None:
        hermes = ControlledHermes(block_background_at={0})
        _, generator, worker = self.runtime_parts(hermes)
        await self.enqueue_refresh("nonblocking-message")
        generation = asyncio.create_task(generator.process_once())
        self.assertEqual(await self.next_background_call(hermes), 0)

        await self.database.enqueue(
            FakeInboundEvent(
                request_id="message-user-event",
                event_type="message.received",
                speech_text="今すぐ返事して",
                message_id="message-user-event-id",
            )
        )
        started_at = self.clock()
        self.assertTrue(await worker.process_once())
        self.assertEqual(self.clock(), started_at)
        self.assertTrue(await asyncio.wait_for(generation, timeout=LIVENESS_TIMEOUT))

        self.assertEqual(hermes.cancelled_background_calls, [0])
        self.assertEqual([call[0] for call in hermes.calls], ["background", "foreground"])
        self.assertEqual(hermes.max_active, 1)
        self.assertEqual(self.bocco.sent, [("room-1", "ユーザーへの返事です。")])
        refresh = await self.database.get_event(
            "internal:reaction-bank:nonblocking-message"
        )
        assert refresh is not None
        self.assertEqual(refresh.status, "pending")
        self.assertEqual(refresh.last_error, "ForegroundPreempted")

    async def test_partial_progress_survives_interruption_and_resume(self) -> None:
        hermes = ControlledHermes(block_background_at={1})
        gate, generator, _ = self.runtime_parts(hermes)
        await self.enqueue_refresh("partial")
        generation = asyncio.create_task(generator.process_once())
        self.assertEqual(await self.next_background_call(hermes), 0)
        self.assertEqual(await self.next_background_call(hermes), 1)

        first_key = REACTION_EVENT_KEYS[0]
        first_phrases = await self.database.get_reaction_phrases(
            "persona-a", first_key
        )
        self.assertIsNotNone(first_phrases)
        await generator.stop()
        self.assertTrue(await asyncio.wait_for(generation, timeout=LIVENESS_TIMEOUT))

        reopened = EventDatabase(self.config.database_path)
        await reopened.initialize()
        self.clock.advance(1)
        self.assertFalse(
            await reopened.enqueue_reaction_bank_refresh(
                "restart-startup",
                "persona-a",
                "persona-a-instructions",
                "room-1",
                now=self.clock(),
            )
        )
        self.assertIsNone(
            await reopened.get_event("internal:reaction-bank:restart-startup")
        )
        resumed_hermes = ControlledHermes()
        resumed = ReactionBankGenerator(
            reopened,
            HermesPriorityGate(resumed_hermes),
            poll_seconds=self.config.worker_poll_seconds,
            retry_base_seconds=self.config.worker_retry_base_seconds,
            now=self.clock,
        )
        self.assertTrue(await resumed.process_once())

        self.assertEqual(
            len(resumed_hermes.background_calls), len(REACTION_EVENT_KEYS) - 1
        )
        self.assertEqual(
            await reopened.get_reaction_phrases("persona-a", first_key),
            first_phrases,
        )
        for event_key in REACTION_EVENT_KEYS:
            self.assertIsNotNone(
                await reopened.get_reaction_phrases("persona-a", event_key)
            )
        job = await reopened.get_event("internal:reaction-bank:partial")
        assert job is not None
        self.assertEqual(job.status, "completed")

    async def test_persona_change_supersedes_inflight_job_without_duplicates(
        self,
    ) -> None:
        hermes = ControlledHermes(block_background_at={0})
        gate, generator, _ = self.runtime_parts(hermes)
        await self.enqueue_refresh(
            "old-persona", persona_hash="old-hash", instructions="old-instructions"
        )
        old_generation = asyncio.create_task(generator.process_once())
        self.assertEqual(await self.next_background_call(hermes), 0)

        self.clock.advance(1)
        await self.enqueue_refresh(
            "new-persona", persona_hash="new-hash", instructions="new-instructions"
        )
        gate.interrupt_background()
        self.assertTrue(await asyncio.wait_for(old_generation, timeout=LIVENESS_TIMEOUT))
        old_job = await self.database.get_event(
            "internal:reaction-bank:old-persona"
        )
        assert old_job is not None
        self.assertEqual(old_job.status, "completed")
        self.assertEqual(old_job.last_error, "superseded")

        hermes.block_background_at.clear()
        self.assertTrue(await generator.process_once())
        old_calls = [call for call in hermes.background_calls if call[2] == "old-instructions"]
        new_calls = [call for call in hermes.background_calls if call[2] == "new-instructions"]
        self.assertEqual(len(old_calls), 1)
        self.assertEqual(len(new_calls), len(REACTION_EVENT_KEYS))
        self.assertEqual(len({call[1] for call in new_calls}), len(REACTION_EVENT_KEYS))
        for event_key in REACTION_EVENT_KEYS:
            self.assertIsNotNone(
                await self.database.get_reaction_phrases("new-hash", event_key)
            )

    async def test_hermes_gate_serializes_calls_and_prioritizes_foreground(self) -> None:
        hermes = ControlledHermes(
            block_background_at={0}, block_first_foreground=True
        )
        gate = HermesPriorityGate(hermes)
        background = asyncio.create_task(
            gate.background_respond("bocco-reaction-bank:test", "bank", "instructions")
        )
        self.assertEqual(await self.next_background_call(hermes), 0)

        first = asyncio.create_task(
            gate.foreground_respond("bocco-room:1", "first", "instructions")
        )
        second = asyncio.create_task(
            gate.foreground_respond("bocco-room:1", "second", "instructions")
        )
        self.assertEqual(
            await asyncio.wait_for(hermes.foreground_started.get(), timeout=LIVENESS_TIMEOUT), 0
        )
        self.assertTrue(hermes.foreground_started.empty())
        self.assertEqual(hermes.active, 1)
        hermes.foreground_release.set()

        results = await asyncio.gather(first, second)
        displaced = await asyncio.gather(background, return_exceptions=True)
        self.assertEqual(
            results, ["ユーザーへの返事です。", "ユーザーへの返事です。"]
        )
        self.assertIsInstance(displaced[0], BackgroundGenerationPreempted)
        self.assertEqual(hermes.max_active, 1)

    async def test_cold_boot_failure_retries_from_durable_state(self) -> None:
        hermes = ControlledHermes(background_failures=1)
        _, generator, _ = self.runtime_parts(hermes)
        await self.enqueue_refresh("cold-boot")

        self.assertTrue(await generator.process_once())
        retrying = await self.database.get_event(
            "internal:reaction-bank:cold-boot"
        )
        assert retrying is not None
        self.assertEqual(retrying.status, "pending")
        self.assertEqual(retrying.last_error, "RuntimeError")
        self.clock.advance(0.5)
        self.assertFalse(await generator.process_once())
        self.clock.advance(0.5)
        self.assertTrue(await generator.process_once())
        completed = await self.database.get_event(
            "internal:reaction-bank:cold-boot"
        )
        assert completed is not None
        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            len(hermes.background_calls), len(REACTION_EVENT_KEYS) + 1
        )

    async def test_invalid_generation_retries_without_replacing_old_entry(
        self,
    ) -> None:
        old = ("前の一", "前の二", "前の三", "前の四", "前の五")
        await self.database.put_reaction_phrases(
            "persona-a", REACTION_EVENT_KEYS[0], old, generated_at=900
        )
        hermes = ControlledHermes(invalid_background_response=True)
        _, generator, _ = self.runtime_parts(hermes)
        await self.enqueue_refresh("invalid-output")

        self.assertTrue(await generator.process_once())

        self.assertEqual(
            await self.database.get_reaction_phrases(
                "persona-a", REACTION_EVENT_KEYS[0]
            ),
            old,
        )
        retrying = await self.database.get_event(
            "internal:reaction-bank:invalid-output"
        )
        assert retrying is not None
        self.assertEqual(retrying.status, "pending")
        self.assertEqual(retrying.last_error, "ValueError")

    async def test_runtime_startup_starts_background_refresh(self) -> None:
        hermes = ControlledHermes(block_background_at={0})
        runtime = BridgeRuntime(
            self.config,
            RuntimeDependencies(
                bocco=self.bocco,
                hermes=hermes,
                webhook_parser=FakeParser(),
            ),
        )
        with (
            patch("bocco_bridge.app.web.AppRunner", FakeRunner),
            patch("bocco_bridge.app.web.TCPSite", FakeSite),
        ):
            await runtime.start()
            self.assertEqual(await self.next_background_call(hermes), 0)
            await runtime.stop()

    async def test_persona_command_starts_background_refresh(self) -> None:
        hermes = ControlledHermes()
        _, generator, worker = self.runtime_parts(hermes)
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="persona-command",
                event_type="message.received",
                speech_text="ペルソナ：元気な性格",
                message_id="persona-command-message",
            )
        )

        self.assertTrue(await worker.process_once())
        self.assertTrue(await generator.process_once())

        instructions = self.config.compose_response_instructions("元気な性格")
        persona_hash = composed_persona_hash(instructions)
        self.assertEqual(len(hermes.background_calls), len(REACTION_EVENT_KEYS))
        for event_key in REACTION_EVENT_KEYS:
            self.assertIsNotNone(
                await self.database.get_reaction_phrases(persona_hash, event_key)
            )


if __name__ == "__main__":
    unittest.main()
