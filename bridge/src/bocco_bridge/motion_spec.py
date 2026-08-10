"""A compact choreography spec, and the renderer that expands it.

The model never writes a motion document. A full document is seven tracks of
bezier keyframes — around 600 tokens — and a model emitting those both destroys
the 2-4s reply latency and reproduces, keyframe by keyframe, the mechanical
look that hand-authored motions already suffer from. So the model emits a
*spec*: a name and three to six beats, each a small bag of words drawn from
four fixed vocabularies. That is roughly 40 tokens, contains no numbers at all,
and cannot express an out-of-range angle.

Everything that makes a gesture read as alive is applied here, in code, so that
every generated motion gets it for free.

The renderer used to buy liveliness with *timing*: 100-150ms accents, steep
asymmetric easing curves, and an overshoot bolted onto ``p2``. On hardware that
read as "way too snappy, sharp edge turns". Four instrumented runs of a
separate choreography, judged by the same person, say why:

===== =========================== ==========================================
run   head transitions            verdict
===== =========================== ==========================================
1     100-1600ms, peak 1572 deg/s "really cluttered, moved unsmooth"
2     250-1600ms, peak 733 deg/s  "really really slow and hard"
3     **600**-1300ms, 35-122      smooth; the only run never called jittery
4     300-1600ms                  "too fast / jittery again"
===== =========================== ==========================================

Run 4 reintroduced 300ms accents with everything else held constant and the
jitter came straight back, so ~600ms is a *measured* floor and the ~250ms a
servo datasheet implies is a discredited model. Run 2's "hard" was traced to
the easing curve ``[0.9, 0, 1, 1]``, whose terminal slope is ten times its
average: a nominally 700ms move was a 630ms freeze followed by a 70ms lunge.
Saturating a servo reads exactly like hitting an end stop -- the "sharp edge
turns" complaint. Yukai's own shipped samples corroborate all of it: four head
keyframes over 3600ms, byte-identical *linear* easing on every one, x within
+/-15, all the shape in ``controlPoints`` and all the rhythm in the LEDs.

So the design rules here are:

* **Rate is the invariant, not duration.** What saturates a servo is
  degrees per second. Every head transition's duration is *derived* from the
  measured peak speed of its own path (:func:`head_rate_report` composes the
  spatial bezier with the timing curve exactly as the robot does), so a big
  move automatically takes longer. Nothing can be emitted above
  :data:`SAFE_PEAK_DEGREES_PER_SECOND`.
* **Linear head easing, always.** Yukai uses no easing at all, and linear
  timing cannot concentrate travel behind the renderer's back: peak rate is
  simply path speed over duration. The expressive easing curves moved to the
  LED tracks, which have no inertia to saturate.
* **Shape comes from control points.** Anticipation, follow-through and
  turning arcs are all bezier handles. They cost no keyframes and, unlike a
  steep ease, they show up in the rate measurement.
* **No cusps.** Poses are joined with Catmull-Rom tangents, so the path is
  C1 continuous and the head curves *through* a direction change instead of
  stopping dead and reversing. An exact retrace, which has no tangent, is
  bowed sideways into a rounded loop.
* **Contrast from amplitude, not tempo.** A 12 degree move over 600ms and a
  35 degree move over 600ms are very different gestures at the same duration.
  ``accent`` is now the smallest and tightest move in a motion rather than the
  fastest, and its punch is carried by the LEDs and the antenna.
* **Living holds.** A held pose micro-drifts two or three degrees rather than
  freezing; perfect stillness reads as a crashed process.
* **The vertical axis and the bottom LEDs.** Horizontal-only sway reads as
  scanning, and ``led_rec``/``led_play``/``led_func`` are an entire unused
  expressive channel, so a travelling light runs along them.
* **Independent cheeks.** The right cheek lags the left by one beat and carries
  its own alpha; identical cheeks read as a status light rather than a face.

Every hard constraint is guaranteed by construction rather than hoped for: the
seven tracks are built from one shared list of group durations, so they always
agree; the document always ends with the head at ``[0, 0]``; a spec too long
for its budget loses beats rather than having its transitions compressed below
the floor; and the result is passed through :func:`validate_motion_document`
and a rate measurement before it is returned.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
import re
from typing import Any, Iterable, Mapping, Sequence

from .custom_motions import (
    LED_TRACKS,
    MAX_CONTROL_ANGLE,
    MAX_HEAD_ANGLE,
    MAX_MOTION_MILLISECONDS,
    MAX_VERTICAL_ANGLE,
    MIN_CONTROL_ANGLE,
    MIN_HEAD_ANGLE,
    MIN_VERTICAL_ANGLE,
    MOTION_TRACKS,
    validate_motion_document,
)


MAX_SPEC_BEATS = 6
MOTION_NAME_MAX_CHARS = 16
# Deliberately under the 10,000ms hard ceiling: the renderer must be able to
# append its return-to-neutral segment without ever needing to negotiate.
RENDER_BUDGET_MILLISECONDS = 8_000
DEFAULT_MOTION_NAME = "あたらしいうごき"

DIRECTIONS: dict[str, tuple[float, float]] = {
    "center": (0.0, 0.0),
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
    "upleft": (-0.8, 0.8),
    "upright": (0.8, 0.8),
    "downleft": (-0.8, -0.8),
    "downright": (0.8, -0.8),
}
_DIRECTION_ALIASES = {
    "centre": "center",
    "middle": "center",
    "neutral": "center",
    "front": "center",
    "まんなか": "center",
    "ひだり": "left",
    "左": "left",
    "みぎ": "right",
    "右": "right",
    "うえ": "up",
    "上": "up",
    "した": "down",
    "下": "down",
}
ENERGIES = ("low", "mid", "high")
_ENERGY_ALIASES = {
    "medium": "mid",
    "middle": "mid",
    "soft": "low",
    "gentle": "low",
    "slow": "low",
    "calm": "low",
    "strong": "high",
    "fast": "high",
    "big": "high",
    "よわい": "low",
    "ふつう": "mid",
    "つよい": "high",
}
COLOURS: dict[str, tuple[int, int, int] | None] = {
    "pink": (255, 80, 130),
    "amber": (255, 170, 60),
    "blue": (60, 120, 255),
    "green": (70, 220, 140),
    "violet": (170, 90, 255),
    "white": (255, 240, 220),
    "red": (255, 70, 70),
    "off": None,
}
_COLOUR_ALIASES = {
    "orange": "amber",
    "yellow": "amber",
    "gold": "amber",
    "purple": "violet",
    "magenta": "pink",
    "cyan": "blue",
    "aqua": "blue",
    "dark": "off",
    "black": "off",
    "none": "off",
}
_ACCENT_WORDS = frozenset({"accent", "snap", "quick", "pop", "flick"})
_HOLD_WORDS = frozenset({"hold", "still", "pause", "wait", "freeze"})

# The head is always linear, like every keyframe of every Yukai sample. The
# other curves drive LEDs only, where there is no mass to accelerate.
_LINEAR = [0.0, 0.0, 1.0, 1.0]
_LED_SNAP = [0.9, 0.0, 1.0, 1.0]
_LED_FADE = [0.3, 0.0, 0.7, 1.0]

# --- the measured hardware envelope ----------------------------------------
#: No head transition that moves is ever shorter than this. Run 3 (600ms
#: minimum) was smooth; run 4 put 300ms accents back and the jitter returned.
MIN_HEAD_TRANSITION_MILLISECONDS = 600
#: Soft ceiling. A move needing longer is shrunk instead, because a two-second
#: head sweep reads as a fault rather than a gesture.
MAX_HEAD_TRANSITION_MILLISECONDS = 1_600
#: Hard rate ceiling for the composed trajectory. Yukai's own samples sit at
#: 50-75 deg/s and the smooth run measured 35-122, so this is the top of the
#: only band the robot has ever been observed to execute cleanly.
SAFE_PEAK_DEGREES_PER_SECOND = 120.0
#: What each energy *aims* at. Durations are solved from these, so the peak
#: lands here rather than being hoped for.
_TARGET_PEAK_RATE = {"low": 50.0, "mid": 68.0, "high": 88.0}

# Amplitude, the lever that actually buys contrast. Yukai stays inside x
# +/-15; +/-30 is twice as expressive and still two thirds of the +/-45 the
# endpoint validator allows, which leaves room for the arc to bulge.
_HEAD_REACH_DEGREES = 30.0
_HEAD_LIFT_DEGREES = 13.0
_ENERGY_REACH = {"low": 0.40, "mid": 0.70, "high": 1.0}
#: An accent is the *smallest* move in a motion, not the fastest one.
_ACCENT_REACH = 0.55

_LEAD_IN_MILLISECONDS = 250  # Yukai's LED grid unit; the head does not move
_HOLD_DRIFT_MILLISECONDS = (760, 900)
_CATMULL_TENSION = 0.5
_END_TENSION = 0.62
#: Below this fraction of the neighbouring chord a joint is a cusp -- an exact
#: retrace with no tangent -- and gets bowed sideways into a loop instead.
_CUSP_FRACTION = 0.25
_LOOP_GAIN = 0.55
_RATE_SAMPLES = 129

_ENERGY_ALPHA = {"low": 120, "mid": 180, "high": 240}
_ANTENNA_SHAKE = {"low": (0.15, 3), "mid": (0.4, 6), "high": (0.8, 11)}

_NAME_LINE = re.compile(
    r"^(?:name|title|なまえ|名前)\s*[:：]\s*(?P<value>.*)$",
    flags=re.IGNORECASE,
)
_BEAT_LINE = re.compile(
    r"^(?:beat|step|ビート|びーと|\d+|[-*・>])\s*[.)：:]?\s*(?P<value>.*)$",
    flags=re.IGNORECASE,
)
_BEAT_SPLIT = re.compile(r"(?:(?<=\s)|^)(?:beat|ビート)\s*[:：]", flags=re.IGNORECASE)
_WORD_SPLIT = re.compile(r"[^0-9A-Za-z぀-ヿ一-鿿]+")
_NAME_STRIP = re.compile(r"^[\s\"'“”「『（(\[]+|[\s\"'“”」』）)\]]+$")


class MotionSpecError(ValueError):
    """The model's choreography spec could not be understood."""


