"""Bridge-owned, room-scoped conversation transcript backed by SQLite FTS5.

Hermes keeps one server-side conversation per room and appends to it forever,
so its prompt grows without bound. The bridge therefore keeps its own record
of what was actually said and assembles a *bounded* prompt from it: a short
verbatim window of the most recent exchanges plus a few older exchanges
retrieved by relevance.

This is deliberately not part of :mod:`bocco_bridge.memory`. Household facts
are few, explicitly dictated with ``おぼえて``, superseded by subject, and are
meant to live forever. Conversation turns are high-volume, captured without
the user asking, never superseded, and pruned by retention. They share only
their retrieval mechanics — Japanese normalization, an FTS5 trigram index and
BM25 ranking — which this module reuses from :mod:`bocco_bridge.memory`. The
turns live in their own database file so that resetting or deleting a
transcript can never endanger the explicit facts.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Sequence
import os
import re
import sqlite3
import time

from .choreography import extract_motion_cues
from .embeddings import (
    pack_vector,
    rank_by_similarity,
    reciprocal_rank_fusion,
)
from .memory import escape_like, normalize_japanese_text, trigram_match_query


# Stored turn text is capped well above the reply cap (200 characters) so a
# long transcription still lands whole, while one pathological utterance can
# never bloat a row. Prompt-side truncation is separate and configurable.
TURN_TEXT_MAX_CHARS = 500

# A temporal window is walked through the (room_uuid, created_at DESC) index,
# so its cost tracks how much was said inside the window rather than how big
# the store has grown. This cap is the braces to that belt: a week-wide
# window on a chatty room is sampled from its most recent turns instead of
# being materialized whole on the reply hot path.
TEMPORAL_WINDOW_SCAN_CAP = 200

# How much deeper than its configured size the verbatim recent window is read,
# so that thinning deflections out of it can backfill rather than shrink it.
# Three covers a run of refusals twice as long as the window itself, which is
# further than the live store ever got before someone deleted the rows by hand.
RECENT_WINDOW_OVERFETCH = 3


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One completed exchange: what the user said and what was spoken back."""

    id: int
    room_uuid: str
    request_id: str
    user_text: str
    reply_text: str
    created_at: float


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """The two halves of a bounded prompt, both in chronological order.

    ``recent`` is present regardless of relevance — it is what keeps "what did
    I just say", pronouns and follow-ups working, and is ordered oldest to
    newest. ``retrieved`` holds older exchanges in BM25 rank order and is
    disjoint from ``recent``.
    """

    recent: tuple[ConversationTurn, ...] = ()
    retrieved: tuple[ConversationTurn, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.recent or self.retrieved)


@dataclass(frozen=True, slots=True)
class SemanticQuery:
    """An embedded query plus the bounds of the sweep it may run.

    Its presence is the entire feature switch inside this module: ``None``
    means the retrieval below is the byte-for-byte BM25 path that shipped
    before embeddings existed. The caller owns the network call and the
    deadline, so nothing in here can ever wait on a service.

    ``model`` is carried, not assumed. Vectors from two different models are
    not comparable, and a model swap that silently ranked new queries against
    old rows would degrade retrieval in a way no test would catch — so the
    sweep filters on it and simply finds nothing until the backfill has run.
    """

    vector: tuple[float, ...]
    model: str
    candidates: int = 12
    scan_cap: int = 500
    min_similarity: float = 0.0


@dataclass(frozen=True, slots=True)
class TemporalWindow:
    """A half-open ``[start, end)`` range plus the query with the dates removed.

    ``residue`` earns its place. The words that name a period — 昨日, きのう —
    are also the words the robot uses when it *talks about* a period, so a
    keyword search for 「昨日何した」 ranks its own past 「昨日のことは…」 refusal
    above anything that actually happened yesterday. Removing them leaves the
    topic, if the utterance had one, and leaves nothing when it did not.
    """

    start: float
    end: float
    residue: str


def local_time(timestamp: float) -> datetime:
    """The robot's own wall clock for an epoch second.

    The system zone, not a hard-coded one: the Pi image runs Asia/Tokyo, and
    every other local-time decision in the bridge — the morning briefing's
    date, the illuminance greeting's hour — already reads it this way. A
    transcript that disagreed with the briefing about which day it is would be
    a worse bug than one that is merely wrong on a misconfigured host.
    """

    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone()


