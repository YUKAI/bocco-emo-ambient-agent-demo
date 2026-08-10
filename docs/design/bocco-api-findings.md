# BOCCO emo Platform API: measured behavior and integration traps

Date of hardware measurements: 2026-08-05

## Purpose and evidence standard

This document records behavior that is missing from, ambiguous in, or easy to
misread from the public Platform API documentation. It is intended for someone
with a BOCCO emo who needs enough wire-level and timing detail to build a reliable
integration without repeating the same hardware characterization.

Evidence is identified beside every finding:

- **Live measurement** means an API request, webhook capture, or physical robot
  observation made on 2026-08-05. `n` is the number of requests/events when it
  was retained. Where the contemporaneous record did not preserve a repeat count,
  that is stated instead of inventing one.
- **Retained capture** means a journal/SQLite correlation. It is observational,
  not a guarantee that all firmware versions behave identically.
- **Static inspection** means editor, SDK, client, or schema code. A sample size
  is not applicable; the inspected artifact count or code path is given.
- **Inference** means a design conclusion drawn from measurements. It is labeled
  and must not be reported as a direct observation.
- **UNTESTED** means exactly that. It is a lead for a future controlled trial,
  not a capability claim.

The required evidence reports agree on message echo behavior, the approximately
1.5-second device-delivery floor, and non-preemptive worker blocking. Three
evidence conflicts or interpretation changes are material and are documented
under [Evidence conflicts and limits](#evidence-conflicts-and-limits).

## Wire-format quick reference

The following are the minimum request shapes established by the implementation,
live tests, or both. Authentication headers are deliberately omitted.

### Send text

```http
POST /v1/rooms/{room_uuid}/messages/text
Content-Type: application/json

{"text":"短い返事"}
```

The successful response may contain `unique_id`. Persist it before processing
the message echo. **Evidence:** static client inspection, one code path; retained
capture of 403 exact-ID self-echo suppressions across all media, including 194
speech-bearing echoes.

### List and send native stamps

```http
GET /v1/stamps
```

The live catalog returned 20 entries, from names `w1ohayo` through
`w20sleepy`. Listing objects exposed `uuid`, `name`, `summary`, and `image`; they
did not advertise their audio asset.

```http
POST /v1/rooms/{room_uuid}/messages/stamp
Content-Type: application/json

{"uuid":"<stamp uuid>"}
```

Optional speech is added with a second JSON key:

```json
{"uuid":"<stamp uuid>","text":"任意の発話"}
```

The identifier key is **`uuid`**, not `stamp`. A request using a key named
`stamp` returned HTTP 500 with `failed to generate response`. The official YUKAI
SDK independently builds `payload = {"uuid": stamp_id}` and adds `text` only
when a message is supplied. **Evidence:** live endpoint checks on 2026-08-05;
the supplied trial record does not state repeat counts for the correct, optional
text, and wrong-key variants. Static cross-check: one SDK send method; the SDK
source is not vendored with these reports.

Omitting `text` produced the stamp's native motion and sound without synthesized
speech. Supplying `text` added spoken words. A successful send response exposed
an `audio_url`, even though the catalog listing did not. **Evidence:** at least
one observed live send in each described mode; exact repeat counts were not
retained. The `audio_url` finding comes from one recorded successful response.

### Upload an audio message

```http
POST /v1/rooms/{room_uuid}/messages/audio
Content-Type: multipart/form-data; boundary=...

... file field: audio=<MP3 or M4A bytes>
... form field: immediate=true|false
```

The multipart form field is named **`immediate`** and is boolean. The bridge path
studied here sends `false`.

> **UNTESTED:** what `immediate=true` changes on the device is unknown. It might
> change playback scheduling, or it might do nothing relevant to latency. No
> live `true` trial exists in the evidence set.

**Evidence:** static client/encoder inspection and tests for the field encoding;
live behavioral sample size for `immediate=true`: **n=0**.

This endpoint posts a real audio message into the room; it is not a private
device-control channel. Stamps likewise post real room messages. Retained echo
auditing contained 178 audio-message echoes and six stamp echoes among 403 exact
outbound-ID suppressions. For frequent sensor reactions, the resulting room
history is a material product cost rather than an incidental implementation
detail. Any audio or stamp experiment must persist the response's `unique_id`
through the same outbound echo-suppression path as text, or it can re-enter the
bridge as user input. **Evidence:** n=178 audio and n=6 stamp exact-ID echoes;
n=1 observed self-reply incident across the outbound system. The assessment of
chat clutter as unacceptable for frequent reactions is a product constraint,
not a latency measurement.

### Send a preset or custom motion

Preset catalog:

```http
GET /v1/motions
```

Preset send and custom-document send use the room motion endpoint used by the
client:

```http
POST /v1/rooms/{room_uuid}/motions
```

The live catalog snapshot contained 136 presets. Custom Motion Editor documents
contain cubic head paths and track data rather than a preset UUID. **Evidence:**
one complete paginated live catalog snapshot, n=136 entries; static custom-motion
schema/editor inspection, one schema path.

## Motion and sound

### The custom-document `sound` field is inert

Three otherwise controlled live custom-motion sends produced the same silent
result:

| Custom document | HTTP result | Physical result | Sample |
|---|---:|---|---:|
| `sound.name="zzz_not_a_real_sound_zzz"` | 200 | No sound; invalid name was not rejected | n=1 |
| `sound.name="w10question"` | 200 | No sound, despite this being a genuine device sound name | n=1 |
| `sound` key omitted | 200 | Identical silence | n=1 |

This demonstrates both lack of name validation and lack of playback in the tested
firmware/API path. It does not prove how every firmware version will behave.

Static evidence explains the result: the official editor's sound picker stores a
local file path, but the selected bytes are never uploaded with the exported
motion. An inspection of YUKAI's 12 supplied sample motions found all 12 using an
empty sound name and zero delay. **Evidence:** one editor export/data-flow
inspection; n=12 official sample documents.

Practical rule: treat custom motion documents as silent. Do not attempt to name a
stamp sound in the custom `sound` field.

### Stamps are the only working Platform-native packaged sound found

Native stamps carry pre-rendered audio. The live stamp list had 20 entries, and
the successful send response that revealed `audio_url` established the presence
of audio. **Evidence:** n=20 catalog entries; n=1 response inspected for
`audio_url`; physical stamp playback observed, repeat count not retained.

“Only source of robot audio” must be read narrowly: stamps were the only working
source of a **Platform-native packaged sound effect** found in these tests. BOCCO
can still synthesize a text message and can accept an uploaded audio message.

A stamp is a complete performance, not an isolated sound effect. It executes its
own motion while playing its native audio. Pairing it with a custom motion sends
two animations that may compete or queue. **Evidence:** physical observation of
stamp motion plus sound; exact repeat count not retained. **Inference:** avoid
pairing unless a controlled timing test proves the combined choreography is
acceptable.

### Preset and custom motion completion is not observable as expected

A later retained census contained 332 `motion.finished` events. Every observed
kind was a stock device behavior:

| `motion.finished` kind | Count |
|---|---:|
| `newMessageMotion` | 142 |
| `accel_lift` | 53 |
| `recordStart` | 30 |
| `ambientDarker` | 25 |
| `recordFinish` | 24 |
| `ambientBrighter` | 24 |
| `accel_beaten` | 22 |
| `triggerDetect` | 6 |
| `accel_lying_down_in` | 5 |
| `weatherMotion_cloudy` | 1 |

No API-sent preset identity appeared in this n=332 capture. This is evidence of
absence in the retained firmware/capture, not a universal protocol guarantee.
Do not build a preset-motion queue that assumes every API send will receive a
matching finish callback.

Custom motion documents produced no `motion.finished` webhook at all. The
motion/speech study contained five custom-only chains and no physical finish
signal for them. A runtime must therefore mark custom documents complete after
successful API delivery or use its own duration/timeout model. **Evidence:** n=5
custom-only retained chains; n=0 matching custom finish webhooks.

An earlier snapshot contained 262 motion-finished events and only one
recognizable preset-finish candidate (`weatherMotion_cloudy`), which arrived
about 37 seconds after the associated speech anchor and was confounded by later
message batching. It did not establish whether speech and motion execute
concurrently. **Evidence:** n=262 events, n=1 confounded candidate, n=0 clean
concurrency samples. Concurrent-versus-queued behavior remains **UNRESOLVED**.

### The preset catalog contains built-in sound variants

The n=136 live preset snapshot divided into:

- 80 family variants: eight families (`ALRIGHT`, `BOCCO`, `EMO`, `GOOD`, `NO`,
  `WHAT`, `WTF`, `YES`) with ten variants per family, using an `A/H/N/W` letter
  axis in their names.
- 56 scene and other motions.

The family variants differed in their built-in sound selection. That is why the
same semantic gesture family can sound different between turns. **Evidence:**
one full catalog inspection, n=136 presets (80 family, 56 other). This finding
does not conflict with the inert custom-document `sound` key: preset sounds are
firmware/catalog assets, whereas a custom export does not upload audio.

### Organic head motion requires control points, timing, and asymmetry

Head segments are cubic Beziers with `p0`, `p1`, `p2`, and `p3`. The accepted
endpoint bounds are +/-45 degrees horizontally and +/-20 degrees vertically;
Bezier control points are accepted to +/-120 degrees. **Evidence:** static lint
and consuming-schema inspection; n/a as a schema limit. Horizontally, the
control-point range is about 2.7 times the endpoint range; vertically it is six
times the endpoint range.

**Inference from authored-motion comparison:** off-axis control points create
curved travel and overshoot without requiring an unreachable endpoint. More than
half of head transitions leaving `p1` unset is therefore flagged by the motion
linter as `straight-line-head`. Uniform tempo, one easing curve, identical
cheeks, no overshoot, and long frozen holds are separate linter warnings because
the resulting performance reads as mechanical. Evidence is a static linter rule
set plus 12 documented library motions; it is an authoring heuristic, not a
quantified user study.

## Messaging, echoes, and delivery confidence

### Sender UUID cannot distinguish humans from the bridge

With personal user OAuth, human speech, official-app text, and API-posted message
echoes all carried the same account sender UUID. The API call acts as the user;
there is no separate bot identity. **Evidence:** live captures across all three
origin classes (n=3 classes); the reports do not preserve the raw event count
used for the UUID equality check.

The reliable suppression key is the outbound response's `unique_id`, matched to
the `message.received` message ID. Retained auditing found 403 exact-ID echo
suppressions: 180 text echoes with text, 14 legacy/no-marker text echoes, 178
audio echoes, 25 motion echoes, and 6 stamp echoes. No transcribed human audio was
misclassified in the retained set (0 of 40). **Evidence:** n=403 suppressions;
n=40 transcribed audio inputs.

Getting this wrong creates a feedback loop. One live incident, request
`fe4075a1`, was processed as human speech and ended `outcome=speech_replied`.
**Evidence:** n=1 observed self-reply incident.

Implementation rule:

1. Before sending, preserve the source request and intended output durably.
2. After a successful POST, persist its returned `unique_id`, room, media type,
   and sent time before allowing its echo to be processed.
3. On `message.received`, consume an exact outbound ID match before invoking an
   AI model.
4. Use a one-shot room + exact-content hash only when the API response omitted an
   ID. Sender UUID is, at most, a legacy guard for deployments with a genuinely
   separate posting identity.

The durability order is an architectural inference validated by the n=1 failure
and n=403 successful exact-ID suppressions. It does not create exactly-once
delivery across an ambiguous network failure; the text endpoint exposes no
documented idempotency key.

### API acceptance is not proof that the robot spoke

In a retained voice subset, 44 reply chunks from 38 sources all received message
IDs. Forty-three of 44 received an exact echo and 43 of 44 had a nearby
`newMessageMotion`; one had neither signal despite more than an hour of later
events. **Evidence:** n=44 chunks, one unconfirmed delivery candidate.

Neither echo nor `newMessageMotion` is a guaranteed playback receipt, so the one
gap does not prove silence. It does prove that `HTTP 2xx + unique_id` should be
recorded as **API accepted**, not **robot spoke**.

### Recording and transcript events can be missing or reordered

The retained voice-input study found 48 audio inputs requiring a response: 40
with transcripts and eight without usable transcripts. All 48 reached a
successful BOCCO text POST checkpoint on the first worker attempt. **Evidence:**
n=48 inputs; 48 successes; zero bridge-side silent terminations.

At the recording boundary, 40 of 41 non-synthetic `recording.finished` events
could be paired with a genuine audio input within +/-15 seconds. One finished
recording produced no genuine audio `message.received` at all, so no conversation
handler could reply. **Evidence:** n=41 finished recordings; n=1 missing input
message.

Platform timestamps were reordered relative to intuitive recording flow: in 29
of 40 matched pairs the audio-message timestamp was 1-6 seconds **before**
`recording.finished`; 11 shared the same whole-second timestamp; none was later.
**Evidence:** n=40 pairs. Correlate bidirectionally by local ingress time and room,
not only by platform timestamp ordering.

## Timing: measured floors and variable stages

### Device delivery and speaking

The calibrated text API completion-to-speech-start lag was 1.595 seconds over 58
samples. It did not materially scale with output length: messages of 15 or fewer
characters had a 1.44-second median versus 1.66 seconds for messages of 30 or
more characters. **Evidence:** n=58 total; the required reports do not preserve
the two bin counts, so the per-bin medians must not be given confidence intervals.

Motions with no TTS exhibited the same broad 1-2.5-second delivery floor.
**Evidence:** live motion timing range reported by the motion study; the number of
independent motion deliveries used for that range was not retained. **Inference:**
text-to-speech synthesis is not the dominant part of the fixed delivery lag.

The runtime calibration value for speaking rate is
`seconds_per_char=0.175`. **Important evidence conflict:** this value appears in
the n=58 calibration and latency reports, but the motion/speech report traced its
last change to one impossible 1,440-second `emo_talk.finished` association and
therefore retained 0.150 seconds/character as its prior estimate. Treat 0.175 as
the current runtime parameter, not a validated physical speaking-rate
measurement, until a unique-phrase test measures it again.

### Touch and sensor timing

Physical lift tests put physical interaction-to-webhook ingress at no more than
about 0.7 seconds. Their trial count and exact distribution were not retained.
BOCCO event payload timestamps lagged wall-clock reality by about two seconds in
those tests. Use the HTTP/journal ingress clock for latency; use platform
timestamps only when comparing events on the same biased BOCCO clock.

Paired retained measurements put normal webhook-ingress-to-speech-anchor latency
at 1.908 seconds median (n=28). A conservative unblocked subset with plausible
anchor lag had a 2.040-second median and 2.471-second p90 (n=9). Combining this
with the <=0.7-second physical transit yields an observed practical touch-to-
speech-start range of roughly 1.9-2.7 seconds. **Evidence:** n=28 paired records;
n=9 filtered corroborating records; physical trial count not retained.

The firmware performs an immediate non-verbal reflex locally when touched,
independent of the webhook integration. **Evidence:** physical observation during
the lift/touch trials; repeat count was not retained. **Inference:** a cloud
reaction cannot win the first second. Design the cloud voice/motion as a follow-up
to the firmware reflex, not a replacement for it.

Accelerometer semantic kinds are not a reliable description of user intent.
Genuine lifts were sometimes reported as put-down/settle states. **Evidence:**
physical characterization observation; exact event/trial count was not retained
in the required reports. Treat kinds as noisy device states, coalesce related
same-second events, and use cooldowns rather than mapping one kind to one human
gesture with certainty.

### Voice/model latency

For 18 genuine audio messages on the current streamed model path, active bridge
processing ranged from 2.145 to 7.476 seconds, with a 3.853-second median and
7.152-second p90. Time to first BOCCO send completed at a 3.736-second median and
7.088-second p90. Seventeen of 18 first sends were within 370 ms of event
completion. **Evidence:** n=18 streamed voice replies.

The two replies over six seconds were already slow before the first useful
sentence was posted. Queue time for all 18 was only 7-19 ms. The best available
Hermes/pre-call proxy ranged from 1.922 to 7.113 seconds. **Inference supported by
the n=18 decomposition:** the retained outliers are in the model/Hermes-dominant
pre-send segment, not device delivery, TTS length, or the event queue. Existing
telemetry cannot separate provider queueing, model generation, Hermes history or
tools, memory lookup, and sentence buffering inside that segment.

### TLS connection setup

A fresh BOCCO HTTPS call took 148 ms; a kept-alive call took 56 ms. The 93 ms
difference is avoidable per warm sequential call. **Evidence:** reported
fresh-versus-kept-alive benchmark; repetition count was not recorded in the
required reports.

A naïve `urllib.request.urlopen` implementation opens a new connection for each
call; server support does not automatically give a Python caller pooling. Use an
explicit bounded persistent session/pool. For sporadic touch traffic, pooling has
limited benefit unless another request occurred recently: 56 of 60 retained
accel sends were more than 30 seconds after the previous recorded text send.
**Evidence:** n=60 accel sends, 56 idle beyond 30 seconds. Do not add a high-rate
API keepalive merely to save 93 ms.

## Queue architecture trap: priority is not preemption

The measured runtime had one event worker. Priority changed only which pending
event was claimed next; it could not interrupt an event already being processed.

In one contention snapshot, six of 170 accel webhooks (3.5%) arrived while the
worker was busy. One spoken touch reaction waited 10.875 seconds behind a
reaction-bank refresh, then performed its normal 304 ms processing. **Evidence:**
n=170 accel arrivals; n=6 busy arrivals; n=1 spoken collision.

Refresh-duration reports used live snapshots taken at different times:

- One report counted 18 completed refreshes: median 19.447 s, p90 43.778 s,
  maximum 71.712 s.
- Another counted 20 refresh events: median 19.447 s, p90 36.726 s, maximum
  71.712 s; 19 exceeded five seconds.
- Two individual refreshes highlighted during the physical-latency work took
  13.847 and 16.741 seconds.

The count and p90 disagree, while the median and maximum agree. The likely cause
is snapshot/method drift, but that was not proven. The robust conclusion is that
multi-second background jobs are routine and one caused a measured 10.875-second
touch delay.

Implementation rule: do not await long model generation inside a sole global
event worker. Use a separate bounded background generator, or split work into
preemptible units with explicit retry/shutdown state. **Inference:** this changes
tail latency, not the normal 1.9-2.7-second cloud floor.

## What a replicator should assume

1. Custom motion JSON is silent; a 2xx response does not validate `sound.name`.
2. A stamp uses `{"uuid":"..."}`, creates a real chat message, and performs its
   own motion and native sound. Optional `text` adds speech.
3. `immediate=true` on the audio endpoint is **UNTESTED**.
4. API-posted messages echo as the user's identity. Persist and consume returned
   message IDs before calling an AI model.
5. Treat a successful POST as API acceptance, not proof of playback.
6. Do not expect a finish webhook for custom documents or a reliably identifiable
   finish for API-sent preset motions.
7. Use local ingress time for latency and recording correlation; BOCCO event
   timestamps are biased and can be reordered.
8. Budget approximately 1.5 seconds from API acceptance to speech start before
   adding model time. A normal touch voice follow-up cannot feel instantaneous.
9. Make background generation preemptible or independent from user/sensor event
   processing.
10. Preserve the firmware's instant reflex as the first beat; let cloud behavior
    add meaning afterward.

## Evidence conflicts and limits

The following requested claims could not be fully substantiated from the eight
required reports alone and are therefore explicitly scoped above:

- The three custom `sound` trials, stamp wrong-key response, stamp `audio_url`,
  n=20 stamp catalog, n=136 motion catalog, n=332 finish-kind census, preset
  sound-axis breakdown, firmware reflex, and unreliable accel-kind observation
  came from the supplied 2026-08-05 live/static measurement record. The required
  reports either only partially overlap or do not include their raw test logs.
- The YUKAI SDK `payload={"uuid": stamp_id}` cross-check was supplied, but the
  SDK source file is not present beside these documents for independent review.
- Physical touch-to-webhook trials, motion 1-2.5-second delivery range,
  same-sender capture, TLS benchmark, stamp physical behavior, and accel semantic
  misclassification do not retain complete repeat counts. They are reported with
  that limitation rather than assigned invented denominators.

Three source disagreements or interpretation changes must remain visible:

1. `seconds_per_char=0.175` is the current stored/runtime value, but the
   motion/speech analysis says it was influenced by one invalid 1,440-second
   association and prefers the prior 0.150 estimate pending a clean test.
2. Reaction-refresh snapshots report n=18/p90 43.778 s and n=20/p90 36.726 s.
   They agree on the 19.447-second median and 71.712-second maximum.
3. The earlier motion/speech report described its sole
   `weatherMotion_cloudy` event as a recognizable API-preset completion
   candidate. The later n=332 kind census classifies that kind with stock device
   behaviors. Because the event was delayed by about 37 seconds and confounded
   by later batching, it cannot substantiate an API-preset finish callback under
   either interpretation.

No evidence in the required reports contradicts the inert custom `sound` field,
the `uuid` stamp payload, exact-ID echo suppression, the 1.595-second delivery
calibration, or the non-preemptive worker collision. Several of those details are
simply absent from the older reports rather than independently corroborated.

## Source reports

- [Accelerometer voice-reaction latency](accel-voice-latency.md)
- [Voice-mode latency outliers](voice-latency-outliers.md)
- [Missing voice replies](missing-replies.md)
- [Motion/speech concurrency](motion-speech-concurrency.md)
- [Bridge architecture](bridge-architecture.md)
- [Bridge runtime](bridge-runtime.md)
- Functional motion library and Motion Editor MCP behaviour — authoring-side
  notes kept in the separate motion-editor working repository, which is not
  part of this repository.