@dataclass(frozen=True, slots=True)
class MotionBeat:
    """One beat: a direction, an energy, a colour, and an optional modifier."""

    direction: str = "center"
    energy: str = "mid"
    colour: str = "white"
    accent: bool = False
    hold: bool = False

    @property
    def kind(self) -> str:
        """``accent`` wins over ``hold``: a beat cannot be both short and long."""

        if self.accent:
            return "accent"
        if self.hold:
            return "hold"
        return "travel"

    def to_text(self) -> str:
        words = [self.direction, self.energy, self.colour]
        if self.accent:
            words.append("accent")
        elif self.hold:
            words.append("hold")
        return "beat: " + " ".join(words)


@dataclass(frozen=True, slots=True)
class MotionSpec:
    """A named, ordered list of beats — the only thing that is ever stored."""

    name: str
    beats: tuple[MotionBeat, ...]

    def to_text(self) -> str:
        return "\n".join((f"name: {self.name}", *(beat.to_text() for beat in self.beats)))


def spec_format_prompt(theme: str) -> str:
    """Ask for a spec and nothing else; the bridge owns every number."""

    subject = theme.strip() or "any mood you like"
    return (
        "Invent a short expressive dance for a small desk robot that has a "
        "movable head, one antenna, two cheek LEDs and three small LEDs on its "
        f"body. Theme: {subject}.\n"
        "Answer with ONLY these lines and nothing else — no prose, no Markdown, "
        "no numbers:\n"
        f"name: <a short Japanese name, at most {MOTION_NAME_MAX_CHARS} characters>\n"
        "beat: <direction> <energy> <colour> [accent|hold]\n"
        f"Give 3 to {MAX_SPEC_BEATS} beat lines.\n"
        "direction is one of: " + " ".join(DIRECTIONS) + "\n"
        "energy is one of: " + " ".join(ENERGIES) + "\n"
        "colour is one of: " + " ".join(COLOURS) + "\n"
        "Vary the beats: mix directions, mix energies, and use at least one "
        "accent and at least one hold."
    )


