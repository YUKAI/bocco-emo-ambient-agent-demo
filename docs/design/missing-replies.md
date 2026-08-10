# Missing voice replies

Date: 2026-08-05

## Verdict

The retained data contains **no case where the bridge received an audio message with a transcript and then silently failed to send a reply**. At the final snapshot, all 40 such events had `event_effects.bocco_sent=1`; all completed in one worker attempt. Eight additional audio messages arrived without a transcript and all eight received the fixed STT-retry response. Thus, 48 of 48 retained audio-message inputs reached a successful BOCCO text POST checkpoint.

There are, however, two credible ways for the user to experience “no reply” outside that successful message-processing set:

1. **One completed record-button session never produced a genuine audio `message.received` event.** The bridge received `recording.started` and `recording.finished`, sent its acknowledgment media, and received those media echoes, but saw no transcript or failed-STT message and sent no conversational text. This is a confirmed missing-reply episode at the recording/STT-to-webhook boundary.
2. **One streamed reply was accepted by the Platform API but has no delivery evidence.** The POST returned a message ID and the bridge checkpointed the reply, but no exact `message.received` echo and no nearby `newMessageMotion` event exist. This is a delivery-gap candidate, not proof that the device stayed silent, because neither signal is a guaranteed device-playback receipt.

The deliberate error-silence policy did **not** cause a retained conversational miss. In the current code, conversational Hermes failures are retried and then converted to a fixed apology; ambient failures remain silent. A message can still become truly silent if all BOCCO text-send attempts fail and the worker dead-letters it, but no such event exists in the retained data.

Confidence is **high** about the zero bridge-processing failures and the one missing transcript webhook, and **moderate** about the outbound delivery gap. The platform does not expose a definitive “robot spoke this message” receipt.

## Snapshot and method

Final snapshot: 2026-08-05 05:03:08 UTC (14:03:08 JST).

Retained bridge journal: 2026-08-03 07:58:23 UTC through 2026-08-05 05:02:04 UTC. SQLite contained 1,752 events, all in `completed` state.

The analysis reused the method in `voice-latency-outliers.md`:

- Read-only `bocco-bridge.service` journal parsing.
- `/var/lib/bocco-bridge/state.db` opened with SQLite `mode=ro` through `/opt/bocco-bridge/.venv/bin/python`.
- Correlation by request ID, media type, timestamps, terminal outcome, worker attempts, `bocco_sent`, outbound message-ID presence, and fixed event kinds.
- No message, model output, persona, memory, token, or secret content was printed or stored in this report.

No API call, robot action, deployment, service restart, source edit, or commit was performed.

## Every terminal path for a speech-bearing message

The following paths exist in `events.py`. “Speech-bearing” means `speech_text` is present and non-whitespace after parsing.

| Terminal path | Sends conversational text? | Notes |
|---|---|---|
| `missing_room` | No | A valid `message.received` cannot normally reach this because the parser rejects a missing room first. |
| `self_echo_message_id` | No | Correctly suppresses an exact outbound message-ID echo. |
| `self_echo_content_hash` | No | Fallback only for an outbound record without a usable returned ID; consumes one match. |
| `self_echo_sender_uuid` | No | Optional legacy guard. It is dangerous with a shared user UUID but produced no retained outcomes. |
| Persona command outcomes | Yes | Changed, preset-changed, reset, and over-length rejection all use the normal text-delivery path. |
| Schedule command outcomes | Yes | Add, list, remove, and invalid-command responses use normal delivery. |
| Memory command outcomes | Yes | Remember, forget, list, invalid, and over-length responses use normal delivery. |
| `speech_replied_streamed` | Yes | At least one stream chunk was posted and durably recorded. A later stream interruption preserves the already-delivered chunks. |
| `speech_replied` | Yes | Covers non-streamed model replies, direct fast routes, STT fallback, and the fixed apology after final conversational failure. |
| Worker `dead_letter` after three failed attempts | Not guaranteed | There is no `event_completed` outcome. If the text POST never succeeded, this is a genuine silent failure. None occurred. |

