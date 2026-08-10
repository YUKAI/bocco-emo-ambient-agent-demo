# Accelerometer voice-reaction latency

Date: 2026-08-05

## Verdict

The ordinary accelerometer path is already close to the floor imposed by BOCCO. A
normal, unblocked reaction starts speaking about **1.9-2.0 seconds after the
webhook reaches the bridge**. Adding the independently established physical-event
to webhook transit of at most about 0.7 seconds puts the practical
physical-touch-to-speech-start range at roughly **1.9-2.7 seconds**, with
**2.3-2.6 seconds** a representative budget. This directly corroborates the
previously assembled arithmetic rather than merely repeating it.

Most of that time is outside the bridge:

- BOCCO API completion to device speech start is about **1.5 seconds**
  (`delivery_lag_seconds=1.595`, n=58 in the requested baseline). This is Yukai's
  transport/device behavior. The bridge cannot optimize it for text messages.
- Physical interaction to webhook ingress is already at most about **0.7
  seconds**. BOCCO payload timestamps lag reality by about two seconds, so they
  must not be used as arrival clocks.
- The bridge's normal webhook-to-text-send segment is about **0.36 seconds** in
  the two direct samples (358 and 365 ms). A larger retained correlation has a
  300 ms median and 450 ms p90 to text API completion (n=50), excluding neither
  the one large worker collision nor routine variation.

SQLite is not the missing hundreds of milliseconds. DB-only accel branches
finish in 17-18 ms at the median, and each live connection-plus-SELECT benchmark
is about 0.35 ms. A delivered event reaches the text API response 243 ms after
worker start at the median (n=50); a fresh BOCCO HTTPS call accounts for about
148 ms. The entire remaining, not separately instrumented envelope before that
response is only about 77-95 ms, including DB writes, thread handoffs, phrase
selection, response preparation, and network variance.

There is, however, a separate long-tail defect. The runtime has one
non-preemptive event worker. Six of 170 accel webhooks in the contention snapshot
(3.5%) arrived while it was busy. One spoken reaction arrived during a
`reaction_bank.refresh` and waited about **10.9 seconds** before its normal
304 ms processing began. This is rare, but it exactly produces the experience of
a voice reaction landing long after the physical interaction is over.

The realistic normal-path recovery is approximately **10-100 ms**, not seconds.
The high-value architectural fix is eliminating the 10-second-plus tail caused by
background generation. The 1.5-second BOCCO text-delivery leg is immovable by us.

## Scope and evidence

This investigation was read-only on the Pi. It used only the retained
`bocco-bridge.service` journal and `/var/lib/bocco-bridge/state.db`, opened with
the bridge virtual environment's Python in SQLite read-only/query-only mode. It
did not call a BOCCO endpoint, send a message, trigger the robot, restart a
service, deploy code, or change configuration.

Local source was inspected to explain the measurements. The connection-pooling
change from another worker is treated as in flight: its supplied fresh-versus-hot
benchmark is included, but no connection change was implemented here.

The journal was live while the snapshots were taken, so counts increased slightly
during the investigation. The latency correlation contains 50 retained delivered
accel rows from 2026-08-04 onward. The branch-duration snapshot taken later
contains 173 accel completions: 61 delivered, 75 state-return-silent, and 37
cooldown-silent. The contention snapshot contains 170 accel arrivals. These
slightly different denominators are called out rather than silently combining
them.

## End-to-end composition

`inbound_events.received_at` is the BOCCO event clock and is biased by roughly
two seconds. Queue and bridge timing therefore use journal arrival time. Device
speech anchors use BOCCO's `newMessageMotion` correlation, for which the common
clock bias cancels when compared with BOCCO message timing, but individual
anchors remain noisy.

