"""Timestamped events extracted from conversation, and the rule that admits them.

WHY THIS EXISTS, MEASURED. The bridge already had two memories and neither one
could answer "what happened". ``memory.db``'s ``facts`` table only ever fills
when somebody says 「おぼえて：」 and was, on the live robot, completely empty.
So every 「覚えてる？」 fell through to :mod:`bocco_bridge.transcript`, which
searches raw chat, and raw chat produced two documented failures:

* Asked 「何を覚えているの」 the robot answered 「りんご、まくらを覚えてるよ」.
  Those are しりとり moves. Retrieval was correct; the corpus was wrong.
* Asked 「昨日の天気、29.2度だった？」 the stored line was 「**いま**東京は…29.2
  度だよ」 — true when spoken, false when replayed a day later under a
  「（昨日 16:24）」 label. **Deictic words rot in storage.**

Both are corpus problems, so this is a corpus: a store whose every row is a
dated occurrence with the utterance it came from still attached, and whose
deictic words were resolved at extraction time, while their meaning was still
known. 「いま」 spoken at 16:24 on 08-05 becomes that timestamp, permanently.

THE RULE, in the household's own words, and it is the correctness bar:

    "facts are what happened. this happened in the convo where you mentioned
    that you liked [x] is a fact. you like [x] is not a fact. e.g I went to
    the dentist today. the fact is that on this particular day i went to the
    dentist."

The rule is deliberately conservative and that is the whole point. A store the
robot may speak from confidently is only worth having if every row is
checkable, and a wrong row is worse than a missing one — the household will
trust this store in a way they do not trust the chat log.

WHY ITS OWN DATABASE, and not ``facts`` with ``kind='event'``. The house rule
is already written down in :mod:`bocco_bridge.repertoire`: a separate concern
gets a separate store. Three specifics decide it here:

* ``facts`` supersedes by **subject** — a second 「歯医者」 row would deactivate
  the first. Two dentist visits on two days are both true forever, so the one
  behaviour that makes ``facts`` right for dictated prose makes it wrong for
  occurrences.
* ``facts`` has one timestamp. An event needs two — when the thing happened and
  when it was said — and the gap between them is exactly the provenance the
  household asked for.
* ``facts`` is what 「なにを覚えてる」 lists back. Machine-extracted rows flooding
  a list of things the user dictated by hand would take a feature that works
  and make it useless.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Iterator
import json
import logging
import os
import re
import sqlite3
import time
from string import Template

from .memory import escape_like, normalize_japanese_text, trigram_match_query
from .transcript import local_time, resolve_temporal_window


LOGGER = logging.getLogger(__name__)

# One line, spoken aloud in a summary alongside two others. The extraction
# prompt asks for 50; the store accepts a little slack rather than throwing
# away an otherwise good row for one character.
EVENT_TEXT_MAX_CHARS = 60
# Enough of the exchange to check a row against later, not so much that the
# store becomes a second transcript.
EVENT_SOURCE_MAX_CHARS = 200
EVENT_KINDS = ("life", "profile")
# A date the model returns is only believed inside this window around the
# conversation. Two months covers everything a household states to the day —
# 「先月」, 「来週の火曜」, a holiday being planned — and excludes the failure mode
# that actually happens, which is a model writing last year's year. A row dated
# to a year it has no business being in is unfalsifiable noise; a row that
# falls back to the day it was spoken is at worst imprecise, and the utterance
# it was extracted from is stored beside it either way.
EVENT_DATE_WINDOW_DAYS = 60
# Events dated to a different day than the conversation are anchored mid-day,
# not at midnight: the row is a whole-day claim, and mid-day keeps a day
# boundary query from missing it on a rounding or timezone edge at 00:00.
_NOON = clock_time(hour=12)


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """One dated occurrence, with the exchange it was extracted from.

    ``occurred_at`` is when the thing happened; ``said_at`` is when it was
    said. They differ whenever somebody reports yesterday's dentist visit, and
    keeping both is what lets the robot say *when* as well as *what*.
    """

    id: int
    room_uuid: str
    text: str
    kind: str
    occurred_at: float
    said_at: float
    source_request_id: str
    source_user_text: str
    source_reply_text: str
    created_at: float


# ---------------------------------------------------------------------------
# Deixis
# ---------------------------------------------------------------------------

# The words whose meaning is fixed by *when they were spoken* and which
# therefore cannot survive being stored. The prompt forbids them; this strips
# them anyway, because a prompt is a request and this is a guarantee.
#
# Stripping rather than rejecting is deliberate. 「ユーザーは今日ミーティングが
# あると話した」 is a correct extraction that merely says the day twice — the day
# is already in ``occurred_at`` — so removing the word keeps a good row instead
# of discarding it. Longest spellings first, so 一昨日 cannot be eaten by 昨日.
#
# Bare 「今」 is guarded rather than listed: an unguarded strip would turn
# 「今度」 into 「度」. The lookahead lists the characters that start a longer
# word beginning with 今, including the ones already handled above it.
_DEICTIC = re.compile(
    "一昨日|おととい|おとつい"
    "|明後日|あさって|明日|あした|あす"
    "|昨日|きのう|さくじつ|昨晩|昨夜|ゆうべ"
    "|今日|きょう|本日|今朝|けさ|今晩|今夜"
    "|今週|来週|先週|今月|来月|先月|今年|来年|去年|昨年"
    "|さっき|先ほど|さきほど|ただいま|いま"
    "|今(?![日週月年度回夜朝晩後])"
)
# Punctuation the strip leaves stranded. 「ユーザーは、、ミーティング」 and a line
# that now opens on a comma are both worth one regex to tidy.
_DOUBLED_PUNCTUATION = re.compile(r"[、,]{2,}")
_EDGE_PUNCTUATION = "、。，．,. 　「」\"'"


def strip_deixis(text: str) -> str:
    """Remove every word whose meaning depends on when the line is read.

    Pure and deterministic, so the guarantee is testable without a model. The
    result may be shorter than the model wrote; callers reject what is left
    when nothing survives.
    """

    stripped = _DEICTIC.sub("", text)
    stripped = _DOUBLED_PUNCTUATION.sub("、", stripped)
    return " ".join(stripped.split()).strip(_EDGE_PUNCTUATION)


def contains_deixis(text: str) -> bool:
    """Whether a line still carries a word that would rot in storage."""

    return _DEICTIC.search(text) is not None


# ---------------------------------------------------------------------------
# The extraction prompt
# ---------------------------------------------------------------------------

# Written to REJECT, and measured that way: run over all 228 exchanges in the
# live store it accepted ONE — 「今日meetingがあるから、覚えてくれない」 — and
# rejected 227, with no unparsable output. That ratio is not a defect. The
# store is weather lookups, 「聞こえる？」, questions about the robot itself and
# しりとり moves, and a prompt that read as an invitation to summarise would
# refill this database with exactly the material that made the raw transcript
# unusable. Every negative example below is a real row from that store, quoted.
#
# Three of the reject clauses were added *because* a measured pass wrote a row
# that should not exist: a passing mood (「何をするかわからない」), a garbled
# utterance about the robot re-attributed to the household (「文字を答える時は、
# その、時間がかかるの。」), and an ambiguous fragment. Do not remove them
# without re-running bridge/tools/backfill_events.py over the live store.
# ``string.Template``, not ``str.format``: this prompt is mostly JSON examples,
# and every brace in them would have to be doubled to survive ``format`` — a
# rule that is silently broken by the next edit and shows up as a KeyError at
# runtime rather than in a test. ``$name`` has no such collision.
_EXTRACTION_PROMPT = Template("""\
You keep a household robot's record of WHAT HAPPENED. Below is one completed \
exchange between the household and the robot. Decide whether anything happened \
in it that is worth writing down, and answer with JSON only.

