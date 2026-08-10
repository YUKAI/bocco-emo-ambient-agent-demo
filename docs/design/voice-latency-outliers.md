# Voice-mode latency outliers

Date: 2026-08-05

## Verdict

The retained evidence supports **Hermes/model-path variance before the first sendable sentence** as the main cause of current voice-response outliers. Confidence is **moderate**, not high: the bridge records total event time and outbound chunk times, but neither the bridge nor Hermes records request start, first SSE delta, tool activity, or model completion time per request.

For the current streamed voice path, 18 genuine audio messages were retained. Active bridge processing ranged from 2.145 to 7.476 seconds, with a 3.853-second median and a 7.152-second nearest-rank p90. After subtracting the measured BOCCO POST time and the observed post-send checkpoint tail, the closest available Hermes-dominant proxy ranged from 1.922 to 7.113 seconds (median 3.629 seconds, p90 6.940 seconds). This proxy still includes the uninstrumented local persona/memory lookup before the Hermes call.

The two retained streamed voice outliers over six seconds were:

- A one-chunk, 12-output-character reply that took 7.152 seconds. Its first BOCCO send completed at 7.088 seconds.
- A two-chunk, 40-output-character reply that took 7.476 seconds. Its first send completed at 7.161 seconds and the second followed only 248 ms later.

These are not queue, device-delivery, TTS-length, fast-route-fallback, or late-chunk cases. They became slow before the first useful sentence reached BOCCO. This localizes the delay to the pre-send Hermes segment, but the existing telemetry cannot separate model/provider variance from Hermes conversation loading, tool selection, or the small bridge-side instruction/memory preparation step.

## Scope and method

This was a read-only analysis of:

- The retained `bocco-bridge.service` and `hermes-api.service` journals.
- `/var/lib/bocco-bridge/state.db`, opened in SQLite read-only mode through `/opt/bocco-bridge/.venv/bin/python`.
- The local bridge source and the existing motion/speech concurrency report.

No API call, robot action, deployment, service restart, configuration change, or commit was made. No message or instruction text was emitted by the analysis; only media types, lengths, timestamps, outcomes, and request identifiers were used.

Journal retention covers 1,433 completed bridge events from 2026-08-03 through the investigation. SQLite contains 28 completed `message.received` events that were marked `audio`, had a non-empty transcript, and delivered a reply. Of those, 18 used the current sentence-streaming model path, eight used an older non-streaming model path, and two were direct fast routes.

The six streamed replies measured earlier today are not all voice samples: SQLite identifies three as `audio` and three as `text`. The voice-only current sample is therefore widened across the retained interval (n=18), while all streamed model replies across audio and app text (n=31) are used as a corroborating sample.

Queue wait is calculated from journal clock timestamps, not `inbound_events.received_at`. The latter is the BOCCO event timestamp and is known to lag real arrival by about two seconds. For each request:

```text
worker_start = journal_event_completed_time - logged_duration_ms
queue_wait   = worker_start - journal_webhook_enqueued_time
```

Percentiles below use nearest rank. Correlations are Pearson correlations and are descriptive only; the samples are too small for causal claims.

## Latency decomposition

The table separates time from webhook ingress to speech start. The previously closed 3–6-second Yukai STT/materialization floor occurs before webhook ingress and is shown for context only.

