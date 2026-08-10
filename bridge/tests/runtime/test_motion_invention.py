import asyncio
import itertools
import json
import math
from pathlib import Path
import tempfile
import unittest

from bocco_bridge.config import BridgeConfig
from bocco_bridge.custom_motions import (
    MAX_ANTENNA_TRANSITIONS,
    MAX_CONTROL_ANGLE,
    MAX_HEAD_ANGLE,
    MAX_HEAD_TRANSITIONS,
    MAX_MOTION_MILLISECONDS,
    MAX_VERTICAL_ANGLE,
    MOTION_TRACKS,
    invented_motion_token,
    is_custom_document_token,
    parse_invented_motion_token,
    validate_motion_document,
)
from bocco_bridge.db import EventDatabase
from bocco_bridge.events import (
    MOTION_INVENTION_USAGE_TEXT,
    EventProcessor,
    EventWorker,
    _parse_motion_invention_command,
)
from bocco_bridge.motion_invention import (
    MOTION_INVENTION_ACK_TEXT,
    MOTION_INVENTION_EMPTY_TEXT,
    MOTION_INVENTION_FAILED_TEXT,
    MOTION_INVENTION_READY_TEXT,
    MOTION_INVENTION_REPLAY_TEXT,
    MotionInventionGenerator,
)
from bocco_bridge.motion_spec import (
    MAX_SPEC_BEATS,
    MIN_HEAD_TRANSITION_MILLISECONDS,
    SAFE_PEAK_DEGREES_PER_SECOND,
    MotionBeat,
    MotionSpec,
    MotionSpecError,
    _LED_FADE,
    _LED_SNAP,
    _TARGET_PEAK_RATE,
    _timing,
    head_rate_report,
    parse_motion_spec,
    render_motion_document,
    spec_format_prompt,
)
from bocco_bridge.reaction_generation import HermesPriorityGate
from bocco_bridge.repertoire import MotionRepertoire
from runtime.fakes import FakeBocco, FakeHermes, FakeInboundEvent
from timeouts import LIVENESS_TIMEOUT


SAMPLE_SPEC = (
    "name: きらきらダンス\n"
    "beat: right high pink accent\n"
    "beat: up mid amber hold\n"
    "beat: left low blue\n"
    "beat: center high white accent\n"
)


def head_arcs(document: dict) -> list[dict]:
    """Head transitions that actually travel (the lead-in only joins)."""

    return [
        transition
        for transition in document["head"]
        if transition["p3"] != [None, None]
    ]


class MotionSpecParsingTests(unittest.TestCase):
    def test_canonical_spec_parses_into_beats(self) -> None:
        spec = parse_motion_spec(SAMPLE_SPEC)
        self.assertEqual(spec.name, "きらきらダンス")
        self.assertEqual(len(spec.beats), 4)
        self.assertEqual(
            spec.beats[0],
            MotionBeat("right", "high", "pink", accent=True, hold=False),
        )
        self.assertEqual(
            spec.beats[1],
            MotionBeat("up", "mid", "amber", accent=False, hold=True),
        )

    def test_spec_round_trips_through_its_stored_text(self) -> None:
        spec = parse_motion_spec(SAMPLE_SPEC)
        self.assertEqual(parse_motion_spec(spec.to_text()), spec)
        # The stored form stays small: this is what makes storing the spec
        # instead of the rendered document worth doing.
        self.assertLess(len(spec.to_text()), 200)

    def test_beat_words_are_unordered_and_unknown_words_are_dropped(self) -> None:
        spec = parse_motion_spec("beat: PINK left sparkly hold high")
        self.assertEqual(
            spec.beats,
            (MotionBeat("left", "high", "pink", accent=False, hold=True),),
        )

    def test_malformed_output_degrades_instead_of_raising(self) -> None:
        cases = {
            "```\nname: A\nbeat: left\n```": ("A", 1),
            "name: B beat: left high pink beat: right low blue": ("B", 2),
            "1. up mid green\n2. down high red": ("テーマ", 2),
            "- left\n- right": ("テーマ", 2),
            "beat: total nonsense here": ("テーマ", 1),
            "Sure! Here is a dance:\nbeat: left mid pink": ("テーマ", 1),
        }
        for raw, (name, beats) in cases.items():
            with self.subTest(raw=raw):
                spec = parse_motion_spec(raw, fallback_name="テーマ")
                self.assertEqual(spec.name, name)
                self.assertEqual(len(spec.beats), beats)
                validate_motion_document(render_motion_document(spec))

    def test_hopeless_output_raises_a_spec_error(self) -> None:
        for raw in ("", "   ", None, 42, "I am afraid I cannot help with that."):
            with self.subTest(raw=raw):
                with self.assertRaises(MotionSpecError):
                    parse_motion_spec(raw, fallback_name="テーマ")

    def test_extra_beats_and_long_names_are_bounded(self) -> None:
        raw = "name: " + "な" * 60 + "\n" + "beat: left mid pink\n" * 20
        spec = parse_motion_spec(raw)
        self.assertEqual(len(spec.beats), MAX_SPEC_BEATS)
        self.assertLessEqual(len(spec.name), 16)

    def test_prompt_asks_only_for_the_spec(self) -> None:
        prompt = spec_format_prompt("うれしい気持ち")
        self.assertIn("name:", prompt)
        self.assertIn("beat:", prompt)
        self.assertIn("うれしい気持ち", prompt)
        # A spec, never a document: nothing the API schema uses appears here,
        # so the model has no numbers to get wrong.
        for fragment in ("led_cheek_l", "led_rec", "duration", "p3", "ease"):
            self.assertNotIn(fragment, prompt)


