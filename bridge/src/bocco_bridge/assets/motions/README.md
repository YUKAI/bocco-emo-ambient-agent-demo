# Packaged custom motions

Six hand-authored motion documents, shipped inside the Python package and sent
to `POST /v1/rooms/{room_uuid}/motions` as a single API call.

## Format

These are BOCCO Motion Editor documents: one JSON object with exactly seven
tracks — `head`, `antenna`, and the five LED tracks (`led_cheek_l`,
`led_cheek_r`, `led_play`, `led_rec`, `led_func`) — plus a `sound` object.

Head segments are cubic Bezier paths with `p0`…`p3`. Endpoints are accepted to
±45° horizontally and ±20° vertically; control points reach ±120°, which is what
makes travel curve and overshoot rather than sliding in a straight line.
`custom_motions.py` holds the exact limits and `validate_motion_document()`
enforces every one of them, so a malformed document fails locally rather than
being silently accepted by the API.

**The `sound` field does nothing.** It is present because the format requires
it, and it is always `{"delay_ms": 0, "name": ""}`. The device neither validates
nor plays a sound named here — three controlled live sends established that,
including one with a genuine device sound name. Custom motion documents are
silent. See `docs/design/bocco-api-findings.md`.

Custom documents also emit no `motion.finished` webhook, unlike preset motions.
The runtime marks them complete after successful delivery and estimates their
duration from the document itself.

## Provenance and licensing

Authored for this repository against the format documented above, using
YUKAI's Motion Editor and its exported JSON as the reference for the schema.
No third-party motion content is vendored here, so these files carry the
repository's own licence (Apache-2.0) with no separate attribution.

## The two groups, and why one is opt-in

| Motion | Group | Reachable from a reply cue? |
|---|---|---|
| `delight-burst` | short | yes — `[motion:delight-burst]` |
| `surprise-realisation` | short | yes |
| `sleepy-drift` | short | yes |
| `morning-awakening` | long | no |
| `celebration-crescendo` | long | no |
| `goodnight-sequence` | long | no |

The short ones are in `CUSTOM_MOTION_CUE_NAMES`, so the model may name them
inline in a reply and the bridge will perform them.

The long ones are deliberately excluded, and a test
(`test_long_packaged_motions_are_not_reply_cues`) keeps them excluded. They run
for many seconds, and the device queues a motion until speech finishes — so a
long motion attached to a conversational reply plays *after* the robot has
stopped talking, as an orphaned performance. They are an **opt-in palette** for
deliberate, non-conversational moments: name one in
`BOCCO_BRIDGE_ACCEL_LIFT_MOTION`, `BOCCO_BRIDGE_ACCEL_BEATEN_MOTION` or
`BOCCO_BRIDGE_ACCEL_SHAKEN_MOTION`, or dispatch one from a tool. They are not dead
weight, but they are not reply material either.

## Authoring another one

Add the JSON file here, register the name in `PACKAGED_MOTION_NAMES` in
`custom_motions.py` (short or long), and run `scripts/test-bridge.sh` —
`test_custom_motions.py` validates every packaged document against the same
limits the runtime enforces at dispatch time.