| Stage | Sample | Median | p90 | Range | Interpretation |
|---|---:|---:|---:|---:|---|
| Yukai recording/STT to transcript webhook | Prior closed investigation | About 3–6 s floor | — | — | Upstream of the bridge; deliberately not reopened. |
| Queue wait, streamed voice model path | 18 | 15.5 ms | 18 ms | 7–19 ms | Negligible in every current streamed voice sample. |
| Active bridge event | 18 | 3.853 s | 7.152 s | 2.145–7.476 s | Contains Hermes, local instruction lookup, BOCCO POSTs, and checkpoints. |
| Hermes + pre-call bridge proxy | 18 | 3.629 s | 6.940 s | 1.922–7.113 s | Active time minus 148 ms per text POST and the measured post-last-send tail. Not an exact Hermes timer. |
| Fresh BOCCO text POST | Existing benchmark | 148 ms/call | — | 1–2 calls observed | Kept-alive benchmark is 56 ms; the in-flight pooling fix saves about 93 ms per call. |
| Post-last-send checkpoint/log tail | 18 | 70 ms | 92 ms | 63–128 ms | Small and stable. |
| BOCCO API completion to device speech anchor | 58 | 1.595 s | — | — | Previously measured; does not materially scale with text length. |
| Audible speaking time | 18 outputs | About 2.98 s at the median 17 chars | About 7.0 s at 40 chars | 9–40 chars | Uses 0.175 s/char. This affects when a reply finishes, not when it starts. |

There were 23 text POSTs across the 18 streamed voice events (1.28 per reply). Connection reuse therefore has an expected mean saving of about 119 ms in this sample: 93 ms for a one-chunk reply and 186 ms for two chunks. It is worthwhile but much smaller than the 3–7-second Hermes-dominant segment.

Using medians from the independently measured stages, a representative current model-path reply starts on the device about 5.35 seconds after the transcript webhook:

```text
0.016 s queue + 3.736 s to first POST completion + 1.595 s device delivery
= about 5.35 s after webhook ingress
```

At the retained first-send p90, the analogous sum is about 8.70 seconds after webhook ingress. Adding the already-characterized 3–6-second STT floor explains user-visible stop-talking-to-speech-start turns of roughly 8–11 seconds typically and roughly 12–15 seconds in the retained tail. These are sums of separate sample summaries, not a measured end-to-end percentile.

## Hypotheses

### 1. Model/Hermes latency variance — supported, with moderate confidence

For streamed voice replies, time until the first BOCCO send completed was n=18, median 3.736 seconds, p90 7.088 seconds, and range 2.070–7.161 seconds. Seventeen of 18 first sends occurred within 370 ms of the whole event completing. The long cases therefore spent almost all their time before any reply chunk was available to the robot.

The Hermes-dominant proxy varies by 3.7x, from 1.922 to 7.113 seconds. User-input length does not explain the spread in this small sample:

- Voice streamed n=18: input-length versus duration `r=-0.189`.
- All streamed model replies n=31: input-length versus duration `r=-0.258`.

The bridge instructions are mostly fixed, and memory injection is capped at 600 characters. A fixed roughly 7.1 KB Hermes prompt can affect the baseline, but by itself cannot explain request-to-request variation. Memory-hit count, final instruction length, conversation-history size, model first-token time, and tool activity are not logged, so their individual effects cannot be tested.

The strongest residual explanation is variance inside Hermes and its upstream model path. “Model path” here deliberately includes provider queueing, conversation/history handling, token generation, and any Hermes tool orchestration. The data does not justify assigning the variance specifically to GPT serving rather than one of those sub-stages.

### 2. Single-worker serialization — real tail risk, not the cause of current retained outliers

The runtime has one event worker. Priority controls what is claimed next but cannot preempt an event already running.

Across all 28 retained transcribed audio replies:

- 27 started 7–29 ms after enqueue.
- One (3.6%) arrived while another event was running and waited 1.028 seconds.
- That historical request arrived near the end of an 8.707-second `accel.compound` reaction and had 1.014 seconds of that work left.
- No retained voice request arrived during a `reaction_bank.refresh`.

The blocked request itself then spent 20.887 seconds actively processing, so serialization contributed about one second to that historical outlier rather than explaining its roughly 22-second total after ingress. The old on-demand accel generation path has since been superseded by reaction banks.