class MotionRendererTests(unittest.TestCase):
    def _corpus(self) -> list[MotionSpec]:
        specs = [parse_motion_spec(SAMPLE_SPEC)]
        directions = ("left", "right", "up", "down", "center", "upright")
        energies = ("low", "mid", "high")
        modifiers = ("", "accent", "hold")
        for index, (direction, energy, modifier) in enumerate(
            itertools.product(directions, energies, modifiers)
        ):
            beats = [f"beat: {direction} {energy} pink {modifier}".strip()]
            for extra in range(index % MAX_SPEC_BEATS):
                beats.append(
                    f"beat: {directions[extra % len(directions)]} "
                    f"{energies[extra % len(energies)]} blue"
                )
            specs.append(
                parse_motion_spec(f"name: 動き{index}\n" + "\n".join(beats))
            )
        return specs

    def test_rendered_documents_always_validate(self) -> None:
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                document = render_motion_document(spec)
                validate_motion_document(document)
                json.dumps(document)
                self.assertLessEqual(
                    len(document["head"]), MAX_HEAD_TRANSITIONS
                )
                self.assertLessEqual(
                    len(document["antenna"]), MAX_ANTENNA_TRANSITIONS
                )

    def test_rendered_documents_end_with_the_head_at_neutral(self) -> None:
        # The library-wide invariant: a motion may be the last thing that ever
        # plays, so emo is never left holding a posture.
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                self.assertEqual(
                    render_motion_document(spec)["head"][-1]["p3"], [0, 0]
                )

    def test_a_spec_that_ends_far_from_neutral_still_returns(self) -> None:
        spec = parse_motion_spec("name: はしっこ\nbeat: right high pink hold")
        document = render_motion_document(spec)
        self.assertNotEqual(document["head"][-2]["p3"], [0, 0])
        self.assertEqual(document["head"][-1]["p3"], [0, 0])

    def test_all_seven_tracks_share_one_total_duration(self) -> None:
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                document = render_motion_document(spec)
                totals = {
                    track: sum(
                        transition["duration"] for transition in document[track]
                    )
                    for track in MOTION_TRACKS
                }
                self.assertEqual(len(set(totals.values())), 1, totals)
                self.assertLessEqual(
                    max(totals.values()), MAX_MOTION_MILLISECONDS
                )

    def test_a_squeezed_budget_keeps_every_invariant(self) -> None:
        spec = parse_motion_spec(
            "name: ながい\n" + "beat: left low blue hold\n" * MAX_SPEC_BEATS
        )
        document = render_motion_document(spec, budget_milliseconds=900)
        validate_motion_document(document)
        totals = {
            sum(transition["duration"] for transition in document[track])
            for track in MOTION_TRACKS
        }
        self.assertEqual(totals, {900})
        self.assertEqual(document["head"][-1]["p3"], [0, 0])

    def test_control_points_are_actually_used(self) -> None:
        # p1 unset and p2 == p3 is a straight-line slide, and is the single
        # biggest reason authored motions read as mechanical.
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                arcs = head_arcs(render_motion_document(spec))
                arced = [
                    transition
                    for transition in arcs
                    if transition["p1"] != [None, None]
                    and transition["p2"] != transition["p3"]
                ]
                self.assertEqual(len(arced), len(arcs))

    def test_endpoints_stay_inside_the_range_control_points_may_leave(self) -> None:
        spec = parse_motion_spec(
            "name: おおきく\nbeat: right high pink\nbeat: left high blue"
        )
        document = render_motion_document(spec)
        overshooting = 0
        for transition in head_arcs(document):
            horizontal, vertical = transition["p3"]
            self.assertLessEqual(abs(horizontal), MAX_HEAD_ANGLE)
            self.assertLessEqual(abs(vertical), MAX_VERTICAL_ANGLE)
            for control in (transition["p1"], transition["p2"]):
                self.assertLessEqual(abs(control[0]), MAX_CONTROL_ANGLE)
                self.assertLessEqual(abs(control[1]), MAX_CONTROL_ANGLE)
            if abs(transition["p2"][0]) > abs(horizontal) or abs(
                transition["p2"][1]
            ) > abs(vertical):
                overshooting += 1
        self.assertGreater(overshooting, 0)

    def test_head_easing_is_always_linear(self) -> None:
        # Run 2's "hard" was this exact curve, [0.9, 0, 1, 1], whose terminal
        # slope is ten times its average: a 700ms move became a 630ms freeze
        # and a 70ms lunge. Linear timing cannot hide travel behind the
        # renderer's back, so peak rate is just path speed over duration.
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                document = render_motion_document(spec)
                eases = {
                    tuple(transition["ease"]) for transition in document["head"]
                }
                self.assertEqual(eases, {(0.0, 0.0, 1.0, 1.0)})

    def test_head_easing_never_multiplies_the_commanded_rate(self) -> None:
        # The property the previous test buys, stated directly: at no point
        # may the timing curve run faster than the motion's average.
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                document = render_motion_document(spec)
                for transition in document["head"]:
                    for step in range(1, 200):
                        _, slope = _timing(transition["ease"], step / 200)
                        self.assertLessEqual(slope, 1.0 + 1e-9)

    def test_the_timing_sweep_does_not_depend_on_its_parameterisation(
        self,
    ) -> None:
        # _timing walks the timing bezier in its own parameter t, not in
        # normalised time, so progress is y(t) and not y-at-time-t. That reads
        # like a bug and is not one, because every caller sweeps the whole
        # interval and takes a maximum, and x(t) is a monotonic bijection onto
        # [0, 1] -- reparameterising cannot change the set of points visited.
        #
        # For the linear curve the head is actually given, the two agree
        # exactly rather than merely in aggregate: x(t) and y(t) are the same
        # polynomial, so the progress handed back *is* the normalised time.
        linear = [0.0, 0.0, 1.0, 1.0]
        for step in range(1, 500):
            parameter = step / 500
            rest = 1.0 - parameter
            elapsed = (
                3.0 * rest * rest * parameter * linear[0]
                + 3.0 * rest * parameter * parameter * linear[2]
                + parameter**3
            )
            progress, slope = _timing(linear, parameter)
            self.assertAlmostEqual(progress, elapsed, places=12)
            self.assertAlmostEqual(slope, 1.0, places=12)
        # x(t) is monotonic for every curve the renderer emits, which is what
        # makes the argument above hold in general and not just for linear.
        for ease in (linear, _LED_SNAP, _LED_FADE):
            with self.subTest(ease=ease):
                previous = -1.0
                for step in range(0, 501):
                    parameter = step / 500
                    rest = 1.0 - parameter
                    elapsed = (
                        3.0 * rest * rest * parameter * ease[0]
                        + 3.0 * rest * parameter * parameter * ease[2]
                        + parameter**3
                    )
                    self.assertGreaterEqual(elapsed, previous)
                    previous = elapsed
                self.assertAlmostEqual(previous, 1.0, places=12)

    def test_expressive_easing_survives_on_the_leds(self) -> None:
        # Contrast did not disappear, it moved to the channel that has no
        # mass to accelerate — which is where Yukai's samples put it too.
        document = render_motion_document(parse_motion_spec(SAMPLE_SPEC))
        eases = {
            tuple(transition["ease"])
            for track in ("led_cheek_l", "led_cheek_r")
            for transition in document[track]
        }
        self.assertGreaterEqual(len(eases), 2)
        self.assertTrue([ease for ease in eases if ease[0] + ease[2] != 1.0])

    def test_contrast_comes_from_amplitude_and_duration_not_from_snapping(
        self,
    ) -> None:
        document = render_motion_document(parse_motion_spec(SAMPLE_SPEC))
        moving = head_arcs(document)
        durations = [transition["duration"] for transition in moving]
        # Still a range of tempos — holds drift long, travels are shorter —
        # but the short end is the hardware floor, not 100ms.
        self.assertGreaterEqual(max(durations), 1.3 * min(durations))
        self.assertGreaterEqual(min(durations), MIN_HEAD_TRANSITION_MILLISECONDS)
        reaches = [
            max(abs(transition["p3"][0]), abs(transition["p3"][1]))
            for transition in moving
        ]
        self.assertGreaterEqual(max(reaches), 2 * min(reaches))

    def test_a_hold_micro_drifts_rather_than_freezing(self) -> None:
        document = render_motion_document(
            parse_motion_spec("name: とまる\nbeat: up mid amber hold")
        )
        poses = [transition["p3"] for transition in head_arcs(document)]
        held = poses[:3]
        self.assertEqual(len(held), 3)
        self.assertEqual(len(set(map(tuple, held))), 3)
        for previous, current in zip(held, held[1:]):
            drift = max(
                abs(current[0] - previous[0]), abs(current[1] - previous[1])
            )
            self.assertTrue(1 <= drift <= 6, drift)

    def test_the_vertical_axis_is_never_left_unused(self) -> None:
        document = render_motion_document(
            parse_motion_spec("name: よこ\nbeat: left mid pink\nbeat: right mid blue")
        )
        self.assertTrue(
            any(transition["p3"][1] != 0 for transition in head_arcs(document))
        )

    def test_cheeks_are_independent_and_the_body_leds_are_used(self) -> None:
        document = render_motion_document(parse_motion_spec(SAMPLE_SPEC))
        self.assertTrue(
            any(
                left["end"] != right["end"]
                for left, right in zip(
                    document["led_cheek_l"], document["led_cheek_r"]
                )
            )
        )
        for track in ("led_rec", "led_play", "led_func"):
            with self.subTest(track=track):
                self.assertTrue(
                    any(
                        transition["end"] != [0, 0, 0, 0]
                        for transition in document[track]
                    )
                )

    def test_no_rendered_motion_outruns_the_hardware(self) -> None:
        # THE test. Endpoint checks are blind to what happens between two
        # legal endpoints, and these documents are generated from model output
        # at runtime with nobody reviewing them before the robot performs
        # them. Four instrumented runs put the ceiling here: the only version
        # never called jittery measured 35-122 deg/s, and Yukai's own shipped
        # samples sit at 50-75.
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                report = head_rate_report(render_motion_document(spec))
                self.assertLessEqual(
                    report.peak_degrees_per_second,
                    SAFE_PEAK_DEGREES_PER_SECOND,
                    report.per_transition,
                )
                self.assertTrue(report.is_safe)

    def test_the_path_between_legal_endpoints_stays_in_range(self) -> None:
        # p2 sits outside +/-45 by design, so a swing can leave the range
        # between two perfectly legal keyframes. Sample the path, not the
        # corners.
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                report = head_rate_report(render_motion_document(spec))
                self.assertLessEqual(report.peak_horizontal, MAX_HEAD_ANGLE)
                self.assertLessEqual(report.peak_vertical, MAX_VERTICAL_ANGLE)

    def test_every_moving_transition_clears_the_measured_floor(self) -> None:
        # ~600ms is measured, not modelled: run 4 reintroduced 300ms accents
        # with everything else held constant and the jitter came back.
        for spec in self._corpus():
            with self.subTest(spec=spec.name):
                for transition in head_arcs(render_motion_document(spec)):
                    self.assertGreaterEqual(
                        transition["duration"],
                        MIN_HEAD_TRANSITION_MILLISECONDS,
                    )

    def test_the_rate_measurement_would_have_failed_the_old_renderer(
        self,
    ) -> None:
        # Teeth check. This is what the previous renderer emitted: a 130ms
        # accent on the snap curve. Both the duration floor and the rate
        # ceiling have to reject it, or the assertions above prove nothing.
        snappy = {
            "duration": 130,
            "p0": [None, None],
            "p1": [10, 4],
            "p2": [38, 9],
            "p3": [32, 8],
            "ease": [0.9, 0.0, 1.0, 1.0],
        }
        report = head_rate_report({"head": [snappy]})
        self.assertFalse(report.is_safe)
        self.assertGreater(report.peak_degrees_per_second, 1_000.0)
        # ...and the steep ease alone is enough, even at an honest duration.
        report = head_rate_report({"head": [{**snappy, "duration": 700}]})
        self.assertGreater(
            report.peak_degrees_per_second, SAFE_PEAK_DEGREES_PER_SECOND
        )

    def test_a_reversal_curves_through_the_turn(self) -> None:
        # A direction change that stops dead and reverses is the worst case
        # for the servo and for the eye. The head should arrive at the corner
        # still moving, so the outgoing tangent is not the exact opposite of
        # the incoming one.
        document = render_motion_document(
            parse_motion_spec(
                "name: ぎゃく\n"
                "beat: left high blue\n"
                "beat: right high pink\n"
                "beat: left high blue\n"
                "beat: right high pink\n"
            )
        )
        arcs = head_arcs(document)
        turns = 0
        for incoming, outgoing in zip(arcs, arcs[1:]):
            corner = incoming["p3"]
            arriving = (corner[0] - incoming["p2"][0], corner[1] - incoming["p2"][1])
            leaving = (outgoing["p1"][0] - corner[0], outgoing["p1"][1] - corner[1])
            arriving_length = math.hypot(*arriving)
            leaving_length = math.hypot(*leaving)
            self.assertGreater(arriving_length, 0.0)
            self.assertGreater(leaving_length, 0.0)
            cosine = (
                arriving[0] * leaving[0] + arriving[1] * leaving[1]
            ) / (arriving_length * leaving_length)
            # -1 is a cusp: leave exactly the way you came in.
            self.assertGreater(cosine, -0.2, (incoming, outgoing))
            turns += 1
        self.assertGreater(turns, 0)

    def test_an_accent_is_the_smallest_move_not_the_fastest(self) -> None:
        accented = render_motion_document(
            parse_motion_spec("name: あくせんと\nbeat: right high pink accent")
        )
        plain = render_motion_document(
            parse_motion_spec("name: ふつう\nbeat: right high pink")
        )
        self.assertLess(
            abs(head_arcs(accented)[0]["p3"][0]),
            abs(head_arcs(plain)[0]["p3"][0]),
        )
        # The punch it used to buy with 100ms now lives on the LEDs.
        self.assertIn(
            [0.9, 0.0, 1.0, 1.0],
            [transition["ease"] for transition in accented["led_cheek_l"]],
        )

    def test_a_budget_squeeze_drops_beats_rather_than_compressing_them(
        self,
    ) -> None:
        # Proportional scaling is how a 600ms travel used to become a 200ms
        # one. Time is not negotiable now, so content gives way instead.
        spec = parse_motion_spec(
            "name: つめこみ\n"
            + "beat: right high pink\nbeat: left high blue\n" * 3
        )
        roomy = render_motion_document(spec)
        tight = render_motion_document(spec, budget_milliseconds=3_400)
        self.assertLess(len(head_arcs(tight)), len(head_arcs(roomy)))
        for transition in head_arcs(tight):
            self.assertGreaterEqual(
                transition["duration"], MIN_HEAD_TRANSITION_MILLISECONDS
            )
        self.assertTrue(head_rate_report(tight).is_safe)

    def test_a_budget_below_one_beat_shrinks_amplitude_not_time(self) -> None:
        document = render_motion_document(
            parse_motion_spec("name: ちいさい\nbeat: right high pink"),
            budget_milliseconds=900,
        )
        self.assertTrue(head_rate_report(document).is_safe)
        self.assertLessEqual(
            head_rate_report(document).peak_degrees_per_second,
            SAFE_PEAK_DEGREES_PER_SECOND,
        )

    def test_no_budget_is_small_enough_to_break_the_fallback(self) -> None:
        # The fallback shrinks amplitude to fit the time available, but
        # amplitude is quantised to whole degrees and so cannot shrink for
        # ever. It used to hold reach at a 1 degree floor, which for a short
        # enough span commands a rate the head cannot track -- the document
        # then failed its own safety check and the "always renders" fallback
        # raised instead. Sweep the whole range rather than sampling it.
        spec = parse_motion_spec("name: ちいさい\nbeat: right high pink")
        for budget in range(0, 1_600):
            with self.subTest(budget=budget):
                document = render_motion_document(
                    spec, budget_milliseconds=budget
                )
                report = head_rate_report(document)
                self.assertTrue(report.is_safe)
                # Below the backstop is not enough: nothing may outrun the
                # slowest target the renderer ever aims at.
                self.assertLessEqual(
                    report.peak_degrees_per_second, _TARGET_PEAK_RATE["low"]
                )
                # A fallback that overran its budget would desynchronise the
                # seven tracks it is cut from.
                self.assertLessEqual(
                    sum(t["duration"] for t in document["head"]),
                    max(1, budget),
                )
                # Standing still is an acceptable answer at 20ms; ending on a
                # posture never is.
                self.assertEqual(document["head"][-1]["p3"], [0, 0])

    def test_a_stored_spec_re_renders_inside_the_new_envelope(self) -> None:
        # Specs are what is persisted and they are re-rendered on playback, so
        # every motion invented before this renderer existed picks the
        # envelope up the next time it plays.
        stored = MotionSpec(
            name="むかしのうごき",
            beats=parse_motion_spec(SAMPLE_SPEC).beats,
        )
        replayed = parse_motion_spec(stored.to_text(), fallback_name=stored.name)
        self.assertEqual(replayed.beats, stored.beats)
        self.assertTrue(head_rate_report(render_motion_document(replayed)).is_safe)

    def test_rendering_is_deterministic_for_a_stored_spec(self) -> None:
        spec = parse_motion_spec(SAMPLE_SPEC)
        self.assertEqual(
            render_motion_document(spec), render_motion_document(spec)
        )

    def test_rendering_an_empty_spec_raises(self) -> None:
        with self.assertRaises(MotionSpecError):
            render_motion_document(MotionSpec(name="から", beats=()))


