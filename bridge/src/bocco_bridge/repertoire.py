"""Bridge-owned, room-scoped repertoire of invented motions.

A separate concern gets a separate store, exactly as
:mod:`bocco_bridge.transcript` is separate from :mod:`bocco_bridge.memory`.
Household facts are dictated prose superseded by subject; conversation turns are
high-volume and pruned by retention; a repertoire entry is neither. It is a
named artefact the user asked to be created, recalled by name, and re-performed
— so it lives in its own database file and can be deleted without touching
either of the other two.

**What is stored is the SPEC, not the rendered document.** Re-rendering on
playback keeps the stored form tiny, and means every improvement to
:mod:`bocco_bridge.motion_spec` retroactively improves every motion ever
invented rather than only the ones invented afterwards.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
import os
import sqlite3
import time

from .memory import escape_like, normalize_japanese_text


SPEC_TEXT_MAX_CHARS = 500


@dataclass(frozen=True, slots=True)
class InventedMotion:
    """One remembered motion: its name and the spec it is rendered from."""

    id: int
    room_uuid: str
    name: str
    spec_text: str
    source_request_id: str
    created_at: float
    played_count: int


class MotionRepertoire:
    """A lazy-initialized async facade over a private repertoire database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
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
                CREATE TABLE IF NOT EXISTS invented_motions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_uuid TEXT NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    spec_text TEXT NOT NULL,
                    source_request_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    played_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS invented_motions_room_recent
                ON invented_motions(room_uuid, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS invented_motions_room_name
                ON invented_motions(room_uuid, normalized_name);
                """
            )
        os.chmod(self.path, 0o600)

    async def remember(
        self,
        room_uuid: str,
        name: str,
        spec_text: str,
        source_request_id: str,
        *,
        created_at: float | None = None,
        retention: int = 0,
    ) -> InventedMotion | None:
        """Store one invented motion; ``None`` when there is nothing to store.

        Idempotent per ``source_request_id``, so a replayed generation job
        stores one motion. A repeated name replaces the older entry: asking for
        "きらきら" twice should leave one motion called きらきら, not two.
        """

        await self.initialize()
        return await asyncio.to_thread(
            self._remember_sync,
            room_uuid,
            name,
            spec_text,
            source_request_id,
            created_at,
            retention,
        )

    def _remember_sync(
        self,
        room_uuid: str,
        name: str,
        spec_text: str,
        source_request_id: str,
        created_at: float | None,
        retention: int,
    ) -> InventedMotion | None:
        motion_name = " ".join(str(name).split())
        spec = str(spec_text).strip()[:SPEC_TEXT_MAX_CHARS]
        if not room_uuid or not source_request_id or not motion_name or not spec:
            return None
        normalized = normalize_japanese_text(motion_name)
        current = time.time() if created_at is None else created_at
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM invented_motions WHERE source_request_id = ?",
                (source_request_id,),
            ).fetchone()
            if existing is not None:
                return _row_to_motion(existing)
            if normalized:
                connection.execute(
                    """
                    DELETE FROM invented_motions
                    WHERE room_uuid = ? AND normalized_name = ?
                    """,
                    (room_uuid, normalized),
                )
            cursor = connection.execute(
                """
                INSERT INTO invented_motions (
                    room_uuid, name, normalized_name, spec_text,
                    source_request_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (room_uuid, motion_name, normalized, spec, source_request_id, current),
            )
            motion_id = int(cursor.lastrowid)
            if retention > 0:
                connection.execute(
                    """
                    DELETE FROM invented_motions
                    WHERE room_uuid = ? AND id NOT IN (
                        SELECT id FROM invented_motions WHERE room_uuid = ?
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (room_uuid, room_uuid, retention),
                )
            row = connection.execute(
                "SELECT * FROM invented_motions WHERE id = ?", (motion_id,)
            ).fetchone()
            return None if row is None else _row_to_motion(row)

    async def get(self, motion_id: int) -> InventedMotion | None:
        await self.initialize()
        return await asyncio.to_thread(self._get_sync, motion_id)

    def _get_sync(self, motion_id: int) -> InventedMotion | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM invented_motions WHERE id = ?", (motion_id,)
            ).fetchone()
            return None if row is None else _row_to_motion(row)

    async def find(self, room_uuid: str, query_text: str) -> InventedMotion | None:
        """Recall a motion by name: exact first, then containment either way."""

        await self.initialize()
        return await asyncio.to_thread(self._find_sync, room_uuid, query_text)

    def _find_sync(self, room_uuid: str, query_text: str) -> InventedMotion | None:
        normalized = normalize_japanese_text(query_text)
        if not room_uuid or not normalized:
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM invented_motions
                WHERE room_uuid = ? AND normalized_name = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (room_uuid, normalized),
            ).fetchone()
            if row is not None:
                return _row_to_motion(row)
            # Speech recognition rarely returns a name character for character,
            # so accept a name that contains the request or is contained by it.
            row = connection.execute(
                """
                SELECT * FROM invented_motions
                WHERE room_uuid = ? AND (
                    normalized_name LIKE ? ESCAPE '\\'
                    OR ? LIKE '%' || normalized_name || '%'
                )
                ORDER BY LENGTH(normalized_name) DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (room_uuid, f"%{escape_like(normalized)}%", normalized),
            ).fetchone()
            return None if row is None else _row_to_motion(row)

    async def latest(self, room_uuid: str) -> InventedMotion | None:
        await self.initialize()
        return await asyncio.to_thread(self._latest_sync, room_uuid)

    def _latest_sync(self, room_uuid: str) -> InventedMotion | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM invented_motions WHERE room_uuid = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (room_uuid,),
            ).fetchone()
            return None if row is None else _row_to_motion(row)

    async def list_names(
        self, room_uuid: str, *, limit: int = 10
    ) -> tuple[str, ...]:
        await self.initialize()
        return await asyncio.to_thread(self._list_names_sync, room_uuid, limit)

    def _list_names_sync(self, room_uuid: str, limit: int) -> tuple[str, ...]:
        if limit <= 0:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT name FROM invented_motions WHERE room_uuid = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (room_uuid, limit),
            ).fetchall()
            return tuple(str(row["name"]) for row in rows)

    async def record_play(self, motion_id: int) -> None:
        await self.initialize()
        await asyncio.to_thread(self._record_play_sync, motion_id)

    def _record_play_sync(self, motion_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE invented_motions SET played_count = played_count + 1 "
                "WHERE id = ?",
                (motion_id,),
            )


def _row_to_motion(row: sqlite3.Row) -> InventedMotion:
    return InventedMotion(
        id=int(row["id"]),
        room_uuid=str(row["room_uuid"]),
        name=str(row["name"]),
        spec_text=str(row["spec_text"]),
        source_request_id=str(row["source_request_id"]),
        created_at=float(row["created_at"]),
        played_count=int(row["played_count"]),
    )
