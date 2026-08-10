"""Named custom-motion documents and composite preset chains.

Each named motion is one Platform API call: a Motion Editor-format JSON
document (POST /v1/rooms/{room}/motions) animating the head, the bonbori
antenna, and the cheek LEDs together. Timings were tuned against the live
characterization of 2026-08-04: the API answers in ~0.2s, the device
receives a motion ~1-2.5s later, plays the whole document by itself, and
emits no motion.finished webhook (only preset motions do). Chain entries
holding these documents are therefore finished explicitly after a successful
API delivery (see db.py).

Composite chains reuse the existing preset machinery: each element is a
preset family prefix resolved through the motion catalog, and the chain
advances on the motion.finished webhooks that presets do emit.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, Mapping


MIN_HEAD_ANGLE, MAX_HEAD_ANGLE = -45, 45
MIN_VERTICAL_ANGLE, MAX_VERTICAL_ANGLE = -20, 20
MIN_CONTROL_ANGLE, MAX_CONTROL_ANGLE = -120, 120
MAX_TRANSITION_MILLISECONDS = 10_000
MAX_MOTION_MILLISECONDS = 10_000
MAX_HEAD_TRANSITIONS = 35
MAX_LED_TRANSITIONS = 35
MAX_ANTENNA_TRANSITIONS = 15
MAX_ANTENNA_FREQUENCY = 20
LED_TRACKS = ("led_cheek_l", "led_cheek_r", "led_play", "led_rec", "led_func")
MOTION_TRACKS = ("head", "antenna", *LED_TRACKS)

CUSTOM_MOTION_TOKEN_PREFIX = "custom:"
# Invented motions are stored as a choreography spec, not as a document, so
# their chain token carries the repertoire row id and the document is rendered
# at dispatch time.  They are still self-contained documents as far as the
# chain machinery is concerned: no motion.finished webhook ever arrives.
INVENTED_MOTION_TOKEN_PREFIX = "invented:"

SHORT_PACKAGED_MOTION_NAMES = frozenset(
    {"delight-burst", "surprise-realisation", "sleepy-drift"}
)
LONG_PACKAGED_MOTION_NAMES = frozenset(
    {
        "morning-awakening",
        "celebration-crescendo",
        "goodnight-sequence",
    }
)
PACKAGED_MOTION_NAMES = SHORT_PACKAGED_MOTION_NAMES | LONG_PACKAGED_MOTION_NAMES
PACKAGED_MOTION_FILES = {
    name: f"{name}.json" for name in PACKAGED_MOTION_NAMES
}

_LINEAR = [0.0, 0.0, 1.0, 1.0]
_SMOOTH = [0.5, 0.0, 0.5, 1.0]


def _head(
    duration: int,
    target: tuple[int, int] | None,
    *,
    ease: list[float] = _LINEAR,
) -> dict[str, Any]:
    """One head transition; ``None`` target keeps the previous posture."""

    point = [None, None] if target is None else [target[0], target[1]]
    return {
        "duration": duration,
        "p0": [None, None],
        "p1": [None, None],
        "p2": list(point),
        "p3": list(point),
        "ease": list(ease),
    }


def _sway(duration: int, start_pos: float, end_pos: float) -> dict[str, Any]:
    """One phase-mode antenna transition (slow deliberate bonbori posture)."""

    return {
        "duration": duration,
        "start": {"amp": None, "freq": None, "pos": start_pos},
        "end": {"amp": None, "freq": None, "pos": end_pos},
    }


def _shake(
    duration: int,
    start: tuple[float, int],
    end: tuple[float, int] | None = None,
) -> dict[str, Any]:
    """One amplitude/frequency-mode antenna transition (vibration)."""

    end_amp, end_freq = start if end is None else end
    return {
        "duration": duration,
        "start": {"amp": start[0], "freq": start[1], "pos": None},
        "end": {"amp": end_amp, "freq": end_freq, "pos": None},
    }


def _led(
    duration: int,
    end: tuple[int, int, int, int] | None,
    *,
    ease: list[float] = _LINEAR,
) -> dict[str, Any]:
    """One LED transition from the previous color to ``end`` (None = off)."""

    color = [0, 0, 0, 0] if end is None else list(end)
    return {
        "duration": duration,
        "start": [None, None, None, None],
        "end": color,
        "ease": list(ease),
    }


def _document(
    *,
    head: list[dict[str, Any]],
    antenna: list[dict[str, Any]],
    cheeks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cheek_track = cheeks or []
    return {
        "head": head,
        "antenna": antenna,
        "led_cheek_l": [dict(item) for item in cheek_track],
        "led_cheek_r": [dict(item) for item in cheek_track],
        "led_play": [],
        "led_rec": [],
        "led_func": [],
    }


CUSTOM_MOTION_DOCUMENTS: dict[str, dict[str, Any]] = {
    # Curious look-around: right, hold, left, hold, back; ears flutter
    # lightly; cheeks glow warm amber. ~2.9s of head travel.
    "きょろきょろ": _document(
        head=[
            _head(200, None),
            _head(500, (30, 8), ease=_SMOOTH),
            _head(500, (30, 8)),
            _head(600, (-30, 8), ease=_SMOOTH),
            _head(500, (-30, 8)),
            _head(600, (0, 0), ease=_SMOOTH),
        ],
        antenna=[
            _shake(1400, (0.3, 5)),
            _shake(1500, (0.15, 3), (0.0, 2)),
        ],
        cheeks=[
            _led(700, (255, 170, 60, 180)),
            _led(1500, (255, 170, 60, 180)),
            _led(700, None),
        ],
    ),
    # Slow droop, hold the sad posture, slow recovery; bonbori sags all
    # the way down and cheeks fade to a dim blue. ~4.3s.
    "しょんぼり": _document(
        head=[
            _head(200, None),
            _head(900, (0, -20), ease=_SMOOTH),
            _head(1800, (0, -20)),
            _head(1400, (0, 0), ease=_SMOOTH),
        ],
        antenna=[
            _sway(900, 0.0, -1.0),
            _sway(1800, -1.0, -1.0),
            _sway(1400, -1.0, 0.0),
        ],
        cheeks=[
            _led(900, (60, 90, 255, 110)),
            _led(1800, (60, 90, 255, 110)),
            _led(1500, None),
        ],
    ),
    # Bashful: look away and down, pause, peek back partway, then return;
    # pink blush and a shy half-lowered bonbori. ~3.9s.
    "てれてれ": _document(
        head=[
            _head(200, None),
            _head(600, (35, -10), ease=_SMOOTH),
            _head(1400, (35, -10)),
            _head(500, (18, -4), ease=_SMOOTH),
            _head(500, (18, -4)),
            _head(700, (0, 0), ease=_SMOOTH),
        ],
        antenna=[
            _sway(700, 0.0, -0.4),
            _sway(1600, -0.4, -0.4),
            _sway(1600, -0.4, 0.0),
        ],
        cheeks=[
            _led(600, (255, 80, 110, 220)),
            _led(2200, (255, 80, 110, 220)),
            _led(1100, None),
        ],
    ),
    # Vigorous no-no shakes with an energetic bonbori buzz. ~1.75s.
    "ぶんぶん": _document(
        head=[
            _head(150, None),
            _head(300, (25, 0)),
            _head(350, (-25, 0)),
            _head(350, (25, 0)),
            _head(300, (-15, 0)),
            _head(300, (0, 0)),
        ],
        antenna=[
            _shake(1200, (0.8, 10)),
            _shake(600, (0.3, 6), (0.0, 3)),
        ],
    ),
    # Considering: head drifts up and away, hangs there weighing it, drifts
    # back across, then eases home.  ~2.0s.
    #
    # Length is set by what it has to fit inside, and that has moved twice.
    # It was 3.1s, stretched to 4.2s when replies took 8.6s and ending early
    # left a visible dead beat.  Replies are now 2-4s, so a 4.2s gesture
    # outlives the wait it was filling and runs into the answer.  Motions also
    # take 1-2.5s to reach the device, which is a large fraction of that gap,
    # so some overlap is unavoidable; a shorter document simply spends less
    # time overlapping.  Retune this if reply latency moves again — the
    # measurement, not the shape, is what fixes the value.
    #
    # Still lands at neutral, per the documents-end-at-neutral invariant: a
    # reply can legitimately never arrive under the error-silence policy, and
    # emo must not be left holding a posture.
    "かんがえちゅう": _document(
        head=[
            _head(150, None),
            _head(450, (-16, 11), ease=_SMOOTH),
            _head(400, (-16, 11)),
            _head(500, (12, 8), ease=_SMOOTH),
            _head(500, (0, 0), ease=_SMOOTH),
        ],
        antenna=[
            _sway(700, 0.0, 0.3),
            _sway(700, 0.3, -0.15),
            _sway(600, -0.15, 0.0),
        ],
        cheeks=[
            _led(600, (120, 150, 200, 90)),
            _led(800, (120, 150, 200, 40)),
            _led(600, None),
        ],
    ),
    # Recentre everything: head to neutral, bonbori stilled, cheeks off.
    "まっすぐ": _document(
        head=[
            _head(200, None),
            _head(600, (0, 0), ease=_SMOOTH),
        ],
        antenna=[_shake(500, (0.0, 0))],
        cheeks=[_led(500, None)],
    ),
}

_HAND_WRITTEN_CUSTOM_MOTION_NAMES = frozenset(CUSTOM_MOTION_DOCUMENTS)

# A motion document that commands nothing, used only to prove that BOCCO is
# still delivering webhooks (see delivery.py).  Deliberately NOT a member of
# CUSTOM_MOTION_DOCUMENTS: it must never become a cue name a model can emit or
# a person can ask for, because it is a diagnostic, not a performance.
#
# Why this exact shape is believed silent and invisible:
#   * The single head transition is `_head(duration, None)` — every point null,
#     which the device reads as "hold the posture you already have".  It is the
#     same lead-in element that opens all four hand-written documents above and
#     has therefore been played on the live robot many times without any
#     observable head movement of its own.
#   * Every other track is empty.  led_play/led_rec/led_func are already empty
#     in every shipped document, and `_document(cheeks=None)` already ships
#     empty cheek tracks, so an empty list is a proven-accepted track value.
#   * Custom documents carry no audio at all: the `sound` key is inert in the
#     tested firmware (three controlled live sends, including a real device
#     sound name, all produced silence), and this document does not even set
#     it.  See docs/design/bocco-api-findings.md.
#
# What it costs: one POST /v1/rooms/{room}/motions, which echoes back as a
# `message.received` with media=motion.  That echo is the signal.  The message
# does appear in the room's chat history in the official app, which is the one
# side effect that cannot be avoided by any probe that uses the messaging API.
SILENT_PROBE_HOLD_MILLISECONDS = 200


def silent_probe_document(
    duration_milliseconds: int = SILENT_PROBE_HOLD_MILLISECONDS,
) -> dict[str, Any]:
    """Build the no-op document used to probe Webhook delivery.

    Returned fresh each call so a caller cannot mutate a shared constant into
    something that moves.
    """

    if (
        isinstance(duration_milliseconds, bool)
        or not isinstance(duration_milliseconds, int)
        or not 0 < duration_milliseconds <= MAX_TRANSITION_MILLISECONDS
    ):
        raise ValueError(
            f"probe duration must be 1..{MAX_TRANSITION_MILLISECONDS}ms"
        )
    return _document(head=[_head(duration_milliseconds, None)], antenna=[])


COMPOSITE_PRESET_CHAINS: dict[str, tuple[str, ...]] = {
    "びっくりよろこび": ("WHAT", "YES", "GOOD"),
    "納得": ("ALRIGHT", "GOOD"),
}


def custom_motion_token(name: str) -> str:
    """Encode a named document as a chain token stored beside preset uuids."""

    if name not in CUSTOM_MOTION_DOCUMENTS:
        raise KeyError(f"unknown custom motion: {name}")
    return CUSTOM_MOTION_TOKEN_PREFIX + name


def parse_custom_motion_token(token: str) -> str | None:
    """Return the motion name for a custom chain token, else ``None``."""

    if not isinstance(token, str) or not token.startswith(
        CUSTOM_MOTION_TOKEN_PREFIX
    ):
        return None
    name = token[len(CUSTOM_MOTION_TOKEN_PREFIX) :]
    return name if name in CUSTOM_MOTION_DOCUMENTS else None


def invented_motion_token(motion_id: int) -> str:
    """Encode a remembered invented motion as a chain token."""

    if isinstance(motion_id, bool) or not isinstance(motion_id, int) or motion_id < 1:
        raise ValueError("invented motion ids must be positive integers")
    return f"{INVENTED_MOTION_TOKEN_PREFIX}{motion_id}"


def parse_invented_motion_token(token: str) -> int | None:
    """Return the repertoire row id for an invented token, else ``None``."""

    if not isinstance(token, str) or not token.startswith(
        INVENTED_MOTION_TOKEN_PREFIX
    ):
        return None
    raw = token[len(INVENTED_MOTION_TOKEN_PREFIX) :]
    if not raw.isdigit():
        return None
    motion_id = int(raw)
    return motion_id if motion_id > 0 else None


def is_custom_document_token(token: str) -> bool:
    """True for any token whose motion is a document the bridge itself sends.

    Both named documents and invented ones are delivered as a full document in
    one API call and emit no ``motion.finished`` webhook, so the chain has to
    finish them explicitly.
    """

    return (
        parse_custom_motion_token(token) is not None
        or parse_invented_motion_token(token) is not None
    )


def motion_duration_seconds(name: str) -> float:
    """Wall-clock length of a named document (longest element track)."""

    document = CUSTOM_MOTION_DOCUMENTS[name]
    return (
        max(
            sum(transition["duration"] for transition in document[track])
            for track in MOTION_TRACKS
        )
        / 1000.0
    )


def validate_motion_document(document: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless ``document`` satisfies the API motion spec."""

    if not isinstance(document, Mapping):
        raise ValueError("motion document must be a mapping")
    if set(document) != set(MOTION_TRACKS):
        raise ValueError("motion document must define exactly the seven tracks")
    for track in MOTION_TRACKS:
        transitions = document[track]
        if not isinstance(transitions, list):
            raise ValueError(f"{track} must be a list")
        if track == "antenna":
            limit = MAX_ANTENNA_TRANSITIONS
        elif track in LED_TRACKS:
            limit = MAX_LED_TRANSITIONS
        else:
            limit = MAX_HEAD_TRANSITIONS
        if len(transitions) > limit:
            raise ValueError(f"{track} exceeds {limit} transitions")
        total = 0
        for transition in transitions:
            total += _validate_transition(track, transition)
        if total > MAX_MOTION_MILLISECONDS:
            raise ValueError(f"{track} exceeds {MAX_MOTION_MILLISECONDS}ms total")