THE RULE, from the household that owns this robot, verbatim:
"facts are what happened. this happened in the convo where you mentioned that \
you liked [x] is a fact. you like [x] is not a fact. e.g I went to the dentist \
today. the fact is that on this particular day i went to the dentist."

So a record is always a DATED OCCURRENCE, never a standing attribute.
  OK  「ユーザーは歯医者に行ったと話した」 — an occurrence, filed under its day
  NO  「ユーザーは歯医者が好き」 — an attribute you inferred
  NO  「りんご」 — a word from a game; not an occurrence at all

REJECT — answer {"event": null} — for every one of these. MOST EXCHANGES ARE \
ONE OF THESE, and rejecting is the correct answer far more often than not:
- Word games. しりとり, なぞなぞ, and every move in them including the words \
themselves. 「こま」→「まくら！次は「ら」だよ」 records NOTHING.
- Greetings, pleasantries, audibility checks: 「聞こえる？」「こんにちは」「元気？」
- Questions about the robot itself — what it can do, where it is, what it is \
thinking, how it feels, what it likes, why it is slow. Its answers about \
itself are invented fresh each time; they are not occurrences.
- Anything the robot looked up: weather, time, temperature, news, trivia. \
「今日の天気は？」→「東京は晴れで29.2度だよ」 records NOTHING — it was true for \
one minute and will be false tomorrow.
- A question the user asked that was not answered, or that the robot asked \
back: 「今日7時にやることがある」→「朝の7時か夜の7時か教えてね」 records NOTHING \
until they say which.
- Hypotheticals, wishes, jokes, guesses: 〜たいな, 〜かも, 〜たら, 〜と思う.
- The user testing, correcting, or asking about the robot's memory.
- A passing mood or state of mind. 「疲れた」「何をするかわからない」「暇だな」 \
are how somebody feels in the moment; they are not occurrences and they are \
not facts about the person.
- An utterance you cannot read with confidence. Everything here arrives \
through speech recognition and a lot of it is garbled — 「エモ恋山、ないが月」, \
「今じわんて記録が」, 「え、もちは今は何時?」. If you are not certain WHO the \
sentence is about or WHAT it claims, reject. In particular, never turn a \
remark about the ROBOT into a statement about the household.
- Anything you would have to guess at, fill in, or infer. Silence is cheap; a \
wrong row is not, because the household will believe it.