| Stage | Sample | Typical value | Tail/range | Ownership |
|---|---:|---:|---:|---|
| Physical interaction to webhook ingress | Prior physical lift tests | <=700 ms | Exact distribution unavailable | Yukai firmware/cloud |
| Webhook handler/queue before worker start, delivered accel | 50 | 45.5 ms median | 101 ms p90; 10.888 s max | Bridge; max is single-worker blocking |
| Worker start to text API response | 50 | 243 ms median | 325 ms p90; 537 ms max | Bridge plus BOCCO HTTPS request |
| Webhook ingress to text API response | 50 | 300 ms median | 450 ms p90; 11.136 s max | Bridge plus BOCCO HTTPS request |
| Direct webhook-to-text measurements | 2 | 358/365 ms | — | Bridge plus BOCCO HTTPS request |
| Text API response to device speech anchor | Requested baseline, 58 | 1.595 s calibrated | Does not materially scale with text length | Yukai cloud/device |
| Paired webhook ingress to device anchor | 28 | 1.908 s median | Noisy 4.468 s p90 | Measured composition; platform timestamps create outliers |
| Paired, unblocked records with a plausible 1.0-2.5 s anchor lag | 9 | 2.040 s median | 2.471 s p90; 2.550 s max | Corroborating subset, not an independent percentile |
| Audible phrase duration | Typical 8-10 chars | 1.4-1.8 s | 0.175 s/char | Content/device; affects finish, not start |

The paired median is the important composition check:

```text
webhook ingress -> device speech anchor ~= 1.9-2.0 s
physical interaction -> webhook ingress <= 0.7 s
physical interaction -> speech start ~= 1.9-2.7 s
```

The 1.908-second unfiltered paired median and 2.040-second plausible/unblocked
median agree with the independent `0.36 + 1.50 = 1.86` second estimate. Because
physical transit is an upper bound rather than a measured median, it would be
incorrect to claim a precise physical-to-speech percentile. The evidence does
validate the proposed 2.3-2.6-second representative budget.

Once speech starts, the already-short reaction still takes about 1.4-1.8 seconds
to finish. Thus even at the normal floor, the spoken reaction can end roughly
3.7-4.4 seconds after the touch. Shortening the current 8-10-character phrases
further cannot materially improve onset.

## Where the bridge segment goes

The current normal accel path is sequential:

1. `coalesce_same_second_accel` opens an immediate write transaction, consumes
   pending same-second peers, and persists the selected kind.
2. `get_effects` checks the durable response/send checkpoint.
3. `reserve_accel_reaction` opens an immediate write transaction and reserves the
   global/kind cooldown. This must happen before any output.
4. `_reaction_phrase` reads the stored persona, hashes the composed persona, and
   reads the reaction bank. There is no event-time Hermes call.
5. `_prepare_generated_response` performs small in-process string/cue work.
6. `save_response_if_absent` durably checkpoints the selected phrase.
7. A second `get_effects` reloads the checkpoint.
8. `_deliver_response` reads speech calibration and calls `send_text`.
9. After the API returns, `record_bocco_delivery` atomically records the returned
   message ID, content hash, `bocco_sent`, and speech observation. Normal accel
   may then send a motion before `event_completed` is logged.

The measurements localize these operations:

| Path/operation | n | Median | p90 | Interpretation |
|---|---:|---:|---:|---|
| State-return accel: coalesce then exit | 75 | 17 ms | 20 ms | Upper bound for the initial write/thread path |
| Cooldown accel: coalesce + effects read + cooldown reservation then exit | 37 | 18 ms | 21.8 ms | Reservation is not a material delay |
| Delivered accel, whole active event | 61 | 302 ms | 389 ms | Includes the text send and post-text work/motion |
| Worker start to text API response | 50 | 243 ms | 325 ms | Pre-send work plus BOCCO POST |
| Text API response to event completion | 50 | 60 ms | 68 ms | Happens after text was issued; does not delay speech onset |
| `get_effects`, connection + SELECT | 300 | 0.365 ms | 0.374 ms | Live read-only microbenchmark |
| Persona lookup, connection + SELECT | 300 | 0.347 ms | 0.353 ms | Live read-only microbenchmark |
| Reaction-bank lookup, connection + SELECT | 300 | 0.353 ms | 0.360 ms | Live read-only microbenchmark |
| Calibration lookup, connection + SELECT | 300 | 0.356 ms | 0.362 ms | Live read-only microbenchmark |

