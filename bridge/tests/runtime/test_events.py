import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, call, patch

from bocco_bridge.config import BASE_SPEECH_INSTRUCTIONS, BridgeConfig
from bocco_bridge.bocco import MotionPreset, SentMessage, Stamp
from bocco_bridge.custom_motions import (
    CUSTOM_MOTION_DOCUMENTS,
    motion_duration_seconds,
)
from bocco_bridge.db import EventDatabase
from bocco_bridge.events import (
    PERSONA_CHANGED_TEXT,
    PERSONA_PRESETS,
    PERSONA_RESET_TEXT,
    PERSONA_TOO_LONG_TEXT,
    MEMORY_REMEMBERED_TEXT,
    MEMORY_TOO_LONG_TEXT,
    EventProcessor,
    EventWorker,
    radar_scene_cue,
)
from bocco_bridge.memory import HouseholdMemory
from bocco_bridge.motions import MotionCatalog
from bocco_bridge.reactions import (
    DEFAULT_REACTION_PHRASES,
    SERIOUS_ACCEL_REACTIONS,
    composed_persona_hash,
    radar_reaction_key,
)
from bocco_bridge.stamps import StampCatalog
from runtime.fakes import (
    FakeBocco,
    FakeFastRouteSkills,
    FakeHermes,
    FakeInboundEvent,
    FakeStreamingHermes,
)
from timeouts import LIVENESS_TIMEOUT


class EventWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=Path(self.temporary.name) / "state.db",
            tunnel_enabled=False,
            worker_max_attempts=3,
            worker_retry_base_seconds=0,
            radar_cooldown_seconds=300,
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        self.bocco = FakeBocco()
        self.hermes = FakeHermes("短い返事です。")
        self.now = 1_000.0
        self.processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            reaction_choice=lambda phrases: phrases[0],
        )
        self.worker = EventWorker(self.config, self.database, self.processor)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _enqueue_and_process(self, event: FakeInboundEvent) -> None:
        self.assertTrue(await self.database.enqueue(event))
        self.assertTrue(await self.worker.process_once())

    def _ack_runtime(
        self,
        *,
        enabled: bool = True,
        budget: int = 8,
        thinking_enabled: bool = False,
        thinking_delay: float = 1.5,
        thinking_max_dispatches: int = 2,
        thinking_stamp_name: str = "",
        stamps: tuple[Stamp, ...] = (),
        sleep: AsyncMock | None = None,
        hermes: FakeHermes | AsyncMock | None = None,
        fast_route_skills: FakeFastRouteSkills | None = None,
        memory: HouseholdMemory | None = None,
    ) -> tuple[EventProcessor, EventWorker]:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.config.database_path,
            tunnel_enabled=False,
            worker_max_attempts=3,
            worker_retry_base_seconds=0,
            ack_motion_enabled=enabled,
            ack_motion_name="ALRIGHT_N_0",
            thinking_motion_enabled=thinking_enabled,
            thinking_motion_delay_seconds=thinking_delay,
            thinking_motion_max_dispatches=thinking_max_dispatches,
            thinking_stamp_name=thinking_stamp_name,
            motion_budget_per_minute=budget,
        )
        processor_options = {} if sleep is None else {"sleep": sleep}
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            hermes or self.hermes,
            now=lambda: self.now,
            motion_catalog=MotionCatalog(
                (MotionPreset("ALRIGHT_N_0", "ack-motion-uuid"),)
            ),
            stamp_catalog=StampCatalog(stamps),
            fast_route_skills=fast_route_skills,
            memory=memory,
            **processor_options,
        )
        return processor, EventWorker(config, self.database, processor)

    def _ambient_runtime(
        self,
        *,
        database: EventDatabase | None = None,
        hermes: FakeHermes | AsyncMock | None = None,
    ) -> tuple[EventProcessor, EventWorker]:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.config.database_path,
            tunnel_enabled=False,
            worker_max_attempts=3,
            worker_retry_base_seconds=0,
            accel_active_cooldown_seconds=120,
            accel_default_cooldown_seconds=300,
            illuminance_cooldown_seconds=28_800,
        )
        active_database = database or self.database
        processor = EventProcessor(
            config,
            active_database,
            self.bocco,
            hermes or self.hermes,
            now=lambda: self.now,
            reaction_choice=lambda phrases: phrases[0],
        )
        return processor, EventWorker(config, active_database, processor)

    async def _process_accel_set(
        self,
        kinds: tuple[str, ...],
        request_prefix: str,
        *,
        database: EventDatabase | None = None,
        hermes: FakeHermes | AsyncMock | None = None,
    ) -> tuple[EventProcessor, EventWorker]:
        active_database = database or self.database
        processor, worker = self._ambient_runtime(
            database=active_database, hermes=hermes
        )
        for index, kind in enumerate(kinds):
            self.assertTrue(
                await active_database.enqueue(
                    FakeInboundEvent(
                        request_id=f"{request_prefix}-{index}",
                        event_type="accel.detected",
                        speech_text=None,
                        message_id=None,
                        message_media=None,
                        event_detail=kind,
                        received_at=datetime.fromtimestamp(self.now, tz=UTC),
                    )
                )
            )
        self.assertTrue(await worker.process_once())
        return processor, worker

    async def test_speech_calls_hermes_then_bocco_without_logging_text(self) -> None:
        event = FakeInboundEvent(
            request_id="speech-1",
            event_type="message.received",
            speech_text="本文に出してはいけない秘密の発話",
        )
        with self.assertLogs("bocco_bridge.events", level="INFO") as logs:
            await self._enqueue_and_process(event)
        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(self.hermes.responded[0][0], "bocco-room:room-1")
        self.assertEqual(
            self.hermes.responded[0][2], self.config.response_instructions
        )
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        self.assertNotIn(event.speech_text, "\n".join(logs.output))
        stored = await self.database.get_event("speech-1")
        assert stored is not None
        self.assertEqual(stored.status, "completed")

    async def test_truncated_generation_speaks_fallback_without_retrying(
        self,
    ) -> None:
        # Hermes ran out of output budget before finishing a sentence. The
        # bridge must not speak the fragment, and must not burn the user's
        # patience retrying into the same budget.
        self.hermes.incomplete_responses = 1
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="truncated-1",
                event_type="message.received",
                speech_text="長い説明をして",
            )
        )

        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(
            self.bocco.sent, [("room-1", self.config.error_fallback_text)]
        )
        stored = await self.database.get_event("truncated-1")
        assert stored is not None
        self.assertEqual(stored.status, "completed")
        self.assertEqual(stored.attempts, 1)

    async def test_ack_motion_fires_exactly_once_across_hermes_retries(self) -> None:
        self.hermes.response_failures = 2
        processor, worker = self._ack_runtime()
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="ack-once",
                    event_type="message.received",
                    speech_text="少し考えて答えて",
                )
            )
        )

        for _ in range(3):
            self.assertTrue(await worker.process_once())
            await processor.wait_for_acknowledgments()

        self.assertEqual(
            self.bocco.motion_attempts, [("room-1", "ack-motion-uuid")]
        )
        self.assertEqual(len(self.hermes.responded), 3)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        effects = await self.database.get_effects("ack-once")
        self.assertTrue(effects.ack_motion_attempted)

    async def test_recording_telemetry_is_silent_and_finished_fires_early_ack(
        self,
    ) -> None:
        processor, worker = self._ack_runtime()
        for request_id, event_type, timestamp in (
            ("recording-started", "recording.started", 990),
            ("recording-finished", "recording.finished", 1_000),
        ):
            self.now = float(timestamp)
            self.assertTrue(
                await self.database.enqueue(
                    FakeInboundEvent(
                        request_id=request_id,
                        event_type=event_type,
                        speech_text=None,
                        message_id=None,
                        message_media=None,
                        event_detail="record_button",
                        received_at=datetime.fromtimestamp(timestamp, tz=UTC),
                    )
                )
            )
            self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(self.bocco.sent, [])
        self.assertEqual(
            self.bocco.motion_attempts, [("room-1", "ack-motion-uuid")]
        )
        for request_id, timestamp in (
            ("recording-started", 990),
            ("recording-finished", 1_000),
        ):
            stored = await self.database.get_event(request_id)
            assert stored is not None
            self.assertEqual(stored.status, "completed")
            self.assertEqual(stored.received_at.timestamp(), timestamp)

    async def test_thinking_motion_disabled_by_default_dispatches_nothing(
        self,
    ) -> None:
        processor, worker = self._ack_runtime()
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-disabled",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )

        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(
            self.bocco.motion_sent, [("room-1", "ack-motion-uuid")]
        )
        self.assertEqual(self.bocco.custom_motion_attempts, [])

    async def test_thinking_motion_dispatches_after_configured_delay(
        self,
    ) -> None:
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            self.assertEqual(
                self.bocco.motion_sent, [("room-1", "ack-motion-uuid")]
            )
            delays.append(delay)

        sleep = AsyncMock(side_effect=fake_sleep)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_delay=0.75,
            thinking_max_dispatches=1,
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-enabled",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="INFO") as logs:
            self.assertTrue(await worker.process_once())
            await processor.wait_for_acknowledgments()

        self.assertEqual(delays, [0.75])
        self.assertEqual(
            self.bocco.custom_motion_sent,
            [("room-1", CUSTOM_MOTION_DOCUMENTS["かんがえちゅう"])],
        )
        output = "\n".join(logs.output)
        self.assertIn(
            "thinking_motion_sent request_id=thinking-enabled index=1", output
        )
        self.assertIn(
            "thinking_motion_stopped request_id=thinking-enabled reason=max_dispatches",
            output,
        )
        self.assertFalse(
            await self.database.room_reply_sent_since_recording(
                "thinking-enabled", 0.0
            )
        )

    async def test_thinking_motion_stops_when_reply_wins_webhook_race(
        self,
    ) -> None:
        question = Stamp("w10question", "stamp-question", "疑問", "question.png")
        skills = FakeFastRouteSkills()
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=2,
            thinking_stamp_name="w10question",
            stamps=(question,),
            sleep=sleep,
            fast_route_skills=skills,
        )
        self.now = 1_000.321
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-race-message",
                    event_type="message.received",
                    speech_text="今何時",
                    received_at=datetime.fromtimestamp(1_000, UTC),
                    message_id="thinking-race-audio",
                    message_media="audio",
                )
            )
        )
        self.assertTrue(await worker.process_once())
        self.assertEqual(skills.calls, [("time", "今何時", None)])
        self.assertEqual(self.hermes.responded, [])

        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-race-recording",
                    event_type="recording.finished",
                    speech_text=None,
                    received_at=datetime.fromtimestamp(1_000.007, UTC),
                    message_id=None,
                    message_media=None,
                )
            )
        )
        self.assertFalse(
            await self.database.recording_reply_sent(
                "thinking-race-recording"
            )
        )
        self.assertTrue(
            await self.database.room_reply_sent_since_recording(
                "thinking-race-recording", 0.0
            )
        )

        with self.assertLogs("bocco_bridge.events", level="INFO") as logs:
            self.assertTrue(await worker.process_once())
            await processor.wait_for_acknowledgments()

        self.assertEqual(
            self.bocco.motion_sent,
            [("room-1", "ack-motion-uuid")],
        )
        self.assertEqual(self.bocco.custom_motion_attempts, [])
        self.assertEqual(self.bocco.stamp_attempts, [])
        self.assertIn(
            "thinking_motion_stopped request_id=thinking-race-recording reason=reply_sent",
            "\n".join(logs.output),
        )

    async def test_thinking_motion_stops_when_correlated_reply_is_sent(
        self,
    ) -> None:
        sleep_started = asyncio.Event()
        release_sleep = asyncio.Event()

        async def controlled_sleep(_: float) -> None:
            sleep_started.set()
            await release_sleep.wait()

        sleep = AsyncMock(side_effect=controlled_sleep)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=1,
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-reply-recording",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )
        self.assertTrue(await worker.process_once())
        await asyncio.wait_for(sleep_started.wait(), timeout=LIVENESS_TIMEOUT)

        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-reply-message",
                    event_type="message.received",
                    speech_text="音声の質問です",
                    message_id="thinking-reply-audio",
                    message_media="audio",
                )
            )
        )
        self.assertTrue(await worker.process_once())
        self.assertFalse(
            await self.database.room_reply_sent_since_recording(
                "thinking-reply-recording", 0.0
            )
        )
        self.assertTrue(
            await self.database.recording_reply_sent(
                "thinking-reply-recording"
            )
        )
        with self.assertLogs("bocco_bridge.events", level="INFO") as logs:
            release_sleep.set()
            await processor.wait_for_acknowledgments()

        self.assertEqual(self.bocco.custom_motion_attempts, [])
        self.assertIn(
            "thinking_motion_stopped request_id=thinking-reply-recording reason=reply_sent",
            "\n".join(logs.output),
        )

    async def test_thinking_motion_respects_max_dispatches(self) -> None:
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=2,
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-max",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )

        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(len(self.bocco.custom_motion_sent), 2)
        # The repeat interval is the motion's own length, so derive it rather
        # than hardcoding it — retuning the document must not break this test.
        self.assertEqual(
            sleep.await_args_list,
            [call(1.5), call(motion_duration_seconds("かんがえちゅう"))],
        )

    async def test_thinking_motion_budget_exhaustion_is_quiet(self) -> None:
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            budget=1,
            thinking_enabled=True,
            thinking_max_dispatches=2,
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-budget",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="INFO") as logs:
            self.assertTrue(await worker.process_once())
            await processor.wait_for_acknowledgments()

        self.assertEqual(self.bocco.custom_motion_attempts, [])
        self.assertIn(
            "thinking_motion_stopped request_id=thinking-budget reason=budget",
            "\n".join(logs.output),
        )

    async def test_thinking_motion_failure_is_swallowed_and_reply_continues(
        self,
    ) -> None:
        self.bocco.custom_motion_failures = 1
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=1,
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-failure-recording",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )
        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            self.assertTrue(await worker.process_once())
            await processor.wait_for_acknowledgments()

        self.assertIn("thinking_motion_skipped", "\n".join(logs.output))
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-failure-message",
                    event_type="message.received",
                    speech_text="失敗しても答えて",
                    message_id="thinking-failure-audio",
                    message_media="audio",
                )
            )
        )
        self.assertTrue(await worker.process_once())

        self.assertEqual(self.bocco.custom_motion_sent, [])
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        stored = await self.database.get_event("thinking-failure-message")
        assert stored is not None
        self.assertEqual(stored.status, "completed")

    async def test_thinking_stamp_empty_config_sends_nothing(self) -> None:
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=1,
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-stamp-empty",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )

        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(len(self.bocco.custom_motion_sent), 1)
        self.assertEqual(self.bocco.stamp_attempts, [])

    async def test_thinking_stamp_absent_logs_once_and_motion_continues(
        self,
    ) -> None:
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=1,
            thinking_stamp_name="missing-stamp",
            sleep=sleep,
        )

        with self.assertLogs("bocco_bridge.events", level="INFO") as logs:
            for index in range(2):
                self.assertTrue(
                    await self.database.enqueue(
                        FakeInboundEvent(
                            request_id=f"thinking-stamp-missing-{index}",
                            event_type="recording.finished",
                            speech_text=None,
                            message_id=None,
                            message_media=None,
                        )
                    )
                )
                self.assertTrue(await worker.process_once())
                await processor.wait_for_acknowledgments()

        self.assertEqual(len(self.bocco.custom_motion_sent), 2)
        self.assertEqual(self.bocco.stamp_attempts, [])
        self.assertEqual(
            "\n".join(logs.output).count(
                "thinking_stamp_unavailable stamp_name=missing-stamp"
            ),
            1,
        )

    async def test_thinking_stamp_and_motion_dispatch_concurrently(self) -> None:
        stamp_started = asyncio.Event()
        release_stamp = asyncio.Event()
        motion_dispatched = asyncio.Event()
        question = Stamp("w10question", "stamp-question", "疑問", "question.png")
        original_custom_motion = self.bocco.send_custom_motion

        async def blocked_stamp(room_uuid: str, stamp_uuid: str) -> SentMessage:
            del room_uuid, stamp_uuid
            stamp_started.set()
            await release_stamp.wait()
            return SentMessage(message_id="slow-stamp-message")

        async def observed_motion(room_uuid: str, document) -> SentMessage:
            sent = await original_custom_motion(room_uuid, document)
            motion_dispatched.set()
            return sent

        self.bocco.send_stamp = AsyncMock(side_effect=blocked_stamp)
        self.bocco.send_custom_motion = AsyncMock(side_effect=observed_motion)
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=1,
            thinking_stamp_name="w10question",
            stamps=(question,),
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-stamp-concurrent",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )

        self.assertTrue(await worker.process_once())
        await asyncio.gather(
            asyncio.wait_for(stamp_started.wait(), timeout=LIVENESS_TIMEOUT),
            asyncio.wait_for(motion_dispatched.wait(), timeout=LIVENESS_TIMEOUT),
        )
        self.assertEqual(len(self.bocco.custom_motion_sent), 1)
        self.bocco.send_stamp.assert_awaited_once_with(
            "room-1", "stamp-question"
        )

        release_stamp.set()
        await processor.wait_for_acknowledgments()

    async def test_thinking_stamp_echo_is_suppressed_without_reply(self) -> None:
        question = Stamp("w10question", "stamp-question", "疑問", "question.png")
        self.bocco.stamp_message_ids = ["thinking-stamp-echo-id"]
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=1,
            thinking_stamp_name="w10question",
            stamps=(question,),
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-stamp-echo-source",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )
        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-stamp-echo",
                    event_type="message.received",
                    speech_text="疑問",
                    message_id="thinking-stamp-echo-id",
                    message_media="stamp",
                )
            )
        )
        self.assertTrue(await worker.process_once())

        self.assertEqual(
            self.bocco.stamp_sent,
            [("room-1", "stamp-question", None)],
        )
        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(self.bocco.sent, [])
        stored = await self.database.get_event("thinking-stamp-echo")
        assert stored is not None
        self.assertEqual(stored.status, "completed")

    async def test_thinking_stamp_sends_at_most_once_per_recording_gap(
        self,
    ) -> None:
        question = Stamp("w10question", "stamp-question", "疑問", "question.png")
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=2,
            thinking_stamp_name="w10question",
            stamps=(question,),
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-stamp-once",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )

        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(len(self.bocco.custom_motion_sent), 2)
        self.assertEqual(
            self.bocco.stamp_attempts,
            [("room-1", "stamp-question", None)],
        )
        self.assertFalse(
            await self.database.room_reply_sent_since_recording(
                "thinking-stamp-once", 0.0
            )
        )

    async def test_thinking_stamp_failure_is_swallowed_and_reply_continues(
        self,
    ) -> None:
        question = Stamp("w10question", "stamp-question", "疑問", "question.png")
        self.bocco.stamp_failures = 1
        sleep = AsyncMock(return_value=None)
        processor, worker = self._ack_runtime(
            thinking_enabled=True,
            thinking_max_dispatches=1,
            thinking_stamp_name="w10question",
            stamps=(question,),
            sleep=sleep,
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-stamp-failure-source",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )
        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            self.assertTrue(await worker.process_once())
            await processor.wait_for_acknowledgments()

        self.assertIn("thinking_motion_skipped", "\n".join(logs.output))
        self.assertIn("phase=stamp", "\n".join(logs.output))
        self.assertEqual(len(self.bocco.custom_motion_sent), 1)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="thinking-stamp-failure-message",
                    event_type="message.received",
                    speech_text="失敗しても答えて",
                    message_id="thinking-stamp-failure-audio",
                    message_media="audio",
                )
            )
        )
        self.assertTrue(await worker.process_once())
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    async def test_audio_message_logs_stt_latency_without_second_ack(self) -> None:
        processor, worker = self._ack_runtime()
        self.now = 1_000
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="early-ack-finished",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                    event_detail="record_button",
                    received_at=datetime.fromtimestamp(1_000, tz=UTC),
                )
            )
        )
        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.now = 1_012
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="early-ack-audio",
                    event_type="message.received",
                    speech_text="音声の質問です",
                    message_id="early-ack-audio-message",
                    message_media="audio",
                    received_at=datetime.fromtimestamp(1_012, tz=UTC),
                )
            )
        )
        with self.assertLogs("bocco_bridge.events", level="INFO") as logs:
            self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertIn("stt_latency=12.000", "\n".join(logs.output))
        self.assertEqual(
            self.bocco.motion_attempts, [("room-1", "ack-motion-uuid")]
        )
        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    async def test_app_text_without_recording_keeps_message_time_ack(self) -> None:
        processor, worker = self._ack_runtime()
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="app-text-ack",
                    event_type="message.received",
                    speech_text="アプリからの質問",
                    message_id="app-text-ack-message",
                    message_media="text",
                )
            )
        )

        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(
            self.bocco.motion_attempts, [("room-1", "ack-motion-uuid")]
        )

    async def test_ack_skips_commands_fast_routes_radar_and_non_speech(self) -> None:
        skills = FakeFastRouteSkills()
        memory = HouseholdMemory(self.config.memory_path)
        await memory.initialize()
        processor, worker = self._ack_runtime(
            fast_route_skills=skills, memory=memory
        )
        cases = (
            FakeInboundEvent(
                request_id="ack-skip-persona",
                event_type="message.received",
                speech_text="ペルソナ：明るく話す",
                message_id="ack-skip-persona-message",
            ),
            FakeInboundEvent(
                request_id="ack-skip-schedule",
                event_type="message.received",
                speech_text="よてい：08:00 おはよう",
                message_id="ack-skip-schedule-message",
            ),
            FakeInboundEvent(
                request_id="ack-skip-memory",
                event_type="message.received",
                speech_text="おぼえて：犬の名前はポチ",
                message_id="ack-skip-memory-message",
            ),
            FakeInboundEvent(
                request_id="ack-skip-fast-route",
                event_type="message.received",
                speech_text="天気",
                message_id="ack-skip-fast-route-message",
            ),
            FakeInboundEvent(
                request_id="ack-skip-radar",
                event_type="radar.detected",
                speech_text=None,
                message_id=None,
                message_media=None,
            ),
            FakeInboundEvent(
                request_id="ack-skip-motion",
                event_type="message.received",
                speech_text=None,
                message_id="ack-skip-motion-message",
                message_media="motion",
            ),
        )
        for event in cases:
            self.assertTrue(await self.database.enqueue(event))
            self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(skills.calls, [("weather", "天気", None)])
        self.assertEqual(self.bocco.motion_attempts, [])

    async def test_ack_budget_exhaustion_skips_later_event(self) -> None:
        processor, worker = self._ack_runtime(budget=1)
        for index in range(2):
            self.assertTrue(
                await self.database.enqueue(
                    FakeInboundEvent(
                        request_id=f"ack-budget-{index}",
                        event_type="message.received",
                        speech_text=f"普通の質問{index}",
                        message_id=f"ack-budget-message-{index}",
                    )
                )
            )
            self.assertTrue(await worker.process_once())
            await processor.wait_for_acknowledgments()

        self.assertEqual(
            self.bocco.motion_attempts, [("room-1", "ack-motion-uuid")]
        )
        self.assertTrue(
            (await self.database.get_effects("ack-budget-1")).ack_motion_attempted
        )

    async def test_ack_failure_is_harmless_to_conversational_reply(self) -> None:
        self.bocco.motion_failures = 1
        processor, worker = self._ack_runtime()
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="ack-failure",
                    event_type="message.received",
                    speech_text="普通の質問",
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="WARNING"):
            self.assertTrue(await worker.process_once())
            await processor.wait_for_acknowledgments()

        self.assertEqual(self.bocco.motion_sent, [])
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        stored = await self.database.get_event("ack-failure")
        assert stored is not None
        self.assertEqual(stored.status, "completed")

    async def test_ack_config_off_disables_motion(self) -> None:
        processor, worker = self._ack_runtime(enabled=False)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="ack-disabled",
                    event_type="message.received",
                    speech_text="普通の質問",
                )
            )
        )

        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(self.bocco.motion_attempts, [])
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    async def test_ack_send_does_not_delay_hermes_or_text_delivery(self) -> None:
        motion_started = asyncio.Event()
        release_motion = asyncio.Event()

        async def blocked_motion(_: str, __: str) -> None:
            motion_started.set()
            await release_motion.wait()

        self.bocco.send_motion = AsyncMock(side_effect=blocked_motion)
        processor, worker = self._ack_runtime()
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="ack-concurrent",
                    event_type="message.received",
                    speech_text="普通の質問",
                )
            )
        )

        processing = asyncio.create_task(worker.process_once())
        await asyncio.wait_for(motion_started.wait(), timeout=LIVENESS_TIMEOUT)
        self.assertTrue(await asyncio.wait_for(processing, timeout=LIVENESS_TIMEOUT))
        self.assertFalse(release_motion.is_set())
        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

        release_motion.set()
        await processor.wait_for_acknowledgments()

    async def test_fast_routes_send_skill_output_verbatim_without_hermes(
        self,
    ) -> None:
        skills = FakeFastRouteSkills()
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(self.config, self.database, processor)
        cases = (
            ("fast-weather", "今日の天気を教えて", "weather", None),
            ("fast-osaka", "大阪の天気", "weather", "大阪"),
            ("fast-tokyo", "東京の天気はどう？", "weather", "東京"),
            ("fast-time", "今日は何曜日ですか", "time", None),
            ("fast-news", "ニュースを教えて", "news", None),
        )
        for request_id, utterance, route, location in cases:
            self.assertTrue(
                await self.database.enqueue(
                    FakeInboundEvent(
                        request_id=request_id,
                        event_type="message.received",
                        speech_text=utterance,
                        message_id=f"{request_id}-message",
                    )
                )
            )
            await worker.process_once()
            self.assertEqual(skills.calls[-1], (route, utterance, location))
            self.assertEqual(
                self.bocco.sent[-1], ("room-1", skills.outputs[route])
            )

        self.assertEqual(len(skills.calls), 5)
        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(len(self.bocco.sent), 5)

    async def test_fast_route_output_is_trimmed_and_capped_at_speech_limit(
        self,
    ) -> None:
        skills = FakeFastRouteSkills()
        skills.outputs["time"] = "  今日は月曜日です。\n" + "あ" * 400 + "  \n"
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(self.config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-trim",
                    event_type="message.received",
                    speech_text="今何時",
                    message_id="fast-trim-message",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(self.hermes.responded, [])
        room, text = self.bocco.sent[0]
        self.assertEqual(room, "room-1")
        self.assertTrue(text.startswith("今日は月曜日です。"))
        self.assertEqual(len(text), self.config.max_speech_chars)
        self.assertFalse(text.endswith(" "))

    async def test_empty_skill_output_falls_through_to_normal_hermes(self) -> None:
        skills = FakeFastRouteSkills()
        skills.outputs["news"] = "   \n  "
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(self.config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-empty",
                    event_type="message.received",
                    speech_text="ニュース",
                    message_id="fast-empty-message",
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            await worker.process_once()

        self.assertIn("EmptySkillOutput", "\n".join(logs.output))
        self.assertEqual(self.hermes.responded[0][1], "ニュース")
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    async def test_phrasing_flag_restores_single_hermes_call_per_fast_route(
        self,
    ) -> None:
        skills = FakeFastRouteSkills()
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.config.database_path,
            tunnel_enabled=False,
            worker_max_attempts=3,
            worker_retry_base_seconds=0,
            fast_route_phrasing=True,
        )
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-phrasing",
                    event_type="message.received",
                    speech_text="ニュースを教えて",
                    message_id="fast-phrasing-message",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(skills.calls, [("news", "ニュースを教えて", None)])
        self.assertEqual(len(self.hermes.responded), 1)
        prompt = self.hermes.responded[0][1]
        self.assertIn("route=news", prompt)
        self.assertIn("<skill_data>", prompt)
        self.assertIn(skills.outputs["news"], prompt)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    async def test_fast_route_near_miss_uses_normal_hermes_path(self) -> None:
        skills = FakeFastRouteSkills()
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(self.config, self.database, processor)
        utterance = "天気の話をしよう"
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-route-near-miss",
                    event_type="message.received",
                    speech_text=utterance,
                    message_id="fast-route-near-miss-message",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(skills.calls, [])
        self.assertEqual(self.hermes.responded[0][1], utterance)

    async def test_missing_fast_route_script_falls_through_to_normal_hermes(
        self,
    ) -> None:
        skills = AsyncMock()
        skills.run.side_effect = FileNotFoundError("shared script path unavailable")
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(self.config, self.database, processor)
        utterance = "今何時"
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-route-failure",
                    event_type="message.received",
                    speech_text=utterance,
                    message_id="fast-route-failure-message",
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            await worker.process_once()

        skills.run.assert_awaited_once_with("time", utterance, location=None)
        self.assertEqual(self.hermes.responded[0][1], utterance)
        self.assertNotIn(utterance, "\n".join(logs.output))

    async def test_disabled_fast_routes_bypass_skill_runner(self) -> None:
        skills = FakeFastRouteSkills()
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.config.database_path,
            tunnel_enabled=False,
            fast_routes=frozenset(),
        )
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-route-disabled",
                    event_type="message.received",
                    speech_text="ニュース",
                    message_id="fast-route-disabled-message",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(skills.calls, [])
        self.assertEqual(self.hermes.responded[0][1], "ニュース")

    async def test_fast_routed_reply_echo_is_suppressed(self) -> None:
        skills = FakeFastRouteSkills()
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(self.config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-route-echo-source",
                    event_type="message.received",
                    speech_text="天気",
                    message_id="fast-route-echo-source-message",
                )
            )
        )
        await worker.process_once()
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-route-echo",
                    event_type="message.received",
                    speech_text=skills.outputs["weather"],
                    message_id="fake-outbound-1",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(skills.calls, [("weather", "天気", None)])
        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(
            self.bocco.sent, [("room-1", skills.outputs["weather"])]
        )

    async def test_skill_stdout_invisibles_reach_neither_speech_nor_hash(
        self,
    ) -> None:
        """The fast route speaks stdout with no model in between.

        ``fast_route_phrasing`` is off by default, so a fast-routed reply is
        whatever an external script printed, normalized once and sent. That
        makes stdout a likelier source of a byte-order mark than the model is,
        not a less likely one: a mark is what a UTF-8-with-BOM file or a copied
        upstream JSON body carries. This asserts the same two things the model
        path asserts — what reaches the device is clean, and the hash recorded
        for echo suppression is taken over that same clean text, so the reply
        coming back is still recognised as our own words rather than answered.
        """

        clean = "東京の今の天気は晴れです。"
        skills = FakeFastRouteSkills()
        skills.outputs["weather"] = f"{chr(0xFEFF)}東京の今の天気は{chr(0x200B)}晴れです。"
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(self.config, self.database, processor)
        # No id comes back from the send, which puts suppression on the content
        # hash rather than on the message id.
        self.bocco.message_ids = [None]
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-route-bom",
                    event_type="message.received",
                    speech_text="天気",
                    message_id="fast-route-bom-message",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(self.bocco.sent, [("room-1", clean)])
        self.assertEqual(
            await self.database.consume_outbound_echo(
                "room-1",
                None,
                clean,
                self.config.outbound_echo_window_seconds,
                now=self.now,
            ),
            "content_hash",
        )

    async def test_skill_stdout_of_only_invisibles_is_empty_output(
        self,
    ) -> None:
        """Invisible-only stdout is no answer, so the model gets its turn.

        Before format characters were stripped here this string was truthy after
        the whitespace pass, so it would have been delivered as the reply — the
        robot asked a question and given a string that says nothing at all.
        """

        skills = FakeFastRouteSkills()
        skills.outputs["weather"] = f"{chr(0xFEFF)}{chr(0x200B)}  {chr(0x2060)}"
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            fast_route_skills=skills,
        )
        worker = EventWorker(self.config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="fast-route-blank",
                    event_type="message.received",
                    speech_text="天気",
                    message_id="fast-route-blank-message",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    def _streaming_runtime(
        self,
        hermes: FakeStreamingHermes,
        *,
        stream_sentences: bool = True,
    ) -> tuple[EventProcessor, EventWorker]:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.config.database_path,
            tunnel_enabled=False,
            worker_max_attempts=3,
            worker_retry_base_seconds=0,
            stream_sentences=stream_sentences,
        )
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            hermes,
            now=lambda: self.now,
        )
        return processor, EventWorker(config, self.database, processor)

    async def test_streamed_reply_sends_each_sentence_as_its_own_message(
        self,
    ) -> None:
        hermes = FakeStreamingHermes(
            deltas=(
                "おはよう。今日",
                "は晴れです。",
                "散歩に行こう。",
                "元気ですか。",
            )
        )
        processor, worker = self._streaming_runtime(hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-1",
                    event_type="message.received",
                    speech_text="おはようの挨拶をして",
                    message_id="stream-1-message",
                )
            )
        )

        self.assertTrue(await worker.process_once())

        self.assertEqual(len(hermes.stream_calls), 1)
        self.assertEqual(hermes.stream_calls[0][0], "bocco-room:room-1")
        self.assertEqual(hermes.responded, [])
        self.assertEqual(
            self.bocco.sent,
            [
                ("room-1", "おはよう。"),
                ("room-1", "今日は晴れです。"),
                ("room-1", "散歩に行こう。元気ですか。"),
            ],
        )
        stored = await self.database.get_event("stream-1")
        assert stored is not None
        self.assertEqual(stored.status, "completed")
        effects = await self.database.get_effects("stream-1")
        self.assertTrue(effects.bocco_sent)
        self.assertEqual(
            effects.response_text,
            "おはよう。今日は晴れです。散歩に行こう。元気ですか。",
        )

    async def test_streamed_chunk_echoes_are_suppressed(self) -> None:
        hermes = FakeStreamingHermes(
            deltas=("一つ目です。", "二つ目です。", "三つ目です。")
        )
        processor, worker = self._streaming_runtime(hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-echo-source",
                    event_type="message.received",
                    speech_text="三つ話して",
                    message_id="stream-echo-source-message",
                )
            )
        )
        self.assertTrue(await worker.process_once())
        chunks = [text for _, text in self.bocco.sent]
        self.assertEqual(
            chunks, ["一つ目です。", "二つ目です。", "三つ目です。"]
        )

        # Exact message-id echoes for every chunk are suppressed.
        for index in range(3):
            self.assertTrue(
                await self.database.enqueue(
                    FakeInboundEvent(
                        request_id=f"stream-echo-id-{index}",
                        event_type="message.received",
                        speech_text=chunks[index],
                        message_id=f"fake-outbound-{index + 1}",
                    )
                )
            )
            self.assertTrue(await worker.process_once())
        self.assertEqual(len(self.bocco.sent), 3)
        self.assertEqual(len(hermes.stream_calls), 1)
        self.assertEqual(hermes.responded, [])

    async def test_streamed_chunk_hash_echo_without_message_id_is_suppressed(
        self,
    ) -> None:
        hermes = FakeStreamingHermes(
            deltas=("一つ目です。", "二つ目です。",)
        )
        self.bocco.message_ids = [None, None]
        processor, worker = self._streaming_runtime(hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-hash-source",
                    event_type="message.received",
                    speech_text="二つ話して",
                    message_id="stream-hash-source-message",
                )
            )
        )
        self.assertTrue(await worker.process_once())
        self.assertEqual(len(self.bocco.sent), 2)

        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-hash-echo",
                    event_type="message.received",
                    speech_text="二つ目です。",
                    message_id="platform-generated-id",
                )
            )
        )
        self.assertTrue(await worker.process_once())

        self.assertEqual(len(self.bocco.sent), 2)
        self.assertEqual(len(hermes.stream_calls), 1)
        self.assertEqual(hermes.responded, [])

    async def test_stream_flag_off_keeps_single_message_path(self) -> None:
        hermes = FakeStreamingHermes(
            deltas=("一つ目です。", "二つ目です。")
        )
        processor, worker = self._streaming_runtime(
            hermes, stream_sentences=False
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-off",
                    event_type="message.received",
                    speech_text="普通の質問",
                    message_id="stream-off-message",
                )
            )
        )

        self.assertTrue(await worker.process_once())

        self.assertEqual(hermes.stream_calls, [])
        self.assertEqual(len(hermes.responded), 1)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    async def test_stream_without_terminal_punctuation_sends_one_chunk(
        self,
    ) -> None:
        hermes = FakeStreamingHermes(deltas=("句点のない", "返事です"))
        processor, worker = self._streaming_runtime(hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-no-punct",
                    event_type="message.received",
                    speech_text="一言どうぞ",
                    message_id="stream-no-punct-message",
                )
            )
        )

        self.assertTrue(await worker.process_once())

        self.assertEqual(self.bocco.sent, [("room-1", "句点のない返事です")])
        effects = await self.database.get_effects("stream-no-punct")
        self.assertEqual(effects.response_text, "句点のない返事です")

    async def test_stream_failure_before_output_falls_back_to_single_call(
        self,
    ) -> None:
        hermes = FakeStreamingHermes(deltas=("使われない。",))
        hermes.stream_failures = 1
        processor, worker = self._streaming_runtime(hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-fallback",
                    event_type="message.received",
                    speech_text="普通の質問",
                    message_id="stream-fallback-message",
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            self.assertTrue(await worker.process_once())

        self.assertIn("stream_fallback", "\n".join(logs.output))
        self.assertEqual(len(hermes.stream_calls), 1)
        self.assertEqual(len(hermes.responded), 1)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        stored = await self.database.get_event("stream-fallback")
        assert stored is not None
        self.assertEqual(stored.status, "completed")

    async def test_stream_interruption_after_first_chunk_salvages_partial(
        self,
    ) -> None:
        hermes = FakeStreamingHermes(deltas=("やあ。続きです。", "こない"))
        hermes.fail_after_deltas = 1
        processor, worker = self._streaming_runtime(hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-partial",
                    event_type="message.received",
                    speech_text="長めの話をして",
                    message_id="stream-partial-message",
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            self.assertTrue(await worker.process_once())

        self.assertIn("stream_interrupted", "\n".join(logs.output))
        # The second sentence was already complete when the stream died, so
        # it is still spoken rather than silently discarded with the failure.
        self.assertEqual(
            self.bocco.sent, [("room-1", "やあ。"), ("room-1", "続きです。")]
        )
        self.assertEqual(hermes.responded, [])
        stored = await self.database.get_event("stream-partial")
        assert stored is not None
        self.assertEqual(stored.status, "completed")
        effects = await self.database.get_effects("stream-partial")
        self.assertTrue(effects.bocco_sent)
        self.assertEqual(effects.response_text, "やあ。続きです。")

    async def test_stream_interruption_never_speaks_an_unfinished_sentence(
        self,
    ) -> None:
        hermes = FakeStreamingHermes(deltas=("やあ。途中まで話し", "こない"))
        hermes.fail_after_deltas = 1
        processor, worker = self._streaming_runtime(hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-cut",
                    event_type="message.received",
                    speech_text="長めの話をして",
                    message_id="stream-cut-message",
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            self.assertTrue(await worker.process_once())

        self.assertIn("stream_interrupted", "\n".join(logs.output))
        # "途中まで話し" never reached a terminator, so it is dropped instead
        # of being spoken as though it were the end of the reply.
        self.assertEqual(self.bocco.sent, [("room-1", "やあ。")])
        effects = await self.database.get_effects("stream-cut")
        self.assertEqual(effects.response_text, "やあ。")

    async def test_clean_stream_still_speaks_a_reply_without_punctuation(
        self,
    ) -> None:
        # A reply that simply ends without 。 is complete, not truncated, so
        # the truncation guard must not eat it.
        hermes = FakeStreamingHermes(deltas=("はい", "そうだよ"))
        processor, worker = self._streaming_runtime(hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-clean-nopunct",
                    event_type="message.received",
                    speech_text="そうなの",
                    message_id="stream-clean-nopunct-message",
                )
            )
        )

        self.assertTrue(await worker.process_once())

        self.assertEqual(self.bocco.sent, [("room-1", "はいそうだよ")])

    async def test_streamed_reply_fires_single_ack_for_all_chunks(self) -> None:
        hermes = FakeStreamingHermes(
            deltas=("一つ目です。", "二つ目です。", "三つ目です。")
        )
        processor, worker = self._ack_runtime(hermes=hermes)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-ack",
                    event_type="message.received",
                    speech_text="三つ話して",
                    message_id="stream-ack-message",
                )
            )
        )

        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        self.assertEqual(
            self.bocco.motion_attempts, [("room-1", "ack-motion-uuid")]
        )
        self.assertEqual(len(self.bocco.sent), 3)
        effects = await self.database.get_effects("stream-ack")
        self.assertTrue(effects.ack_motion_attempted)

    async def test_streamed_chunk_cues_attach_to_their_own_chunk(self) -> None:
        hermes = FakeStreamingHermes(
            deltas=(
                "一文目です。",
                "[motion:うなずき]はい、そうです。",
            )
        )
        processor, worker = self._streaming_runtime(hermes)
        processor.motion_catalog.replace(
            (MotionPreset("YES_01", "yes-motion"),)
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stream-cues",
                    event_type="message.received",
                    speech_text="うなずいて",
                    message_id="stream-cues-message",
                )
            )
        )

        self.assertTrue(await worker.process_once())

        # Cue markers never reach the room; the cue's chain belongs to the
        # chunk that carried it.
        self.assertEqual(
            self.bocco.sent,
            [("room-1", "一文目です。"), ("room-1", "はい、そうです。")],
        )
        chain = await self.database.get_motion_chain("stream-cues")
        assert chain is not None
        self.assertEqual(chain.motion_uuids, ("yes-motion",))

    async def test_optional_sender_uuid_suppression_can_be_enabled(self) -> None:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            agent_user_uuid="separate-agent",
            database_path=self.config.database_path,
            tunnel_enabled=False,
        )
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            reaction_choice=lambda phrases: phrases[0],
        )
        worker = EventWorker(config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="sender-echo",
                    event_type="message.received",
                    sender_uuid="separate-agent",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(self.bocco.sent, [])

    async def test_exact_message_id_echo_replays_are_suppressed_with_shared_sender(
        self,
    ) -> None:
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="human-1",
                event_type="message.received",
                sender_uuid="shared-account",
                message_id="human-message-1",
            )
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="echo-1",
                event_type="message.received",
                sender_uuid="shared-account",
                speech_text="短い返事です。",
                message_id="fake-outbound-1",
            )
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="echo-1-replay",
                event_type="message.received",
                sender_uuid="shared-account",
                speech_text="短い返事です。",
                message_id="fake-outbound-1",
            )
        )

        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        echo = await self.database.get_event("echo-1")
        assert echo is not None
        self.assertEqual(echo.status, "completed")
        replay = await self.database.get_event("echo-1-replay")
        assert replay is not None
        self.assertEqual(replay.status, "completed")

    async def test_hash_echo_is_consumed_once_then_identical_user_text_is_processed(
        self,
    ) -> None:
        self.bocco.message_ids.append(None)
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="human-before-hash",
                event_type="message.received",
                sender_uuid="shared-account",
                message_id="human-before-hash-id",
            )
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="hash-echo",
                event_type="message.received",
                sender_uuid="shared-account",
                speech_text="短い返事です。",
                message_id="echo-id-not-returned-by-post",
            )
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="same-text-human",
                event_type="message.received",
                sender_uuid="shared-account",
                speech_text="短い返事です。",
                message_id="different-human-message",
            )
        )

        self.assertEqual(len(self.hermes.responded), 2)
        self.assertEqual(len(self.bocco.sent), 2)

    async def test_hash_echo_suppression_survives_database_reopen(self) -> None:
        self.bocco.message_ids.append(None)
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="before-restart",
                event_type="message.received",
                message_id="before-restart-human-id",
            )
        )
        reopened = EventDatabase(self.config.database_path)
        await reopened.initialize()
        restarted_processor = EventProcessor(
            self.config,
            reopened,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
        )
        restarted_worker = EventWorker(self.config, reopened, restarted_processor)
        self.assertTrue(
            await reopened.enqueue(
                FakeInboundEvent(
                    request_id="echo-after-restart",
                    event_type="message.received",
                    speech_text="短い返事です。",
                    message_id="echo-after-restart-id",
                )
            )
        )

        await restarted_worker.process_once()

        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(len(self.bocco.sent), 1)

    async def test_hash_echo_outside_window_is_not_suppressed(self) -> None:
        self.bocco.message_ids.append(None)
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="before-expiry",
                event_type="message.received",
                message_id="before-expiry-human-id",
            )
        )
        self.now += self.config.outbound_echo_window_seconds + 1

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="late-identical-message",
                event_type="message.received",
                speech_text="短い返事です。",
                message_id="late-identical-message-id",
            )
        )

        self.assertEqual(len(self.hermes.responded), 2)
        self.assertEqual(len(self.bocco.sent), 2)

    async def test_empty_stt_uses_fixed_fallback_without_hermes(self) -> None:
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="stt-empty",
                event_type="message.received",
                speech_text="",
                message_media="audio",
            )
        )
        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(self.bocco.sent[0][1], self.config.stt_fallback_text)

    async def test_non_speech_messages_with_null_text_complete_silently(self) -> None:
        for media in ("motion", "stamp", "image", "text", None):
            request_id = f"non-speech-{media or 'unknown'}"
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=request_id,
                    event_type="message.received",
                    speech_text=None,
                    message_id=f"{request_id}-message",
                    message_media=media,
                )
            )
            event = await self.database.get_event(request_id)
            assert event is not None
            self.assertEqual(event.status, "completed")

        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(self.bocco.sent, [])

    async def test_persona_command_accepts_all_three_prefixes_without_hermes(
        self,
    ) -> None:
        commands = (
            "ペルソナ：明るい性格",
            "ペルソナ: 落ち着いた性格",
            "PeRsOnA: 好奇心旺盛な性格",
        )
        expected = ("明るい性格", "落ち着いた性格", "好奇心旺盛な性格")

        for index, (command, persona) in enumerate(zip(commands, expected), start=1):
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=f"persona-prefix-{index}",
                    event_type="message.received",
                    speech_text=command,
                    message_id=f"persona-prefix-message-{index}",
                )
            )
            self.assertEqual(await self.database.get_setting("persona"), persona)

        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(
            self.bocco.sent,
            [("room-1", PERSONA_CHANGED_TEXT)] * len(commands),
        )
        stored = await self.database.get_event("persona-prefix-3")
        assert stored is not None
        self.assertEqual(stored.status, "completed")

    async def test_persona_command_replaces_then_clears_stored_value(self) -> None:
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="persona-first",
                event_type="message.received",
                speech_text="ペルソナ：元気な性格",
                message_id="persona-first-message",
            )
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="persona-replace",
                event_type="message.received",
                speech_text="persona: 穏やかな性格",
                message_id="persona-replace-message",
            )
        )
        self.assertEqual(await self.database.get_setting("persona"), "穏やかな性格")

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="persona-clear",
                event_type="message.received",
                speech_text="ペルソナ:   ",
                message_id="persona-clear-message",
            )
        )

        self.assertIsNone(await self.database.get_setting("persona"))
        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(self.bocco.sent[-1], ("room-1", PERSONA_RESET_TEXT))

    async def test_persona_presets_store_profiles_and_name_confirmation(self) -> None:
        for index, (name, profile) in enumerate(PERSONA_PRESETS.items(), start=1):
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=f"persona-preset-{index}",
                    event_type="message.received",
                    speech_text=f"ペルソナ：{name}",
                    message_id=f"persona-preset-message-{index}",
                )
            )

            self.assertEqual(await self.database.get_setting("persona"), profile)
            self.assertEqual(profile.count("。"), 3)
            self.assertEqual(
                self.bocco.sent[-1],
                ("room-1", f"性格を「{name}」に変更しました！"),
            )

        self.assertEqual(self.hermes.responded, [])

    async def test_nonpreset_persona_remains_free_text(self) -> None:
        free_text = "宇宙が好きで、星の話を楽しむ性格です。"

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="persona-free-text",
                event_type="message.received",
                speech_text=f"persona: {free_text}",
                message_id="persona-free-text-message",
            )
        )

        self.assertEqual(await self.database.get_setting("persona"), free_text)
        self.assertEqual(self.bocco.sent, [("room-1", PERSONA_CHANGED_TEXT)])
        self.assertEqual(self.hermes.responded, [])

    async def test_stored_persona_overrides_env_then_clear_restores_default(
        self,
    ) -> None:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.config.database_path,
            tunnel_enabled=False,
            robot_nickname="コロン",
            persona="環境の性格",
        )
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            reaction_choice=lambda phrases: phrases[0],
        )
        worker = EventWorker(config, self.database, processor)
        await self.database.set_setting("persona", "チャットで設定した性格")
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stored-persona-query",
                    event_type="message.received",
                    speech_text="今の性格を教えて",
                    message_id="stored-persona-query-message",
                )
            )
        )
        await worker.process_once()
        await self.database.clear_setting("persona")
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="env-persona-query",
                    event_type="message.received",
                    speech_text="もう一度教えて",
                    message_id="env-persona-query-message",
                )
            )
        )
        await worker.process_once()

        stored_instructions = self.hermes.responded[0][2]
        default_instructions = self.hermes.responded[1][2]
        for instructions in (stored_instructions, default_instructions):
            self.assertTrue(instructions.startswith(BASE_SPEECH_INSTRUCTIONS))
            self.assertIn(
                "あなたは「コロン」という名前のロボットです。", instructions
            )
        self.assertIn("チャットで設定した性格", stored_instructions)
        self.assertNotIn("環境の性格", stored_instructions)
        self.assertIn("環境の性格", default_instructions)

    async def test_duplicate_persona_command_and_confirmation_echo_apply_once(
        self,
    ) -> None:
        command = FakeInboundEvent(
            request_id="persona-dedup",
            event_type="message.received",
            speech_text="persona: のんびりした性格",
            message_id="persona-dedup-message",
        )
        self.assertTrue(await self.database.enqueue(command))
        self.assertFalse(await self.database.enqueue(command))
        await self.worker.process_once()
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="persona-confirmation-echo",
                event_type="message.received",
                speech_text=PERSONA_CHANGED_TEXT,
                message_id="fake-outbound-1",
            )
        )

        self.assertEqual(
            await self.database.get_setting("persona"), "のんびりした性格"
        )
        self.assertEqual(self.bocco.sent, [("room-1", PERSONA_CHANGED_TEXT)])
        self.assertEqual(self.hermes.responded, [])

    async def test_oversized_persona_is_rejected_without_replacing_setting(
        self,
    ) -> None:
        await self.database.set_setting("persona", "現在の性格")

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="persona-too-long",
                event_type="message.received",
                speech_text="ペルソナ：" + ("長" * 501),
                message_id="persona-too-long-message",
            )
        )

        self.assertEqual(await self.database.get_setting("persona"), "現在の性格")
        self.assertEqual(self.bocco.sent, [("room-1", PERSONA_TOO_LONG_TEXT)])
        self.assertEqual(self.hermes.responded, [])

    async def test_memory_commands_accept_prefixes_store_list_and_forget_without_hermes(
        self,
    ) -> None:
        commands = (
            ("memory-hiragana", "おぼえて：猫の名前はミケ"),
            ("memory-kanji", "覚えて：犬の好物はさつまいも"),
            ("memory-english", "REMEMBER: 母の誕生日は五月三日"),
        )
        for request_id, text in commands:
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=request_id,
                    event_type="message.received",
                    speech_text=text,
                    message_id=f"{request_id}-message",
                )
            )

        facts = await self.processor.memory.list_active("room-1")
        self.assertEqual(len(facts), 3)
        self.assertTrue(all(reply[1] == MEMORY_REMEMBERED_TEXT for reply in self.bocco.sent))

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="memory-list",
                event_type="message.received",
                speech_text="記憶：リスト",
                message_id="memory-list-message",
            )
        )
        self.assertIn("猫の名前はミケ", self.bocco.sent[-1][1])
        self.assertIn("犬の好物はさつまいも", self.bocco.sent[-1][1])

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="memory-forget",
                event_type="message.received",
                speech_text="forget: 猫の名前",
                message_id="memory-forget-message",
            )
        )
        self.assertEqual(self.bocco.sent[-1], ("room-1", "1件の記憶を忘れました。"))
        self.assertNotIn(
            "猫の名前はミケ",
            [fact.text for fact in await self.processor.memory.list_active("room-1")],
        )
        self.assertEqual(self.hermes.responded, [])

    async def test_memory_retrieval_is_room_scoped_and_appended_after_persona(
        self,
    ) -> None:
        await self.database.set_setting("persona", "静かで親切な性格です。")
        await self.processor.memory.remember(
            "room-1", "猫の名前はミケ", "memory-room-1", created_at=self.now
        )
        await self.processor.memory.remember(
            "room-2", "猫の名前はタマ", "memory-room-2", created_at=self.now
        )

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="memory-query",
                event_type="message.received",
                room_uuid="room-1",
                speech_text="猫の名前を教えて",
                message_id="memory-query-message",
            )
        )

        instructions = self.hermes.responded[0][2]
        self.assertTrue(instructions.startswith(BASE_SPEECH_INSTRUCTIONS))
        self.assertLess(instructions.index("静かで親切"), instructions.index("[家庭の長期記憶]"))
        self.assertIn("・猫の名前はミケ", instructions)
        self.assertNotIn("タマ", instructions)
        self.assertIn("[/家庭の長期記憶]", instructions)

    async def test_superseded_memory_is_not_injected(self) -> None:
        await self.processor.memory.remember(
            "room-1", "猫の名前はミケ", "memory-old-name", created_at=self.now
        )
        await self.processor.memory.remember(
            "room-1", "猫の名前はタマ", "memory-new-name", created_at=self.now + 1
        )

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="memory-superseded-query",
                event_type="message.received",
                speech_text="猫の名前を教えて",
                message_id="memory-superseded-query-message",
            )
        )

        instructions = self.hermes.responded[0][2]
        self.assertIn("猫の名前はタマ", instructions)
        self.assertNotIn("猫の名前はミケ", instructions)

    async def test_duplicate_memory_command_and_confirmation_echo_apply_once(
        self,
    ) -> None:
        command = FakeInboundEvent(
            request_id="memory-command-dedup",
            event_type="message.received",
            speech_text="remember: 家の色は白",
            message_id="memory-command-dedup-message",
        )
        self.assertTrue(await self.database.enqueue(command))
        self.assertFalse(await self.database.enqueue(command))
        await self.worker.process_once()
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="memory-confirmation-echo",
                event_type="message.received",
                speech_text=MEMORY_REMEMBERED_TEXT,
                message_id="fake-outbound-1",
            )
        )

        facts = await self.processor.memory.list_active("room-1")
        self.assertEqual([fact.text for fact in facts], ["家の色は白"])
        self.assertEqual(self.bocco.sent, [("room-1", MEMORY_REMEMBERED_TEXT)])
        self.assertEqual(self.hermes.responded, [])

    async def test_oversized_memory_is_rejected_without_storage(self) -> None:
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="memory-too-long",
                event_type="message.received",
                speech_text="おぼえて：" + ("長" * 501),
                message_id="memory-too-long-message",
            )
        )

        self.assertEqual(await self.processor.memory.list_active("room-1"), ())
        self.assertEqual(self.bocco.sent, [("room-1", MEMORY_TOO_LONG_TEXT)])
        self.assertEqual(self.hermes.responded, [])

    async def test_schedule_add_list_and_remove_commands_skip_hermes(self) -> None:
        commands = (
            ("schedule-add-custom", "よてい：08:00 薬の時間を知らせて"),
            ("schedule-add-briefing-ja", "予定：09:00 ブリーフィング"),
            ("schedule-add-briefing-en", "schedule:10:00 briefing"),
        )
        for request_id, text in commands:
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=request_id,
                    event_type="message.received",
                    speech_text=text,
                    message_id=f"{request_id}-message",
                )
            )

        schedules = await self.database.list_schedules("room-1")
        self.assertEqual(
            [(item.local_time, item.kind, item.prompt_text) for item in schedules],
            [
                ("08:00", "custom", "薬の時間を知らせて"),
                ("09:00", "briefing", ""),
                ("10:00", "briefing", ""),
            ],
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="schedule-list",
                event_type="message.received",
                speech_text="schedule:list",
                message_id="schedule-list-message",
            )
        )
        listed = self.bocco.sent[-1][1]
        self.assertIn("08:00 薬の時間を知らせて", listed)
        self.assertIn("09:00 ブリーフィング", listed)
        self.assertIn("10:00 ブリーフィング", listed)

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="schedule-remove-hiragana",
                event_type="message.received",
                speech_text="よてい：けし 08:00",
                message_id="schedule-remove-hiragana-message",
            )
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="schedule-remove-kanji",
                event_type="message.received",
                speech_text="予定：削除 09:00",
                message_id="schedule-remove-kanji-message",
            )
        )

        remaining = await self.database.list_schedules("room-1")
        self.assertEqual(
            [(item.local_time, item.kind) for item in remaining],
            [("10:00", "briefing")],
        )
        self.assertEqual(self.hermes.responded, [])

    async def test_custom_schedule_uses_persona_and_normal_delivery_path(self) -> None:
        await self.database.set_setting("persona", "朝は元気に話す性格です。")
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="internal:schedule:1:2026-08-03",
                event_type="schedule.custom",
                speech_text="朝のストレッチを促してください。",
                message_id=None,
            )
        )

        self.assertEqual(len(self.hermes.responded), 1)
        conversation, prompt, instructions = self.hermes.responded[0]
        self.assertEqual(conversation, "bocco-room:room-1")
        self.assertEqual(prompt, "朝のストレッチを促してください。")
        self.assertIn("朝は元気に話す性格です。", instructions)
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="scheduled-reply-echo",
                event_type="message.received",
                speech_text="短い返事です。",
                message_id="fake-outbound-1",
            )
        )
        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(len(self.bocco.sent), 1)

    async def test_briefing_schedule_prompt_includes_pi_local_date_and_skills(
        self,
    ) -> None:
        received_at = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="internal:schedule:2:2026-08-03",
                event_type="schedule.briefing",
                speech_text="",
                received_at=received_at,
                message_id=None,
            )
        )

        self.assertEqual(len(self.hermes.responded), 1)
        _, prompt, _ = self.hermes.responded[0]
        local_date = received_at.astimezone()
        self.assertIn(
            f"{local_date.year}年{local_date.month}月{local_date.day}日", prompt
        )
        self.assertIn("天気", prompt)
        self.assertIn("ニュース", prompt)
        self.assertIn("短い朝のブリーフィング一発話", prompt)

    async def test_scheduled_hermes_failure_completes_silently(self) -> None:
        upstream_error = "HTTP401: Missing Authentication Header"
        hermes = AsyncMock()
        hermes.respond.side_effect = RuntimeError(upstream_error)
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            hermes,
            now=lambda: self.now,
        )
        worker = EventWorker(self.config, self.database, processor)
        request_id = "internal:schedule:3:2026-08-03"
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id=request_id,
                    event_type="schedule.custom",
                    speech_text="失敗する予定",
                    message_id=None,
                )
            )
        )

        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            for _ in range(self.config.worker_max_attempts):
                await worker.process_once()

        event = await self.database.get_event(request_id)
        assert event is not None
        self.assertEqual(event.status, "completed")
        self.assertEqual(self.bocco.sent, [])
        self.assertIsNone((await self.database.get_effects(request_id)).response_text)
        self.assertNotIn(upstream_error, "\n".join(logs.output))

    async def test_message_hermes_401_500_timeout_only_speaks_fixed_apology(
        self,
    ) -> None:
        failures = (
            RuntimeError("HTTP401: Missing Authentication Header"),
            RuntimeError("HTTP500: upstream-body-must-not-be-spoken"),
            TimeoutError("provider timeout detail must not be spoken"),
        )
        for index, failure in enumerate(failures, start=1):
            hermes = AsyncMock()
            hermes.respond.side_effect = failure
            processor = EventProcessor(
                self.config,
                self.database,
                self.bocco,
                hermes,
                now=lambda: self.now,
            )
            worker = EventWorker(self.config, self.database, processor)
            request_id = f"message-hermes-failure-{index}"
            self.assertTrue(
                await self.database.enqueue(
                    FakeInboundEvent(
                        request_id=request_id,
                        event_type="message.received",
                        message_id=f"message-hermes-failure-id-{index}",
                    )
                )
            )

            for _ in range(self.config.worker_max_attempts):
                await worker.process_once()

            event = await self.database.get_event(request_id)
            assert event is not None
            self.assertEqual(event.status, "completed")
            effects = await self.database.get_effects(request_id)
            self.assertEqual(effects.response_text, self.config.error_fallback_text)

        self.assertEqual(
            self.bocco.sent,
            [("room-1", self.config.error_fallback_text)] * len(failures),
        )

    async def test_error_like_hermes_output_is_retried_then_replaced_by_apology(
        self,
    ) -> None:
        upstream_error = "HTTP401: Missing Authentication Header"
        self.hermes.response = upstream_error
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="hermes-error-output",
                    event_type="message.received",
                    message_id="hermes-error-output-message",
                )
            )
        )

        for _ in range(self.config.worker_max_attempts):
            await self.worker.process_once()

        self.assertEqual(
            self.bocco.sent, [("room-1", self.config.error_fallback_text)]
        )
        self.assertNotIn(upstream_error, self.bocco.sent[0][1])
        effects = await self.database.get_effects("hermes-error-output")
        self.assertEqual(effects.response_text, self.config.error_fallback_text)

    async def test_accel_reacts_immediately_from_cold_bank_without_hermes(
        self,
    ) -> None:
        started = time.monotonic()
        await self._process_accel_set(("lift",), "instant-lift")
        elapsed = time.monotonic() - started

        # A ceiling on wall clock, not a latency budget. What "immediately from
        # the cold bank" means is asserted on the two lines below: no model was
        # called, and the phrase came from the packaged bank. Both are exact.
        # This one guards the failure those two cannot see — a debounce timer or
        # a sleep reintroduced on the reaction path, which is what this replaced
        # and which would cost whole seconds.
        #
        # It measured 0.246 s on a shared two-core CI runner, against the 0.2 s
        # it used to assert, and failed a build for it. Timing that a loaded
        # machine can lose is not evidence of anything, and a check that flakes
        # gets re-run rather than read.
        self.assertLess(elapsed, 1.0)
        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(
            self.bocco.sent, [("room-1", DEFAULT_REACTION_PHRASES["lift"][0])]
        )

    async def test_accel_dispatches_packaged_motion_in_same_processing_pass(self) -> None:
        await self._process_accel_set(("lift",), "instant-motion")
        self.assertEqual(len(self.bocco.custom_motion_sent), 1)
        self.assertEqual(self.bocco.custom_motion_sent[0][0], "room-1")
        self.assertEqual(
            self.bocco.custom_motion_sent[0][1],
            CUSTOM_MOTION_DOCUMENTS["surprise-realisation"],
        )

    async def test_same_second_accel_batch_uses_drama_priority_once(self) -> None:
        await self._process_accel_set(
            ("normal", "lift", "upside_down", "shaken", "dropped"), "same-batch"
        )

        self.assertEqual(
            self.bocco.sent, [("room-1", SERIOUS_ACCEL_REACTIONS["dropped"])]
        )
        effects = await self.database.get_effects("same-batch-0")
        self.assertEqual(effects.motion_cues, (("custom:しょんぼり", 0),))
        self.assertEqual(self.hermes.responded, [])
        for index in range(1, 5):
            peer = await self.database.get_event(f"same-batch-{index}")
            assert peer is not None
            self.assertEqual(peer.status, "completed")

    async def test_isolated_state_returns_use_only_lift_phrase_lookup(self) -> None:
        phrase_lookup = AsyncMock(return_value="うわ、うごいた気がする。")
        with patch.object(EventProcessor, "_reaction_phrase", phrase_lookup):
            await self._process_accel_set(("normal",), "isolated-normal")
            self.now += 121
            await self._process_accel_set(("lying_down",), "isolated-lying-down")

        self.assertEqual(
            self.bocco.sent,
            [
                ("room-1", "うわ、うごいた気がする。"),
                ("room-1", "うわ、うごいた気がする。"),
            ],
        )
        self.assertEqual(phrase_lookup.await_args_list, [call("lift"), call("lift")])

    async def test_settle_within_lift_cooldown_is_suppressed(self) -> None:
        await self._process_accel_set(("lift",), "lift-before-settle")
        self.now += 3
        await self._process_accel_set(("lying_down",), "settle-after-lift")

        self.assertEqual(
            self.bocco.sent, [("room-1", DEFAULT_REACTION_PHRASES["lift"][0])]
        )

    async def test_coalesced_lift_wins_over_normal(self) -> None:
        await self._process_accel_set(("normal", "lift"), "lift-and-normal")

        self.assertEqual(
            self.bocco.sent, [("room-1", DEFAULT_REACTION_PHRASES["lift"][0])]
        )

    async def test_trailing_accel_is_suppressed_by_global_cooldown(self) -> None:
        await self._process_accel_set(("lift",), "trailing-first")
        self.now += 1
        await self._process_accel_set(("shaken",), "trailing-second")

        self.assertEqual(len(self.bocco.sent), 1)
        self.assertEqual(self.hermes.responded, [])

    async def test_dropped_within_five_seconds_bypasses_lesser_cooldown(
        self,
    ) -> None:
        await self._process_accel_set(("lift",), "drop-override-first")
        self.now += 4
        await self._process_accel_set(("dropped",), "drop-override-safety")
        self.now += 1
        await self._process_accel_set(("dropped",), "drop-override-repeat")

        self.assertEqual(
            self.bocco.sent,
            [
                ("room-1", DEFAULT_REACTION_PHRASES["lift"][0]),
                ("room-1", SERIOUS_ACCEL_REACTIONS["dropped"]),
            ],
        )
        self.assertEqual(self.hermes.responded, [])

    async def test_accel_cooldown_persists_across_restart(self) -> None:
        await self._process_accel_set(("lift",), "cooldown-first")

        reopened = EventDatabase(self.config.database_path)
        await reopened.initialize()
        self.now += 1
        await self._process_accel_set(
            ("beaten",), "cooldown-after-restart", database=reopened
        )

        self.assertEqual(len(self.bocco.sent), 1)
        self.assertEqual(self.hermes.responded, [])

    async def test_accel_delivery_retry_does_not_lose_reserved_reaction(self) -> None:
        processor, worker = self._ambient_runtime()
        self.bocco.send_failures = 1
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="accel-delivery-retry",
                    event_type="accel.detected",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                    event_detail="lift",
                    received_at=datetime.fromtimestamp(self.now, tz=UTC),
                )
            )
        )

        self.assertTrue(await worker.process_once())
        self.assertTrue(await worker.process_once())

        self.assertEqual(
            self.bocco.sent, [("room-1", DEFAULT_REACTION_PHRASES["lift"][0])]
        )
        self.assertEqual(self.hermes.responded, [])
        await processor.stop_background_tasks()

    async def test_persona_change_queues_reaction_bank_for_background_generator(
        self,
    ) -> None:
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="persona-bank-change",
                event_type="message.received",
                speech_text="ペルソナ：元気な性格",
                message_id="persona-bank-change-message",
            )
        )
        self.assertEqual(self.hermes.responded, [])
        instructions = self.config.compose_response_instructions("元気な性格")
        persona_hash = composed_persona_hash(instructions)
        queued = await self.database.get_event(
            "internal:reaction-bank:persona-bank-change"
        )
        assert queued is not None
        self.assertEqual(queued.status, "pending")
        detail = json.loads(queued.event_detail or "")
        self.assertEqual(detail["persona_hash"], persona_hash)
        self.assertEqual(detail["completed_keys"], [])
        self.assertFalse(await self.worker.process_once())
        self.assertEqual(self.hermes.responded, [])

    async def test_event_worker_never_claims_reaction_bank_refresh(
        self,
    ) -> None:
        instructions = self.config.compose_response_instructions()
        persona_hash = composed_persona_hash(instructions)
        old = ("前の一", "前の二", "前の三", "前の四", "前の五")
        await self.database.put_reaction_phrases(persona_hash, "lift", old)
        self.hermes.response = "not json"
        self.assertTrue(
            await self.processor.schedule_reaction_bank_refresh(
                "strict-failure", "room-1"
            )
        )

        self.assertFalse(await self.worker.process_once())

        self.assertEqual(
            await self.database.get_reaction_phrases(persona_hash, "lift"), old
        )
        self.assertEqual(self.hermes.responded, [])

    async def test_accel_random_choice_uses_persona_bank(self) -> None:
        instructions = self.config.compose_response_instructions()
        persona_hash = composed_persona_hash(instructions)
        phrases = ("一", "二", "三", "四", "最後")
        await self.database.put_reaction_phrases(persona_hash, "lift", phrases)
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            reaction_choice=lambda choices: choices[-1],
        )
        worker = EventWorker(self.config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="bank-choice",
                    event_type="accel.detected",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                    event_detail="lift",
                    received_at=datetime.fromtimestamp(self.now, tz=UTC),
                )
            )
        )

        self.assertTrue(await worker.process_once())

        self.assertEqual(self.bocco.sent, [("room-1", "最後")])
        self.assertEqual(self.hermes.responded, [])

    async def test_illuminance_reacts_only_in_local_morning_and_evening(self) -> None:
        local_timezone = datetime.now().astimezone().tzinfo
        assert local_timezone is not None
        await self.database.set_setting("persona", "季節を楽しむ性格です。")

        cases = (
            ("morning-light", "brighter", 7, "room-morning", True),
            ("midday-light", "brighter", 12, "room-midday", False),
            ("night-light", "darker", 21, "room-night", True),
            ("afternoon-dark", "darker", 15, "room-afternoon", False),
        )
        for request_id, kind, hour, room_uuid, should_react in cases:
            self.now = datetime(
                2026, 8, 3, hour, 0, tzinfo=local_timezone
            ).timestamp()
            before = len(self.hermes.responded)
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=request_id,
                    event_type="illuminance.changed",
                    room_uuid=room_uuid,
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                    event_detail=kind,
                )
            )
            self.assertEqual(
                len(self.hermes.responded), before + (1 if should_react else 0)
            )

        self.assertEqual(len(self.hermes.responded), 2)
        morning_prompt = self.hermes.responded[0][1]
        night_prompt = self.hermes.responded[1][1]
        self.assertIn("7時", morning_prompt)
        self.assertIn("朝の目覚め", morning_prompt)
        self.assertIn("21時", night_prompt)
        self.assertIn("おやすみ前", night_prompt)
        self.assertTrue(
            all(
                "季節を楽しむ性格です。" in call[2]
                for call in self.hermes.responded
            )
        )
        self.assertEqual(len(self.bocco.sent), 2)

    async def test_illuminance_hermes_failure_is_silent_and_cools_down(
        self,
    ) -> None:
        local_timezone = datetime.now().astimezone().tzinfo
        assert local_timezone is not None
        self.now = datetime(2026, 8, 3, 7, 0, tzinfo=local_timezone).timestamp()
        hermes = FakeHermes("unused")
        hermes.response_failures = self.config.worker_max_attempts
        processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            hermes,
            now=lambda: self.now,
        )
        worker = EventWorker(self.config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="morning-light-failure",
                    event_type="illuminance.changed",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                    event_detail="brighter",
                )
            )
        )

        for _ in range(self.config.worker_max_attempts):
            await worker.process_once()

        self.assertEqual(self.bocco.sent, [])
        self.assertFalse(
            await self.database.cooldown_ready(
                "illuminance:room-1", now=self.now
            )
        )
        event = await self.database.get_event("morning-light-failure")
        assert event is not None
        self.assertEqual(event.status, "completed")

    async def test_radar_uses_time_bucket_banks_without_hermes(self) -> None:
        local_timezone = datetime.now().astimezone().tzinfo
        assert local_timezone is not None
        instructions = self.config.compose_response_instructions()
        persona_hash = composed_persona_hash(instructions)
        expected: list[tuple[str, str]] = []
        for index, hour in enumerate((7, 13, 21), start=1):
            self.now = datetime(2026, 8, 3, hour, tzinfo=local_timezone).timestamp()
            event_key = radar_reaction_key(hour)
            phrases = tuple(f"{event_key}-{item}" for item in range(5))
            await self.database.put_reaction_phrases(
                persona_hash, event_key, phrases
            )
            room_uuid = f"radar-bucket-{index}"
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=f"radar-bucket-event-{index}",
                    event_type="radar.detected",
                    room_uuid=room_uuid,
                )
            )
            expected.append((room_uuid, phrases[0]))

        self.assertEqual(self.bocco.sent, expected)
        self.assertEqual(self.hermes.responded, [])

    async def test_radar_completes_after_bocco_and_cooldown_without_discord(self) -> None:
        original_set_cooldown = self.database.set_cooldown
        cooldown_attempts = 0

        async def set_cooldown_after_one_failure(
            behavior_key: str, duration_seconds: float
        ) -> None:
            nonlocal cooldown_attempts
            cooldown_attempts += 1
            if cooldown_attempts == 1:
                raise RuntimeError("fake cooldown failure")
            await original_set_cooldown(behavior_key, duration_seconds)

        self.database.set_cooldown = AsyncMock(
            side_effect=set_cooldown_after_one_failure
        )
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(request_id="radar-1", event_type="radar.detected")
            )
        )
        await self.worker.process_once()
        await self.worker.process_once()
        local_hour = datetime.fromtimestamp(self.now, tz=UTC).astimezone().hour
        expected = DEFAULT_REACTION_PHRASES[radar_reaction_key(local_hour)][0]
        self.assertEqual(self.bocco.sent, [("room-1", expected)])
        self.assertEqual(self.hermes.responded, [])
        event = await self.database.get_event("radar-1")
        assert event is not None
        self.assertEqual(event.status, "completed")
        self.assertEqual(self.database.set_cooldown.await_count, 2)
        self.assertFalse(await self.database.cooldown_ready("radar:room-1"))

        self.database.set_cooldown = original_set_cooldown
        await self._enqueue_and_process(
            FakeInboundEvent(request_id="radar-2", event_type="radar.detected")
        )
        self.assertEqual(len(self.bocco.sent), 1)
        self.assertEqual(self.hermes.responded, [])

    async def test_radar_bank_key_uses_composed_persona_hash(
        self,
    ) -> None:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.config.database_path,
            tunnel_enabled=False,
            robot_nickname="コロン",
            persona="明るく親しみやすい性格です。",
        )
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            reaction_choice=lambda phrases: phrases[0],
        )
        worker = EventWorker(config, self.database, processor)
        await self.database.set_setting("persona", "静かで思慮深い性格です。")
        instructions = config.compose_response_instructions(
            "静かで思慮深い性格です。"
        )
        persona_hash = composed_persona_hash(instructions)
        expected_hour = datetime.fromtimestamp(self.now, tz=UTC).astimezone().hour
        event_key = radar_reaction_key(expected_hour)
        phrases = ("専用の挨拶", "二", "三", "四", "五")
        await self.database.put_reaction_phrases(persona_hash, event_key, phrases)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="radar-time-aware",
                    event_type="radar.detected",
                )
            )
        )

        await worker.process_once()

        self.assertEqual(self.bocco.sent, [("room-1", "専用の挨拶")])
        self.assertEqual(self.hermes.responded, [])
        self.assertIn("あなたは「コロン」という名前のロボットです。", instructions)
        self.assertIn("静かで思慮深い性格です。", instructions)
        self.assertNotIn("明るく親しみやすい性格です。", instructions)

    async def test_a_cue_tag_holding_a_bom_never_becomes_the_words(self) -> None:
        """The live silence, end to end: reply, wire, hash, transcript.

        The model wrote a zero-width no-break space inside the tag. The cue
        pattern is built from ``\\s``, which does not match format characters, so
        it missed, nothing was stripped, and the raw tag went out as the text to
        say — the robot nodded its delivery animation and made no intelligible
        sound. This asserts the whole path, not just the parse: what reaches the
        device is speech, and the SHA-256 recorded for outbound echo suppression
        is taken over that same clean text. A hash holding an invisible character
        the device does not echo back would leave the bridge unable to recognise
        its own words, and it would answer itself.
        """

        spoken = "昨日の13時41分ごろだよ。"
        self.hermes.response = f"[{chr(0xFEFF)}motion:ふつう]{spoken}"
        # No message id comes back from the send, which is what puts echo
        # suppression on the content hash rather than on the id.
        self.bocco.message_ids = [None]
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="bom-cue",
                event_type="message.received",
                speech_text="いつだったっけ",
            )
        )

        self.assertEqual(self.bocco.sent, [("room-1", spoken)])
        self.assertEqual(
            await self.database.consume_outbound_echo(
                "room-1",
                None,
                spoken,
                self.config.outbound_echo_window_seconds,
                now=self.now,
            ),
            "content_hash",
        )

    async def test_an_echo_carrying_an_invisible_is_still_our_own_reply(
        self,
    ) -> None:
        """Both sides of the echo hash are normalized, so neither can drift.

        The reply goes out clean; if the copy the device sends back has picked up
        a format character on the way, it still has to be recognised, because the
        alternative is the robot treating its own words as something to answer.
        """

        self.bocco.message_ids = [None]
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="echo-invisible",
                event_type="message.received",
                speech_text="普通の質問です",
            )
        )
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="echo-invisible-back",
                event_type="message.received",
                speech_text=f"短い返事です{chr(0x200B)}。",
                message_id="echoed-without-id",
            )
        )

        self.assertEqual(len(self.bocco.sent), 1)

    async def test_cueless_reply_gets_default_motion(self) -> None:
        self.processor.motion_catalog.replace(
            (
                MotionPreset("YES_01", "yes-motion"),
                MotionPreset("ALRIGHT_01", "alright-motion"),
            )
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="cueless-motion",
                event_type="message.received",
                speech_text="普通の質問です",
            )
        )

        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        effects = await self.database.get_effects("cueless-motion")
        self.assertEqual(len(effects.motion_cues), 1)
        self.assertIn(
            effects.motion_cues[0][0], {"yes-motion", "alright-motion"}
        )
        self.assertEqual(effects.motion_cues[0][1], 2)

    async def test_default_reply_motion_config_off_keeps_reply_motionless(
        self,
    ) -> None:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.config.database_path,
            tunnel_enabled=False,
            worker_max_attempts=3,
            worker_retry_base_seconds=0,
            default_reply_motion=False,
        )
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            motion_catalog=MotionCatalog(
                (MotionPreset("YES_01", "yes-motion"),)
            ),
        )
        worker = EventWorker(config, self.database, processor)
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="cueless-motion-off",
                    event_type="message.received",
                    speech_text="普通の質問です",
                )
            )
        )
        self.assertTrue(await worker.process_once())

        effects = await self.database.get_effects("cueless-motion-off")
        self.assertFalse(effects.motion_cues)

    async def test_inline_cues_are_stripped_and_chain_advances_on_finished_events(
        self,
    ) -> None:
        self.processor.motion_catalog.replace(
            (
                MotionPreset("GOOD_01", "good-motion"),
                MotionPreset("YES_01", "yes-motion"),
            )
        )
        self.hermes.response = (
            "やった[motion:うれしい]ね[motion:うなずき]"
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="inline-chain",
                event_type="message.received",
                speech_text="うれしいニュースです",
            )
        )

        self.assertEqual(self.bocco.sent, [("room-1", "やったね")])
        self.assertEqual(self.bocco.motion_sent, [])
        effects = await self.database.get_effects("inline-chain")
        self.assertIn(effects.motion_cues[0][0], {"good-motion", "yes-motion"})
        self.assertEqual(effects.motion_cues[0][1], 3)
        self.assertEqual(effects.motion_cues[1], ("yes-motion", 4))
        first_motion = effects.motion_cues[0][0]

        self.now = 1_002.0
        await self.worker.process_once()
        await self.worker.process_once()
        self.assertEqual(self.bocco.motion_sent, [("room-1", first_motion)])

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="talk-finished-inline",
                event_type="emo_talk.finished",
                speech_text=None,
                event_detail="やったね",
                received_at=datetime.fromtimestamp(1_002, tz=UTC),
            )
        )
        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(self.bocco.sent, [("room-1", "やったね")])

        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="motion-finished-first",
                event_type="motion.finished",
                speech_text=None,
                event_detail={
                    "good-motion": "GOOD_01",
                    "yes-motion": "YES_01",
                }[first_motion],
                received_at=datetime.fromtimestamp(1_003, tz=UTC),
            )
        )
        self.assertEqual(
            self.bocco.motion_sent,
            [("room-1", first_motion), ("room-1", "yes-motion")],
        )
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="motion-finished-second",
                event_type="motion.finished",
                speech_text=None,
                event_detail="YES_01",
                received_at=datetime.fromtimestamp(1_004, tz=UTC),
            )
        )
        chain = await self.database.get_motion_chain("inline-chain")
        assert chain is not None
        self.assertEqual(chain.status, "completed")
        calibration = await self.database.get_speech_calibration(
            "room-1", self.processor.cold_calibration
        )
        self.assertEqual(calibration.sample_count, 1)

    async def test_custom_motion_cue_plays_one_document_through_the_chain(
        self,
    ) -> None:
        self.hermes.response = "なになに[motion:きょろきょろ]？"
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="custom-chain",
                event_type="message.received",
                speech_text="きょろきょろして",
            )
        )

        self.assertEqual(self.bocco.sent, [("room-1", "なになに？")])
        effects = await self.database.get_effects("custom-chain")
        self.assertEqual(
            effects.motion_cues, (("custom:きょろきょろ", 4),)
        )

        self.now = 1_003.0
        self.assertTrue(await self.worker.process_once())

        self.assertEqual(self.bocco.motion_sent, [])
        self.assertEqual(
            self.bocco.custom_motion_sent,
            [("room-1", CUSTOM_MOTION_DOCUMENTS["きょろきょろ"])],
        )
        # No motion.finished ever arrives for custom documents, so the
        # entry self-finishes and the single-entry chain completes.
        chain = await self.database.get_motion_chain("custom-chain")
        assert chain is not None
        self.assertEqual(chain.status, "completed")
        self.assertEqual(chain.finished_count, 1)

    async def test_custom_motion_success_drains_already_due_successor(self) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="mixed-custom-chain", event_type="message.received"
            )
        )
        await self.database.ensure_motion_chain(
            "mixed-custom-chain",
            "room-1",
            (
                ("preset-before", 1_000.0),
                ("custom:しょんぼり", 1_000.0),
                ("preset-after", 1_000.0),
            ),
            self.config.motion_chain_timeout_seconds,
            motion_kinds=("PRESET_BEFORE", None, "PRESET_AFTER"),
            now=self.now,
        )

        first = await self.database.dispatch_due_motion(
            "mixed-custom-chain",
            0,
            self.config.motion_chain_timeout_seconds,
            self.config.motion_budget_per_minute,
            now=self.now,
        )
        assert first is not None
        await self.processor._send_motion_dispatch(first)
        # Mimic the queued index-1/index-2 due events being consumed while
        # the first preset still waits for motion.finished.
        self.assertIsNone(
            await self.database.dispatch_due_motion(
                "mixed-custom-chain",
                1,
                self.config.motion_chain_timeout_seconds,
                self.config.motion_budget_per_minute,
                now=self.now,
            )
        )
        self.assertIsNone(
            await self.database.dispatch_due_motion(
                "mixed-custom-chain",
                2,
                self.config.motion_chain_timeout_seconds,
                self.config.motion_budget_per_minute,
                now=self.now,
            )
        )

        self.now = 1_001.0
        custom = await self.database.advance_motion_chain(
            "motion.finished",
            "room-1",
            "PRESET_BEFORE",
            self.config.motion_chain_timeout_seconds,
            self.config.motion_budget_per_minute,
            now=self.now,
        )
        assert custom is not None
        await self.processor._send_motion_dispatch(custom)

        self.assertEqual(
            self.bocco.motion_sent,
            [("room-1", "preset-before"), ("room-1", "preset-after")],
        )
        self.assertEqual(
            self.bocco.custom_motion_sent,
            [("room-1", CUSTOM_MOTION_DOCUMENTS["しょんぼり"])],
        )
        chain = await self.database.get_motion_chain("mixed-custom-chain")
        assert chain is not None
        self.assertEqual((chain.sent_count, chain.finished_count), (3, 2))
        self.assertEqual(chain.status, "active")

    async def test_custom_motion_send_failure_abandons_only_the_chain(self) -> None:
        self.bocco.custom_motion_failures = 1
        self.hermes.response = "ごめんね[motion:しょんぼり]"
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="custom-send-failure",
                event_type="message.received",
                speech_text="しょんぼりして",
            )
        )

        self.now = 1_003.0
        self.assertTrue(await self.worker.process_once())

        self.assertEqual(self.bocco.sent, [("room-1", "ごめんね")])
        self.assertEqual(len(self.bocco.custom_motion_attempts), 1)
        self.assertEqual(self.bocco.custom_motion_sent, [])
        chain = await self.database.get_motion_chain("custom-send-failure")
        assert chain is not None
        self.assertEqual((chain.sent_count, chain.finished_count), (1, 0))
        self.assertEqual(chain.status, "abandoned")

    async def test_composite_cue_expands_into_ordered_preset_chain(self) -> None:
        self.processor.motion_catalog.replace(
            (
                MotionPreset("WHAT_01", "what-motion"),
                MotionPreset("YES_01", "yes-motion"),
                MotionPreset("GOOD_01", "good-motion"),
            )
        )
        self.hermes.response = "えっ[motion:びっくりよろこび]すごい！"
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="composite-chain",
                event_type="message.received",
                speech_text="サプライズがあるよ",
            )
        )

        self.assertEqual(self.bocco.sent, [("room-1", "えっすごい！")])
        effects = await self.database.get_effects("composite-chain")
        self.assertEqual(
            effects.motion_cues,
            (("what-motion", 2), ("yes-motion", 2), ("good-motion", 2)),
        )

        self.now = 1_003.0
        self.assertTrue(await self.worker.process_once())
        self.assertEqual(self.bocco.motion_sent, [("room-1", "what-motion")])

        for index, (finished_kind, expected) in enumerate(
            (("WHAT_01", "yes-motion"), ("YES_01", "good-motion")), start=1
        ):
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=f"composite-finished-{index}",
                    event_type="motion.finished",
                    speech_text=None,
                    event_detail=finished_kind,
                    received_at=datetime.fromtimestamp(1_003 + index, tz=UTC),
                )
            )
            self.assertEqual(
                self.bocco.motion_sent[-1], ("room-1", expected)
            )

    async def test_custom_motion_budget_guard_skips_document_not_speech(
        self,
    ) -> None:
        processor, worker = self._ack_runtime(budget=1)
        self.hermes.response = "ごめんね[motion:しょんぼり]"
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="custom-budget",
                    event_type="message.received",
                    speech_text="しょんぼりして",
                )
            )
        )
        self.assertTrue(await worker.process_once())
        await processor.wait_for_acknowledgments()

        # The acknowledgment consumed the whole one-call budget.
        self.assertEqual(self.bocco.motion_sent, [("room-1", "ack-motion-uuid")])
        self.assertEqual(self.bocco.sent, [("room-1", "ごめんね")])

        self.now = 1_003.0
        self.assertTrue(await worker.process_once())

        self.assertEqual(self.bocco.custom_motion_sent, [])
        self.assertEqual(self.bocco.sent, [("room-1", "ごめんね")])
        chain = await self.database.get_motion_chain("custom-budget")
        assert chain is not None
        self.assertEqual(chain.status, "abandoned")

    async def test_new_message_motion_reanchors_cues_without_hermes_or_speech(
        self,
    ) -> None:
        started_at = time.time() + 10
        self.now = started_at
        self.processor.motion_catalog.replace(
            (MotionPreset("YES_01", "yes-motion"),)
        )
        self.hermes.response = "はい[motion:うなずき]"
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="anchored-chain",
                event_type="message.received",
                speech_text="返事をして",
            )
        )
        before = await self.database.get_motion_chain("anchored-chain")
        assert before is not None
        self.assertIsNone(before.anchored_at)

        anchor_at = started_at + 0.2
        await self._enqueue_and_process(
            FakeInboundEvent(
                request_id="new-message-anchor",
                event_type="motion.finished",
                speech_text=None,
                message_id=None,
                message_media=None,
                event_detail="newMessageMotion",
                received_at=datetime.fromtimestamp(anchor_at, tz=UTC),
            )
        )

        anchored = await self.database.get_motion_chain("anchored-chain")
        assert anchored is not None
        # received_at round-trips through datetime with microsecond
        # precision, so compare with a microsecond tolerance.
        self.assertAlmostEqual(anchored.anchored_at, anchor_at, delta=1e-5)
        self.assertAlmostEqual(
            anchored.due_times[0], anchor_at + 0.3, delta=1e-5
        )
        self.assertEqual(self.bocco.motion_sent, [])
        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(self.bocco.sent, [("room-1", "はい")])
        signal = await self.database.get_event("new-message-anchor")
        assert signal is not None
        self.assertEqual(signal.status, "completed")
        calibration = await self.database.get_speech_calibration(
            "room-1", self.processor.cold_calibration
        )
        self.assertEqual(calibration.sample_count, 1)

    async def test_motion_failure_is_garnish_and_event_completion_is_unchanged(
        self,
    ) -> None:
        self.processor.motion_catalog.replace(
            (MotionPreset("YES_01", "yes-motion"),)
        )
        self.hermes.response = "はい[motion:うなずき]。"
        self.bocco.motion_failures = 1
        await self._enqueue_and_process(
            FakeInboundEvent(request_id="motion-failure", event_type="message.received")
        )
        self.now = 1_002

        with self.assertLogs("bocco_bridge.events", level="WARNING") as logs:
            await self.worker.process_once()

        source = await self.database.get_event("motion-failure")
        due = await self.database.get_event("internal:motion:motion-failure:0")
        assert source is not None and due is not None
        self.assertEqual(source.status, "completed")
        self.assertEqual(due.status, "completed")
        self.assertEqual(self.bocco.sent, [("room-1", "はい。")])
        self.assertEqual(self.bocco.motion_sent, [])
        self.assertIn("motion_delivery_skipped", "\n".join(logs.output))

    async def test_finished_signals_without_a_chain_are_silent(self) -> None:
        for index, event_type in enumerate(
            ("emo_talk.finished", "motion.finished"), start=1
        ):
            await self._enqueue_and_process(
                FakeInboundEvent(
                    request_id=f"orphan-finished-{index}",
                    event_type=event_type,
                    speech_text=None,
                    event_detail="orphan",
                )
            )

        self.assertEqual(self.hermes.responded, [])
        self.assertEqual(self.bocco.sent, [])
        self.assertEqual(self.bocco.motion_sent, [])

    def test_radar_scene_cue_is_time_appropriate(self) -> None:
        self.assertEqual(radar_scene_cue(8).name, "GoodMorning")
        self.assertEqual(radar_scene_cue(14).name, "YES")
        self.assertEqual(radar_scene_cue(22).name, "GoodNight")


if __name__ == "__main__":
    unittest.main()