The architectural risk remains substantial. The retained journal contains 20 `reaction_bank.refresh` events: median 19.447 seconds, p90 36.726 seconds, maximum 71.712 seconds, with 19 over five seconds. Each refresh performs multiple sequential Hermes generations. They run at low priority, but a voice message arriving after one has begun must still wait for it. The retained sample simply contains no such collision. There are no retained scheduled briefing fires from which to estimate that path.

Conclusion: serialization did not create the current 7-second voice outliers, but it can create a much larger future outlier at low frequency.

### 3. Sentence streaming and chunk stalls — output length contributes; stalls are not the main tail

The 18 streamed voice replies produced:

| Chunks | n | Median active time | p90 | Max | Median output length |
|---:|---:|---:|---:|---:|---:|
| 1 | 13 | 3.618 s | 4.615 s | 7.152 s | 14 chars |
| 2 | 5 | 4.963 s | 7.476 s | 7.476 s | 38 chars |
| 3 | 0 | — | — | — | — |

Output length and chunk count have moderate positive associations with duration (`r=0.582` and `r=0.483`, respectively), but they are correlated with each other and the sample is small. The 1.345-second difference between the one- and two-chunk medians cannot be attributed to the extra POST: the extra fresh POST costs about 148 ms, only 93 ms of which is avoidable with connection reuse. Most of the difference is generation/output behavior or sample composition.

The five observed inter-chunk gaps were 209, 248, 278, 307, and 2,151 ms. One reply therefore did pause for 2.151 seconds after its first sentence, but its total active time was a moderate 4.290 seconds. Conversely, the slowest two-chunk reply waited 7.161 seconds for its first chunk and only 248 ms for its second. A late chunk can stall, but it did not cause the retained long-tail cases.

Streaming rarely improved time to first speech in this sample: 17 of 18 first sends were within 370 ms of event completion. The `SentenceAssembler` also holds a terminator at the current end of the buffer until another delta arrives or the stream completes, so a one-sentence answer commonly flushes only at `response.completed`. The present data lacks SSE delta timestamps and cannot say whether earlier safe sentence emission would save 50 ms or several seconds.

### 4. Fast-route fallback — ruled out for retained voice outliers

Two retained voice requests matched direct fast routes and completed in 321 and 338 ms (median 329.5 ms). All retained slow streamed voice requests were normal model-path requests under the deployed conservative detector.

There is exactly one retained `fast_route_fallback` journal entry: a five-character app-text weather request failed with `FileNotFoundError` on 2026-08-03 and then completed through the model in 5.745 seconds. It was not a voice request and predates the shared read-only fast-skill installation fix. There are no later fast-route fallbacks and no retained stream fallback/interruption entries.

### 5. BOCCO transport/TTS — ruled out as the source of silence outliers

The measured device delivery lag is about 1.595 seconds over 58 samples and changes only slightly between short and long text. The client’s fresh TLS setup wastes about 93 ms per call, so even three chunks add at most about 279 ms of avoidable setup. Neither can produce the observed multi-second spread before the first send.

Longer text does increase audible speaking duration at 0.175 s/character. That can make the complete turn feel longer, but it does not explain delayed speech onset.

## What the evidence rules out

- Queue wait as the cause of any of the 18 current streamed voice outliers.
- BOCCO device delivery or TTS text-length scaling as the multi-second variable stage.
- A late second/third sentence as the cause of the two retained current outliers over six seconds.
- Silent fast-route failure as the cause of a retained voice outlier.
- Input utterance length as a useful predictor in the retained sample.

It does **not** rule out:

- A future single-worker collision with reaction-bank refresh or a scheduled briefing.
- Hermes tool calls, conversation-history/compression work, provider queueing, or model token-generation variance.
- Memory injection affecting model latency; memory hits and instruction sizes are not recorded.
- Sentence-boundary buffering hiding an earlier model first token.

## Ranked fixes and expected savings

