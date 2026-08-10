import logging
import sys
import unicodedata
import unittest
import unittest.mock

from bocco_bridge.choreography import (
    MAX_DELIVERY_LAG_SECONDS,
    MAX_SECONDS_PER_CHAR,
    MIN_DELIVERY_LAG_SECONDS,
    MIN_SECONDS_PER_CHAR,
    SpeechCalibration,
    cold_start_calibration,
    cue_offset_seconds,
    cue_speech_offset_seconds,
    extract_motion_cues,
    strip_format_chars,
    update_calibration,
    update_delivery_anchor,
    update_finish_anchor,
)

# Spelled as code points on purpose. Written literally these are indentation-
# coloured nothing, and a reader could not tell a test that carries one from a
# test that does not — which is how the live failure got as far as the robot.
BOM = chr(0xFEFF)
ZERO_WIDTH_SPACE = chr(0x200B)
ZERO_WIDTH_NON_JOINER = chr(0x200C)
ZERO_WIDTH_JOINER = chr(0x200D)
WORD_JOINER = chr(0x2060)
SOFT_HYPHEN = chr(0x00AD)
LEFT_TO_RIGHT_MARK = chr(0x200E)
INVISIBLE_SEPARATOR = chr(0x2063)
INVISIBLES = (
    BOM,
    ZERO_WIDTH_SPACE,
    ZERO_WIDTH_NON_JOINER,
    ZERO_WIDTH_JOINER,
    WORD_JOINER,
    SOFT_HYPHEN,
    LEFT_TO_RIGHT_MARK,
    INVISIBLE_SEPARATOR,
)