RECORD — answer with one line — ONLY when the exchange contains one of these:
1. Something that happened, is happening, or is definitely scheduled in the \
HOUSEHOLD's own life, stated as fact by them.
   「今日ミーティングがある」「昨日歯医者に行った」「試験に受かった」
2. Something specific the household stated about themselves — a name, a place, \
a plan, a relationship, a possession, a preference they declared themselves. \
Record it as the act of saying it, never as a standing truth.
   「ユーザーはサッカーが好きだと話した」 — correct.
   「ユーザーはサッカーが好き」 — wrong shape, reject rather than write this.

WRITING THE LINE
- Plain Japanese, at most 50 characters, one line, of the shape
  「ユーザーは〜と話した」 or 「ユーザーは〜した」.
- Report the utterance. Do not add anything that was not said.
- NEVER use いま・今日・昨日・さっき・今週・来週 or any other word whose meaning \
depends on when it is read. This line will be read months from now, when 「今日」 \
means a different day. The day belongs in the "occurred" field.

ANSWER WITH JSON AND NOTHING ELSE. No prose, no code fence, no explanation.
  {"event": null}
or
  {"event": "ユーザーは歯医者に行ったと話した", "occurred": "2026-08-04", \
"kind": "life"}

"occurred" is the date the thing happened or is scheduled for, as YYYY-MM-DD. \
Today is $today; yesterday was $yesterday; tomorrow is $tomorrow. When no \
day is stated, use today.
"kind" is "life" for rule 1 and "profile" for rule 2.

THE EXCHANGE, spoken at $said:
ユーザー: $user_text
ロボット: $reply_text
""")


def event_extraction_prompt(
    user_text: str, reply_text: str, said_at: float
) -> str:
    """Build the per-exchange extraction request.

    The clock is passed in rather than read, so the same exchange always
    produces the same prompt in a test — and so that a job retried an hour
    later resolves 「今日」 against the day it was *said*, not the day the retry
    happened to run. That is the whole deixis guarantee, and it would be lost
    if this function called ``time.time()``.
    """

    moment = local_time(said_at)
    today = moment.date()
    return _EXTRACTION_PROMPT.substitute(
        today=today.isoformat(),
        yesterday=(today - timedelta(days=1)).isoformat(),
        tomorrow=(today + timedelta(days=1)).isoformat(),
        said=moment.strftime("%Y-%m-%d %H:%M"),
        user_text=_clip(user_text, EVENT_SOURCE_MAX_CHARS),
        reply_text=_clip(reply_text, EVENT_SOURCE_MAX_CHARS),
    )


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractedEvent:
    """A parsed, repaired, still-unstored candidate."""

    text: str
    kind: str
    occurred_at: float


class EventExtractionError(ValueError):
    """The model's output could not be read as an extraction decision."""


