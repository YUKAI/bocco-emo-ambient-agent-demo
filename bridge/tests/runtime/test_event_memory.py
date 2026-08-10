"""Event extraction: the store, the rule's guardrails, and feature-off parity.

The extraction *judgement* belongs to the model and is exercised against the
real 226-turn store by ``bridge/tools/backfill_events.py``. What is tested here
is everything that must hold no matter what the model answers: the deixis
guarantee, the parser's refusal to widen a candidate, retrieval on a resolved
date, a real delete, and — the one that matters most operationally — that with
the feature off the bridge is byte-identical to what it was before.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from bocco_bridge.config import BridgeConfig
from bocco_bridge.db import EventDatabase
from bocco_bridge.event_extraction import EventExtractor
from bocco_bridge.event_memory import (
    EVENT_TEXT_MAX_CHARS,
    EventExtractionError,
    EventMemory,
    ExtractedEvent,
    contains_deixis,
    event_extraction_prompt,
    parse_extracted_event,
    render_event_line,
    strip_deixis,
)
from bocco_bridge.events import EventProcessor, EventWorker
from bocco_bridge.reaction_generation import HermesPriorityGate
from runtime.fakes import FakeBocco, FakeHermes, FakeInboundEvent


# 2026-08-05 16:24 local — the minute of the measured 「いま東京は…29.2度だよ」
# failure, so every date assertion below lines up with the real incident.
def _timestamp(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone().timestamp()


SAID_AT = _timestamp("2026-08-05 16:24")
NEXT_DAY = _timestamp("2026-08-06 13:37")


class DeixisTests(unittest.TestCase):
    """The property the whole design turns on, tested without a model."""

    def test_relative_day_words_never_survive_storage(self) -> None:
        for line in (
            "ユーザーは今日ミーティングがあると話した",
            "ユーザーはいま歯医者に行ったと話した",
            "ユーザーはさっきご飯を食べたと話した",
            "ユーザーは昨日サッカーをしたと話した",
            "ユーザーは今週旅行に行くと話した",
        ):
            with self.subTest(line=line):
                self.assertTrue(contains_deixis(line))
                self.assertFalse(contains_deixis(strip_deixis(line)))

    def test_stripping_keeps_the_claim_and_only_drops_the_clock(self) -> None:
        self.assertEqual(
            strip_deixis("ユーザーは今日ミーティングがあると話した"),
            "ユーザーはミーティングがあると話した",
        )
        self.assertEqual(
            strip_deixis("ユーザーは昨日歯医者に行ったと話した"),
            "ユーザーは歯医者に行ったと話した",
        )

    def test_a_word_that_merely_starts_with_今_is_left_alone(self) -> None:
        # 「今度」 would become 「度」 under an unguarded strip of bare 今.
        self.assertEqual(
            strip_deixis("ユーザーは今度の旅行の話をした"),
            "ユーザーは今度の旅行の話をした",
        )

    def test_一昨日_is_not_eaten_by_昨日(self) -> None:
        self.assertEqual(strip_deixis("一昨日の話をした"), "の話をした")

    def test_a_line_with_no_time_word_is_returned_unchanged(self) -> None:
        line = "ユーザーは歯医者に行ったと話した"
        self.assertEqual(strip_deixis(line), line)


class ExtractionPromptTests(unittest.TestCase):
    def test_prompt_resolves_dates_against_when_it_was_said(self) -> None:
        # The clock is an argument, so a job retried tomorrow still resolves
        # 「今日」 to the day of the exchange. This is the deixis guarantee at
        # the prompt boundary, and it would be lost if the prompt read a clock.
        prompt = event_extraction_prompt("今日ミーティング", "うん", SAID_AT)
        self.assertIn("Today is 2026-08-05", prompt)
        self.assertIn("yesterday was 2026-08-04", prompt)
        self.assertIn("tomorrow is 2026-08-06", prompt)
        self.assertIn("spoken at 2026-08-05 16:24", prompt)

    def test_prompt_carries_the_household_rule_and_the_negative_examples(
        self,
    ) -> None:
        prompt = event_extraction_prompt("こま", "まくら！", SAID_AT)
        self.assertIn("you like [x] is not a fact", prompt)
        self.assertIn("しりとり", prompt)
        self.assertIn("聞こえる？", prompt)
        self.assertIn("こま", prompt)

    def test_prompt_renders_no_leftover_placeholders(self) -> None:
        prompt = event_extraction_prompt("テスト", "うん", SAID_AT)
        self.assertNotIn("$", prompt)
        # The JSON examples must reach the model with single braces.
        self.assertIn('{"event": null}', prompt)


class ParserTests(unittest.TestCase):
    def test_explicit_rejection_is_not_an_error(self) -> None:
        self.assertIsNone(parse_extracted_event('{"event": null}', SAID_AT))

    def test_empty_output_records_nothing(self) -> None:
        self.assertIsNone(parse_extracted_event("", SAID_AT))
        self.assertIsNone(parse_extracted_event('{"event": ""}', SAID_AT))

    def test_code_fence_and_preamble_are_tolerated(self) -> None:
        for wrapped in (
            '```json\n{"event": "ユーザーは歯医者に行ったと話した"}\n```',
            'Here is the record:\n{"event": "ユーザーは歯医者に行ったと話した"}',
        ):
            with self.subTest(wrapped=wrapped):
                parsed = parse_extracted_event(wrapped, SAID_AT)
                assert parsed is not None
                self.assertEqual(parsed.text, "ユーザーは歯医者に行ったと話した")

    def test_a_list_yields_at_most_one_event(self) -> None:
        parsed = parse_extracted_event(
            '[{"event": "ユーザーは歯医者に行ったと話した"},'
            ' {"event": "ユーザーは走ったと話した"}]',
            SAID_AT,
        )
        assert parsed is not None
        self.assertEqual(parsed.text, "ユーザーは歯医者に行ったと話した")

    def test_prose_is_an_error_rather_than_a_silent_rejection(self) -> None:
        with self.assertRaises(EventExtractionError):
            parse_extracted_event("I think nothing happened here.", SAID_AT)

    def test_an_over_long_line_is_refused_rather_than_clipped(self) -> None:
        # Clipping mid-sentence would change what the row claims.
        with self.assertRaises(EventExtractionError):
            parse_extracted_event(
                json.dumps({"event": "あ" * (EVENT_TEXT_MAX_CHARS + 1)}),
                SAID_AT,
            )

    def test_deixis_the_model_ignored_is_stripped_anyway(self) -> None:
        parsed = parse_extracted_event(
            json.dumps(
                {
                    "event": "ユーザーは今日ミーティングがあると話した",
                    "occurred": "2026-08-05",
                    "kind": "life",
                }
            ),
            SAID_AT,
        )
        assert parsed is not None
        self.assertEqual(parsed.text, "ユーザーはミーティングがあると話した")
        self.assertEqual(parsed.occurred_at, SAID_AT)

    def test_a_reported_earlier_day_becomes_that_day(self) -> None:
        parsed = parse_extracted_event(
            json.dumps(
                {
                    "event": "ユーザーは昨日歯医者に行ったと話した",
                    "occurred": "2026-08-04",
                    "kind": "life",
                }
            ),
            SAID_AT,
        )
        assert parsed is not None
        self.assertEqual(parsed.text, "ユーザーは歯医者に行ったと話した")
        occurred = datetime.fromtimestamp(parsed.occurred_at).astimezone()
        self.assertEqual(occurred.date().isoformat(), "2026-08-04")

    def test_a_hallucinated_year_falls_back_to_the_conversation(self) -> None:
        parsed = parse_extracted_event(
            json.dumps({"event": "ユーザーは歯医者に行ったと話した", "occurred": "2019-01-02"}),
            SAID_AT,
        )
        assert parsed is not None
        self.assertEqual(parsed.occurred_at, SAID_AT)

    def test_a_missing_or_invented_kind_does_not_lose_the_row(self) -> None:
        for payload in ({"event": "ユーザーは走ったと話した"},
                        {"event": "ユーザーは走ったと話した", "kind": "vibes"}):
            with self.subTest(payload=payload):
                parsed = parse_extracted_event(json.dumps(payload), SAID_AT)
                assert parsed is not None
                self.assertEqual(parsed.kind, "life")


class EventStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = EventMemory(Path(self.temporary.name) / "events.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _record(
        self,
        text: str,
        request_id: str,
        *,
        occurred: str = "2026-08-05 16:24",
        said: float = SAID_AT,
        kind: str = "life",
    ):
        return await self.store.record(
            "room-1",
            request_id,
            ExtractedEvent(text, kind, _timestamp(occurred)),
            said_at=said,
            source_user_text="今日ミーティングがある",
            source_reply_text="うん、ミーティングがあるんだね。",
            created_at=said,
        )

    async def test_the_utterance_it_came_from_is_stored_beside_it(self) -> None:
        stored = await self._record("ユーザーはミーティングがあると話した", "m-1")
        assert stored is not None
        # Every row has to be checkable, which means the source survives.
        self.assertEqual(stored.source_user_text, "今日ミーティングがある")
        self.assertEqual(stored.said_at, SAID_AT)

    async def test_recording_is_idempotent_per_exchange(self) -> None:
        first = await self._record("ユーザーはミーティングがあると話した", "m-1")
        again = await self._record("ユーザーはミーティングがあると話した", "m-1")
        assert first is not None and again is not None
        self.assertEqual(first.id, again.id)
        self.assertEqual(await self.store.count("room-1"), 1)

    async def test_the_same_sentence_twice_in_one_day_stores_once(self) -> None:
        await self._record("ユーザーはミーティングがあると話した", "m-1")
        self.assertIsNone(
            await self._record("ユーザーはミーティングがあると話した", "m-2")
        )
        self.assertEqual(await self.store.count("room-1"), 1)

    async def test_the_same_sentence_on_another_day_is_another_event(self) -> None:
        # Two dentist visits on two days are both true; this is precisely what
        # the facts table's subject-supersede would have destroyed.
        await self._record("ユーザーは歯医者に行ったと話した", "m-1")
        second = await self._record(
            "ユーザーは歯医者に行ったと話した",
            "m-2",
            occurred="2026-08-12 10:00",
            said=_timestamp("2026-08-12 10:00"),
        )
        self.assertIsNotNone(second)
        self.assertEqual(await self.store.count("room-1"), 2)

    async def test_asking_about_yesterday_finds_yesterdays_events(self) -> None:
        await self._record("ユーザーはミーティングがあると話した", "m-1")
        await self._record(
            "ユーザーは歯医者に行ったと話した",
            "m-2",
            occurred="2026-08-06 11:00",
            said=NEXT_DAY,
        )
        recalled = await self.store.recall(
            "room-1", "昨日何の話をした？", now=NEXT_DAY, limit=3
        )
        self.assertEqual(
            [event.text for event in recalled],
            ["ユーザーはミーティングがあると話した"],
        )

    async def test_a_topical_question_finds_its_event_without_a_date(self) -> None:
        await self._record("ユーザーは歯医者に行ったと話した", "m-1")
        recalled = await self.store.recall(
            "room-1", "歯医者の話をしたっけ", now=NEXT_DAY, limit=3
        )
        self.assertEqual(len(recalled), 1)

    async def test_recall_respects_its_character_budget(self) -> None:
        for index in range(3):
            await self._record(
                f"ユーザーは{'あ' * 20}と話した{index}",
                f"m-{index}",
                occurred="2026-08-05 16:24",
            )
        recalled = await self.store.recall(
            "room-1", "昨日何した", now=NEXT_DAY, limit=3, max_chars=30
        )
        self.assertEqual(len(recalled), 1)

    async def test_recall_is_chronological(self) -> None:
        await self._record(
            "ユーザーは走ったと話した",
            "m-2",
            occurred="2026-08-05 18:00",
        )
        await self._record("ユーザーはミーティングがあると話した", "m-1")
        recalled = await self.store.recall(
            "room-1", "昨日何した", now=NEXT_DAY, limit=3
        )
        self.assertEqual(
            [event.text for event in recalled],
            ["ユーザーはミーティングがあると話した", "ユーザーは走ったと話した"],
        )

    async def test_forget_really_deletes_including_the_stored_utterance(
        self,
    ) -> None:
        await self._record("ユーザーは歯医者に行ったと話した", "m-1")
        self.assertEqual(await self.store.forget("room-1", "歯医者"), 1)
        self.assertEqual(await self.store.count("room-1"), 0)
        # Not merely deactivated: nothing survives to be retrieved.
        self.assertEqual(
            await self.store.recall("room-1", "歯医者", now=NEXT_DAY), ()
        )
        with self.store._connection() as connection:  # noqa: SLF001 - the point
            rows = connection.execute("SELECT count(*) FROM events").fetchone()
            self.assertEqual(rows[0], 0)

    async def test_retention_drops_the_oldest_occurrences_first(self) -> None:
        for index in range(4):
            await self.store.record(
                "room-1",
                f"m-{index}",
                ExtractedEvent(
                    f"ユーザーは{index}のことを話した",
                    "life",
                    _timestamp("2026-08-01 10:00") + index * 86_400,
                ),
                said_at=SAID_AT,
                retention=2,
            )
        remaining = await self.store.list_recent("room-1", limit=10)
        self.assertEqual(
            [event.text for event in remaining],
            ["ユーザーは3のことを話した", "ユーザーは2のことを話した"],
        )

    async def test_has_source_reports_an_already_extracted_exchange(self) -> None:
        await self._record("ユーザーはミーティングがあると話した", "m-1")
        self.assertTrue(await self.store.has_source("m-1"))
        self.assertFalse(await self.store.has_source("m-2"))


class RenderingTests(unittest.TestCase):
    def test_a_row_is_rendered_with_an_absolute_date(self) -> None:
        from bocco_bridge.event_memory import RecordedEvent

        event = RecordedEvent(
            id=1,
            room_uuid="room-1",
            text="ユーザーはミーティングがあると話した",
            kind="life",
            occurred_at=SAID_AT,
            said_at=SAID_AT,
            source_request_id="m-1",
            source_user_text="",
            source_reply_text="",
            created_at=SAID_AT,
        )
        # Never 「昨日」: the row has to stay true whenever it is read.
        self.assertEqual(
            render_event_line(event, NEXT_DAY),
            "・8月5日：ユーザーはミーティングがあると話した",
        )

    def test_a_reported_day_carries_when_it_was_reported(self) -> None:
        from bocco_bridge.event_memory import RecordedEvent

        event = RecordedEvent(
            id=1,
            room_uuid="room-1",
            text="ユーザーは歯医者に行ったと話した",
            kind="life",
            occurred_at=_timestamp("2026-08-04 12:00"),
            said_at=SAID_AT,
            source_request_id="m-1",
            source_user_text="",
            source_reply_text="",
            created_at=SAID_AT,
        )
        self.assertEqual(
            render_event_line(event, NEXT_DAY),
            "・8月4日：ユーザーは歯医者に行ったと話した（8月5日に聞いた）",
        )


class ScriptedHermes:
    """A gate backend that answers extraction calls from a queue."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def respond(
        self, conversation: str | None, text: str, instructions: str
    ) -> str:
        self.prompts.append(text)
        return self.answers.pop(0) if self.answers else '{"event": null}'


class ExtractorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = EventDatabase(Path(self.temporary.name) / "state.db")
        await self.database.initialize()
        self.store = EventMemory(Path(self.temporary.name) / "events.db")
        await self.store.initialize()
        self.hermes = ScriptedHermes()
        self.gate = HermesPriorityGate(self.hermes)
        self.extractor = EventExtractor(
            self.database,
            self.gate,
            self.store,
            poll_seconds=0.01,
            retry_base_seconds=0.0,
            max_attempts=2,
            retention=100,
            min_interval_seconds=0.0,
            now=lambda: SAID_AT,
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _enqueue(self, request_id: str, user_text: str, reply: str) -> None:
        self.assertTrue(
            await self.database.enqueue_event_extraction(
                request_id, "room-1", user_text, reply, SAID_AT, now=SAID_AT
            )
        )

    async def test_an_accepted_exchange_becomes_one_dated_row(self) -> None:
        self.hermes.answers.append(
            json.dumps(
                {
                    "event": "ユーザーは今日ミーティングがあると話した",
                    "occurred": "2026-08-05",
                    "kind": "life",
                }
            )
        )
        await self._enqueue("m-1", "今日meetingがあるから", "うん、ミーティングだね。")
        self.assertTrue(await self.extractor.process_once())
        stored = await self.store.list_recent("room-1")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].text, "ユーザーはミーティングがあると話した")
        self.assertEqual(stored[0].source_user_text, "今日meetingがあるから")

    async def test_a_rejected_exchange_stores_nothing_and_completes(self) -> None:
        self.hermes.answers.append('{"event": null}')
        await self._enqueue("m-1", "こま", "まくら！次は「ら」だよ。")
        self.assertTrue(await self.extractor.process_once())
        self.assertEqual(await self.store.count("room-1"), 0)
        # Completed, not left to be retried forever.
        self.assertIsNone(await self.database.claim_next_event_extraction())

    async def test_unparsable_output_records_nothing_and_does_not_retry(
        self,
    ) -> None:
        self.hermes.answers.append("I think they went to the dentist.")
        await self._enqueue("m-1", "こんにちは", "こんにちは。")
        self.assertTrue(await self.extractor.process_once())
        self.assertEqual(await self.store.count("room-1"), 0)
        self.assertIsNone(await self.database.claim_next_event_extraction())

    async def test_a_malformed_job_is_dead_lettered_not_retried(self) -> None:
        await self.database.enqueue_event_extraction(
            "m-1", "room-1", "", "reply", SAID_AT, now=SAID_AT
        )
        self.assertTrue(await self.extractor.process_once())
        self.assertIsNone(await self.database.claim_next_event_extraction())

    async def test_a_model_failure_retries_and_then_gives_up_silently(self) -> None:
        class Failing:
            async def respond(self, conversation, text, instructions):
                raise RuntimeError("hermes is down")

        extractor = EventExtractor(
            self.database,
            HermesPriorityGate(Failing()),
            self.store,
            poll_seconds=0.01,
            retry_base_seconds=0.0,
            max_attempts=1,
            retention=100,
            min_interval_seconds=0.0,
            now=lambda: SAID_AT,
        )
        await self._enqueue("m-1", "こんにちは", "こんにちは。")
        self.assertTrue(await extractor.process_once())
        self.assertEqual(await self.store.count("room-1"), 0)
        self.assertIsNone(await self.database.claim_next_event_extraction())

    async def test_the_extraction_call_carries_no_conversation(self) -> None:
        # Stateless: the classification must never append to, or be coloured
        # by, the room's own Hermes history.
        recorded: list[str | None] = []

        class Recording(ScriptedHermes):
            async def respond(self, conversation, text, instructions):
                recorded.append(conversation)
                return '{"event": null}'

        extractor = EventExtractor(
            self.database,
            HermesPriorityGate(Recording()),
            self.store,
            poll_seconds=0.01,
            retry_base_seconds=0.0,
            max_attempts=2,
            retention=100,
            min_interval_seconds=0.0,
            now=lambda: SAID_AT,
        )
        await self._enqueue("m-1", "こんにちは", "こんにちは。")
        await extractor.process_once()
        self.assertEqual(recorded, [None])


