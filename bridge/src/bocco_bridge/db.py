"""SQLite-backed durable event queue and delivery checkpoints."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Protocol
import os
import json
import sqlite3
import time

from .choreography import (
    SpeechCalibration,
    update_calibration,
    update_delivery_anchor,
    update_finish_anchor,
)
from .custom_motions import is_custom_document_token


_MIN_OUTBOUND_ID_RETENTION_SECONDS = 86_400.0
# recording.finished outranks message.received deliberately.  Acknowledging a
# recording is a sub-50ms dispatch whose whole purpose is to happen BEFORE the
# reply, while message.received makes a multi-second model call.  Sharing one
# lane let the reply win the queue and the "early" ack land after the answer.
_ACK_EVENT_PRIORITY = 110
_USER_EVENT_PRIORITY = 100
_REACTION_EVENT_PRIORITY = 50
_MOTION_EVENT_PRIORITY = 40
_BACKGROUND_GENERATION_PRIORITY = -100
_ACCEL_DRAMA_ORDER = ("dropped", "shaken", "upside_down", "lift", "beaten")
# Rows the single event worker must never claim: they are generation jobs
# drained by their own background task, and running one on the worker would
# stall every other event behind a model call.
_BACKGROUND_JOB_TYPES = (
    "reaction_bank.refresh",
    "motion_invention.request",
    "event_memory.extract",
)
_BACKGROUND_JOB_PLACEHOLDERS = ", ".join("?" for _ in _BACKGROUND_JOB_TYPES)


def _event_priority(event_type: str) -> int:
    if event_type == "recording.finished":
        return _ACK_EVENT_PRIORITY
    if event_type == "message.received":
        return _USER_EVENT_PRIORITY
    if event_type in {"radar.detected", "accel.detected"}:
        return _REACTION_EVENT_PRIORITY
    if event_type in {
        "motion.due",
        "motion.finished",
        "emo_talk.finished",
        "recording.started",
    }:
        return _MOTION_EVENT_PRIORITY
    if event_type.startswith("schedule.") or event_type in _BACKGROUND_JOB_TYPES:
        return _BACKGROUND_GENERATION_PRIORITY
    return 0


class InboundEventLike(Protocol):
    request_id: str
    event_type: str
    room_uuid: str | None
    sender_uuid: str | None
    speech_text: str | None
    received_at: datetime
    message_id: str | None
    message_media: str | None
    event_detail: str | None


@dataclass(frozen=True, slots=True)
class QueuedEvent:
    request_id: str
    event_type: str
    room_uuid: str | None
    sender_uuid: str | None
    speech_text: str | None
    message_id: str | None
    message_media: str | None
    event_detail: str | None
    priority: int
    received_at: datetime
    status: str
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class EventEffects:
    response_text: str | None = None
    bocco_sent: bool = False
    motion_cues: tuple[tuple[str, int], ...] = ()
    ack_motion_attempted: bool = False


@dataclass(frozen=True, slots=True)
class RecordingCorrelation:
    stt_latency_seconds: float
    newly_created: bool


@dataclass(frozen=True, slots=True)
class MotionChain:
    source_request_id: str
    room_uuid: str
    motion_uuids: tuple[str, ...]
    motion_kinds: tuple[str | None, ...]
    due_times: tuple[float, ...]
    anchor_offsets: tuple[float, ...]
    anchored_at: float | None
    sent_count: int
    finished_count: int
    talk_finished: bool
    status: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class MotionDispatch:
    source_request_id: str
    room_uuid: str
    motion_uuid: str


@dataclass(frozen=True, slots=True)
class Schedule:
    id: int
    source_request_id: str
    room_uuid: str
    local_time: str
    weekday_mask: int
    kind: str
    prompt_text: str
    enabled: bool
    last_fired_date: str | None


@dataclass(frozen=True, slots=True)
class AccelBatch:
    batch_id: str
    room_uuid: str
    kinds: tuple[str, ...]
    opened_at: float
    fire_at: float
    status: str


class EventDatabase:
    """Small async facade over short, independently connected SQLite calls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._outbound_audio_pending: dict[str, asyncio.Event] = {}
        self._outbound_stamp_pending: dict[str, asyncio.Event] = {}

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_events (
                    request_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    room_uuid TEXT,
                    sender_uuid TEXT,
                    speech_text TEXT,
                    message_id TEXT,
                    message_media TEXT,
                    event_detail TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'completed', 'dead_letter')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS inbound_events_ready
                ON inbound_events(status, available_at, received_at);

                CREATE TABLE IF NOT EXISTS event_effects (
                    request_id TEXT PRIMARY KEY
                        REFERENCES inbound_events(request_id) ON DELETE CASCADE,
                    response_text TEXT,
                    bocco_sent INTEGER NOT NULL DEFAULT 0,
                    motion_cues_json TEXT NOT NULL DEFAULT '[]',
                    ack_motion_attempted INTEGER NOT NULL DEFAULT 0,
                    discord_sent INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS recording_message_correlations (
                    recording_request_id TEXT PRIMARY KEY,
                    message_request_id TEXT NOT NULL UNIQUE,
                    stt_latency_seconds REAL NOT NULL
                        CHECK (stt_latency_seconds >= 0),
                    correlated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cooldowns (
                    behavior_key TEXT PRIMARY KEY,
                    next_allowed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_request_id TEXT NOT NULL UNIQUE,
                    room_uuid TEXT NOT NULL,
                    local_time TEXT NOT NULL,
                    weekday_mask INTEGER NOT NULL DEFAULT 127
                        CHECK (weekday_mask BETWEEN 1 AND 127),
                    kind TEXT NOT NULL CHECK (kind IN ('briefing', 'custom')),
                    prompt_text TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_fired_date TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS schedules_due
                ON schedules(enabled, local_time, last_fired_date);

                CREATE TABLE IF NOT EXISTS outbound_messages (
                    source_request_id TEXT PRIMARY KEY
                        REFERENCES inbound_events(request_id) ON DELETE CASCADE,
                    message_id TEXT,
                    room_uuid TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    sent_at REAL NOT NULL,
                    consumed_at REAL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS outbound_messages_message_id
                ON outbound_messages(message_id)
                WHERE message_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS outbound_messages_fallback
                ON outbound_messages(room_uuid, text_sha256, sent_at)
                WHERE message_id IS NULL AND consumed_at IS NULL;

                CREATE TABLE IF NOT EXISTS outbound_stream_messages (
                    source_request_id TEXT NOT NULL
                        REFERENCES inbound_events(request_id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    message_id TEXT,
                    room_uuid TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    sent_at REAL NOT NULL,
                    consumed_at REAL,
                    PRIMARY KEY (source_request_id, chunk_index)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS outbound_stream_messages_message_id
                ON outbound_stream_messages(message_id)
                WHERE message_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS outbound_stream_messages_fallback
                ON outbound_stream_messages(room_uuid, text_sha256, sent_at)
                WHERE message_id IS NULL AND consumed_at IS NULL;

                CREATE TABLE IF NOT EXISTS outbound_media_ids (
                    message_id TEXT PRIMARY KEY,
                    source_request_id TEXT NOT NULL
                        REFERENCES inbound_events(request_id) ON DELETE CASCADE,
                    room_uuid TEXT NOT NULL,
                    media TEXT NOT NULL,
                    sent_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS outbound_media_ids_room
                ON outbound_media_ids(room_uuid, message_id, sent_at);

                CREATE TABLE IF NOT EXISTS motion_chains (
                    source_request_id TEXT PRIMARY KEY
                        REFERENCES inbound_events(request_id) ON DELETE CASCADE,
                    room_uuid TEXT NOT NULL,
                    motion_uuids_json TEXT NOT NULL,
                    motion_kinds_json TEXT NOT NULL DEFAULT '[]',
                    due_times_json TEXT NOT NULL,
                    anchor_offsets_json TEXT NOT NULL DEFAULT '[]',
                    anchored_at REAL,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    finished_count INTEGER NOT NULL DEFAULT 0,
                    talk_finished INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'completed', 'abandoned')),
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS motion_chains_pending
                ON motion_chains(room_uuid, status, created_at);

                CREATE TABLE IF NOT EXISTS motion_call_budget (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sent_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS motion_call_budget_window
                ON motion_call_budget(sent_at);

                CREATE TABLE IF NOT EXISTS speech_observations (
                    source_request_id TEXT PRIMARY KEY
                        REFERENCES inbound_events(request_id) ON DELETE CASCADE,
                    room_uuid TEXT NOT NULL,
                    spoken_text_sha256 TEXT NOT NULL,
                    text_sent_at REAL NOT NULL,
                    text_length INTEGER NOT NULL,
                    delivery_anchor_at REAL,
                    finished_at REAL
                );

                CREATE INDEX IF NOT EXISTS speech_observations_pending
                ON speech_observations(room_uuid, finished_at, text_sent_at);

                CREATE TABLE IF NOT EXISTS speech_calibration (
                    room_uuid TEXT PRIMARY KEY,
                    delivery_lag_seconds REAL NOT NULL,
                    seconds_per_char REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reaction_bank (
                    persona_hash TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    phrases_json TEXT NOT NULL,
                    generated_at REAL NOT NULL,
                    PRIMARY KEY (persona_hash, event_key)
                );

                CREATE TABLE IF NOT EXISTS accel_reaction_state (
                    room_uuid TEXT PRIMARY KEY,
                    last_kind TEXT NOT NULL,
                    last_reacted_at REAL NOT NULL
                );


                CREATE TABLE IF NOT EXISTS accel_batches (
                    batch_id TEXT PRIMARY KEY,
                    room_uuid TEXT NOT NULL,
                    kinds_json TEXT NOT NULL,
                    opened_at REAL NOT NULL,
                    fire_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'collecting'
                        CHECK (status IN ('collecting', 'sealed', 'completed')),
                    completed_at REAL
                );

                CREATE INDEX IF NOT EXISTS accel_batches_collecting
                ON accel_batches(room_uuid, status, fire_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(inbound_events)")
            }
            if "message_id" not in columns:
                connection.execute("ALTER TABLE inbound_events ADD COLUMN message_id TEXT")
            if "message_media" not in columns:
                connection.execute(
                    "ALTER TABLE inbound_events ADD COLUMN message_media TEXT"
                )
            if "event_detail" not in columns:
                connection.execute(
                    "ALTER TABLE inbound_events ADD COLUMN event_detail TEXT"
                )
            if "priority" not in columns:
                connection.execute(
                    "ALTER TABLE inbound_events ADD COLUMN priority "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                UPDATE inbound_events SET priority = CASE
                    WHEN event_type = 'recording.finished' THEN ?
                    WHEN event_type = 'message.received' THEN ?
                    WHEN event_type IN ('radar.detected', 'accel.detected') THEN ?
                    WHEN event_type IN (
                        'motion.due', 'motion.finished', 'emo_talk.finished',
                        'recording.started'
                    ) THEN ?
                    WHEN event_type LIKE 'schedule.%'
                        OR event_type IN ('reaction_bank.refresh',
                            'motion_invention.request',
                            'event_memory.extract') THEN ?
                    ELSE 0
                END
                """,
                (
                    _ACK_EVENT_PRIORITY,
                    _USER_EVENT_PRIORITY,
                    _REACTION_EVENT_PRIORITY,
                    _MOTION_EVENT_PRIORITY,
                    _BACKGROUND_GENERATION_PRIORITY,
                ),
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS inbound_events_priority_ready "
                "ON inbound_events(status, priority DESC, received_at, available_at)"
            )
            effect_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(event_effects)")
            }
            if "motion_cues_json" not in effect_columns:
                connection.execute(
                    "ALTER TABLE event_effects ADD COLUMN motion_cues_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "ack_motion_attempted" not in effect_columns:
                connection.execute(
                    "ALTER TABLE event_effects ADD COLUMN ack_motion_attempted "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            motion_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(motion_chains)")
            }
            if "anchor_offsets_json" not in motion_columns:
                connection.execute(
                    "ALTER TABLE motion_chains ADD COLUMN anchor_offsets_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            if "anchored_at" not in motion_columns:
                connection.execute(
                    "ALTER TABLE motion_chains ADD COLUMN anchored_at REAL"
                )
            if "motion_kinds_json" not in motion_columns:
                connection.execute(
                    "ALTER TABLE motion_chains ADD COLUMN motion_kinds_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            observation_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(speech_observations)")
            }
            if "delivery_anchor_at" not in observation_columns:
                connection.execute(
                    "ALTER TABLE speech_observations ADD COLUMN delivery_anchor_at REAL"
                )
        os.chmod(self.path, 0o600)

    async def enqueue(self, event: InboundEventLike) -> bool:
        return await asyncio.to_thread(self._enqueue_sync, event)

    def _enqueue_sync(self, event: InboundEventLike) -> bool:
        received_at = event.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events (
                    request_id, event_type, room_uuid, sender_uuid, speech_text,
                    message_id, message_media, event_detail, priority, received_at,
                    status, attempts, available_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                """,
                (
                    event.request_id,
                    event.event_type,
                    event.room_uuid,
                    event.sender_uuid,
                    event.speech_text,
                    event.message_id,
                    event.message_media,
                    event.event_detail,
                    (
                        _event_priority(event.event_type)
                    ),
                    received_at.astimezone(timezone.utc).isoformat(),
                    time.time(),
                ),
            )
            return cursor.rowcount == 1

    async def recover_interrupted(self) -> int:
        return await asyncio.to_thread(self._recover_interrupted_sync)

    def _recover_interrupted_sync(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE inbound_events
                SET status = 'pending', available_at = ?, started_at = NULL,
                    last_error = COALESCE(last_error, 'interrupted')
                WHERE status = 'processing'
                    AND event_type NOT IN ({_BACKGROUND_JOB_PLACEHOLDERS})
                """,
                (time.time(), *_BACKGROUND_JOB_TYPES),
            )
            return cursor.rowcount

    async def claim_next(self) -> QueuedEvent | None:
        return await asyncio.to_thread(self._claim_next_sync)

    def _claim_next_sync(self) -> QueuedEvent | None:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Acknowledgments are drained ahead of the user lane.  They are
            # sub-50ms dispatches that must land before the reply, and unlike
            # user messages they carry no ordering contract with each other,
            # so an unavailable ack falls through instead of blocking.
            row = connection.execute(
                """
                SELECT * FROM inbound_events
                WHERE status = 'pending' AND priority = ? AND available_at <= ?
                ORDER BY received_at, rowid
                LIMIT 1
                """,
                (_ACK_EVENT_PRIORITY, now),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM inbound_events
                    WHERE status = 'pending' AND priority = ?
                    ORDER BY received_at, rowid
                    LIMIT 1
                    """,
                    (_USER_EVENT_PRIORITY,),
                ).fetchone()
                # The user lane keeps strict arrival order: if its head is not
                # yet available, stall rather than reordering past it.
                if row is not None and float(row["available_at"]) > now:
                    connection.commit()
                    return None
            if row is None:
                row = connection.execute(
                    f"""
                    SELECT * FROM inbound_events
                    WHERE status = 'pending' AND available_at <= ?
                        AND event_type NOT IN ({_BACKGROUND_JOB_PLACEHOLDERS})
                    ORDER BY priority DESC, available_at, received_at, rowid
                    LIMIT 1
                    """,
                    (now, *_BACKGROUND_JOB_TYPES),
                ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE inbound_events
                SET status = 'processing', attempts = attempts + 1, started_at = ?, last_error = NULL
                WHERE request_id = ? AND status = 'pending'
                """,
                (now, row["request_id"]),
            )
            connection.commit()
            values = dict(row)
            values["status"] = "processing"
            values["attempts"] = int(values["attempts"]) + 1
            return self._row_to_event(values)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row_to_event(row: sqlite3.Row | dict[str, Any]) -> QueuedEvent:
        return QueuedEvent(
            request_id=row["request_id"],
            event_type=row["event_type"],
            room_uuid=row["room_uuid"],
            sender_uuid=row["sender_uuid"],
            speech_text=row["speech_text"],
            message_id=row["message_id"],
            message_media=row["message_media"],
            event_detail=row["event_detail"],
            priority=int(row["priority"]),
            received_at=datetime.fromisoformat(row["received_at"]),
            status=row["status"],
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
        )

    async def complete(self, request_id: str) -> None:
        await asyncio.to_thread(self._complete_sync, request_id)

    def _complete_sync(self, request_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE inbound_events
                SET status = 'completed', completed_at = ?, started_at = NULL, last_error = NULL
                WHERE request_id = ? AND status = 'processing'
                """,
                (time.time(), request_id),
            )

    async def retry(self, request_id: str, error: str, delay_seconds: float) -> None:
        await asyncio.to_thread(self._retry_sync, request_id, error, delay_seconds)

    def _retry_sync(self, request_id: str, error: str, delay_seconds: float) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE inbound_events
                SET status = 'pending', available_at = ?, started_at = NULL, last_error = ?
                WHERE request_id = ? AND status = 'processing'
                """,
                (time.time() + max(0.0, delay_seconds), error[:240], request_id),
            )

    async def dead_letter(self, request_id: str, error: str) -> None:
        await asyncio.to_thread(self._dead_letter_sync, request_id, error)

    def _dead_letter_sync(self, request_id: str, error: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE inbound_events
                SET status = 'dead_letter', completed_at = ?, started_at = NULL, last_error = ?
                WHERE request_id = ? AND status = 'processing'
                """,
                (time.time(), error[:240], request_id),
            )

    async def get_event(self, request_id: str) -> QueuedEvent | None:
        return await asyncio.to_thread(self._get_event_sync, request_id)

    def _get_event_sync(self, request_id: str) -> QueuedEvent | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_events WHERE request_id = ?", (request_id,)
            ).fetchone()
            return None if row is None else self._row_to_event(row)

    async def correlate_recent_recording_finished(
        self,
        message_request_id: str,
        room_uuid: str,
        message_received_at: datetime,
        window_seconds: float,
        *,
        now: float | None = None,
    ) -> RecordingCorrelation | None:
        return await asyncio.to_thread(
            self._correlate_recent_recording_finished_sync,
            message_request_id,
            room_uuid,
            message_received_at,
            window_seconds,
            now,
        )

    def _correlate_recent_recording_finished_sync(
        self,
        message_request_id: str,
        room_uuid: str,
        message_received_at: datetime,
        window_seconds: float,
        now: float | None,
    ) -> RecordingCorrelation | None:
        if message_received_at.tzinfo is None:
            message_received_at = message_received_at.replace(tzinfo=timezone.utc)
        message_time = message_received_at.astimezone(timezone.utc)
        cutoff = datetime.fromtimestamp(
            message_time.timestamp() - max(0.0, window_seconds), timezone.utc
        )
        current = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT stt_latency_seconds FROM recording_message_correlations "
                "WHERE message_request_id = ?",
                (message_request_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return RecordingCorrelation(
                    stt_latency_seconds=float(existing["stt_latency_seconds"]),
                    newly_created=False,
                )
            row = connection.execute(
                """
                SELECT request_id, received_at
                FROM inbound_events AS recording
                WHERE event_type = 'recording.finished'
                    AND room_uuid = ?
                    AND status = 'completed'
                    AND received_at >= ? AND received_at <= ?
                    AND NOT EXISTS (
                        SELECT 1 FROM recording_message_correlations AS link
                        WHERE link.recording_request_id = recording.request_id
                    )
                ORDER BY received_at DESC, rowid DESC
                LIMIT 1
                """,
                (room_uuid, cutoff.isoformat(), message_time.isoformat()),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            recording_time = datetime.fromisoformat(str(row["received_at"]))
            latency = message_time.timestamp() - recording_time.timestamp()
            connection.execute(
                """
                INSERT INTO recording_message_correlations (
                    recording_request_id, message_request_id,
                    stt_latency_seconds, correlated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (str(row["request_id"]), message_request_id, latency, current),
            )
            connection.commit()
            return RecordingCorrelation(latency, True)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def get_effects(self, request_id: str) -> EventEffects:
        return await asyncio.to_thread(self._get_effects_sync, request_id)

    def _get_effects_sync(self, request_id: str) -> EventEffects:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM event_effects WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                return EventEffects()
            return EventEffects(
                response_text=row["response_text"],
                bocco_sent=bool(row["bocco_sent"]),
                motion_cues=self._decode_motion_cues(row["motion_cues_json"]),
                ack_motion_attempted=bool(row["ack_motion_attempted"]),
            )

    async def recording_reply_sent(self, recording_request_id: str) -> bool:
        """Return whether the message correlated to a recording has replied."""

        return await asyncio.to_thread(
            self._recording_reply_sent_sync, recording_request_id
        )

    def _recording_reply_sent_sync(self, recording_request_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT effects.bocco_sent
                FROM recording_message_correlations AS correlation
                JOIN event_effects AS effects
                    ON effects.request_id = correlation.message_request_id
                WHERE correlation.recording_request_id = ?
                """,
                (recording_request_id,),
            ).fetchone()
            return row is not None and bool(row["bocco_sent"])

    async def room_reply_sent_since_recording(
        self, recording_request_id: str, lookback_seconds: float
    ) -> bool:
        """Return whether this recording's room has already been answered.

        ``lookback_seconds`` widens the window *backwards* from the moment the
        recording event was received. It is required rather than defaulted:
        zero is the behaviour this method had when it was wrong, so a new call
        site must say what window it means instead of inheriting the bug.

        The audio ``message.received`` carrying an utterance's transcript
        routinely arrives BEFORE that utterance's own ``recording.finished``:
        measured 29 of 40 matched pairs, by 1-6 seconds, none later. So a reply
        can be spoken before the recording event that it answers even exists in
        this table, and a window that only looks forward cannot see it.

        Live, 2026-08-07: a fast route answered in 277 ms at 11:15:27.3, the
        recording event landed at 11:15:31.8, and the considering gesture was
        dispatched at 11:15:34.9 — 7.6 s after the robot had finished
        answering. Fast routes make this the common case rather than the rare
        one, because they reply before the second webhook has any chance to
        arrive.
        """

        return await asyncio.to_thread(
            self._room_reply_sent_since_recording_sync,
            recording_request_id,
            lookback_seconds,
        )

    def _room_reply_sent_since_recording_sync(
        self, recording_request_id: str, lookback_seconds: float
    ) -> bool:
        with self._connection() as connection:
            recording = connection.execute(
                """
                SELECT room_uuid, received_at
                FROM inbound_events
                WHERE request_id = ? AND event_type = 'recording.finished'
                """,
                (recording_request_id,),
            ).fetchone()
            if recording is None or not recording["room_uuid"]:
                return False
            received_at = datetime.fromisoformat(str(recording["received_at"]))
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
            recording_timestamp = received_at.timestamp() - max(
                0.0, lookback_seconds
            )
            row = connection.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM outbound_messages
                        WHERE room_uuid = ? AND sent_at >= ?
                    ) OR EXISTS (
                        SELECT 1 FROM outbound_stream_messages
                        WHERE room_uuid = ? AND sent_at >= ?
                    ) AS reply_sent
                """,
                (
                    recording["room_uuid"],
                    recording_timestamp,
                    recording["room_uuid"],
                    recording_timestamp,
                ),
            ).fetchone()
            return row is not None and bool(row["reply_sent"])

    async def reserve_ack_motion(
        self,
        request_id: str,
        room_uuid: str,
        motion_uuid: str,
        budget_per_minute: int,
        *,
        now: float | None = None,
    ) -> MotionDispatch | None:
        """Reserve one durable, at-most-once acknowledgment within the budget."""

        return await asyncio.to_thread(
            self._reserve_ack_motion_sync,
            request_id,
            room_uuid,
            motion_uuid,
            budget_per_minute,
            now,
        )

    def _reserve_ack_motion_sync(
        self,
        request_id: str,
        room_uuid: str,
        motion_uuid: str,
        budget_per_minute: int,
        now: float | None,
    ) -> MotionDispatch | None:
        current = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT ack_motion_attempted FROM event_effects WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None and bool(row["ack_motion_attempted"]):
                connection.commit()
                return None
            connection.execute(
                """
                INSERT INTO event_effects(request_id, ack_motion_attempted)
                VALUES (?, 1)
                ON CONFLICT(request_id) DO UPDATE SET ack_motion_attempted = 1
                """,
                (request_id,),
            )
            if not self._reserve_motion_budget_slot(
                connection, current, budget_per_minute
            ):
                connection.commit()
                return None
            connection.commit()
            return MotionDispatch(
                source_request_id=request_id,
                room_uuid=room_uuid,
                motion_uuid=motion_uuid,
            )
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def reserve_thinking_motion(
        self,
        request_id: str,
        room_uuid: str,
        motion_uuid: str,
        budget_per_minute: int,
        *,
        now: float | None = None,
    ) -> MotionDispatch | None:
        """Reserve one best-effort thinking motion in the shared call budget."""

        return await asyncio.to_thread(
            self._reserve_thinking_motion_sync,
            request_id,
            room_uuid,
            motion_uuid,
            budget_per_minute,
            now,
        )

    async def reserve_motion_call(
        self,
        request_id: str,
        room_uuid: str,
        motion_uuid: str,
        budget_per_minute: int,
        *,
        now: float | None = None,
    ) -> MotionDispatch | None:
        """Reserve one standalone motion call in the shared per-minute budget.

        Chain dispatches reserve their slot inside the chain transaction; a
        standalone performance has no chain, so it books the same budget here
        rather than spending motion calls the budget cannot see.
        """

        return await asyncio.to_thread(
            self._reserve_thinking_motion_sync,
            request_id,
            room_uuid,
            motion_uuid,
            budget_per_minute,
            now,
        )

    def _reserve_thinking_motion_sync(
        self,
        request_id: str,
        room_uuid: str,
        motion_uuid: str,
        budget_per_minute: int,
        now: float | None,
    ) -> MotionDispatch | None:
        current = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not self._reserve_motion_budget_slot(
                connection, current, budget_per_minute
            ):
                connection.commit()
                return None
            connection.commit()
            return MotionDispatch(request_id, room_uuid, motion_uuid)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _reserve_motion_budget_slot(
        connection: sqlite3.Connection,
        current: float,
        budget_per_minute: int,
    ) -> bool:
        connection.execute(
            "DELETE FROM motion_call_budget WHERE sent_at <= ?", (current - 60.0,)
        )
        count_row = connection.execute(
            "SELECT COUNT(*) AS count FROM motion_call_budget"
        ).fetchone()
        assert count_row is not None
        if int(count_row["count"]) >= budget_per_minute:
            return False
        connection.execute(
            "INSERT INTO motion_call_budget(sent_at) VALUES (?)", (current,)
        )
        return True

    async def save_response_if_absent(
        self,
        request_id: str,
        text: str,
        motion_cues: tuple[tuple[str, int], ...] = (),
    ) -> str:
        return await asyncio.to_thread(
            self._save_response_if_absent_sync, request_id, text, motion_cues
        )

    def _save_response_if_absent_sync(
        self,
        request_id: str,
        text: str,
        motion_cues: tuple[tuple[str, int], ...],
    ) -> str:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO event_effects(request_id, response_text, motion_cues_json)
                VALUES (?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE
                SET motion_cues_json = CASE
                        WHEN event_effects.response_text IS NULL
                        THEN excluded.motion_cues_json
                        ELSE event_effects.motion_cues_json
                    END,
                    response_text = COALESCE(event_effects.response_text, excluded.response_text)
                """,
                (
                    request_id,
                    text,
                    json.dumps(motion_cues, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            row = connection.execute(
                "SELECT response_text FROM event_effects WHERE request_id = ?", (request_id,)
            ).fetchone()
            assert row is not None and row["response_text"] is not None
            return str(row["response_text"])

    async def mark_bocco_sent(self, request_id: str) -> None:
        await asyncio.to_thread(self._mark_effect_sync, request_id, "bocco_sent")

    async def record_bocco_delivery(
        self,
        request_id: str,
        room_uuid: str,
        text: str,
        message_id: str | None,
        correlation_window_seconds: float,
        *,
        sent_at: float | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._record_bocco_delivery_sync,
            request_id,
            room_uuid,
            text,
            message_id,
            correlation_window_seconds,
            sent_at,
        )

    def _record_bocco_delivery_sync(
        self,
        request_id: str,
        room_uuid: str,
        text: str,
        message_id: str | None,
        correlation_window_seconds: float,
        sent_at: float | None,
    ) -> None:
        current = time.time() if sent_at is None else sent_at
        normalized_id = message_id.strip() if isinstance(message_id, str) else None
        if not normalized_id:
            normalized_id = None
        text_sha256 = _text_sha256(text)
        retention_seconds = max(
            correlation_window_seconds, _MIN_OUTBOUND_ID_RETENTION_SECONDS
        )
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM outbound_messages WHERE sent_at < ?",
                (current - max(0.0, retention_seconds),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbound_messages (
                    source_request_id, message_id, room_uuid, text_sha256, sent_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, normalized_id, room_uuid, text_sha256, current),
            )
            connection.execute(
                """
                INSERT INTO event_effects(request_id, bocco_sent)
                VALUES (?, 1)
                ON CONFLICT(request_id) DO UPDATE SET bocco_sent = 1
                """,
                (request_id,),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO speech_observations (
                    source_request_id, room_uuid, spoken_text_sha256,
                    text_sent_at, text_length
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, room_uuid, text_sha256, current, len(text)),
            )

    async def record_stream_chunk_delivery(
        self,
        request_id: str,
        chunk_index: int,
        room_uuid: str,
        text: str,
        message_id: str | None,
        correlation_window_seconds: float,
        *,
        sent_at: float | None = None,
    ) -> None:
        """Record one streamed sentence chunk for echo suppression.

        Streamed replies send several messages for a single inbound event, so
        chunks are keyed by ``(source_request_id, chunk_index)`` in their own
        table; ``event_effects.bocco_sent`` is set on the first chunk so a
        retried event never replays the reply.
        """

        await asyncio.to_thread(
            self._record_stream_chunk_delivery_sync,
            request_id,
            chunk_index,
            room_uuid,
            text,
            message_id,
            correlation_window_seconds,
            sent_at,
        )

    def _record_stream_chunk_delivery_sync(
        self,
        request_id: str,
        chunk_index: int,
        room_uuid: str,
        text: str,
        message_id: str | None,
        correlation_window_seconds: float,
        sent_at: float | None,
    ) -> None:
        current = time.time() if sent_at is None else sent_at
        normalized_id = message_id.strip() if isinstance(message_id, str) else None
        if not normalized_id:
            normalized_id = None
        retention_seconds = max(
            correlation_window_seconds, _MIN_OUTBOUND_ID_RETENTION_SECONDS
        )
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM outbound_stream_messages WHERE sent_at < ?",
                (current - max(0.0, retention_seconds),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbound_stream_messages (
                    source_request_id, chunk_index, message_id, room_uuid,
                    text_sha256, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    chunk_index,
                    normalized_id,
                    room_uuid,
                    _text_sha256(text),
                    current,
                ),
            )
            connection.execute(
                """
                INSERT INTO event_effects(request_id, bocco_sent)
                VALUES (?, 1)
                ON CONFLICT(request_id) DO UPDATE SET bocco_sent = 1
                """,
                (request_id,),
            )

    async def record_motion_delivery(
        self,
        request_id: str,
        room_uuid: str,
        message_id: str | None,
        correlation_window_seconds: float,
        *,
        sent_at: float | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._record_media_delivery_sync,
            request_id,
            room_uuid,
            message_id,
            "motion",
            correlation_window_seconds,
            sent_at,
        )

    async def record_audio_delivery(
        self,
        request_id: str,
        room_uuid: str,
        message_id: str | None,
        correlation_window_seconds: float,
        *,
        sent_at: float | None = None,
    ) -> None:
        """Record an uploaded cue id so its message webhook is self-echo."""

        await asyncio.to_thread(
            self._record_media_delivery_sync,
            request_id,
            room_uuid,
            message_id,
            "audio",
            correlation_window_seconds,
            sent_at,
        )

    async def record_stamp_delivery(
        self,
        request_id: str,
        room_uuid: str,
        message_id: str | None,
        correlation_window_seconds: float,
        *,
        sent_at: float | None = None,
    ) -> None:
        """Record a native stamp id so its message webhook is self-echo."""

        await asyncio.to_thread(
            self._record_media_delivery_sync,
            request_id,
            room_uuid,
            message_id,
            "stamp",
            correlation_window_seconds,
            sent_at,
        )

    @asynccontextmanager
    async def outbound_audio_delivery(
        self, room_uuid: str
    ) -> AsyncIterator[None]:
        """Defer only same-room audio webhooks until their outbound id is durable."""

        while (
            existing := self._outbound_audio_pending.get(room_uuid)
        ) is not None:
            await existing.wait()
        pending = asyncio.Event()
        self._outbound_audio_pending[room_uuid] = pending
        try:
            yield
        finally:
            if self._outbound_audio_pending.get(room_uuid) is pending:
                self._outbound_audio_pending.pop(room_uuid, None)
            pending.set()

    async def wait_for_outbound_audio(self, room_uuid: str) -> None:
        """Wait for a same-room outbound audio id before processing its webhook."""

        while (
            pending := self._outbound_audio_pending.get(room_uuid)
        ) is not None:
            await pending.wait()

    @asynccontextmanager
    async def outbound_stamp_delivery(
        self, room_uuid: str
    ) -> AsyncIterator[None]:
        """Defer same-room stamp webhooks until their outbound id is durable."""

        while (
            existing := self._outbound_stamp_pending.get(room_uuid)
        ) is not None:
            await existing.wait()
        pending = asyncio.Event()
        self._outbound_stamp_pending[room_uuid] = pending
        try:
            yield
        finally:
            if self._outbound_stamp_pending.get(room_uuid) is pending:
                self._outbound_stamp_pending.pop(room_uuid, None)
            pending.set()

    async def wait_for_outbound_stamp(self, room_uuid: str) -> None:
        """Wait for a same-room outbound stamp id before processing its webhook."""

        while (
            pending := self._outbound_stamp_pending.get(room_uuid)
        ) is not None:
            await pending.wait()

    def _record_media_delivery_sync(
        self,
        request_id: str,
        room_uuid: str,
        message_id: str | None,
        media: str,
        correlation_window_seconds: float,
        sent_at: float | None,
    ) -> None:
        if media not in {"audio", "motion", "stamp"}:
            raise ValueError("unsupported outbound media type")
        current = time.time() if sent_at is None else sent_at
        normalized_id = message_id.strip() if isinstance(message_id, str) else None
        if not normalized_id:
            normalized_id = None
        retention_seconds = max(
            correlation_window_seconds, _MIN_OUTBOUND_ID_RETENTION_SECONDS
        )
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM outbound_media_ids WHERE sent_at < ?",
                (current - max(0.0, retention_seconds),),
            )
            if normalized_id is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO outbound_media_ids (
                        message_id, source_request_id, room_uuid, media, sent_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (normalized_id, request_id, room_uuid, media, current),
                )

    async def ensure_motion_chain(
        self,
        request_id: str,
        room_uuid: str,
        motion_schedule: tuple[tuple[str, float], ...],
        timeout_seconds: float,
        *,
        motion_kinds: tuple[str | None, ...] = (),
        anchor_offsets: tuple[float, ...] = (),
        now: float | None = None,
    ) -> None:
        if not 1 <= len(motion_schedule) <= 3:
            raise ValueError("motion chains must contain one to three motions")
        if not all(
            isinstance(uuid, str)
            and uuid
            and isinstance(due_at, (int, float))
            and not isinstance(due_at, bool)
            for uuid, due_at in motion_schedule
        ):
            raise ValueError("motion schedule entries are invalid")
        if anchor_offsets and (
            len(anchor_offsets) != len(motion_schedule)
            or any(
                isinstance(offset, bool)
                or not isinstance(offset, (int, float))
                or offset < 0
                for offset in anchor_offsets
            )
        ):
            raise ValueError("motion anchor offsets are invalid")
        if motion_kinds and (
            len(motion_kinds) != len(motion_schedule)
            or any(
                kind is not None
                and (not isinstance(kind, str) or not kind.strip())
                for kind in motion_kinds
            )
        ):
            raise ValueError("motion kinds are invalid")
        normalized_kinds = (
            tuple(
                kind.strip() if kind is not None else None
                for kind in motion_kinds
            )
            if motion_kinds
            else (None,) * len(motion_schedule)
        )
        await asyncio.to_thread(
            self._ensure_motion_chain_sync,
            request_id,
            room_uuid,
            motion_schedule,
            timeout_seconds,
            normalized_kinds,
            anchor_offsets,
            now,
        )

    def _ensure_motion_chain_sync(
        self,
        request_id: str,
        room_uuid: str,
        motion_schedule: tuple[tuple[str, float], ...],
        timeout_seconds: float,
        motion_kinds: tuple[str | None, ...],
        anchor_offsets: tuple[float, ...],
        now: float | None,
    ) -> None:
        current = time.time() if now is None else now
        motion_uuids = tuple(item[0] for item in motion_schedule)
        due_times = tuple(float(item[1]) for item in motion_schedule)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO motion_chains (
                    source_request_id, room_uuid, motion_uuids_json,
                    motion_kinds_json, due_times_json, anchor_offsets_json,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    room_uuid,
                    json.dumps(
                        motion_uuids,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        motion_kinds,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(due_times, separators=(",", ":")),
                    json.dumps(anchor_offsets, separators=(",", ":")),
                    current + timeout_seconds,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                return
            for index, due_at in enumerate(due_times):
                due_request_id = f"internal:motion:{request_id}:{index}"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO inbound_events (
                        request_id, event_type, room_uuid, sender_uuid,
                        speech_text, message_id, message_media, event_detail,
                        received_at, status, attempts, available_at
                    ) VALUES (?, 'motion.due', ?, NULL, NULL, NULL, NULL, ?,
                        ?, 'pending', 0, ?)
                    """,
                    (
                        due_request_id,
                        room_uuid,
                        json.dumps(
                            {"source_request_id": request_id, "index": index},
                            separators=(",", ":"),
                        ),
                        datetime.fromtimestamp(due_at, timezone.utc).isoformat(),
                        due_at,
                    ),
                )

    async def dispatch_due_motion(
        self,
        request_id: str,
        cue_index: int,
        timeout_seconds: float,
        budget_per_minute: int,
        *,
        now: float | None = None,
    ) -> MotionDispatch | None:
        return await asyncio.to_thread(
            self._dispatch_due_motion_sync,
            request_id,
            cue_index,
            timeout_seconds,
            budget_per_minute,
            now,
        )

    def _dispatch_due_motion_sync(
        self,
        request_id: str,
        cue_index: int,
        timeout_seconds: float,
        budget_per_minute: int,
        now: float | None,
    ) -> MotionDispatch | None:
        current = time.time() if now is None else now
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM motion_chains
                WHERE source_request_id = ? AND status = 'active'
                    AND expires_at > ?
                """,
                (request_id, current),
            ).fetchone()
            if row is None:
                return None
            sent_count = int(row["sent_count"])
            if cue_index != sent_count or sent_count != int(row["finished_count"]):
                return None
            motions = self._decode_motion_uuids(row["motion_uuids_json"])
            due_times = self._decode_due_times(row["due_times_json"])
            if cue_index >= len(due_times) or due_times[cue_index] > current:
                return None
            return self._reserve_and_claim_motion(
                connection,
                row,
                motions,
                current,
                timeout_seconds,
                budget_per_minute,
            )

    async def complete_custom_motion(
        self,
        request_id: str,
        timeout_seconds: float,
        budget_per_minute: int,
        *,
        now: float | None = None,
    ) -> MotionDispatch | None:
        """Finish a successfully delivered custom motion and claim its successor.

        Custom documents emit no ``motion.finished`` webhook.  Completing them
        explicitly after the API call preserves the normal one-in-flight chain
        invariant and lets an already-due successor advance immediately.
        """

        return await asyncio.to_thread(
            self._complete_custom_motion_sync,
            request_id,
            timeout_seconds,
            budget_per_minute,
            now,
        )

    def _complete_custom_motion_sync(
        self,
        request_id: str,
        timeout_seconds: float,
        budget_per_minute: int,
        now: float | None,
    ) -> MotionDispatch | None:
        current = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM motion_chains
                WHERE source_request_id = ? AND status = 'active'
                    AND expires_at > ?
                """,
                (request_id, current),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            motions = self._decode_motion_uuids(row["motion_uuids_json"])
            due_times = self._decode_due_times(row["due_times_json"])
            sent_count = int(row["sent_count"])
            finished_count = int(row["finished_count"])
            if sent_count != finished_count + 1:
                connection.commit()
                return None
            if not is_custom_document_token(motions[finished_count]):
                connection.commit()
                return None

            finished_count += 1
            if finished_count >= len(motions):
                connection.execute(
                    """
                    UPDATE motion_chains
                    SET finished_count = ?, status = 'completed', expires_at = ?
                    WHERE source_request_id = ? AND status = 'active'
                    """,
                    (finished_count, current + timeout_seconds, request_id),
                )
                connection.commit()
                return None

            connection.execute(
                """
                UPDATE motion_chains SET finished_count = ?, expires_at = ?
                WHERE source_request_id = ? AND status = 'active'
                """,
                (finished_count, current + timeout_seconds, request_id),
            )
            next_is_due = due_times[sent_count] <= current
            if bool(row["talk_finished"]) or next_is_due:
                values = dict(row)
                values["finished_count"] = finished_count
                values["expires_at"] = current + timeout_seconds
                dispatch = self._reserve_and_claim_motion(
                    connection,
                    values,
                    motions,
                    current,
                    timeout_seconds,
                    budget_per_minute,
                )
                connection.commit()
                return dispatch
            connection.commit()
            return None
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def record_message_anchor(
        self,
        room_uuid: str,
        anchored_at: float,
        cold_start: SpeechCalibration,
        timeout_seconds: float,
    ) -> SpeechCalibration | None:
        """Record newMessageMotion and rebase any pending cue schedule."""

        return await asyncio.to_thread(
            self._record_message_anchor_sync,
            room_uuid,
            anchored_at,
            cold_start,
            timeout_seconds,
        )

    def _record_message_anchor_sync(
        self,
        room_uuid: str,
        anchored_at: float,
        cold_start: SpeechCalibration,
        timeout_seconds: float,
    ) -> SpeechCalibration | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            observation = connection.execute(
                """
                SELECT * FROM speech_observations
                WHERE room_uuid = ? AND delivery_anchor_at IS NULL
                    AND text_sent_at <= ? AND text_sent_at >= ?
                ORDER BY text_sent_at DESC LIMIT 1
                """,
                (room_uuid, anchored_at, anchored_at - min(timeout_seconds, 10.0)),
            ).fetchone()
            if observation is None:
                connection.commit()
                return None
            current_row = connection.execute(
                "SELECT * FROM speech_calibration WHERE room_uuid = ?",
                (room_uuid,),
            ).fetchone()
            current = (
                cold_start
                if current_row is None
                else SpeechCalibration(
                    delivery_lag_seconds=float(current_row["delivery_lag_seconds"]),
                    seconds_per_char=float(current_row["seconds_per_char"]),
                    sample_count=int(current_row["sample_count"]),
                )
            )
            updated = update_delivery_anchor(
                current,
                send_time=float(observation["text_sent_at"]),
                anchor_time=anchored_at,
            )
            connection.execute(
                """
                UPDATE speech_observations SET delivery_anchor_at = ?
                WHERE source_request_id = ?
                """,
                (anchored_at, observation["source_request_id"]),
            )
            self._upsert_speech_calibration(
                connection, room_uuid, updated, anchored_at
            )

            chain = connection.execute(
                """
                SELECT * FROM motion_chains
                WHERE source_request_id = ? AND status = 'active'
                    AND anchored_at IS NULL
                """,
                (observation["source_request_id"],),
            ).fetchone()
            if chain is not None:
                offsets = self._decode_due_times(chain["anchor_offsets_json"])
                motions = self._decode_motion_uuids(chain["motion_uuids_json"])
                if len(offsets) == len(motions):
                    due_times = tuple(anchored_at + offset for offset in offsets)
                    connection.execute(
                        """
                        UPDATE motion_chains
                        SET due_times_json = ?, anchored_at = ?, expires_at = ?
                        WHERE source_request_id = ?
                        """,
                        (
                            json.dumps(due_times, separators=(",", ":")),
                            anchored_at,
                            anchored_at + timeout_seconds,
                            observation["source_request_id"],
                        ),
                    )
                    for index, due_at in enumerate(due_times):
                        connection.execute(
                            """
                            UPDATE inbound_events
                            SET available_at = ?, received_at = ?
                            WHERE request_id = ? AND status = 'pending'
                            """,
                            (
                                due_at,
                                datetime.fromtimestamp(
                                    due_at, timezone.utc
                                ).isoformat(),
                                f"internal:motion:{observation['source_request_id']}:{index}",
                            ),
                        )
                else:
                    connection.execute(
                        "UPDATE motion_chains SET anchored_at = ? "
                        "WHERE source_request_id = ?",
                        (anchored_at, observation["source_request_id"]),
                    )
            connection.commit()
            return updated
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def record_talk_finished(
        self,
        room_uuid: str,
        spoken_text: str | None,
        finished_at: float,
        cold_start: SpeechCalibration,
    ) -> SpeechCalibration | None:
        return await asyncio.to_thread(
            self._record_talk_finished_sync,
            room_uuid,
            spoken_text,
            finished_at,
            cold_start,
        )

    def _record_talk_finished_sync(
        self,
        room_uuid: str,
        spoken_text: str | None,
        finished_at: float,
        cold_start: SpeechCalibration,
    ) -> SpeechCalibration | None:
        with self._connection() as connection:
            row = None
            if spoken_text:
                row = connection.execute(
                    """
                    SELECT * FROM speech_observations
                    WHERE room_uuid = ? AND finished_at IS NULL
                        AND spoken_text_sha256 = ? AND text_sent_at <= ?
                    ORDER BY text_sent_at DESC LIMIT 1
                    """,
                    (room_uuid, _text_sha256(spoken_text), finished_at),
                ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM speech_observations
                    WHERE room_uuid = ? AND finished_at IS NULL
                        AND text_sent_at <= ?
                    ORDER BY text_sent_at DESC LIMIT 1
                    """,
                    (room_uuid, finished_at),
                ).fetchone()
            if row is None:
                return None
            current_row = connection.execute(
                "SELECT * FROM speech_calibration WHERE room_uuid = ?",
                (room_uuid,),
            ).fetchone()
            current = (
                cold_start
                if current_row is None
                else SpeechCalibration(
                    delivery_lag_seconds=float(current_row["delivery_lag_seconds"]),
                    seconds_per_char=float(current_row["seconds_per_char"]),
                    sample_count=int(current_row["sample_count"]),
                )
            )
            updated = update_calibration(
                current,
                send_time=float(row["text_sent_at"]),
                finished_time=finished_at,
                text_length=int(row["text_length"]),
            )
            if row["delivery_anchor_at"] is not None:
                updated = update_finish_anchor(
                    current,
                    speech_anchor_time=float(row["delivery_anchor_at"]),
                    finished_time=finished_at,
                    text_length=int(row["text_length"]),
                )
            connection.execute(
                """
                UPDATE speech_observations SET finished_at = ?
                WHERE source_request_id = ?
                """,
                (finished_at, row["source_request_id"]),
            )
            self._upsert_speech_calibration(
                connection, room_uuid, updated, finished_at
            )
            return updated

    @staticmethod
    def _upsert_speech_calibration(
        connection: sqlite3.Connection,
        room_uuid: str,
        calibration: SpeechCalibration,
        updated_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO speech_calibration (
                room_uuid, delivery_lag_seconds, seconds_per_char,
                sample_count, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(room_uuid) DO UPDATE SET
                delivery_lag_seconds = excluded.delivery_lag_seconds,
                seconds_per_char = excluded.seconds_per_char,
                sample_count = excluded.sample_count,
                updated_at = excluded.updated_at
            """,
            (
                room_uuid,
                calibration.delivery_lag_seconds,
                calibration.seconds_per_char,
                calibration.sample_count,
                updated_at,
            ),
        )

    async def get_speech_calibration(
        self, room_uuid: str, cold_start: SpeechCalibration
    ) -> SpeechCalibration:
        return await asyncio.to_thread(
            self._get_speech_calibration_sync, room_uuid, cold_start
        )

    def _get_speech_calibration_sync(
        self, room_uuid: str, cold_start: SpeechCalibration
    ) -> SpeechCalibration:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM speech_calibration WHERE room_uuid = ?",
                (room_uuid,),
            ).fetchone()
            if row is None:
                return cold_start
            return SpeechCalibration(
                delivery_lag_seconds=float(row["delivery_lag_seconds"]),
                seconds_per_char=float(row["seconds_per_char"]),
                sample_count=int(row["sample_count"]),
            )

    async def advance_motion_chain(
        self,
        event_type: str,
        room_uuid: str,
        event_detail: str | None,
        timeout_seconds: float,
        budget_per_minute: int,
        *,
        now: float | None = None,
    ) -> MotionDispatch | None:
        if event_type not in {"emo_talk.finished", "motion.finished"}:
            raise ValueError("unsupported motion-chain signal")
        return await asyncio.to_thread(
            self._advance_motion_chain_sync,
            event_type,
            room_uuid,
            event_detail,
            timeout_seconds,
            budget_per_minute,
            now,
        )

    def _advance_motion_chain_sync(
        self,
        event_type: str,
        room_uuid: str,
        event_detail: str | None,
        timeout_seconds: float,
        budget_per_minute: int,
        now: float | None,
    ) -> MotionDispatch | None:
        current = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE motion_chains SET status = 'abandoned'
                WHERE status = 'active' AND expires_at <= ?
                """,
                (current,),
            )
            row = None
            if event_type == "emo_talk.finished":
                if event_detail:
                    row = connection.execute(
                        """
                        SELECT mc.* FROM motion_chains AS mc
                        JOIN speech_observations AS so
                            ON so.source_request_id = mc.source_request_id
                        WHERE mc.room_uuid = ? AND mc.status = 'active'
                            AND mc.talk_finished = 0
                            AND so.spoken_text_sha256 = ?
                        ORDER BY mc.created_at DESC LIMIT 1
                        """,
                        (room_uuid, _text_sha256(event_detail)),
                    ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT * FROM motion_chains
                        WHERE room_uuid = ? AND status = 'active'
                            AND talk_finished = 0
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (room_uuid,),
                    ).fetchone()
                if row is not None:
                    connection.execute(
                        "UPDATE motion_chains SET talk_finished = 1 WHERE source_request_id = ?",
                        (row["source_request_id"],),
                    )
                    values = dict(row)
                    values["talk_finished"] = 1
                    row = values
            else:
                event_kind = (
                    event_detail.strip().casefold()
                    if isinstance(event_detail, str) and event_detail.strip()
                    else None
                )
                candidates = (
                    connection.execute(
                        """
                        SELECT * FROM motion_chains
                        WHERE room_uuid = ? AND status = 'active'
                            AND sent_count > finished_count
                        ORDER BY created_at DESC
                        """,
                        (room_uuid,),
                    ).fetchall()
                    if event_kind is not None
                    else ()
                )
                # A custom document is acknowledged by
                # complete_custom_motion() after its API call succeeds.  An
                # unrelated preset webhook must not finish it in the interim.
                for candidate in candidates:
                    motions = self._decode_motion_uuids(
                        candidate["motion_uuids_json"]
                    )
                    finished_count = int(candidate["finished_count"])
                    if finished_count >= len(motions):
                        continue
                    if is_custom_document_token(motions[finished_count]):
                        continue
                    motion_kinds = self._decode_motion_kinds(
                        candidate["motion_kinds_json"]
                    )
                    if finished_count >= len(motion_kinds):
                        continue
                    expected_kind = motion_kinds[finished_count]
                    if (
                        expected_kind is None
                        or expected_kind.casefold() != event_kind
                    ):
                        continue
                    row = candidate
                    break
                if row is not None:
                    finished_count = int(row["finished_count"]) + 1
                    connection.execute(
                        """
                        UPDATE motion_chains SET finished_count = ?
                        WHERE source_request_id = ?
                        """,
                        (finished_count, row["source_request_id"]),
                    )
                    values = dict(row)
                    values["finished_count"] = finished_count
                    row = values
            if row is None:
                connection.commit()
                return None

            motions = self._decode_motion_uuids(row["motion_uuids_json"])
            due_times = self._decode_due_times(row["due_times_json"])
            sent_count = int(row["sent_count"])
            finished_count = int(row["finished_count"])
            if event_type == "motion.finished" and finished_count >= len(motions):
                connection.execute(
                    """
                    UPDATE motion_chains SET status = 'completed'
                    WHERE source_request_id = ?
                    """,
                    (row["source_request_id"],),
                )
                connection.commit()
                return None
            next_is_due = sent_count < len(due_times) and due_times[sent_count] <= current
            if finished_count == sent_count and (
                bool(row["talk_finished"]) or next_is_due
            ):
                if sent_count >= len(motions):
                    connection.execute(
                        """
                        UPDATE motion_chains SET status = 'completed'
                        WHERE source_request_id = ?
                        """,
                        (row["source_request_id"],),
                    )
                    connection.commit()
                    return None
                dispatch = self._reserve_and_claim_motion(
                    connection,
                    row,
                    motions,
                    current,
                    timeout_seconds,
                    budget_per_minute,
                )
                connection.commit()
                return dispatch
            connection.commit()
            return None
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _reserve_and_claim_motion(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        motions: tuple[str, ...],
        current: float,
        timeout_seconds: float,
        budget_per_minute: int,
    ) -> MotionDispatch | None:
        connection.execute(
            "DELETE FROM motion_call_budget WHERE sent_at <= ?", (current - 60.0,)
        )
        count_row = connection.execute(
            "SELECT COUNT(*) AS count FROM motion_call_budget"
        ).fetchone()
        assert count_row is not None
        if int(count_row["count"]) >= budget_per_minute:
            connection.execute(
                """
                UPDATE motion_chains SET status = 'abandoned'
                WHERE source_request_id = ?
                """,
                (row["source_request_id"],),
            )
            return None
        sent_count = int(row["sent_count"])
        if sent_count >= len(motions):
            return None
        connection.execute(
            "INSERT INTO motion_call_budget(sent_at) VALUES (?)", (current,)
        )
        claimed = motions[sent_count]
        connection.execute(
            """
            UPDATE motion_chains
            SET sent_count = sent_count + 1, expires_at = ?
            WHERE source_request_id = ? AND status = 'active'
            """,
            (current + timeout_seconds, row["source_request_id"]),
        )
        return MotionDispatch(
            source_request_id=str(row["source_request_id"]),
            room_uuid=str(row["room_uuid"]),
            motion_uuid=claimed,
        )

    async def abandon_expired_motion_chains(
        self, *, now: float | None = None
    ) -> int:
        return await asyncio.to_thread(self._abandon_motion_chains_sync, False, now)

    async def abandon_motion_chain(self, request_id: str) -> bool:
        """Abandon one best-effort chain after a terminal delivery failure."""

        return await asyncio.to_thread(self._abandon_motion_chain_sync, request_id)

    def _abandon_motion_chain_sync(self, request_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE motion_chains SET status = 'abandoned'
                WHERE source_request_id = ? AND status = 'active'
                """,
                (request_id,),
            )
            return cursor.rowcount == 1

    async def abandon_motion_chains_for_restart(
        self, *, now: float | None = None
    ) -> int:
        return await asyncio.to_thread(self._abandon_motion_chains_sync, True, now)

    def _abandon_motion_chains_sync(
        self, all_active: bool, now: float | None
    ) -> int:
        current = time.time() if now is None else now
        with self._connection() as connection:
            if all_active:
                cursor = connection.execute(
                    "UPDATE motion_chains SET status = 'abandoned' WHERE status = 'active'"
                )
                connection.execute(
                    """
                    UPDATE inbound_events
                    SET status = 'completed', completed_at = ?
                    WHERE event_type = 'motion.due'
                        AND status IN ('pending', 'processing')
                    """,
                    (current,),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE motion_chains SET status = 'abandoned'
                    WHERE status = 'active' AND expires_at <= ?
                    """,
                    (current,),
                )
            return cursor.rowcount

    async def get_motion_chain(self, request_id: str) -> MotionChain | None:
        return await asyncio.to_thread(self._get_motion_chain_sync, request_id)

    def _get_motion_chain_sync(self, request_id: str) -> MotionChain | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM motion_chains WHERE source_request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            return MotionChain(
                source_request_id=str(row["source_request_id"]),
                room_uuid=str(row["room_uuid"]),
                motion_uuids=self._decode_motion_uuids(row["motion_uuids_json"]),
                motion_kinds=self._decode_motion_kinds(row["motion_kinds_json"]),
                due_times=self._decode_due_times(row["due_times_json"]),
                anchor_offsets=self._decode_due_times(row["anchor_offsets_json"]),
                anchored_at=(
                    float(row["anchored_at"])
                    if row["anchored_at"] is not None
                    else None
                ),
                sent_count=int(row["sent_count"]),
                finished_count=int(row["finished_count"]),
                talk_finished=bool(row["talk_finished"]),
                status=str(row["status"]),
                expires_at=float(row["expires_at"]),
            )

    @staticmethod
    def _decode_motion_uuids(value: object) -> tuple[str, ...]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored motion chain is invalid") from exc
        if not isinstance(decoded, list) or not all(
            isinstance(item, str) and item for item in decoded
        ):
            raise RuntimeError("stored motion chain is invalid")
        return tuple(decoded)

    @staticmethod
    def _decode_motion_kinds(value: object) -> tuple[str | None, ...]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored motion chain identities are invalid") from exc
        if not isinstance(decoded, list) or not all(
            item is None or (isinstance(item, str) and item.strip())
            for item in decoded
        ):
            raise RuntimeError("stored motion chain identities are invalid")
        return tuple(
            item.strip() if isinstance(item, str) else None for item in decoded
        )

    @staticmethod
    def _decode_motion_cues(value: object) -> tuple[tuple[str, int], ...]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored motion cues are invalid") from exc
        if not isinstance(decoded, list):
            raise RuntimeError("stored motion cues are invalid")
        cues: list[tuple[str, int]] = []
        for item in decoded:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
                or item[1] < 0
            ):
                raise RuntimeError("stored motion cues are invalid")
            cues.append((item[0], item[1]))
        return tuple(cues)

    @staticmethod
    def _decode_due_times(value: object) -> tuple[float, ...]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored motion due times are invalid") from exc
        if not isinstance(decoded, list) or not all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in decoded
        ):
            raise RuntimeError("stored motion due times are invalid")
        return tuple(float(item) for item in decoded)

    async def consume_outbound_echo(
        self,
        room_uuid: str,
        message_id: str | None,
        text: str | None,
        correlation_window_seconds: float,
        *,
        now: float | None = None,
    ) -> str | None:
        return await asyncio.to_thread(
            self._consume_outbound_echo_sync,
            room_uuid,
            message_id,
            text,
            correlation_window_seconds,
            now,
        )

    def _consume_outbound_echo_sync(
        self,
        room_uuid: str,
        message_id: str | None,
        text: str | None,
        correlation_window_seconds: float,
        now: float | None,
    ) -> str | None:
        current = time.time() if now is None else now
        content_cutoff = current - max(0.0, correlation_window_seconds)
        retention_seconds = max(
            correlation_window_seconds, _MIN_OUTBOUND_ID_RETENTION_SECONDS
        )
        retention_cutoff = current - max(0.0, retention_seconds)
        normalized_id = message_id.strip() if isinstance(message_id, str) else None
        if not normalized_id:
            normalized_id = None
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM outbound_messages WHERE sent_at < ?", (retention_cutoff,)
            )
            connection.execute(
                "DELETE FROM outbound_media_ids WHERE sent_at < ?", (retention_cutoff,)
            )
            connection.execute(
                "DELETE FROM outbound_stream_messages WHERE sent_at < ?",
                (retention_cutoff,),
            )
            if normalized_id is not None:
                for table in (
                    "outbound_messages",
                    "outbound_media_ids",
                    "outbound_stream_messages",
                ):
                    row = connection.execute(
                        f"""
                        SELECT source_request_id FROM {table}
                        WHERE room_uuid = ? AND message_id = ? AND sent_at <= ?
                        LIMIT 1
                        """,
                        (room_uuid, normalized_id, current),
                    ).fetchone()
                    if row is not None:
                        return "message_id"
            if text is not None:
                row = connection.execute(
                    """
                    SELECT source_request_id FROM outbound_messages
                    WHERE room_uuid = ? AND message_id IS NULL
                        AND text_sha256 = ? AND consumed_at IS NULL
                        AND sent_at >= ? AND sent_at <= ?
                    ORDER BY sent_at, source_request_id
                    LIMIT 1
                    """,
                    (room_uuid, _text_sha256(text), content_cutoff, current),
                ).fetchone()
                if row is not None:
                    cursor = connection.execute(
                        """
                        UPDATE outbound_messages SET consumed_at = ?
                        WHERE source_request_id = ? AND consumed_at IS NULL
                        """,
                        (current, row["source_request_id"]),
                    )
                    return "content_hash" if cursor.rowcount == 1 else None
                stream_row = connection.execute(
                    """
                    SELECT rowid FROM outbound_stream_messages
                    WHERE room_uuid = ? AND message_id IS NULL
                        AND text_sha256 = ? AND consumed_at IS NULL
                        AND sent_at >= ? AND sent_at <= ?
                    ORDER BY sent_at, source_request_id, chunk_index
                    LIMIT 1
                    """,
                    (room_uuid, _text_sha256(text), content_cutoff, current),
                ).fetchone()
                if stream_row is not None:
                    cursor = connection.execute(
                        """
                        UPDATE outbound_stream_messages SET consumed_at = ?
                        WHERE rowid = ? AND consumed_at IS NULL
                        """,
                        (current, stream_row["rowid"]),
                    )
                    return "content_hash" if cursor.rowcount == 1 else None
            return None

    def _mark_effect_sync(self, request_id: str, column: str) -> None:
        if column != "bocco_sent":
            raise ValueError("unknown event effect")
        with self._connection() as connection:
            connection.execute(
                f"""
                INSERT INTO event_effects(request_id, {column})
                VALUES (?, 1)
                ON CONFLICT(request_id) DO UPDATE SET {column} = 1
                """,
                (request_id,),
            )

    async def get_setting(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_setting_sync, key)

    def _get_setting_sync(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM runtime_settings WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None else str(row["value"])

    async def set_setting(
        self, key: str, value: str, *, updated_at: float | None = None
    ) -> None:
        await asyncio.to_thread(self._set_setting_sync, key, value, updated_at)

    def _set_setting_sync(
        self, key: str, value: str, updated_at: float | None
    ) -> None:
        current = time.time() if updated_at is None else updated_at
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runtime_settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, current),
            )

    async def clear_setting(self, key: str) -> None:
        await asyncio.to_thread(self._clear_setting_sync, key)

    def _clear_setting_sync(self, key: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM runtime_settings WHERE key = ?", (key,))

    async def get_reaction_phrases(
        self, persona_hash: str, event_key: str
    ) -> tuple[str, ...] | None:
        return await asyncio.to_thread(
            self._get_reaction_phrases_sync, persona_hash, event_key
        )

    def _get_reaction_phrases_sync(
        self, persona_hash: str, event_key: str
    ) -> tuple[str, ...] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT phrases_json FROM reaction_bank "
                "WHERE persona_hash = ? AND event_key = ?",
                (persona_hash, event_key),
            ).fetchone()
            if row is None:
                return None
            return self._decode_string_tuple(row["phrases_json"])

    async def put_reaction_phrases(
        self,
        persona_hash: str,
        event_key: str,
        phrases: tuple[str, ...],
        *,
        generated_at: float | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._put_reaction_phrases_sync,
            persona_hash,
            event_key,
            phrases,
            generated_at,
        )

    def _put_reaction_phrases_sync(
        self,
        persona_hash: str,
        event_key: str,
        phrases: tuple[str, ...],
        generated_at: float | None,
    ) -> None:
        current = time.time() if generated_at is None else generated_at
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO reaction_bank (
                    persona_hash, event_key, phrases_json, generated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(persona_hash, event_key) DO UPDATE SET
                    phrases_json = excluded.phrases_json,
                    generated_at = excluded.generated_at
                """,
                (
                    persona_hash,
                    event_key,
                    json.dumps(phrases, ensure_ascii=False, separators=(",", ":")),
                    current,
                ),
            )

    async def enqueue_reaction_bank_refresh(
        self,
        trigger_id: str,
        persona_hash: str,
        instructions: str,
        room_uuid: str | None,
        *,
        now: float | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._enqueue_reaction_bank_refresh_sync,
            trigger_id,
            persona_hash,
            instructions,
            room_uuid,
            now,
        )

    def _enqueue_reaction_bank_refresh_sync(
        self,
        trigger_id: str,
        persona_hash: str,
        instructions: str,
        room_uuid: str | None,
        now: float | None,
    ) -> bool:
        current = time.time() if now is None else now
        request_id = f"internal:reaction-bank:{trigger_id}"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_rows = connection.execute(
                """
                SELECT event_detail FROM inbound_events
                WHERE event_type = 'reaction_bank.refresh'
                    AND status IN ('pending', 'processing')
                """
            ).fetchall()
            for row in active_rows:
                try:
                    active_detail = json.loads(str(row["event_detail"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(active_detail, dict)
                    and active_detail.get("persona_hash") == persona_hash
                ):
                    return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events (
                    request_id, event_type, room_uuid, event_detail, priority,
                    received_at, status, attempts, available_at
                ) VALUES (?, 'reaction_bank.refresh', ?, ?, ?, ?,
                    'pending', 0, ?)
                """,
                (
                    request_id,
                    room_uuid,
                    json.dumps(
                        {
                            "persona_hash": persona_hash,
                            "instructions": instructions,
                            "completed_keys": [],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    _BACKGROUND_GENERATION_PRIORITY,
                    datetime.fromtimestamp(current, timezone.utc).isoformat(),
                    current,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                connection.execute(
                    """
                    UPDATE inbound_events
                    SET status = 'completed', completed_at = ?, started_at = NULL,
                        last_error = 'superseded'
                    WHERE event_type = 'reaction_bank.refresh'
                        AND request_id != ?
                        AND status IN ('pending', 'processing')
                    """,
                    (current, request_id),
                )
            return inserted

    async def recover_reaction_bank_refreshes(
        self, *, now: float | None = None
    ) -> int:
        return await asyncio.to_thread(
            self._recover_background_jobs_sync, "reaction_bank.refresh", now
        )

    def _recover_background_jobs_sync(
        self, event_type: str, now: float | None
    ) -> int:
        current = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_events
                SET status = 'pending', available_at = ?, started_at = NULL,
                    last_error = COALESCE(last_error, 'interrupted')
                WHERE status = 'processing' AND event_type = ?
                """,
                (current, event_type),
            )
            return cursor.rowcount

    async def claim_next_reaction_bank_refresh(
        self, *, now: float | None = None
    ) -> QueuedEvent | None:
        return await asyncio.to_thread(
            self._claim_next_background_job_sync, "reaction_bank.refresh", now
        )

    def _claim_next_background_job_sync(
        self, event_type: str, now: float | None
    ) -> QueuedEvent | None:
        current = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM inbound_events
                WHERE event_type = ?
                    AND status = 'pending' AND available_at <= ?
                ORDER BY available_at, received_at, rowid
                LIMIT 1
                """,
                (event_type, current),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                """
                UPDATE inbound_events
                SET status = 'processing', attempts = attempts + 1,
                    started_at = ?, last_error = NULL
                WHERE request_id = ? AND status = 'pending'
                """,
                (current, row["request_id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            values = dict(row)
            values["status"] = "processing"
            values["attempts"] = int(values["attempts"]) + 1
            return self._row_to_event(values)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def publish_reaction_bank_entry(
        self,
        request_id: str,
        persona_hash: str,
        event_key: str,
        phrases: tuple[str, ...],
        *,
        generated_at: float | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._publish_reaction_bank_entry_sync,
            request_id,
            persona_hash,
            event_key,
            phrases,
            generated_at,
        )

    def _publish_reaction_bank_entry_sync(
        self,
        request_id: str,
        persona_hash: str,
        event_key: str,
        phrases: tuple[str, ...],
        generated_at: float | None,
    ) -> bool:
        current = time.time() if generated_at is None else generated_at
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT event_detail FROM inbound_events
                WHERE request_id = ? AND event_type = 'reaction_bank.refresh'
                    AND status = 'processing'
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            detail = json.loads(str(row["event_detail"]))
            if not isinstance(detail, dict) or detail.get("persona_hash") != persona_hash:
                raise RuntimeError("reaction bank job detail changed")
            completed = detail.get("completed_keys", [])
            if not isinstance(completed, list) or not all(
                isinstance(item, str) for item in completed
            ):
                raise RuntimeError("reaction bank job progress is invalid")
            if event_key in completed:
                connection.commit()
                return True

            connection.execute(
                """
                INSERT INTO reaction_bank (
                    persona_hash, event_key, phrases_json, generated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(persona_hash, event_key) DO UPDATE SET
                    phrases_json = excluded.phrases_json,
                    generated_at = excluded.generated_at
                """,
                (
                    persona_hash,
                    event_key,
                    json.dumps(phrases, ensure_ascii=False, separators=(",", ":")),
                    current,
                ),
            )
            detail["completed_keys"] = [*completed, event_key]
            cursor = connection.execute(
                """
                UPDATE inbound_events SET event_detail = ?
                WHERE request_id = ? AND status = 'processing'
                """,
                (
                    json.dumps(
                        detail, ensure_ascii=False, separators=(",", ":")
                    ),
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def retry_reaction_bank_refresh(
        self,
        request_id: str,
        error: str,
        delay_seconds: float,
        *,
        now: float | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._retry_background_job_sync,
            "reaction_bank.refresh",
            request_id,
            error,
            delay_seconds,
            now,
        )

    def _retry_background_job_sync(
        self,
        event_type: str,
        request_id: str,
        error: str,
        delay_seconds: float,
        now: float | None,
    ) -> bool:
        current = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_events
                SET status = 'pending', available_at = ?, started_at = NULL,
                    last_error = ?
                WHERE request_id = ? AND event_type = ?
                    AND status = 'processing'
                """,
                (
                    current + max(0.0, delay_seconds),
                    error[:240],
                    request_id,
                    event_type,
                ),
            )
            return cursor.rowcount == 1

    async def complete_reaction_bank_refresh(
        self, request_id: str, *, now: float | None = None
    ) -> bool:
        return await asyncio.to_thread(
            self._complete_background_job_sync,
            "reaction_bank.refresh",
            request_id,
            now,
        )

    def _complete_background_job_sync(
        self, event_type: str, request_id: str, now: float | None
    ) -> bool:
        current = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_events
                SET status = 'completed', completed_at = ?, started_at = NULL,
                    last_error = NULL
                WHERE request_id = ? AND event_type = ?
                    AND status = 'processing'
                """,
                (current, request_id, event_type),
            )
            return cursor.rowcount == 1

    async def enqueue_motion_invention(
        self,
        trigger_id: str,
        room_uuid: str,
        theme: str,
        instructions: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Queue one durable invent-a-motion job for the background generator.

        Idempotent per triggering utterance, and invisible to the event worker:
        the job is drained by :class:`MotionInventionGenerator`, because a model
        call on the single event worker would stall every other event behind it.
        """

        return await asyncio.to_thread(
            self._enqueue_motion_invention_sync,
            trigger_id,
            room_uuid,
            theme,
            instructions,
            now,
        )

    def _enqueue_motion_invention_sync(
        self,
        trigger_id: str,
        room_uuid: str,
        theme: str,
        instructions: str,
        now: float | None,
    ) -> bool:
        current = time.time() if now is None else now
        request_id = f"internal:motion-invention:{trigger_id}"
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events (
                    request_id, event_type, room_uuid, event_detail, priority,
                    received_at, status, attempts, available_at
                ) VALUES (?, 'motion_invention.request', ?, ?, ?, ?,
                    'pending', 0, ?)
                """,
                (
                    request_id,
                    room_uuid,
                    json.dumps(
                        {"theme": theme, "instructions": instructions},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    _BACKGROUND_GENERATION_PRIORITY,
                    datetime.fromtimestamp(current, timezone.utc).isoformat(),
                    current,
                ),
            )
            return cursor.rowcount == 1

    async def recover_motion_inventions(self, *, now: float | None = None) -> int:
        return await asyncio.to_thread(
            self._recover_background_jobs_sync, "motion_invention.request", now
        )

    async def claim_next_motion_invention(
        self, *, now: float | None = None
    ) -> QueuedEvent | None:
        return await asyncio.to_thread(
            self._claim_next_background_job_sync, "motion_invention.request", now
        )

    async def retry_motion_invention(
        self,
        request_id: str,
        error: str,
        delay_seconds: float,
        *,
        now: float | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._retry_background_job_sync,
            "motion_invention.request",
            request_id,
            error,
            delay_seconds,
            now,
        )

    async def complete_motion_invention(
        self, request_id: str, *, now: float | None = None
    ) -> bool:
        return await asyncio.to_thread(
            self._complete_background_job_sync,
            "motion_invention.request",
            request_id,
            now,
        )

    async def enqueue_event_extraction(
        self,
        source_request_id: str,
        room_uuid: str,
        user_text: str,
        reply_text: str,
        said_at: float,
        *,
        delay_seconds: float = 0.0,
        now: float | None = None,
    ) -> bool:
        """Queue one durable "did anything happen in this exchange" job.

        Called from the worker *after* the reply is out, and it does nothing
        but one INSERT — the model call belongs to
        :class:`~bocco_bridge.event_extraction.EventExtractor`. Idempotent per
        exchange, so a replayed event enqueues one job.

        ``delay_seconds`` pushes ``available_at`` into the future rather than
        making the extractor sleep: during a fast back-and-forth the jobs
        accumulate in the queue and drain once the household stops talking,
        which is both cheaper and crash-safe.
        """

        return await asyncio.to_thread(
            self._enqueue_event_extraction_sync,
            source_request_id,
            room_uuid,
            user_text,
            reply_text,
            said_at,
            delay_seconds,
            now,
        )

    def _enqueue_event_extraction_sync(
        self,
        source_request_id: str,
        room_uuid: str,
        user_text: str,
        reply_text: str,
        said_at: float,
        delay_seconds: float,
        now: float | None,
    ) -> bool:
        current = time.time() if now is None else now
        request_id = f"internal:event-extract:{source_request_id}"
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events (
                    request_id, event_type, room_uuid, event_detail, priority,
                    received_at, status, attempts, available_at
                ) VALUES (?, 'event_memory.extract', ?, ?, ?, ?,
                    'pending', 0, ?)
                """,
                (
                    request_id,
                    room_uuid,
                    json.dumps(
                        {
                            "source_request_id": source_request_id,
                            "user_text": user_text,
                            "reply_text": reply_text,
                            "said_at": said_at,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    _BACKGROUND_GENERATION_PRIORITY,
                    datetime.fromtimestamp(current, timezone.utc).isoformat(),
                    current + max(0.0, delay_seconds),
                ),
            )
            return cursor.rowcount == 1

    async def recover_event_extractions(self, *, now: float | None = None) -> int:
        return await asyncio.to_thread(
            self._recover_background_jobs_sync, "event_memory.extract", now
        )

    async def claim_next_event_extraction(
        self, *, now: float | None = None
    ) -> QueuedEvent | None:
        return await asyncio.to_thread(
            self._claim_next_background_job_sync, "event_memory.extract", now
        )

    async def retry_event_extraction(
        self,
        request_id: str,
        error: str,
        delay_seconds: float,
        *,
        now: float | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._retry_background_job_sync,
            "event_memory.extract",
            request_id,
            error,
            delay_seconds,
            now,
        )

    async def complete_event_extraction(
        self, request_id: str, *, now: float | None = None
    ) -> bool:
        return await asyncio.to_thread(
            self._complete_background_job_sync,
            "event_memory.extract",
            request_id,
            now,
        )

    async def add_schedule(
        self,
        source_request_id: str,
        room_uuid: str,
        local_time: str,
        kind: str,
        prompt_text: str,
        *,
        weekday_mask: int = 127,
        created_at: float | None = None,
    ) -> Schedule:
        return await asyncio.to_thread(
            self._add_schedule_sync,
            source_request_id,
            room_uuid,
            local_time,
            kind,
            prompt_text,
            weekday_mask,
            created_at,
        )

    def _add_schedule_sync(
        self,
        source_request_id: str,
        room_uuid: str,
        local_time: str,
        kind: str,
        prompt_text: str,
        weekday_mask: int,
        created_at: float | None,
    ) -> Schedule:
        current = time.time() if created_at is None else created_at
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO schedules (
                    source_request_id, room_uuid, local_time, weekday_mask,
                    kind, prompt_text, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    source_request_id,
                    room_uuid,
                    local_time,
                    weekday_mask,
                    kind,
                    prompt_text,
                    current,
                ),
            )
            row = connection.execute(
                "SELECT * FROM schedules WHERE source_request_id = ?",
                (source_request_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_schedule(row)

    async def list_schedules(self, room_uuid: str) -> list[Schedule]:
        return await asyncio.to_thread(self._list_schedules_sync, room_uuid)

    def _list_schedules_sync(self, room_uuid: str) -> list[Schedule]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM schedules
                WHERE room_uuid = ? AND enabled = 1
                ORDER BY local_time, id
                """,
                (room_uuid,),
            ).fetchall()
            return [self._row_to_schedule(row) for row in rows]

    async def remove_schedules_at(self, room_uuid: str, local_time: str) -> int:
        return await asyncio.to_thread(
            self._remove_schedules_at_sync, room_uuid, local_time
        )

    def _remove_schedules_at_sync(self, room_uuid: str, local_time: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM schedules WHERE room_uuid = ? AND local_time = ?",
                (room_uuid, local_time),
            )
            return cursor.rowcount

    async def enqueue_due_schedules(self, local_now: datetime) -> int:
        return await asyncio.to_thread(self._enqueue_due_schedules_sync, local_now)

    def _enqueue_due_schedules_sync(self, local_now: datetime) -> int:
        if local_now.tzinfo is None or local_now.utcoffset() is None:
            raise ValueError("scheduler time must include the Pi-local timezone")
        local_time = local_now.strftime("%H:%M")
        local_date = local_now.date().isoformat()
        weekday_bit = 1 << local_now.weekday()
        available_at = time.time()
        received_at = local_now.astimezone(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM schedules
                WHERE enabled = 1 AND local_time = ?
                    AND (weekday_mask & ?) != 0
                    AND (last_fired_date IS NULL OR last_fired_date != ?)
                ORDER BY id
                """,
                (local_time, weekday_bit, local_date),
            ).fetchall()
            enqueued = 0
            for row in rows:
                request_id = f"internal:schedule:{row['id']}:{local_date}"
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO inbound_events (
                        request_id, event_type, room_uuid, sender_uuid,
                        speech_text, message_id, priority, received_at, status,
                        attempts, available_at
                    ) VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, 'pending', 0, ?)
                    """,
                    (
                        request_id,
                        f"schedule.{row['kind']}",
                        row["room_uuid"],
                        row["prompt_text"],
                        _BACKGROUND_GENERATION_PRIORITY,
                        received_at,
                        available_at,
                    ),
                )
                connection.execute(
                    "UPDATE schedules SET last_fired_date = ? WHERE id = ?",
                    (local_date, row["id"]),
                )
                enqueued += cursor.rowcount
            connection.commit()
            return enqueued
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row | dict[str, Any]) -> Schedule:
        return Schedule(
            id=int(row["id"]),
            source_request_id=str(row["source_request_id"]),
            room_uuid=str(row["room_uuid"]),
            local_time=str(row["local_time"]),
            weekday_mask=int(row["weekday_mask"]),
            kind=str(row["kind"]),
            prompt_text=str(row["prompt_text"]),
            enabled=bool(row["enabled"]),
            last_fired_date=row["last_fired_date"],
        )

    async def buffer_accel_event(
        self,
        source_request_id: str,
        room_uuid: str,
        kind: str,
        debounce_seconds: float,
        *,
        event_at: float | None = None,
        now: float | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._buffer_accel_event_sync,
            source_request_id,
            room_uuid,
            kind,
            debounce_seconds,
            event_at,
            now,
        )

    def _buffer_accel_event_sync(
        self,
        source_request_id: str,
        room_uuid: str,
        kind: str,
        debounce_seconds: float,
        event_at: float | None,
        now: float | None,
    ) -> str:
        current = time.time() if now is None else now
        event_time = current if event_at is None else event_at
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM accel_batches WHERE status = 'completed' AND completed_at < ?",
                (current - 86_400.0,),
            )
            row = connection.execute(
                """
                SELECT * FROM accel_batches
                WHERE room_uuid = ? AND status = 'collecting'
                    AND opened_at <= ? AND opened_at + ? >= ?
                ORDER BY opened_at, batch_id LIMIT 1
                """,
                (room_uuid, event_time, debounce_seconds, event_time),
            ).fetchone()
            if row is not None:
                kinds = list(self._decode_string_tuple(row["kinds_json"]))
                if kind not in kinds:
                    kinds.append(kind)
                    connection.execute(
                        "UPDATE accel_batches SET kinds_json = ? WHERE batch_id = ?",
                        (
                            json.dumps(kinds, ensure_ascii=False, separators=(",", ":")),
                            row["batch_id"],
                        ),
                    )
                connection.commit()
                return str(row["batch_id"])

            batch_id = source_request_id
            fire_at = current + debounce_seconds
            connection.execute(
                """
                INSERT INTO accel_batches (
                    batch_id, room_uuid, kinds_json, opened_at, fire_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    room_uuid,
                    json.dumps([kind], ensure_ascii=False, separators=(",", ":")),
                    event_time,
                    fire_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events (
                    request_id, event_type, room_uuid, sender_uuid,
                    speech_text, message_id, message_media, event_detail,
                    received_at, status, attempts, available_at
                ) VALUES (?, 'accel.compound', ?, NULL, NULL, NULL, NULL, ?,
                    ?, 'pending', 0, ?)
                """,
                (
                    f"internal:accel:{batch_id}",
                    room_uuid,
                    batch_id,
                    datetime.fromtimestamp(fire_at, timezone.utc).isoformat(),
                    fire_at,
                ),
            )
            connection.commit()
            return batch_id
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def seal_accel_batch(self, batch_id: str) -> AccelBatch | None:
        return await asyncio.to_thread(self._seal_accel_batch_sync, batch_id)

    def _seal_accel_batch_sync(self, batch_id: str) -> AccelBatch | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM accel_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None or row["status"] == "completed":
                connection.commit()
                return None
            if row["status"] == "collecting":
                connection.execute(
                    "UPDATE accel_batches SET status = 'sealed' WHERE batch_id = ?",
                    (batch_id,),
                )
            connection.commit()
            values = dict(row)
            values["status"] = "sealed"
            return self._row_to_accel_batch(values)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def complete_accel_batch(
        self, batch_id: str, *, now: float | None = None
    ) -> None:
        await asyncio.to_thread(self._complete_accel_batch_sync, batch_id, now)

    def _complete_accel_batch_sync(
        self, batch_id: str, now: float | None
    ) -> None:
        current = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE accel_batches SET status = 'completed', completed_at = ?
                WHERE batch_id = ?
                """,
                (current, batch_id),
            )

    @classmethod
    def _row_to_accel_batch(
        cls, row: sqlite3.Row | dict[str, Any]
    ) -> AccelBatch:
        return AccelBatch(
            batch_id=str(row["batch_id"]),
            room_uuid=str(row["room_uuid"]),
            kinds=cls._decode_string_tuple(row["kinds_json"]),
            opened_at=float(row["opened_at"]),
            fire_at=float(row["fire_at"]),
            status=str(row["status"]),
        )

    @staticmethod
    def _decode_string_tuple(value: object) -> tuple[str, ...]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored string sequence is invalid") from exc
        if not isinstance(decoded, list) or not all(
            isinstance(item, str) and item for item in decoded
        ):
            raise RuntimeError("stored string sequence is invalid")
        return tuple(decoded)

    async def coalesce_same_second_accel(
        self,
        request_id: str,
        room_uuid: str,
        kind: str,
        received_at: datetime,
        *,
        now: float | None = None,
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(
            self._coalesce_same_second_accel_sync,
            request_id,
            room_uuid,
            kind,
            received_at,
            now,
        )

    def _coalesce_same_second_accel_sync(
        self,
        request_id: str,
        room_uuid: str,
        kind: str,
        received_at: datetime,
        now: float | None,
    ) -> tuple[str, ...]:
        current = time.time() if now is None else now
        target_second = int(received_at.timestamp())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT request_id, event_detail, received_at
                FROM inbound_events
                WHERE event_type = 'accel.detected' AND room_uuid = ?
                    AND status = 'pending' AND request_id != ?
                ORDER BY received_at, rowid
                """,
                (room_uuid, request_id),
            ).fetchall()
            kinds = {kind.casefold()}
            consumed: list[str] = []
            for row in rows:
                peer_time = datetime.fromisoformat(str(row["received_at"]))
                if int(peer_time.timestamp()) != target_second:
                    continue
                peer_kind = row["event_detail"]
                if isinstance(peer_kind, str) and peer_kind:
                    kinds.add(peer_kind.casefold())
                    consumed.append(str(row["request_id"]))
            if consumed:
                placeholders = ",".join("?" for _ in consumed)
                connection.execute(
                    f"""
                    UPDATE inbound_events
                    SET status = 'completed', completed_at = ?,
                        started_at = NULL, last_error = NULL
                    WHERE request_id IN ({placeholders}) AND status = 'pending'
                    """,
                    (current, *consumed),
                )
            selected_kind = next(
                (candidate for candidate in _ACCEL_DRAMA_ORDER if candidate in kinds),
                kind.casefold(),
            )
            connection.execute(
                "UPDATE inbound_events SET event_detail = ? "
                "WHERE request_id = ? AND status = 'processing'",
                (selected_kind, request_id),
            )
            connection.commit()
            return tuple(sorted(kinds))
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def reserve_accel_reaction(
        self,
        room_uuid: str,
        kind: str,
        kind_cooldown_seconds: float,
        global_cooldown_seconds: float,
        *,
        dropped_override_seconds: float = 5.0,
        now: float | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._reserve_accel_reaction_sync,
            room_uuid,
            kind,
            kind_cooldown_seconds,
            global_cooldown_seconds,
            dropped_override_seconds,
            now,
        )

    def _reserve_accel_reaction_sync(
        self,
        room_uuid: str,
        kind: str,
        kind_cooldown_seconds: float,
        global_cooldown_seconds: float,
        dropped_override_seconds: float,
        now: float | None,
    ) -> bool:
        current = time.time() if now is None else now
        global_key = f"accel:{room_uuid}:reaction"
        kind_key = f"accel:{room_uuid}:kind:{kind}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            blocked = {
                str(row["behavior_key"])
                for row in connection.execute(
                    """
                    SELECT behavior_key FROM cooldowns
                    WHERE behavior_key IN (?, ?) AND next_allowed_at > ?
                    """,
                    (global_key, kind_key, current),
                )
            }
            allowed = not blocked
            state = connection.execute(
                "SELECT * FROM accel_reaction_state WHERE room_uuid = ?", (room_uuid,)
            ).fetchone()
            if not allowed and kind == "dropped" and kind_key not in blocked:
                state = connection.execute(
                    "SELECT * FROM accel_reaction_state WHERE room_uuid = ?",
                    (room_uuid,),
                ).fetchone()
                allowed = bool(
                    state is not None
                    and str(state["last_kind"]) != "dropped"
                    and 0 <= current - float(state["last_reacted_at"])
                    <= dropped_override_seconds
                )
            if not allowed:
                connection.commit()
                return False
            connection.executemany(
                """
                INSERT INTO cooldowns(behavior_key, next_allowed_at)
                VALUES (?, ?)
                ON CONFLICT(behavior_key) DO UPDATE
                SET next_allowed_at = excluded.next_allowed_at
                """,
                (
                    (global_key, current + max(0.0, global_cooldown_seconds)),
                    (kind_key, current + max(0.0, kind_cooldown_seconds)),
                ),
            )
            connection.execute(
                """
                INSERT INTO accel_reaction_state(room_uuid, last_kind, last_reacted_at)
                VALUES (?, ?, ?)
                ON CONFLICT(room_uuid) DO UPDATE SET
                    last_kind = excluded.last_kind,
                    last_reacted_at = excluded.last_reacted_at
                """,
                (room_uuid, kind, current),
            )
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def cooldown_ready(self, behavior_key: str, now: float | None = None) -> bool:
        return await asyncio.to_thread(self._cooldown_ready_sync, behavior_key, now)

    def _cooldown_ready_sync(self, behavior_key: str, now: float | None) -> bool:
        current = time.time() if now is None else now
        with self._connection() as connection:
            row = connection.execute(
                "SELECT next_allowed_at FROM cooldowns WHERE behavior_key = ?", (behavior_key,)
            ).fetchone()
            return row is None or float(row["next_allowed_at"]) <= current

    async def cooldowns_ready(
        self, behavior_keys: tuple[str, ...], *, now: float | None = None
    ) -> bool:
        return await asyncio.to_thread(
            self._cooldowns_ready_sync, behavior_keys, now
        )

    def _cooldowns_ready_sync(
        self, behavior_keys: tuple[str, ...], now: float | None
    ) -> bool:
        if not behavior_keys:
            return True
        current = time.time() if now is None else now
        placeholders = ",".join("?" for _ in behavior_keys)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS blocked FROM cooldowns
                WHERE behavior_key IN ({placeholders}) AND next_allowed_at > ?
                """,
                (*behavior_keys, current),
            ).fetchone()
            assert row is not None
            return int(row["blocked"]) == 0

    async def set_cooldown(
        self, behavior_key: str, duration_seconds: float, now: float | None = None
    ) -> None:
        await asyncio.to_thread(self._set_cooldown_sync, behavior_key, duration_seconds, now)

    def _set_cooldown_sync(
        self, behavior_key: str, duration_seconds: float, now: float | None
    ) -> None:
        current = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cooldowns(behavior_key, next_allowed_at)
                VALUES (?, ?)
                ON CONFLICT(behavior_key) DO UPDATE SET next_allowed_at = excluded.next_allowed_at
                """,
                (behavior_key, current + max(0.0, duration_seconds)),
            )

    async def set_cooldowns(
        self,
        cooldowns: tuple[tuple[str, float], ...],
        *,
        now: float | None = None,
    ) -> None:
        await asyncio.to_thread(self._set_cooldowns_sync, cooldowns, now)

    def _set_cooldowns_sync(
        self,
        cooldowns: tuple[tuple[str, float], ...],
        now: float | None,
    ) -> None:
        current = time.time() if now is None else now
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO cooldowns(behavior_key, next_allowed_at)
                VALUES (?, ?)
                ON CONFLICT(behavior_key) DO UPDATE
                SET next_allowed_at = excluded.next_allowed_at
                """,
                (
                    (key, current + max(0.0, duration))
                    for key, duration in cooldowns
                ),
            )

    async def queue_counts(self) -> dict[str, int]:
        return await asyncio.to_thread(self._queue_counts_sync)

    def _queue_counts_sync(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM inbound_events GROUP BY status"
            ).fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