def _wall_midnight(moment: datetime) -> datetime:
    """Local midnight of ``moment``'s date, as a naive wall-clock value."""

    return moment.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def _localize(wall: datetime) -> datetime:
    """Re-attach the offset that actually applies on *that* date.

    Boundaries are computed on the naive wall clock and localized here rather
    than by adding timedeltas to an aware value: a fixed offset carried forward
    over a DST edge would put "midnight" an hour into the wrong day. Japan has
    no DST, so this only ever matters on a mis-zoned host — which is precisely
    when a date boundary must not quietly move.
    """

    return wall.astimezone()


def _at(moment: datetime, *, days: int = 0, hours: int = 0) -> datetime:
    return _localize(_wall_midnight(moment) + timedelta(days=days, hours=hours))


def _days(moment: datetime, offset: int) -> datetime:
    return _at(moment, days=offset)


def _month_start(moment: datetime, offset: int) -> datetime:
    year, month = moment.year, moment.month + offset
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return _localize(_wall_midnight(moment).replace(year=year, month=month, day=1))


def _week_start(moment: datetime, offset: int) -> datetime:
    """Monday-anchored, matching how 先週/今週 are read in Japanese."""

    return _days(moment, -moment.weekday() + 7 * offset)


_Bounds = Callable[[datetime], tuple[datetime, datetime]]

# Ordered most-specific first, and the order is load-bearing twice over: 一昨日
# contains 昨日, so the longer spelling has to be tried first or every 一昨日
# collapses into 昨日 — and the same order is reused verbatim to build the
# stripping pattern below, where Python's leftmost-alternative rule needs it
# for the same reason.
_TEMPORAL_PERIODS: tuple[tuple[str, _Bounds], ...] = (
    ("一昨日|おととい|おとつい", lambda n: (_days(n, -2), _days(n, -1))),
    ("今朝|けさ", lambda n: (_days(n, 0), _at(n, hours=12))),
    (
        "昨夜|昨晩|ゆうべ|夕べ|昨日の夜",
        lambda n: (_at(n, days=-1, hours=18), _at(n, hours=6)),
    ),
    ("昨日|きのう|さくじつ", lambda n: (_days(n, -1), _days(n, 0))),
    # 「さっき」 is the only reference that is not a calendar span; three hours
    # is roughly "this sitting" without reaching back into the morning.
    ("さっき|先ほど|さきほど", lambda n: (n - timedelta(hours=3), n)),
    ("先週|せんしゅう", lambda n: (_week_start(n, -1), _week_start(n, 0))),
    ("今週|こんしゅう", lambda n: (_week_start(n, 0), _days(n, 1))),
    ("先月|せんげつ", lambda n: (_month_start(n, -1), _month_start(n, 0))),
    ("今月|こんげつ", lambda n: (_month_start(n, 0), _days(n, 1))),
    (
        "この前|このまえ|こないだ|この間|このあいだ",
        lambda n: (_days(n, -7), _days(n, 1)),
    ),
    ("今日|きょう|本日", lambda n: (_days(n, 0), _days(n, 1))),
)

_TEMPORAL_PATTERNS = tuple(
    (re.compile(pattern), resolve) for pattern, resolve in _TEMPORAL_PERIODS
)

# Speech transcription hands back whichever digit width the recognizer felt
# like, so 「3日前」 and 「３日前」 both have to land.
_DAYS_AGO_SOURCE = r"[0-9０-９]{1,2}日前"
_DAYS_AGO = re.compile(r"([0-9０-９]{1,2})日前")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

_TEMPORAL_ANY = re.compile(
    "|".join(pattern for pattern, _ in _TEMPORAL_PERIODS) + f"|{_DAYS_AGO_SOURCE}"
)

# A time word alone is not a question about the past. 「今日はいい天気だね」 must
# keep behaving exactly as it did before this existed, so the temporal branch
# only opens when the utterance also *asks* something — an interrogative, a
# recall verb, or a plain question mark.
_RECALL_CUE = re.compile(
    "覚え|おぼえ|憶え|忘れ|わすれ"
    "|何|なに|なん|どんな|どう|どこ|いつ|誰|だれ"
    "|っけ|教えて|おしえて"
    "|話した|話し|言った|言って|聞いた|やった|したこと"
    r"|[?？]"
)