class ChoreographyTests(unittest.TestCase):
    def test_inline_cues_are_stripped_at_unicode_codepoint_positions(self) -> None:
        result = extract_motion_cues(
            "今日は[motion:Sunny]晴れ。うん[motion:うなずき]、行こう！"
        )

        self.assertEqual(result.text, "今日は晴れ。うん、行こう！")
        self.assertEqual(
            [(cue.name, cue.char_position) for cue in result.cues],
            [("Sunny", 3), ("うなずき", 8)],
        )

    def test_unknown_cues_are_stripped_and_later_known_cues_are_capped(self) -> None:
        result = extract_motion_cues(
            "A[motion:unknown]B[motion:YES]C[motion:NO]D"
            "[motion:GOOD]E[motion:WHAT]F"
        )

        self.assertEqual(result.text, "ABCDEF")
        self.assertEqual([cue.name for cue in result.cues], ["YES", "NO", "GOOD"])

    def test_custom_and_composite_names_are_first_class_cues(self) -> None:
        result = extract_motion_cues(
            "わあ[motion:きょろきょろ]、それは[motion:びっくりよろこび]すごい"
            "[motion:しょんぼり]ね[motion:てれてれ]"
        )

        self.assertEqual(result.text, "わあ、それはすごいね")
        self.assertEqual(
            [(cue.name, cue.char_position) for cue in result.cues],
            [("きょろきょろ", 2), ("びっくりよろこび", 6), ("しょんぼり", 9)],
        )

    def test_cue_tags_with_padding_are_stripped_and_still_resolve(self) -> None:
        variants = (
            "[motion: うれしい]",
            "[ motion:うれしい]",
            "[motion:うれしい ]",
            "[ motion:うれしい ]",
            "[\tmotion:うれしい\t]",
            "[motion\t: うれしい ]",
        )

        for tag in variants:
            with self.subTest(tag=tag):
                result = extract_motion_cues(f"やった{tag}ね")

                self.assertEqual(result.text, "やったね")
                self.assertEqual(
                    [(cue.name, cue.char_position) for cue in result.cues],
                    [("うれしい", 3)],
                )

    def test_padded_unknown_cue_is_stripped_without_leaking(self) -> None:
        result = extract_motion_cues("A[ motion:しらないやつ ]B[\tmotion: YES ]C")

        self.assertEqual(result.text, "ABC")
        self.assertEqual([cue.name for cue in result.cues], ["YES"])

    def test_japanese_douki_keyword_is_accepted_alongside_motion(self) -> None:
        """The exact live failure: 「動き」 where the parser expected "motion".

        Captured from the robot: "[動き:こまった]今の時刻は確認できないよ。" did
        not parse under the "motion"-only pattern, so the raw tag survived into
        the spoken text and the robot read markup aloud instead of gesturing.
        動き is the Japanese word MOTION_CUE_INSTRUCTIONS itself uses to
        describe the concept, so the model reaching for it as the tag keyword
        is the ambiguity that produced this, not a one-off typo.
        """

        result = extract_motion_cues("[動き:こまった]今の時刻は確認できないよ。")

        self.assertEqual(result.text, "今の時刻は確認できないよ。")
        self.assertEqual(
            [(cue.name, cue.char_position) for cue in result.cues],
            [("こまった", 0)],
        )

    def test_bare_known_cue_name_is_extracted_and_stripped(self) -> None:
        """The model shortcuts the keyword: [うなずき] instead of [motion:うなずき].

        Live data showed this as the dominant failure mode — eight rows in the
        bare-cue backup (2026-08-06) where the model wrote just the name in
        brackets with no keyword prefix, and both parsers let it pass through
        to speech.  A bare known name in brackets is a cue tag, not speech,
        and must be both stripped and extracted.
        """

        for bare_name in ("うなずき", "こまった", "しょんぼり", "ふつう"):
            with self.subTest(name=bare_name):
                result = extract_motion_cues(f"今日は[{bare_name}]いい天気だね")

                self.assertEqual(result.text, "今日はいい天気だね")
                self.assertEqual(
                    [(cue.name, cue.char_position) for cue in result.cues],
                    [(bare_name, 3)],
                )

    def test_bare_name_can_be_combined_with_keyword_prefix_tags(self) -> None:
        result = extract_motion_cues(
            "わあ[motion:きょろきょろ]、[うなずき]今日は晴れだね[しょんぼり]"
        )

        self.assertEqual(result.text, "わあ、今日は晴れだね")
        self.assertEqual(
            [(cue.name, cue.char_position) for cue in result.cues],
            [("きょろきょろ", 2), ("うなずき", 3), ("しょんぼり", 10)],
        )

    def test_a_bare_unknown_word_in_brackets_is_left_alone(self) -> None:
        """A bare unknown word is speech, not a tag — only known names qualify."""

        result = extract_motion_cues("[大事な話][サッカー][りんご]今日は晴れ")

        self.assertEqual(result.text, "[大事な話][サッカー][りんご]今日は晴れ")
        self.assertEqual(result.cues, ())

    def test_douki_keyword_tolerates_the_same_padding_as_motion(self) -> None:
        variants = (
            "[動き:うれしい]",
            "[ 動き:うれしい]",
            "[動き: うれしい]",
            "[動き : うれしい ]",
        )

        for tag in variants:
            with self.subTest(tag=tag):
                result = extract_motion_cues(f"やった{tag}ね")

                self.assertEqual(result.text, "やったね")
                self.assertEqual(
                    [(cue.name, cue.char_position) for cue in result.cues],
                    [("うれしい", 3)],
                )

    def test_offset_math_includes_lag_and_relative_character_position(self) -> None:
        calibration = SpeechCalibration(0.5, 0.15)

        self.assertAlmostEqual(cue_offset_seconds(0, 20, calibration), 0.5)
        self.assertAlmostEqual(cue_offset_seconds(10, 20, calibration), 2.0)
        self.assertAlmostEqual(cue_offset_seconds(20, 20, calibration), 3.5)
        self.assertAlmostEqual(
            cue_speech_offset_seconds(10, 20, calibration), 1.5
        )

    def test_motion_transport_lag_is_subtracted_and_clamped(self) -> None:
        calibration = SpeechCalibration(0.5, 0.15)
        self.assertAlmostEqual(
            cue_offset_seconds(10, 20, calibration, motion_transport_lag_seconds=1.0),
            1.0,
        )
        self.assertEqual(
            cue_offset_seconds(0, 2, calibration, motion_transport_lag_seconds=9.0),
            0.0,
        )

    def test_zero_transport_lag_reproduces_uncompensated_schedule(self) -> None:
        """At the shipped default the clamp must be a no-op, exactly."""

        for delivery_lag, seconds_per_char in ((0.5, 0.15), (0.0, 0.05), (2.3, 0.2)):
            calibration = SpeechCalibration(delivery_lag, seconds_per_char)
            for position, length in ((0, 0), (0, 20), (7, 20), (20, 20), (99, 20)):
                with self.subTest(calibration=calibration, position=position):
                    uncompensated = (
                        calibration.delivery_lag_seconds
                        + cue_speech_offset_seconds(position, length, calibration)
                    )

                    self.assertEqual(
                        cue_offset_seconds(position, length, calibration),
                        uncompensated,
                    )
                    self.assertEqual(
                        cue_offset_seconds(
                            position,
                            length,
                            calibration,
                            motion_transport_lag_seconds=0.0,
                        ),
                        uncompensated,
                    )

    def test_anchor_ema_separates_delivery_lag_from_speech_rate(self) -> None:
        current = SpeechCalibration(0.5, 0.15, sample_count=2)

        delivered = update_delivery_anchor(
            current, send_time=10, anchor_time=12, alpha=0.1
        )
        finished = update_finish_anchor(
            delivered,
            speech_anchor_time=12,
            finished_time=16,
            text_length=20,
            alpha=0.1,
        )

        self.assertAlmostEqual(delivered.delivery_lag_seconds, 0.65)
        self.assertEqual(delivered.seconds_per_char, 0.15)
        self.assertAlmostEqual(finished.delivery_lag_seconds, 0.65)
        self.assertAlmostEqual(finished.seconds_per_char, 0.155)
        self.assertEqual(finished.sample_count, 4)

    def test_cold_start_rate_adjusts_for_voice_speed(self) -> None:
        normal = cold_start_calibration(100)
        fast = cold_start_calibration(150)

        self.assertEqual(normal.delivery_lag_seconds, 0.5)
        self.assertAlmostEqual(normal.seconds_per_char, 0.15)
        self.assertAlmostEqual(fast.seconds_per_char, 0.1)

    def test_calibration_ema_updates_and_clamps_outliers(self) -> None:
        current = SpeechCalibration(0.5, 0.15, sample_count=3)
        updated = update_calibration(
            current,
            send_time=10,
            finished_time=14,
            text_length=20,
            alpha=0.1,
        )

        self.assertAlmostEqual(updated.delivery_lag_seconds, 0.55)
        self.assertAlmostEqual(updated.seconds_per_char, 0.1525)
        self.assertEqual(updated.sample_count, 4)

        high = update_calibration(
            SpeechCalibration(MAX_DELIVERY_LAG_SECONDS, MAX_SECONDS_PER_CHAR),
            send_time=0,
            finished_time=10_000,
            text_length=1,
            alpha=1,
        )
        low = update_calibration(
            SpeechCalibration(MIN_DELIVERY_LAG_SECONDS, MIN_SECONDS_PER_CHAR),
            send_time=0,
            finished_time=0.001,
            text_length=100,
            alpha=1,
        )
        self.assertEqual(high.delivery_lag_seconds, MAX_DELIVERY_LAG_SECONDS)
        self.assertEqual(high.seconds_per_char, MAX_SECONDS_PER_CHAR)
        self.assertEqual(low.delivery_lag_seconds, MIN_DELIVERY_LAG_SECONDS)
        self.assertEqual(low.seconds_per_char, MIN_SECONDS_PER_CHAR)