def _validate_transition(track: str, transition: object) -> int:
    if not isinstance(transition, Mapping):
        raise ValueError(f"{track} transitions must be mappings")
    duration = transition.get("duration")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 0 <= duration <= MAX_TRANSITION_MILLISECONDS
    ):
        raise ValueError(f"{track} durations must be 0..{MAX_TRANSITION_MILLISECONDS}ms")
    if track == "head":
        _validate_head_transition(transition)
    elif track == "antenna":
        _validate_antenna_transition(transition)
    else:
        _validate_led_transition(track, transition)
    return duration


def _validate_head_transition(transition: Mapping[str, Any]) -> None:
    _validate_ease(transition.get("ease"), "head")
    for endpoint in ("p0", "p3"):
        _validate_head_point(
            transition.get(endpoint),
            endpoint,
            (MIN_HEAD_ANGLE, MAX_HEAD_ANGLE),
            (MIN_VERTICAL_ANGLE, MAX_VERTICAL_ANGLE),
        )
    for control in ("p1", "p2"):
        _validate_head_point(
            transition.get(control),
            control,
            (MIN_CONTROL_ANGLE, MAX_CONTROL_ANGLE),
            (MIN_CONTROL_ANGLE, MAX_CONTROL_ANGLE),
        )


def _validate_head_point(
    point: object,
    name: str,
    horizontal_range: tuple[int, int],
    vertical_range: tuple[int, int],
) -> None:
    if not isinstance(point, list) or len(point) != 2:
        raise ValueError(f"head {name} must be a two-item list")
    for value, (minimum, maximum) in zip(point, (horizontal_range, vertical_range)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"head {name} angles must be numbers or null")
        if not minimum <= value <= maximum:
            raise ValueError(
                f"head {name} angles must be within {minimum}..{maximum}"
            )


