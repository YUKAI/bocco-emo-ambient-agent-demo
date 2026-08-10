import os
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bocco_bridge.db import EventDatabase
from bocco_bridge.choreography import SpeechCalibration
from runtime.fakes import FakeInboundEvent


class EventDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.db"
        self.database = EventDatabase(self.path)
        await self.database.initialize()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_duplicate_request_id_is_inserted_once(self) -> None:
        event = FakeInboundEvent(request_id="same", event_type="message.received")
        self.assertTrue(await self.database.enqueue(event))
        self.assertFalse(await self.database.enqueue(event))
        self.assertEqual(await self.database.queue_counts(), {"pending": 1})

    async def test_recording_finished_outranks_a_co_pending_message(self) -> None:
        """The early ack must not queue behind the reply's model call.

        Observed live 2026-08-04: both events landed together, message.received
        was claimed first, and its 4.06s model call delayed the acknowledgment
        until after the reply had already been spoken.
        """

        await self.database.enqueue(
            FakeInboundEvent(
                request_id="speech", event_type="message.received"
            )
        )
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="ack", event_type="recording.finished"
            )
        )

        first = await self.database.claim_next()
        assert first is not None
        self.assertEqual(first.request_id, "ack")
        self.assertEqual(first.event_type, "recording.finished")
        self.assertEqual(first.priority, 110)
        await self.database.complete(first.request_id)

        second = await self.database.claim_next()
        assert second is not None
        self.assertEqual(second.request_id, "speech")
        self.assertEqual(second.priority, 100)

    async def test_user_priority_preserves_message_order_then_drains_ambient(
        self,
    ) -> None:
        ambient = (
            ("ambient-radar", "radar.detected"),
            ("ambient-accel", "accel.detected"),
            ("ambient-schedule", "schedule.custom"),
        )
        for request_id, event_type in ambient:
            await self.database.enqueue(
                FakeInboundEvent(request_id=request_id, event_type=event_type)
            )
        for request_id in ("user-first", "user-second"):
            await self.database.enqueue(
                FakeInboundEvent(request_id=request_id, event_type="message.received")
            )

        claimed_ids: list[str] = []
        for _ in range(5):
            event = await self.database.claim_next()
            self.assertIsNotNone(event)
            assert event is not None
            claimed_ids.append(event.request_id)
            if event.event_type == "message.received":
                self.assertEqual(event.priority, 100)
            elif event.event_type in {"radar.detected", "accel.detected"}:
                self.assertEqual(event.priority, 50)
            else:
                self.assertEqual(event.event_type, "schedule.custom")
                self.assertEqual(event.priority, -100)
            await self.database.complete(event.request_id)

        self.assertEqual(claimed_ids[:2], ["user-first", "user-second"])
        self.assertEqual(
            claimed_ids[2:],
            ["ambient-radar", "ambient-accel", "ambient-schedule"],
        )

    async def test_processing_event_recovers_after_restart(self) -> None:
        await self.database.enqueue(FakeInboundEvent(request_id="recover", event_type="radar.detected"))
        claimed = await self.database.claim_next()
        self.assertIsNotNone(claimed)

        reopened = EventDatabase(self.path)
        await reopened.initialize()
        self.assertEqual(await reopened.recover_interrupted(), 1)
        resumed = await reopened.claim_next()
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(resumed.request_id, "recover")
        self.assertEqual(resumed.attempts, 2)

    async def test_effect_checkpoints_and_cooldown_are_durable(self) -> None:
        await self.database.enqueue(FakeInboundEvent(request_id="effects", event_type="radar.detected"))
        self.assertEqual(
            await self.database.save_response_if_absent("effects", "first"), "first"
        )
        self.assertEqual(
            await self.database.save_response_if_absent("effects", "second"), "first"
        )
        await self.database.mark_bocco_sent("effects")
        effects = await self.database.get_effects("effects")
        self.assertEqual(effects.response_text, "first")
        self.assertTrue(effects.bocco_sent)

        self.assertTrue(await self.database.cooldown_ready("radar:room", now=100.0))
        await self.database.set_cooldown("radar:room", 30.0, now=100.0)
        self.assertFalse(await self.database.cooldown_ready("radar:room", now=129.0))
        self.assertTrue(await self.database.cooldown_ready("radar:room", now=130.0))

    async def test_ack_motion_is_reserved_once_and_shares_durable_budget(self) -> None:
        for request_id in ("ack-first", "ack-budgeted"):
            await self.database.enqueue(
                FakeInboundEvent(request_id=request_id, event_type="message.received")
            )

        first = await self.database.reserve_ack_motion(
            "ack-first", "room-1", "ack-motion", 1, now=100.0
        )
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.motion_uuid, "ack-motion")
        self.assertTrue(
            (await self.database.get_effects("ack-first")).ack_motion_attempted
        )

        reopened = EventDatabase(self.path)
        await reopened.initialize()
        self.assertIsNone(
            await reopened.reserve_ack_motion(
                "ack-first", "room-1", "ack-motion", 1, now=101.0
            )
        )
        self.assertIsNone(
            await reopened.reserve_ack_motion(
                "ack-budgeted", "room-1", "ack-motion", 1, now=101.0
            )
        )
        self.assertTrue(
            (await reopened.get_effects("ack-budgeted")).ack_motion_attempted
        )

    async def test_runtime_setting_is_durable_replaceable_and_clearable(self) -> None:
        self.assertIsNone(await self.database.get_setting("persona"))
        await self.database.set_setting("persona", "明るい性格", updated_at=100.0)
        await self.database.set_setting("persona", "落ち着いた性格", updated_at=101.0)

        reopened = EventDatabase(self.path)
        await reopened.initialize()
        self.assertEqual(await reopened.get_setting("persona"), "落ち着いた性格")

        await reopened.clear_setting("persona")
        self.assertIsNone(await self.database.get_setting("persona"))

    async def test_recording_finished_correlation_is_durable_and_one_shot(
        self,
    ) -> None:
        finished_at = datetime.fromtimestamp(100, tz=timezone.utc)
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="recording-finished",
                event_type="recording.finished",
                speech_text=None,
                message_id=None,
                message_media=None,
                event_detail="record_button",
                received_at=finished_at,
            )
        )
        claimed = await self.database.claim_next()
        assert claimed is not None
        await self.database.complete(claimed.request_id)

        reopened = EventDatabase(self.path)
        await reopened.initialize()
        message_at = datetime.fromtimestamp(112, tz=timezone.utc)
        first = await reopened.correlate_recent_recording_finished(
            "audio-message", "room-1", message_at, 15, now=113
        )
        assert first is not None
        self.assertEqual(first.stt_latency_seconds, 12)
        self.assertTrue(first.newly_created)

        retry = await reopened.correlate_recent_recording_finished(
            "audio-message", "room-1", message_at, 15, now=114
        )
        assert retry is not None
        self.assertEqual(retry.stt_latency_seconds, 12)
        self.assertFalse(retry.newly_created)
        self.assertIsNone(
            await reopened.correlate_recent_recording_finished(
                "second-message", "room-1", message_at, 15, now=115
            )
        )

    async def test_outbound_id_is_reusable_and_hash_is_durable_one_shot(
        self,
    ) -> None:
        await self.database.enqueue(
            FakeInboundEvent(request_id="source-id", event_type="message.received")
        )
        await self.database.record_bocco_delivery(
            "source-id", "room-1", "reply", "outbound-id", 600, sent_at=100
        )
        reopened = EventDatabase(self.path)
        await reopened.initialize()

        self.assertEqual(
            await reopened.consume_outbound_echo(
                "room-1", "outbound-id", "reply", 600, now=101
            ),
            "message_id",
        )
        self.assertEqual(
            await reopened.consume_outbound_echo(
                "room-1", "outbound-id", "reply", 600, now=102
            ),
            "message_id",
        )

        await reopened.enqueue(
            FakeInboundEvent(request_id="source-delayed-id", event_type="message.received")
        )
        await reopened.record_bocco_delivery(
            "source-delayed-id", "room-1", "delayed reply", "delayed-id", 600, sent_at=100
        )
        self.assertEqual(
            await reopened.consume_outbound_echo(
                "room-1", "delayed-id", "different text", 600, now=10_000
            ),
            "message_id",
        )

        await reopened.enqueue(
            FakeInboundEvent(request_id="source-hash", event_type="message.received")
        )
        await reopened.record_bocco_delivery(
            "source-hash", "room-1", "same text", None, 600, sent_at=200
        )
        self.assertEqual(
            await reopened.consume_outbound_echo(
                "room-1", "unrelated-inbound-id", "same text", 600, now=201
            ),
            "content_hash",
        )
        self.assertIsNone(
            await reopened.consume_outbound_echo(
                "room-1", "another-user-id", "same text", 600, now=202
            )
        )

        await reopened.enqueue(
            FakeInboundEvent(request_id="source-expired", event_type="message.received")
        )
        await reopened.record_bocco_delivery(
            "source-expired", "room-1", "old reply", None, 600, sent_at=300
        )
        self.assertIsNone(
            await reopened.consume_outbound_echo(
                "room-1", "late-echo", "old reply", 600, now=901
            )
        )

    async def test_room_reply_since_recording_is_order_independent_and_scoped(
        self,
    ) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="other-room-source",
                event_type="message.received",
                room_uuid="room-2",
            )
        )
        await self.database.record_bocco_delivery(
            "other-room-source",
            "room-2",
            "other room reply",
            "other-room-outbound",
            600,
            sent_at=101,
        )
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="early-source",
                event_type="message.received",
            )
        )
        await self.database.record_bocco_delivery(
            "early-source",
            "room-1",
            "old reply",
            "early-outbound",
            600,
            sent_at=99,
        )
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="ordering-recording",
                event_type="recording.finished",
                speech_text=None,
                received_at=datetime.fromtimestamp(100, tz=timezone.utc),
                message_id=None,
                message_media=None,
            )
        )
        self.assertFalse(
            await self.database.room_reply_sent_since_recording(
                "ordering-recording", 0.0
            )
        )

        await self.database.enqueue(
            FakeInboundEvent(
                request_id="late-source",
                event_type="message.received",
            )
        )
        await self.database.record_bocco_delivery(
            "late-source",
            "room-1",
            "new reply",
            "late-outbound",
            600,
            sent_at=100.25,
        )
        self.assertTrue(
            await self.database.room_reply_sent_since_recording(
                "ordering-recording", 0.0
            )
        )

        await self.database.enqueue(
            FakeInboundEvent(
                request_id="stream-recording",
                event_type="recording.finished",
                room_uuid="room-3",
                speech_text=None,
                received_at=datetime.fromtimestamp(200, tz=timezone.utc),
                message_id=None,
                message_media=None,
            )
        )
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="stream-source",
                event_type="message.received",
                room_uuid="room-3",
            )
        )
        await self.database.record_stream_chunk_delivery(
            "stream-source",
            0,
            "room-3",
            "streamed reply",
            "stream-outbound",
            600,
            sent_at=201,
        )
        self.assertTrue(
            await self.database.room_reply_sent_since_recording(
                "stream-recording", 0.0
            )
        )

    async def test_a_reply_just_before_the_recording_event_counts_with_lookback(
        self,
    ) -> None:
        """The webhook race, which is the common case rather than the rare one.

        An utterance's audio ``message.received`` routinely arrives before its
        own ``recording.finished`` — measured 29 of 40 pairs, by 1-6 seconds,
        none later. A fast route answers in ~300 ms, so the reply is sent and
        done before the recording event lands. Looking only forward from the
        recording event cannot see that reply, and the robot then plays a
        considering gesture at somebody who has already been answered.
        """

        await self.database.enqueue(
            FakeInboundEvent(
                request_id="raced-source",
                event_type="message.received",
                room_uuid="room-9",
            )
        )
        await self.database.record_bocco_delivery(
            "raced-source",
            "room-9",
            "answered already",
            "raced-outbound",
            600,
            sent_at=95,
        )
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="raced-recording",
                event_type="recording.finished",
                room_uuid="room-9",
                speech_text=None,
                received_at=datetime.fromtimestamp(100, tz=timezone.utc),
                message_id=None,
                message_media=None,
            )
        )

        # Forward-only: invisible, which is the bug.
        self.assertFalse(
            await self.database.room_reply_sent_since_recording(
                "raced-recording", 0.0
            )
        )
        # With the correlation window as lookback: seen.
        self.assertTrue(
            await self.database.room_reply_sent_since_recording(
                "raced-recording", 15.0
            )
        )
        # And the lookback is bounded — a reply from a minute earlier belongs
        # to an older exchange and must not suppress this one's gesture.
        self.assertFalse(
            await self.database.room_reply_sent_since_recording(
                "raced-recording", 3.0
            )
        )

    async def test_room_reply_since_recording_ignores_stamp_and_ack_motion(
        self,
    ) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="media-recording",
                event_type="recording.finished",
                speech_text=None,
                received_at=datetime.fromtimestamp(100, tz=timezone.utc),
                message_id=None,
                message_media=None,
            )
        )
        await self.database.record_motion_delivery(
            "media-recording",
            "room-1",
            "ack-motion-message",
            600,
            sent_at=101,
        )
        await self.database.record_stamp_delivery(
            "media-recording",
            "room-1",
            "thinking-stamp-message",
            600,
            sent_at=102,
        )

        self.assertFalse(
            await self.database.room_reply_sent_since_recording(
                "media-recording", 0.0
            )
        )

    async def test_initialize_migrates_pre_echo_correlation_database(self) -> None:
        old_path = Path(self.temporary.name) / "old-state.db"
        connection = sqlite3.connect(old_path)
        connection.execute(
            """
            CREATE TABLE inbound_events (
                request_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                room_uuid TEXT,
                sender_uuid TEXT,
                speech_text TEXT,
                received_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                last_error TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE event_effects (
                request_id TEXT PRIMARY KEY,
                response_text TEXT,
                bocco_sent INTEGER NOT NULL DEFAULT 0,
                discord_sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE motion_chains (
                source_request_id TEXT PRIMARY KEY,
                room_uuid TEXT NOT NULL,
                motion_uuids_json TEXT NOT NULL,
                due_times_json TEXT NOT NULL,
                sent_count INTEGER NOT NULL DEFAULT 0,
                finished_count INTEGER NOT NULL DEFAULT 0,
                talk_finished INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        connection.commit()
        connection.close()

        migrated = EventDatabase(old_path)
        await migrated.initialize()

        connection = sqlite3.connect(old_path)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(inbound_events)")
        }
        effect_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(event_effects)")
        }
        motion_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(motion_chains)")
        }
        outbound_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'outbound_messages'"
        ).fetchone()
        settings_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runtime_settings'"
        ).fetchone()
        schedules_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schedules'"
        ).fetchone()
        accel_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'accel_batches'"
        ).fetchone()
        reaction_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'reaction_bank'"
        ).fetchone()
        connection.close()
        self.assertIn("message_id", columns)
        self.assertIn("message_media", columns)
        self.assertIn("priority", columns)
        self.assertIn("motion_cues_json", effect_columns)
        self.assertIn("ack_motion_attempted", effect_columns)
        self.assertIn("motion_kinds_json", motion_columns)
        self.assertIsNotNone(outbound_table)
        self.assertIsNotNone(settings_table)
        self.assertIsNotNone(schedules_table)
        self.assertIsNotNone(accel_table)
        self.assertIsNotNone(reaction_table)

    async def test_reaction_bank_is_durable_and_refresh_is_low_priority(self) -> None:
        phrases = ("一", "二", "三", "四", "五")
        await self.database.put_reaction_phrases(
            "persona-hash", "lift", phrases, generated_at=100.0
        )
        self.assertTrue(
            await self.database.enqueue_reaction_bank_refresh(
                "boot", "persona-hash", "instructions", "room-1", now=101.0
            )
        )
        reopened = EventDatabase(self.path)
        await reopened.initialize()
        self.assertEqual(
            await reopened.get_reaction_phrases("persona-hash", "lift"), phrases
        )
        queued = await reopened.get_event("internal:reaction-bank:boot")
        assert queued is not None
        self.assertEqual(queued.event_type, "reaction_bank.refresh")
        self.assertEqual(queued.priority, -100)

    async def test_same_second_accel_coalescing_and_dropped_override(self) -> None:
        received_at = datetime.fromtimestamp(100.25, tz=timezone.utc)
        for request_id, kind in (("peer-shaken", "shaken"), ("peer-drop", "dropped")):
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id=request_id,
                    event_type="accel.detected",
                    event_detail=kind,
                    received_at=received_at,
                )
            )
        claimed = await self.database.claim_next()
        assert claimed is not None
        kinds = await self.database.coalesce_same_second_accel(
            claimed.request_id,
            "room-1",
            claimed.event_detail or "",
            received_at,
            now=100.5,
        )
        self.assertEqual(kinds, ("dropped", "shaken"))
        persisted = await self.database.get_event(claimed.request_id)
        assert persisted is not None
        self.assertEqual(persisted.event_detail, "dropped")
        peer = "peer-drop" if claimed.request_id == "peer-shaken" else "peer-shaken"
        assert (await self.database.get_event(peer)) is not None
        self.assertEqual((await self.database.get_event(peer)).status, "completed")

        reopened = EventDatabase(self.path)
        await reopened.initialize()
        self.assertTrue(
            await reopened.reserve_accel_reaction(
                "other-room", "lift", 120, 120, now=200.0
            )
        )
        self.assertFalse(
            await reopened.reserve_accel_reaction(
                "other-room", "shaken", 120, 120, now=201.0
            )
        )
        self.assertTrue(
            await reopened.reserve_accel_reaction(
                "other-room", "dropped", 300, 120, now=204.0
            )
        )
        self.assertFalse(
            await reopened.reserve_accel_reaction(
                "other-room", "dropped", 300, 120, now=205.0
            )
        )

    async def test_motion_chain_advances_in_order_and_budget_drops_later_cues(
        self,
    ) -> None:
        await self.database.enqueue(
            FakeInboundEvent(request_id="motion-source", event_type="message.received")
        )
        await self.database.ensure_motion_chain(
            "motion-source",
            "room-1",
            (("motion-1", 100.0), ("motion-2", 100.1), ("motion-3", 100.2)),
            60,
            motion_kinds=("GOOD_01", "YES_01", "GOOD_02"),
            now=100,
        )

        first = await self.database.dispatch_due_motion(
            "motion-source", 0, 60, 1, now=100
        )
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.motion_uuid, "motion-1")

        second = await self.database.advance_motion_chain(
            "motion.finished", "room-1", "GOOD_01", 60, 1, now=101
        )
        self.assertIsNone(second)
        chain = await self.database.get_motion_chain("motion-source")
        assert chain is not None
        self.assertEqual(chain.sent_count, 1)
        self.assertEqual(chain.finished_count, 1)
        self.assertEqual(chain.status, "abandoned")

    async def test_unrelated_motion_finished_does_not_advance_preset_chain(
        self,
    ) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="identity-mismatch", event_type="message.received"
            )
        )
        await self.database.ensure_motion_chain(
            "identity-mismatch",
            "room-1",
            (("expected-uuid", 100.0),),
            60,
            motion_kinds=("EXPECTED_01",),
            now=100,
        )
        self.assertIsNotNone(
            await self.database.dispatch_due_motion(
                "identity-mismatch", 0, 60, 8, now=100
            )
        )

        self.assertIsNone(
            await self.database.advance_motion_chain(
                "motion.finished", "room-1", "UNRELATED_01", 60, 8, now=101
            )
        )

        chain = await self.database.get_motion_chain("identity-mismatch")
        assert chain is not None
        self.assertEqual((chain.sent_count, chain.finished_count), (1, 0))
        self.assertEqual(chain.status, "active")

    async def test_matching_motion_kind_advances_preset_chain(self) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="identity-match", event_type="message.received"
            )
        )
        await self.database.ensure_motion_chain(
            "identity-match",
            "room-1",
            (("expected-uuid", 100.0),),
            60,
            motion_kinds=("EXPECTED_01",),
            now=100,
        )
        self.assertIsNotNone(
            await self.database.dispatch_due_motion(
                "identity-match", 0, 60, 8, now=100
            )
        )

        self.assertIsNone(
            await self.database.advance_motion_chain(
                "motion.finished", "room-1", "EXPECTED_01", 60, 8, now=101
            )
        )

        chain = await self.database.get_motion_chain("identity-match")
        assert chain is not None
        self.assertEqual((chain.sent_count, chain.finished_count), (1, 1))
        self.assertEqual(chain.status, "completed")

    async def test_motion_finished_without_kind_does_not_advance_chain(self) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="identity-missing", event_type="message.received"
            )
        )
        await self.database.ensure_motion_chain(
            "identity-missing",
            "room-1",
            (("expected-uuid", 100.0),),
            60,
            motion_kinds=("EXPECTED_01",),
            now=100,
        )
        self.assertIsNotNone(
            await self.database.dispatch_due_motion(
                "identity-missing", 0, 60, 8, now=100
            )
        )

        self.assertIsNone(
            await self.database.advance_motion_chain(
                "motion.finished", "room-1", None, 60, 8, now=101
            )
        )

        chain = await self.database.get_motion_chain("identity-missing")
        assert chain is not None
        self.assertEqual((chain.sent_count, chain.finished_count), (1, 0))
        self.assertEqual(chain.status, "active")

    async def test_preset_webhook_never_advances_custom_motion_entry(self) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="custom-webhook", event_type="message.received"
            )
        )
        await self.database.ensure_motion_chain(
            "custom-webhook",
            "room-1",
            (("custom:しょんぼり", 100.0),),
            60,
            # Even an accidentally stored matching identity may not let a
            # preset webhook finish a custom document.
            motion_kinds=("EXPECTED_01",),
            now=100,
        )
        self.assertIsNotNone(
            await self.database.dispatch_due_motion(
                "custom-webhook", 0, 60, 8, now=100
            )
        )

        self.assertIsNone(
            await self.database.advance_motion_chain(
                "motion.finished", "room-1", "EXPECTED_01", 60, 8, now=101
            )
        )

        chain = await self.database.get_motion_chain("custom-webhook")
        assert chain is not None
        self.assertEqual((chain.sent_count, chain.finished_count), (1, 0))
        self.assertEqual(chain.status, "active")

    async def test_custom_chain_entries_finish_after_send_so_later_cues_stay_live(
        self,
    ) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="custom-source", event_type="message.received"
            )
        )
        await self.database.ensure_motion_chain(
            "custom-source",
            "room-1",
            (("custom:しょんぼり", 100.0), ("preset-uuid", 105.0)),
            60,
            now=100,
        )

        first = await self.database.dispatch_due_motion(
            "custom-source", 0, 60, 8, now=100
        )
        assert first is not None
        self.assertEqual(first.motion_uuid, "custom:しょんぼり")
        chain = await self.database.get_motion_chain("custom-source")
        assert chain is not None
        # The custom entry stays in flight until its API send succeeds.
        self.assertEqual((chain.sent_count, chain.finished_count), (1, 0))
        self.assertEqual(chain.status, "active")

        successor = await self.database.complete_custom_motion(
            "custom-source", 60, 8, now=100
        )
        self.assertIsNone(successor)
        chain = await self.database.get_motion_chain("custom-source")
        assert chain is not None
        self.assertEqual((chain.sent_count, chain.finished_count), (1, 1))

        second = await self.database.dispatch_due_motion(
            "custom-source", 1, 60, 8, now=106
        )
        assert second is not None
        self.assertEqual(second.motion_uuid, "preset-uuid")

    async def test_final_custom_chain_entry_completes_the_chain(self) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="custom-final", event_type="message.received"
            )
        )
        await self.database.ensure_motion_chain(
            "custom-final",
            "room-1",
            (("custom:ぶんぶん", 100.0),),
            60,
            now=100,
        )

        dispatch = await self.database.dispatch_due_motion(
            "custom-final", 0, 60, 8, now=100
        )
        assert dispatch is not None
        before = await self.database.get_motion_chain("custom-final")
        assert before is not None
        self.assertEqual(before.status, "active")
        self.assertEqual((before.sent_count, before.finished_count), (1, 0))

        successor = await self.database.complete_custom_motion(
            "custom-final", 60, 8, now=100
        )
        self.assertIsNone(successor)
        chain = await self.database.get_motion_chain("custom-final")
        assert chain is not None
        self.assertEqual(chain.status, "completed")

    async def test_custom_completion_drains_an_already_due_successor(self) -> None:
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="mixed-chain", event_type="message.received"
            )
        )
        await self.database.ensure_motion_chain(
            "mixed-chain",
            "room-1",
            (
                ("preset-before", 100.0),
                ("custom:しょんぼり", 100.0),
                ("preset-after", 100.0),
            ),
            60,
            motion_kinds=("PRESET_BEFORE", None, "PRESET_AFTER"),
            now=100,
        )

        first = await self.database.dispatch_due_motion(
            "mixed-chain", 0, 60, 8, now=100
        )
        assert first is not None
        self.assertEqual(first.motion_uuid, "preset-before")
        # These due events can be consumed while the first preset is still
        # waiting for its completion webhook.
        self.assertIsNone(
            await self.database.dispatch_due_motion(
                "mixed-chain", 1, 60, 8, now=100
            )
        )
        self.assertIsNone(
            await self.database.dispatch_due_motion(
                "mixed-chain", 2, 60, 8, now=100
            )
        )

        custom = await self.database.advance_motion_chain(
            "motion.finished", "room-1", "PRESET_BEFORE", 60, 8, now=101
        )
        assert custom is not None
        self.assertEqual(custom.motion_uuid, "custom:しょんぼり")

        # A stale preset webhook must not acknowledge the in-flight custom
        # document; only a successful custom API send may do that.
        self.assertIsNone(
            await self.database.advance_motion_chain(
                "motion.finished", "room-1", "STALE", 60, 8, now=101
            )
        )
        in_flight = await self.database.get_motion_chain("mixed-chain")
        assert in_flight is not None
        self.assertEqual((in_flight.sent_count, in_flight.finished_count), (2, 1))

        final = await self.database.complete_custom_motion(
            "mixed-chain", 60, 8, now=101
        )
        assert final is not None
        self.assertEqual(final.motion_uuid, "preset-after")
        drained = await self.database.get_motion_chain("mixed-chain")
        assert drained is not None
        self.assertEqual((drained.sent_count, drained.finished_count), (3, 2))

        self.assertIsNone(
            await self.database.advance_motion_chain(
                "motion.finished", "room-1", "PRESET_AFTER", 60, 8, now=102
            )
        )
        completed = await self.database.get_motion_chain("mixed-chain")
        assert completed is not None
        self.assertEqual(completed.status, "completed")

    async def test_motion_chain_timeout_and_restart_abandon_safely(self) -> None:
        for request_id in ("timeout-chain", "restart-chain"):
            await self.database.enqueue(
                FakeInboundEvent(request_id=request_id, event_type="message.received")
            )
            await self.database.ensure_motion_chain(
                request_id,
                "room-1",
                ((f"{request_id}-motion", 101.0),),
                60,
                now=100,
            )

        self.assertEqual(
            await self.database.abandon_expired_motion_chains(now=161), 2
        )
        timed_out = await self.database.get_motion_chain("timeout-chain")
        assert timed_out is not None
        self.assertEqual(timed_out.status, "abandoned")

        await self.database.enqueue(
            FakeInboundEvent(request_id="restart-active", event_type="message.received")
        )
        await self.database.ensure_motion_chain(
            "restart-active",
            "room-1",
            (("restart-motion", 201.0),),
            60,
            now=200,
        )
        reopened = EventDatabase(self.path)
        await reopened.initialize()
        self.assertEqual(await reopened.abandon_motion_chains_for_restart(now=201), 1)
        restarted = await reopened.get_motion_chain("restart-active")
        assert restarted is not None
        self.assertEqual(restarted.status, "abandoned")
        due = await reopened.get_event("internal:motion:restart-active:0")
        assert due is not None
        self.assertEqual(due.status, "completed")

    async def test_speech_calibration_updates_and_survives_reopen(self) -> None:
        await self.database.enqueue(
            FakeInboundEvent(request_id="calibration-source", event_type="message.received")
        )
        spoken = "あ" * 20
        await self.database.record_bocco_delivery(
            "calibration-source",
            "room-1",
            spoken,
            "calibration-message",
            600,
            sent_at=100,
        )
        cold = SpeechCalibration(0.5, 0.15)

        updated = await self.database.record_talk_finished(
            "room-1", spoken, 104, cold
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertAlmostEqual(updated.delivery_lag_seconds, 0.55)
        self.assertAlmostEqual(updated.seconds_per_char, 0.1525)
        reopened = EventDatabase(self.path)
        await reopened.initialize()
        self.assertEqual(
            await reopened.get_speech_calibration("room-1", cold), updated
        )

    async def test_new_message_anchor_rebases_cues_and_splits_calibration(self) -> None:
        await self.database.enqueue(
            FakeInboundEvent(request_id="anchor-source", event_type="message.received")
        )
        spoken = "あ" * 20
        await self.database.record_bocco_delivery(
            "anchor-source",
            "room-1",
            spoken,
            "anchor-message",
            600,
            sent_at=100,
        )
        await self.database.ensure_motion_chain(
            "anchor-source",
            "room-1",
            (("motion-1", 105.0), ("motion-2", 107.0)),
            60,
            anchor_offsets=(0.5, 1.5),
            now=100,
        )
        cold = SpeechCalibration(0.5, 0.15)

        delivered = await self.database.record_message_anchor(
            "room-1", 101.5, cold, 60
        )

        self.assertIsNotNone(delivered)
        assert delivered is not None
        self.assertAlmostEqual(delivered.delivery_lag_seconds, 0.6)
        self.assertEqual(delivered.seconds_per_char, 0.15)
        chain = await self.database.get_motion_chain("anchor-source")
        assert chain is not None
        self.assertEqual(chain.due_times, (102.0, 103.0))
        self.assertEqual(chain.anchor_offsets, (0.5, 1.5))
        self.assertEqual(chain.anchored_at, 101.5)
        self.assertIsNone(
            await self.database.dispatch_due_motion(
                "anchor-source", 0, 60, 8, now=101.9
            )
        )
        self.assertIsNotNone(
            await self.database.dispatch_due_motion(
                "anchor-source", 0, 60, 8, now=102.0
            )
        )

        finished = await self.database.record_talk_finished(
            "room-1", spoken, 104.5, cold
        )
        self.assertIsNotNone(finished)
        assert finished is not None
        self.assertAlmostEqual(finished.delivery_lag_seconds, 0.6)
        self.assertAlmostEqual(finished.seconds_per_char, 0.15)
        self.assertEqual(finished.sample_count, 2)

    async def test_database_file_is_private(self) -> None:
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
