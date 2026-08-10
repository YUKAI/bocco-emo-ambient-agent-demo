"""Semantic retrieval: the latency guarantee first, the quality claim second.

Most of what follows tests things *not* happening — no call, no delay, no
change to the rendered prompt — because that is where the risk is. The feature
is worth having only if it cannot cost a reply, so the degradation paths get
more coverage here than the happy one does.
"""

from datetime import datetime
from pathlib import Path
import asyncio
import json
import tempfile
import time
import unittest

import httpx

from bocco_bridge.config import BridgeConfig
from bocco_bridge.db import EventDatabase
from bocco_bridge.embeddings import (
    EmbeddingClient,
    EmbeddingConfig,
    normalize,
    pack_vector,
    rank_by_similarity,
    reciprocal_rank_fusion,
    unpack_vector,
)
from bocco_bridge.events import (
    EventProcessor,
    EventWorker,
    _format_conversation_section,
)
from bocco_bridge.transcript import (
    ConversationTranscript,
    SemanticQuery,
    embedding_text,
    is_deflection,
    retrieval_query_text,
)
from runtime.fakes import FakeBocco, FakeHermes, FakeInboundEvent


def at(month: int, day: int, hour: int, minute: int = 0, *, year: int = 2026) -> float:
    return datetime(year, month, day, hour, minute).astimezone().timestamp()


NOW = at(8, 5, 17, 30)

# A four-dimensional stand-in for a sentence encoder. Each axis is a topic and
# a text lands on the axes whose words it contains, so 「ごはん」 and 「食べ物」
# and 「スープ」 occupy the same direction while sharing not one character —
# which is exactly the property the real model is being bought for, reproduced
# here without a 130 MB download or a millisecond of nondeterminism.
TOPIC_WORDS: tuple[tuple[str, ...], ...] = (
    ("食べ物", "スープ", "ごはん", "夕飯", "料理", "おいしい"),
    ("天気", "晴れ", "雨", "くもり", "気温"),
    ("犬", "猫", "散歩", "ペット"),
    ("仕事", "会議", "出張", "残業"),
)
FAKE_DIMS = len(TOPIC_WORDS) + 1


def fake_embedding(text: str) -> list[float]:
    """Topic membership, plus a constant axis so nothing is the zero vector."""

    values = [0.0] * FAKE_DIMS
    values[-1] = 0.05
    for index, words in enumerate(TOPIC_WORDS):
        if any(word in text for word in words):
            values[index] = 1.0
    return values