The raw SELECT execution previously measured about 0.01 ms; opening and closing
a SQLite connection brings each full read method to about 0.35 ms. Even four or
five such reads total only roughly 1.5-2 ms. Writes use `synchronous=FULL` and
were not benchmarked because this investigation was strictly read-only, but the
17-18 ms control branches include the relevant coalesce/reservation write paths.

At the medians, `243 ms - 148 ms fresh POST - 17/18 ms baseline` leaves about
77 ms. Using no baseline subtraction leaves a conservative 95 ms upper envelope.
That remainder includes the response checkpoint write, extra thread scheduling,
phrase preparation, and HTTP timing variance; the existing logs cannot divide it
further. It is not hundreds of milliseconds of SQLite work.

## Can the POST be issued earlier?

### What may safely move or collapse

There are small safe opportunities:

- Move the calibration read below the text POST and only execute it when inline
  motion cues exist. Normal non-serious accel explicitly clears motion cues, so
  it does not need calibration before sending.
- Make `save_response_if_absent` return the complete effects needed by delivery,
  avoiding the second `get_effects` connection/thread round trip while preserving
  the durable phrase checkpoint.
- Cache the composed persona hash and reaction-bank rows in memory, with explicit
  invalidation after persona changes/bank refresh.
- Consider one DB method that coalesces and reserves the accel in a single
  transaction/thread hop. Cooldown reservation must still commit before output.

Together these are likely worth **5-30 ms**. The measured residual puts a hard,
conservative ceiling of about **77-95 ms** on all such pre-send work combined.

### Why `select -> send -> persist` is not safe

The response checkpoint and send marker serve different failure cases:

- `save_response_if_absent` makes a retry choose the same phrase.
- `record_bocco_delivery` stores `bocco_sent`, the returned `unique_id`, and the
  outbound echo record after BOCCO accepts the request.

If the bridge sends first and crashes after BOCCO accepts the POST but before the
checkpoint transaction, the retried inbound accel has no durable proof of the
send. Resending can produce duplicate speech/chat messages. Treating an
unconfirmed attempt as sent instead creates the opposite failure: a request lost
before BOCCO accepted it would be suppressed forever. The text API exposes no
idempotency key, so an ambiguous connection failure cannot provide exactly-once
delivery.

Echo handling also matters. While the single worker is healthy, an echoed
`message.received` waits behind the active accel event, giving the original event
time to store its returned ID. After a crash/restart, however, the higher-priority
message webhook may be processed before the accel retry. Without a durable
outbound record it can be treated as user speech. This class of failure has
already occurred for request `fe4075a1` (`outcome=speech_replied`).

A redesigned outbox could pre-insert a `message_id IS NULL` row with the text hash
before POSTing and update it with the returned ID. That would close the healthy
webhook race if sending became concurrent, but it would not solve the
accepted-versus-not-accepted crash ambiguity. It also risks consuming a genuine
identical user message as a content-hash echo. A real solution would need either
a platform idempotency key or a reliable post-crash reconciliation query.

Conclusion: do not trade duplicate/echo guarantees for a speculative few tens of
milliseconds. Preserve cooldown reservation and response persistence before the
POST; only collapse redundant reads and move calibration work that is genuinely
post-send.

## Cold connections and pre-warming

The supplied benchmark is 148 ms for a fresh HTTPS request and 56 ms on an
already-kept-alive socket, a **93 ms** per-call difference. That is the maximum
normal-path benefit of the connection-reuse fix when the socket is actually warm.

Accelerometer speech is sporadic. In the retained state, 56 of 60 delivered accel
messages (93%) were more than 30 seconds after the previous recorded text send;
52 of 60 were more than 60 seconds later. Fifty-eight of 59 successive accel
messages were more than 30 seconds apart. Other API operations can warm a socket,
so the text-send comparison is a proxy rather than a complete connection trace,
but it establishes that warm bursts are unusual.

The in-flight pool's inspected default expires idle connections after 30 seconds.
Using the recorded-text proxy, only 4 of 60 accel sends would have been eligible
for a hot socket. At 93 ms each, that is roughly **6 ms average saving per accel
reaction**, though clustered activity may do somewhat better. The pool remains a
sound low-risk improvement for conversation bursts; it is not the solution to
sporadic touch latency.