def parse_motion_spec(text: object, *, fallback_name: str = "") -> MotionSpec:
    """Parse a spec defensively; raise :class:`MotionSpecError` when hopeless.

    Written for malformed output, not for well-formed output: unknown words and
    unknown lines are dropped, a beat is an unordered bag of words, extra beats
    past :data:`MAX_SPEC_BEATS` are discarded, and a missing name falls back to
    the requested theme. Only a spec with no recognizable beat at all fails, and
    the caller degrades to a spoken line rather than raising into the reply.
    """

    if not isinstance(text, str) or not text.strip():
        raise MotionSpecError("choreography spec was empty")
    name = ""
    beats: list[MotionBeat] = []
    for line in _candidate_lines(text):
        name_match = _NAME_LINE.match(line)
        if name_match is not None:
            if not name:
                name = _clean_name(name_match.group("value"))
            continue
        beat_match = _BEAT_LINE.match(line)
        body = line if beat_match is None else beat_match.group("value")
        words = _known_words(body)
        if beat_match is None:
            # An unlabelled line has to look like a beat to count as one;
            # otherwise stray prose would become choreography.
            if not words or not (words["direction"] or words["colour"]):
                continue
        elif not words:
            # A labelled beat whose words are all unknown still counts: a bland
            # beat is a better degradation than losing the whole motion.
            words = _known_words("center")
        beats.append(_beat_from_words(words))
    if not beats:
        raise MotionSpecError("choreography spec contained no usable beat")
    name = name or _clean_name(fallback_name) or DEFAULT_MOTION_NAME
    return MotionSpec(name=name, beats=tuple(beats[:MAX_SPEC_BEATS]))


def _candidate_lines(text: str) -> Iterable[str]:
    stripped = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("```")
    )
    # Tolerate a whole spec collapsed onto one line.
    stripped = _BEAT_SPLIT.sub("\nbeat:", stripped)
    for line in stripped.splitlines():
        candidate = line.strip()
        if candidate:
            yield candidate


def _known_words(body: str) -> dict[str, Any]:
    found: dict[str, Any] = {
        "direction": None,
        "energy": None,
        "colour": None,
        "accent": False,
        "hold": False,
    }
    for raw in _WORD_SPLIT.split(body):
        word = raw.strip().casefold()
        if not word:
            continue
        direction = _DIRECTION_ALIASES.get(word, word)
        if direction in DIRECTIONS and found["direction"] is None:
            found["direction"] = direction
            continue
        energy = _ENERGY_ALIASES.get(word, word)
        if energy in ENERGIES and found["energy"] is None:
            found["energy"] = energy
            continue
        colour = _COLOUR_ALIASES.get(word, word)
        if colour in COLOURS and found["colour"] is None:
            found["colour"] = colour
            continue
        if word in _ACCENT_WORDS:
            found["accent"] = True
        elif word in _HOLD_WORDS:
            found["hold"] = True
    if (
        found["direction"] is None
        and found["energy"] is None
        and found["colour"] is None
        and not found["accent"]
        and not found["hold"]
    ):
        return {}
    return found


def _beat_from_words(words: Mapping[str, Any]) -> MotionBeat:
    return MotionBeat(
        direction=words["direction"] or "center",
        energy=words["energy"] or "mid",
        colour=words["colour"] or "white",
        accent=bool(words["accent"]),
        hold=bool(words["hold"]) and not bool(words["accent"]),
    )


def _clean_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    compact = " ".join(value.split())
    compact = _NAME_STRIP.sub("", compact)
    return compact[:MOTION_NAME_MAX_CHARS]


# --------------------------------------------------------------------------
# Measuring what the robot will actually do
# --------------------------------------------------------------------------
#
# Endpoint checks are blind: ``p3`` can be a perfectly legal [30, 8] while the
# path between two legal endpoints swings to 47 degrees and commands 700 deg/s
# on the way. These documents are generated from model output at runtime and
# nobody reviews them before the robot performs them, so the renderer measures
# the composed trajectory -- spatial bezier through timing bezier, exactly as
# the firmware evaluates it -- and refuses to emit anything outside the band.


