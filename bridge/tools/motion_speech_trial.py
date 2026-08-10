#!/usr/bin/env python3
"""Settle whether BOCCO emo plays a dispatched motion while it is speaking.

Runs on the Pi beside the live bridge. Read-only with respect to bridge state:
it reads the token file and the state database, and it never writes either.

Protocol (docs/design/motion-speech-concurrency.md):
  1. send one unique, long Japanese utterance
  2. wait for that utterance's real newMessageMotion webhook (the speech anchor)
  3. --delay seconds later, send one motion, preset or authored document
  4. print every subsequent webhook so motion.finished and emo_talk.finished can
     be ordered against the anchor

The decisive evidence is the human watching the robot. The webhook timeline is
corroboration: it is on the BOCCO event clock, which lags reality by about two
seconds, so it orders events but does not time them precisely. Custom documents
emit no motion.finished at all, so for those the observation is the only
evidence there is.

Run it on the Pi as the bridge user, with the service environment loaded:

    sudo -u bocco-bridge env $(sudo grep -E '^(BOCCO_|BRIDGE_)' \\
        /etc/bocco-bridge/bridge.env | xargs) \\
      /opt/bocco-bridge/.venv/bin/python bridge/tools/motion_speech_trial.py \\
      --motion BUNBUN --delay 0

Note that the bridge answers the trial's own utterance as though a person
had spoken it while BOCCO_AGENT_USER_UUID is unset; see the design document.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta, timezone

from bocco_bridge.bocco.client import BoccoClient
from bocco_bridge.bocco.models import BoccoClientConfig
from bocco_bridge.bocco.token_store import AtomicFileTokenStore
from bocco_bridge.custom_motions import CUSTOM_MOTION_DOCUMENTS

JST = timezone(timedelta(hours=9), "JST")

# Long enough that a motion dispatched a few seconds in still lands mid-speech,
# and unique so emo_talk.finished cannot hash-match an older observation.
UTTERANCE = (
    "よく見ていてね。いまから、"
    "わたしが話しているあいだに、あたまが大きくうごくはずです。"
    "うごいたかどうか、おぼえておいてください。"
    "いち、に、さん、し、ご、ろく、なな、はち、きゅう、じゅう。"
    "テスト{nonce}、これでおわりです。"
)

# The trial must not refresh: the running bridge holds its refresh token in
# memory, so a rotation from this process would strand it at its next refresh.
MIN_TOKEN_LIFETIME_SECONDS = 600

POLL_INTERVAL_SECONDS = 0.1
ANCHOR_TIMEOUT_SECONDS = 60.0
DISPATCH_DELAY_SECONDS = 1.0
OBSERVE_SECONDS = 45.0


def stamp(when: float | None = None) -> str:
    return datetime.fromtimestamp(when or time.time(), JST).strftime("%H:%M:%S.%f")[:-3]


def say(message: str) -> None:
    print(f"[{stamp()}] {message}", flush=True)


def open_db(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def latest_rowid(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(rowid) AS top FROM inbound_events").fetchone()
    return row["top"] or 0


def events_after(connection: sqlite3.Connection, rowid: int) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT rowid, event_type, event_detail, room_uuid, received_at,"
            "       speech_text, message_media"
            "  FROM inbound_events WHERE rowid > ? ORDER BY rowid",
            (rowid,),
        )
    )


def describe(row: sqlite3.Row) -> str:
    detail = f":{row['event_detail']}" if row["event_detail"] else ""
    text = f"  text={row['speech_text']!r}" if row["speech_text"] else ""
    return f"{row['event_type']}{detail}  bocco_clock={row['received_at']}{text}"


def newest_room(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT room_uuid FROM inbound_events"
        " WHERE room_uuid IS NOT NULL ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise SystemExit("no room uuid in the database; let the robot see an event first")
    return row["room_uuid"]


def build_client() -> BoccoClient:
    token_file = os.environ.get("BOCCO_TOKEN_FILE")
    if not token_file:
        raise SystemExit("BOCCO_TOKEN_FILE is not set; source /etc/bocco-bridge/bridge.env")

    store = AtomicFileTokenStore(token_file)
    tokens = store.load()
    expires_at = tokens.access_expires_at
    if expires_at is not None:
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        if remaining < MIN_TOKEN_LIFETIME_SECONDS:
            raise SystemExit(
                f"access token expires in {remaining:.0f}s; refusing to run because a "
                "refresh here would rotate the token out from under the live bridge"
            )
        say(f"access token valid for another {remaining / 60:.0f} min")

    base_url = os.environ.get("BOCCO_PLATFORM_BASE_URL", "https://platform-api.bocco.me")
    return BoccoClient(BoccoClientConfig(base_url=base_url), store)


async def list_motions() -> None:
    client = build_client()
    for preset in await client.list_motions():
        print(f"{preset.name}")


async def run_trial(
    motion_name: str, db_path: str, dispatch_delay: float, custom: bool
) -> None:
    connection = open_db(db_path)
    room = newest_room(connection)
    client = build_client()

    # Custom documents go through POST /motions instead of the preset endpoint,
    # and they emit no motion.finished webhook — for those the human observation
    # is the only evidence, which is why the label below says so.
    if custom:
        document = CUSTOM_MOTION_DOCUMENTS.get(motion_name)
        if document is None:
            raise SystemExit(
                f"no custom motion named {motion_name!r};"
                f" known: {', '.join(CUSTOM_MOTION_DOCUMENTS)}"
            )

        async def dispatch() -> None:
            await client.send_custom_motion(room, document)

        label = f"custom:{motion_name}"
    else:
        presets = await client.list_motions()
        match = next((p for p in presets if p.name == motion_name), None)
        if match is None:
            raise SystemExit(
                f"no preset named {motion_name!r}; run with --list-motions to see the catalog"
            )

        async def dispatch() -> None:
            await client.send_motion(room, match.uuid)

        label = f"preset:{motion_name}"

    nonce = int(time.time()) % 10000
    utterance = UTTERANCE.format(nonce=nonce)
    say(f"room={room[:8]}…  motion={label}  nonce={nonce}")
    say(f"utterance is {len(utterance)} characters (~{len(utterance) * 0.15:.1f}s of speech)")
    print()

    cursor = latest_rowid(connection)
    sent_at = time.time()
    await client.send_text(room, utterance)
    say("TEXT SENT — watch the robot now")

    anchor_at: float | None = None
    deadline = sent_at + ANCHOR_TIMEOUT_SECONDS
    while time.time() < deadline:
        for row in events_after(connection, cursor):
            cursor = row["rowid"]
            say(f"  webhook  {describe(row)}")
            if row["event_type"] == "motion.finished" and row["event_detail"] == "newMessageMotion":
                anchor_at = time.time()
        if anchor_at is not None:
            break
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    if anchor_at is None:
        say("no newMessageMotion anchor arrived; aborting rather than guessing a dispatch time")
        return

    print()
    say(f"ANCHOR — speech has started. dispatching {label} in {dispatch_delay}s")
    await asyncio.sleep(dispatch_delay)
    dispatch_at = time.time()
    await dispatch()
    say(f"MOTION SENT (+{dispatch_at - anchor_at:.2f}s after anchor) — is the head moving WHILE it talks?")
    print()

    end = dispatch_at + OBSERVE_SECONDS
    while time.time() < end:
        for row in events_after(connection, cursor):
            cursor = row["rowid"]
            offset = time.time() - dispatch_at
            say(f"  webhook  (+{offset:5.1f}s from dispatch)  {describe(row)}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    print()
    say("observation window closed")
    say("CONCURRENT if the head moved while the voice was still going;")
    say("QUEUED if it only moved after the utterance finished.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-motions", action="store_true")
    parser.add_argument("--motion", default="")
    parser.add_argument("--db", default=os.environ.get("BOCCO_BRIDGE_DB", "/var/lib/bocco-bridge/state.db"))
    parser.add_argument("--delay", type=float, default=DISPATCH_DELAY_SECONDS)
    parser.add_argument("--custom", action="store_true", help="send an authored document via POST /motions")
    args = parser.parse_args()

    if args.list_motions:
        asyncio.run(list_motions())
        return
    if not args.motion:
        raise SystemExit("pass --motion NAME (see --list-motions)")
    asyncio.run(run_trial(args.motion, args.db, args.delay, args.custom))


if __name__ == "__main__":
    main()
