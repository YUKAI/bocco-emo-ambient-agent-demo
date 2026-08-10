"""Semantic retrieval support: a deadline-bounded embedding client and the
brute-force vector search the transcript ranks with.

BM25 over the FTS5 trigram index matches spelling. It cannot match meaning,
and the live store is full of exchanges where meaning is all there is:
「君の好きな食べ物何?」/「あったかいスープが好きだよ」 shares not one character
with 「ごはんの話したっけ」, and a large share of the turns are five-character
pleasantries with no lexical signal at all. Embeddings close exactly that gap
and are bad at what BM25 is good at — rare exact tokens, 大阪, names, numbers —
so the two are fused rather than swapped.

Two rules shape everything here.

*The reply latency is not negotiable.* Two days of work took the end-to-end
reply to 2-4 seconds and nothing in this module may give any of it back. So
the query embedding is a network call with a hard deadline, a circuit breaker
behind it, and exactly one failure mode: return ``None`` and let the caller
retrieve the way it did before this file existed. Not a raised exception, not
a retry, not a longer wait.

*The bridge stays lean.* The venv is 43 MB and two third-party packages, and
that is a property worth keeping: the model runs in its own service over
loopback, exactly like ``hermes-api``, and the similarity search here is
stdlib arithmetic. Measured on 500x384 float32 vectors, one full sweep costs
~2.2 ms on an M5 and an estimated 8-11 ms on the Pi's Cortex-A76 — 0.3% of a
reply, paid inside a worker thread, never on the event loop. numpy would make
that 0.014 ms; buying 8 ms of a 3000 ms reply with a 20 MB dependency is not a
trade this bridge makes.
"""

from __future__ import annotations

import asyncio
import logging
import math
import operator
import sys
from array import array
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence
import time

import httpx


LOGGER = logging.getLogger(__name__)

# float32, native order. Chosen over int8 on measurement, not on principle:
# int8 halves nothing that matters (500 vectors is 768 KB either way), scans
# *slower* in CPython because every row needs a dequantizing correction term
# (2.41 ms vs 2.15 ms per sweep), and costs recall — 0.985 against an exact
# float ranking over 300 random queries. Paying accuracy for a slowdown is not
# a trade; float32 stays.
_VECTOR_TYPECODE = "f"
_VECTOR_ITEM_BYTES = array(_VECTOR_TYPECODE).itemsize

# Reciprocal Rank Fusion's damping constant, from Cormack et al. 2009 and
# unchanged since. It is what makes fusion work on ranks alone: BM25 scores and
# cosine similarities live on incomparable scales, and any attempt to normalize
# them needs a corpus-wide calibration that a 500-row store cannot supply.
RRF_K = 60