def parse_extracted_event(
    output: str, said_at: float
) -> ExtractedEvent | None:
    """Read one extraction decision, tolerating everything but ambiguity.

    ``None`` is the *expected* answer — most exchanges contain no event — and
    is returned both for an explicit ``{"event": null}`` and for output that is
    empty or says nothing usable. Raising is reserved for output that is not
    JSON at all, so that a genuinely broken model shows up as a retry rather
    than as silent under-recording.

    Modelled on :func:`bocco_bridge.reactions.parse_reaction_phrases`: repair
    what can be repaired, drop what cannot, and never let a malformed
    generation reach the store. Everything after the JSON decode is a
    narrowing — a candidate can only be dropped here, never widened.
    """

    payload = _decode(output)
    if payload is None:
        return None
    raw_text = payload.get("event")
    if raw_text is None or raw_text is False:
        return None
    if not isinstance(raw_text, str):
        raise EventExtractionError("event must be a string or null")
    text = " ".join(raw_text.split())
    if not text or text.casefold() in {"null", "none", "なし"}:
        return None

    # The deixis guarantee, enforced rather than requested.
    if contains_deixis(text):
        LOGGER.info("event_extraction_deixis_stripped")
        text = strip_deixis(text)
    text = text.strip("「」\"' 　")
    if len(text) > EVENT_TEXT_MAX_CHARS:
        # Clipping mid-sentence would change what the row claims, which is the
        # one thing a store of checkable rows may never do.
        raise EventExtractionError("event line is too long to store")
    if not normalize_japanese_text(text):
        return None

    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        # A missing or invented kind is not worth losing the row over; "life"
        # is the conservative reading because it makes no claim beyond the
        # occurrence itself.
        kind = "life"
    return ExtractedEvent(
        text=text,
        kind=kind,
        occurred_at=_resolve_occurred_at(payload.get("occurred"), said_at),
    )