class ExtractionLaneTests(unittest.IsolatedAsyncioTestCase):
    """Nothing about extraction may reach the single event worker."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=Path(self.temporary.name) / "state.db",
            transcript_database_path=Path(self.temporary.name) / "transcript.db",
            event_database_path=Path(self.temporary.name) / "events.db",
            tunnel_enabled=False,
            worker_max_attempts=2,
            worker_retry_base_seconds=0,
            conversation_memory_enabled=True,
            event_memory_enabled=True,
            event_extraction_delay_seconds=0.0,
            default_reply_motion=False,
            ack_motion_enabled=False,
            stream_sentences=False,
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        self.bocco = FakeBocco()
        self.background_started = asyncio.Event()
        self.background_release = asyncio.Event()
        self.foreground_calls = 0
        lane = self

        class LaneAwareHermes:
            async def respond(self, conversation, text, instructions):
                if "WHAT HAPPENED" in text:
                    lane.background_started.set()
                    await lane.background_release.wait()
                    return '{"event": null}'
                lane.foreground_calls += 1
                return "短い返事です。"

        self.gate = HermesPriorityGate(LaneAwareHermes())
        self.store = EventMemory(self.config.event_path)
        self.processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.gate.foreground,
            now=lambda: SAID_AT,
            events=self.store,
        )
        self.worker = EventWorker(self.config, self.database, self.processor)
        self.extractor = EventExtractor(
            self.database,
            self.gate,
            self.store,
            poll_seconds=0.01,
            retry_base_seconds=0.0,
            max_attempts=2,
            retention=100,
            min_interval_seconds=0.0,
            now=lambda: SAID_AT,
        )

    async def asyncTearDown(self) -> None:
        self.background_release.set()
        self.temporary.cleanup()

    async def test_the_worker_never_claims_an_extraction_job(self) -> None:
        await self.database.enqueue_event_extraction(
            "m-0", "room-1", "こんにちは", "こんにちは。", SAID_AT, now=SAID_AT
        )
        # The one thing that must never happen: a model call on the reply path.
        self.assertIsNone(await self.database.claim_next())

    async def test_extraction_in_flight_never_stalls_a_reply(self) -> None:
        await self.database.enqueue_event_extraction(
            "m-0", "room-1", "こんにちは", "こんにちは。", SAID_AT, now=SAID_AT
        )
        extracting = asyncio.create_task(self.extractor.process_once())
        await asyncio.wait_for(self.background_started.wait(), timeout=1.0)

        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="message-1",
                    event_type="message.received",
                    speech_text="こんにちは",
                )
            )
        )
        self.assertTrue(
            await asyncio.wait_for(self.worker.process_once(), timeout=1.0)
        )
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        self.background_release.set()
        self.assertTrue(await asyncio.wait_for(extracting, timeout=1.0))

    async def test_a_delivered_reply_queues_exactly_one_extraction_job(
        self,
    ) -> None:
        self.background_release.set()
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="message-1",
                event_type="message.received",
                speech_text="今日meetingがある",
            )
        )
        await self.worker.process_once()
        job = await self.database.claim_next_event_extraction(now=SAID_AT + 1)
        assert job is not None
        self.assertEqual(job.event_type, "event_memory.extract")
        detail = json.loads(job.event_detail or "{}")
        self.assertEqual(detail["user_text"], "今日meetingがある")
        self.assertEqual(detail["source_request_id"], "message-1")

    async def test_the_job_is_not_available_until_the_delay_has_passed(
        self,
    ) -> None:
        await self.database.enqueue_event_extraction(
            "m-0",
            "room-1",
            "こんにちは",
            "こんにちは。",
            SAID_AT,
            delay_seconds=15.0,
            now=SAID_AT,
        )
        self.assertIsNone(
            await self.database.claim_next_event_extraction(now=SAID_AT + 1)
        )
        self.assertIsNotNone(
            await self.database.claim_next_event_extraction(now=SAID_AT + 16)
        )


class FeatureOffTests(unittest.IsolatedAsyncioTestCase):
    """With the flag off the bridge must be what it was before this existed."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def _config(self, *, enabled: bool) -> BridgeConfig:
        return BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=Path(self.temporary.name) / "state.db",
            transcript_database_path=Path(self.temporary.name) / "transcript.db",
            event_database_path=Path(self.temporary.name) / "events.db",
            tunnel_enabled=False,
            conversation_memory_enabled=True,
            event_memory_enabled=enabled,
            event_extraction_delay_seconds=0.0,
            default_reply_motion=False,
            ack_motion_enabled=False,
            stream_sentences=False,
        )

    async def test_default_is_off(self) -> None:
        self.assertFalse(BridgeConfig(webhook_secret="s").event_memory_enabled)

    async def test_the_environment_reads_the_flag_and_its_knobs(self) -> None:
        default = BridgeConfig.from_environment(
            {"BOCCO_WEBHOOK_SECRET": "present"}
        )
        self.assertFalse(default.event_memory_enabled)
        self.assertEqual(default.event_path.name, "events.db")

        enabled = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "present",
                "BRIDGE_CONVERSATION_MEMORY": "on",
                "BRIDGE_EVENT_MEMORY": "on",
                "BRIDGE_EVENT_EXTRACTION_DELAY_SECONDS": "30",
                "BRIDGE_EVENT_EXTRACTION_MIN_INTERVAL_SECONDS": "7",
                "BRIDGE_EVENT_MEMORY_RECALLED": "2",
                "BRIDGE_EVENT_MEMORY_MAX_CHARS": "250",
                "BRIDGE_EVENT_MEMORY_RETENTION": "500",
                "BOCCO_BRIDGE_EVENT_DB": "/tmp/somewhere/events.db",
            }
        )
        self.assertTrue(enabled.event_memory_enabled)
        self.assertEqual(enabled.event_extraction_delay_seconds, 30.0)
        self.assertEqual(enabled.event_extraction_min_interval_seconds, 7.0)
        self.assertEqual(enabled.event_memory_recalled, 2)
        self.assertEqual(enabled.event_memory_max_chars, 250)
        self.assertEqual(enabled.event_memory_retention, 500)
        self.assertEqual(str(enabled.event_path), "/tmp/somewhere/events.db")

    async def test_the_feature_requires_conversation_memory(self) -> None:
        with self.assertRaises(ValueError):
            BridgeConfig(
                webhook_secret="s",
                conversation_memory_enabled=False,
                event_memory_enabled=True,
            )

    async def test_no_event_database_is_created(self) -> None:
        config = self._config(enabled=False)
        database = EventDatabase(config.database_path)
        await database.initialize()
        processor = EventProcessor(
            config, database, FakeBocco(), FakeHermes(), now=lambda: SAID_AT
        )
        worker = EventWorker(config, database, processor)
        await database.enqueue(
            FakeInboundEvent(
                request_id="message-1",
                event_type="message.received",
                speech_text="今日meetingがある",
            )
        )
        await worker.process_once()
        self.assertFalse(config.event_path.exists())
        self.assertIsNone(await database.claim_next_event_extraction())

    async def test_instructions_are_byte_identical_with_the_feature_off(
        self,
    ) -> None:
        # A store with rows in it must make no difference at all when the flag
        # is off — that is what "byte-identical to today" has to mean.
        store = EventMemory(Path(self.temporary.name) / "events.db")
        await store.initialize()
        await store.record(
            "room-1",
            "m-1",
            ExtractedEvent("ユーザーはミーティングがあると話した", "life", SAID_AT),
            said_at=SAID_AT,
        )
        database = EventDatabase(Path(self.temporary.name) / "state.db")
        await database.initialize()

        off = EventProcessor(
            self._config(enabled=False),
            database,
            FakeBocco(),
            FakeHermes(),
            now=lambda: NEXT_DAY,
            events=store,
        )
        on = EventProcessor(
            self._config(enabled=True),
            database,
            FakeBocco(),
            FakeHermes(),
            now=lambda: NEXT_DAY,
            events=store,
        )
        off_text = await off._response_instructions("room-1", "昨日何した")
        on_text = await on._response_instructions("room-1", "昨日何した")
        self.assertNotIn("できごとの記録", off_text)
        # The block is purely additive, and it answers 「昨日」 with the day
        # written out rather than replayed as a relative label.
        self.assertIn("8月5日：ユーザーはミーティングがあると話した", on_text)
        self.assertTrue(on_text.startswith(off_text))
        self.assertNotIn("昨日", on_text[len(off_text) :])

    async def test_forget_reports_only_facts_with_the_feature_off(self) -> None:
        store = EventMemory(Path(self.temporary.name) / "events.db")
        await store.initialize()
        await store.record(
            "room-1",
            "m-1",
            ExtractedEvent("ユーザーは歯医者に行ったと話した", "life", SAID_AT),
            said_at=SAID_AT,
        )
        database = EventDatabase(Path(self.temporary.name) / "state.db")
        await database.initialize()
        processor = EventProcessor(
            self._config(enabled=False),
            database,
            FakeBocco(),
            FakeHermes(),
            now=lambda: SAID_AT,
            events=store,
        )
        self.assertEqual(await processor._forget_events("room-1", "歯医者"), 0)
        self.assertEqual(await store.count("room-1"), 1)


class ForgetCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=Path(self.temporary.name) / "state.db",
            transcript_database_path=Path(self.temporary.name) / "transcript.db",
            event_database_path=Path(self.temporary.name) / "events.db",
            memory_database_path=Path(self.temporary.name) / "memory.db",
            tunnel_enabled=False,
            conversation_memory_enabled=True,
            event_memory_enabled=True,
            default_reply_motion=False,
            ack_motion_enabled=False,
            stream_sentences=False,
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        self.bocco = FakeBocco()
        self.store = EventMemory(self.config.event_path)
        await self.store.initialize()
        self.processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            FakeHermes(),
            now=lambda: SAID_AT,
            events=self.store,
        )
        self.worker = EventWorker(self.config, self.database, self.processor)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_the_household_can_delete_a_wrong_event(self) -> None:
        await self.store.record(
            "room-1",
            "m-1",
            ExtractedEvent("ユーザーは歯医者に行ったと話した", "life", SAID_AT),
            said_at=SAID_AT,
        )
        await self.database.enqueue(
            FakeInboundEvent(
                request_id="message-1",
                event_type="message.received",
                speech_text="わすれて：歯医者",
            )
        )
        await self.worker.process_once()
        self.assertEqual(self.bocco.sent, [("room-1", "1件の記憶を忘れました。")])
        self.assertEqual(await self.store.count("room-1"), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
