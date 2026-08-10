# Semantic retrieval for the conversation transcript

Retrieval memory ranks past exchanges with BM25 over an FTS5 trigram index.
That matches spelling. Three failures in the live 187-turn store show what it
cannot do:

- stored 「君の好きな食べ物何?」→「あったかいスープが好きだよ」; a later
  「ごはんの話したっけ」 shares **zero characters** and retrieves nothing.
- a large share of turns are 5-10 character exchanges (「元気？」→「元気だよ。」)
  with almost no lexical signal, effectively unretrievable.
- 「エモは何ができるの」 vs 「君にできることは？」 — the same question, no overlap.

Embeddings fix all three. They do not replace BM25, which stays better at rare
exact tokens — 大阪, スープ, names, numbers — where a dense model blurs the very
feature that made the match. The two rankings are fused.

**The feature is off by default and must stay cheap to leave off.** Reply
latency is 2-4 seconds and was won by two days of work on prompt slimming,
tool-list trimming, prompt caching, fast routes and sentence streaming. A
latency regression here would be a failed feature regardless of retrieval
quality, so the design makes one structurally impossible rather than merely
unlikely.

## The latency guarantee

There is exactly one place in a reply where the bridge waits on anything new:
`EventProcessor._semantic_query`, embedding the utterance. It is bounded four
ways, and every one of them lands on the same fallback — `None`, which puts
`ConversationTranscript.context` back on the byte-for-byte BM25 path that
shipped before this existed.

| Bound | Mechanism | Worst case added |
|---|---|---|
| Slow service | `asyncio.wait_for(..., 0.12)` around the request | 120 ms, once |
| Repeatedly slow or dead | circuit breaker: 3 misses opens it for 60 s | 360 ms per minute, then 0 |
| Unreachable, HTTP error, malformed JSON, wrong dimension | `EmbeddingClient` returns `None`; it never raises | 0 |
| Anything unforeseen | `except Exception` in `_semantic_query`, on top of the client's own | 0 |

The write path is not on the reply path at all: `_schedule_turn_embedding`
starts a detached task *after* the reply has been spoken, the same discipline
`_record_turn` already follows, and a turn whose embedding fails is simply
lexical-only until the backfill tool picks it up.

Nothing added here runs on the event loop. The HTTP call is async; the cosine
sweep runs inside `asyncio.to_thread`, in the same worker hop the existing
SQLite queries already pay.

### Why 120 ms

The deadline is chosen from the encode cost, not from taste. A short Japanese
utterance is ~12-16 XLM-RoBERTa tokens; `multilingual-e5-small` has 118M
parameters of which only ~21M are transformer weights (the rest is the 250k
vocabulary embedding table, which a forward pass reads one row of per token).
One encode is therefore ~1 GFLOP of compute against ~130 MB of weight traffic,
which on four Cortex-A76 cores at 2.4 GHz and ~17 GB/s models to **20-45 ms**,
plus 1-3 ms of loopback HTTP.

**This is an estimate, not a measurement — no Pi 5 was available while writing
this.** Measure it before trusting it:

```
PYTHONPATH=bridge/src python3 bridge/tools/bench_retrieval.py --service
```

That prints p50/p95 of encode-plus-HTTP and the headroom against 120 ms. If p95
lands above ~60 ms, either drop to `--threads 4` or reconsider the model; do
not raise the deadline, which is the one knob that converts a slow service into
a slow reply. `BridgeConfig` refuses to start above 0.5 s for that reason.

## Serving stack

**llama.cpp `llama-server --embeddings`, `intfloat/multilingual-e5-small` as
Q8_0 GGUF, on loopback at 127.0.0.1:8646.**

Why a separate process at all: the bridge venv is 43 MB with exactly two
third-party packages (aiohttp, httpx) and that leanness is deliberate.
`sentence-transformers` would pull torch and make it ~2.5 GB. A separate
service keeps the bridge's dependency tree *completely* untouched — this change
adds zero packages — isolates model memory and load from the reply path, and
follows the pattern already in use for `hermes-api`.

Why llama.cpp over ONNX Runtime: llama.cpp is one static binary and one `.gguf`
file with no Python anywhere, and it already speaks an OpenAI-shaped
`/v1/embeddings`. ONNX Runtime would need a venv with `onnxruntime` and
`tokenizers` (both native wheels), plus an HTTP server of our own — three more
things to keep patched for no measured benefit.

Why this model: 384 dimensions, genuinely multilingual (trained on mC4
including Japanese), and small enough that the forward pass is bounded by
memory traffic rather than arithmetic. XLM-RoBERTa support landed in llama.cpp
in August 2024, so the embedding path is well-trodden.