def _validate_antenna_transition(transition: Mapping[str, Any]) -> None:
    start = transition.get("start")
    end = transition.get("end")
    for name, params in (("start", start), ("end", end)):
        if not isinstance(params, Mapping):
            raise ValueError(f"antenna {name} must be a mapping")
        amp = params.get("amp")
        if amp is not None and (
            isinstance(amp, bool)
            or not isinstance(amp, (int, float))
            or not 0 <= amp <= 1
        ):
            raise ValueError("antenna amp must be null or within 0..1")
        freq = params.get("freq")
        if freq is not None and (
            isinstance(freq, bool)
            or not isinstance(freq, int)
            or not 0 <= freq <= MAX_ANTENNA_FREQUENCY
        ):
            raise ValueError(
                "antenna freq must be null or an integer within "
                f"0..{MAX_ANTENNA_FREQUENCY}"
            )
        pos = params.get("pos")
        if pos is not None and (
            isinstance(pos, bool)
            or not isinstance(pos, (int, float))
            or not -1 <= pos <= 1
        ):
            raise ValueError("antenna pos must be null or within -1..1")
    if isinstance(start, Mapping) and isinstance(end, Mapping):
        start_mode = start.get("pos") is not None
        end_mode = end.get("pos") is not None
        if start_mode != end_mode:
            raise ValueError("antenna start and end must use the same mode")


