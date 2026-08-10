# Motion/speech concurrency: settled by live trial

Date: 2026-08-04 (analysis), 2026-08-05 (live trial)

## Verdict

**BOCCO emo queues a Platform API motion until speech ends. It does not play it concurrently, and it does not mute or truncate speech to play it. This holds for both preset motions and authored custom documents.**

The live trial described under "Live trial" below settles the question the retained-evidence analysis could not. The original inconclusive verdict is preserved beneath it as the reasoning that motivated the trial.

The practical consequence is the opposite of the hypothesis that prompted this work. Speech is never the casualty; the motion is. A motion dispatched during speech is not lost, but it plays **after** the utterance completes, detached from the words it was meant to accompany.

### What this invalidates

- **Mid-speech cue timing is not achievable.** `choreography.cue_offset_seconds` computes a dispatch time intended to land a gesture at a chosen point inside the spoken reply. The device defers it to the end of speech regardless, so the `speech_offset` term cannot do what it was written to do. Timing a motion to a word is not possible through this API.
- **"Motion mutes speech" was wrong.** `BRIDGE_DEFAULT_REPLY_MOTION=false` was deployed against this belief. It remains a reasonable setting, but for a different reason: every reply gesture it suppresses is one that would otherwise have played *after* the reply, as an orphaned movement.
- **Stale thinking motions are a physical consequence, not a scheduling bug.** A `かんがえちゅう` dispatched at `t+2.8 s` plays before the reply only if it reaches the device before the reply's speech does. Once speech begins, the motion is deferred behind it and surfaces afterwards, when the robot has already answered. Tuning the delay cannot fix this; only cancelling a thinking motion that has not yet reached the device can.
- **Chain advancement on `motion.finished` is worse than the earlier analysis assumed.** Because a queued motion's completion can arrive many seconds after dispatch, the window in which an unrelated same-room `motion.finished` can advance the wrong chain (`db.py:1765-1790`) is as wide as the speech that preceded it.

## Live trial

Method as specified under "Smallest live experiment that settles it", using `motion_speech_trial.py`: send one unique utterance, wait for its real `newMessageMotion` webhook, dispatch one motion at that anchor, and record what the robot physically did. Times are JST on 2026-08-05.

| Run | Motion | Path | Dispatched | Utterance | Observed movement | Result |
|---|---|---|---|---|---|---|
| 1 | `GOOD_H_1` | preset | 17:19:28.5, anchor +1.0 s | 82 chars (~12 s) | none distinguishable | Inconclusive; `GOOD_H_1` is a small nod. Contaminated (below). |
| 2 | `BUNBUN` | preset | 17:22:42.3, anchor +0.0 s | 109 chars (~16 s) | head swayed left-right **after** the utterance completed | **Queued** |
| 3 | `ぶんぶん` | custom document | 17:25:37.5, anchor +0.0 s | 109 chars (~16 s) | head swayed left-right **after** the utterance completed | **Queued** |

Runs 2 and 3 dispatched at the speech anchor and did not move the head for roughly fifteen seconds, until speech had finished. Measured device-delivery lag is 1-2.5 s, which does not come close to accounting for the gap. In both runs the utterance was spoken through to completion — the counting `いち` through `じゅう` was intact and the closing sentence was reached — so speech was not truncated, interrupted, or muted.

Run 3 matters independently: custom documents use `POST /v1/rooms/{room}/motions` rather than the preset endpoint and emit no `motion.finished` webhook, so it was an open question whether they behaved differently. They do not. Because `ぶんぶん` exists in both the preset catalog and `CUSTOM_MOTION_DOCUMENTS` as visually equivalent gestures, runs 2 and 3 differ only in API path.

Confidence is **high**. The two positive runs agree with each other, agree across API paths, and agree with the direction of the retained P1 sample (a dispatched preset that completed 37 s later). The effect size is far outside the known sources of timing noise.

### Trial contamination, and a real defect it exposed

All three runs were noisier than designed: the bridge replied to the trial's own injected utterance, so the robot spoke additional messages inside the observation window.

The cause is structural, not a misconfiguration. `BOCCO_AGENT_USER_UUID` is unset, so `config.agent_user_uuid` is `None` and `is_self_echo()` (`bocco/webhook.py:51-56`) can never return true. Suppression rests entirely on hash-matching the bridge's own `outbound_messages` rows within `outbound_echo_window_seconds`, and the trial posted directly through the Platform API without recording an outbound row — so its utterance looked like user speech and was answered.