class EmbeddingUnavailable(Exception):
    """Internal signal that a request could not be served. Never escapes."""


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Everything the client needs, resolved by the caller as usual here.

    ``query_prefix``/``passage_prefix`` are configuration rather than constants
    because they are the *model's* convention, not ours: E5 wants ``query: ``
    and ``passage: ``, Ruri wants 検索クエリ:／検索文書:, and a model that wants
    neither takes empty strings. Swapping models is then an env change plus a
    re-backfill, never a code change.
    """

    base_url: str = "http://127.0.0.1:8646"
    model: str = "multilingual-e5-small"
    dims: int = 384
    # 120 ms. Chosen from the encode cost, not from taste: a short Japanese
    # utterance is ~12-16 XLM-R tokens and multilingual-e5-small has only 21M
    # non-embedding parameters, which models to 20-45 ms on four A76 cores,
    # plus 1-3 ms of loopback HTTP. 120 ms is roughly three times the upper end,
    # so it never fires when the service is healthy, and it is 3-6% of a
    # 2000-4000 ms reply in the worst case where it does. It is paid at most
    # `breaker_failures` times before the breaker opens and stops paying it.
    query_deadline_seconds: float = 0.12
    # The write path is off the hot path entirely — a background task started
    # after the reply is already spoken — so it can afford to wait for a
    # service that is merely busy rather than dropping the vector.
    write_deadline_seconds: float = 5.0
    breaker_failures: int = 3
    breaker_cooldown_seconds: float = 60.0
    query_prefix: str = "query: "
    passage_prefix: str = "passage: "

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("embedding base_url must be an HTTP URL")
        if not self.model.strip():
            raise ValueError("embedding model must not be empty")
        if self.dims < 1:
            raise ValueError("embedding dims must be positive")
        if self.query_deadline_seconds <= 0 or self.write_deadline_seconds <= 0:
            raise ValueError("embedding deadlines must be positive")
        if self.breaker_failures < 1:
            raise ValueError("breaker_failures must be at least 1")
        if self.breaker_cooldown_seconds < 0:
            raise ValueError("breaker_cooldown_seconds must be non-negative")


class EmbeddingClient:
    """Async client for a loopback OpenAI-shaped ``/v1/embeddings`` service.

    Every public method answers ``None`` instead of raising. That is the whole
    contract: the caller has one branch for "no vector", and service down,
    service slow, model not loaded, wrong dimension, truncated JSON and
    unhandled exception all take it.
    """

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._now = now
        # Circuit breaker. Without it a service that is down but still
        # listening — or one wedged behind a model reload — would charge the
        # deadline to every single reply. With it the cost of an outage is
        # `breaker_failures` deadlines per cooldown window, which amortizes to
        # under 6 ms per reply at the defaults and to nothing as the outage
        # lengthens.
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._open_logged = False

    @property
    def config(self) -> EmbeddingConfig:
        return self._config

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def embed_query(self, text: str) -> tuple[float, ...] | None:
        """Embed one utterance under the hot-path deadline, or answer ``None``."""

        vectors = await self._embed(
            (text,),
            prefix=self._config.query_prefix,
            deadline=self._config.query_deadline_seconds,
        )
        return None if vectors is None else vectors[0]

    async def embed_passages(
        self, texts: Sequence[str]
    ) -> tuple[tuple[float, ...], ...] | None:
        """Embed stored turns under the generous background deadline."""

        if not texts:
            return ()
        return await self._embed(
            tuple(texts),
            prefix=self._config.passage_prefix,
            deadline=self._config.write_deadline_seconds,
        )

    async def _embed(
        self, texts: tuple[str, ...], *, prefix: str, deadline: float
    ) -> tuple[tuple[float, ...], ...] | None:
        if not texts or any(not text.strip() for text in texts):
            return None
        if self._breaker_open():
            return None
        try:
            # wait_for is the load-bearing line. httpx's own timeout covers
            # connect/read/write separately and a pathological server can stay
            # inside all of them while the wall clock runs; one outer deadline
            # cannot be talked out of firing.
            vectors = await asyncio.wait_for(
                self._request(texts, prefix, deadline), deadline
            )
        except (asyncio.TimeoutError, EmbeddingUnavailable) as exc:
            self._record_failure(type(exc).__name__)
            return None
        except asyncio.CancelledError:
            # Shutdown, not a service fault: do not hold it against the breaker.
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._record_failure(type(exc).__name__)
            return None
        self._record_success()
        return vectors

    async def _request(
        self, texts: tuple[str, ...], prefix: str, deadline: float
    ) -> tuple[tuple[float, ...], ...]:
        client = self._client()
        try:
            response = await client.post(
                f"{self._config.base_url.rstrip('/')}/v1/embeddings",
                json={
                    "model": self._config.model,
                    "input": [f"{prefix}{text}" for text in texts],
                },
                timeout=deadline,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable("embedding request failed") from exc
        if not response.is_success:
            raise EmbeddingUnavailable(
                f"embedding service returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingUnavailable("embedding response was not JSON") from exc
        return self._parse(payload, len(texts))

    def _parse(self, payload: object, expected: int) -> tuple[tuple[float, ...], ...]:
        """Trust nothing: a malformed payload is an outage, not a crash."""

        if not isinstance(payload, dict):
            raise EmbeddingUnavailable("embedding response was not an object")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected:
            raise EmbeddingUnavailable("embedding response had the wrong shape")
        # The OpenAI shape carries an explicit index and does not promise
        # order; honour the index when it is present so a batched backfill
        # cannot silently attach vectors to the wrong turns.
        ordered: list[tuple[float, ...] | None] = [None] * expected
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                raise EmbeddingUnavailable("embedding entry was not an object")
            index = item.get("index", position)
            if not isinstance(index, int) or not 0 <= index < expected:
                index = position
            values = item.get("embedding")
            if not isinstance(values, list) or len(values) != self._config.dims:
                raise EmbeddingUnavailable("embedding had an unexpected dimension")
            try:
                vector = normalize([float(value) for value in values])
            except (TypeError, ValueError) as exc:
                raise EmbeddingUnavailable("embedding was not numeric") from exc
            if vector is None:
                raise EmbeddingUnavailable("embedding had zero magnitude")
            ordered[index] = vector
        if any(vector is None for vector in ordered):
            raise EmbeddingUnavailable("embedding response was missing an entry")
        return tuple(vector for vector in ordered if vector is not None)

    def _client(self) -> httpx.AsyncClient:
        # Built on first use, never in __init__: a bridge with the feature off
        # must construct this object and touch nothing.
        if self._http_client is None:
            self._http_client = httpx.AsyncClient()
        return self._http_client

    def _breaker_open(self) -> bool:
        if self._open_until <= 0.0:
            return False
        if self._now() < self._open_until:
            return True
        # Cooldown elapsed: let exactly one request through to probe. The
        # failure counter is left alone, so a still-dead service re-opens the
        # breaker on that single probe rather than after another three.
        self._open_until = 0.0
        return False

    def _record_failure(self, error_type: str) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures < self._config.breaker_failures:
            return
        self._open_until = self._now() + self._config.breaker_cooldown_seconds
        if not self._open_logged:
            # Once per outage, not once per reply: retrieval silently degrading
            # to BM25 is worth one line, not a log flood on the hot path.
            LOGGER.warning(
                "embedding_service_unavailable error_type=%s cooldown_seconds=%.1f",
                error_type,
                self._config.breaker_cooldown_seconds,
            )
            self._open_logged = True

    def _record_success(self) -> None:
        if self._open_logged:
            LOGGER.info("embedding_service_recovered")
            self._open_logged = False
        self._consecutive_failures = 0
        self._open_until = 0.0


def normalize(values: Sequence[float]) -> tuple[float, ...] | None:
    """L2-normalize so a dot product *is* the cosine.

    Done here as well as in the service because the ranking sweep below has no
    per-row division to spare and no way to notice an un-normalized row. A zero
    or non-finite vector answers ``None`` — it can only come from a broken
    service, and letting it through would put a NaN into every comparison.
    """

    total = 0.0
    for value in values:
        if not math.isfinite(value):
            return None
        total += value * value
    if total <= 0.0:
        return None
    scale = 1.0 / math.sqrt(total)
    return tuple(value * scale for value in values)


def pack_vector(values: Sequence[float]) -> bytes:
    """Serialize a vector for the ``turn_vectors.vector`` BLOB."""

    packed = array(_VECTOR_TYPECODE, values)
    if sys.byteorder != "little":
        # The database is little-endian by construction so that a transcript
        # copied off the Pi still ranks correctly on a developer machine.
        packed.byteswap()
    return packed.tobytes()


def unpack_vector(blob: bytes) -> array | None:
    """Deserialize a stored vector, or ``None`` if the BLOB is not one."""

    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    if len(blob) % _VECTOR_ITEM_BYTES:
        return None
    values = array(_VECTOR_TYPECODE)
    values.frombytes(bytes(blob))
    if sys.byteorder != "little":
        values.byteswap()
    return values


def rank_by_similarity(
    query: Sequence[float],
    rows: Iterable[tuple[int, bytes]],
    *,
    limit: int,
    min_similarity: float = 0.0,
) -> tuple[int, ...]:
    """Brute-force cosine top-k over the candidate rows, best first.

    Brute force is not a compromise at this scale, it is the correct answer:
    retention caps the store at 500 turns, one sweep is 192k multiply-adds, and
    an ANN index would add a build step, a staleness question and a recall
    cliff in exchange for microseconds nobody can perceive. The whole call runs
    inside :func:`asyncio.to_thread`, so the event loop never sees it.

    Deterministic by construction: ties break on the turn id, descending, so
    two turns with byte-identical vectors always resolve the same way and the
    fusion downstream is reproducible in a test.
    """

    if limit <= 0:
        return ()
    dims = len(query)
    if dims == 0:
        return ()
    scored: list[tuple[float, int]] = []
    # Bound locally: this loop runs 500 times per reply and every attribute
    # lookup inside it is paid 500 times over.
    multiply = operator.mul
    total = sum
    unpack = unpack_vector
    for turn_id, blob in rows:
        values = unpack(blob)
        if values is None or len(values) != dims:
            # A row embedded by a different model or a half-written BLOB. Skip
            # it rather than compare across incompatible spaces.
            continue
        # sum(map(mul, ...)) rather than an indexed loop: both are O(dims), but
        # the former runs the multiply-accumulate entirely in C and measured
        # ~3x faster on the 500x384 sweep, which is most of the difference
        # between "invisible" and "worth arguing about" on the Pi.
        similarity = total(map(multiply, query, values))
        if similarity < min_similarity:
            continue
        scored.append((-similarity, -turn_id))
    scored.sort()
    return tuple(-turn_id for _score, turn_id in scored[:limit])


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]], *, limit: int, k: int = RRF_K
) -> tuple[int, ...]:
    """Fuse ranked id lists into one, best first.

    RRF because the two rankings have no common scale: BM25 returns an
    unbounded negative log-odds, cosine returns a number in [-1, 1] whose
    useful range for a given model is a narrow band nobody has calibrated on
    this corpus. RRF reads only positions, so it needs no normalization, no
    training data, and no per-model tuning — and it degrades gracefully, since
    a list that comes back empty simply contributes nothing.

    Ties break on id descending, so the fused order is a pure function of the
    inputs. That matters: a non-deterministic prompt is a non-reproducible bug.
    """

    if limit <= 0:
        return ()
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, turn_id in enumerate(ranking):
            scores[turn_id] = scores.get(turn_id, 0.0) + 1.0 / (k + position + 1)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], -item[0]))
    return tuple(turn_id for turn_id, _score in ordered[:limit])