@dataclass(frozen=True, slots=True)
class HeadRateReport:
    """What a rendered head track commands, sampled along its real path."""

    #: Peak commanded angular rate, deg/s, over the whole track.
    peak_degrees_per_second: float
    #: Peak per transition, in document order. Non-moving joins report 0.
    per_transition: tuple[float, ...]
    #: Largest |x| and |y| the *path* reaches, not just its endpoints.
    peak_horizontal: float
    peak_vertical: float

    @property
    def is_safe(self) -> bool:
        return (
            self.peak_degrees_per_second <= SAFE_PEAK_DEGREES_PER_SECOND
            and self.peak_horizontal <= MAX_HEAD_ANGLE
            and self.peak_vertical <= MAX_VERTICAL_ANGLE
        )


def head_rate_report(
    document: Mapping[str, Any], *, samples: int = 257
) -> HeadRateReport:
    """Sample the head trajectory and report peak rate and peak excursion.

    ``p0`` is ``[None, None]`` throughout -- the robot continues from wherever
    the head already is -- so the start of each transition is the previous
    transition's ``p3``, and the very first is neutral. That is what makes this
    a measurement of the performance rather than of the keyframes.
    """

    transitions = document.get("head") or []
    current = (0.0, 0.0)
    peaks: list[float] = []
    peak_x = 0.0
    peak_y = 0.0
    for transition in transitions:
        end = transition.get("p3")
        if not isinstance(end, list) or end[0] is None or end[1] is None:
            # A pure join: no target, so nothing is commanded to move.
            peaks.append(0.0)
            continue
        target = (float(end[0]), float(end[1]))
        one = _control_or(transition.get("p1"), current)
        two = _control_or(transition.get("p2"), target)
        rate, excursion = _sample_transition(
            current,
            one,
            two,
            target,
            transition.get("ease") or _LINEAR,
            int(transition.get("duration") or 0),
            samples,
        )
        peaks.append(rate)
        peak_x = max(peak_x, excursion[0])
        peak_y = max(peak_y, excursion[1])
        current = target
    return HeadRateReport(
        peak_degrees_per_second=max(peaks, default=0.0),
        per_transition=tuple(peaks),
        peak_horizontal=peak_x,
        peak_vertical=peak_y,
    )


def _control_or(
    point: object, fallback: tuple[float, float]
) -> tuple[float, float]:
    if not isinstance(point, list) or len(point) != 2:
        return fallback
    return (
        fallback[0] if point[0] is None else float(point[0]),
        fallback[1] if point[1] is None else float(point[1]),
    )


def _sample_transition(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    ease: Sequence[float],
    duration: int,
    samples: int,
) -> tuple[float, tuple[float, float]]:
    seconds = duration / 1000.0
    peak_rate = 0.0
    peak = (0.0, 0.0)
    for index in range(samples):
        # Open interval: a cubic timing curve has a zero derivative at both
        # ends, and 0/0 there says nothing about the interior.
        parameter = (index + 0.5) / samples
        progress, slope = _timing(ease, parameter)
        point = _cubic(p0, p1, p2, p3, progress)
        peak = (max(peak[0], abs(point[0])), max(peak[1], abs(point[1])))
        if seconds <= 0.0:
            peak_rate = math.inf
            continue
        speed = math.hypot(*_cubic_derivative(p0, p1, p2, p3, progress))
        peak_rate = max(peak_rate, speed * slope / seconds)
    return peak_rate, peak


def _timing(ease: Sequence[float], parameter: float) -> tuple[float, float]:
    """``(y(t), dy/dx)`` for a CSS-style timing bezier at parameter ``t``.

    The curve runs (0,0) -> (ease[0], ease[1]) -> (ease[2], ease[3]) -> (1,1)
    and is traversed in **its own parameter**, not in normalised time. So
    ``parameter`` is ``t``, and the returned progress is ``y(t)`` rather than
    ``y`` at time ``t``. Inverting ``x(t)`` to sample uniformly in time would
    be the other option, and it is deliberately not taken:

    * Every caller sweeps the whole interval and takes a maximum, and ``x(t)``
      is a monotonic bijection from [0,1] onto [0,1]. Reparameterising a curve
      does not change the set of ``(progress, slope)`` pairs on it, so the
      peak is identical either way -- only the spacing of the samples differs.
    * For the linear curve ``[0, 0, 1, 1]`` -- the only one the head is ever
      given -- ``x(t)`` and ``y(t)`` are the same polynomial, so the returned
      progress *is* the normalised time, exactly, and ``dy/dx`` is exactly 1.
      That identity is the whole reason the head uses nothing else, and
      ``test_the_timing_sweep_does_not_depend_on_its_parameterisation`` pins it.
    * Where the two do differ, on the steep curves, sampling in ``t`` clusters
      points near the ends, where a steep curve is at its most extreme. It
      therefore reports a peak at or above the uniformly-timed one -- the
      conservative direction for something whose job is to refuse motions.

    The rate multiplier the motion actually sees is ``dy/dx``, which is what
    the second element is, computed as ``(dy/dt) / (dx/dt)``.
    """

    x1, y1, x2, y2 = (float(value) for value in ease)
    rest = 1.0 - parameter
    progress = (
        3.0 * rest * rest * parameter * y1
        + 3.0 * rest * parameter * parameter * y2
        + parameter * parameter * parameter
    )
    d_x = (
        3.0 * rest * rest * x1
        + 6.0 * rest * parameter * (x2 - x1)
        + 3.0 * parameter * parameter * (1.0 - x2)
    )
    d_y = (
        3.0 * rest * rest * y1
        + 6.0 * rest * parameter * (y2 - y1)
        + 3.0 * parameter * parameter * (1.0 - y2)
    )
    if abs(d_x) < 1e-9:
        # Time stands still while the value moves: an unbounded command.
        return progress, math.inf
    return progress, abs(d_y / d_x)


