#!/usr/bin/env python3
"""Embed transcript turns that were stored before semantic retrieval existed.

The live store holds 187 turns recovered by ``backfill_transcript.py`` plus
whatever has been said since; none of them carry a vector, so until this has
run, semantic retrieval quietly finds nothing and the bridge behaves exactly as
it did on BM25 alone. That is the intended failure mode, not a bug — but it is
also not the point of the feature.

Three properties are deliberate and each earns its complexity:

*Resumable.* Progress is the ``turn_vectors`` table itself, not a cursor file.
Every batch asks the database which turns still lack a vector *for this model*,
so an interrupted run resumes exactly where it stopped and a completed run does
nothing on a second pass.

*Rate-limited.* Someone may be talking to emo while this runs. Each batch is
small and followed by a sleep, so the embedding service returns to idle between
batches instead of holding four cores for a minute. The default pace embeds the
187-turn store in roughly a minute of wall clock while leaving the machine
responsive throughout; ``--batch-size 1 --pace 1.0`` makes it slower still.

*Interruptible.* Ctrl-C stops after the batch in flight, which is already
committed. There is no half-written state to clean up and no reason to be
careful about when you press it.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

from bocco_bridge.embeddings import EmbeddingClient, EmbeddingConfig
from bocco_bridge.transcript import ConversationTranscript, embedding_text


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript-db", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument("--service-url", default="http://127.0.0.1:8646")
    parser.add_argument("--model", default="multilingual-e5-small")
    parser.add_argument("--dims", type=int, default=384)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="turns per request; keep small so one batch is a short burst",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=0.25,
        help="seconds to idle between batches, yielding the CPU to live replies",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=30.0,
        help="per-batch timeout; generous because nothing waits on this",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="stop after this many turns (0 = all)"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.batch_size < 1 or args.pace < 0:
        parser.error("--batch-size must be positive and --pace non-negative")

    transcript = ConversationTranscript(Path(args.transcript_db))
    await transcript.initialize()
    total = await transcript.count(args.room)
    embedded = await transcript.vector_count(args.room, model=args.model)
    print(f"room {args.room}: {total} turns, {embedded} already embedded")

    pending = await transcript.turns_without_vectors(
        args.room, model=args.model, limit=max(args.limit, 1) if args.limit else total
    )
    if args.dry_run:
        print(f"--dry-run: {len(pending)} turns would be embedded, nothing written.")
        for turn in pending[:10]:
            print(f"  #{turn.id} {embedding_text(turn.user_text, turn.reply_text)[:60]}")
        return 0
    if not pending:
        print("nothing to do.")
        return 0

    stopping = False

    def request_stop(*_signal_args: object) -> None:
        nonlocal stopping
        if not stopping:
            print("\nstopping after the batch in flight...", file=sys.stderr)
        stopping = True

    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, request_stop)

    client = EmbeddingClient(
        EmbeddingConfig(
            base_url=args.service_url,
            model=args.model,
            dims=args.dims,
            # A backfill has no reply waiting on it, so the breaker would only
            # get in the way; one long deadline and an honest error is better.
            query_deadline_seconds=args.deadline,
            write_deadline_seconds=args.deadline,
            breaker_failures=10_000,
            breaker_cooldown_seconds=0.0,
        )
    )
    written = failed = 0
    started = time.monotonic()
    try:
        # Re-query each round rather than iterating one snapshot: the live
        # bridge may be embedding new turns underneath us, and retention may be
        # deleting old ones. Asking the table what is left is always right.
        while not stopping:
            if args.limit and written >= args.limit:
                break
            batch_size = args.batch_size
            if args.limit:
                batch_size = min(batch_size, args.limit - written)
            batch = await transcript.turns_without_vectors(
                args.room, model=args.model, limit=batch_size
            )
            if not batch:
                break
            texts = [
                embedding_text(turn.user_text, turn.reply_text) for turn in batch
            ]
            vectors = await client.embed_passages(texts)
            if vectors is None:
                failed += len(batch)
                print(
                    f"batch of {len(batch)} failed; is the service up at "
                    f"{args.service_url}?",
                    file=sys.stderr,
                )
                break
            for turn, vector in zip(batch, vectors):
                if await transcript.store_vector(
                    turn.id, vector, model=args.model
                ):
                    written += 1
            # Overwrite in place on a terminal, one line per batch in a log.
            print(
                f"  embedded {written} turns",
                end="\r" if sys.stdout.isatty() else "\n",
                flush=True,
            )
            if args.pace:
                await asyncio.sleep(args.pace)
    finally:
        await client.aclose()

    elapsed = time.monotonic() - started
    remaining = len(
        await transcript.turns_without_vectors(
            args.room, model=args.model, limit=total or 1
        )
    )
    print(
        f"\nembedded {written} turns in {elapsed:.1f}s"
        f" ({failed} failed, {remaining} still without a vector)"
    )
    print("re-run this command at any time; it resumes and never duplicates.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