def _validate_led_transition(track: str, transition: Mapping[str, Any]) -> None:
    _validate_ease(transition.get("ease"), track)
    for name in ("start", "end"):
        channels = transition.get(name)
        if not isinstance(channels, list) or len(channels) != 4:
            raise ValueError(f"{track} {name} must be a four-item RGBA list")
        for channel in channels:
            if channel is None:
                continue
            if (
                isinstance(channel, bool)
                or not isinstance(channel, int)
                or not 0 <= channel <= 255
            ):
                raise ValueError(f"{track} channels must be null or 0..255")


def _validate_ease(ease: object, track: str) -> None:
    if not isinstance(ease, list) or len(ease) != 4:
        raise ValueError(f"{track} ease must be a four-item list")
    for value in ease:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
        ):
            raise ValueError(f"{track} ease values must be within 0..1")


def _load_packaged_motion_document(filename: str) -> dict[str, Any]:
    """Load one untouched Motion Editor export as a seven-track API body."""

    payload = json.loads(
        files("bocco_bridge")
        .joinpath("assets", "motions", filename)
        .read_text(encoding="utf-8")
    )
    if not isinstance(payload, Mapping):
        raise ValueError(f"packaged motion {filename} must contain an object")
    unexpected = set(payload) - set(MOTION_TRACKS) - {"sound"}
    if unexpected:
        raise ValueError(
            f"packaged motion {filename} has unexpected fields: "
            + ", ".join(sorted(unexpected))
        )
    # Motion Editor exports carry a sound selector beside the seven tracks.
    # The Platform API endpoint accepts only the track payload, and the BOCCO
    # client applies the same projection before POSTing.
    document = {track: payload.get(track) for track in MOTION_TRACKS}
    validate_motion_document(document)
    track_totals = {
        sum(transition["duration"] for transition in document[track])
        for track in MOTION_TRACKS
    }
    if len(track_totals) != 1:
        raise ValueError(f"packaged motion {filename} tracks have unequal durations")
    if not document["head"] or document["head"][-1]["p3"] != [0, 0]:
        raise ValueError(f"packaged motion {filename} must end with a neutral head")
    return document


if _HAND_WRITTEN_CUSTOM_MOTION_NAMES & PACKAGED_MOTION_NAMES:
    raise ValueError("packaged custom motion names collide with hand-written motions")
CUSTOM_MOTION_DOCUMENTS.update(
    {
        name: _load_packaged_motion_document(filename)
        for name, filename in PACKAGED_MOTION_FILES.items()
    }
)
CUSTOM_MOTION_NAMES = frozenset(CUSTOM_MOTION_DOCUMENTS)
# Long packaged motions are intentionally standalone-only: recognizing them
# in streamed reply cues would let a ten-second motion outlive its sentence.
CUSTOM_MOTION_CUE_NAMES = (
    _HAND_WRITTEN_CUSTOM_MOTION_NAMES | SHORT_PACKAGED_MOTION_NAMES
)
COMPOSITE_MOTION_NAMES = frozenset(COMPOSITE_PRESET_CHAINS)