class InvisibleCharacterCueTests(unittest.TestCase):
    """Cue tags carrying invisible format characters must still be swept up.

    The class of bug: Python's ``\\s`` does not match Unicode format characters,
    so one of them inside a tag defeats the cue pattern, nothing is stripped, and
    the robot is handed the raw markup as the words to say. It does not degrade
    to a missing gesture — it costs the whole utterance.
    """

    def test_the_live_bom_failure_yields_its_cue_and_clean_speech(self) -> None:
        """The exact reply that made the robot go silent on the deployed build."""

        result = extract_motion_cues(f"[{BOM}motion:ふつう]昨日の13時41分ごろだよ。")

        self.assertEqual(result.text, "昨日の13時41分ごろだよ。")
        self.assertEqual([cue.name for cue in result.cues], ["ふつう"])

    def test_invisibles_anywhere_in_or_around_a_tag_still_resolve(self) -> None:
        for invisible in INVISIBLES:
            for tag in (
                f"[{invisible}motion:うれしい]",
                f"[motion{invisible}:うれしい]",
                f"[motion:{invisible}うれしい]",
                f"[motion:うれしい{invisible}]",
                f"[mo{invisible}tion:うれしい]",
                # Inside the name itself: the lookup is case-folded against a
                # fixed vocabulary, and an invisible character in the middle of
                # a key misses it, which costs the gesture rather than the words.
                f"[motion:うれ{invisible}しい]",
                f"{invisible}[motion:うれしい]{invisible}",
            ):
                with self.subTest(codepoint=hex(ord(invisible)), tag=tag):
                    result = extract_motion_cues(f"やった{tag}ね")

                    self.assertEqual(result.text, "やったね")
                    self.assertEqual(
                        [cue.name for cue in result.cues], ["うれしい"]
                    )

    def test_invisibles_never_survive_into_the_spoken_text(self) -> None:
        """The speech is also the echo-suppression hash and the transcript row.

        A format character that reaches the wire is hashed with the reply; if the
        device's echo of it differs by that one codepoint the hashes miss, the
        bridge fails to recognise its own words coming back, and the robot
        answers itself. That is a far quieter failure than a silent robot.
        """

        for invisible in INVISIBLES:
            with self.subTest(codepoint=hex(ord(invisible))):
                result = extract_motion_cues(
                    f"[motion:ふつう]今日{invisible}は晴れ{invisible}。"
                )

                self.assertEqual(result.text, "今日は晴れ。")
                self.assertNotIn(invisible, result.text)

    def test_the_stripped_class_is_exactly_unicode_format_characters(self) -> None:
        """The guard against hand-maintaining a list of invisible codepoints.

        A Unicode update that adds a format character should fail here, loudly,
        rather than on a robot that has gone quiet. Unassigned code points are
        allowed to be stripped: they are assigned by later Unicode versions than
        the interpreter running the tests, and the ranges are written from the
        newest data.
        """

        for code in range(sys.maxunicode + 1):
            character = chr(code)
            category = unicodedata.category(character)
            if category == "Cf":
                self.assertEqual(
                    strip_format_chars(character), "", f"missed {code:#06x}"
                )
            elif category != "Cn":
                self.assertEqual(
                    strip_format_chars(character),
                    character,
                    f"wrongly stripped {code:#06x}",
                )

    def test_full_width_brackets_and_colon_resolve_to_a_gesture(self) -> None:
        """A model writing Japanese reaches for these; each one that parses is a
        gesture kept instead of a token the backstop merely deletes."""

        for tag in (
            "［motion：うれしい］",
            "【motion:うれしい】",
            f"［{BOM}motion：うれしい］",
        ):
            with self.subTest(tag=tag):
                result = extract_motion_cues(f"やった{tag}ね")

                self.assertEqual(result.text, "やったね")
                self.assertEqual([cue.name for cue in result.cues], ["うれしい"])

    def test_every_keyword_survives_every_invisible_and_full_width_variant(
        self,
    ) -> None:
        """The cross product of two fixes that landed separately.

        The keyword vocabulary (「動き」) and the invisible-character and
        full-width handling were written on different branches, each against
        its own live failure, and neither branch could test the other's half.
        A merge that kept only one side would look correct — every test on the
        surviving branch still passes — and would lose real utterances the
        moment the model combined the two, which is exactly what it did to
        produce each half in the first place.

        So this asserts the combinations rather than the union: every keyword,
        against every bracket and colon form, with and without a leading BOM.
        """

        for keyword in ("motion", "動き"):
            for opening, closing in (("[", "]"), ("［", "］"), ("【", "】")):
                for colon in (":", "："):
                    for prefix in ("", BOM):
                        tag = f"{opening}{prefix}{keyword}{colon}うれしい{closing}"
                        with self.subTest(tag=tag):
                            result = extract_motion_cues(f"やった{tag}ね")

                            self.assertEqual(result.text, "やったね")
                            self.assertEqual(
                                [cue.name for cue in result.cues], ["うれしい"]
                            )

    def test_unparseable_cue_shaped_tokens_never_reach_the_speech(self) -> None:
        """The backstop: an unreadable variant costs a gesture, not the words."""

        for text, expected in (
            ("A[moton:うれしい]B", "AB"),
            ("A[motion うれしい]B", "AB"),
            ("A[モーション:うれしい]B", "AB"),
            ("A[gesture:happy]B", "AB"),
            ("A[note: internal]B", "AB"),
            # No closing bracket: only the marker and a known name are certain
            # enough to delete.
            ("A[motion:ふつうB", "AB"),
            ("[motion:うれしい やった", "やった"),
        ):
            with self.subTest(text=text):
                result = extract_motion_cues(text)

                self.assertEqual(result.text, expected)
                self.assertNotIn("motion", result.text)

    def test_speech_that_merely_contains_brackets_is_left_alone(self) -> None:
        """The backstop is licensed to delete markup, never speech.

        Bracketed Japanese carries neither an ASCII keyword nor a colon, which is
        what keeps a rule this blunt off the words the household actually hears.
        """

        for text in (
            "これは[大事な話]だよ",
            "「明日」の話[晴れ]ね",
            "【重要】明日は雨だよ",
            "数式は[a:b]みたいな形だね",
            "配列の[0]番目のことだよ",
        ):
            with self.subTest(text=text):
                result = extract_motion_cues(text)

                self.assertEqual(result.text, text)
                self.assertEqual(result.cues, ())

    def test_a_cue_that_fails_to_resolve_says_so(self) -> None:
        """The previous bug in this family went unnoticed by being silent."""

        with self.assertLogs("bocco_bridge.choreography", level="WARNING") as logs:
            extract_motion_cues("A[motion:しらないやつ]B")
        self.assertTrue(
            any("motion_cue_unknown_name" in line for line in logs.output),
            logs.output,
        )

        with self.assertLogs("bocco_bridge.choreography", level="WARNING") as logs:
            extract_motion_cues("A[moton:うれしい]B")
        self.assertTrue(
            any("motion_cue_unparsed" in line for line in logs.output), logs.output
        )

    def test_a_resolved_cue_is_not_noise(self) -> None:
        """Every reply carries cues; a healthy one must not log a thing."""

        logger = logging.getLogger("bocco_bridge.choreography")
        with unittest.mock.patch.object(logger, "warning") as warning:
            extract_motion_cues("今日は[motion:Sunny]晴れ[motion:うれしい]だね")
        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