def _cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    progress: float,
) -> tuple[float, float]:
    rest = 1.0 - progress
    a = rest * rest * rest
    b = 3.0 * rest * rest * progress
    c = 3.0 * rest * progress * progress
    d = progress * progress * progress
    return (
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    )


def _cubic_derivative(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    progress: float,
) -> tuple[float, float]:
    rest = 1.0 - progress
    a = 3.0 * rest * rest
    b = 6.0 * rest * progress
    c = 3.0 * progress * progress
    return (
        a * (p1[0] - p0[0]) + b * (p2[0] - p1[0]) + c * (p3[0] - p2[0]),
        a * (p1[1] - p0[1]) + b * (p2[1] - p1[1]) + c * (p3[1] - p2[1]),
    )


def _peak_path_speed(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> float:
    """Fastest point on a spatial bezier, in degrees per unit of progress.

    Sampled over the *closed* interval, unlike :func:`_sample_transition`,
    which has to open it to dodge the timing curve's 0/0 at both ends. There is
    no timing here -- this is pure geometry -- and a cubic very often reaches
    its peak speed exactly at an endpoint. A closed loop out of and back into
    the origin always does. Solving a duration from a midpoint-only sample
    therefore underestimates the peak and hands back a rate slightly above the
    target, which is precisely what it is this function's job to prevent.
    """

    return max(
        math.hypot(
            *_cubic_derivative(p0, p1, p2, p3, index / (_RATE_SAMPLES - 1))
        )
        for index in range(_RATE_SAMPLES)
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_motion_document(
    spec: MotionSpec, *, budget_milliseconds: int = RENDER_BUDGET_MILLISECONDS
) -> dict[str, Any]:
    """Expand a spec into a validated seven-track document.

    Deterministic in ``spec.name``: a remembered motion looks the same every
    time it is recalled, while two differently named motions get different
    jitter. Stored specs are re-rendered on playback, so every motion already
    invented picks up this renderer's envelope the next time it plays.
    """

    if not isinstance(spec, MotionSpec) or not spec.beats:
        raise MotionSpecError("choreography spec has no beats to render")
    seed = int(hashlib.sha256(spec.name.encode("utf-8")).hexdigest()[:16], 16)
    budget = min(int(budget_milliseconds), MAX_MOTION_MILLISECONDS)
    beats, head, durations = _plan_head(spec.beats[:MAX_SPEC_BEATS], budget, seed)

    document: dict[str, Any] = {
        "head": head,
        "antenna": _render_antenna(beats, durations),
        **_render_leds(beats, durations),
    }
    document = {track: document[track] for track in MOTION_TRACKS}

    # The backstop, not the guarantee. Everything above is built inside the
    # limits; this refuses to hand anything doubtful to the robot.
    validate_motion_document(document)
    totals = {
        sum(int(transition["duration"]) for transition in document[track])
        for track in MOTION_TRACKS
    }
    if len(totals) != 1:
        raise MotionSpecError("rendered tracks disagree on total duration")
    if not document["head"] or document["head"][-1]["p3"] != [0, 0]:
        raise MotionSpecError("rendered document does not end at a neutral head")
    report = head_rate_report(document)
    if not report.is_safe:
        raise MotionSpecError(
            "rendered head trajectory leaves the safe envelope "
            f"({report.peak_degrees_per_second:.0f} deg/s, "
            f"reaching {report.peak_horizontal:.0f}/{report.peak_vertical:.0f} deg)"
        )
    return document


def _plan_head(
    beats: Sequence[MotionBeat], budget: int, seed: int
) -> tuple[tuple[MotionBeat, ...], list[dict[str, Any]], list[int]]:
    """Fit a motion inside ``budget`` without ever compressing a transition.

    The old renderer scaled every group proportionally when a spec ran long,
    which is precisely how a 600ms travel became a 200ms one. Time is not
    negotiable here, so content is: drop the drift out of holds first, then
    drop trailing beats, and only if a single beat cannot fit fall back to a
    minimal figure sized to whatever time there is.
    """

    for count in range(len(beats), 0, -1):
        for drifts in (2, 1):
            built = None
            # Amplitude is the only thing that shrinks for *safety*: a move so
            # large it would need more than the ceiling to stay in the rate
            # band gets smaller, never faster.
            for scale in (1.0, 0.8, 0.64, 0.5):
                built = _build_head(beats[:count], drifts, scale, seed)
                if built is not None:
                    break
            if built is None:
                continue
            head, durations = built
            if sum(durations) <= budget:
                return tuple(beats[:count]), head, durations
    return (beats[0],), *_minimal_head(budget)


def _build_head(
    beats: Sequence[MotionBeat], drifts: int, scale: float, seed: int
) -> tuple[list[dict[str, Any]], list[int]] | None:
    """Build the head track, or ``None`` if a segment would run too long.

    Returns the transitions plus the *group* durations -- lead-in, one per
    beat, return -- which is the single list all seven tracks are cut from.
    """

    rng = random.Random(seed)
    groups = [
        _beat_poses(beat, index, drifts, scale)
        for index, beat in enumerate(beats)
    ]
    keyframes: list[tuple[int, int]] = [(0, 0)]
    for _, poses in groups:
        keyframes.extend(poses)
    # The library-wide invariant: emo is never left holding a posture.
    keyframes.append((0, 0))

    handles = _tangent_handles(keyframes)
    transitions: list[dict[str, Any]] = [_head_continue(_LEAD_IN_MILLISECONDS)]
    spans: list[int] = []
    for index in range(len(keyframes) - 1):
        start = keyframes[index]
        end = keyframes[index + 1]
        one = _clamp_control(handles[index][1])
        two = _clamp_control(handles[index + 1][0])
        if two == [int(end[0]), int(end[1])]:
            # p2 == p3 is the straight-line slide this renderer exists to
            # avoid, so nudge the handle back along the segment.
            two = _clamp_control(
                (end[0] - (end[0] - start[0]) * 0.2 - 2.0, end[1] - 2.0)
            )
        # Solve the duration from the emitted, rounded geometry rather than
        # from the ideal floats, so what is measured is what is played. The
        # ceiling (not round) is what makes "peak rate <= target" an identity
        # rather than an empirical observation.
        speed = _peak_path_speed(
            (float(start[0]), float(start[1])),
            (float(one[0]), float(one[1])),
            (float(two[0]), float(two[1])),
            (float(end[0]), float(end[1])),
        )
        floor = _segment_floor(index, groups, rng)
        duration = max(
            floor, math.ceil(speed / _segment_rate(index, groups) * 1000.0)
        )
        if duration > MAX_HEAD_TRANSITION_MILLISECONDS:
            return None
        transitions.append(
            {
                "duration": duration,
                "p0": [None, None],
                "p1": one,
                "p2": two,
                "p3": [int(end[0]), int(end[1])],
                # Linear, on every keyframe, exactly as Yukai ships it.
                "ease": list(_LINEAR),
            }
        )
        spans.append(duration)

    durations = [_LEAD_IN_MILLISECONDS]
    cursor = 0
    for _, poses in groups:
        durations.append(sum(spans[cursor : cursor + len(poses)]))
        cursor += len(poses)
    durations.append(sum(spans[cursor:]))
    return transitions, durations


_PoseGroup = tuple[str, tuple[tuple[int, int], ...]]


def _segment_owner(index: int, groups: Sequence[_PoseGroup]) -> tuple[int, int]:
    """Which beat a keyframe segment belongs to, and its offset within it."""

    cursor = 0
    for group_index, (_, poses) in enumerate(groups):
        if index < cursor + len(poses):
            return group_index, index - cursor
        cursor += len(poses)
    return -1, 0  # the return-to-neutral segment


def _segment_rate(index: int, groups: Sequence[_PoseGroup]) -> float:
    group_index, offset = _segment_owner(index, groups)
    if group_index < 0:
        # Coming home. The last thing a motion does should not be its fastest.
        return _TARGET_PEAK_RATE["low"]
    if offset > 0:
        # Drift inside a hold: barely moving, and it should look it.
        return _TARGET_PEAK_RATE["low"] * 0.4
    return _TARGET_PEAK_RATE[groups[group_index][0]]


def _segment_floor(
    index: int, groups: Sequence[_PoseGroup], rng: random.Random
) -> int:
    group_index, offset = _segment_owner(index, groups)
    if group_index >= 0 and offset > 0:
        # A hold should feel like a hold. Its drift segments are the longest
        # things in the document, which is where the duration contrast now
        # lives now that nothing is allowed to be short.
        return _HOLD_DRIFT_MILLISECONDS[(offset - 1) % 2] + rng.randint(-60, 60)
    return MIN_HEAD_TRANSITION_MILLISECONDS


def _minimal_head(budget: int) -> tuple[list[dict[str, Any]], list[int]]:
    """A budget too small for one beat still gets a rate-safe gesture.

    Rather than compress a real move into the time available -- which is the
    original bug -- shrink the *amplitude* until the rate fits. The head
    traces a small closed loop off neutral and back, so it both moves and ends
    where the library requires.

    Amplitude is quantised to whole degrees, so it cannot shrink indefinitely:
    below a certain span even the smallest loop this document can express is
    too fast. There the honest answer is not to move at all. A settle -- a
    transition that commands neutral from neutral -- holds the posture, still
    ends where the library requires, and commands a rate of exactly zero.
    """

    budget = max(1, int(budget))
    lead_in = max(1, min(_LEAD_IN_MILLISECONDS, budget // 4))
    if budget - lead_in < 1:
        # Not enough time for a lead-in and a move that both round to >=1ms.
        # Spend the whole budget standing still rather than overrunning it.
        return [_head_settle(budget)], [budget]
    span = budget - lead_in
    # Solve the reach from the geometry that will actually be emitted. The
    # closed-form "3 * |handle|" this used to assume is wrong twice over: it
    # ignores the vertical component of the handle, and the handles round to
    # whole degrees, so the ideal float reach is not what the robot is
    # commanded to trace. Measure the rounded loop -- exactly as _build_head
    # measures its own emitted geometry -- and step down until it fits.
    limit = _TARGET_PEAK_RATE["low"] * (span / 1000.0)
    reach = min(_HEAD_REACH_DEGREES * 0.5, limit / 3.0)
    while reach >= 0.5:
        one = _clamp_control((reach, reach * 0.45))
        two = _clamp_control((-reach, reach * 0.45))
        speed = _peak_path_speed(
            (0.0, 0.0),
            (float(one[0]), float(one[1])),
            (float(two[0]), float(two[1])),
            (0.0, 0.0),
        )
        if speed <= limit:
            transitions = [
                _head_continue(lead_in),
                {
                    "duration": span,
                    "p0": [None, None],
                    "p1": one,
                    "p2": two,
                    "p3": [0, 0],
                    "ease": list(_LINEAR),
                },
            ]
            return transitions, [lead_in, span]
        reach -= 0.5
    # Even a one degree loop outruns the target in this much time.
    return [_head_continue(lead_in), _head_settle(span)], [lead_in, span]


def _beat_poses(
    beat: MotionBeat, index: int, drifts: int, scale: float
) -> _PoseGroup:
    """Geometry only: the pose(s) a beat visits, before any timing exists."""

    target = _beat_target(beat, index, scale)
    if beat.kind == "hold":
        # A living hold: two or three degrees of drift, never a freeze.
        wander = [
            _clamp_pose((target[0] + (2 if index % 2 else -3), target[1] + 2)),
            _clamp_pose((target[0] + (-3 if index % 2 else 2), target[1] - 3)),
        ][:drifts]
        return beat.energy, (target, *wander)
    return beat.energy, (target,)


def _beat_target(beat: MotionBeat, index: int, scale: float) -> tuple[int, int]:
    horizontal, vertical = DIRECTIONS[beat.direction]
    reach = _ENERGY_REACH[beat.energy] * scale
    if beat.accent:
        # An accent is punctuation. It is the shortest transition in the
        # document, so the only way it can stay inside the rate band is to be
        # the smallest -- and a tight 12 degree flick reads far more like a
        # beat landing than a 30 degree lunge the servo cannot track anyway.
        reach *= _ACCENT_REACH
    side = 1 if index % 2 == 0 else -1
    if horizontal == 0.0 and vertical == 0.0:
        # A centred beat is a small nod, not a dead stop.
        return _clamp_pose((0, round(side * reach * 6)))
    if vertical == 0.0:
        # Purely lateral cues would read as scanning; lend them the vertical
        # axis so the head arcs rather than sweeps.
        vertical = side * 0.4
    return _clamp_pose(
        (
            round(horizontal * reach * _HEAD_REACH_DEGREES),
            round(vertical * reach * _HEAD_LIFT_DEGREES),
        )
    )


def _tangent_handles(
    keyframes: Sequence[tuple[int, int]]
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Catmull-Rom handles: ``(incoming p2, outgoing p1)`` for each keyframe.

    This is where the "sharp edge turns" go. Giving every interior keyframe a
    tangent proportional to the vector between its neighbours makes the whole
    path C1 continuous: the head arrives at a pose already moving in the
    direction it is about to leave in, so a direction change is a curve rather
    than a stop and a reversal. An exact retrace (left, right, left) has no
    such tangent -- the neighbours coincide -- so it is bowed sideways instead
    and the turn becomes a rounded loop.
    """

    count = len(keyframes)
    handles: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for index, point in enumerate(keyframes):
        previous = keyframes[index - 1] if index > 0 else None
        following = keyframes[index + 1] if index + 1 < count else None
        if previous is None and following is None:
            tangent = (0.0, 0.0)
        elif previous is None:
            assert following is not None
            tangent = (
                (following[0] - point[0]) * _END_TENSION,
                (following[1] - point[1]) * _END_TENSION,
            )
        elif following is None:
            tangent = (
                (point[0] - previous[0]) * _END_TENSION,
                (point[1] - previous[1]) * _END_TENSION,
            )
        else:
            tangent = (
                (following[0] - previous[0]) * _CATMULL_TENSION,
                (following[1] - previous[1]) * _CATMULL_TENSION,
            )
            incoming = math.hypot(point[0] - previous[0], point[1] - previous[1])
            outgoing = math.hypot(following[0] - point[0], following[1] - point[1])
            span = max(incoming, outgoing)
            if math.hypot(*tangent) < _CUSP_FRACTION * span:
                # A cusp: the head would stop dead and go back the way it
                # came. Swing it sideways through the turn instead.
                sideways = _LOOP_GAIN * span * (1.0 if index % 2 else -1.0)
                if incoming > 1e-6:
                    tangent = (
                        -(point[1] - previous[1]) / incoming * sideways,
                        (point[0] - previous[0]) / incoming * sideways,
                    )
                else:
                    tangent = (sideways, sideways * 0.45)
            # Keep the handles inside the shorter neighbouring chord so a
            # tight corner cannot inflate the path length, and with it the
            # rate, without bound.
            limit = max(4.0, min(incoming, outgoing) * 1.1)
            length = math.hypot(*tangent)
            if length > limit:
                tangent = (tangent[0] * limit / length, tangent[1] * limit / length)
        handles.append(
            (
                (point[0] - tangent[0] / 3.0, point[1] - tangent[1] / 3.0),
                (point[0] + tangent[0] / 3.0, point[1] + tangent[1] / 3.0),
            )
        )
    return handles


def _head_continue(duration: int) -> dict[str, Any]:
    """Join whatever posture the head is already in, as the library does."""

    return {
        "duration": int(duration),
        "p0": [None, None],
        "p1": [None, None],
        "p2": [None, None],
        "p3": [None, None],
        "ease": list(_LINEAR),
    }


def _head_settle(duration: int) -> dict[str, Any]:
    """Command neutral from neutral: ends where the library requires, at rest.

    Distinct from :func:`_head_continue`, which leaves ``p3`` unset and so
    holds whatever posture the head happened to be in. A document has to end
    neutral, so the no-motion fallback needs a real target.
    """

    return {
        "duration": int(duration),
        "p0": [None, None],
        "p1": [0, 0],
        "p2": [0, 0],
        "p3": [0, 0],
        "ease": list(_LINEAR),
    }


def _render_antenna(
    beats: Sequence[MotionBeat], durations: Sequence[int]
) -> list[dict[str, Any]]:
    """One antenna transition per group, in a single mode for the whole document.

    The API validates modes per transition, but mixing a phase sweep and a
    vibration inside one document reads as a fault rather than a gesture, so the
    document commits to one: energetic specs buzz, calm specs sway.
    """

    energetic = any(
        beat.energy == "high" or beat.accent for beat in beats
    )
    transitions: list[dict[str, Any]] = []
    if energetic:
        current: Any = (0.0, 0)
        rest: Any = (0.0, 0)
        values: list[Any] = [rest, *(_antenna_shake(beat) for beat in beats), rest]
    else:
        current = 0.0
        rest = 0.0
        values = [
            0.0,
            *(_antenna_sway(beat, index) for index, beat in enumerate(beats)),
            0.0,
        ]
    if len(values) > len(durations):
        # A budget too small to give every beat a group still has to bring the
        # antenna home; truncating the list would strand it mid-buzz.
        values = [*values[: len(durations) - 1], rest]
    for duration, value in zip(durations, values):
        if energetic:
            assert isinstance(value, tuple) and isinstance(current, tuple)
            transitions.append(
                {
                    "duration": int(duration),
                    "start": {"amp": current[0], "freq": current[1], "pos": None},
                    "end": {"amp": value[0], "freq": value[1], "pos": None},
                }
            )
        else:
            transitions.append(
                {
                    "duration": int(duration),
                    "start": {"amp": None, "freq": None, "pos": current},
                    "end": {"amp": None, "freq": None, "pos": value},
                }
            )
        current = value
    return transitions


def _antenna_shake(beat: MotionBeat) -> tuple[float, int]:
    if beat.accent:
        return (0.9, 13)
    amplitude, frequency = _ANTENNA_SHAKE[beat.energy]
    return (amplitude, frequency)


def _antenna_sway(beat: MotionBeat, index: int) -> float:
    horizontal, vertical = DIRECTIONS[beat.direction]
    reach = _ENERGY_REACH[beat.energy]
    if vertical == 0.0:
        vertical = 0.5 if index % 2 == 0 else -0.5
    return round(max(-1.0, min(1.0, vertical * reach)), 2)


def _render_leds(
    beats: Sequence[MotionBeat], durations: Sequence[int]
) -> dict[str, list[dict[str, Any]]]:
    """Cheeks that differ from each other, plus a light travelling the body."""

    tracks: dict[str, list[dict[str, Any]]] = {track: [] for track in LED_TRACKS}
    groups = len(durations)
    for group in range(groups):
        duration = durations[group]
        beat = beats[group - 1] if 1 <= group <= len(beats) else None
        previous = beats[group - 2] if 2 <= group <= len(beats) else None
        # The head can no longer be sharp, so the LEDs carry the attack --
        # which is exactly where Yukai's own samples put the rhythm.
        ease = _LED_SNAP if beat is not None and beat.accent else _LED_FADE
        left = _led_colour(beat)
        # The right cheek lags the left by one beat and carries its own alpha:
        # two cheeks lit identically read as a status light, not a face.
        right = _led_colour(previous if previous is not None else beat, alpha_shift=-45)
        tracks["led_cheek_l"].append(_led(duration, left, ease))
        tracks["led_cheek_r"].append(_led(duration, right, ease))
        lit = None if beat is None else (group - 1) % 3
        for index, track in enumerate(("led_rec", "led_play", "led_func")):
            tracks[track].append(
                _led(
                    duration,
                    _led_colour(beat, alpha_shift=-70) if index == lit else None,
                    ease,
                )
            )
    return tracks


def _led_colour(
    beat: MotionBeat | None, *, alpha_shift: int = 0
) -> tuple[int, int, int, int] | None:
    if beat is None:
        return None
    channels = COLOURS[beat.colour]
    if channels is None:
        return None
    alpha = 255 if beat.accent else _ENERGY_ALPHA[beat.energy]
    return (*channels, max(0, min(255, alpha + alpha_shift)))


def _led(
    duration: int,
    end: tuple[int, int, int, int] | None,
    ease: Sequence[float],
) -> dict[str, Any]:
    return {
        "duration": int(duration),
        "start": [None, None, None, None],
        "end": [0, 0, 0, 0] if end is None else [int(value) for value in end],
        "ease": [float(value) for value in ease],
    }


def _clamp_pose(pose: tuple[float, float]) -> tuple[int, int]:
    return (
        int(max(MIN_HEAD_ANGLE, min(MAX_HEAD_ANGLE, round(pose[0])))),
        int(max(MIN_VERTICAL_ANGLE, min(MAX_VERTICAL_ANGLE, round(pose[1])))),
    )


def _clamp_control(point: tuple[float, float]) -> list[int]:
    return [
        int(max(MIN_CONTROL_ANGLE, min(MAX_CONTROL_ANGLE, round(point[0])))),
        int(max(MIN_CONTROL_ANGLE, min(MAX_CONTROL_ANGLE, round(point[1])))),
    ]