Why Q8_0 rather than Q4_K: the vocabulary table dominates the file, so Q4_K
saves 11 MB of 132 MB while measuring 0.990 cosine agreement with the reference
weights against Q8_0's 0.9999. Eleven megabytes is not worth a percent of
retrieval quality on a machine with 14 GB free.

The upgrade path is configuration, not code: `cl-nagoya/ruri-v3-30m` is a
Japanese-specialist alternative at 256 dimensions. The model name, dimension
and the model's own `query:`/`passage:` prefix convention all live in config,
and every stored vector is stamped with the model that produced it — so a swap
is an env change plus a re-backfill, and *cannot* silently rank new queries
against old vectors, because the sweep filters on the name and finds nothing
until the backfill has run.

## Similarity search

Retention caps the store at 500 turns. 500 × 384 float32 is 768 KB and one
sweep is 192k multiply-adds. **No FAISS, no vector database, no ANN index** —
brute force is not a compromise at this scale, it is the correct answer, and it
stays correct as the store fills.

The question was how to do 192k multiply-adds without spending the latency
budget. Measured on this repository, 500 × 384, Apple M5:

| Approach | p50 per sweep | recall@3 vs exact float |
|---|---|---|
| pure-Python float32 dot | **2.15 ms** | 1.000 (exact) |
| int8 quantized, exact scan | 2.41 ms | 0.985 |
| binary sign prefilter (32) + int8 rescore | 0.28 ms | 0.61 |
| binary sign prefilter (128) + int8 rescore | ~0.8 ms | 0.87 |
| numpy float32 matmul | 0.014 ms | 1.000 |

**Chosen: pure-Python float32.** The reasoning:

- *int8 was evaluated and rejected on its own numbers.* It is **slower**, not
  faster — every row needs a dequantizing correction term, and unpacking
  `bytes` into Python ints costs more than unpacking floats. Its only win is
  4× storage, and 768 KB → 192 KB is not a win worth 1.5% of recall.
- *Binary quantization prefilters were rejected on recall.* Even at a rescore
  width of 128 — a quarter of the whole store — recall@3 is 0.87. The brief
  was "only if the recall cost is negligible"; it is not.
- *numpy was rejected on the trade, not the number.* It is 150× faster in
  relative terms and irrelevant in absolute terms: the sweep is ~3 ms of a
  ~3000 ms reply, inside a worker thread. Buying that back costs a 20 MB
  dependency in a venv whose two-package leanness is a stated design property.
  If that trade is ever wanted, `rank_by_similarity` is the only function that
  would change.

Estimated cost on the Pi: **~10-15 ms**, scaling the M5 measurement by 3.5-5×
for CPython on a Cortex-A76 at 2.4 GHz. Flagged as an estimate; `--local`
measures it.

Storage lives beside the turns in `turn_vectors`, one row per turn, with an
`AFTER DELETE ON turns` trigger so retention takes vectors with it rather than
leaving them to accumulate behind a store that is supposed to be bounded.

## Measured: the retrieval path, off vs on

`bridge/tools/bench_retrieval.py --local`, 500 stored turns, 300 queries per
shape, Apple M5, milliseconds. Excludes the embedding round trip, which is
bounded separately by the deadline above.

| Query shape | OFF p50 | OFF p95 | ON p50 | ON p95 | Δ p50 | Δ p95 |
|---|---|---|---|---|---|---|
| plain topical | 0.598 | 0.750 | 3.592 | 3.757 | +2.994 | +3.007 |
| temporal + topic | 0.651 | 0.740 | 3.690 | 3.847 | +3.040 | +3.107 |
| temporal only | 0.436 | 0.486 | 3.472 | 3.593 | +3.035 | +3.108 |

The cost is flat at ~3 ms whatever the query shape, which is the sweep and
nothing else: 500 rows are scanned every time, so there is no query that is
unexpectedly expensive and no p95 tail to discover in production.

Three shapes measured apart rather than blended, because the temporal branch is
already an order of magnitude more expensive than the plain one on a store
where a window covers many turns — a single p95 over a mixed workload would
report the cost of time-aware retrieval and call it the cost of embeddings.

## Fusion

**Reciprocal Rank Fusion, k = 60, unweighted**, over the BM25 ranking and the
cosine ranking, each deepened to `conversation_vector_candidates` (12) before
fusing. RRF because the two rankings have no common scale: BM25 returns an
unbounded negative log-odds and cosine returns a number whose useful band for a
given model is narrow and uncalibrated on this corpus. RRF reads positions
only, so it needs no normalization and no training data, and it degrades
gracefully — an empty ranking contributes nothing and the answer is exactly the
other ranking's.

Both rankings are deepened before fusing because the entire point is that a
turn ranked fourth lexically and first semantically should win a slot, and it
cannot if the lexical list was truncated at three before anyone looked.