def resolve_temporal_window(query_text: str, now: float) -> TemporalWindow | None:
    """Turn 「昨日何した」 into a concrete range in the robot's local time.

    ``None`` means "no time reference worth acting on", which is the answer for
    the overwhelming majority of utterances and keeps retrieval on exactly the
    path it took before. Pure function of its arguments — the caller supplies
    ``now`` so the resolution is reproducible in a test.
    """

    if not query_text or _RECALL_CUE.search(query_text) is None:
        return None
    local_now = local_time(now)
    bounds: tuple[datetime, datetime] | None = None
    for pattern, resolve in _TEMPORAL_PATTERNS:
        if pattern.search(query_text) is not None:
            bounds = resolve(local_now)
            break
    if bounds is None:
        relative = _DAYS_AGO.search(query_text)
        if relative is None:
            return None
        offset = int(str(relative.group(1)).translate(_FULLWIDTH_DIGITS))
        if offset < 1:
            return None
        bounds = (_days(local_now, -offset), _days(local_now, -offset + 1))
    start, end = bounds
    return TemporalWindow(
        start=start.timestamp(),
        end=end.timestamp(),
        residue=_TEMPORAL_ANY.sub(" ", query_text),
    )


# The negated forms of a handful of epistemic and ability verbs — 分かる, 知る,
# 覚える, 記憶, 出来る. Deliberately a *grammatical* shape (stem plus negation)
# rather than a list of the persona's sentences: swap the persona, its
# politeness level or its sentence-final particles and the stem-plus-negation
# is what survives, which is exactly the brittleness a phrase blocklist has
# and this does not.
_DECLINE = re.compile(
    "(?:わから|わかり|分から|分かり|判ら|判り|しら|しり|知ら|知り"
    "|覚えて|覚え|おぼえて|おぼえ|憶えて|記憶に|でき|出来)"
    "(?:ない|ぬ|ません|なかった|ありません|かねます|かねる"
    "|ん[だでよねなじ、。！？]|ん$)"
)

# A reply that declines *and* goes on to say something useful is longer than
# one that only declines. The refusals in the live store measure 7 to 21
# characters; the bound sits above that and below
# 「そこはわからないけど、明日は雨だから傘を持っていってね。きっと役に立つよ」, which
# carries the weather and is worth showing. Getting this wrong in the strict
# direction costs one turn out of five hundred; getting it wrong the other way
# is the bug this exists to kill, so the bound leans strict.
DECLINE_MAX_CHARS = 30


def is_deflection(reply_text: str) -> bool:
    """Whether a stored reply is the robot declining rather than answering.

    Recognised on the words the household actually heard: ``[motion:こまった]``
    is an instruction to the body, stripped before the speech is sent, and a
    copy that reached the transcript must not change whether a refusal reads
    as one.
    """

    spoken = " ".join(extract_motion_cues(reply_text).text.split())
    if not spoken or len(spoken) > DECLINE_MAX_CHARS:
        return False
    return _DECLINE.search(spoken) is not None


def retrieval_query_text(query_text: str, now: float) -> str:
    """The text retrieval should actually search for, dates removed.

    Exposed so the caller can embed the same string the lexical path searches.
    Embedding the raw utterance would hand the vector space the exact problem
    :class:`TemporalWindow` exists to solve, and hand it worse: 昨日 and
    「昨日のことは、わからないよ。」 are not merely lexically similar, they are
    *semantically* similar, so a dense ranker would promote the robot's own
    refusal even more confidently than BM25 did.
    """

    window = resolve_temporal_window(query_text, now)
    return query_text if window is None else window.residue


def embedding_text(user_text: str, reply_text: str) -> str:
    """The passage text for one stored turn.

    Both halves, joined exactly as ``normalized_text`` joins them, because the
    topic can sit on either side: 「君の好きな食べ物何?」 carries 食べ物 and
    「あったかいスープが好きだよ」 carries スープ, and a later 「ごはんの話したっけ」
    needs to reach the pair.

    Deliberately *without* the 「（昨日 19:09）」 label the prompt renders. The
    label is relative to the moment of rendering — today's 昨日 is tomorrow's
    一昨日 — so embedding it would bake in a value that goes stale nightly and
    would need the whole store re-embedded to stay true. Time is already
    handled exactly, by ``created_at`` and a half-open window; asking a
    384-dimensional space to approximate a comparison SQLite does precisely
    would trade a correct answer for a fuzzy one.
    """

    return f"{_clip(user_text)} {_clip(reply_text)}".strip()


