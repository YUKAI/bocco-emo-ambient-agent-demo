import json
from pathlib import Path
import sys
import tempfile
import unittest

from bocco_bridge.config import BridgeConfig
from bocco_bridge.fast_routes import (
    _strip_nickname,
    FastRouteSkillRunner,
    detect_fast_route,
    extract_weather_location,
)


def _filesystem_encoding_handles_japanese() -> bool:
    """Can this interpreter put a Japanese string into a subprocess argv?

    Python encodes arguments with the filesystem encoding, which under a
    C/POSIX locale is ASCII. That is a property of the environment, not
    something the bridge can code around: the deployment answer is to pin a
    UTF-8 locale, which both systemd units now do and which
    `test_both_units_pin_a_utf8_locale` enforces.

    Under an ASCII locale these two tests would be asserting something the
    platform cannot do, so they skip. The rest of the suite still runs, which
    is what keeps the C-locale CI job meaningful instead of red for a reason it
    cannot fix.
    """

    try:
        "大阪".encode(sys.getfilesystemencoding())
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class FastRouteDetectionTests(unittest.TestCase):
    def test_only_exact_conservative_patterns_route(self) -> None:
        cases = (
            ("今日の天気を教えて？", "weather"),
            ("大阪の天気", "weather"),
            ("東京の天気はどう？", "weather"),
            ("今何時？", "time"),
            ("何時ですか", "time"),
            ("今日何日", "time"),
            ("今日は何曜日ですか？", "time"),
            ("ニュース", "news"),
            ("ニュースを教えて。", "news"),
        )
        for utterance, expected in cases:
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(
                        utterance, frozenset({"weather", "time", "news"})
                    ),
                    expected,
                )

        self.assertEqual(extract_weather_location("大阪の天気"), "大阪")
        self.assertEqual(extract_weather_location("東京の天気はどう？"), "東京")
        self.assertIsNone(extract_weather_location("今日の天気"))

    def test_weather_and_news_near_misses_do_not_route(self) -> None:
        near_misses = (
            "天気の話をしよう",
            "天気とニュースを教えて",
            "今日の天気を見てから服を決めたいです",
            "明日の天気を教えて",
            "ニュースについてどう思う",
            "何時に出発すればいいですか",
            "明日大阪に行くんだけど天気どうかな",
            "明日は大阪の天気",
            "大阪の天気と服装を教えて",
        )
        for utterance in near_misses:
            with self.subTest(utterance=utterance):
                self.assertIsNone(
                    detect_fast_route(
                        utterance, frozenset({"weather", "time", "news"})
                    )
                )

    def test_disabled_route_is_bypassed(self) -> None:
        self.assertIsNone(detect_fast_route("天気", frozenset()))
        self.assertIsNone(detect_fast_route("ニュース", frozenset({"time"})))

    def test_natural_time_phrasings_route(self) -> None:
        """Ordinary phrasings that missed the fourteen-string literal set.

        「エモちゃん、今何時」 is the live failure: it fell through to the
        model, which answered 「今の時刻は確認できないよ」 because it has no
        clock of its own. The rest are near-misses an earlier agent reported:
        いま何時ですか is the hiragana spelling of an entry that IS listed
        (今何時ですか), and the others are shapes the exact-match set never
        covered at all.
        """

        cases = (
            "いま何時ですか",
            "何日ですか",
            "何曜日ですか",
            "今日の日付",
            "今何時かな",
            "時間わかる",
            "時間わかりますか",
            "今日って何日",
        )
        for utterance in cases:
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(
                        utterance, frozenset({"weather", "time", "news"})
                    ),
                    "time",
                )

    def test_time_near_misses_do_not_route(self) -> None:
        """A false positive here replaces a real reply with a canned time.

        「時間の話をしよう」 must not route just because 時間 appears in it, and
        「何時に帰る？」 must not route just because it starts with 何時 - both
        leave real, unanswered content after the clock-shaped prefix.
        """

        near_misses = (
            "何時に帰る？",
            "時間の話をしよう",
            "何時に出発すればいいですか",
            "明日は何時ですか",
            "時間ある?",
            "何時から始まる?",
        )
        for utterance in near_misses:
            with self.subTest(utterance=utterance):
                self.assertIsNone(
                    detect_fast_route(
                        utterance, frozenset({"weather", "time", "news"})
                    )
                )

    def test_natural_news_phrasings_route(self) -> None:
        """NEWS_UTTERANCES is two exact strings; these are how it is asked.

        Unlike weather and time, a missed news question has no useful model
        fallback - the model has no headlines - so these are worth the same
        small pattern the other two routes get.
        """

        for utterance in ("ニュースは？", "今日のニュース", "最新のニュース"):
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(
                        utterance, frozenset({"weather", "time", "news"})
                    ),
                    "news",
                )

    def test_the_ordinary_way_to_ask_about_the_weather_here(self) -> None:
        """Live miss: 「エモちゃん、今日の天気はどう?」 went to a model with no forecast.

        Time and news each got a general pattern; weather never did. The
        literal set lists 「今日の天気」 and 「今日の天気は」 but not
        「今日の天気はどう」, and the place extractor cannot rescue it because
        the only place-shaped token before 「の天気」 is 「今日」, which is
        correctly refused as a city.

        The visible absurdity that produced: asking about somewhere else
        worked, asking about here did not.
        """

        routes = frozenset({"weather", "time", "news"})
        for utterance in (
            "エモちゃん、今日の天気はどう?",
            "今日の天気はどう",
            "天気はどう",
            "天気はどうですか",
            "今の天気はどう",
            "天気教えて",
        ):
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(utterance, routes, robot_nickname="エモちゃん"),
                    "weather",
                )

    def test_the_general_weather_pattern_yields_no_ground_it_should_not(
        self,
    ) -> None:
        """A named city keeps its own path, and other days keep falling through."""

        routes = frozenset({"weather", "time", "news"})
        # The place extractor still sees the city: the general pattern admits
        # only present-tense qualifiers, so it cannot swallow 大阪.
        self.assertEqual(detect_fast_route("大阪の天気はどう", routes), "weather")
        self.assertEqual(extract_weather_location("大阪の天気はどう"), "大阪")

        for utterance in ("明日の天気はどう", "昨日の天気は", "天気の話をしよう"):
            with self.subTest(utterance=utterance):
                self.assertIsNone(detect_fast_route(utterance, routes))

    # Every utterance below was said to the robot on 2026-08-07 and answered by
    # a model that had nothing to answer it with. They are kept together, and
    # verbatim, because each one arrived as its own bug report and the pattern
    # only became visible once they were in one place: the question was fine
    # every time, and the framing around it was what failed.
    LIVE_MISSES = (
        ("えもちゃん、今日の天気は?", "weather"),          # name in the other kana
        ("エモちゃん、今日の天気はどう?", "weather"),       # no general weather pattern
        ("お嬢、今日のニュース教えて。", "news"),           # a different household name
        ("え、もちゃん、今日のニュースは?", "news"),        # comma inside the name
        ("絵文字は今日のニュースをお願い。", "news"),        # name as kanji, particle
        ("もう1度、今日のニュースをお願い。", "news"),       # no name, leading phrase
    )

    def test_every_utterance_that_reached_the_model_by_mistake(self) -> None:
        for utterance, expected in self.LIVE_MISSES:
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(
                        utterance,
                        frozenset({"weather", "time", "news"}),
                        robot_nickname="エモちゃん",
                        robot_nickname_aliases=("お嬢", "エモ"),
                    ),
                    expected,
                )

    def test_framing_and_an_address_in_either_order(self) -> None:
        """They arrive in both orders and sometimes both at once."""

        routes = frozenset({"weather", "time", "news"})
        for utterance, expected in (
            ("エモちゃん、もう1度、今日のニュース", "news"),
            ("もう一度、エモちゃん、今何時", "time"),
            ("ちょっと、今何時", "time"),
        ):
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(utterance, routes, robot_nickname="エモちゃん"),
                    expected,
                )

    def test_framing_removal_cannot_manufacture_a_route(self) -> None:
        """The closed list and the required separator are the whole safety story.

        Removing framing must never change which day or place is being asked
        about — that is the test for admitting an entry — and it must never
        turn a non-question into one.
        """

        routes = frozenset({"weather", "time", "news"})
        for utterance in (
            "明日、天気は",        # a leading comma token that is not framing
            "もう1度、歌って",      # framing, but not a question these routes answer
            "天気の話をしよう",
            "え、なんて言った?",
        ):
            with self.subTest(utterance=utterance):
                self.assertIsNone(
                    detect_fast_route(utterance, routes, robot_nickname="エモちゃん")
                )

    def test_nickname_prefix_is_stripped_before_matching_every_route(self) -> None:
        """「エモちゃん、」 is how the user actually addresses the robot.

        Applied identically to weather, time, and news so a nickname does not
        work for one route and silently miss for another.
        """

        cases = (
            ("エモちゃん、今何時", "time"),
            ("エモちゃん今何時", "time"),
            ("エモちゃん、大阪の天気", "weather"),
            ("エモちゃん、ニュース", "news"),
        )
        for utterance, expected in cases:
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(
                        utterance,
                        frozenset({"weather", "time", "news"}),
                        robot_nickname="エモちゃん",
                    ),
                    expected,
                )
        self.assertEqual(
            extract_weather_location(
                "エモちゃん、大阪の天気", robot_nickname="エモちゃん"
            ),
            "大阪",
        )

    def test_the_name_matches_in_either_kana_script(self) -> None:
        """STT chooses a script for a spoken name; the config chose one too.

        Live failure: the nickname is configured 「エモちゃん」 and the robot
        was asked 「えもちゃん、今日の天気は?」 — same name, other script. The
        exact prefix match missed, the name stayed in front of the question,
        and the weather route did not fire, so a question with a local script
        to answer it went to a model that has no weather.
        """

        for spelling in ("エモちゃん", "えもちゃん"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    detect_fast_route(
                        f"{spelling}、今日の天気は?",
                        frozenset({"weather", "time", "news"}),
                        robot_nickname="エモちゃん",
                    ),
                    "weather",
                )

    def test_a_household_name_that_is_not_the_configured_one(self) -> None:
        """Live failure: 「お嬢、今日のニュース教えて。」 matched nothing.

        The configured nickname is one spelling of one name; a household is
        not. The news route missed and the model — which has no headlines —
        answered 「ニュースは今わからないよ。」
        """

        routes = frozenset({"weather", "time", "news"})
        self.assertIsNone(
            detect_fast_route(
                "お嬢、今日のニュース教えて。", routes, robot_nickname="エモちゃん"
            )
        )
        self.assertEqual(
            detect_fast_route(
                "お嬢、今日のニュース教えて。",
                routes,
                robot_nickname="エモちゃん",
                robot_nickname_aliases=("お嬢",),
            ),
            "news",
        )

    def test_the_addresses_speech_to_text_actually_produced(self) -> None:
        """Every one of these is this robot being asked for the news or weather.

        Captured from the live transcript. The name is the fragile part of the
        utterance, not the question: STT drops a comma into the middle of it
        (「え、もちゃん、」), writes it in kanji as a different word entirely
        (「絵文字」), or marks it as the topic with は instead of pausing. Each
        one defeated a prefix match and sent a question with a local script to
        answer it to a model that has neither headlines nor a forecast.
        """

        routes = frozenset({"weather", "time", "news"})
        for utterance, expected in (
            ("絵文字は今日のニュースをお願い。", "news"),
            ("え、もちゃん、今日のニュースは?", "news"),
            ("えもちゃん、今日の天気は?", "weather"),
            ("メモちゃん、今何時", "time"),
            ("エモちゃん、今日のニュース", "news"),
        ):
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(utterance, routes, robot_nickname="エモちゃん"),
                    expected,
                )

    def test_the_measured_mishearings_apply_only_to_that_deployment(self) -> None:
        """A robot named something else must not answer to 「絵文字」.

        The mishearing set was measured for a persona named 「エモちゃん」 and
        generalizes to no other name, so it is applied only when the configured
        nickname is itself one of those forms.
        """

        routes = frozenset({"weather", "time", "news"})
        self.assertEqual(
            detect_fast_route("絵文字は今日のニュース", routes, robot_nickname="エモちゃん"),
            "news",
        )
        self.assertIsNone(
            detect_fast_route("絵文字は今日のニュース", routes, robot_nickname="コロン")
        )

    def test_a_particle_ends_an_address_but_never_eats_the_question(self) -> None:
        """は and が mark the robot as the topic instead of pausing after it.

        The hazard is that these are single kana which also begin ordinary
        words, so the strip has to remove a *prefix* — at most one particle,
        directly against the name — rather than a character set. Stripping a
        set would turn 「エモちゃん、もう一度言って」 into 「う一度言って」 by
        eating the も of もう, and も is excluded outright for that reason: it
        is the likeliest of the three to open a real word, and it was never
        observed marking an address.
        """

        routes = frozenset({"weather", "time", "news"})
        for utterance, expected in (
            ("絵文字は今日のニュース", "news"),
            ("エモちゃんが今日の天気", "weather"),
            ("エモちゃん、今日のニュース", "news"),
        ):
            with self.subTest(utterance=utterance):
                self.assertEqual(
                    detect_fast_route(utterance, routes, robot_nickname="エモちゃん"),
                    expected,
                )

        # The question survives intact when it merely starts with one of these.
        self.assertEqual(
            _strip_nickname("エモちゃん、もう一度言って", "エモちゃん"),
            "もう一度言って",
        )
        self.assertEqual(
            _strip_nickname("エモちゃん、はやく教えて", "エモちゃん"),
            "はやく教えて",
        )

    def test_a_sentence_about_emoji_is_not_an_address(self) -> None:
        """「絵文字」 is a real word before it is a mishearing of a name.

        Nothing here relies on the strip being clever, and that is the point:
        the strip may fire, but every route ends in a fullmatch against a
        narrow pattern, so an over-eager strip costs a fall-through to the
        model rather than a confidently wrong answer.
        """

        routes = frozenset({"weather", "time", "news"})
        for utterance in (
            "絵文字を送って",
            "絵文字は可愛いね",
            "ニュースの話をしよう",
            "え、なんて言った?",
        ):
            with self.subTest(utterance=utterance):
                self.assertIsNone(
                    detect_fast_route(utterance, routes, robot_nickname="エモちゃん")
                )

    def test_a_leading_comma_token_that_is_not_a_name_is_left_alone(self) -> None:
        """Why this is a name list and not "strip up to the first comma".

        「明日、天気は」 opens with a comma-separated token too. Eating it
        would turn tomorrow's question into today's and answer confidently
        with the wrong day — the weather route only answers for today, which
        is why 明日 is meant to fall through to the model.
        """

        for utterance in ("明日、天気は", "昨日、ニュースあった"):
            with self.subTest(utterance=utterance):
                self.assertIsNone(
                    detect_fast_route(
                        utterance,
                        frozenset({"weather", "time", "news"}),
                        robot_nickname="エモちゃん",
                        robot_nickname_aliases=("お嬢",),
                    )
                )

    def test_nickname_prefix_does_not_manufacture_a_false_positive(self) -> None:
        self.assertIsNone(
            detect_fast_route(
                "エモちゃん、時間の話をしよう",
                frozenset({"weather", "time", "news"}),
                robot_nickname="エモちゃん",
            )
        )

    def test_unconfigured_nickname_leaves_matching_unchanged(self) -> None:
        # robot_nickname defaults to "" (unset). An empty configured nickname
        # must not turn into a prefix every utterance "starts with".
        self.assertEqual(
            detect_fast_route("今何時", frozenset({"time"})), "time"
        )
        self.assertEqual(
            detect_fast_route(
                "今何時", frozenset({"time"}), robot_nickname=""
            ),
            "time",
        )


class FastRouteRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_default_runner_resolves_shared_read_only_install_directory(self) -> None:
        runner = FastRouteSkillRunner(BridgeConfig(webhook_secret="secret"))

        self.assertEqual(
            runner.scripts_dir,
            Path("/usr/local/share/bocco-bridge/fast-skills"),
        )

    @unittest.skipUnless(
        _filesystem_encoding_handles_japanese(),
        "filesystem encoding cannot represent Japanese in argv; "
        "the deployment pins a UTF-8 locale for exactly this reason",
    )
    async def test_runner_uses_fixed_interpreter_script_and_restricted_env(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        scripts = Path(temporary.name)
        script = scripts / "time_info.py"
        script.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'location': os.environ.get('DEFAULT_LOCATION'), "
            "'query': sys.argv[-1], 'secret': os.environ.get('BOCCO_ACCESS_TOKEN')}))\n",
            encoding="utf-8",
        )
        config = BridgeConfig(
            webhook_secret="secret",
            default_location="Tokyo",
            fast_route_python=Path(sys.executable),
            fast_route_scripts_dir=scripts,
        )

        output = await FastRouteSkillRunner(config).run("time", "今何時")
        decoded = json.loads(output)

        self.assertEqual(decoded["location"], "Tokyo")
        self.assertEqual(decoded["query"], "今何時")
        self.assertIsNone(decoded["secret"])

    @unittest.skipUnless(
        _filesystem_encoding_handles_japanese(),
        "filesystem encoding cannot represent Japanese in argv; "
        "the deployment pins a UTF-8 locale for exactly this reason",
    )
    async def test_weather_location_is_passed_as_an_explicit_script_argument(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        scripts = Path(temporary.name)
        script = scripts / "weather.py"
        script.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'args': sys.argv[1:], "
            "'default': os.environ.get('DEFAULT_LOCATION')}))\n",
            encoding="utf-8",
        )
        config = BridgeConfig(
            webhook_secret="secret",
            default_location="Tokyo",
            fast_route_python=Path(sys.executable),
            fast_route_scripts_dir=scripts,
        )

        output = await FastRouteSkillRunner(config).run(
            "weather", "大阪の天気", location="大阪"
        )
        decoded = json.loads(output)

        self.assertEqual(
            decoded["args"], ["--query", "大阪の天気", "--location", "大阪"]
        )
        self.assertEqual(decoded["default"], "Tokyo")

    async def test_missing_install_directory_raises_file_not_found(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        missing = Path(temporary.name) / "not-installed"
        self.addCleanup(temporary.cleanup)
        config = BridgeConfig(
            webhook_secret="secret",
            fast_route_python=Path(sys.executable),
            fast_route_scripts_dir=missing,
        )

        with self.assertRaises(FileNotFoundError):
            await FastRouteSkillRunner(config).run("weather", "天気")

    async def test_symlinked_script_is_rejected(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        scripts = root / "fast-skills"
        scripts.mkdir()
        outside = root / "outside.py"
        outside.write_text("print('owned')\n", encoding="utf-8")
        (scripts / "weather.py").symlink_to(outside)
        config = BridgeConfig(
            webhook_secret="secret",
            fast_route_python=Path(sys.executable),
            fast_route_scripts_dir=scripts,
        )

        with self.assertRaises(ValueError):
            await FastRouteSkillRunner(config).run("weather", "天気")

    async def test_directory_in_place_of_script_is_rejected(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        scripts = Path(temporary.name)
        (scripts / "weather.py").mkdir()
        config = BridgeConfig(
            webhook_secret="secret",
            fast_route_python=Path(sys.executable),
            fast_route_scripts_dir=scripts,
        )

        with self.assertRaises(ValueError):
            await FastRouteSkillRunner(config).run("weather", "天気")


if __name__ == "__main__":
    unittest.main()
