"""Conservative deterministic routing to fixed, audited local skill scripts."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
import re

from .config import BridgeConfig


WEATHER_UTTERANCES = frozenset(
    {
        "天気",
        "天気は",
        "天気ですか",
        "天気を教えて",
        "今日の天気",
        "今日の天気は",
        "今日の天気ですか",
        "今日の天気を教えて",
        "今の天気",
        "今の天気は",
        "現在の天気",
        "現在の天気は",
    }
)
TIME_UTTERANCES = frozenset(
    {
        "今何時",
        "いま何時",
        "今は何時",
        "何時ですか",
        "今何時ですか",
        "今は何時ですか",
        "今日何日",
        "今日は何日",
        "今日何日ですか",
        "今日は何日ですか",
        "今日何曜日",
        "今日は何曜日",
        "今日何曜日ですか",
        "今日は何曜日ですか",
    }
)
NEWS_UTTERANCES = frozenset({"ニュース", "ニュースを教えて"})
_TRAILING_PUNCTUATION = re.compile(r"[?？。!！]+$")
# The qualifier vocabulary shared by the time and news general patterns below.
# It is deliberately the same present-tense-only list _RELATIVE_WEATHER_WORDS
# draws from: none of these routes can answer for a day other than today, so
# a qualifier that names one (明日, 来週, ...) must fall through to the model
# exactly as it does for weather.
_TODAY_QUALIFIER = r"(?:今日|きょう|本日|今|いま|現在)"
_PLACE_WEATHER = re.compile(
    r"^(?P<place>[^\s、。?？!！はをにでとがもへ]{1,10})"
    r"の天気(?:はどう|を教えて|は)?$"
)
_RELATIVE_WEATHER_WORDS = frozenset(
    {"今日", "きょう", "今", "現在", "明日", "あした", "昨日", "きのう"}
)
# The general (no-literal-match) weather question — the counterpart time and
# news already had, and weather never did.
#
# The live miss: 「エモちゃん、今日の天気はどう?」 routed nowhere and went to a
# model with no forecast. WEATHER_UTTERANCES lists 「今日の天気」 and
# 「今日の天気は」 but not 「今日の天気はどう」, and _PLACE_WEATHER cannot rescue
# it because the only place-shaped token in front of 「の天気」 is 「今日」, which
# _RELATIVE_WEATHER_WORDS correctly refuses to treat as a city.
#
# The result was that asking about somewhere else worked while asking about
# here did not: 「大阪の天気はどう」 routed, 「今日の天気はどう」 did not.
#
# Only present-tense qualifiers, exactly as time and news do, because
# weather.py answers for today and nothing else — 「明日の天気はどう」 must keep
# falling through to the model. The qualifier is optional so bare 「天気はどう」
# routes too, and fullmatch keeps 「天気の話をしよう」 out.
_GENERAL_WEATHER = re.compile(
    rf"(?:{_TODAY_QUALIFIER}[のは]?)?"
    r"天気"
    r"(?:は|を|って)?"
    r"(?:どう|どうですか|どんな感じ|教えて|教えてください|ですか|かな|なに|何)?"
)
# The general (no-literal-match) time question, following 4fbc899's shape for
# weather: TIME_UTTERANCES stays the literal fast path for the fourteen
# phrasings already known to work, so nothing that answers today can regress;
# this catches the ones nobody wrote down. 「いま何時ですか」 is the hiragana
# spelling of an entry that IS listed (今何時ですか) and still missed, because
# the set is exact strings, not a normalization.
#
# time_info.py ignores --query entirely (see hermes/fast-routes/time_info.py),
# so unlike weather there is no place token to preserve; the pattern only has
# to decide yes/no. That makes it safe to close over a handful of question
# cores instead of one fixed subject: 何時 (the hour), 何日 / 何曜日 (today's
# date and weekday, already both askable per TIME_UTTERANCES), 日付 (「今日の
# 日付」, the noun form of the 何日 question), and the fixed phrase 時間わか
# る/りますか (「時間わかる？」). 時間 alone is deliberately excluded: unlike
# 何時, it is not a clock question by itself ("時間ある?" asks for availability,
# "時間の話をしよう" is near_misses below and must not route), so it only
# appears here already welded to わかる.
#
# fullmatch anchors the same way weather's does: 「何時に帰る？」 leaves 「に
# 帰る」 unconsumed and must fall through, and a false positive here spends the
# user's turn on a canned time report instead of a real reply.
_GENERAL_TIME = re.compile(
    rf"(?:{_TODAY_QUALIFIER}(?:の|は|って)?)?"
    r"(?:"
    r"何時(?:ですか|かな)?"
    r"|何日(?:ですか)?"
    r"|何曜日(?:ですか)?"
    r"|日付(?:は|ですか)?"
    r"|時間わか(?:る|りますか)"
    r")"
)
# NEWS_UTTERANCES is two literal strings; 「ニュースは？」「今日のニュース」
# 「最新のニュース」 all miss it. Unlike weather and time there is no useful
# model fallback for a missed news question — the model has no headlines to
# report — so closing this gap is worth the same small pattern the other two
# routes get. news.py also ignores --query, so this is another yes/no-only
# pattern with no token to extract.
_GENERAL_NEWS = re.compile(
    rf"(?:{_TODAY_QUALIFIER}[のは]?|最新[のは]?)?"
    r"ニュース"
    r"(?:は|を|って)?"
    r"(?:教えて|教えてください|お願い|おねがい|ちょうだい|聞かせて|ある)?"
)
_SCRIPT_NAMES = {
    "weather": "weather.py",
    "time": "time_info.py",
    "news": "news.py",
}


def _fold_kana(text: str) -> str:
    """Katakana to hiragana, for comparison only.

    Speech-to-text picks a script for a spoken name and does not always pick
    the one the configuration was written in. The live failure: the nickname is
    configured 「エモちゃん」 and STT returned 「えもちゃん、今日の天気は?」 —
    the same syllables, the other script — so the exact prefix match missed,
    the name stayed in front of the question, and every fast route failed to
    match a question they all handle. Folding one way is enough; the folded
    form is never spoken or stored, only compared.
    """

    return "".join(
        chr(ord(character) - 0x60) if "ァ" <= character <= "ヶ" else character
        for character in text
    )


# What a real recognizer produced for a persona named 「エモちゃん」, measured
# against the live store rather than imagined. STT mangles a short name
# constantly, and the routing path sees every mangling: 「絵文字は今日のニュース
# をお願い。」 and 「え、もちゃん、今日のニュースは?」 are both this robot being
# asked for the news, and both reached a model that has no headlines.
#
# Applied only when the configured nickname is itself one of these forms — i.e.
# this is that deployment. A robot named something else gets exact matching on
# its own name and its own configured aliases, because there is no live
# measurement to generalize from for a name nobody has spoken into a microphone.
_STT_NAME_FORMS: tuple[str, ...] = (
    "エモちゃん",
    "えもちゃん",
    "メモちゃん",
    "ねもちゃん",
    "もちゃん",
    "ねこちゃん",
    "絵文字",
    "めもちゃん",
)

# Framing that can sit in front of the question without changing it.
#
# Every route ends in a `fullmatch` against the whole utterance, which is the
# safety property — it is why 「何時に帰る？」 cannot be answered with the clock.
# It is also brittle in one specific direction: anything a person says *around*
# the question defeats it, and each such phrase has been arriving as its own
# separate bug report.
#
#   「え、もちゃん、今日のニュースは?」   a hesitation, then a mangled name
#   「もう1度、今日のニュースをお願い。」  no name at all, just "once more"
#
# Rather than widening the question patterns once per phrase, the framing is
# removed before matching, from a closed list, and only when followed by a
# separator. A closed list and a required separator are what keep this from
# becoming "strip up to the first comma", which would turn 「明日、天気は」 into
# tomorrow's question answered with today's weather.
#
# None of these can change which day or place is being asked about, which is
# the only thing the three routes vary on — that is the test for admitting a
# new entry here.
_LEADING_FRAME = re.compile(
    r"^(?:"
    # hesitation
    r"えーと|えっと|あのさ|あの|ええと|ええ|ねえ|ねぇ|え|あ"
    # asking again
    r"|もう一度|もう1度|もういちど|もう一回|もう1回|もっかい|やっぱり|やっぱ"
    # softeners
    r"|ちょっと|すみません|すいません|ごめん|お願いだから"
    r")[、,，]\s*"
)

# What can sit between the address and the question.
#
# Punctuation is a character set and safe to strip greedily. A particle is not:
# these are single kana that also begin ordinary words, and `str.lstrip` takes a
# set of characters rather than a prefix — so folding them in here would turn
# 「エモちゃん、もう一度言って」 into 「う一度言って」 by eating the も of もう.
#
# So a particle is removed as a prefix, at most one, and only when it sits
# directly against the name with no punctuation between: 「絵文字は今日の…」 marks
# the robot as the topic instead of pausing after it, whereas a comma has
# already ended the address and whatever follows belongs to the question.
#
# も is deliberately not in the list even so. It is the likeliest of the three
# to open a real word (もう, もし), and unlike は it was never observed marking an
# address in the live store — the evidence does not pay for the risk.
_ADDRESS_PUNCTUATION = "、,，:： 　"
_ADDRESS_PARTICLES = ("は", "が")
# Enough for a name plus framing on both sides of it, and no more.
_MAX_FRAME_PEELS = 4


def _strip_nickname(
    text: str, robot_nickname: str, aliases: Sequence[str] = ()
) -> str:
    """Drop a leading address to the robot before route matching.

    Real usage names the robot first — 「エモちゃん、今何時」 — and every
    pattern above is written against the question that follows, not the
    greeting in front of it. Doing this once, ahead of every route, is what
    keeps weather/time/news matching the nickname identically instead of each
    route growing its own copy of "maybe there's a name first" and drifting
    out of sync with the other two.

    Matching is deliberately narrow rather than "strip whatever precedes the
    first comma". A leading comma-separated token is not always an address:
    「明日、天気は？」 opens with one, and eating it would change the question
    into a different question. Only a known name is removed — the configured
    nickname, or one of the household's other names for the robot — so a word
    that merely sits in that position is left alone.
    """

    candidates = [robot_nickname, *aliases]
    folded_nickname = _fold_kana(robot_nickname.strip())
    if folded_nickname and any(
        folded_nickname == _fold_kana(form) for form in _STT_NAME_FORMS
    ):
        candidates.extend(_STT_NAME_FORMS)

    # Longest first: 「もちゃん」 is a suffix of 「えもちゃん」, and matching the
    # short form first would strand 「え」 at the front of the residue.
    names = sorted(
        {name.strip() for name in candidates if name.strip()},
        key=len,
        reverse=True,
    )
    # Folded once, not once per name per peel: this runs on every utterance,
    # and the peel loop would otherwise refold the whole list up to four times.
    folded_names = [(name, _fold_kana(name)) for name in names]

    def peel_name(candidate: str) -> str | None:
        folded = _fold_kana(candidate)
        for name, folded_name in folded_names:
            if not folded.startswith(folded_name):
                continue
            # Slice the original, not the folded copy: folding is one codepoint
            # for one codepoint, so the offset holds, and the text that
            # survives is the text the user actually said.
            residue = candidate[len(name) :]
            if residue[:1] in _ADDRESS_PARTICLES:
                residue = residue[1:]
            return residue.lstrip(_ADDRESS_PUNCTUATION).strip()
        return None

    # Framing and the address can arrive in either order and in either
    # combination — 「え、もちゃん、…」 is filler then name, 「エモちゃん、もう1度、
    # …」 is the reverse, and 「もう1度、…」 is framing with no name at all. Peel
    # whichever is in front until neither is, bounded so a pathological string
    # cannot spin.
    current = text
    for _ in range(_MAX_FRAME_PEELS):
        before = current
        peeled = peel_name(current)
        if peeled is not None:
            current = peeled
            continue
        current = _LEADING_FRAME.sub("", current, count=1)
        if current == before:
            break
    return current.strip()


def detect_fast_route(
    text: str | None,
    enabled_routes: frozenset[str],
    *,
    robot_nickname: str = "",
    robot_nickname_aliases: Sequence[str] = (),
) -> str | None:
    if not isinstance(text, str):
        return None
    normalized = _TRAILING_PUNCTUATION.sub("", text.strip()).strip()
    normalized = _strip_nickname(normalized, robot_nickname, robot_nickname_aliases)
    if not normalized:
        return None
    if "weather" in enabled_routes and len(normalized) < 20:
        if normalized in WEATHER_UTTERANCES:
            return "weather"
        # Checked before the place extractor: this admits only present-tense
        # qualifiers, so a named city cannot match it and still reaches the
        # extractor below with its place token intact.
        if _GENERAL_WEATHER.fullmatch(normalized) is not None:
            return "weather"
        # Checked last so a named city keeps its own path: the general pattern
        # only admits present-tense qualifiers, so 「大阪の天気はどう」 falls
        # through to here and still yields its place token to the caller.
        # normalized is already nickname-stripped, so the extraction does not
        # need robot_nickname passed again.
        if extract_weather_location(normalized) is not None:
            return "weather"
    if "time" in enabled_routes:
        if normalized in TIME_UTTERANCES:
            return "time"
        if _GENERAL_TIME.fullmatch(normalized) is not None:
            return "time"
    if "news" in enabled_routes:
        if normalized in NEWS_UTTERANCES:
            return "news"
        if _GENERAL_NEWS.fullmatch(normalized) is not None:
            return "news"
    return None


def extract_weather_location(
    text: str | None,
    *,
    robot_nickname: str = "",
    robot_nickname_aliases: Sequence[str] = (),
) -> str | None:
    """Extract only an anchored short place token from a city weather form."""

    if not isinstance(text, str):
        return None
    normalized = _TRAILING_PUNCTUATION.sub("", text.strip()).strip()
    normalized = _strip_nickname(normalized, robot_nickname, robot_nickname_aliases)
    if not normalized or len(normalized) >= 20:
        return None
    match = _PLACE_WEATHER.fullmatch(normalized)
    if match is None:
        return None
    place = match.group("place")
    if place in _RELATIVE_WEATHER_WORDS:
        return None
    return place


class FastRouteSkillRunner:
    """Run a fixed script without a shell or access to bridge credentials."""

    def __init__(self, config: BridgeConfig) -> None:
        self.python = config.fast_route_python
        self.scripts_dir = config.fast_route_scripts_dir
        self.default_location = config.default_location
        self.timeout_seconds = config.fast_route_timeout_seconds

    async def run(
        self, route: str, utterance: str, *, location: str | None = None
    ) -> str:
        script_name = _SCRIPT_NAMES.get(route)
        if script_name is None:
            raise ValueError("unknown fast route")
        script = self.scripts_dir / script_name
        _require_fixed_child(script, self.scripts_dir)
        if location is not None and route != "weather":
            raise ValueError("location is supported only for weather")
        arguments = [str(self.python), str(script), "--query", utterance]
        if location is not None:
            arguments.extend(("--location", location))
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=str(self.scripts_dir),
            env={
                "DEFAULT_LOCATION": self.default_location,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONIOENCODING": "utf-8",
            },
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("fast-route skill timed out") from None
        if process.returncode != 0:
            raise RuntimeError("fast-route skill failed")
        if len(stdout) > 8_192:
            raise RuntimeError("fast-route skill output was too large")
        result = stdout.decode("utf-8", errors="strict").strip()
        if not result:
            raise RuntimeError("fast-route skill returned no data")
        return result


def _require_fixed_child(script: Path, scripts_dir: Path) -> None:
    if script.parent != scripts_dir or script.name not in _SCRIPT_NAMES.values():
        raise ValueError("fast-route script path is not fixed")
    # A fixed parent and name are not enough on their own: a symlink planted
    # under the scripts directory keeps both and still redirects execution
    # elsewhere.  Require a real file whose resolved location is still inside
    # the configured directory.  Both sides are resolved so that a scripts
    # directory reached through a symlink is not rejected by itself.
    if script.is_symlink():
        raise ValueError("fast-route script is a symlink")
    if not script.exists():
        raise FileNotFoundError(f"fast-route script is not installed: {script}")
    if not script.is_file():
        raise ValueError("fast-route script is not a regular file")
    if script.resolve().parent != scripts_dir.resolve():
        raise ValueError("fast-route script resolves outside its directory")
