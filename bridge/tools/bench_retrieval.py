#!/usr/bin/env python3
"""Measure the retrieval path with semantic search off and on.

The claim this feature had to defend is that it costs no reply latency. That
is not a thing to assert, so this measures it: the same synthetic store, the
same queries, the same code, once with ``semantic=None`` and once with a
vector in hand, reported as p50/p95.

Two costs are deliberately separated, because they are bounded by different
mechanisms:

  --local   the database work and the brute-force sweep, with the query vector
            supplied rather than fetched. This is pure CPU inside
            ``asyncio.to_thread`` and is what the "on" column below measures.
  --service the round trip to a running embedding service, which is the part
            the hard deadline exists for. Run this one on the Pi; it is the
            only number that cannot be estimated honestly from a laptop.

Usage:
    python3 bridge/tools/bench_retrieval.py --local
    python3 bridge/tools/bench_retrieval.py --service --url http://127.0.0.1:8646
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import tempfile
import time
from pathlib import Path

from bocco_bridge.embeddings import EmbeddingClient, EmbeddingConfig, normalize
from bocco_bridge.transcript import ConversationTranscript, SemanticQuery


DIMS = 384
# Query shapes taken from the live store: short, mostly recall questions.
TOPICS = (
    ("好きな食べ物は何", "あったかいスープが好きだよ"),
    ("元気", "元気だよ"),
    ("今日の天気は", "晴れているみたいだよ"),
    ("大阪に行ってきた", "楽しかったならよかったね"),
    ("エモは何ができるの", "おしゃべりとダンスができるよ"),
    ("犬の散歩はいつ", "朝の七時だよ"),
)
# Stored turns are built from this bag rather than from repeats of TOPICS. A
# store of 500 copies of six sentences is not a benchmark, it is a worst case
# for BM25 specifically: every trigram of a query matches eighty rows, the FTS
# candidate set explodes, and the lexical path alone swings from 0.6 ms to
# 26 ms — noise that has nothing to do with the feature under test and would
# swamp its cost in both columns. A household's 500 turns look like this.
VOCABULARY = (
    "散歩 天気 ごはん スープ 大阪 京都 電車 学校 仕事 音楽 映画 旅行"
    " 犬 猫 コーヒー 紅茶 掃除 洗濯 買い物 公園 図書館 病院 誕生日 プレゼント"
    " 宿題 テスト 会議 出張 温泉 花火 祭り 料理 桜 紅葉 雪 雨 風 暑い 寒い"
    " 眠い 疲れた 楽しい 嬉しい 忙しい 静か 賑やか 朝 昼 夜 週末 来週 去年"
).split()


def percentiles(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return p50 * 1000.0, p95 * 1000.0


async def build_store(path: Path, turns: int, model: str) -> ConversationTranscript:
    transcript = ConversationTranscript(path)
    await transcript.initialize()
    generator = random.Random(20260805)
    now = time.time()
    for index in range(turns):
        # Every sixth turn is one of the topic pairs, so the queries below do
        # have real lexical matches to find; the rest is household chatter.
        if index % 6 == 0:
            user, reply = TOPICS[(index // 6) % len(TOPICS)]
        else:
            user = "".join(generator.sample(VOCABULARY, 3)) + "はどう"
            reply = "".join(generator.sample(VOCABULARY, 3)) + "だね"
        turn = await transcript.record(
            "bench-room",
            f"bench-{index}",
            user,
            reply,
            created_at=now - (turns - index) * 90.0,
            retention_turns=0,
        )
        assert turn is not None
        vector = normalize([generator.gauss(0.0, 1.0) for _ in range(DIMS)])
        assert vector is not None
        await transcript.store_vector(turn.id, vector, model=model, created_at=now)
    return transcript


async def bench_local(turns: int, rounds: int, model: str) -> None:
    generator = random.Random(7)
    # Three shapes, measured apart rather than blended. Mixing them hides the
    # thing worth seeing: the temporal branch is an order of magnitude more
    # expensive than the plain one *before* this feature existed, so a single
    # p95 over a mixed workload reports the cost of time-aware retrieval and
    # calls it the cost of embeddings.
    # 「今日の天気は」 carries a date word, so as a query it is a temporal one
    # however it is labelled; keeping it out of the plain set is what stops the
    # plain p95 from silently reporting the temporal branch.
    plain = tuple(
        user for user, _reply in TOPICS if "今日" not in user and "昨日" not in user
    )
    shapes = {
        "plain topical": [
            f"{plain[index % len(plain)]}の話したっけ" for index in range(rounds)
        ],
        "temporal + topic": [
            f"昨日{plain[index % len(plain)]}の話したっけ" for index in range(rounds)
        ],
        # No residue survives the date strip, so the vector path is skipped
        # entirely and both columns must be identical.
        "temporal only": ["昨日何した" for _ in range(rounds)],
    }
    with tempfile.TemporaryDirectory() as directory:
        transcript = await build_store(Path(directory) / "bench.db", turns, model)
        now = time.time()
        vectors = [
            normalize([generator.gauss(0.0, 1.0) for _ in range(DIMS)])
            for _ in range(rounds)
        ]

        async def run(semantic: SemanticQuery | None, query: str) -> float:
            started = time.perf_counter()
            await transcript.context(
                "bench-room",
                query,
                recent_turns=6,
                retrieved_turns=3,
                now=now,
                semantic=semantic,
            )
            return time.perf_counter() - started

        print(f"retrieval path, {turns} stored turns, {rounds} queries per shape")
        print(f"{'':20s} {'semantic OFF':>22s} {'semantic ON':>22s}   delta")
        for label, queries in shapes.items():
            # Warm the page cache so the first sample is not measuring SQLite
            # opening a file.
            for index in range(10):
                await run(None, queries[index % len(queries)])
            off = [await run(None, queries[index]) for index in range(rounds)]
            on = [
                await run(
                    SemanticQuery(vector=vectors[index], model=model, candidates=12),
                    queries[index],
                )
                for index in range(rounds)
            ]
            off_p50, off_p95 = percentiles(off)
            on_p50, on_p95 = percentiles(on)
            print(
                f"{label:20s} "
                f"p50 {off_p50:7.3f}  p95 {off_p95:7.3f}   "
                f"p50 {on_p50:7.3f}  p95 {on_p95:7.3f}   "
                f"p50 {on_p50 - off_p50:+6.3f}  p95 {on_p95 - off_p95:+6.3f}"
            )
    print("  all figures in ms, and exclude the embedding round trip (--service)")


async def bench_service(url: str, model: str, dims: int, rounds: int) -> None:
    client = EmbeddingClient(
        EmbeddingConfig(
            base_url=url,
            model=model,
            dims=dims,
            # Measure the service, do not police it: a benchmark that timed out
            # at 120 ms would report 120 ms instead of the truth.
            query_deadline_seconds=10.0,
            write_deadline_seconds=10.0,
            breaker_failures=10_000,
        )
    )
    try:
        probe = await client.embed_query("こんにちは")
        if probe is None:
            print(f"no embedding service answering at {url}")
            return
        samples = []
        for index in range(rounds):
            text = f"{TOPICS[index % len(TOPICS)][0]}の話したっけ"
            started = time.perf_counter()
            vector = await client.embed_query(text)
            samples.append(time.perf_counter() - started)
            if vector is None:
                print(f"request {index} failed")
                return
        p50, p95 = percentiles(samples)
        print(f"embedding service {url} ({model}, {dims}d), {rounds} short queries")
        print(f"  encode + loopback HTTP   p50 {p50:7.2f} ms   p95 {p95:7.2f} ms")
        print(f"  configured deadline is 120 ms; p95 headroom {120.0 - p95:+.1f} ms")
    finally:
        await client.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--service", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:8646")
    parser.add_argument("--model", default="multilingual-e5-small")
    parser.add_argument("--dims", type=int, default=DIMS)
    parser.add_argument("--turns", type=int, default=500)
    parser.add_argument("--rounds", type=int, default=200)
    args = parser.parse_args()

    if not args.local and not args.service:
        args.local = True
    if args.local:
        await bench_local(args.turns, args.rounds, args.model)
    if args.service:
        await bench_service(args.url, args.model, args.dims, args.rounds)


if __name__ == "__main__":
    asyncio.run(main())