A periodic application-level keepalive short enough to beat a 30-second idle
limit would require at least two API requests per minute, about 2,880 per day. It
would consume rate-limit budget and cloud resources to recover at most about
93 ms on each relatively rare spoken accel. TCP keepalive alone cannot guarantee
that an HTTP proxy retains application state, and the current client itself
expires the entry. Prewarming only after the webhook arrives is also unattractive:
the warm-up must first pay the same cold handshake, and if it overlaps the POST,
the bounded pool opens a separate connection.

Recommendation: keep connection reuse, but do **not** add a periodic BOCCO API
keepalive. The expected gain is below perceptual significance and cannot change
the 1.5-second device-delivery leg.

## Alternative vocal delivery channels

No alternative was live-tested because this task prohibited sending anything to
the robot. Neither endpoint can currently be claimed faster.

### Audio message with `immediate=True`

`POST /v1/rooms/{room}/messages/audio` accepts a multipart `immediate` boolean.
The bridge's `_send_ack_audio` currently passes `False`; the client and multipart
encoder correctly support `True`. A fixed pre-rendered short clip with
`immediate=True` is the only alternate worth a tightly controlled future latency
test. The hypothesis is not “skip TTS” alone—the lack of text-length scaling has
already ruled TTS synthesis out as the main text bottleneck. The useful hypothesis
is that `immediate=True` bypasses the stock new-message notification/playback
ceremony and chooses a faster device path.

Expected saving is **unknown and may be 0 ms**. The theoretical ceiling is some
fraction of the current 1.5-second device-delivery leg; there is no evidence to
assign it a positive value yet. Uploading audio can also add payload time. A
future test should use one small already-rendered clip, measure physical playback
start, and compare it with a nearby text baseline.

This endpoint creates a real room chat message. Frequent touch reactions would
therefore add exactly the clutter the user has already rejected. It also limits
persona variation unless many clips are maintained. Even if faster, it should be
opt-in or reserved for rare/high-severity reactions, not the default accel path.

Any implementation must wrap the send in the existing per-room
`outbound_audio_delivery` barrier and persist the returned `unique_id` through
`record_audio_delivery` before releasing the barrier. Otherwise its
`message.received` webhook can become a self-reply.

### Native stamps

`POST /v1/rooms/{room}/messages/stamp` sends `{"uuid":"<stamp uuid>"}`. The 20
catalog entries carry pre-rendered audio and avoid TTS. However, text-length
independence already indicates that TTS is not the 1.5-second bottleneck. Unless
the device prioritizes native stamp assets differently, the expected saving is
probably small; **0-200 ms is a test hypothesis, not a measured result**.

Stamps have only 20 fixed meanings, provide little persona control, and also
create a real chat message. The clutter/semantic cost is high for frequent
physical events. A future benchmark may be informative, but stamps are not a
recommended default vocal-reaction channel.

Any stamp send must use `outbound_stamp_delivery` and persist the returned
`unique_id` with `record_stamp_delivery`. The generic echo consumer recognizes
that ID in `outbound_media_ids`; omitting it recreates the self-reply failure.

## Single-worker contention

The priority lanes affect only the next claim. Once `process_event` starts, a
newly arrived accel cannot preempt it.

In the 170-arrival contention snapshot:

- 6 accel webhooks (3.5%) arrived while another event was active.
- Blockers were two `reaction_bank.refresh`, two `message.received`, one
  `motion.due`, and one other `accel.detected`.
- Busy-arrival remaining time had a 462 ms median and 12.559 s maximum (n=6).
- Non-busy accel queue time was 41 ms median and 96.7 ms p90 (n=164).
- Only one busy arrival produced speech. It waited 10.875 seconds behind a
  reaction-bank refresh; its total queue wait was 10.888 seconds, then its normal
  processing took 304 ms.

The broader refresh-duration snapshot contains 18 completed
`reaction_bank.refresh` events: 19.447 s median, 43.778 s p90, and 71.712 s
maximum. The 13.847 s and 16.741 s observations from the brief are therefore
representative, not exceptional.