**Setting `BOCCO_AGENT_USER_UUID` is not the fix, and must not be attempted.** The bridge authenticates with the user's own BOCCO account, so messages it sends carry the *same* sender UUID as messages the human sends. Confirmed live: `sender_uuid` was identical for 「聞こえますか」 (typed by the user) and 「うん、ちゃんと聞こえてるよ。」 (spoken by the bridge). Populating the setting would make `is_self_echo()` discard the user's real messages along with the robot's, which is why outbound-correlation suppression exists in the first place.

The consequence for future work is narrower: any tool that posts to the room outside the bridge will be answered as though a person spoke. That is a property of the trial harness, not a bug in the bridge, and the harness documents it.

This did not change the verdict. The deferral in runs 2 and 3 was observed against the trial's own utterance, anchored on its own `newMessageMotion` webhook, and the extra replies arrived later than the movement would have had to occur under the concurrent hypothesis.

---

## Prior analysis (2026-08-04): why the retained evidence was insufficient

**Inconclusive. The retained journal and database do not establish whether BOCCO emo plays a Platform API motion concurrently with speech or queues it until speech ends. Do not assume concurrency.**

There is one identifiable completion of a bridge-dispatched preset motion. It completed far outside the original reply's estimated speech window, which is queue-like, but it also arrived in the same one-second BOCCO event batch as the delivery animation for a later message. That leaves cloud/device delivery backlog, event batching, and unknown preset duration as confounders. There are zero clean samples containing all of: an observed speech-start anchor, a trustworthy speech-finish anchor, an identifiable dispatched-motion finish, and no intervening outbound speech.

Confidence is **high** that the existing evidence is insufficient and **low** about the underlying device behavior. The single physical-completion sample leans toward serialization/delay, but it is not strong enough to call the device queued.

## Method

The bridge stores the BOCCO webhook payload's Unix timestamp as `received_at` (`bocco/webhook.py:76-82`). Therefore `newMessageMotion`, `motion.finished`, and `emo_talk.finished` can be compared on the same BOCCO event clock; the known roughly two-second lag relative to reality mostly cancels. Journal arrival timestamps were used separately to establish bridge send/arrival order.

For speech without a valid finish event, the inferred window is:

```text
start = newMessageMotion timestamp
end   = start + text_length * 0.150 seconds
```

`0.150 s/character` is the pre-existing estimate. The database's current `0.175` value was not used: it was produced by one bad `emo_talk.finished` association described below. Also, `speech_calibration.sample_count` is not a count of complete speech-duration measurements. Most increments came from delivery anchors (`db.py:1484-1498`), which update delivery lag but leave the speech rate unchanged.

The relevant retained database interval is 2026-08-03 19:00:55 through 2026-08-04 17:58:08 JST. Older journal history does not add correlatable motion-chain/speech rows for this feature.

## Evidence coverage

| Evidence class | Count | Usable for concurrency? | Reason |
|---|---:|---|---|
| Speech observations | 76 | Partly | 39 have `newMessageMotion` anchors. |
| Anchored speech observations | 39 | Start only | No trustworthy paired speech finish. |
| Stored `emo_talk.finished` events | 1 | No | It was associated with a six-character reply from 24 minutes earlier. |
| Motion chains | 53 | Partly | 48 preset/mixed chains and 5 custom-only chains. |
| Claimed preset sends | 49 | Dispatch evidence only | API/bridge completion does not prove device execution time. |
| Anchored speech + preset chain | 18 | No clean finish | These show intended mid-speech dispatches but have no identifiable preset completion webhook. |
| All `motion.finished` events | 262 | Mostly no | 109 are `newMessageMotion`; 152 are automatic recording, accelerometer, radar, or light motions. |
| Recognizable dispatched-preset finish | 1 | Confounded | `weatherMotion_cloudy`; detailed below. |
| Clean joint samples | **0** | **No** | None meet all timing and isolation requirements. |

The 29 `motion_chains.finished_count` claims for preset chains cannot be treated as 29 physical completions. For a preset in flight, the current chain advancement selects the newest active preset chain on **any** same-room `motion.finished` event and does not compare `event_detail` with the expected catalog motion (`db.py:1765-1790`). Automatic events such as `recordFinish` or `accel_lift` can therefore increment a preset chain. Custom motions are different: they emit no `motion.finished` webhook and are marked complete immediately after successful API delivery (`custom_motions.py:3-10`), so they provide no physical finish timestamp at all.

## Timing evidence

All times below are JST.