`non_speech_message` is a deliberate silent terminal outcome for non-audio media without usable text, but by definition it is not a speech-bearing event. An audio event without usable speech instead receives the fixed STT-retry response.

There is no conversational cooldown in `_process_message`; radar, accel, and illuminance cooldowns cannot suppress a user message.

Fast-route failure is not terminal. It logs `fast_route_fallback` and continues through Hermes. Empty, whitespace, or upstream-error-shaped Hermes output is also not terminal: it raises into the bounded retry policy, and the final conversational attempt uses the fixed apology.

Acknowledgment audio/motion failures run in background tasks and do not abort the text reply. Choreography motion failure is also non-critical. In the streamed path, the chunk is recorded before choreography setup; in the single-message path, BOCCO text delivery is recorded before the motion chain is created.

## Observed terminal outcomes

There were 271 retained speech-bearing `message.received` events:

| Media | Outcome | `bocco_sent` | Count | Classification |
|---|---|---:|---:|---|
| Audio | `speech_replied_streamed` | Yes | 30 | Delivered user reply. |
| Audio | `speech_replied` | Yes | 10 | Delivered user reply. |
| Text | `speech_replied_streamed` | Yes | 20 | Delivered app-text reply. |
| Text | `speech_replied` | Yes | 7 | Delivered app-text/fast response. |
| Text | Persona command outcomes | Yes | 3 | Delivered configuration confirmation. |
| Legacy/no media marker | `speech_replied` | Yes | 7 | Delivered reply. |
| Text | `self_echo_message_id` | No | 180 | Correct suppression of bridge output echoes. |
| Legacy/no media marker | `self_echo_message_id` | No | 14 | Correct suppression of older bridge output echoes. |

Therefore:

- Delivered speech-bearing messages: 77.
- Correctly suppressed speech-bearing outbound echoes: 194.
- Speech-bearing audio messages silently terminated by the bridge: **0 of 40**.
- Dead-lettered speech-bearing messages: **0**.
- Conversational cooldown suppressions: **0**.

The broader message set also contains non-speech media that correctly completed silently; these should not be counted as unanswered utterances.

## Voice-input to reply gap

### Audio messages

The retained bridge received 48 audio inputs that required a conversational response:

- 40 carried transcripts; all 40 posted replies.
- 8 carried no usable transcript; all 8 posted the fixed STT-retry response.
- All 48 completed on attempt 1.
- No audio input had `bocco_sent=0`.

This rules out Hermes failure, empty output, BOCCO send exhaustion, worker retry exhaustion, and echo suppression **after a genuine audio input reached the message handler** for the retained sample.

### Recording telemetry

Excluding one explicitly synthetic recording test row, the database contains:

- 48 `recording.started` events.
- 41 `recording.finished` events.
- 40 finished recordings manually paired one-to-one with a genuine audio input within ±15 seconds.
- 1 finished recording with no genuine audio input and no outbound conversational text within 30 seconds.

The unmatched finished recording is request `688c3224` at 2026-08-05 03:46:42 UTC. It followed a record-button start 17 seconds earlier. Six seconds after finish, the bridge received an exact echo of its own acknowledgment audio, followed by motion/stamp echoes, but no genuine transcript or failed-STT message. The conversational handler had nothing to process.

This is the clearest retained explanation for a user speaking and receiving no answer: **the Platform never delivered the input message event that would trigger either an AI response or the bridge’s STT fallback**. The silence is not a deliberate conversational error policy; it is an unhandled absence of the trigger event.

The difference between 48 starts and 41 finishes is not counted as seven additional missing replies. A start alone cannot prove that the user completed an utterance; it may represent an aborted recording, missing telemetry, or retained test activity.

### Why the correlation table reports only five links

`recording_message_correlations` contains only five rows, all of which point to sent replies. That is a severe undercount, not evidence of 36 missing replies.

Manual timestamp matching found 40 pairs:

- In 29, the audio-message event timestamp was 1–6 seconds **before** `recording.finished`.
- In 11, both had the same whole-second timestamp.
- None had a later audio-message timestamp.

