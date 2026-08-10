#!/usr/bin/env python3
"""Backfill the bridge transcript from Hermes' compacted session history.

Retrieval memory was enabled on 2026-08-05, so it starts empty and emo cannot
recall anything said before that. The conversation itself was never lost —
Hermes compacted it out of the model's active context but kept the rows. This
walks those rows and replays the real exchanges into the bridge's own store.

Writes go through ConversationTranscript.record(), not raw SQL, so the
normalization, the FTS triggers and the per-request idempotency are exactly
what the live path uses. Re-running is safe: a turn already present under the
same request id is returned unchanged rather than duplicated.

Only genuine conversation is replayed. The bridge drives Hermes with
synthetic prompts of its own — reaction-bank generation, accelerometer and
radar reaction requests, ambient-light greetings, skill data — and those are
machinery, not things anyone said. Feeding them into a memory that answers
"what did we talk about" would be worse than starting empty.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from bocco_bridge.transcript import ConversationTranscript

# Prompts the bridge generates. Matched against the start of the user message,
# except the compaction marker, which Hermes injects mid-history.
SYNTHETIC_PREFIXES = (
    "Write exactly",
    "This is the character you are writing for",
    "あなたは今、",
    "人が近づきました",
    "現在のPiローカル時刻",
    "次のskill_data",
)
SYNTHETIC_ANYWHERE = (
    "[CONTEXT COMPACTION",
    # Health/latency probes the bridge sends on its own behalf. They read as
    # ordinary Japanese, so only the instruction tail distinguishes them.
    "一文だけの短い自然な日本語で返事してください",
    "外部ツールは使わ",
)


def is_synthetic(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if any(stripped.startswith(prefix) for prefix in SYNTHETIC_PREFIXES):
        return True
    return any(marker in stripped for marker in SYNTHETIC_ANYWHERE)


def collect_exchanges(state_db: Path) -> list[tuple[str, str, str, float]]:
    """Pair each real user message with the reply that followed it.

    A single turn can span several assistant rows: tool calls leave empty
    content behind and only the last row carries the spoken text. Taking the
    last non-empty assistant message before the next user message is what
    reconstructs the exchange as it was actually heard.
    """

    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    exchanges: list[tuple[str, str, str, float]] = []

    sessions = [
        row["id"]
        for row in connection.execute("SELECT id FROM sessions ORDER BY started_at")
    ]
    for session_id in sessions:
        rows = list(
            connection.execute(
                "SELECT id, role, content, timestamp FROM messages"
                " WHERE session_id = ? AND role IN ('user', 'assistant')"
                " ORDER BY timestamp, id",
                (session_id,),
            )
        )
        pending_user: sqlite3.Row | None = None
        reply: str | None = None
        for row in rows:
            if row["role"] == "user":
                if pending_user is not None and reply:
                    exchanges.append(
                        (
                            f"backfill:{session_id[:8]}:{pending_user['id']}",
                            pending_user["content"].strip(),
                            reply.strip(),
                            float(pending_user["timestamp"]),
                        )
                    )
                text = row["content"] or ""
                pending_user = None if is_synthetic(text) else row
                reply = None
            elif pending_user is not None:
                content = (row["content"] or "").strip()
                if content:
                    reply = content
        if pending_user is not None and reply:
            exchanges.append(
                (
                    f"backfill:{session_id[:8]}:{pending_user['id']}",
                    pending_user["content"].strip(),
                    reply.strip(),
                    float(pending_user["timestamp"]),
                )
            )

    exchanges.sort(key=lambda item: item[3])
    return exchanges


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--transcript-db", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    exchanges = collect_exchanges(Path(args.state_db))
    print(f"real exchanges recovered: {len(exchanges)}")
    if exchanges:
        import datetime

        first = datetime.datetime.fromtimestamp(exchanges[0][3])
        last = datetime.datetime.fromtimestamp(exchanges[-1][3])
        print(f"spanning {first:%m-%d %H:%M} -> {last:%m-%d %H:%M}")
        print("\nsample:")
        step = max(1, len(exchanges) // 20)
        for _, user, reply, when in exchanges[::step]:
            stamp = __import__("datetime").datetime.fromtimestamp(when)
            print(f"  {stamp:%m-%d %H:%M}  {user[:38]!r} -> {reply[:38]!r}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    transcript = ConversationTranscript(Path(args.transcript_db))
    await transcript.initialize()
    written = skipped = 0
    for request_id, user, reply, when in exchanges:
        # retention_turns=0: never trim mid-backfill. The live path applies the
        # configured bound on the next real exchange, so the newest survive.
        turn = await transcript.record(
            args.room, request_id, user, reply, created_at=when, retention_turns=0
        )
        if turn is None:
            skipped += 1
        else:
            written += 1
    print(f"\nrecorded {written}, skipped {skipped} (empty after clipping)")
    print(f"transcript now holds {await transcript.count(args.room)} turns")


if __name__ == "__main__":
    asyncio.run(main())
