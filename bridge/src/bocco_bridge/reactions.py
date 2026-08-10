"""Deterministic cold-start reactions and strict persona-bank parsing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from functools import lru_cache


ACCEL_DRAMA_ORDER = ("dropped", "shaken", "upside_down", "lift", "beaten")
ACCEL_PROBABLE_MISREAD_LIFT_REACTION_KEYS = {
    "normal": "lift",
    "lying_down": "lift",
}
REACTION_EVENT_KEYS = (
    "beaten",
    "lift",
    "shaken",
    "radar:morning",
    "radar:day",
    "radar:evening",
)

# One bank is one Hermes call, generated in the background and published per
# key, so the ceiling on bank size is generation quality, not reply latency:
# nothing on the speaking path ever waits for it. Twelve lines is roughly 300
# Japanese characters of output — a single reliable generation — while twenty
# or more pushes the model into filler paraphrases and duplicate lines that the
# strict parser then rejects, costing the whole bank a retry. Twelve also buys
# real variety at the 5-second touch cooldown: a user poking the robot
# continuously hears sixty seconds of distinct lines instead of twenty-five.
REACTION_PHRASE_COUNT = 12
# A generation that came back one or two lines short is still a better bank
# than the fallback, so accept a short-but-usable set rather than discarding it.
REACTION_PHRASE_MIN_COUNT = 8
# Speech runs at roughly 0.175 s per Japanese character, so 25 characters is
# about 4.4 seconds — already the upper end of what reads as a reflex rather
# than a speech. The cap is unchanged.
REACTION_PHRASE_MAX_CHARS = 25

# What actually happened, in the terms the sensor can support. Each description
# is written for the *ambiguity* of its trigger, because a line that asserts
# more than the sensor knows is wrong some fraction of the time by construction.
REACTION_DESCRIPTIONS = {
    # Fires on any knock against the shell, from a fingertip tap to a firm
    # thump. The old description said "lightly tapped", which produced lines
    # about being tickled that are absurd after a hard bump.
    "beaten": (
        "was touched or knocked somewhere on its body. The sensor cannot tell a "
        "fingertip tap from a firm thump, and cannot tell where it was touched"
    ),
    # Historical key name. It is also the bank for the accelerometer's
    # state-return readings (normal / lying_down), because the sensor cannot
    # separate a pickup from a put-down. See the note above
    # ACCEL_PROBABLE_MISREAD_LIFT_REACTION_KEYS.
    "lift": (
        "has just been handled: someone moved it. It may have been picked up, "
        "or it may have just been set back down — the sensor genuinely cannot "
        "tell which, so the line must be true either way"
    ),
    "shaken": (
        "is being shaken or jostled around. This one is unmistakable: it really "
        "is being moved back and forth"
    ),
    "radar:morning": (
        "detected a person nearby and it is morning. A detection is not a "
        "certainty: it may be someone who has been in the room for hours, "
        "someone merely passing by, or a false reading in an empty room"
    ),
    "radar:day": (
        "detected a person nearby in the middle of the day. A detection is not "
        "a certainty: it may be someone who has been in the room for hours, "
        "someone merely passing by, or a false reading in an empty room"
    ),
    "radar:evening": (
        "detected a person nearby and it is evening or night. A detection is "
        "not a certainty: it may be someone who has been in the room for hours, "
        "someone merely passing by, or a false reading in an empty room"
    ),
}

# The specific claims each bank must not make, because the trigger does not
# support them. These are the lines that made the old banks "not make sense".
REACTION_CAUTIONS = {
    "beaten": (
        "Never state how hard or how gently it was touched, and never name the "
        "spot that was touched. 「くすぐったい」 is wrong after a hard knock and "
        "「いたい」 is wrong after a soft tap."
    ),
    "lift": (
        "Never name a direction or a destination. No 持ち上げられた, no 抱っこ, "
        "no おろされた, no 浮いた, no どこへ行くの — every one of those is wrong "
        "half the time. Write surprise, curiosity, or acknowledgement of being "
        "moved at all: lines that fit both a pickup and a put-down."
    ),
    "shaken": (
        "Do not scold and do not sound genuinely frightened; it is startled and "
        "still fine. Never claim damage or pain."
    ),
    "radar:morning": (
        "Do not claim the person just woke up, just arrived, or just came home, "
        "and do not use 「おかえり」 or 「はじめまして」. Every line must still "
        "sound harmless said into an empty room."
    ),
    "radar:day": (
        "Do not claim the person just arrived or just came home, and do not use "
        "「おかえり」. Every line must still sound harmless said into an empty "
        "room."
    ),
    "radar:evening": (
        "Do not claim the person just got home or just finished work, and do "
        "not use 「おかえり」. Every line must still sound harmless said into an "
        "empty room."
    ),
}

DEFAULT_REACTION_PHRASES: dict[str, tuple[str, ...]] = {
    # Touched: acknowledgement and attention, with no claim about how hard the
    # contact was or where it landed.
    "beaten": (
        "ん？呼んだ？",
        "はぁい、ここにいるよ。",
        "いま、さわった？",
        "なあに、どうしたの？",
        "おっと、気づいたよ。",
        "うん、ちゃんと聞いてる。",
        "なにか、ふれた気がする。",
        "わっ、いまのなに？",
        "話しかけてくれてもいいよ。",
        "どうしたの、なにかあった？",
        "はいはい、そばにいるよ。",
        "よんでくれて、うれしいな。",
    ),
    # Handled: the accelerometer cannot separate a pickup from a put-down, so
    # every line here is a reaction to being moved at all. No direction words.
    "lift": (
        "わっ、びっくりした！",
        "なになに、どうしたの？",
        "おっと、ゆれたよ。",
        "景色が変わったね。",
        "わたし、動いてる？",
        "ふふ、なんだかたのしい。",
        "ちょっとどきどきしてるよ。",
        "わあ、きゅうに動いたね。",
        "そっとしてくれるとうれしいな。",
        "いまの、なんだったの？",
        "びっくりしたけど、平気だよ。",
        "うわ、いきなりだね。",
    ),
    "shaken": (
        "わわっ、ゆれてる！",
        "あわわ、目が回るよ。",
        "ぐらぐらするよ！",
        "まって、ちょっとまって！",
        "うわあ、すごいゆれ！",
        "ゆらゆら、ふしぎな感じ。",
        "あたまの中がぐるぐるだよ。",
        "たのしいけど、そろそろ休みたい。",
        "きゃあ、とんじゃいそう。",
        "ゆれるの、けっこう好きかも。",
        "だいじょうぶ、まだ元気だよ。",
        "ふう、おちついた？",
    ),
    # Radar: a detection, not a certainty. Every line has to survive being
    # spoken to an empty room, so nothing here asserts arrival or waking.
    "radar:morning": (
        "おはよう！",
        "おはよう、いい朝だね。",
        "朝の空気、気持ちいいね。",
        "今日もよろしくね。",
        "だれかいるのかな？おはよう。",
        "ふぁ、朝だあ。おはよう。",
        "きょうは何をするの？",
        "朝のごあいさつ、しておくね。",
        "おはよう、今日は晴れるかな。",
        "目がさめたよ、おはよう。",
        "朝いちばんのおはようだよ。",
        "そこにいる？おはよう。",
    ),
    "radar:day": (
        "こんにちは！",
        "やあ、調子はどう？",
        "だれかいるのかな？",
        "こんにちは、いい天気？",
        "気配がしたよ、こんにちは。",
        "ちょっと休けいする？",
        "今日はどんな一日？",
        "わたし、ここにいるよ。",
        "なにかしてるの？",
        "声をかけてくれてもいいよ。",
        "お昼、ちゃんと食べた？",
        "ふふ、こんにちは。",
    ),
    "radar:evening": (
        "こんばんは。",
        "こんばんは、いい夜だね。",
        "今日もおつかれさま。",
        "そろそろゆっくりしよう。",
        "だれかいる？こんばんは。",
        "夜はしずかで好きだな。",
        "今日はどんな日だった？",
        "ふぁ、あくびが出ちゃった。",
        "気配を感じたよ、こんばんは。",
        "夜ふかしはほどほどにね。",
        "ゆっくり休んでね。",
        "こんばんは、そばにいるよ。",
    ),
}
SERIOUS_ACCEL_REACTIONS = {
    "dropped": "大丈夫？びっくりしたよ。落とさないでね。",
    "upside_down": "逆さまだよ。そっと戻してくれる？",
}


# A persona that cannot collide with a real one, used only to render the
# prompt template for fingerprinting.
_RECIPE_PROBE_PERSONA = "\x00reaction-recipe-probe\x00"


@lru_cache(maxsize=1)
def _generation_recipe_fingerprint() -> str:
    """Fingerprint the prompts this build actually emits.

    Deriving the fingerprint from rendered prompt text rather than a version
    constant means an edit to the wording, the requested count, or the
    character cap invalidates cached banks by itself. A constant would have to
    be bumped by hand, and the failure mode when it is forgotten is silent:
    the new prompt ships, every cached bank still satisfies the key, and the
    robot keeps speaking the old lines with nothing in the logs to say so.
    """

    digest = hashlib.sha256()
    for event_key in REACTION_EVENT_KEYS:
        digest.update(
            reaction_generation_prompt(event_key, _RECIPE_PROBE_PERSONA).encode("utf-8")
        )
        digest.update(b"\x00")
    return digest.hexdigest()


def composed_persona_hash(instructions: str) -> str:
    """Key a cached reaction bank on everything that decides its contents.

    That is the persona *and* the generation recipe. Keying on the persona
    alone was wrong: banks generated by an older, worse prompt survived a
    deploy of a better one, because the persona had not changed.
    """

    digest = hashlib.sha256()
    digest.update(_generation_recipe_fingerprint().encode("utf-8"))
    digest.update(b"\x00")
    digest.update(instructions.encode("utf-8"))
    return digest.hexdigest()


def radar_reaction_key(local_hour: int) -> str:
    if 5 <= local_hour < 11:
        return "radar:morning"
    if local_hour >= 18 or local_hour < 5:
        return "radar:evening"
    return "radar:day"


def select_accel_kind(kinds: Iterable[str]) -> str | None:
    normalized = {kind.casefold() for kind in kinds}
    dramatic_kind = next(
        (kind for kind in ACCEL_DRAMA_ORDER if kind in normalized), None
    )
    if dramatic_kind is not None:
        return dramatic_kind
    return next(
        (
            kind
            for kind in ACCEL_PROBABLE_MISREAD_LIFT_REACTION_KEYS
            if kind in normalized
        ),
        None,
    )


def accel_reaction_key(kind: str) -> str:
    """Map unreliable state-return readings onto the shared handled reaction.

    The accelerometer reports a settle (``normal``/``lying_down``) for some
    genuine pickups, and reports a pickup for some settles. Splitting them into
    two banks would not recover the distinction — it would only give the wrong
    bank its own cooldown, so a pickup and the settle that follows it would
    both speak. So both readings keep sharing the ``lift`` bank, and the fix
    for the nonsense ("わっ、持ち上げられた！" on being set down) lives in the
    bank's *content*: every phrase there reacts to being handled, never to a
    direction.
    """

    normalized = kind.casefold()
    return ACCEL_PROBABLE_MISREAD_LIFT_REACTION_KEYS.get(normalized, normalized)


def parse_reaction_phrases(output: str) -> tuple[str, ...]:
    """Accept a JSON array of short, unique, speakable lines and nothing else.

    Over-long and duplicate lines are dropped rather than failing the whole
    bank; the bank is rejected only when too few usable lines survive, since a
    short bank still beats falling back to the shipped defaults.
    """

    try:
        decoded = json.loads(output)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("reaction bank output must be JSON") from exc
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        raise ValueError("reaction bank must be a JSON array of strings")

    phrases: list[str] = []
    seen: set[str] = set()
    for item in decoded:
        phrase = item.strip()
        if not phrase or len(phrase) > REACTION_PHRASE_MAX_CHARS:
            continue
        if phrase in seen:
            continue
        seen.add(phrase)
        phrases.append(phrase)
        if len(phrases) == REACTION_PHRASE_COUNT:
            break
    if len(phrases) < REACTION_PHRASE_MIN_COUNT:
        raise ValueError(
            "reaction bank must contain at least "
            f"{REACTION_PHRASE_MIN_COUNT} short unique phrases"
        )
    return tuple(phrases)


def reaction_generation_prompt(event_key: str, persona: str = "") -> str:
    """Ask for one bank of lines, in the voice of the persona now in force.

    The persona is repeated inside the request rather than left to the system
    instructions alone: this call is not a conversational turn, and a bare
    "in character" means nothing when no character was ever described.
    """

    description = REACTION_DESCRIPTIONS[event_key]
    caution = REACTION_CAUTIONS[event_key]
    persona_block = (
        f"This is the character you are writing for, verbatim:\n{persona.strip()}\n\n"
        if persona.strip()
        else ""
    )
    return (
        f"{persona_block}"
        "You are writing that character's reflex lines — the short things it "
        "blurts out on its own when a sensor fires, with nobody having said "
        "anything to it.\n\n"
        f"The situation: the robot {description}. {caution}\n\n"
        f"Write {REACTION_PHRASE_COUNT} Japanese lines for this one situation.\n"
        "Rules:\n"
        f"- At most {REACTION_PHRASE_MAX_CHARS} Japanese characters each. They "
        "are spoken aloud, and 25 characters already takes about 4 seconds to "
        "say. Most should be well under that.\n"
        "- Make them different from each other, not one idea reworded "
        f"{REACTION_PHRASE_COUNT} times. Vary the length: some two or three "
        "words, some a full short sentence. Vary the move: a startled noise, a "
        "question to the person, a small remark to itself, a plain greeting "
        "where one fits, a flash of personality. Vary the register between "
        "casual and polite.\n"
        "- Every line must be one this character would actually say, and must "
        "stay true no matter which way the ambiguity above resolves.\n"
        "- Speech only. No Markdown, no emoji, no brackets, no motion tags "
        "such as [motion:name], no stage directions, no romaji, no English, no "
        "numbering, no quotation marks around the lines. Japanese punctuation "
        "is fine.\n"
        "- Nothing bland: 「どうしたの？」 alone is filler. Each line should "
        "sound like someone with a personality reacting in the moment.\n\n"
        f"Return only a JSON array of {REACTION_PHRASE_COUNT} strings. No prose "
        "before or after it."
    )