The current durable correlator only searches for an already-completed `recording.finished` whose platform timestamp is at or before the audio message. It cannot link the common reverse-order case, and same-second worker ordering can also prevent a link. This is an observability/early-ack correlation defect, but it does not prevent `_process_message` from replying.

## Outbound delivery evidence

Old outbound correlation records are pruned, so this check covers the retained current subset rather than all 48 voice inputs:

- 38 voice reply source events retained outbound records.
- Those replies contained 44 text chunks, all with returned message IDs.
- 43 of 44 chunks received an exact message-ID echo.
- 43 of 44 had a nearby `newMessageMotion` event within the timing window.
- One chunk, from source request `07e7defd`, had neither signal.

For `07e7defd`, the bridge received a 12-character audio transcript, completed a streamed reply in 2.596 seconds, received a message ID from the text POST, and set `bocco_sent=1`. More than an hour of later events is retained, yet the exact reply echo and a nearby `newMessageMotion` are absent.

This is a **genuine delivery-confidence defect**: `bocco_sent` currently means “the Platform API accepted and the bridge checkpointed the POST,” not “the robot started speaking.” It may be the reported missing reply. It is not possible to prove device silence from the available telemetry because webhook echo delivery and `newMessageMotion` are themselves best-effort observations.

## Swallowed exceptions and warnings

Every warning path in `events.py` was counted by label and `error_type`:

| Warning | Error type | Count | Related to missing voice reply? |
|---|---|---:|---|
| `reaction_bank_refresh_skipped` | `ValueError` | 57 | No. Background bank parsing/generation; existing/default reactions remain. |
| `reaction_bank_refresh_skipped` | `HermesAPIError` | 6 | No. Background startup/persona refresh; not a user conversation. |
| `fast_route_fallback` | `FileNotFoundError` | 1 | No. One app-text request fell through to Hermes and delivered a reply. |

Retained counts for all of these were zero:

- `stream_fallback`
- `stream_interrupted`
- `ack_motion_skipped`
- `ack_audio_skipped`
- `thinking_motion_skipped`
- `motion_delivery_skipped`
- `motion_chain_abandon_failed`
- `illuminance_generation_skipped`
- `scheduled_generation_skipped`
- worker `event_failed`

There is consequently no retained conversational Hermes error, timeout, empty output, BOCCO text-send exception, or post-send motion error to correlate with a missing audio reply.

## Worker retries and durability

At the snapshot:

- All 1,752 database events were `completed`.
- No event was pending, processing, or dead-lettered.
- No `event_failed` journal entry was retained.
- All 48 voice inputs had `attempts=1`.
- One illuminance event had `attempts=2`; it was completed and unrelated to voice.

The single worker can delay a voice message behind in-flight background generation, as documented in `voice-latency-outliers.md`, but it does not drop claimed work. There is no evidence that a retained user message was deferred until uselessness or exhausted the three-attempt limit.

## Echo-suppression audit

Across all retained message types, 403 events ended as `self_echo_message_id`:

- 180 text echoes with text.
- 14 legacy/no-marker echoes with text.
- 178 audio-media echoes without transcripts.
- 25 motion echoes without transcripts.
- 6 stamp echoes without transcripts.

Critical findings:

- Audio messages with transcripts suppressed as echoes: **0**.
- `self_echo_content_hash` outcomes: **0**.
- `self_echo_sender_uuid` outcomes: **0**.
- Every observed suppression used an exact returned outbound message ID.

The audio echo count is high because bridge-sent acknowledgment audio returns as `message.received`; those events have no transcript and exact outbound IDs. There is no retained evidence that a human utterance was discarded by the shared sender UUID or content matching.

## Silent cases ranked by frequency

### User-visible missing-reply candidates