class ConversationTranscript:
    """A lazy-initialized async facade over a private transcript database."""

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
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_uuid TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    user_text TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS turns_room_recent
                ON turns(room_uuid, created_at DESC, id DESC);

                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    normalized_text,
                    content='turns',
                    content_rowid='id',
                    tokenize='trigram'
                );

                CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
                    INSERT INTO turns_fts(rowid, normalized_text)
                    VALUES (new.id, new.normalized_text);
                END;

                CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
                    INSERT INTO turns_fts(turns_fts, rowid, normalized_text)
                    VALUES ('delete', old.id, old.normalized_text);
                END;

                CREATE TRIGGER IF NOT EXISTS turns_au
                AFTER UPDATE OF normalized_text ON turns BEGIN
                    INSERT INTO turns_fts(turns_fts, rowid, normalized_text)
                    VALUES ('delete', old.id, old.normalized_text);
                    INSERT INTO turns_fts(rowid, normalized_text)
                    VALUES (new.id, new.normalized_text);
                END;

                -- Semantic retrieval's side of the store. One row per turn at
                -- most, written by a background task well after the reply was
                -- spoken, and absent for any turn the embedding service was
                -- unreachable for. Everything downstream treats a missing row
                -- as "this turn is lexically retrievable only", which is what
                -- the whole store was until this landed.
                --
                -- Created unconditionally: the DDL is idempotent, costs one
                -- statement at startup, writes nothing, and means switching
                -- the feature on later is a config change rather than a
                -- migration. The database itself still does not exist until
                -- conversation memory is enabled at all.
                CREATE TABLE IF NOT EXISTS turn_vectors (
                    turn_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    dims INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    created_at REAL NOT NULL
                );

                -- Retention deletes turns; without this the vectors of the
                -- deleted ones would accumulate forever behind a store that
                -- is supposed to be bounded. A trigger rather than a foreign
                -- key because this connection does not enable foreign keys and
                -- turning them on now would change delete semantics elsewhere.
                CREATE TRIGGER IF NOT EXISTS turns_ad_vectors
                AFTER DELETE ON turns BEGIN
                    DELETE FROM turn_vectors WHERE turn_id = old.id;
                END;
                """
            )
            # Retention keeps this table small, so the external-content index
            # stays cheap to rebuild and recoverable across schema upgrades.
            connection.execute("INSERT INTO turns_fts(turns_fts) VALUES ('rebuild')")
        os.chmod(self.path, 0o600)

    async def record(
        self,
        room_uuid: str,
        request_id: str,
        user_text: str,
        reply_text: str,
        *,
        created_at: float | None = None,
        retention_turns: int = 0,
    ) -> ConversationTurn | None:
        """Persist one completed exchange; ``None`` when there is nothing to store.

        Idempotent per ``request_id``: a replayed event stores one turn.
        """

        await self.initialize()
        return await asyncio.to_thread(
            self._record_sync,
            room_uuid,
            request_id,
            user_text,
            reply_text,
            created_at,
            retention_turns,
        )

    def _record_sync(
        self,
        room_uuid: str,
        request_id: str,
        user_text: str,
        reply_text: str,
        created_at: float | None,
        retention_turns: int,
    ) -> ConversationTurn | None:
        user = _clip(user_text)
        reply = _clip(reply_text)
        if not user or not reply or not room_uuid or not request_id:
            return None
        normalized = normalize_japanese_text(f"{user} {reply}")
        if not normalized:
            return None
        current = time.time() if created_at is None else created_at
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM turns WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                return _row_to_turn(existing)
            cursor = connection.execute(
                """
                INSERT INTO turns (
                    room_uuid, request_id, user_text, reply_text,
                    normalized_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (room_uuid, request_id, user, reply, normalized, current),
            )
            turn_id = int(cursor.lastrowid)
            if retention_turns > 0:
                connection.execute(
                    """
                    DELETE FROM turns
                    WHERE room_uuid = ? AND id NOT IN (
                        SELECT id FROM turns WHERE room_uuid = ?
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (room_uuid, room_uuid, retention_turns),
                )
            row = connection.execute(
                "SELECT * FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            assert row is not None
            return _row_to_turn(row)

    async def store_vector(
        self,
        turn_id: int,
        vector: Sequence[float],
        *,
        model: str,
        created_at: float | None = None,
    ) -> bool:
        """Attach an embedding to a turn; ``False`` when the turn has gone.

        ``INSERT OR REPLACE`` rather than ``INSERT``: re-embedding a turn under
        the same model has to be a no-op the second time and a correction the
        first, which is what makes the backfill tool safe to interrupt and
        re-run. The join to ``turns`` is what stops a vector outliving a turn
        retention deleted while the background task was in flight.
        """

        await self.initialize()
        return await asyncio.to_thread(
            self._store_vector_sync,
            turn_id,
            pack_vector(vector),
            len(vector),
            model,
            time.time() if created_at is None else created_at,
        )

    def _store_vector_sync(
        self, turn_id: int, blob: bytes, dims: int, model: str, created_at: float
    ) -> bool:
        if dims < 1 or not model:
            return False
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR REPLACE INTO turn_vectors
                    (turn_id, model, dims, vector, created_at)
                SELECT turns.id, ?, ?, ?, ? FROM turns WHERE turns.id = ?
                """,
                (model, dims, blob, created_at, turn_id),
            )
            return cursor.rowcount > 0

    async def turns_without_vectors(
        self, room_uuid: str, *, model: str, limit: int, after_id: int = 0
    ) -> tuple[ConversationTurn, ...]:
        """The next batch of turns that has no embedding under ``model``.

        Ordered by id so the backfill walks forward and can resume from the
        last id it finished rather than re-deciding from scratch — and the
        ``NOT EXISTS`` means it stays correct even if it does re-decide, which
        is what makes an interrupted run idempotent instead of merely cheap.
        """

        await self.initialize()
        return await asyncio.to_thread(
            self._turns_without_vectors_sync, room_uuid, model, limit, after_id
        )

    def _turns_without_vectors_sync(
        self, room_uuid: str, model: str, limit: int, after_id: int
    ) -> tuple[ConversationTurn, ...]:
        if limit <= 0:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT turns.* FROM turns
                WHERE turns.room_uuid = ? AND turns.id > ?
                  AND NOT EXISTS (
                      SELECT 1 FROM turn_vectors
                      WHERE turn_vectors.turn_id = turns.id
                        AND turn_vectors.model = ?
                  )
                ORDER BY turns.id LIMIT ?
                """,
                (room_uuid, after_id, model, limit),
            ).fetchall()
            return tuple(_row_to_turn(row) for row in rows)

    async def vector_count(self, room_uuid: str, *, model: str) -> int:
        """How much of a room is semantically retrievable, for the tools."""

        await self.initialize()
        return await asyncio.to_thread(self._vector_count_sync, room_uuid, model)

    def _vector_count_sync(self, room_uuid: str, model: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total FROM turn_vectors
                JOIN turns ON turns.id = turn_vectors.turn_id
                WHERE turns.room_uuid = ? AND turn_vectors.model = ?
                """,
                (room_uuid, model),
            ).fetchone()
            return 0 if row is None else int(row["total"])

    async def count(self, room_uuid: str) -> int:
        """Total turns ever stored for a room, retention losses included."""

        await self.initialize()
        return await asyncio.to_thread(self._count_sync, room_uuid)

    def _count_sync(self, room_uuid: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM turns WHERE room_uuid = ?",
                (room_uuid,),
            ).fetchone()
            return 0 if row is None else int(row["total"])

    async def context(
        self,
        room_uuid: str,
        query_text: str,
        *,
        recent_turns: int = 6,
        retrieved_turns: int = 3,
        now: float | None = None,
        semantic: SemanticQuery | None = None,
    ) -> ConversationContext:
        """Fetch the verbatim recent window plus disjoint retrieved older turns.

        ``now`` is what lets 「昨日何した」 be answered by time rather than by
        keyword; the caller passes its injected clock so a test can sit the
        robot anywhere it likes. Omitting it is the pre-temporal behaviour and
        the caller then gets pure BM25, exactly as before.

        ``semantic`` carries an already-embedded query. It is optional in the
        strongest sense: without it not one line of the vector code below runs,
        no ``turn_vectors`` row is read, and the result is the same rows in the
        same order this method returned before embeddings existed. The caller
        passes ``None`` when the feature is off *and* when the embedding
        service missed its deadline, so both are one code path rather than two.
        """

        await self.initialize()
        return await asyncio.to_thread(
            self._context_sync,
            room_uuid,
            query_text,
            recent_turns,
            retrieved_turns,
            now,
            semantic,
        )

    def _context_sync(
        self,
        room_uuid: str,
        query_text: str,
        recent_turns: int,
        retrieved_turns: int,
        now: float | None = None,
        semantic: SemanticQuery | None = None,
    ) -> ConversationContext:
        if not room_uuid:
            return ConversationContext()
        # One connection, both queries: the single event worker pays one thread
        # hop per reply rather than two.
        with self._connection() as connection:
            recent: tuple[ConversationTurn, ...] = ()
            if recent_turns > 0:
                # Over-fetched so that thinning refuses to shrink the window:
                # what a run of deflections displaces is replaced from further
                # back. The multiplier bounds the read at a constant — still
                # one walk of the (room_uuid, created_at DESC) index, a few
                # dozen small rows — and a streak longer than it simply leaves
                # the window shorter, which is the safe direction.
                rows = connection.execute(
                    """
                    SELECT * FROM turns WHERE room_uuid = ?
                    ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    (room_uuid, recent_turns * RECENT_WINDOW_OVERFETCH),
                ).fetchall()
                recent = _thin_deflections(
                    tuple(_row_to_turn(row) for row in reversed(rows)),
                    recent_turns,
                )
            exclude_ids = tuple(turn.id for turn in recent)
            window = (
                None if now is None else resolve_temporal_window(query_text, now)
            )
            retrieved: tuple[ConversationTurn, ...] = ()
            if window is not None:
                retrieved = self._retrieve_temporal(
                    connection,
                    room_uuid,
                    window,
                    retrieved_turns,
                    exclude_ids,
                    semantic,
                )
            # Keyword search is both the non-temporal path and the safety net
            # under the temporal one: a window that turns up nothing — the user
            # asks about 昨日 on a day the robot was unplugged — must not leave
            # retrieval emptier than it was before this feature existed. The
            # fallback searches the de-dated residue, never the raw utterance,
            # so 昨日 cannot re-acquire its old habit of matching the robot's
            # own 「昨日のことは…」 refusal instead of anything from yesterday.
            if not retrieved:
                retrieved = self._retrieve_ranked(
                    connection,
                    room_uuid,
                    query_text if window is None else window.residue,
                    retrieved_turns,
                    exclude_ids,
                    semantic,
                )
        return ConversationContext(recent=recent, retrieved=retrieved)

    @classmethod
    def _retrieve_ranked(
        cls,
        connection: sqlite3.Connection,
        room_uuid: str,
        query_text: str,
        limit: int,
        exclude_ids: Sequence[int],
        semantic: SemanticQuery | None,
        *,
        window: tuple[float, float] | None = None,
    ) -> tuple[ConversationTurn, ...]:
        """BM25 alone, or BM25 fused with a vector sweep when one is available.

        The ``semantic is None`` arm is not a special case bolted on — it is
        the original call, unchanged, and every degradation upstream funnels
        into it. That is what makes a latency regression structurally
        impossible rather than merely unlikely: the slow path cannot be reached
        without a vector already in hand.
        """

        if semantic is None or not normalize_japanese_text(query_text):
            # Nothing left to search for is the 「昨日？」 case: the date was the
            # whole utterance and stripping it left punctuation. Ranking the
            # day's turns by similarity to a question that named no subject
            # would be arithmetic in search of a meaning, so the sweep is
            # skipped and the temporal spread answers on its own. The emptiness
            # test is the lexical path's own — whatever BM25 considers
            # unsearchable, the sweep declines too, so the two halves of the
            # fusion never disagree about whether there was a question.
            return cls._retrieve(
                connection, room_uuid, query_text, limit, exclude_ids, window=window
            )
        return cls._retrieve_hybrid(
            connection, room_uuid, query_text, limit, exclude_ids, semantic, window
        )

    @classmethod
    def _retrieve_hybrid(
        cls,
        connection: sqlite3.Connection,
        room_uuid: str,
        query_text: str,
        limit: int,
        exclude_ids: Sequence[int],
        semantic: SemanticQuery,
        window: tuple[float, float] | None,
    ) -> tuple[ConversationTurn, ...]:
        """Rank BM25 and cosine separately, then fuse the two orderings.

        Both rankings are deepened to ``semantic.candidates`` before fusing:
        the point of fusion is that a turn ranked fourth lexically and first
        semantically should win a slot, and it cannot if the lexical list was
        truncated at three before anyone looked at it.

        The two searches complement rather than compete. BM25 keeps winning on
        rare exact tokens — 大阪, スープ, a name, a number — where a dense model
        blurs the very thing that made the match; the sweep wins on paraphrase,
        where there is no shared character to score at all.
        """

        lexical = cls._retrieve(
            connection,
            room_uuid,
            query_text,
            semantic.candidates,
            exclude_ids,
            window=window,
        )
        vector_ids = cls._retrieve_semantic(
            connection, room_uuid, semantic, exclude_ids, window
        )
        if not vector_ids:
            # No embedded turns in range, or none above the similarity floor.
            # Answer exactly what BM25 answered.
            return lexical[:limit]
        lexical_ids = tuple(turn.id for turn in lexical)
        fused = reciprocal_rank_fusion(
            (lexical_ids, vector_ids), limit=limit
        )
        if not fused:
            return ()
        known = {turn.id: turn for turn in lexical}
        # Only the vector-only ids need a second trip; the lexical half already
        # came back whole. One IN-list against the primary key, bounded by
        # `limit`, is cheaper than re-running either search.
        missing = tuple(turn_id for turn_id in fused if turn_id not in known)
        if missing:
            placeholders = ",".join("?" for _ in missing)
            for row in connection.execute(
                f"SELECT * FROM turns WHERE id IN ({placeholders})", missing
            ).fetchall():
                turn = _row_to_turn(row)
                known[turn.id] = turn
        return tuple(known[turn_id] for turn_id in fused if turn_id in known)

    @staticmethod
    def _retrieve_semantic(
        connection: sqlite3.Connection,
        room_uuid: str,
        semantic: SemanticQuery,
        exclude_ids: Sequence[int],
        window: tuple[float, float] | None,
    ) -> tuple[int, ...]:
        """Cosine top-k over the room's stored vectors, best first.

        The ``window`` restriction is not optional decoration. Time-aware
        retrieval confines the haystack to a half-open ``created_at`` range,
        and a semantic sweep that ignored it would answer 「昨日の天気の話覚えてる？」
        with last month's weather chat — a *better* match by cosine and a wrong
        answer to the question asked. Meaning narrows within the window; it
        never widens it.

        ``dims`` is filtered alongside ``model`` so that a row written by an
        earlier model with the same name but a different output width is
        skipped rather than scored against an incompatible query.
        """

        if semantic.candidates <= 0 or semantic.scan_cap <= 0:
            return ()
        restriction = ""
        bounded: tuple[object, ...] = ()
        if window is not None:
            restriction = " AND turns.created_at >= ? AND turns.created_at < ?"
            bounded = window
        exclusion = ""
        parameters: list[object] = []
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            exclusion = f" AND turns.id NOT IN ({placeholders})"
            parameters.extend(exclude_ids)
        rows = connection.execute(
            f"""
            SELECT turn_vectors.turn_id AS turn_id, turn_vectors.vector AS vector
            FROM turn_vectors
            JOIN turns ON turns.id = turn_vectors.turn_id
            WHERE turns.room_uuid = ?
              AND turn_vectors.model = ? AND turn_vectors.dims = ?
            {restriction}
            {exclusion}
            ORDER BY turns.created_at DESC, turns.id DESC LIMIT ?
            """,
            (
                room_uuid,
                semantic.model,
                len(semantic.vector),
                *bounded,
                *parameters,
                semantic.scan_cap,
            ),
        ).fetchall()
        return rank_by_similarity(
            semantic.vector,
            ((int(row["turn_id"]), row["vector"]) for row in rows),
            limit=semantic.candidates,
            min_similarity=semantic.min_similarity,
        )

    @classmethod
    def _retrieve_temporal(
        cls,
        connection: sqlite3.Connection,
        room_uuid: str,
        window: TemporalWindow,
        limit: int,
        exclude_ids: Sequence[int],
        semantic: SemanticQuery | None = None,
    ) -> tuple[ConversationTurn, ...]:
        """Fill the retrieval budget from inside ``window``, topic first.

        A question can be temporal *and* topical — 「昨日の天気の話覚えてる？」 —
        so the window narrows the haystack rather than replacing the search: the
        de-dated residue ranks turns inside the range first, and whatever slots
        that leaves are spent on a spread of the period itself. For 「昨日何した」
        the residue is nothing but particles and the whole budget goes to the
        spread, which is the right answer to a question about a day.
        """

        if limit <= 0:
            return ()
        bounds = (window.start, window.end)
        topical = cls._retrieve_ranked(
            connection,
            room_uuid,
            window.residue,
            limit,
            exclude_ids,
            semantic,
            window=bounds,
        )
        taken = [*exclude_ids, *(turn.id for turn in topical)]
        spread = cls._spread(connection, room_uuid, bounds, limit - len(topical), taken)
        # Topical hits lead because the caller drops the tail first when the
        # prompt budget runs out; a keyword match is the more defensible thing
        # to keep when only one turn fits.
        return topical + spread

    @staticmethod
    def _spread(
        connection: sqlite3.Connection,
        room_uuid: str,
        bounds: tuple[float, float],
        limit: int,
        exclude_ids: Sequence[int],
    ) -> tuple[ConversationTurn, ...]:
        """Sample ``limit`` turns spaced evenly across a period.

        A whole day does not fit in three slots, so the question is which three.
        The most recent three would answer 「昨日何した」 with nothing but last
        night; an even spread returns the morning, the middle and the evening,
        which is the *shape* of the day and the only selection that lets the
        model say what the day contained rather than how it ended.
        """

        if limit <= 0:
            return ()
        exclusion = ""
        parameters: list[object] = []
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            exclusion = f" AND id NOT IN ({placeholders})"
            parameters.extend(exclude_ids)
        # Newest-first with a hard cap, then reversed: the walk is an index
        # range scan over (room_uuid, created_at DESC), so it costs what the
        # window holds and not what the store holds, and the cap keeps even a
        # 先週 window from materializing a month of chatter on the hot path.
        rows = connection.execute(
            f"""
            SELECT * FROM turns
            WHERE room_uuid = ? AND created_at >= ? AND created_at < ?
            {exclusion}
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (room_uuid, bounds[0], bounds[1], *parameters, TEMPORAL_WINDOW_SCAN_CAP),
        ).fetchall()
        if not rows:
            return ()
        turns = [_row_to_turn(row) for row in reversed(rows)]
        if len(turns) <= limit:
            return tuple(turns)
        if limit == 1:
            return (turns[-1],)
        last = len(turns) - 1
        picked = dict.fromkeys(
            round(index * last / (limit - 1)) for index in range(limit)
        )
        return tuple(turns[index] for index in picked)

    @staticmethod
    def _retrieve(
        connection: sqlite3.Connection,
        room_uuid: str,
        query_text: str,
        limit: int,
        exclude_ids: Sequence[int],
        *,
        window: tuple[float, float] | None = None,
    ) -> tuple[ConversationTurn, ...]:
        normalized = normalize_japanese_text(query_text)
        if not normalized or limit <= 0:
            return ()
        restriction = ""
        bounded: tuple[object, ...] = ()
        if window is not None:
            restriction = " AND turns.created_at >= ? AND turns.created_at < ?"
            bounded = window
        exclusion = ""
        parameters: list[object] = []
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            exclusion = f" AND turns.id NOT IN ({placeholders})"
            parameters.extend(exclude_ids)
        match = trigram_match_query(normalized)
        if match is None:
            rows = connection.execute(
                f"""
                SELECT turns.* FROM turns
                WHERE turns.room_uuid = ?
                  AND turns.normalized_text LIKE ? ESCAPE '\\'
                {restriction}
                {exclusion}
                ORDER BY turns.created_at DESC, turns.id DESC LIMIT ?
                """,
                (
                    room_uuid,
                    f"%{escape_like(normalized)}%",
                    *bounded,
                    *parameters,
                    limit,
                ),
            ).fetchall()
        else:
            rows = connection.execute(
                f"""
                SELECT turns.* FROM turns_fts
                JOIN turns ON turns.id = turns_fts.rowid
                WHERE turns_fts MATCH ? AND turns.room_uuid = ?
                {restriction}
                {exclusion}
                ORDER BY bm25(turns_fts), turns.created_at DESC
                LIMIT ?
                """,
                (match, room_uuid, *bounded, *parameters, limit),
            ).fetchall()
        # Rank order, not chronological: the caller drops the weakest matches
        # first when the prompt budget runs out, and orders what survives.
        return tuple(_row_to_turn(row) for row in rows)


