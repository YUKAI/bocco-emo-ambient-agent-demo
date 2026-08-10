#!/usr/bin/env python3
"""Extract events from transcript turns that were stored before extraction existed.

The live store holds 226 exchanges recorded before anything looked at them for
occurrences. They are the household's actual history, so they are worth
walking once — but they are also the corpus that produced the two documented
failures, so this is the safest possible place to *look* at the extraction rule
before it ever writes to the robot on its own.

Hence --dry-run by default, and hence the printed verdict for every turn: this
tool is as much an audit instrument as a migration. Read the RECORD lines and
the REJECT lines side by side and judge the rule.

Resumable: an exchange already in the event store under its own request id is
skipped without a model call, so an interrupted run costs nothing to repeat.

Rate-limited: --interval seconds between calls, defaulting high enough that a
long backfill cannot monopolise the one background Hermes lane while somebody
is talking to the robot. This calls Hermes DIRECTLY rather than through
HermesPriorityGate — the gate lives inside the running bridge process and this
is a separate process — so the interval is the only thing standing between a
backfill and a slow reply. Do not set it to zero on a live robot.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sqlite3
import sys
import time
from pathlib import Path

from bocco_bridge.event_extraction import EXTRACTION_INSTRUCTIONS
from bocco_bridge.event_memory import (
    EventExtractionError,
    EventMemory,
    event_extraction_prompt,
    parse_extracted_event,
    render_event_line,
)
from bocco_bridge.hermes import HermesClient, HermesConfig


def load_turns(
    transcript_db: Path, room: str | None, limit: int, every: int
) -> list[tuple[str, str, str, float]]:
    """Read exchanges oldest-first, optionally sampling every Nth one."""

    connection = sqlite3.connect(f"file:{transcript_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if room:
            rows = connection.execute(
                "SELECT request_id, user_text, reply_text, created_at, room_uuid"
                " FROM turns WHERE room_uuid = ? ORDER BY created_at, id",
                (room,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT request_id, user_text, reply_text, created_at, room_uuid"
                " FROM turns ORDER BY created_at, id"
            ).fetchall()
    finally:
        connection.close()
    selected = rows[:: max(1, every)]
    if limit > 0:
        selected = selected[:limit]
    return [
        (
            str(row["request_id"]),
            str(row["user_text"]),
            str(row["reply_text"]),
            float(row["created_at"]),
        )
        for row in selected
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript-db", required=True)
    parser.add_argument("--event-db", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument(
        "--hermes-url", default=os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642")
    )
    parser.add_argument("--model", default=os.environ.get("HERMES_MODEL", "hermes-agent"))
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between Hermes calls; keep this generous on a live robot",
    )
    parser.add_argument(
        "--every", type=int, default=1, help="sample every Nth exchange"
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--write",
        action="store_true",
        help="actually store the extracted events; without it nothing is written",
    )
    args = parser.parse_args()

    api_key = os.environ.get("API_SERVER_KEY", "").strip()
    if not api_key:
        print("API_SERVER_KEY must be set in the environment", file=sys.stderr)
        return 2

    turns = load_turns(
        Path(args.transcript_db), args.room, args.limit, args.every
    )
    print(f"exchanges to consider: {len(turns)}")
    if not turns:
        return 0

    events = EventMemory(Path(args.event_db))
    await events.initialize()
    hermes = HermesClient(
        HermesConfig(
            api_key=api_key,
            api_base_url=args.hermes_url,
            model=args.model,
            response_timeout_seconds=90.0,
            max_output_tokens=200,
        )
    )

    recorded = rejected = skipped = failed = 0
    try:
        for index, (request_id, user_text, reply_text, said_at) in enumerate(turns):
            stamp = datetime.datetime.fromtimestamp(said_at).strftime("%m-%d %H:%M")
            if await events.has_source(request_id):
                skipped += 1
                continue
            if index:
                await asyncio.sleep(max(0.0, args.interval))
            try:
                generated = await hermes.respond(
                    conversation=None,
                    text=event_extraction_prompt(user_text, reply_text, said_at),
                    instructions=EXTRACTION_INSTRUCTIONS,
                )
                candidate = parse_extracted_event(generated, said_at)
            except EventExtractionError as exc:
                failed += 1
                print(f"  UNPARSABLE {stamp}  {user_text[:32]!r}  ({exc})")
                continue
            except Exception as exc:  # pragma: no cover - operational tool
                failed += 1
                print(f"  ERROR      {stamp}  {type(exc).__name__}")
                continue
            if candidate is None:
                rejected += 1
                print(f"  reject {stamp}  {user_text[:34]!r} -> {reply_text[:30]!r}")
                continue
            recorded += 1
            print(
                f"  RECORD {stamp}  {user_text[:34]!r}\n"
                f"         => [{candidate.kind}] {candidate.text}"
                f"  @{datetime.datetime.fromtimestamp(candidate.occurred_at):%Y-%m-%d}"
            )
            if args.write:
                stored = await events.record(
                    args.room,
                    request_id,
                    candidate,
                    said_at=said_at,
                    source_user_text=user_text,
                    source_reply_text=reply_text,
                    # retention=0: never trim mid-backfill. The live path
                    # applies the configured bound on the next real exchange.
                    retention=0,
                )
                if stored is None:
                    print("         (duplicate of an event already stored)")
    finally:
        close = getattr(hermes, "aclose", None)
        if close is not None:
            await close()

    print(
        f"\nrecorded {recorded}, rejected {rejected}, "
        f"skipped {skipped}, failed {failed}"
    )
    if not args.write:
        print("--write was not given: nothing was stored.")
    else:
        print(f"event store now holds {await events.count(args.room)} events")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