| Rank | Cause | Retained count | Denominator | Confidence | Defect? |
|---:|---|---:|---:|---|---|
| 1 | Finished recording produced no genuine audio message webhook | 1 | 41 non-synthetic finished recordings | High | Yes: upstream trigger absence is not covered by a timeout fallback. |
| 2 | Reply POST accepted but neither exact echo nor device-motion anchor observed | 1 | 38 voice reply sources with retained outbound correlation | Moderate | Yes: API acceptance is treated as final delivery without confirmation. |
| 3 | Speech-bearing audio reached bridge but no reply POST | 0 | 40 transcribed audio events | High | Would be a bridge defect; not observed. |
| 4 | Echo suppression misclassified genuine transcribed audio | 0 | 40 transcribed audio events | High | Would be critical; not observed. |
| 5 | Worker retry/dead-letter exhaustion | 0 | 48 voice inputs | High | Would be a bridge defect; not observed. |
| 6 | Hermes/empty-output failure ended silently | 0 | 48 voice inputs | High for retained journal | Current policy would normally send the fixed apology. |

The two positive counts are separate incidents. They may or may not correspond to the user’s remembered reports; without a user-provided incident timestamp, that identity cannot be established.

### Correct silence that should not be treated as a missing reply

- 403 exact message-ID echoes, including 194 with text, were correctly suppressed.
- Non-speech motion/stamp/media messages completed silently by design.
- Ambient reaction-bank generation failures kept old/default reactions and did not speak errors.
- Radar/accel/illuminance cooldown outcomes are intentional and do not apply to conversational messages.

## Ranked fixes

1. **Add a durable no-transcript watchdog after `recording.finished`.** After roughly 15–20 seconds, check whether the room has a genuine audio input or conversational text reply associated with the recording. If not, send the existing fixed STT-retry response once. The check must be bidirectional because 29 of 40 matched audio events had platform timestamps before `recording.finished`. Expected retained impact: covers the one confirmed missing session (1/41).

2. **Separate API acceptance from delivery confirmation.** Store `api_accepted_at`, exact-echo time, and `new_message_motion_at` for every outbound message ID. Emit a high-signal `reply_delivery_unconfirmed` warning when both confirmations are absent after a bounded window. Consider one idempotency-aware retry only after evaluating duplicate-speech risk. Expected retained detection: one of 38 current voice reply sources.

3. **Fix recording correlation using local ingress time and reverse-order matching.** Add an immutable `ingested_at` timestamp and correlate the nearest unused recording/audio event by room within a bounded absolute window. The current one-direction query recorded 5 links where metadata supports 40. This mainly fixes telemetry, early-ack suppression, and the watchdog’s safety checks rather than reply generation itself.

4. **Record a content-free reply terminal reason.** Distinguish `model_reply`, `fast_reply`, `stt_fallback`, `error_apology`, `partial_stream`, and `send_dead_letter` in durable state. Currently several all appear as `speech_replied`, so a future failure can only be reconstructed from rotated journals.

5. **Alert on conversational retry/dead-letter immediately.** A `message.received` retry, final apology, or dead letter should produce a fixed-label metric/journal event and readiness degradation without exposing text. The current retained count is zero, but this closes the real error-silence path if BOCCO text delivery fails three times.

6. **Keep echo suppression exact-ID-first and add an invariant alarm.** If an event with `media=audio` and a transcript is ever classified as self-echo, log a content-free critical invariant violation with request ID and strategy. Consider disallowing content-hash/sender suppression for transcribed audio entirely. The retained misclassification count is zero.

## Smallest instrumentation needed for the next incident

The minimum content-free capture is:

```text
recording request_id, room, ingested_at, platform timestamp, started/finished
message request_id, room, media, has_transcript, ingested_at, platform timestamp
recording correlation id and direction (message-before-finish or after-finish)
reply kind, attempt number, failure stage, fixed error_type
outbound message id present, api_accepted_at, exact_echo_at, new_message_motion_at
terminal status: delivered, delivery_unconfirmed, no_transcript_timeout, dead_letter
```

This would distinguish, without retaining speech:

- No transcript webhook after a completed recording.
- A bridge/Hermes failure before POST.
- A BOCCO POST failure.
- API acceptance without later delivery evidence.
- Genuine echo suppression and its exact strategy.

Until then, the defensible conclusion is: **the bridge replied to every voice message it actually received, but one completed recording never became a message and one API-accepted reply lacks downstream evidence. Those boundary gaps, rather than the conversational error-silence policy, best explain the retained “no reply” cases.**