def _thin_deflections(
    turns: tuple[ConversationTurn, ...], limit: int
) -> tuple[ConversationTurn, ...]:
    """Newest ``limit`` turns, carrying at most one deflection: the latest.

    The verbatim window has two jobs and a deflection serves only one of them.
    It is needed for continuity — 「なんで？」 as the next utterance has to still
    refer to something — and anaphora reaches back exactly one exchange, so one
    deflection is the whole of what continuity requires. Every older one is
    pure prior.

    And prior is what broke this on the robot. Three consecutive 「昨日のことは
    覚えてないよ」 in front of a fourth asking is a model being consistent with
    itself, and it stayed consistent even with yesterday's conversations
    retrieved and labelled beside them. Worse, one of the three was volunteered
    in answer to a question that was not about yesterday at all — a general
    claim about its own memory, which is the kind that generalises.

    Keeping only the newest makes the prompt for the third asking identical to
    the prompt for the first. Asking again cannot entrench the answer, because
    asking again does not change what the model is shown — which is the
    property that matters, since a user who tries three times is exactly the
    one who most needs the third try to work.

    Dropped turns are replaced from further back rather than shrinking the
    window: its size is a budget of exchanges worth showing, and a refusal
    never was one.
    """

    kept: list[ConversationTurn] = []
    seen_deflection = False
    for turn in reversed(turns):
        if is_deflection(turn.reply_text):
            if seen_deflection:
                continue
            seen_deflection = True
        kept.append(turn)
        if len(kept) >= limit:
            break
    return tuple(reversed(kept))


def _clip(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:TURN_TEXT_MAX_CHARS]


def _row_to_turn(row: sqlite3.Row) -> ConversationTurn:
    return ConversationTurn(
        id=int(row["id"]),
        room_uuid=str(row["room_uuid"]),
        request_id=str(row["request_id"]),
        user_text=str(row["user_text"]),
        reply_text=str(row["reply_text"]),
        created_at=float(row["created_at"]),
    )