class RecordingService:
    """A fake embedding service that can be made slow, broken or absent."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        failure: Exception | None = None,
        status: int = 200,
        payload: object | None = None,
    ) -> None:
        self.delay = delay
        self.failure = failure
        self.status = status
        self.payload = payload
        self.requests: list[list[str]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        self.requests.append(list(body["input"]))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        if self.payload is not None:
            return httpx.Response(self.status, json=self.payload)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": fake_embedding(text)}
                    for index, text in enumerate(body["input"])
                ]
            },
        )

    def client(self, config: EmbeddingConfig, **kwargs: object) -> EmbeddingClient:
        return EmbeddingClient(
            config,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self)),
            **kwargs,  # type: ignore[arg-type]
        )


def embedding_config(**overrides: object) -> EmbeddingConfig:
    values: dict[str, object] = {
        "base_url": "http://127.0.0.1:8646",
        "model": "test-model",
        "dims": FAKE_DIMS,
        "query_deadline_seconds": 0.05,
        "write_deadline_seconds": 0.5,
    }
    values.update(overrides)
    return EmbeddingConfig(**values)  # type: ignore[arg-type]


class VectorMathTests(unittest.TestCase):
    def test_pack_and_unpack_round_trip_within_float32(self) -> None:
        original = normalize([0.5, -0.25, 0.125, 0.75, 0.0])
        assert original is not None
        restored = unpack_vector(pack_vector(original))
        assert restored is not None
        self.assertEqual(len(restored), len(original))
        for expected, actual in zip(original, restored):
            self.assertAlmostEqual(expected, actual, places=6)

    def test_a_corrupt_blob_is_not_a_vector(self) -> None:
        self.assertIsNone(unpack_vector(b"\x00\x01\x02"))
        self.assertIsNone(unpack_vector("not bytes"))  # type: ignore[arg-type]

    def test_normalize_rejects_what_cannot_be_compared(self) -> None:
        self.assertIsNone(normalize([0.0, 0.0]))
        self.assertIsNone(normalize([float("nan"), 1.0]))
        self.assertIsNone(normalize([float("inf"), 1.0]))

    def test_ranking_is_by_cosine_and_ties_break_deterministically(self) -> None:
        query = normalize(fake_embedding("ごはんの話"))
        assert query is not None
        rows = [
            (1, pack_vector(normalize(fake_embedding("夕飯はカレー")) or ())),
            (2, pack_vector(normalize(fake_embedding("今日の天気")) or ())),
            (3, pack_vector(normalize(fake_embedding("スープが好き")) or ())),
        ]
        ranked = rank_by_similarity(query, rows, limit=3)
        # 1 and 3 are both pure food and tie exactly; the higher id wins, every
        # time, so the prompt this feeds is reproducible.
        self.assertEqual(ranked, (3, 1, 2))
        self.assertEqual(rank_by_similarity(query, rows, limit=3), ranked)

    def test_a_similarity_floor_drops_the_weak_matches(self) -> None:
        query = normalize(fake_embedding("ごはんの話"))
        assert query is not None
        rows = [
            (1, pack_vector(normalize(fake_embedding("夕飯はカレー")) or ())),
            (2, pack_vector(normalize(fake_embedding("今日の天気")) or ())),
        ]
        self.assertEqual(
            rank_by_similarity(query, rows, limit=3, min_similarity=0.5), (1,)
        )

    def test_rows_of_the_wrong_width_are_skipped_not_compared(self) -> None:
        query = normalize([1.0, 0.0, 0.0])
        assert query is not None
        rows = [
            (1, pack_vector([1.0, 0.0])),
            (2, pack_vector([1.0, 0.0, 0.0])),
        ]
        self.assertEqual(rank_by_similarity(query, rows, limit=3), (2,))

    def test_fusion_rewards_agreement_and_is_a_pure_function(self) -> None:
        lexical = (10, 11, 12)
        vector = (12, 20, 10)
        fused = reciprocal_rank_fusion((lexical, vector), limit=4)
        # 12 is 3rd and 1st, 10 is 1st and 3rd: identical RRF mass, so the tie
        # breaks on id and 12 leads. 11 appears once and trails both.
        self.assertEqual(fused, (12, 10, 20, 11))
        self.assertEqual(reciprocal_rank_fusion((lexical, vector), limit=4), fused)

    def test_fusion_of_an_empty_ranking_is_the_other_ranking(self) -> None:
        self.assertEqual(reciprocal_rank_fusion(((3, 1, 2), ()), limit=3), (3, 1, 2))
        self.assertEqual(reciprocal_rank_fusion(((), ()), limit=3), ())


class EmbeddingClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_healthy_service_answers_a_normalized_vector(self) -> None:
        service = RecordingService()
        client = service.client(embedding_config())
        try:
            vector = await client.embed_query("ごはん")
            assert vector is not None
            self.assertEqual(len(vector), FAKE_DIMS)
            self.assertAlmostEqual(sum(value * value for value in vector), 1.0, 6)
            # The model's own convention is applied by the client, so swapping
            # models is configuration rather than code.
            self.assertEqual(service.requests, [["query: ごはん"]])
        finally:
            await client.aclose()

    async def test_a_slow_service_is_abandoned_at_the_deadline(self) -> None:
        # Two full seconds of silence from the service, against a 50 ms budget.
        service = RecordingService(delay=2.0)
        client = service.client(embedding_config(query_deadline_seconds=0.05))
        try:
            started = time.perf_counter()
            self.assertIsNone(await client.embed_query("ごはん"))
            elapsed = time.perf_counter() - started
        finally:
            await client.aclose()
        # The assertion that matters: the wall clock, not the return value.
        # 1.0 s rather than something tighter because the service being
        # abandoned sleeps 2.0 s: anything under that proves the deadline was
        # honoured, and the extra headroom is for a loaded CI runner, which was
        # measured 5x slower than a local container.
        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(elapsed, 0.05)

    async def test_an_unreachable_service_answers_none(self) -> None:
        service = RecordingService(
            failure=httpx.ConnectError("connection refused")
        )
        client = service.client(embedding_config())
        try:
            self.assertIsNone(await client.embed_query("ごはん"))
        finally:
            await client.aclose()

    async def test_an_http_error_answers_none(self) -> None:
        client = RecordingService(status=503).client(embedding_config())
        try:
            self.assertIsNone(await client.embed_query("ごはん"))
        finally:
            await client.aclose()

    async def test_malformed_payloads_answer_none(self) -> None:
        payloads: tuple[object, ...] = (
            {"data": []},
            {"data": [{"embedding": "not a list"}]},
            {"data": [{"embedding": [0.1, 0.2]}]},  # wrong dimension
            {"data": [{"embedding": [0.0] * FAKE_DIMS}]},  # zero magnitude
            [1, 2, 3],
        )
        for payload in payloads:
            client = RecordingService(payload=payload).client(embedding_config())
            try:
                self.assertIsNone(
                    await client.embed_query("ごはん"), msg=repr(payload)
                )
            finally:
                await client.aclose()

    async def test_batched_passages_are_matched_by_index_not_arrival(self) -> None:
        service = RecordingService(
            payload={
                "data": [
                    {"index": 1, "embedding": fake_embedding("犬")},
                    {"index": 0, "embedding": fake_embedding("ごはん")},
                ]
            }
        )
        client = service.client(embedding_config())
        try:
            vectors = await client.embed_passages(("ごはん", "犬"))
        finally:
            await client.aclose()
        assert vectors is not None
        expected = normalize(fake_embedding("ごはん"))
        assert expected is not None
        for wanted, actual in zip(expected, vectors[0]):
            self.assertAlmostEqual(wanted, actual, places=6)

    async def test_the_breaker_stops_paying_the_deadline_for_a_dead_service(
        self,
    ) -> None:
        clock = [0.0]
        service = RecordingService(failure=httpx.ConnectError("down"))
        client = service.client(
            embedding_config(breaker_failures=3, breaker_cooldown_seconds=60.0),
            now=lambda: clock[0],
        )
        try:
            for _ in range(3):
                self.assertIsNone(await client.embed_query("ごはん"))
            self.assertEqual(len(service.requests), 3)

            # Breaker open: the next twenty replies cost nothing at all.
            for _ in range(20):
                self.assertIsNone(await client.embed_query("ごはん"))
            self.assertEqual(len(service.requests), 3)

            # After the cooldown exactly one probe goes out, and a still-dead
            # service re-opens the breaker on that one rather than on another
            # three.
            clock[0] = 61.0
            self.assertIsNone(await client.embed_query("ごはん"))
            self.assertEqual(len(service.requests), 4)
            self.assertIsNone(await client.embed_query("ごはん"))
            self.assertEqual(len(service.requests), 4)

            # Recovery closes it and normal service resumes.
            service.failure = None
            clock[0] = 122.0
            self.assertIsNotNone(await client.embed_query("ごはん"))
            self.assertIsNotNone(await client.embed_query("ごはん"))
            self.assertEqual(len(service.requests), 6)
        finally:
            await client.aclose()

    async def test_blank_input_never_reaches_the_service(self) -> None:
        service = RecordingService()
        client = service.client(embedding_config())
        try:
            self.assertIsNone(await client.embed_query("   "))
            self.assertEqual(await client.embed_passages(()), ())
        finally:
            await client.aclose()
        self.assertEqual(service.requests, [])


class SemanticRetrievalTests(unittest.IsolatedAsyncioTestCase):
    """The quality claim, on the shape of the failure that motivated it."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "transcript.db"
        self.transcript = ConversationTranscript(self.path)
        await self.transcript.initialize()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _store(
        self,
        request_id: str,
        user: str,
        reply: str,
        created_at: float,
        *,
        model: str = "test-model",
        embed: bool = True,
    ) -> int:
        turn = await self.transcript.record(
            "room-1", request_id, user, reply, created_at=created_at
        )
        assert turn is not None
        if embed:
            vector = normalize(fake_embedding(embedding_text(user, reply)))
            assert vector is not None
            await self.transcript.store_vector(turn.id, vector, model=model)
        return turn.id

    def _semantic(self, text: str, **overrides: object) -> SemanticQuery:
        vector = normalize(fake_embedding(text))
        assert vector is not None
        values: dict[str, object] = {"candidates": 12, "scan_cap": 500}
        values.update(overrides)
        return SemanticQuery(vector=vector, model="test-model", **values)  # type: ignore[arg-type]

    async def test_a_paraphrase_bm25_provably_misses_is_retrieved(self) -> None:
        # Straight out of the live 187-turn store.
        food = await self._store(
            "req-food", "君の好きな食べ物何?", "あったかいスープが好きだよ", at(8, 1, 12)
        )
        for index in range(8):
            await self._store(
                f"req-filler-{index}",
                f"仕事の会議どうだった{index}",
                f"忙しかったよ{index}",
                at(8, 2, 9) + index,
            )

        query = "ごはんの話したっけ"
        lexical = await self.transcript.context(
            "room-1", query, recent_turns=0, retrieved_turns=3, now=NOW
        )
        # The premise: not one character in common, so BM25 has nothing to score.
        self.assertNotIn(food, [turn.id for turn in lexical.retrieved])

        hybrid = await self.transcript.context(
            "room-1",
            query,
            recent_turns=0,
            retrieved_turns=3,
            now=NOW,
            semantic=self._semantic(query),
        )
        self.assertIn(food, [turn.id for turn in hybrid.retrieved])

    async def test_bm25_keeps_its_rare_exact_token(self) -> None:
        # The other half of the bargain: fusion must not cost the lexical path
        # a match no dense model would have found. 大阪 is in no topic axis, so
        # the fake encoder is blind to it and only BM25 can win this.
        osaka = await self._store(
            "req-osaka", "大阪に行ってきたよ", "楽しかった？", at(8, 1, 12)
        )
        for index in range(8):
            await self._store(
                f"req-filler-{index}",
                f"今日の天気どう{index}",
                f"晴れだよ{index}",
                at(8, 2, 9) + index,
            )

        context = await self.transcript.context(
            "room-1",
            "大阪の話",
            recent_turns=0,
            retrieved_turns=3,
            now=NOW,
            semantic=self._semantic("大阪の話"),
        )
        self.assertIn(osaka, [turn.id for turn in context.retrieved])

    async def test_fusion_is_deterministic_across_repeated_calls(self) -> None:
        for index in range(12):
            await self._store(
                f"req-{index}",
                f"夕飯のごはん{index}",
                f"おいしい料理だね{index}",
                at(8, 1, 9) + index,
            )

        runs = set()
        for _ in range(5):
            context = await self.transcript.context(
                "room-1",
                "ごはんの話",
                recent_turns=0,
                retrieved_turns=3,
                now=NOW,
                semantic=self._semantic("ごはんの話"),
            )
            runs.add(tuple(turn.id for turn in context.retrieved))
        self.assertEqual(len(runs), 1)

    async def test_the_temporal_window_still_constrains_the_vector_sweep(
        self,
    ) -> None:
        # A perfect semantic match last month, a weaker one yesterday. Asked
        # about yesterday, the window wins: a better cosine outside the range
        # is a wrong answer to the question that was asked.
        old = await self._store(
            "req-old", "スープとごはんの話", "おいしい料理だね", at(7, 1, 12)
        )
        recent = await self._store(
            "req-recent", "夕飯どうする", "カレーにしよう", at(8, 4, 19)
        )

        context = await self.transcript.context(
            "room-1",
            "昨日ごはんの話したっけ",
            recent_turns=0,
            retrieved_turns=3,
            now=NOW,
            semantic=self._semantic("ごはんの話したっけ"),
        )
        found = [turn.id for turn in context.retrieved]
        self.assertIn(recent, found)
        self.assertNotIn(old, found)

    async def test_thinning_the_verbatim_window_composes_with_the_sweep(
        self,
    ) -> None:
        # The verbatim window and the vector sweep now feed one budget, so the
        # thinning rule has to hold with a vector in hand and not only on the
        # lexical path — a dense ranker promotes 「昨日のことは…」 against a
        # question about 昨日 even more confidently than BM25 did.
        for index in range(4):
            await self._store(
                f"req-real-{index}",
                f"ごはんの話{index}",
                f"おいしい料理だね{index}",
                at(8, 4, 10 + index),
            )
        for index in range(3):
            await self._store(
                f"req-no-{index}",
                f"昨日のごはんの話覚えてる？{index}",
                "昨日のことは、覚えてないよ",
                at(8, 5, 12 + index),
            )

        context = await self.transcript.context(
            "room-1",
            "昨日ごはんの話したっけ",
            recent_turns=4,
            retrieved_turns=3,
            now=NOW,
            semantic=self._semantic("ごはんの話したっけ"),
        )

        declined = [
            turn for turn in context.recent if is_deflection(turn.reply_text)
        ]
        self.assertEqual(len(declined), 1)
        self.assertEqual(declined[0].reply_text, "昨日のことは、覚えてないよ")
        # And what the window let go of does not come back through the other
        # half: a thinned turn is no longer excluded by id, so retrieval is now
        # free to reach it, and must still not answer a question about
        # yesterday with the robot's own refusal from today.
        self.assertTrue(context.retrieved)
        for turn in context.retrieved:
            self.assertFalse(is_deflection(turn.reply_text))

    async def test_the_embedded_text_is_the_residue_not_the_utterance(self) -> None:
        # What the caller embeds. A plain question is embedded whole; a dated
        # one loses its date, so the vector describes the topic and not the
        # calendar — the same discipline the lexical path already follows.
        self.assertEqual(retrieval_query_text("犬の散歩の話", NOW), "犬の散歩の話")
        self.assertNotIn("昨日", retrieval_query_text("昨日ごはんの話したっけ", NOW))
        # And a date word that is not a question is left entirely alone, so the
        # most common utterance there is keeps behaving as it did.
        self.assertEqual(
            retrieval_query_text("昨日は疲れた", NOW), "昨日は疲れた"
        )

    async def test_vectors_from_another_model_are_never_compared(self) -> None:
        stale = await self._store(
            "req-stale",
            "スープとごはんの話",
            "おいしい料理だね",
            at(8, 1, 12),
            model="some-older-model",
        )
        for index in range(8):
            await self._store(
                f"req-filler-{index}",
                f"仕事の会議どうだった{index}",
                f"忙しかったよ{index}",
                at(8, 2, 9) + index,
            )

        # No shared characters, so BM25 cannot find it either: whatever comes
        # back came back through the sweep, and nothing does.
        context = await self.transcript.context(
            "room-1",
            "夕飯どうする",
            recent_turns=0,
            retrieved_turns=3,
            now=NOW,
            semantic=self._semantic("夕飯どうする"),
        )
        self.assertNotIn(stale, [turn.id for turn in context.retrieved])
        self.assertEqual(
            await self.transcript.vector_count("room-1", model="test-model"), 8
        )
        self.assertEqual(
            await self.transcript.vector_count("room-1", model="some-older-model"), 1
        )

    async def test_retention_takes_the_vectors_with_the_turns(self) -> None:
        for index in range(5):
            turn = await self.transcript.record(
                "room-1",
                f"req-{index}",
                f"ごはんの話{index}",
                f"おいしいね{index}",
                created_at=at(8, 1, 9) + index,
                retention_turns=3,
            )
            assert turn is not None
            vector = normalize(fake_embedding("ごはん"))
            assert vector is not None
            await self.transcript.store_vector(turn.id, vector, model="test-model")

        # Three turns survive retention, and exactly three vectors with them.
        self.assertEqual(await self.transcript.count("room-1"), 3)
        self.assertEqual(
            await self.transcript.vector_count("room-1", model="test-model"), 3
        )

    async def test_a_vector_for_a_vanished_turn_is_not_written(self) -> None:
        vector = normalize(fake_embedding("ごはん"))
        assert vector is not None
        self.assertFalse(
            await self.transcript.store_vector(9_999, vector, model="test-model")
        )


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    """Resumable, idempotent, and safe to interrupt at any point."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.transcript = ConversationTranscript(
            Path(self.temporary.name) / "transcript.db"
        )
        await self.transcript.initialize()
        for index in range(10):
            await self.transcript.record(
                "room-1",
                f"req-{index}",
                f"発話{index}",
                f"返事{index}",
                created_at=at(8, 1, 9) + index,
            )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _embed_batch(self, size: int) -> int:
        batch = await self.transcript.turns_without_vectors(
            "room-1", model="test-model", limit=size
        )
        for turn in batch:
            vector = normalize(fake_embedding(turn.user_text))
            assert vector is not None
            await self.transcript.store_vector(turn.id, vector, model="test-model")
        return len(batch)

    async def test_an_interrupted_run_resumes_where_it_stopped(self) -> None:
        self.assertEqual(await self._embed_batch(4), 4)
        # Interruption is just "stop calling"; there is no state to repair.
        self.assertEqual(
            len(
                await self.transcript.turns_without_vectors(
                    "room-1", model="test-model", limit=100
                )
            ),
            6,
        )
        self.assertEqual(await self._embed_batch(100), 6)
        self.assertEqual(
            await self.transcript.vector_count("room-1", model="test-model"), 10
        )

    async def test_a_second_complete_run_does_nothing(self) -> None:
        await self._embed_batch(100)
        self.assertEqual(await self._embed_batch(100), 0)
        self.assertEqual(
            await self.transcript.vector_count("room-1", model="test-model"), 10
        )

    async def test_re_embedding_replaces_rather_than_duplicates(self) -> None:
        turns = await self.transcript.turns_without_vectors(
            "room-1", model="test-model", limit=1
        )
        first = normalize(fake_embedding("ごはん"))
        second = normalize(fake_embedding("天気"))
        assert first is not None and second is not None
        await self.transcript.store_vector(turns[0].id, first, model="test-model")
        await self.transcript.store_vector(turns[0].id, second, model="test-model")
        self.assertEqual(
            await self.transcript.vector_count("room-1", model="test-model"), 1
        )

    async def test_a_new_model_reopens_the_whole_backfill(self) -> None:
        await self._embed_batch(100)
        # Same turns, different space: everything needs embedding again, which
        # is what stops a model swap from silently half-working.
        self.assertEqual(
            len(
                await self.transcript.turns_without_vectors(
                    "room-1", model="a-different-model", limit=100
                )
            ),
            10,
        )


class ConfigTests(unittest.TestCase):
    def test_vectors_are_off_by_default(self) -> None:
        config = BridgeConfig(webhook_secret="secret")
        self.assertFalse(config.conversation_vectors_enabled)
        self.assertEqual(config.conversation_vector_query_deadline_seconds, 0.12)

    def test_vectors_require_a_transcript_to_search(self) -> None:
        with self.assertRaises(ValueError):
            BridgeConfig(
                webhook_secret="secret",
                conversation_memory_enabled=False,
                conversation_vectors_enabled=True,
            )

    def test_the_deadline_cannot_be_widened_into_a_regression(self) -> None:
        with self.assertRaises(ValueError):
            BridgeConfig(
                webhook_secret="secret",
                conversation_vector_query_deadline_seconds=1.5,
            )
        with self.assertRaises(ValueError):
            BridgeConfig(
                webhook_secret="secret",
                conversation_vector_query_deadline_seconds=0,
            )

    def test_other_vector_settings_are_validated(self) -> None:
        for override in (
            {"conversation_vector_service_url": "ftp://host"},
            {"conversation_vector_model": "  "},
            {"conversation_vector_dims": 0},
            {"conversation_vector_candidates": 0},
            {"conversation_vector_scan_cap": 0},
            {"conversation_vector_min_similarity": 1.5},
            {"conversation_vector_breaker_failures": 0},
        ):
            with self.assertRaises(ValueError, msg=repr(override)):
                BridgeConfig(webhook_secret="secret", **override)

    def test_the_client_config_is_projected_from_the_bridge_config(self) -> None:
        config = BridgeConfig(
            webhook_secret="secret",
            conversation_vector_service_url="http://127.0.0.1:9999",
            conversation_vector_model="ruri-v3-30m",
            conversation_vector_dims=256,
        )
        projected = config.embedding_config
        self.assertEqual(projected.base_url, "http://127.0.0.1:9999")
        self.assertEqual(projected.model, "ruri-v3-30m")
        self.assertEqual(projected.dims, 256)


class ProcessorVectorTests(unittest.IsolatedAsyncioTestCase):
    """The wiring, where the guarantee is either kept or lost."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = EventDatabase(self.root / "state.db")
        await self.database.initialize()
        self.bocco = FakeBocco()
        self.hermes = FakeHermes("短い返事です。")
        self.now = NOW
        self.service = RecordingService()
        self.clients: list[EmbeddingClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.aclose()
            if client._http_client is not None:
                await client._http_client.aclose()
        self.temporary.cleanup()

    def _runtime(
        self, *, vectors: bool = True, **config_overrides: object
    ) -> tuple[EventProcessor, EventWorker, ConversationTranscript, BridgeConfig]:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=self.root / "state.db",
            tunnel_enabled=False,
            worker_max_attempts=1,
            worker_retry_base_seconds=0,
            stream_sentences=False,
            conversation_memory_enabled=True,
            conversation_vectors_enabled=vectors,
            conversation_vector_model="test-model",
            conversation_vector_dims=FAKE_DIMS,
            conversation_vector_query_deadline_seconds=0.05,
            conversation_vector_write_deadline_seconds=0.5,
            **config_overrides,  # type: ignore[arg-type]
        )
        transcript = ConversationTranscript(config.transcript_path)
        client = self.service.client(config.embedding_config)
        self.clients.append(client)
        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            transcript=transcript,
            embeddings=client,
            reaction_choice=lambda phrases: phrases[0],
        )
        return processor, EventWorker(config, self.database, processor), transcript, config

    async def _speak(self, worker: EventWorker, request_id: str, text: str) -> None:
        event = FakeInboundEvent(
            request_id=request_id, event_type="message.received", speech_text=text
        )
        self.assertTrue(await self.database.enqueue(event))
        self.assertTrue(await worker.process_once())

    async def test_vectors_off_calls_nothing_and_renders_the_old_section(
        self,
    ) -> None:
        processor, worker, transcript, config = self._runtime(vectors=False)

        await self._speak(worker, "req-1", "君の好きな食べ物何?")
        await processor.wait_for_embeddings()
        await self._speak(worker, "req-2", "ごはんの話したっけ")
        await processor.wait_for_embeddings()

        # Not one request, and not one stored vector.
        self.assertEqual(self.service.requests, [])
        self.assertEqual(
            await transcript.vector_count("room-1", model="test-model"), 0
        )
        # And the section is byte-for-byte the lexical one, recomputed here
        # from the pre-feature call rather than copied from a golden file.
        expected = _format_conversation_section(
            await transcript.context(
                "room-1",
                "ごはんの話したっけ",
                recent_turns=config.conversation_recent_turns,
                retrieved_turns=config.conversation_retrieved_turns,
                now=self.now,
            ),
            turn_max_chars=config.conversation_turn_max_chars,
            max_chars=config.conversation_max_chars,
            now=self.now,
        )
        self.assertEqual(
            await processor._conversation_section("room-1", "ごはんの話したっけ"),
            expected,
        )

    async def test_a_stored_turn_is_embedded_in_the_background(self) -> None:
        processor, worker, transcript, _ = self._runtime()

        await self._speak(worker, "req-1", "君の好きな食べ物何?")
        # Nothing on the reply path waited for this; the test has to.
        await processor.wait_for_embeddings()

        self.assertEqual(
            await transcript.vector_count("room-1", model="test-model"), 1
        )
        self.assertIn(
            ["passage: 君の好きな食べ物何? 短い返事です。"], self.service.requests
        )

    async def test_a_slow_service_neither_delays_nor_breaks_the_reply(self) -> None:
        self.service.delay = 2.0
        processor, worker, transcript, config = self._runtime()

        await transcript.record(
            "room-1", "seed", "君の好きな食べ物何?", "あったかいスープが好きだよ",
            created_at=at(8, 1, 12),
        )

        started = time.perf_counter()
        section = await processor._conversation_section(
            "room-1", "ごはんの話したっけ"
        )
        elapsed = time.perf_counter() - started

        # The deadline is 50 ms; a two-second service must not buy more. The
        # ceiling only has to separate those two, so it is set well clear of
        # scheduling noise on a shared runner rather than close to the deadline.
        self.assertLess(elapsed, 1.0)
        # And what came back is the ordinary lexical section, not an error.
        self.assertEqual(
            section,
            _format_conversation_section(
                await transcript.context(
                    "room-1",
                    "ごはんの話したっけ",
                    recent_turns=config.conversation_recent_turns,
                    retrieved_turns=config.conversation_retrieved_turns,
                    now=self.now,
                ),
                turn_max_chars=config.conversation_turn_max_chars,
                max_chars=config.conversation_max_chars,
                now=self.now,
            ),
        )

    async def test_an_unreachable_service_still_delivers_a_reply(self) -> None:
        self.service.failure = httpx.ConnectError("connection refused")
        _, worker, _, _ = self._runtime()

        await self._speak(worker, "req-1", "ごはんの話したっけ")

        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    async def test_a_broken_service_still_delivers_a_reply(self) -> None:
        self.service.payload = {"data": [{"embedding": "garbage"}]}
        _, worker, _, _ = self._runtime()

        await self._speak(worker, "req-1", "ごはんの話したっけ")

        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])

    async def test_a_paraphrase_reaches_hermes_once_vectors_are_on(self) -> None:
        processor, worker, transcript, _ = self._runtime()

        await transcript.record(
            "room-1",
            "seed",
            "君の好きな食べ物何?",
            "あったかいスープが好きだよ",
            created_at=at(8, 1, 12),
        )
        turn_id = (
            await transcript.turns_without_vectors(
                "room-1", model="test-model", limit=1
            )
        )[0].id
        vector = normalize(
            fake_embedding("君の好きな食べ物何? あったかいスープが好きだよ")
        )
        assert vector is not None
        await transcript.store_vector(turn_id, vector, model="test-model")
        # Push it out of the verbatim recent window so only retrieval can find it.
        for index in range(6):
            await transcript.record(
                "room-1",
                f"filler-{index}",
                f"仕事の会議どうだった{index}",
                f"忙しかったよ{index}",
                created_at=at(8, 3, 9) + index,
            )

        await self._speak(worker, "req-1", "ごはんの話したっけ")
        await processor.wait_for_embeddings()

        instructions = self.hermes.responded[-1][2]
        self.assertIn("あったかいスープが好きだよ", instructions)
        self.assertIn("query: ごはんの話したっけ", self.service.requests[0])

    async def test_a_pure_date_question_never_calls_the_service(self) -> None:
        processor, _, transcript, _ = self._runtime()

        await transcript.record(
            "room-1", "seed", "犬の散歩", "楽しかったね", created_at=at(8, 4, 19)
        )
        # 「昨日？」 is all date and no subject: strip the date and only a
        # question mark is left, which neither ranker can search. The temporal
        # spread answers it, and no round trip is spent finding that out.
        section = await processor._conversation_section("room-1", "昨日？")

        self.assertEqual(
            [
                request
                for request in self.service.requests
                if request[0].startswith("query:")
            ],
            [],
        )
        # And it still answered: the window found yesterday's turn on its own.
        self.assertIn("犬の散歩", section)


if __name__ == "__main__":
    unittest.main()