Ties break on turn id descending, so the fused order is a pure function of its
inputs. A non-deterministic prompt is a non-reproducible bug.

Known limitation: when BM25 has a strong exact match and the vector list
returns something merely plausible, both sit at rank 1 and tie on RRF mass.
`conversation_vector_min_similarity` is the knob for that — a cosine floor
below which vector candidates are not admitted at all. It defaults to 0.0 (off)
because the useful band is model-specific and picking a number without live
data would silently delete recall. Tune it from the store.

## Integration with time-aware retrieval

Time-aware retrieval strips date words from the query before searching, because
昨日 was otherwise retrieving the robot's own 「昨日のことは、わからないよ。」 Two
consequences, both handled:

**The query embedded is the residue, never the raw utterance.** Embedding 昨日
would hand the vector space the same problem *and worse*: 昨日 and
「昨日のことは、わからないよ。」 are not merely lexically similar, they are
semantically similar, so a dense ranker would promote the refusal more
confidently than BM25 ever did. `retrieval_query_text()` is the shared helper.
An utterance that leaves nothing searchable behind — 「昨日？」 — skips the
service call entirely; the temporal spread answers a question about a day
better than any ranking could.

**The sweep respects the window.** `_retrieve_semantic` carries the same
half-open `created_at` restriction the lexical search takes. A better cosine
outside the window is a wrong answer to the question that was asked: 「昨日の天気
の話覚えてる？」 must not return last month's weather chat. Meaning narrows
within the window; it never widens it.

**The stored text is embedded without its time label.** `（昨日 19:09）` is
relative to the moment of rendering — today's 昨日 is tomorrow's 一昨日 — so
embedding it would bake in a value that goes stale nightly and would need the
whole store re-embedded to stay true. Time is already handled exactly, by
`created_at` and a half-open range; asking a 384-dimensional space to
approximate a comparison SQLite does precisely would trade a correct answer for
a fuzzy one.

## Deployment

Nothing below restarts the bridge until the last step, and every step is safe
to stop after.

**1. Install and start the service** (builds llama.cpp, ~5 min on a Pi 5):

```
sudo ./scripts/install-embeddings.sh
sudo systemctl enable --now bocco-embeddings.service
```

**2. Verify it answers, with the right dimension:**

```
curl -s http://127.0.0.1:8646/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"multilingual-e5-small","input":["query: ごはんの話"]}' \
  | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["data"][0]["embedding"]))'
```

Expect `384`.

**3. Measure the encode latency before trusting the deadline:**

```
cd /opt/bocco-bridge  # or the repository checkout
PYTHONPATH=bridge/src python3 bridge/tools/bench_retrieval.py --service
```

If p95 is comfortably under 120 ms, continue. If not, see "Why 120 ms" above.

**4. Backfill the existing turns.** Safe to run while emo is in use — small
batches with a pace between them — and safe to interrupt with Ctrl-C:

```
sudo -u bocco-bridge PYTHONPATH=bridge/src python3 \
  bridge/tools/backfill_embeddings.py \
  --transcript-db /var/lib/bocco-bridge/transcript.db \
  --room "$BOCCO_ROOM_UUID" --dry-run
```

then drop `--dry-run`. Re-running resumes and never duplicates.

**5. Turn it on.** In `/etc/bocco-bridge/bridge.env`:

```
BRIDGE_CONVERSATION_VECTORS=on
```

then `sudo systemctl restart bocco-bridge.service`.

**6. Confirm on hardware.** Ask emo something a paraphrase should reach —
「ごはんの話したっけ」 against a stored 食べ物/スープ exchange — and watch reply
latency. To roll back, set `BRIDGE_CONVERSATION_VECTORS=off` and restart; the
stored vectors are inert and cost nothing.

## Failure modes, in full

| What happens | What the user experiences |
|---|---|
| Service not installed / not running | Identical to today: BM25 retrieval, unchanged reply time. Three connection refusals per minute in the log at most. |
| Service slow (model reload, CPU contention) | The reply waits at most 120 ms once, then proceeds on BM25. After three such misses, nothing waits at all for 60 s. |
| Service returns garbage, wrong dimension, non-JSON | Treated as an outage. BM25 result, no exception, no retry. |
| Service dies mid-conversation | Turns keep being stored; their vectors are missing. Re-run the backfill to recover them. |
| Model changed without a re-backfill | The sweep matches nothing (vectors are filtered by model name), so retrieval is exactly BM25 until the backfill runs. Never a cross-space comparison. |
| Transcript database missing or corrupt | Unchanged from before: `_conversation_section` swallows it into an empty string. |
| Bridge restarted mid-embedding | The detached task is cancelled at shutdown; that turn is lexical-only until the backfill runs. |