1. **Instrument the Hermes boundary before changing more behavior.** Add redacted numeric timestamps for request start, first SSE delta, first complete sentence, `response.completed`, each BOCCO POST start/end, instruction character count, memory fact count/characters, and Hermes tool count/durations. Propagate the bridge request ID into Hermes logs. Immediate latency saving: **0 seconds**. Value: it is the smallest change that separates model TTFT, token/agent time, tools, memory preparation, sentence buffering, and BOCCO transport instead of treating a 1.9–7.1-second proxy as one stage.

2. **Use the direct fast path for additional genuinely unambiguous intents only.** The retained voice fast path is about 0.330 seconds versus a 3.853-second model median, an observed saving of about **3.5 seconds per eligible request**, and up to roughly seven seconds versus a tail request. This will not help open-ended conversation, and conservatism should remain more important than coverage.

3. **A/B a lower-latency conversational model or a simpler direct conversation path.** The reducible segment has a 3.629-second median and 6.940-second p90. Bringing its p90 down to its current median would save about **3.3 seconds at p90**; actual savings are unknown until a replay/A-B capture because current logs cannot isolate provider/model time from Hermes agent work. Preserve the fixed voice-safety instructions in either path.

4. **Make background generation preemptible between calls or move it off the single event worker.** Split the six reaction-bank keys into separate lowest-priority jobs, or use a dedicated background generator that yields whenever a user event is pending. Do the same for scheduled briefings. Expected saving in the retained voice sample: **0 seconds** because no collision occurred. Tail protection: avoids up to the remaining duration of a refresh, with **71.7 seconds observed** as the worst full refresh. This is a low-frequency, high-severity guard.

5. **Keep the BOCCO connection-pooling fix already in flight.** Expected saving: about **93 ms for one chunk, 186 ms for two, and 119 ms on average** over the 18 retained streamed voice replies. It improves every reply but does not solve outliers.

6. **Tighten spoken output only if product quality permits.** Two-chunk replies were 1.345 seconds slower at the median than one-chunk replies, but only about 148 ms is the extra POST and the groups differ in output length. A stricter one-sentence/character target might save generation time plus one POST, plausibly **hundreds of milliseconds and at most about the observed 1.3-second group difference**, but that estimate is confounded and should be validated rather than assumed.

7. **Revisit sentence-boundary emission after adding SSE timestamps.** Emit a completed `。！？` sentence without waiting for a later delta, using a small punctuation-coalescing rule if needed. One retained reply demonstrates a **2.151-second** gap where streaming helped; for the other 17, the database cannot reveal when the terminator first arrived, so expected saving is presently unquantifiable.

## Smallest capture that separates the remaining hypotheses

Capture 20 additional genuine voice turns, aiming to include at least five active-processing cases over six seconds. Log only numeric metadata and fixed labels, never utterance, persona, memory, tool arguments, or credentials:

```text
request_id
input_chars, instruction_chars, memory_fact_count, memory_chars
hermes_start, first_sse_delta, first_sentence_ready, hermes_completed
output_chars, stream_chunk_count
tool_count and per-tool elapsed_ms (tool name optional; no arguments/results)
bocco_post_start/end for each chunk
worker_enqueue/start/complete and blocker_event_type, if any
```

With that capture:

- Long `hermes_start -> first_sse_delta` identifies provider/model TTFT or Hermes pre-model work.
- Normal TTFT but long `first_delta -> completed` identifies generation, tool loops, or conversation processing.
- Early `first_sentence_ready` but late POST identifies bridge/BOCCO behavior.
- Latency correlated with `memory_chars`, `instruction_chars`, or `tool_count` separates the currently unobservable prompt/memory/tool hypotheses.
- A blocker type and remaining duration directly identifies single-worker serialization.

Until that instrumentation exists, the defensible conclusion is: **the retained current outliers are generated inside the Hermes-dominant pre-send stage, with output/chunk count contributing but not sufficient to explain the tail; the exact sub-cause remains unobserved.**
