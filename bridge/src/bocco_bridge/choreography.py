"""Pure motion-cue parsing and speech-timing calibration helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re

from .custom_motions import COMPOSITE_MOTION_NAMES, CUSTOM_MOTION_CUE_NAMES


LOGGER = logging.getLogger(__name__)


MOTION_CUE_FAMILIES: dict[str, tuple[str, ...]] = {
    "うれしい": ("GOOD", "YES"),
    "こまった": ("WTF",),
    "びっくり": ("WHAT",),
    "うなずき": ("YES", "ALRIGHT"),
    "いやいや": ("NO",),
    "ふつう": (),
    "Sunny": ("Sunny",),
    "Rain_Little": ("Rain_Little",),
    "Rain_Hard": ("Rain_Hard",),
    "Cloudy": ("Cloudy",),
    "Hot": ("Hot",),
    "Cold": ("Cold",),
    "GoodMorning": ("GoodMorning",),
    "GoodNight": ("GoodNight",),
    "tadaima": ("tadaima",),
    "YES": ("YES",),
    "NO": ("NO",),
    "GOOD": ("GOOD",),
    "WHAT": ("WHAT",),
}
_CUE_NAMES = {
    name.casefold(): name
    for name in (
        *MOTION_CUE_FAMILIES,
        *CUSTOM_MOTION_CUE_NAMES,
        *COMPOSITE_MOTION_NAMES,
    )
}
# The keyword vocabulary the tag's opening word is matched against, kept as
# one collection so a future spelling has one obvious place to join rather
# than a second copy of the regex growing an alternation of its own. "motion"
# is what the parser has always accepted; "動き" was added after a live
# failure — the model wrote "[動き:こまった]" (the Japanese word for
# "movement," which is also how the prompt names the concept in
# MOTION_CUE_INSTRUCTIONS), the pattern matched only the literal "motion" and
# did not, and the unparsed tag was spoken aloud verbatim instead of being
# turned into a gesture. A missed cue should cost a gesture, never the
# sentence around it.
#
# Other spellings the model has been seen to reach for — "モーション" is the
# next most likely — belong here too, as entries in this tuple rather than as
# another branch of the regex.
CUE_KEYWORDS = ("motion", "動き")
_CUE_KEYWORD_PATTERN = "|".join(re.escape(keyword) for keyword in CUE_KEYWORDS)


# Unicode format characters (general category Cf): zero-width space U+200B, the
# zero-width joiner/non-joiner U+200C/U+200D, the word joiner U+2060, the BOM
# U+FEFF, the bidi and Arabic-shaping controls, the tag characters, and the rest
# of the family.  They render as nothing at all, and Python's ``\s`` does not
# match any of them — they are format characters, not whitespace — so a single
# one landing inside a cue tag defeats a pattern built out of ``\s*``.  That is
# not hypothetical: the model emitted "[<U+FEFF>motion:ふつう]" live — spelled out
# here because the character itself would show as nothing — the pattern
# missed, nothing was stripped, and the robot was handed the raw tag as the
# words to say.  Stripping the whole category once, before anything looks at the
# text, is the only version of this fix that does not need extending the next
# time a different invisible codepoint shows up.
#
# The ranges are Unicode's, not a hand-picked list; test_choreography checks them
# against ``unicodedata`` so a Unicode update that adds a format character fails
# a test here rather than going silent on the robot.  Combining marks and
# variation selectors are deliberately *not* in scope: they are invisible too,
# but they belong to the identity of the character in front of them, so removing
# them would silently rewrite legitimate text.  A format character never carries
# speech content, which is what makes dropping it safe.  The one thing it does
# carry is emoji joining (U+200D), and an emoji sequence that comes apart into
# its components is of no consequence to a text-to-speech robot whose prompts ask
# it not to emit emoji in the first place.
_FORMAT_CHARS = re.compile(
    # Written as escapes on purpose: spelled literally this class would be a
    # run of nothing, indistinguishable from an empty string, and no reviewer
    # could tell what is in it or that anything is.
    "["
    "\u00ad"  # soft hyphen
    "\u0600-\u0605\u061c\u06dd\u070f\u0890-\u0891\u08e2"  # Arabic formatting
    "\u180e"  # Mongolian vowel separator
    "\u200b-\u200f"  # zero-width space, non-joiner, joiner, LRM, RLM
    "\u202a-\u202e"  # bidi embedding and override
    "\u2060-\u2064\u2066-\u206f"  # word joiner, invisible operators, bidi
    "\ufeff"  # BOM / zero-width no-break space - the live failure
    "\ufff9-\ufffb"  # interlinear annotation
    "\U000110bd\U000110cd\U00013430-\U0001343f"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    "\U000e0001\U000e0020-\U000e007f"  # tag characters
    "]"
)


# Whitespace is tolerated after the opening bracket and around the colon: the
# model sometimes emits "[ motion:うれしい ]", and a pattern that did not match
# left the tag in the reply text, so the robot spoke it aloud. Whitespace around
# the extracted name is stripped before lookup, so padded names still resolve.
# The bracket and colon each accept their full-width twins as well — a model
# writing Japanese reaches for ［］【】and ： often enough, and every variant that
# parses is a gesture kept rather than a tag to be swept up by the backstop.
_MOTION_CUE = re.compile(
    rf"[\[［【]\s*(?:{_CUE_KEYWORD_PATTERN})\s*[:：]"
    rf"(?P<name>[^\]］】\r\n]{{1,64}})[\]］】]",
    flags=re.IGNORECASE,
)

# The sorted alternation of every name the vocabulary knows, longest first —
# used by the bare-name pattern, the backstop, and the unclosed-tag pattern.
_UNCLOSED_CUE_NAME = "|".join(
    re.escape(name) for name in sorted(_CUE_NAMES.values(), key=len, reverse=True)
)

# The model sometimes emits the bare cue name in brackets without the keyword
# prefix — "[うなずき]" instead of "[motion:うなずき]" — treating the instruction
# as "put the name in brackets" rather than "write [motion:name]."  This pattern
# matches only the names the vocabulary already knows, because a bare unknown
# word in brackets is speech, not a tag.  It is run alongside _MOTION_CUE rather
# than as part of the backstop, so a bare-name tag that the main parser cannot
# read still yields its gesture rather than costing one.
_BARE_NAME_CUE = re.compile(
    rf"[\[［【]\s*(?P<name>" + _UNCLOSED_CUE_NAME + r")[\]］】]",
    flags=re.IGNORECASE,
)

# The backstop, run over whatever survived the pass above.  A cue tag that fails
# to parse must never degrade to "the robot reads markup aloud" — that failure
# costs the whole utterance, while dropping the token costs one gesture — so
# anything still bracket-shaped and still shaped like markup is removed and
# logged.  Three shapes qualify: a bracketed token mentioning any of the cue
# keywords — "motion", "動き", and "モーション", which the parser does not accept
# but a mangled tag may still carry; any bracketed
# ASCII-keyword-colon-value pair, which is markup by construction; and a bare
# known cue name the parser should have caught — it is listed last so an
# unmatched bare name that reaches the backstop is at least stripped rather than
# spoken.  None of these occurs in speech: this robot talks in Japanese, the
# persona prompt asks for no brackets, and Japanese bracketed asides
# ("【重要】", "[大事な話]") carry neither an ASCII keyword nor a colon nor a
# bare known cue name, so they are left alone.
#
# A tag with no closing bracket at all gets a third, deliberately timid shape.
# There is no end boundary to read off the text, and a rule that guessed one
# would eat the sentence it guessed wrong about — the exact harm this exists to
# prevent — so it removes only what is certainly markup: the opening marker
# itself ("[motion:" or "[動き:"), plus the cue name if what follows it is one of the names the
# vocabulary knows.  An unclosed tag naming something unknown therefore leaves
# that unknown word in the speech.  That is the intended trade: one stray word
# spoken, against a rule licensed to delete arbitrary speech.
_CUE_SHAPED = re.compile(
    r"[\[［【]"
    r"(?:"
    rf"[^\]］】\r\n]{{0,16}}?(?:{_CUE_KEYWORD_PATTERN}|モーション)"
    rf"[^\]］】\r\n]{{0,64}}[\]］】]"
    r"|"
    r"\s*[A-Za-z]{2,16}\s*[:：][^\]］】\r\n]{0,64}[\]］】]"
    r"|"
    rf"\s*(?:{_CUE_KEYWORD_PATTERN})\s*[:：]\s*(?:" + _UNCLOSED_CUE_NAME + r")?"
    r"|"
    rf"\s*(?:" + _UNCLOSED_CUE_NAME + r")"
    r")",
    flags=re.IGNORECASE,
)

DEFAULT_DELIVERY_LAG_SECONDS = 0.5
DEFAULT_SECONDS_PER_JAPANESE_CHAR = 0.15
MIN_DELIVERY_LAG_SECONDS = 0.0
MAX_DELIVERY_LAG_SECONDS = 5.0
MIN_SECONDS_PER_CHAR = 0.05
MAX_SECONDS_PER_CHAR = 0.4
CALIBRATION_ALPHA = 0.1


@dataclass(frozen=True, slots=True)
class MotionCue:
    name: str
    char_position: int


FALLBACK_REPLY_CUE_NAME = "うなずき"


def fallback_reply_cue(text_length: int) -> MotionCue:
    """A guaranteed-motion cue for replies where the model omitted cues.

    Positioned ~40% into the utterance so the gesture lands mid-speech
    rather than colliding with the delivery animation at the start.
    """

    return MotionCue(
        FALLBACK_REPLY_CUE_NAME, max(0, int(text_length * 0.4))
    )


@dataclass(frozen=True, slots=True)
class CueExtraction:
    text: str
    cues: tuple[MotionCue, ...]


@dataclass(frozen=True, slots=True)
class SpeechCalibration:
    delivery_lag_seconds: float
    seconds_per_char: float
    sample_count: int = 0


def strip_format_chars(text: str) -> str:
    """Remove Unicode format characters (category Cf) from model-written text.

    This is the one place the invisible-character class is dealt with, and it
    runs before the cue patterns rather than being folded into them, because the
    exposure is wider than the tag.  The same stray codepoint can land inside the
    cue *name*, where it defeats the case-folded lookup against a fixed
    vocabulary and costs a gesture; or in the spoken text, where it survives into
    what is sent to the robot, into the SHA-256 the outbound echo suppressor
    matches replies against, and into the transcript the model is shown next
    turn.  A BOM that changes a hash breaks echo suppression, and a robot
    answering its own reply is a far quieter failure than this one was.  Widening
    the regexes would have fixed only the tag, and only for the codepoints named
    in the widening.

    The scan is a guard, not a rewrite: the overwhelmingly common case is text
    with no format characters at all, which costs one C-level pass and returns
    the original string untouched.  This is on the hot path for every reply.
    """

    if not _FORMAT_CHARS.search(text):
        return text
    return _FORMAT_CHARS.sub("", text)


def extract_motion_cues(text: str, *, max_cues: int = 3) -> CueExtraction:
    """Strip every cue-looking token and retain the first three known cues."""

    if max_cues < 0:
        raise ValueError("max_cues cannot be negative")
    text = strip_format_chars(text)
    # Collect every match from both patterns — the keyword-prefixed form and the
    # bare-name form — and process them in text order so that overlaps (a bare
    # name that sits inside a keyword-prefix tag) go to whichever matched first.
    raw_matches: list[tuple[int, int, str, str | None]] = []
    for match in _MOTION_CUE.finditer(text):
        raw_name = match.group("name").strip()
        canonical = _CUE_NAMES.get(raw_name.casefold())
        raw_matches.append((match.start(), match.end(), raw_name, canonical))
    for match in _BARE_NAME_CUE.finditer(text):
        raw_name = match.group("name").strip()
        canonical = _CUE_NAMES.get(raw_name.casefold())
        # _BARE_NAME_CUE is built from _CUE_NAMES, so canonical is always set,
        # but the vocabulary can differ from the names in the compiled regex
        # across patches — guard anyway.
        if canonical is not None:
            raw_matches.append((match.start(), match.end(), raw_name, canonical))

    raw_matches.sort(key=lambda m: m[0])
    pieces: list[str] = []
    cues: list[MotionCue] = []
    cursor = 0
    for start, end, raw_name, canonical in raw_matches:
        if start < cursor:
            # Overlapping tag: a bare name that sits inside a keyword-prefix tag
            # the first pass already consumed.  The second match is correct — the
            # name is still a cue — but the text between the two already belongs
            # to the first tag's removal, and reading it again would double-count.
            continue
        pieces.append(text[cursor:start])
        if canonical is None:
            LOGGER.warning("motion_cue_unknown_name name=%r", raw_name[:64])
        elif len(cues) < max_cues:
            cues.append(
                MotionCue(
                    name=canonical,
                    char_position=len(_compact("".join(pieces))),
                )
            )
        cursor = end
    pieces.append(text[cursor:])
    # The backstop runs on the joined remainder, after the parser has had its
    # turn: every tag it can read is already gone, so anything _CUE_SHAPED still
    # finds is by definition a variant the parser could not read.  Removing it
    # here rather than earlier is what keeps the two passes from fighting over
    # the same tag.  Cue positions were measured against the text before this
    # removal and may sit a few characters late as a result; they only order
    # cues against each other, which order-preserving deletion cannot disturb,
    # and they are clamped to the final length below.
    joined = "".join(pieces)
    if _CUE_SHAPED.search(joined):
        for leftover in _CUE_SHAPED.findall(joined):
            LOGGER.warning("motion_cue_unparsed token=%r", leftover[:80])
        joined = _CUE_SHAPED.sub("", joined)
    stripped = _compact(joined)
    bounded = tuple(
        MotionCue(cue.name, min(cue.char_position, len(stripped))) for cue in cues
    )
    return CueExtraction(text=stripped, cues=bounded)


def cold_start_calibration(voice_speed: int | float = 100) -> SpeechCalibration:
    if isinstance(voice_speed, bool) or not isinstance(voice_speed, (int, float)):
        voice_speed = 100
    bounded_speed = min(200.0, max(50.0, float(voice_speed)))
    rate = DEFAULT_SECONDS_PER_JAPANESE_CHAR * (100.0 / bounded_speed)
    return SpeechCalibration(
        delivery_lag_seconds=DEFAULT_DELIVERY_LAG_SECONDS,
        seconds_per_char=_clamp(
            rate, MIN_SECONDS_PER_CHAR, MAX_SECONDS_PER_CHAR
        ),
    )


def estimated_speech_duration(text_length: int, seconds_per_char: float) -> float:
    if text_length < 0:
        raise ValueError("text_length cannot be negative")
    return text_length * _clamp(
        seconds_per_char, MIN_SECONDS_PER_CHAR, MAX_SECONDS_PER_CHAR
    )


def cue_offset_seconds(
    char_position: int,
    text_length: int,
    calibration: SpeechCalibration,
    *,
    motion_transport_lag_seconds: float = 0.0,
) -> float:
    """Absolute cue delay from send time, optionally pulled earlier.

    ``motion_transport_lag_seconds`` defaults to 0 — and so does the config that
    supplies it — because pulling cues earlier makes them land mid-speech, which
    is suspected of muting the rest of the utterance; the hypothesis is
    unconfirmed and only the hardware trial in
    docs/design/motion-speech-concurrency.md would justify raising it. At 0 this
    returns exactly ``delivery_lag + cue_speech_offset_seconds(...)``: both terms
    are non-negative, so the clamp cannot alter the result.
    """

    return max(
        0.0,
        calibration.delivery_lag_seconds
        + cue_speech_offset_seconds(char_position, text_length, calibration)
        - motion_transport_lag_seconds,
    )


def cue_speech_offset_seconds(
    char_position: int,
    text_length: int,
    calibration: SpeechCalibration,
) -> float:
    """Offset from an observed speech-start anchor, excluding delivery lag."""

    if text_length <= 0:
        return 0.0
    bounded_position = min(text_length, max(0, char_position))
    progress = bounded_position / text_length
    return progress * estimated_speech_duration(
        text_length, calibration.seconds_per_char
    )


def update_calibration(
    current: SpeechCalibration,
    *,
    send_time: float,
    finished_time: float,
    text_length: int,
    alpha: float = CALIBRATION_ALPHA,
) -> SpeechCalibration:
    if text_length <= 0 or finished_time <= send_time:
        return current
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    elapsed = finished_time - send_time
    observed_lag = _clamp(
        elapsed - current.seconds_per_char * text_length,
        MIN_DELIVERY_LAG_SECONDS,
        MAX_DELIVERY_LAG_SECONDS,
    )
    observed_rate = _clamp(
        (elapsed - current.delivery_lag_seconds) / text_length,
        MIN_SECONDS_PER_CHAR,
        MAX_SECONDS_PER_CHAR,
    )
    return SpeechCalibration(
        delivery_lag_seconds=_clamp(
            (1 - alpha) * current.delivery_lag_seconds + alpha * observed_lag,
            MIN_DELIVERY_LAG_SECONDS,
            MAX_DELIVERY_LAG_SECONDS,
        ),
        seconds_per_char=_clamp(
            (1 - alpha) * current.seconds_per_char + alpha * observed_rate,
            MIN_SECONDS_PER_CHAR,
            MAX_SECONDS_PER_CHAR,
        ),
        sample_count=current.sample_count + 1,
    )


def update_delivery_anchor(
    current: SpeechCalibration,
    *,
    send_time: float,
    anchor_time: float,
    alpha: float = CALIBRATION_ALPHA,
) -> SpeechCalibration:
    """Update only message-delivery lag from send to newMessageMotion."""

    if anchor_time <= send_time:
        return current
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    observed_lag = _clamp(
        anchor_time - send_time,
        MIN_DELIVERY_LAG_SECONDS,
        MAX_DELIVERY_LAG_SECONDS,
    )
    return SpeechCalibration(
        delivery_lag_seconds=_clamp(
            (1 - alpha) * current.delivery_lag_seconds + alpha * observed_lag,
            MIN_DELIVERY_LAG_SECONDS,
            MAX_DELIVERY_LAG_SECONDS,
        ),
        seconds_per_char=current.seconds_per_char,
        sample_count=current.sample_count + 1,
    )


def update_finish_anchor(
    current: SpeechCalibration,
    *,
    speech_anchor_time: float,
    finished_time: float,
    text_length: int,
    alpha: float = CALIBRATION_ALPHA,
) -> SpeechCalibration:
    """Update speech rate from newMessageMotion to emo_talk.finished."""

    if text_length <= 0 or finished_time <= speech_anchor_time:
        return current
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    observed_rate = _clamp(
        (finished_time - speech_anchor_time) / text_length,
        MIN_SECONDS_PER_CHAR,
        MAX_SECONDS_PER_CHAR,
    )
    return SpeechCalibration(
        delivery_lag_seconds=current.delivery_lag_seconds,
        seconds_per_char=_clamp(
            (1 - alpha) * current.seconds_per_char + alpha * observed_rate,
            MIN_SECONDS_PER_CHAR,
            MAX_SECONDS_PER_CHAR,
        ),
        sample_count=current.sample_count + 1,
    )


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))