Contention does not explain the normal 2-3-second reaction floor, but it explains
rare reactions that are dramatically and visibly late. Based on one collision
among 61 delivered reactions, removing the observed 10.875-second wait is about
**178 ms averaged over all spoken accel reactions** in this sample while saving
**10.875 seconds for the affected reaction**. The mean understates its product
value: this is a tail-correctness issue.

## Ranked fixes

| Rank | Change | Expected saving | Risk/cost |
|---:|---|---:|---|
| 1 | Move `reaction_bank.refresh` off the sole event worker, or make it yield/preempt between bounded units of work | Typical reaction: 0 ms. Observed affected reaction: 10,875 ms. About 178 ms averaged across 61 spoken reactions. | Medium. Requires shutdown/retry discipline and avoiding uncontrolled concurrent Hermes work, but DB writes are short and SQLite-safe. |
| 2 | Complete the in-flight persistent-connection pool | 93 ms when warm; about 6 ms average for the observed sporadic accel pattern under a 30 s idle limit | Low. Retain bounded pooling and one reconnect on a stale socket; ambiguous POST replay remains an at-least-once concern. |
| 3 | Collapse only safe pre-send bookkeeping: return effects from `save_response_if_absent`, skip/move calibration for no-cue accel, cache persona/bank, optionally combine coalesce+reserve | Likely 5-30 ms; all local residual work has a 77-95 ms conservative ceiling | Low to medium. Preserve the cooldown commit, durable phrase checkpoint, and retry semantics. |
| 4 | Add redacted numeric stage timings (`worker_start`, each DB phase, POST start/end, durable-record end) before deeper optimization | 0 ms directly | Low. Needed to divide the remaining 77-95 ms without guessing; log no text, IDs beyond request correlation, or credentials. |
| 5 | One controlled A/B of pre-rendered audio with `immediate=True` | Unknown, possibly 0; theoretical opportunity is within the 1.5 s device leg | High product cost. Creates chat clutter and fixed audio/persona maintenance; must durably record `unique_id`. Do not make default without a large measured win. |
| 6 | One controlled native-stamp timing test | Unmeasured; working hypothesis 0-200 ms | High product cost. Creates chat clutter, only 20 fixed assets, weak persona fit; must durably record `unique_id`. |
| 7 | Periodic API keepalive | At most about 87-93 ms average if it kept every touch warm | Poor tradeoff. At least 2,880 requests/day for a <30 s interval, rate-limit/resource cost, and no proxy-lifetime guarantee. Do not implement. |
| 8 | Optimistic `send -> persist` | Perhaps only 5-30 ms beyond the safe collapses | High correctness risk: duplicate speech after crash, lost speech under at-most-once recovery, and self-echo after restart. Do not implement without platform idempotency/reconciliation. |

For rank 1, the safest shape is a dedicated background-generation task that does
not own the event worker while awaiting Hermes. It should publish each completed
bank entry with a short transaction, remain lowest priority at the Hermes
boundary, and have explicit cancellation/retry state. Merely splitting a refresh
into six queued event jobs reduces the maximum block from a full refresh to one
Hermes call but does not eliminate non-preemption; it is an improvement, not a
complete fix.

## Product floor

After removing worker collisions and taking the plausible normal-path local wins,
the likely text-channel onset is still approximately:

```text
physical-to-webhook       0.0-0.7 s
bridge + text POST        about 0.25-0.35 s
BOCCO device delivery     about 1.5 s
------------------------------------------------
speech starts             about 1.75-2.55 s after touch
```

The lower end assumes unusually fast physical transit; the representative case
remains near 2.3-2.6 seconds. Connection reuse and bookkeeping may recover only
about 10-100 ms on a sporadic event. They cannot make our spoken reaction feel as
instant as the firmware's local non-verbal reflex.

The honest conclusion is therefore two-part:

1. Fix the single-worker/background-refresh collision because it causes rare but
   catastrophic 10-second-plus voice delays.
2. For the normal path, accept that BOCCO's roughly 1.5-second cloud/device
   transport is the dominant immovable floor unless a separately authorized test
   proves that `audio immediate=True` uses a genuinely faster playback path and
   its chat-clutter cost is acceptable.
