import json
import unittest
from importlib.resources import files

from bocco_bridge.choreography import MOTION_CUE_FAMILIES, extract_motion_cues
from bocco_bridge.config import MOTION_CUE_INSTRUCTIONS
from bocco_bridge.custom_motions import (
    COMPOSITE_MOTION_NAMES,
    COMPOSITE_PRESET_CHAINS,
    CUSTOM_MOTION_CUE_NAMES,
    CUSTOM_MOTION_DOCUMENTS,
    CUSTOM_MOTION_NAMES,
    CUSTOM_MOTION_TOKEN_PREFIX,
    LONG_PACKAGED_MOTION_NAMES,
    MOTION_TRACKS,
    PACKAGED_MOTION_FILES,
    PACKAGED_MOTION_NAMES,
    SHORT_PACKAGED_MOTION_NAMES,
    custom_motion_token,
    motion_duration_seconds,
    parse_custom_motion_token,
    validate_motion_document,
)


class CustomMotionDocumentTests(unittest.TestCase):
    def test_expected_names_are_defined(self) -> None:
        packaged = {
            "delight-burst",
            "surprise-realisation",
            "sleepy-drift",
            "morning-awakening",
            "celebration-crescendo",
            "goodnight-sequence",
        }
        self.assertEqual(
            CUSTOM_MOTION_NAMES,
            {
                "きょろきょろ",
                "しょんぼり",
                "てれてれ",
                "ぶんぶん",
                "まっすぐ",
                "かんがえちゅう",
            }
            | packaged,
        )
        self.assertEqual(PACKAGED_MOTION_NAMES, packaged)
        self.assertEqual(
            SHORT_PACKAGED_MOTION_NAMES,
            {"delight-burst", "surprise-realisation", "sleepy-drift"},
        )
        self.assertEqual(
            LONG_PACKAGED_MOTION_NAMES,
            {
                "morning-awakening",
                "celebration-crescendo",
                "goodnight-sequence",
            },
        )
        self.assertEqual(COMPOSITE_MOTION_NAMES, {"びっくりよろこび", "納得"})

    def test_every_document_satisfies_the_api_motion_spec(self) -> None:
        for name, document in CUSTOM_MOTION_DOCUMENTS.items():
            with self.subTest(name=name):
                validate_motion_document(document)
                json.dumps(document)

    def test_packaged_motion_assets_load_and_validate(self) -> None:
        self.assertEqual(set(PACKAGED_MOTION_FILES), PACKAGED_MOTION_NAMES)
        for name, filename in PACKAGED_MOTION_FILES.items():
            with self.subTest(name=name):
                exported = json.loads(
                    files("bocco_bridge")
                    .joinpath("assets", "motions", filename)
                    .read_text(encoding="utf-8")
                )
                document = CUSTOM_MOTION_DOCUMENTS[name]
                self.assertEqual(
                    document,
                    {track: exported[track] for track in MOTION_TRACKS},
                )
                validate_motion_document(document)

    def test_packaged_motion_tracks_share_authored_duration(self) -> None:
        expected_milliseconds = {
            "delight-burst": 2_400,
            "surprise-realisation": 3_000,
            "sleepy-drift": 5_400,
            "morning-awakening": 9_800,
            "celebration-crescendo": 9_900,
            "goodnight-sequence": 10_000,
        }
        for name, expected in expected_milliseconds.items():
            with self.subTest(name=name):
                document = CUSTOM_MOTION_DOCUMENTS[name]
                totals = {
                    track: sum(item["duration"] for item in document[track])
                    for track in MOTION_TRACKS
                }
                self.assertEqual(set(totals.values()), {expected})
                self.assertLessEqual(expected, 10_000)
                self.assertEqual(motion_duration_seconds(name), expected / 1000)

    def test_documents_animate_head_and_stay_under_ten_seconds(self) -> None:
        for name in CUSTOM_MOTION_NAMES:
            with self.subTest(name=name):
                document = CUSTOM_MOTION_DOCUMENTS[name]
                self.assertGreaterEqual(len(document["head"]), 2)
                self.assertLessEqual(motion_duration_seconds(name), 10.0)
                # Hand-written motions join the current posture with a null
                # target; Motion Editor exports explicitly begin at neutral.
                first = document["head"][0]
                expected_start = (
                    [0, 0] if name in PACKAGED_MOTION_NAMES else [None, None]
                )
                self.assertEqual(first["p2"], expected_start)
                self.assertEqual(first["p3"], expected_start)

    def test_documents_end_with_head_at_neutral(self) -> None:
        for name in CUSTOM_MOTION_NAMES:
            with self.subTest(name=name):
                last = CUSTOM_MOTION_DOCUMENTS[name]["head"][-1]
                self.assertEqual(last["p3"], [0, 0])

    def test_validation_rejects_out_of_range_documents(self) -> None:
        valid = CUSTOM_MOTION_DOCUMENTS["まっすぐ"]
        missing = {
            track: valid[track] for track in MOTION_TRACKS if track != "antenna"
        }
        with self.assertRaises(ValueError):
            validate_motion_document(missing)
        overshoot = {
            **valid,
            "head": [
                {
                    "duration": 500,
                    "p0": [None, None],
                    "p1": [None, None],
                    "p2": [50, 0],
                    "p3": [50, 0],
                    "ease": [0, 0, 1, 1],
                }
            ],
        }
        with self.assertRaises(ValueError):
            validate_motion_document(overshoot)
        too_long = {
            **valid,
            "head": [
                {
                    "duration": 6_000,
                    "p0": [None, None],
                    "p1": [None, None],
                    "p2": [0, 0],
                    "p3": [0, 0],
                    "ease": [0, 0, 1, 1],
                }
            ]
            * 2,
        }
        with self.assertRaises(ValueError):
            validate_motion_document(too_long)
        mixed_mode_antenna = {
            **valid,
            "antenna": [
                {
                    "duration": 500,
                    "start": {"amp": 0.5, "freq": 5, "pos": None},
                    "end": {"amp": None, "freq": None, "pos": 0.5},
                }
            ],
        }
        with self.assertRaises(ValueError):
            validate_motion_document(mixed_mode_antenna)

    def test_composite_chains_use_known_preset_families(self) -> None:
        preset_families = {
            family
            for families in MOTION_CUE_FAMILIES.values()
            for family in families
        }
        self.assertEqual(
            COMPOSITE_PRESET_CHAINS["びっくりよろこび"], ("WHAT", "YES", "GOOD")
        )
        self.assertEqual(COMPOSITE_PRESET_CHAINS["納得"], ("ALRIGHT", "GOOD"))
        for name, chain in COMPOSITE_PRESET_CHAINS.items():
            with self.subTest(name=name):
                self.assertTrue(1 <= len(chain) <= 3)
                for preset in chain:
                    self.assertIn(preset, preset_families)

    def test_motion_names_do_not_collide_with_cue_families(self) -> None:
        reserved = set(MOTION_CUE_FAMILIES)
        self.assertFalse(CUSTOM_MOTION_NAMES & reserved)
        self.assertFalse(COMPOSITE_MOTION_NAMES & reserved)
        self.assertFalse(CUSTOM_MOTION_NAMES & COMPOSITE_MOTION_NAMES)

    def test_short_packaged_motion_cues_are_case_insensitive(self) -> None:
        result = extract_motion_cues(
            "A[motion:DELIGHT-BURST]B[motion:Surprise-Realisation]"
            "C[motion:sLeEpY-dRiFt]D"
        )

        self.assertEqual(result.text, "ABCD")
        self.assertEqual(
            [cue.name for cue in result.cues],
            ["delight-burst", "surprise-realisation", "sleepy-drift"],
        )
        self.assertTrue(SHORT_PACKAGED_MOTION_NAMES <= CUSTOM_MOTION_CUE_NAMES)

    def test_long_packaged_motions_are_not_reply_cues(self) -> None:
        self.assertFalse(LONG_PACKAGED_MOTION_NAMES & CUSTOM_MOTION_CUE_NAMES)
        for name in LONG_PACKAGED_MOTION_NAMES:
            with self.subTest(name=name):
                result = extract_motion_cues(f"前[motion:{name}]後")
                self.assertEqual(result.text, "前後")
                self.assertEqual(result.cues, ())
                self.assertNotIn(name, MOTION_CUE_INSTRUCTIONS)

    def test_tokens_round_trip_and_reject_unknown_names(self) -> None:
        token = custom_motion_token("しょんぼり")
        self.assertEqual(token, CUSTOM_MOTION_TOKEN_PREFIX + "しょんぼり")
        self.assertEqual(parse_custom_motion_token(token), "しょんぼり")
        self.assertIsNone(parse_custom_motion_token("preset-uuid"))
        self.assertIsNone(parse_custom_motion_token("custom:未定義"))
        with self.assertRaises(KeyError):
            custom_motion_token("未定義")


if __name__ == "__main__":
    unittest.main()
