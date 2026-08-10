"""Bridge-owned, room-scoped household memory backed by SQLite FTS5."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
import os
import sqlite3
import time
import unicodedata


@dataclass(frozen=True, slots=True)
class MemoryFact:
    id: int
    room_uuid: str
    subject: str
    value: str
    text: str
    kind: str
    source_request_id: str
    created_at: float
    active: bool
    superseded_by: int | None


class HouseholdMemory:
    """A lazy-initialized async facade over a private standalone memory DB."""

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
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_uuid TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    value TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_request_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    superseded_by INTEGER REFERENCES facts(id)
                );

                CREATE INDEX IF NOT EXISTS facts_room_active_recent
                ON facts(room_uuid, active, created_at DESC);

                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                    normalized_text,
                    content='facts',
                    content_rowid='id',
                    tokenize='trigram'
                );

                CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(rowid, normalized_text)
                    VALUES (new.id, new.normalized_text);
                END;

                CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, normalized_text)
                    VALUES ('delete', old.id, old.normalized_text);
                END;

                CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF normalized_text ON facts BEGIN
                    INSERT INTO facts_fts(facts_fts, rowid, normalized_text)
                    VALUES ('delete', old.id, old.normalized_text);
                    INSERT INTO facts_fts(rowid, normalized_text)
                    VALUES (new.id, new.normalized_text);
                END;
                """
            )
            # Keep the external-content index recoverable across schema upgrades.
            connection.execute("INSERT INTO facts_fts(facts_fts) VALUES ('rebuild')")
        os.chmod(self.path, 0o600)

    async def remember(
        self,
        room_uuid: str,
        text: str,
        source_request_id: str,
        *,
        kind: str = "explicit",
        created_at: float | None = None,
    ) -> MemoryFact:
        await self.initialize()
        return await asyncio.to_thread(
            self._remember_sync,
            room_uuid,
            text,
            source_request_id,
            kind,
            created_at,
        )

    def _remember_sync(
        self,
        room_uuid: str,
        text: str,
        source_request_id: str,
        kind: str,
        created_at: float | None,
    ) -> MemoryFact:
        fact_text = text.strip()
        if not fact_text:
            raise ValueError("memory fact cannot be empty")
        normalized = normalize_japanese_text(fact_text)
        if not normalized:
            raise ValueError("memory fact has no searchable text")
        subject, value = split_subject_value(fact_text)
        current = time.time() if created_at is None else created_at
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM facts WHERE source_request_id = ?",
                (source_request_id,),
            ).fetchone()
            if existing is not None:
                return self._row_to_fact(existing)
            active_rows = connection.execute(
                """
                SELECT * FROM facts
                WHERE room_uuid = ? AND active = 1
                ORDER BY created_at DESC, id DESC
                """,
                (room_uuid,),
            ).fetchall()
            cursor = connection.execute(
                """
                INSERT INTO facts (
                    room_uuid, subject, value, normalized_text, kind,
                    source_request_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_uuid,
                    subject,
                    value,
                    normalized,
                    kind,
                    source_request_id,
                    current,
                ),
            )
            fact_id = int(cursor.lastrowid)
            new_subject = normalize_japanese_text(subject)
            superseded_ids = [
                int(row["id"])
                for row in active_rows
                if str(row["normalized_text"]) == normalized
                or (
                    bool(new_subject)
                    and normalize_japanese_text(str(row["subject"])) == new_subject
                )
            ]
            if superseded_ids:
                placeholders = ",".join("?" for _ in superseded_ids)
                connection.execute(
                    f"""
                    UPDATE facts SET active = 0, superseded_by = ?
                    WHERE id IN ({placeholders})
                    """,
                    (fact_id, *superseded_ids),
                )
            row = connection.execute(
                "SELECT * FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            assert row is not None
            return self._row_to_fact(row)

    async def forget(self, room_uuid: str, keyword: str) -> int:
        await self.initialize()
        return await asyncio.to_thread(self._forget_sync, room_uuid, keyword)

    def _forget_sync(self, room_uuid: str, keyword: str) -> int:
        normalized = normalize_japanese_text(keyword)
        if not normalized:
            return 0
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE facts SET active = 0
                WHERE room_uuid = ? AND active = 1
                    AND normalized_text LIKE ? ESCAPE '\\'
                """,
                (room_uuid, f"%{_escape_like(normalized)}%"),
            )
            return cursor.rowcount

    async def list_active(self, room_uuid: str, *, limit: int = 10) -> tuple[MemoryFact, ...]:
        await self.initialize()
        return await asyncio.to_thread(self._list_active_sync, room_uuid, limit)

    def _list_active_sync(self, room_uuid: str, limit: int) -> tuple[MemoryFact, ...]:
        if limit <= 0:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM facts
                WHERE room_uuid = ? AND active = 1
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (room_uuid, limit),
            ).fetchall()
            return tuple(self._row_to_fact(row) for row in rows)

    async def search(
        self,
        room_uuid: str,
        query_text: str,
        *,
        limit: int = 5,
        max_chars: int = 600,
    ) -> tuple[MemoryFact, ...]:
        await self.initialize()
        return await asyncio.to_thread(
            self._search_sync, room_uuid, query_text, limit, max_chars
        )

    def _search_sync(
        self,
        room_uuid: str,
        query_text: str,
        limit: int,
        max_chars: int,
    ) -> tuple[MemoryFact, ...]:
        normalized = normalize_japanese_text(query_text)
        if not normalized or limit <= 0 or max_chars <= 0:
            return ()
        with self._connection() as connection:
            query = trigram_match_query(normalized)
            if query is None:
                rows = connection.execute(
                    """
                    SELECT * FROM facts
                    WHERE room_uuid = ? AND active = 1
                        AND normalized_text LIKE ? ESCAPE '\\'
                    ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    (room_uuid, f"%{_escape_like(normalized)}%", limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT facts.* FROM facts_fts
                    JOIN facts ON facts.id = facts_fts.rowid
                    WHERE facts_fts MATCH ? AND facts.room_uuid = ?
                        AND facts.active = 1
                    ORDER BY bm25(facts_fts), facts.created_at DESC
                    LIMIT ?
                    """,
                    (query, room_uuid, limit),
                ).fetchall()
        selected: list[MemoryFact] = []
        used = 0
        for row in rows:
            fact = self._row_to_fact(row)
            if used + len(fact.text) > max_chars:
                continue
            selected.append(fact)
            used += len(fact.text)
        return tuple(selected)

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> MemoryFact:
        return MemoryFact(
            id=int(row["id"]),
            room_uuid=str(row["room_uuid"]),
            subject=str(row["subject"]),
            value=str(row["value"]),
            text=_display_text(str(row["subject"]), str(row["value"])),
            kind=str(row["kind"]),
            source_request_id=str(row["source_request_id"]),
            created_at=float(row["created_at"]),
            active=bool(row["active"]),
            superseded_by=(
                None if row["superseded_by"] is None else int(row["superseded_by"])
            ),
        )


def normalize_japanese_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] in {"L", "N"}
    )


def trigram_match_query(normalized_text: str) -> str | None:
    """Build an FTS5 trigram MATCH expression, or ``None`` when too short.

    A query shorter than one trigram cannot address the index at all; callers
    fall back to a ``LIKE`` scan. The trigram budget keeps a long utterance
    from turning into an unbounded MATCH expression.
    """

    if len(normalized_text) < 3:
        return None
    trigrams = tuple(
        dict.fromkeys(
            normalized_text[index : index + 3]
            for index in range(len(normalized_text) - 2)
        )
    )[:48]
    return " OR ".join(f'"{trigram}"' for trigram in trigrams)


def escape_like(value: str) -> str:
    """Escape a ``LIKE`` pattern for the shared ``ESCAPE '\\'`` convention."""

    return _escape_like(value)


def split_subject_value(text: str) -> tuple[str, str]:
    compact = " ".join(text.strip().split())
    for separator in ("は", "＝", "=", "：", ":"):
        subject, found, value = compact.partition(separator)
        if found and subject.strip() and value.strip():
            return subject.strip(), value.strip()
    return "", compact


def _display_text(subject: str, value: str) -> str:
    return f"{subject}は{value}" if subject else value


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