def _decode(output: str) -> dict[str, object] | None:
    """Decode a JSON object, forgiving the wrappers models add unasked.

    A fenced block and a sentence of preamble are the two failure modes seen in
    practice, and both are recoverable by finding the outermost braces. Nothing
    else is guessed at.
    """

    if not isinstance(output, str):
        raise EventExtractionError("extraction output must be text")
    text = output.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise EventExtractionError("extraction output must be JSON") from None
        try:
            decoded = json.loads(text[start : end + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise EventExtractionError("extraction output must be JSON") from None
    if isinstance(decoded, list):
        # One event per exchange is the contract; a list is tolerated and its
        # first usable object taken, never all of them.
        decoded = next(
            (item for item in decoded if isinstance(item, dict)), None
        )
        if decoded is None:
            return None
    if not isinstance(decoded, dict):
        raise EventExtractionError("extraction output must be a JSON object")
    return decoded


def _resolve_occurred_at(raw: object, said_at: float) -> float:
    """Believe the model's date only when it is near the conversation.

    Noon local time, not midnight: the row is a whole-day claim, and anchoring
    it mid-day keeps a day-boundary query from missing it because of a
    rounding or timezone edge at exactly 00:00.
    """

    said = local_time(said_at)
    if not isinstance(raw, str):
        return said_at
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match is None:
        return said_at
    try:
        occurred = date(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        )
    except ValueError:
        return said_at
    if abs((occurred - said.date()).days) > EVENT_DATE_WINDOW_DAYS:
        LOGGER.info("event_extraction_date_out_of_range")
        return said_at
    if occurred == said.date():
        # Same day: keep the real clock time, which is finer provenance than
        # noon and costs nothing.
        return said_at
    # Localized from a naive wall clock rather than by adding a timedelta to an
    # aware value, for the reason the transcript spells out: a fixed offset
    # carried across a DST edge puts "noon" in the wrong day.
    return (
        datetime.combine(occurred, _NOON).astimezone().timestamp()
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def event_day_label(occurred_at: float, now: float) -> str:
    """The day an event happened, written as a date and never as a relative.

    The transcript renders 「昨日 16:24」 because it is quoting a conversation
    that a person is expected to place relative to now. An event row is the
    opposite: it exists to be true whenever it is read, so it always carries an
    absolute date. The year is dropped only within the current year, where it
    carries no information.
    """

    moment = local_time(occurred_at)
    today = local_time(now)
    if moment.year == today.year:
        return f"{moment.month}月{moment.day}日"
    return f"{moment.year}年{moment.month}月{moment.day}日"


def render_event_line(event: RecordedEvent, now: float) -> str:
    """One rendered row, with the reporting date attached when it differs.

    「8月4日：ユーザーは歯医者に行ったと話した（8月5日に聞いた）」 — the household
    asked for provenance, and the day something was reported is half of it.
    """

    line = f"・{event_day_label(event.occurred_at, now)}：{event.text}"
    said_day = local_time(event.said_at).date()
    if local_time(event.occurred_at).date() != said_day:
        line += f"（{event_day_label(event.said_at, now)}に聞いた）"
    return line


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class EventMemory:
    """A lazy-initialized async facade over a private event database.

    Constructing it touches no disk, exactly as :class:`MotionRepertoire` and
    :class:`ConversationTranscript` do not, so a bridge with the feature off
    never creates the file.
    """

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
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_uuid TEXT NOT NULL,
                    text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    said_at REAL NOT NULL,
                    source_request_id TEXT NOT NULL UNIQUE,
                    source_user_text TEXT NOT NULL,
                    source_reply_text TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                -- The temporal query 「昨日何した？」 is a half-open range scan
                -- over this index with a LIMIT, so it costs the same on ten
                -- rows and on ten thousand.
                CREATE INDEX IF NOT EXISTS events_room_occurred
                ON events(room_uuid, occurred_at DESC, id DESC);

                -- Same-day duplicate suppression looks the text up directly.
                CREATE INDEX IF NOT EXISTS events_room_text
                ON events(room_uuid, normalized_text);

                CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                    normalized_text,
                    content='events',
                    content_rowid='id',
                    tokenize='trigram'
                );

                CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                    INSERT INTO events_fts(rowid, normalized_text)
                    VALUES (new.id, new.normalized_text);
                END;

                CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
                    INSERT INTO events_fts(events_fts, rowid, normalized_text)
                    VALUES ('delete', old.id, old.normalized_text);
                END;

                CREATE TRIGGER IF NOT EXISTS events_au
                AFTER UPDATE OF normalized_text ON events BEGIN
                    INSERT INTO events_fts(events_fts, rowid, normalized_text)
                    VALUES ('delete', old.id, old.normalized_text);
                    INSERT INTO events_fts(rowid, normalized_text)
                    VALUES (new.id, new.normalized_text);
                END;
                """
            )
            # Triggers keep events_fts in step, so a rebuild on every startup
            # buys nothing and costs more the more the household remembers.
            # Rebuild only when the index is empty while events are not, which
            # is what an interrupted migration or a restored events table looks
            # like.
            stale = connection.execute(
                "SELECT (SELECT COUNT(*) FROM events) > 0"
                " AND (SELECT COUNT(*) FROM events_fts) = 0"
            ).fetchone()[0]
            if stale:
                connection.execute(
                    "INSERT INTO events_fts(events_fts) VALUES ('rebuild')"
                )
        os.chmod(self.path, 0o600)

    async def record(
        self,
        room_uuid: str,
        source_request_id: str,
        candidate: ExtractedEvent,
        *,
        said_at: float,
        source_user_text: str = "",
        source_reply_text: str = "",
        created_at: float | None = None,
        retention: int = 0,
    ) -> RecordedEvent | None:
        await self.initialize()
        return await asyncio.to_thread(
            self._record_sync,
            room_uuid,
            source_request_id,
            candidate,
            said_at,
            source_user_text,
            source_reply_text,
            created_at,
            retention,
        )

    def _record_sync(
        self,
        room_uuid: str,
        source_request_id: str,
        candidate: ExtractedEvent,
        said_at: float,
        source_user_text: str,
        source_reply_text: str,
        created_at: float | None,
        retention: int,
    ) -> RecordedEvent | None:
        text = " ".join(str(candidate.text).split())
        if len(text) > EVENT_TEXT_MAX_CHARS:
            # parse_extracted_event rejects an over-long line rather than
            # clipping it, because a clipped event claims something the model
            # did not say. Clipping here would reintroduce exactly that through
            # the back door, for callers that build a candidate directly.
            LOGGER.warning(
                "event_text_too_long source_request_id=%s chars=%d limit=%d",
                source_request_id,
                len(text),
                EVENT_TEXT_MAX_CHARS,
            )
            return None
        normalized = normalize_japanese_text(text)
        if not room_uuid or not source_request_id or not normalized:
            return None
        current = time.time() if created_at is None else created_at
        occurred_day = local_time(candidate.occurred_at).date().isoformat()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM events WHERE source_request_id = ?",
                (source_request_id,),
            ).fetchone()
            if existing is not None:
                # Idempotent per exchange: a replayed job stores one row.
                return _row_to_event(existing)
            # Same sentence, same day, already there. A household mentions the
            # same meeting twice in five minutes and the second mention is not
            # a second meeting — but the *same* sentence a week later is a
            # genuinely different occurrence, so the day is part of the key.
            duplicate = connection.execute(
                """
                SELECT id FROM events
                WHERE room_uuid = ? AND normalized_text = ?
                    AND date(occurred_at, 'unixepoch', 'localtime') = ?
                LIMIT 1
                """,
                (room_uuid, normalized, occurred_day),
            ).fetchone()
            if duplicate is not None:
                return None
            cursor = connection.execute(
                """
                INSERT INTO events (
                    room_uuid, text, normalized_text, kind, occurred_at,
                    said_at, source_request_id, source_user_text,
                    source_reply_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_uuid,
                    text,
                    normalized,
                    candidate.kind if candidate.kind in EVENT_KINDS else "life",
                    float(candidate.occurred_at),
                    float(said_at),
                    source_request_id,
                    _clip(source_user_text, EVENT_SOURCE_MAX_CHARS),
                    _clip(source_reply_text, EVENT_SOURCE_MAX_CHARS),
                    current,
                ),
            )
            event_id = int(cursor.lastrowid)
            if retention > 0:
                # Oldest-first, by the day it happened: an event store that
                # forgets has to forget the distant past, not the row that
                # happened to be written last.
                connection.execute(
                    """
                    DELETE FROM events
                    WHERE room_uuid = ? AND id NOT IN (
                        SELECT id FROM events WHERE room_uuid = ?
                        ORDER BY occurred_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (room_uuid, room_uuid, retention),
                )
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            return None if row is None else _row_to_event(row)

    async def recall(
        self,
        room_uuid: str,
        query_text: str,
        *,
        now: float,
        limit: int = 3,
        max_chars: int = 400,
    ) -> tuple[RecordedEvent, ...]:
        await self.initialize()
        return await asyncio.to_thread(
            self._recall_sync, room_uuid, query_text, now, limit, max_chars
        )

    def _recall_sync(
        self,
        room_uuid: str,
        query_text: str,
        now: float,
        limit: int,
        max_chars: int,
    ) -> tuple[RecordedEvent, ...]:
        """Retrieve on the temporal axis first, then the lexical one.

        The temporal branch is the reason this store exists. 「昨日何した？」 is
        not a ranking problem — it is a range query, and the range is exact
        because ``occurred_at`` was resolved when the words still meant
        something. It runs first and its rows are never displaced by the
        keyword ranking.

        Both branches are index lookups with a ``LIMIT``, so neither one grows
        with the size of the store. That is a hard requirement: reply-path
        retrieval is currently ~1.6 ms and nothing here may make it a function
        of how long the household has owned the robot.
        """

        if limit <= 0 or max_chars <= 0 or not room_uuid:
            return ()
        selected: list[RecordedEvent] = []
        seen: set[int] = set()
        with self._connection() as connection:
            window = resolve_temporal_window(query_text, now)
            if window is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM events
                    WHERE room_uuid = ? AND occurred_at >= ? AND occurred_at < ?
                    ORDER BY occurred_at DESC, id DESC LIMIT ?
                    """,
                    (room_uuid, window.start, window.end, limit),
                ).fetchall()
                for row in rows:
                    event = _row_to_event(row)
                    seen.add(event.id)
                    selected.append(event)
            if len(selected) < limit:
                # The residue, not the raw utterance, for the reason the
                # transcript searches the residue: 「昨日」 is also the word the
                # robot's own rows use, so a keyword search for it ranks the
                # date words above the topic.
                residue = window.residue if window is not None else query_text
                for row in self._lexical_rows(
                    connection, room_uuid, residue, limit - len(selected) + len(seen)
                ):
                    event = _row_to_event(row)
                    if event.id in seen:
                        continue
                    seen.add(event.id)
                    selected.append(event)
                    if len(selected) >= limit:
                        break
        budgeted: list[RecordedEvent] = []
        used = 0
        for event in selected:
            if used + len(event.text) > max_chars:
                continue
            budgeted.append(event)
            used += len(event.text)
        # Chronological once chosen: a list of dated rows read as a timeline.
        return tuple(sorted(budgeted, key=lambda item: item.occurred_at))

    @staticmethod
    def _lexical_rows(
        connection: sqlite3.Connection,
        room_uuid: str,
        query_text: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        normalized = normalize_japanese_text(query_text)
        if not normalized or limit <= 0:
            return []
        match = trigram_match_query(normalized)
        if match is None:
            return list(
                connection.execute(
                    """
                    SELECT * FROM events
                    WHERE room_uuid = ? AND normalized_text LIKE ? ESCAPE '\\'
                    ORDER BY occurred_at DESC, id DESC LIMIT ?
                    """,
                    (room_uuid, f"%{escape_like(normalized)}%", limit),
                )
            )
        return list(
            connection.execute(
                """
                SELECT events.* FROM events_fts
                JOIN events ON events.id = events_fts.rowid
                WHERE events_fts MATCH ? AND events.room_uuid = ?
                ORDER BY bm25(events_fts), events.occurred_at DESC
                LIMIT ?
                """,
                (match, room_uuid, limit),
            )
        )

    async def list_recent(
        self, room_uuid: str, *, limit: int = 10
    ) -> tuple[RecordedEvent, ...]:
        await self.initialize()
        return await asyncio.to_thread(self._list_recent_sync, room_uuid, limit)

    def _list_recent_sync(
        self, room_uuid: str, limit: int
    ) -> tuple[RecordedEvent, ...]:
        if limit <= 0:
            return ()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events WHERE room_uuid = ?
                ORDER BY occurred_at DESC, id DESC LIMIT ?
                """,
                (room_uuid, limit),
            ).fetchall()
            return tuple(_row_to_event(row) for row in rows)

    async def forget(self, room_uuid: str, keyword: str) -> int:
        """Delete matching events. Really delete them.

        ``HouseholdMemory.forget`` only sets ``active = 0``, which a security
        review flagged: somebody removing something sensitive has not removed
        it. That is defensible for a dictated fact, where the row is the user's
        own words and the deactivation is a correction rather than a redaction.

        It is NOT defensible here. Every row in this store was written by a
        model from something the household said in passing — they never chose
        to store it — and a row also carries the verbatim utterance it came
        from. So "forget that" has to mean the bytes leave the file, and the
        FTS delete trigger takes the index copy with them. There is no audit
        value in retaining a machine-written row its subject has rejected.
        """

        await self.initialize()
        return await asyncio.to_thread(self._forget_sync, room_uuid, keyword)

    def _forget_sync(self, room_uuid: str, keyword: str) -> int:
        normalized = normalize_japanese_text(keyword)
        if not room_uuid or not normalized:
            return 0
        pattern = f"%{escape_like(normalized)}%"
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM events
                WHERE room_uuid = ? AND normalized_text LIKE ? ESCAPE '\\'
                """,
                (room_uuid, pattern),
            )
            return cursor.rowcount

    async def has_source(self, source_request_id: str) -> bool:
        """Whether this exchange already produced a row. Resumability, cheaply.

        Only a positive answer is meaningful: an exchange the extractor decided
        held no event leaves no row either, and is indistinguishable here from
        one never looked at. That is the right trade for a backfill — the cost
        of re-asking about a rejected exchange is one model call, whereas a
        table of negative verdicts is a second store to keep correct.
        """

        await self.initialize()
        return await asyncio.to_thread(self._has_source_sync, source_request_id)

    def _has_source_sync(self, source_request_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM events WHERE source_request_id = ? LIMIT 1",
                (source_request_id,),
            ).fetchone()
            return row is not None

    async def count(self, room_uuid: str) -> int:
        await self.initialize()
        return await asyncio.to_thread(self._count_sync, room_uuid)

    def _count_sync(self, room_uuid: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT count(*) AS total FROM events WHERE room_uuid = ?",
                (room_uuid,),
            ).fetchone()
            return 0 if row is None else int(row["total"])


def _row_to_event(row: sqlite3.Row) -> RecordedEvent:
    return RecordedEvent(
        id=int(row["id"]),
        room_uuid=str(row["room_uuid"]),
        text=str(row["text"]),
        kind=str(row["kind"]),
        occurred_at=float(row["occurred_at"]),
        said_at=float(row["said_at"]),
        source_request_id=str(row["source_request_id"]),
        source_user_text=str(row["source_user_text"]),
        source_reply_text=str(row["source_reply_text"]),
        created_at=float(row["created_at"]),
    )


def _clip(text: object, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: max(limit - 1, 1)] + "…"