class InventedMotionTokenTests(unittest.TestCase):
    def test_tokens_round_trip_and_are_classified_as_documents(self) -> None:
        token = invented_motion_token(7)
        self.assertEqual(parse_invented_motion_token(token), 7)
        self.assertTrue(is_custom_document_token(token))
        self.assertTrue(is_custom_document_token("custom:しょんぼり"))
        self.assertFalse(is_custom_document_token("preset-uuid"))
        for bad in ("invented:", "invented:0", "invented:x", "", "custom:7"):
            self.assertIsNone(parse_invented_motion_token(bad))
        with self.assertRaises(ValueError):
            invented_motion_token(0)


class MotionRepertoireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repertoire = MotionRepertoire(
            Path(self.temporary.name) / "repertoire.db"
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_a_remembered_motion_is_recalled_and_re_rendered(self) -> None:
        spec = parse_motion_spec(SAMPLE_SPEC)
        stored = await self.repertoire.remember(
            "room-1", spec.name, spec.to_text(), "request-1", created_at=10.0
        )
        assert stored is not None
        # The SPEC is stored, not the document.
        self.assertEqual(stored.spec_text, spec.to_text())
        self.assertNotIn("duration", stored.spec_text)

        recalled = await self.repertoire.find("room-1", "きらきらダンス")
        self.assertEqual(recalled, stored)
        rendered = render_motion_document(
            parse_motion_spec(recalled.spec_text, fallback_name=recalled.name)
        )
        validate_motion_document(rendered)
        self.assertEqual(rendered, render_motion_document(spec))

    async def test_recall_tolerates_an_inexact_name(self) -> None:
        await self.repertoire.remember(
            "room-1", "きらきらダンス", SAMPLE_SPEC, "request-1"
        )
        for query in ("きらきら", "きらきらダンスして", "きらきらダンス"):
            with self.subTest(query=query):
                found = await self.repertoire.find("room-1", query)
                self.assertIsNotNone(found)
        self.assertIsNone(await self.repertoire.find("room-1", "ぜんぜん違う"))
        self.assertIsNone(await self.repertoire.find("room-2", "きらきら"))

    async def test_storage_is_idempotent_and_names_are_unique(self) -> None:
        first = await self.repertoire.remember(
            "room-1", "おなじ", SAMPLE_SPEC, "request-1"
        )
        replay = await self.repertoire.remember(
            "room-1", "べつの", SAMPLE_SPEC, "request-1"
        )
        self.assertEqual(replay, first)
        newer = await self.repertoire.remember(
            "room-1", "おなじ", "beat: left mid blue", "request-2"
        )
        assert newer is not None and first is not None
        self.assertNotEqual(newer.id, first.id)
        self.assertIsNone(await self.repertoire.get(first.id))
        self.assertEqual(await self.repertoire.list_names("room-1"), ("おなじ",))

    async def test_retention_bounds_the_repertoire(self) -> None:
        for index in range(6):
            await self.repertoire.remember(
                "room-1",
                f"動き{index}",
                SAMPLE_SPEC,
                f"request-{index}",
                created_at=float(index),
                retention=3,
            )
        self.assertEqual(
            await self.repertoire.list_names("room-1"),
            ("動き5", "動き4", "動き3"),
        )
        latest = await self.repertoire.latest("room-1")
        assert latest is not None
        self.assertEqual(latest.name, "動き5")

    async def test_empty_input_stores_nothing(self) -> None:
        self.assertIsNone(
            await self.repertoire.remember("room-1", "  ", SAMPLE_SPEC, "r")
        )
        self.assertIsNone(
            await self.repertoire.remember("room-1", "な", "   ", "r")
        )
        self.assertEqual(await self.repertoire.list_names("room-1"), ())


class RecordingStage:
    def __init__(self) -> None:
        self.spoken: list[tuple[str, str]] = []
        self.performed: list[tuple[str, int]] = []

    async def speak_aside(self, request_id: str, room_uuid: str, text: str) -> bool:
        self.spoken.append((room_uuid, text))
        return True

    async def perform_invented_motion(
        self, request_id: str, room_uuid: str, motion_id: int
    ) -> bool:
        self.performed.append((room_uuid, motion_id))
        return True


class ScriptedHermes:
    """Hermes whose background answer the test controls, and can stall."""

    def __init__(self, response: str = SAMPLE_SPEC) -> None:
        self.response = response
        self.failures = 0
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def respond(
        self, conversation: str | None, text: str, instructions: str
    ) -> str:
        self.calls.append(text)
        self.started.set()
        await self.release.wait()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("fake Hermes failure")
        return self.response


class MotionInventionGeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=Path(self.temporary.name) / "state.db",
            tunnel_enabled=False,
            worker_max_attempts=2,
            worker_retry_base_seconds=0,
            motion_invention_enabled=True,
            repertoire_database_path=Path(self.temporary.name) / "repertoire.db",
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        self.repertoire = MotionRepertoire(self.config.repertoire_path)
        self.hermes = ScriptedHermes()
        self.gate = HermesPriorityGate(self.hermes)
        self.stage = RecordingStage()
        self.now = 1_000.0
        self.generator = MotionInventionGenerator(
            self.database,
            self.gate,
            self.repertoire,
            self.stage,
            poll_seconds=0.01,
            retry_base_seconds=0.0,
            max_attempts=self.config.worker_max_attempts,
            retention=self.config.motion_invention_retention,
            now=lambda: self.now,
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _enqueue(self, trigger: str = "message-1", theme: str = "うれしい") -> None:
        self.assertTrue(
            await self.database.enqueue_motion_invention(
                trigger, "room-1", theme, "instructions", now=self.now
            )
        )

    async def test_generation_renders_remembers_speaks_and_performs(self) -> None:
        await self._enqueue()
        self.assertTrue(await self.generator.process_once())

        self.assertEqual(len(self.hermes.calls), 1)
        self.assertIn("うれしい", self.hermes.calls[0])
        motion = await self.repertoire.find("room-1", "きらきらダンス")
        assert motion is not None
        self.assertEqual(motion.spec_text, parse_motion_spec(SAMPLE_SPEC).to_text())
        self.assertEqual(
            self.stage.spoken,
            [("room-1", MOTION_INVENTION_READY_TEXT.format(name=motion.name))],
        )
        self.assertEqual(self.stage.performed, [("room-1", motion.id)])
        self.assertFalse(await self.generator.process_once())

    async def test_malformed_output_degrades_to_a_spoken_line(self) -> None:
        self.hermes.response = "I'm sorry, I can't do that."
        await self._enqueue()
        self.assertTrue(await self.generator.process_once())

        self.assertEqual(
            self.stage.spoken, [("room-1", MOTION_INVENTION_FAILED_TEXT)]
        )
        self.assertEqual(self.stage.performed, [])
        self.assertEqual(await self.repertoire.list_names("room-1"), ())
        self.assertFalse(await self.generator.process_once())

    async def test_a_generation_failure_retries_then_speaks_the_fallback(self) -> None:
        self.hermes.failures = 2
        await self._enqueue()
        self.assertTrue(await self.generator.process_once())
        self.assertEqual(self.stage.spoken, [])

        self.assertTrue(await self.generator.process_once())
        self.assertEqual(
            self.stage.spoken, [("room-1", MOTION_INVENTION_FAILED_TEXT)]
        )
        self.assertEqual(self.stage.performed, [])

    async def test_an_invalid_job_is_dead_lettered_not_retried(self) -> None:
        await self._enqueue()
        with self.database._connection() as connection:  # noqa: SLF001
            connection.execute(
                "UPDATE inbound_events SET event_detail = 'not json' "
                "WHERE event_type = 'motion_invention.request'"
            )
        self.assertTrue(await self.generator.process_once())
        self.assertEqual(
            self.stage.spoken, [("room-1", MOTION_INVENTION_FAILED_TEXT)]
        )
        self.assertFalse(await self.generator.process_once())

    async def test_a_foreground_reply_preempts_generation_and_it_is_retried(
        self,
    ) -> None:
        await self._enqueue()
        self.hermes.release.clear()
        background = asyncio.create_task(self.generator.process_once())
        await asyncio.wait_for(self.hermes.started.wait(), timeout=LIVENESS_TIMEOUT)

        self.gate.interrupt_background()
        self.hermes.release.set()
        self.assertTrue(await asyncio.wait_for(background, timeout=LIVENESS_TIMEOUT))
        self.assertEqual(self.stage.spoken, [])
        self.assertEqual(self.stage.performed, [])
        # The job survived: it is claimable again once its retry delay passes.
        self.assertIsNotNone(
            await self.database.claim_next_motion_invention(now=self.now + 1.0)
        )


class LaneAwareHermes:
    """One Hermes for both lanes: the background call stalls, replies do not."""

    def __init__(self) -> None:
        self.background_started = asyncio.Event()
        self.background_release = asyncio.Event()
        self.foreground_calls = 0

    async def respond(
        self, conversation: str | None, text: str, instructions: str
    ) -> str:
        if conversation is None:
            self.background_started.set()
            await self.background_release.wait()
            return SAMPLE_SPEC
        self.foreground_calls += 1
        return "短い返事です。"


class MotionInventionWorkerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=Path(self.temporary.name) / "state.db",
            repertoire_database_path=Path(self.temporary.name) / "repertoire.db",
            tunnel_enabled=False,
            worker_max_attempts=3,
            worker_retry_base_seconds=0,
            motion_invention_enabled=True,
            default_reply_motion=False,
            ack_motion_enabled=False,
            stream_sentences=False,
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        self.bocco = FakeBocco()
        self.hermes = LaneAwareHermes()
        self.gate = HermesPriorityGate(self.hermes)
        self.repertoire = MotionRepertoire(self.config.repertoire_path)
        self.processor = EventProcessor(
            self.config,
            self.database,
            self.bocco,
            self.gate.foreground,
            now=lambda: 1_000.0,
            repertoire=self.repertoire,
        )
        self.worker = EventWorker(self.config, self.database, self.processor)
        self.generator = MotionInventionGenerator(
            self.database,
            self.gate,
            self.repertoire,
            self.processor,
            poll_seconds=0.01,
            retry_base_seconds=0.0,
            max_attempts=3,
            retention=50,
            now=lambda: 1_000.0,
        )

    async def asyncTearDown(self) -> None:
        self.hermes.background_release.set()
        self.temporary.cleanup()

    async def test_generation_in_flight_never_stalls_the_event_worker(self) -> None:
        await self.database.enqueue_motion_invention(
            "message-1", "room-1", "うれしい", "instructions", now=1_000.0
        )
        generating = asyncio.create_task(self.generator.process_once())
        await asyncio.wait_for(self.hermes.background_started.wait(), timeout=LIVENESS_TIMEOUT)

        # The single event worker keeps running while the model call is open.
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="message-2",
                    event_type="message.received",
                    speech_text="こんにちは",
                )
            )
        )
        self.assertTrue(
            await asyncio.wait_for(self.worker.process_once(), timeout=LIVENESS_TIMEOUT)
        )
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        self.assertEqual(self.hermes.foreground_calls, 1)

        self.hermes.background_release.set()
        self.assertTrue(await asyncio.wait_for(generating, timeout=LIVENESS_TIMEOUT))


class MotionInventionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = EventDatabase(Path(self.temporary.name) / "state.db")
        await self.database.initialize()
        self.bocco = FakeBocco()
        self.hermes = FakeHermes("短い返事です。")
        self.now = 1_000.0
        self.notified = 0

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def _build(self, *, enabled: bool = True) -> tuple[EventProcessor, EventWorker]:
        config = BridgeConfig(
            webhook_secret="webhook-secret",
            database_path=Path(self.temporary.name) / "state.db",
            repertoire_database_path=Path(self.temporary.name) / "repertoire.db",
            tunnel_enabled=False,
            worker_max_attempts=3,
            worker_retry_base_seconds=0,
            motion_invention_enabled=enabled,
            default_reply_motion=False,
            ack_motion_enabled=False,
        )
        self.config = config
        self.repertoire = MotionRepertoire(config.repertoire_path)

        def notify() -> None:
            self.notified += 1

        processor = EventProcessor(
            config,
            self.database,
            self.bocco,
            self.hermes,
            now=lambda: self.now,
            repertoire=self.repertoire,
            motion_invention_notify=notify,
        )
        return processor, EventWorker(config, self.database, processor)

    async def _process(self, worker: EventWorker, text: str, request_id: str) -> None:
        event = FakeInboundEvent(
            request_id=request_id,
            event_type="message.received",
            speech_text=text,
        )
        self.assertTrue(await self.database.enqueue(event))
        self.assertTrue(await worker.process_once())

    def test_the_trigger_follows_the_existing_command_style(self) -> None:
        for text in (
            "おどって：うれしい気持ち",
            "踊って:うれしい気持ち",
            "動いて：うれしい気持ち",
            "新しい動き：うれしい気持ち",
            "dance: happy",
            "MOVE：happy",
        ):
            with self.subTest(text=text):
                command = _parse_motion_invention_command(text)
                assert command is not None
                self.assertEqual(command.action, "request")
        self.assertIsNone(_parse_motion_invention_command("おどってほしいな"))
        self.assertIsNone(_parse_motion_invention_command(None))
        self.assertEqual(
            _parse_motion_invention_command("おどって：リスト").action, "list"
        )
        self.assertEqual(
            _parse_motion_invention_command("おどって：" + "あ" * 200).action,
            "invalid",
        )
        self.assertEqual(_parse_motion_invention_command("おどって：").theme, "")

    async def test_an_ask_is_acknowledged_at_once_and_queued_for_later(self) -> None:
        _, worker = self._build()
        await self._process(worker, "おどって：うれしい気持ち", "message-1")

        self.assertEqual(self.bocco.sent, [("room-1", MOTION_INVENTION_ACK_TEXT)])
        self.assertEqual(self.notified, 1)
        # No model call happened on the worker: the ack cost no generation.
        self.assertEqual(self.hermes.responded, [])
        job = await self.database.claim_next_motion_invention(now=self.now)
        assert job is not None
        self.assertEqual(job.room_uuid, "room-1")
        self.assertEqual(
            json.loads(job.event_detail or "")["theme"], "うれしい気持ち"
        )

    async def test_the_queued_job_is_invisible_to_the_event_worker(self) -> None:
        _, worker = self._build()
        await self._process(worker, "おどって：うれしい気持ち", "message-1")
        # The worker has exactly one lane; an invention job it could claim
        # would block every other event behind a model call.
        self.assertFalse(await worker.process_once())
        await self._process(worker, "こんにちは", "message-2")
        self.assertEqual(self.bocco.sent[-1], ("room-1", "短い返事です。"))

    async def test_a_remembered_motion_is_recalled_and_performed(self) -> None:
        processor, worker = self._build()
        spec = parse_motion_spec(SAMPLE_SPEC)
        stored = await self.repertoire.remember(
            "room-1", spec.name, spec.to_text(), "invention-1"
        )
        assert stored is not None

        await self._process(worker, "おどって：きらきらダンス", "message-1")

        self.assertEqual(
            self.bocco.sent,
            [("room-1", MOTION_INVENTION_REPLAY_TEXT.format(name=stored.name))],
        )
        self.assertEqual(len(self.bocco.custom_motion_sent), 1)
        room, document = self.bocco.custom_motion_sent[0]
        self.assertEqual(room, "room-1")
        validate_motion_document(document)
        self.assertEqual(document, render_motion_document(spec))
        # Recall costs no generation at all.
        self.assertIsNone(
            await self.database.claim_next_motion_invention(now=self.now)
        )
        replayed = await self.repertoire.get(stored.id)
        assert replayed is not None
        self.assertEqual(replayed.played_count, 1)

    async def test_an_empty_ask_replays_the_most_recent_motion(self) -> None:
        _, worker = self._build()
        await self.repertoire.remember(
            "room-1", "きらきらダンス", SAMPLE_SPEC, "invention-1"
        )
        await self._process(worker, "おどって：", "message-1")
        self.assertEqual(len(self.bocco.custom_motion_sent), 1)

    async def test_an_unrenderable_stored_spec_sends_nothing(self) -> None:
        _, worker = self._build()
        await self.repertoire.remember(
            "room-1", "こわれた", "この記録は壊れています", "invention-1"
        )
        await self._process(worker, "おどって：こわれた", "message-1")
        self.assertEqual(len(self.bocco.sent), 1)
        self.assertEqual(self.bocco.custom_motion_sent, [])

    async def test_the_repertoire_can_be_listed(self) -> None:
        _, worker = self._build()
        await self._process(worker, "おどって：リスト", "message-1")
        self.assertEqual(self.bocco.sent[-1][1], MOTION_INVENTION_EMPTY_TEXT)
        await self.repertoire.remember(
            "room-1", "きらきらダンス", SAMPLE_SPEC, "invention-1"
        )
        await self._process(worker, "おどって：リスト", "message-2")
        self.assertIn("きらきらダンス", self.bocco.sent[-1][1])

    async def test_an_over_long_theme_is_refused_without_generation(self) -> None:
        _, worker = self._build()
        await self._process(worker, "おどって：" + "あ" * 200, "message-1")
        self.assertEqual(self.bocco.sent[-1][1], MOTION_INVENTION_USAGE_TEXT)
        self.assertIsNone(
            await self.database.claim_next_motion_invention(now=self.now)
        )

    async def test_the_feature_is_inert_when_disabled(self) -> None:
        _, worker = self._build(enabled=False)
        await self._process(worker, "おどって：うれしい気持ち", "message-1")

        # An ordinary utterance: answered by Hermes, nothing queued, and no
        # repertoire database created.
        self.assertEqual(self.bocco.sent, [("room-1", "短い返事です。")])
        self.assertEqual(len(self.hermes.responded), 1)
        self.assertEqual(self.notified, 0)
        self.assertIsNone(
            await self.database.claim_next_motion_invention(now=self.now)
        )
        self.assertFalse(self.config.repertoire_path.exists())

    async def test_speak_aside_records_its_message_for_echo_suppression(self) -> None:
        processor, worker = self._build()
        await self._process(worker, "おどって：うれしい気持ち", "message-1")
        job = await self.database.claim_next_motion_invention(now=self.now)
        assert job is not None

        self.assertTrue(
            await processor.speak_aside(job.request_id, "room-1", "できたよ！")
        )
        self.assertEqual(self.bocco.sent[-1], ("room-1", "できたよ！"))
        echo = FakeInboundEvent(
            request_id="message-2",
            event_type="message.received",
            speech_text="できたよ！",
            message_id=self.bocco.message_ids[0]
            if self.bocco.message_ids
            else "fake-outbound-2",
        )
        self.assertTrue(await self.database.enqueue(echo))
        self.assertTrue(await worker.process_once())
        # The robot did not answer its own aside.
        self.assertEqual(self.bocco.sent[-1], ("room-1", "できたよ！"))


class MotionInventionConfigTests(unittest.TestCase):
    def test_the_feature_is_off_by_default(self) -> None:
        config = BridgeConfig(webhook_secret="secret")
        self.assertFalse(config.motion_invention_enabled)
        self.assertEqual(config.motion_invention_retention, 50)
        self.assertEqual(
            config.repertoire_path, config.database_path.with_name("repertoire.db")
        )

    def test_environment_switches_the_feature_on(self) -> None:
        config = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "secret",
                "BRIDGE_MOTION_INVENTION": "true",
                "BRIDGE_MOTION_INVENTION_RETENTION": "10",
                "BOCCO_BRIDGE_REPERTOIRE_DB": "/tmp/repertoire.db",
            }
        )
        self.assertTrue(config.motion_invention_enabled)
        self.assertEqual(config.motion_invention_retention, 10)
        self.assertEqual(config.repertoire_path, Path("/tmp/repertoire.db"))

    def test_invalid_combinations_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            BridgeConfig(
                webhook_secret="secret",
                motion_invention_enabled=True,
                motions_enabled=False,
            )
        with self.assertRaises(ValueError):
            BridgeConfig(webhook_secret="secret", motion_invention_retention=0)


if __name__ == "__main__":
    unittest.main()