| Sample | Speech timing | Motion timing | Position versus speech | Interpretation |
|---|---|---|---|---|
| P1, 2026-08-03 19:08 | A 23-character reply was sent at 19:08:15.272. `newMessageMotion` carried timestamp 19:08:15 and arrived at 19:08:16.531. Inferred speech window: about 19:08:15.000-19:08:18.450 on the BOCCO clock. | The preset POST completed at 19:08:16.130. `weatherMotion_cloudy` finished with BOCCO timestamp 19:08:52 and arrived at 19:08:53.559. | Finish was 37.0 s after the speech anchor and about 33.55 s after the inferred end. | Queue-like, but not decisive. A later 40-character message was sent at 19:08:41.876, and its `newMessageMotion` had the same 19:08:52 BOCCO timestamp and arrived only about 31 ms before the preset-finish processing. The cloud/device appears to have delivered or reported both actions as a batch. This cannot isolate queue-behind-speech from general delivery backlog or batching. |
| T1, 2026-08-04 17:53 | The only `emo_talk.finished` was for `どうしたの？`. The database matched it to a six-character observation sent at 17:29:49.078 and anchored at 17:29:53.000. | The stored finish is 17:53:53.000, exactly 1,440 s after the anchor. | Physically impossible for that short utterance. | Excluded. The text-hash lookup found an old identical phrase; this bad sample moved the EWMA rate from 0.150 to 0.175 s/character. |

P1 does show that “the bridge POST returned” is not a safe proxy for “the device has played the motion”: the observable preset completion came roughly 37 seconds later. It does **not** show what the device did during that interval.

## Implication for repeating thinking motions

Let `t=0` be `recording.finished`, with the spoken reply normally beginning around `t+4` to `t+6`.

| Dispatch | Likely physical interval using the measured 1-2.5 s device-delivery delay and a 3.1 s custom document | Risk under concurrent behavior | Risk under queued behavior |
|---|---|---|---|
| `t+1.5 s` | Starts about `t+2.5` to `t+4.0`; ends about `t+5.6` to `t+7.1`. | It can overlap the beginning of the answer's speech/choreography. | If it reaches the device after the reply action, it can be deferred until the robot is idle. |
| `t+4.6 s` | Starts about `t+5.6` to `t+7.1`; ends about `t+8.7` to `t+10.2`. | It almost certainly overlaps the reply. | It is the dangerous case: it may play after the answer, producing visibly stale “thinking.” |

Until the hardware test below is run, the safe product conclusion is that the second dispatch at `t+4.6 s` is **not justified by the evidence**. A pending thinking dispatch should be suppressed once reply delivery begins; even the first dispatch needs a stop/suppression boundary or shorter choreography if overlap with reply choreography is unacceptable.

> **Resolved by the live trial.** The queued column is the real one. The `t+4.6 s` dispatch is not merely unjustified but actively wrong, and the caution about suppressing a pending dispatch once reply delivery begins is now the load-bearing requirement rather than a precaution: any thinking motion still undelivered when speech starts will play after the answer.

## Smallest live experiment that settles it

**Executed 2026-08-05; see "Live trial" above for the result.** The protocol below is retained because it is what was run, and because it is the procedure to repeat if a firmware or Platform API change makes the verdict worth rechecking.

One controlled trial is sufficient if the result is visually and temporally clear:

1. Send a unique Japanese utterance long enough for at least 10-12 seconds of speech (roughly 70-80 characters). The unique text prevents `emo_talk.finished` from matching an older observation.
2. Wait for that message's actual `newMessageMotion` webhook. One second after that anchor, send one known short **preset** motion. Use a preset because custom Motion Editor documents emit no `motion.finished` webhook.
3. Record the robot on video and retain the journal/webhooks through both `motion.finished` and `emo_talk.finished`.
4. Call it **concurrent** if the motion visibly runs while speech continues and its finish lands several seconds before the unique talk finish. Call it **queued** if motion begins/finishes only after the unique talk finish.

Triggering from the observed speech anchor, rather than sending text and motion back-to-back, removes the cloud-delivery-order ambiguity seen in P1. If BOCCO batches the two finish webhooks or the video is ambiguous, repeat the same trial once; otherwise no larger experiment is needed.

## Scope

The 2026-08-04 analysis was read-only against the repository, the `bocco-bridge.service` journal, and `/var/lib/bocco-bridge/state.db`. No robot action, API call, configuration change, deployment, service restart, commit, or token access was performed.

The 2026-08-05 trial sent three utterances and three motions to the robot through the Platform API. It read the token file and `state.db` but wrote to neither, made no configuration change, and did not restart or redeploy the bridge. It refuses to run when the access token is within ten minutes of expiry, because a refresh from a second process would rotate the refresh token out from under the running bridge, which holds its own copy in memory.
