"""Explicit runtime configuration for the bridge.

Library code never searches for or reads a repository ``.env`` file. Production
may inject these values through systemd's ``EnvironmentFile`` directive; tests
construct :class:`BridgeConfig` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
import os

from .choreography import MOTION_CUE_FAMILIES
from .custom_motions import CUSTOM_MOTION_NAMES
from .embeddings import EmbeddingConfig


FAST_ROUTE_NAMES = frozenset({"weather", "time", "news"})
HERMES_CONVERSATION_MODES = frozenset({"persistent", "rotating", "stateless"})
CATALOG_MOTION_FAMILIES = frozenset(
    family
    for families in MOTION_CUE_FAMILIES.values()
    for family in families
)

# The Webhook delivery repair ladder, in the order it is climbed. These are
# distinct remedies, not retries of one: re-registration is free and covers the
# likeliest cause, and only if it fails is the hostname thrown away. There is
# deliberately no rung after the recycle — that is already what a manual
# restart does, so repeating it would just churn tunnels. Each rung is
# preceded by its own probe, hence the +1 in the attempt budget below.
WEBHOOK_PROBE_REPAIR_LADDER: tuple[str, ...] = ("reregister", "recycle_tunnel")
MAX_WEBHOOK_PROBE_ATTEMPTS = len(WEBHOOK_PROBE_REPAIR_LADDER) + 1


def _is_known_thinking_motion(name: str) -> bool:
    if name in CUSTOM_MOTION_NAMES:
        return True
    normalized = name.casefold()
    return any(
        normalized == family.casefold()
        or normalized.startswith(f"{family.casefold()}_")
        for family in CATALOG_MOTION_FAMILIES
    )


# The BASE_SPEECH_INSTRUCTIONS budget was 240 characters.  It now exceeds that
# deliberately — the saved character was cheaper than the two separate failures
# where the model omitted the "motion:" prefix and invented names not in the
# list, both of which caused the robot to speak raw markup aloud.
#
# The format is stated twice ("[motion:名前]形式で", then an example) and the
# name list is prefixed with "以下の名前のみ" because the previous wording
# ("名前は…") read as a glossary rather than a closed set.  まっすぐ and the
# composite names remain available to explicit/default cues but are not
# advertised here.
MOTION_CUE_INSTRUCTIONS = (
    "[motion:名前]形式で動きを必ず1〜3個、語の直前に。"
    "以下の名前のみ: うれしい、こまった、びっくり、うなずき、いやいや、"
    "ふつう、きょろきょろ、しょんぼり、てれてれ、ぶんぶん、"
    "Sunny、Rain_Little、Rain_Hard、Cloudy、Hot、Cold、GoodMorning、GoodNight、"
    "tadaima、YES、NO、GOOD、WHAT。"
    "例: [motion:うれしい]やった！記号は読まない。"
)

BASE_SPEECH_INSTRUCTIONS = (
    "自然で短く、音声で聞き取りやすい日本語で一発話だけ返してください。"
    "Markdownや箇条書きは使わないでください。"
    + MOTION_CUE_INSTRUCTIONS
)


def _read_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _read_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _read_bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _read_optional_text(
    values: Mapping[str, str], name: str, *, decode_newlines: bool = False
) -> str:
    raw = values.get(name, "")
    if decode_newlines:
        raw = raw.replace("\\n", "\n")
    return raw.strip()


def _read_nickname_aliases(values: Mapping[str, str]) -> tuple[str, ...]:
    """Comma-separated extra names the robot answers to."""

    raw = values.get("BRIDGE_ROBOT_NICKNAME_ALIASES", "")
    return tuple(
        alias.strip() for alias in raw.split(",") if alias.strip()
    )


def _read_fast_routes(values: Mapping[str, str]) -> frozenset[str]:
    raw = values.get("BRIDGE_FAST_ROUTES")
    if raw is None:
        return FAST_ROUTE_NAMES
    routes = frozenset(
        item.strip().casefold() for item in raw.split(",") if item.strip()
    )
    unknown = routes - FAST_ROUTE_NAMES
    if unknown:
        raise ValueError(
            "BRIDGE_FAST_ROUTES contains unknown routes: "
            + ", ".join(sorted(unknown))
        )
    return routes


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Configuration owned by the runtime layer.

    The BOCCO and Hermes clients receive their own explicit config objects from
    the dependency factory; the runtime does not inspect their credentials.
    """

    webhook_secret: str
    agent_user_uuid: str | None = None
    database_path: Path = Path("/var/lib/bocco-bridge/state.db")
    memory_database_path: Path | None = None
    public_host: str = "127.0.0.1"
    public_port: int = 8787
    max_webhook_body_bytes: int = 65_536
    max_speech_chars: int = 200
    outbound_echo_window_seconds: float = 600.0
    radar_cooldown_seconds: float = 1_800.0
    accel_active_cooldown_seconds: float = 5.0
    accel_default_cooldown_seconds: float = 300.0
    # Estimate, not a calibration: custom motions have no start webhook, and
    # preset completions lack published durations. Subtracting it dispatches
    # speech-synchronised cues earlier, intending them to land while the robot
    # is still talking.
    # DEFAULT IS DELIBERATELY 0, and the reason changed once the hardware trial
    # in docs/design/motion-speech-concurrency.md was run. The original fear —
    # that a motion arriving mid-speech mutes the rest of the utterance — was
    # wrong: speech is never truncated. What the trial found instead is that the
    # device *queues* an API motion until speech ends, for presets and custom
    # documents alike. So pulling a cue earlier cannot make it land inside the
    # utterance; the device defers it regardless, and all a non-zero lag buys is
    # a gesture dispatched further from the words it was computed against.
    # Raise it only if new hardware evidence contradicts that trial. At 0 the
    # schedule is identical to the pre-compensation behaviour.
    motion_transport_lag_seconds: float = 0.0
    accel_lift_motion_name: str = "surprise-realisation"
    accel_beaten_motion_name: str = "sleepy-drift"
    accel_shaken_motion_name: str = "delight-burst"
    illuminance_cooldown_seconds: float = 28_800.0
    worker_poll_seconds: float = 0.1
    worker_max_attempts: int = 3
    worker_retry_base_seconds: float = 1.0
    stt_fallback_text: str = "ごめんなさい、うまく聞き取れませんでした。もう一度お願いします。"
    error_fallback_text: str = "ごめんなさい、今はうまくお返事できません。"
    persona: str = ""
    robot_nickname: str = ""
    # Other names the household actually uses for the robot, matched only when
    # they lead an utterance. The configured nickname is one spelling of one
    # name; a household is not. 「お嬢、今日のニュース教えて」 was addressed to
    # this robot and matched nothing, so the news route missed and the model —
    # which has no headlines — answered 「ニュースは今わからないよ。」
    #
    # Script variation does NOT belong here: 「えもちゃん」 for 「エモちゃん」
    # is the same name and is handled by folding kana before comparison.
    robot_nickname_aliases: tuple[str, ...] = ()
    motions_enabled: bool = True
    default_reply_motion: bool = True
    ack_motion_enabled: bool = True
    ack_motion_name: str = "ALRIGHT_N_0"
    thinking_motion_enabled: bool = False
    thinking_motion_name: str = "かんがえちゅう"
    thinking_motion_delay_seconds: float = 2.5
    thinking_motion_max_dispatches: int = 2
    thinking_stamp_name: str = ""
    motion_room_uuid: str = ""
    motion_chain_timeout_seconds: float = 60.0
    motion_budget_per_minute: int = 8
    fast_routes: frozenset[str] = FAST_ROUTE_NAMES
    fast_route_phrasing: bool = False
    stream_sentences: bool = True
    stream_max_chunks: int = 3
    transcript_database_path: Path | None = None
    # Retrieval-based conversational memory, and the bound it makes possible.
    # OFF BY DEFAULT: with conversation_memory_enabled false nothing is stored,
    # nothing is injected, no transcript database is created, and Hermes keeps
    # its own server-side conversation exactly as before. Turning it on stores
    # each completed exchange in the bridge and rebuilds the prompt from a
    # verbatim recent window plus BM25-retrieved older turns.
    conversation_memory_enabled: bool = False
    # Six exchanges covers three quarters of the measured conversation
    # sessions end-to-end (34 sessions, median 3.5 turns, mean 5.0) and always
    # covers the last ~8 minutes at the measured median 83 s gap between
    # utterances. At a measured median 13-character utterance and 20-character
    # reply it costs a few hundred characters — cheap insurance for pronouns
    # and "what did I just say", which retrieval alone cannot guarantee.
    conversation_recent_turns: int = 6
    conversation_retrieved_turns: int = 3
    conversation_turn_max_chars: int = 120
    conversation_max_chars: int = 1200
    conversation_retention_turns: int = 500
    # Semantic retrieval on top of BM25, served by a separate local model
    # process over loopback. OFF BY DEFAULT: with conversation_vectors_enabled
    # false no embedding service is contacted, no turn_vectors row is written
    # or read, and the injected conversation section is byte-for-byte what the
    # lexical path produced before this existed.
    #
    # It exists because BM25 is spelling-matching: 「ごはんの話したっけ」 shares
    # not one character with the stored 「君の好きな食べ物何?」/「あったかいスープが
    # 好きだよ」, and a large share of the live store is 5-10 character
    # exchanges with almost no lexical signal at all. It complements rather
    # than replaces — BM25 stays better on rare exact tokens — so the two
    # rankings are fused, never swapped.
    conversation_vectors_enabled: bool = False
    conversation_vector_service_url: str = "http://127.0.0.1:8646"
    conversation_vector_model: str = "multilingual-e5-small"
    conversation_vector_dims: int = 384
    # THE LATENCY GUARANTEE, AND THE ONLY NUMBER HERE THAT IS LOAD-BEARING.
    # Reply latency is 2-4 s and was won by two days of work; retrieval may
    # not give any of it back. The query embedding gets this long and not one
    # millisecond more, after which retrieval falls back to BM25 and the reply
    # proceeds. 120 ms is ~3x the estimated 20-45 ms encode of a short
    # utterance on the Pi's four A76 cores, so it never fires on a healthy
    # service; and a service that is down costs it at most
    # conversation_vector_breaker_failures times per cooldown, not once per
    # reply. RAISING THIS RAISES THE WORST-CASE REPLY TIME BY THE SAME AMOUNT.
    conversation_vector_query_deadline_seconds: float = 0.12
    # The write side is a background task started after the reply is spoken,
    # so it may wait for a merely-busy service instead of losing the vector.
    conversation_vector_write_deadline_seconds: float = 5.0
    conversation_vector_breaker_failures: int = 3
    conversation_vector_breaker_cooldown_seconds: float = 60.0
    # How deep each ranking goes before fusion. Deeper than the three slots
    # actually rendered, because a turn ranked fourth lexically and first
    # semantically should be able to win one.
    conversation_vector_candidates: int = 12
    # Ceiling on the brute-force sweep. Matches the retention cap, so in normal
    # operation it never binds; it is here so that raising retention cannot
    # quietly turn a 2 ms sweep into a 20 ms one.
    conversation_vector_scan_cap: int = 500
    # Cosine floor for admitting a turn to the vector ranking. 0.0 — off — is
    # the honest default: the useful band is model-specific and narrow (E5
    # similarities for unrelated Japanese pairs sit around 0.75-0.85), and
    # picking a number without live data would be guesswork that silently
    # deletes recall. Tune it from the store, not from a blog post.
    conversation_vector_min_similarity: float = 0.0
    # How the Hermes-side conversation is bounded once the bridge owns
    # context: "persistent" keeps the historical single stored conversation
    # per room, "rotating" starts a fresh one every
    # hermes_conversation_rotate_turns exchanges, "stateless" sends no
    # conversation at all and never asks Hermes to store one.
    hermes_conversation_mode: str = "persistent"
    # Kept short on purpose: the sawtooth peak is the bounded prompt plus a
    # whole epoch of Hermes-side history, and how much that costs per turn is
    # Hermes' business, not ours. Ten exchanges stayed far under the 8,000
    # token compression threshold at the ~170 tokens/turn measured live.
    hermes_conversation_rotate_turns: int = 10
    # Inventing a motion on request: the model emits a compact choreography
    # spec, the bridge renders it into a validated document, performs it, and
    # remembers the spec so it can be asked for again.
    # OFF BY DEFAULT: with motion_invention_enabled false the trigger phrase is
    # an ordinary utterance, no repertoire database is created, and no
    # generation job is ever queued.
    motion_invention_enabled: bool = False
    motion_invention_retention: int = 50
    repertoire_database_path: Path | None = None
    # Automatic event extraction: after every completed exchange, one
    # background Hermes call decides whether anything *happened* and, if so,
    # writes one dated line into a store of its own.
    #
    # OFF BY DEFAULT: with event_memory_enabled false no extraction job is ever
    # queued, no event database is created, and the composed instructions are
    # byte-for-byte what they were before this existed.
    #
    # It exists because the two memories the bridge already had could not
    # answer "what happened". The facts table only fills on 「おぼえて：」 and was
    # empty on the live robot, so every 「覚えてる？」 searched raw chat — which
    # answered 「りんご、まくらを覚えてるよ」 from a しりとり game, and replayed
    # 「いま東京は29.2度」 a day later under a 「（昨日 16:24）」 label.
    event_memory_enabled: bool = False
    # How long a completed exchange waits before it is eligible for
    # extraction. Not a rate limit — it is what keeps the model call out of the
    # gap between two sentences of a live conversation. At the measured median
    # 83 s between utterances this lands well inside the pause.
    event_extraction_delay_seconds: float = 15.0
    # Floor between two extraction calls. This is the third consumer of the one
    # background Hermes lane and by far the most frequent, so a queue that has
    # backed up drains at a rate that always leaves the lane reachable for a
    # motion invention or a reaction-bank refresh.
    event_extraction_min_interval_seconds: float = 3.0
    # Retrieval budget. Small on purpose: the audit found the transcript's own
    # retrieval load-bearing and working for topical questions, so events
    # SUPPLEMENT it and are never allowed to crowd it out. Three dated lines at
    # 60 characters is under a tenth of what the conversation block may use.
    event_memory_recalled: int = 3
    event_memory_max_chars: int = 400
    event_memory_retention: int = 2000
    event_database_path: Path | None = None
    default_location: str = ""
    fast_route_python: Path = Path("/opt/hermes-agent/.venv/bin/python")
    fast_route_scripts_dir: Path = Path(
        "/usr/local/share/bocco-bridge/fast-skills"
    )
    fast_route_timeout_seconds: float = 10.0
    cloudflared_path: str = "/usr/bin/cloudflared"
    tunnel_enabled: bool = True
    tunnel_url_timeout_seconds: float = 20.0
    tunnel_restart_seconds: float = 2.0
    # WEBHOOK DELIVERY SELF-HEAL.
    #
    # The failure it exists for: a Quick Tunnel mints a new random hostname on
    # every start, the bridge registers it, the Platform API accepts the
    # registration — and then delivers nothing, forever, with no error on
    # either side. Health is green, the tunnel is up, the registered URL
    # answers correctly from the public internet, the queue is empty, and the
    # robot is simply mute. Observed three times in one day; a bridge restart
    # (which mints a new hostname and re-registers) cleared it every time.
    #
    # ON BY DEFAULT, because the failure it covers is invisible, total, and
    # takes about twenty minutes to diagnose by hand. The price is a handful of
    # motion posts per day that command no movement and make no sound; see
    # silent_probe_document() for why that is believed true.
    webhook_probe_enabled: bool = True
    # Wait this long after a registration before probing it. Long enough for
    # the Platform side to settle and for a real message to arrive instead
    # (which proves delivery for free), short enough that a dead registration
    # is caught before anybody notices the silence.
    webhook_probe_startup_delay_seconds: float = 5.0
    # A house can legitimately be silent for hours, so silence alone is not
    # evidence of failure — it is only the trigger to go and find out. Six
    # hours means a genuinely quiet daytime house pays at most two probes.
    webhook_probe_silence_seconds: float = 21_600.0
    # Measured device delivery is 1-2.5s and message webhooks land in 2-3s;
    # twenty seconds is deliberately about an order of magnitude of headroom,
    # because a false positive here costs a tunnel recycle.
    webhook_probe_timeout_seconds: float = 20.0
    # One detection probe, then at most two remedies (re-register, then recycle
    # the tunnel), each followed by its own probe. Three is the whole budget
    # for a heal cycle; after that the bridge logs loudly and stops. It is also
    # the ceiling — see MAX_WEBHOOK_PROBE_ATTEMPTS — and may only be lowered.
    webhook_probe_max_attempts: int = MAX_WEBHOOK_PROBE_ATTEMPTS
    webhook_probe_backoff_seconds: float = 15.0
    # How long a recycled tunnel gets to publish and register a new hostname.
    webhook_probe_recycle_timeout_seconds: float = 45.0
    # Silence-triggered probes are confined to waking hours. A stalled bridge
    # discovered at 03:00 helps nobody who is asleep, and a robot that posts
    # into the family chat at 03:00 is its own bug. Registration-triggered
    # probes ignore this window: a restart is a human-caused event at a moment
    # the person is present, and it is the highest-risk moment there is.
    webhook_probe_active_start_hour: int = 8
    webhook_probe_active_end_hour: int = 22
    webhook_path: str = "/webhooks/bocco"
    dependency_factory: str = "bocco_bridge.integration:create_dependencies"
    extra: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.webhook_secret:
            raise ValueError("webhook_secret must not be empty")
        if self.agent_user_uuid is not None and not self.agent_user_uuid.strip():
            raise ValueError("agent_user_uuid must be non-empty when configured")
        if "\n" in self.robot_nickname or "\r" in self.robot_nickname:
            raise ValueError("robot_nickname must be a single line")
        if len(self.robot_nickname.strip()) > 64:
            raise ValueError("robot_nickname must be at most 64 characters")
        for alias in self.robot_nickname_aliases:
            if "\n" in alias or "\r" in alias:
                raise ValueError("robot_nickname_aliases must be single lines")
            if len(alias.strip()) > 64:
                raise ValueError(
                    "each robot_nickname_alias must be at most 64 characters"
                )
        if self.public_host != "127.0.0.1":
            raise ValueError("public listener must remain loopback-only")
        if not 1 <= self.public_port <= 65_535:
            raise ValueError("listener ports must be in the range 1..65535")
        if self.max_webhook_body_bytes <= 0:
            raise ValueError("body limit must be positive")
        if self.max_speech_chars <= 0:
            raise ValueError("max_speech_chars must be positive")
        if self.outbound_echo_window_seconds <= 0:
            raise ValueError("outbound_echo_window_seconds must be positive")
        if self.radar_cooldown_seconds <= 0:
            raise ValueError("radar_cooldown_seconds must be positive")
        if (
            self.accel_active_cooldown_seconds <= 0
            or self.accel_default_cooldown_seconds <= 0
            or self.illuminance_cooldown_seconds <= 0
        ):
            raise ValueError("ambient cooldown values must be positive")
        if self.motion_transport_lag_seconds < 0:
            raise ValueError("motion_transport_lag_seconds must be non-negative")
        for field_name in ("accel_lift_motion_name", "accel_beaten_motion_name", "accel_shaken_motion_name"):
            if not _is_known_thinking_motion(getattr(self, field_name).strip()):
                raise ValueError(f"{field_name} must name a known custom motion or catalog preset")
        if not self.ack_motion_name.strip():
            raise ValueError("ack_motion_name must not be empty")
        if "\n" in self.ack_motion_name or "\r" in self.ack_motion_name:
            raise ValueError("ack_motion_name must be a single line")
        if len(self.ack_motion_name.strip()) > 64:
            raise ValueError("ack_motion_name must be at most 64 characters")
        thinking_motion_name = self.thinking_motion_name.strip()
        if not _is_known_thinking_motion(thinking_motion_name):
            raise ValueError(
                "thinking_motion_name must name a known custom motion or catalog preset"
            )
        if self.thinking_motion_delay_seconds <= 0:
            raise ValueError("thinking_motion_delay_seconds must be positive")
        if self.thinking_motion_max_dispatches < 1:
            raise ValueError("thinking_motion_max_dispatches must be at least 1")
        if "\n" in self.thinking_stamp_name or "\r" in self.thinking_stamp_name:
            raise ValueError("thinking_stamp_name must be a single line")
        if len(self.thinking_stamp_name.strip()) > 64:
            raise ValueError("thinking_stamp_name must be at most 64 characters")
        if self.motion_chain_timeout_seconds <= 0:
            raise ValueError("motion_chain_timeout_seconds must be positive")
        if self.motion_budget_per_minute <= 0:
            raise ValueError("motion_budget_per_minute must be positive")
        if self.fast_routes - FAST_ROUTE_NAMES:
            raise ValueError("fast_routes contains an unknown route")
        if self.fast_route_timeout_seconds <= 0:
            raise ValueError("fast_route_timeout_seconds must be positive")
        if not 1 <= self.stream_max_chunks <= 3:
            raise ValueError("stream_max_chunks must be between 1 and 3")
        if self.conversation_recent_turns < 1:
            raise ValueError("conversation_recent_turns must be at least 1")
        if self.conversation_retrieved_turns < 0:
            raise ValueError("conversation_retrieved_turns must be non-negative")
        if self.conversation_turn_max_chars < 1:
            raise ValueError("conversation_turn_max_chars must be positive")
        if self.conversation_max_chars < 1:
            raise ValueError("conversation_max_chars must be positive")
        if self.conversation_retention_turns < self.conversation_recent_turns:
            raise ValueError(
                "conversation_retention_turns must retain at least the recent window"
            )
        if self.conversation_vectors_enabled and not self.conversation_memory_enabled:
            # Embedding turns that are never stored, to search a transcript
            # that is never assembled, is not a feature.
            raise ValueError(
                "conversation_vectors_enabled requires conversation_memory_enabled"
            )
        if not self.conversation_vector_service_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("conversation_vector_service_url must be an HTTP URL")
        if not self.conversation_vector_model.strip():
            raise ValueError("conversation_vector_model must not be empty")
        if self.conversation_vector_dims < 1:
            raise ValueError("conversation_vector_dims must be positive")
        if self.conversation_vector_query_deadline_seconds <= 0:
            raise ValueError(
                "conversation_vector_query_deadline_seconds must be positive"
            )
        if self.conversation_vector_query_deadline_seconds > 0.5:
            # A guard rail, not a preference. Half a second is already an eighth
            # of the slowest acceptable reply; anything beyond it means somebody
            # is trying to fix a slow service by waiting longer for it, which is
            # exactly the regression this feature promised not to cause.
            raise ValueError(
                "conversation_vector_query_deadline_seconds must stay at or "
                "below 0.5 to protect reply latency"
            )
        if self.conversation_vector_write_deadline_seconds <= 0:
            raise ValueError(
                "conversation_vector_write_deadline_seconds must be positive"
            )
        if self.conversation_vector_breaker_failures < 1:
            raise ValueError("conversation_vector_breaker_failures must be at least 1")
        if self.conversation_vector_breaker_cooldown_seconds < 0:
            raise ValueError(
                "conversation_vector_breaker_cooldown_seconds must be non-negative"
            )
        if self.conversation_vector_candidates < 1:
            raise ValueError("conversation_vector_candidates must be at least 1")
        if self.conversation_vector_scan_cap < 1:
            raise ValueError("conversation_vector_scan_cap must be at least 1")
        if not -1.0 <= self.conversation_vector_min_similarity <= 1.0:
            raise ValueError(
                "conversation_vector_min_similarity must be a cosine in [-1, 1]"
            )
        if self.hermes_conversation_mode not in HERMES_CONVERSATION_MODES:
            raise ValueError(
                "hermes_conversation_mode must be one of: "
                + ", ".join(sorted(HERMES_CONVERSATION_MODES))
            )
        if self.hermes_conversation_rotate_turns < 1:
            raise ValueError("hermes_conversation_rotate_turns must be at least 1")
        if self.motion_invention_retention < 1:
            raise ValueError("motion_invention_retention must be at least 1")
        if self.motion_invention_enabled and not self.motions_enabled:
            # An invented motion that can never be performed is not a feature.
            raise ValueError("motion_invention_enabled requires motions_enabled")
        if (
            self.hermes_conversation_mode != "persistent"
            and not self.conversation_memory_enabled
        ):
            # Cutting Hermes' own history without giving the bridge a
            # transcript to replace it would leave the robot with no memory of
            # the exchange that just happened.
            raise ValueError(
                "hermes_conversation_mode requires conversation_memory_enabled"
            )
        if self.event_memory_recalled < 0:
            raise ValueError("event_memory_recalled must be non-negative")
        if self.event_memory_max_chars < 1:
            raise ValueError("event_memory_max_chars must be positive")
        if self.event_memory_retention < 1:
            raise ValueError("event_memory_retention must be at least 1")
        if (
            self.event_extraction_delay_seconds < 0
            or self.event_extraction_min_interval_seconds < 0
        ):
            raise ValueError("event extraction timing values are invalid")
        if self.event_memory_enabled and not self.conversation_memory_enabled:
            # Extraction is driven off a completed exchange, which is exactly
            # what the transcript defines and records. More to the point, every
            # event row keeps the utterance it came from so the household can
            # check it — and checking is hollow if the conversation it quotes
            # was never stored.
            raise ValueError(
                "event_memory_enabled requires conversation_memory_enabled"
            )
        if self.worker_max_attempts <= 0:
            raise ValueError("worker_max_attempts must be positive")
        if self.worker_poll_seconds <= 0 or self.worker_retry_base_seconds < 0:
            raise ValueError("worker timing values are invalid")
        if not self.webhook_path.startswith("/"):
            raise ValueError("webhook_path must start with '/'")
        if (
            self.webhook_probe_startup_delay_seconds < 0
            or self.webhook_probe_silence_seconds <= 0
            or self.webhook_probe_timeout_seconds <= 0
            or self.webhook_probe_backoff_seconds < 0
            or self.webhook_probe_recycle_timeout_seconds <= 0
        ):
            raise ValueError("webhook probe timing values are invalid")
        if not 1 <= self.webhook_probe_max_attempts <= MAX_WEBHOOK_PROBE_ATTEMPTS:
            # Upper bound because this counts rungs on a fixed ladder, not
            # retries: there is no remedy after the recycle, so a larger budget
            # could only mean recycling the tunnel again and again — minting
            # and re-registering a hostname each time. Refuse it at startup
            # rather than quietly doing something the documentation denies.
            raise ValueError(
                "webhook_probe_max_attempts must be between 1 and "
                f"{MAX_WEBHOOK_PROBE_ATTEMPTS}"
            )
        for hour_field in (
            "webhook_probe_active_start_hour",
            "webhook_probe_active_end_hour",
        ):
            if not 0 <= getattr(self, hour_field) <= 23:
                raise ValueError(f"{hour_field} must be an hour in 0..23")
        if self.webhook_probe_active_start_hour >= self.webhook_probe_active_end_hour:
            # A window that wraps midnight would be a way to ask for 03:00
            # probes without saying so; refuse rather than silently allow it.
            raise ValueError(
                "webhook_probe_active_start_hour must precede "
                "webhook_probe_active_end_hour"
            )

    @property
    def response_instructions(self) -> str:
        """Compose environment-default character context after immutable rules."""

        return self.compose_response_instructions()

    @property
    def memory_path(self) -> Path:
        return self.memory_database_path or self.database_path.with_name("memory.db")

    @property
    def transcript_path(self) -> Path:
        """Conversation turns live beside, never inside, the facts database."""

        return self.transcript_database_path or self.database_path.with_name(
            "transcript.db"
        )

    @property
    def embedding_config(self) -> EmbeddingConfig:
        """Project the vector settings onto the embedding client's own config.

        The client takes its own frozen config rather than the whole bridge
        config, for the same reason :class:`HermesConfig` does: it is a leaf
        that must be constructible in a test without a webhook secret.
        """

        return EmbeddingConfig(
            base_url=self.conversation_vector_service_url,
            model=self.conversation_vector_model,
            dims=self.conversation_vector_dims,
            query_deadline_seconds=(
                self.conversation_vector_query_deadline_seconds
            ),
            write_deadline_seconds=(
                self.conversation_vector_write_deadline_seconds
            ),
            breaker_failures=self.conversation_vector_breaker_failures,
            breaker_cooldown_seconds=(
                self.conversation_vector_breaker_cooldown_seconds
            ),
        )

    @property
    def repertoire_path(self) -> Path:
        """Invented motions live beside, never inside, the facts database."""

        return self.repertoire_database_path or self.database_path.with_name(
            "repertoire.db"
        )

    @property
    def event_path(self) -> Path:
        """Extracted events live beside, never inside, the facts database.

        Not ``kind='event'`` in ``facts``: that table supersedes by subject, so
        a second 「歯医者」 row would deactivate the first — and two dentist
        visits on two days are both true forever.
        """

        return self.event_database_path or self.database_path.with_name(
            "events.db"
        )

    def compose_response_instructions(
        self, persona_override: str | None = None
    ) -> str:
        """Use a durable persona when present, otherwise the environment default."""

        sections = [BASE_SPEECH_INSTRUCTIONS]
        nickname = self.robot_nickname.strip()
        if nickname:
            sections.append(f"あなたは「{nickname}」という名前のロボットです。")
        persona = (
            self.persona if persona_override is None else persona_override
        ).strip()
        if persona:
            sections.append(persona)
        return "\n".join(sections)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "BridgeConfig":
        """Build config from an already-injected environment.

        This deliberately does not load a dotenv file. Values not owned by the
        runtime are retained in ``extra`` for the dependency factory.
        """

        values = dict(os.environ if environ is None else environ)
        required = {"BOCCO_WEBHOOK_SECRET": "webhook_secret"}
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise ValueError(f"missing required environment variables: {', '.join(sorted(missing))}")

        client_names = {
            "BOCCO_PLATFORM_BASE_URL",
            "BOCCO_ACCESS_TOKEN",
            "BOCCO_REFRESH_TOKEN",
            "BOCCO_TOKEN_FILE",
            "BOCCO_ACCESS_EXPIRES_AT",
            "BOCCO_API_TIMEOUT_SECONDS",
            "HERMES_API_URL",
            "API_SERVER_KEY",
            "HERMES_MODEL",
            "HERMES_RESPONSE_TIMEOUT_SECONDS",
            "HERMES_MAX_OUTPUT_TOKENS",
        }
        return cls(
            webhook_secret=values["BOCCO_WEBHOOK_SECRET"],
            agent_user_uuid=values.get("BOCCO_AGENT_USER_UUID", "").strip() or None,
            persona=_read_optional_text(
                values, "BRIDGE_PERSONA", decode_newlines=True
            ),
            robot_nickname=_read_optional_text(values, "BRIDGE_ROBOT_NICKNAME"),
            robot_nickname_aliases=_read_nickname_aliases(values),
            motions_enabled=_read_bool(values, "BRIDGE_MOTIONS_ENABLED", True),
            default_reply_motion=_read_bool(
                values, "BRIDGE_DEFAULT_REPLY_MOTION", True
            ),
            ack_motion_enabled=_read_bool(values, "BRIDGE_ACK_MOTION", True),
            ack_motion_name=values.get(
                "BRIDGE_ACK_MOTION_NAME", "ALRIGHT_N_0"
            ).strip(),
            thinking_motion_enabled=_read_bool(
                values, "BRIDGE_THINKING_MOTION_ENABLED", False
            ),
            thinking_motion_name=values.get(
                "BRIDGE_THINKING_MOTION_NAME", "かんがえちゅう"
            ).strip(),
            thinking_motion_delay_seconds=_read_float(
                values, "BRIDGE_THINKING_MOTION_DELAY_SECONDS", 2.5
            ),
            thinking_motion_max_dispatches=_read_int(
                values, "BRIDGE_THINKING_MOTION_MAX_DISPATCHES", 2
            ),
            thinking_stamp_name=_read_optional_text(
                values, "BRIDGE_THINKING_STAMP_NAME"
            ),
            motion_room_uuid=_read_optional_text(values, "BOCCO_ROOM_UUID"),
            fast_routes=_read_fast_routes(values),
            fast_route_phrasing=_read_bool(
                values, "BRIDGE_FAST_ROUTE_PHRASING", False
            ),
            stream_sentences=_read_bool(
                values, "BRIDGE_STREAM_SENTENCES", True
            ),
            default_location=_read_optional_text(values, "DEFAULT_LOCATION"),
            database_path=Path(values.get("BOCCO_BRIDGE_DB", "/var/lib/bocco-bridge/state.db")),
            memory_database_path=(
                Path(values["BOCCO_BRIDGE_MEMORY_DB"])
                if values.get("BOCCO_BRIDGE_MEMORY_DB")
                else None
            ),
            transcript_database_path=(
                Path(values["BOCCO_BRIDGE_TRANSCRIPT_DB"])
                if values.get("BOCCO_BRIDGE_TRANSCRIPT_DB")
                else None
            ),
            conversation_memory_enabled=_read_bool(
                values, "BRIDGE_CONVERSATION_MEMORY", False
            ),
            conversation_recent_turns=_read_int(
                values, "BRIDGE_CONVERSATION_RECENT_TURNS", 6
            ),
            conversation_retrieved_turns=_read_int(
                values, "BRIDGE_CONVERSATION_RETRIEVED_TURNS", 3
            ),
            conversation_turn_max_chars=_read_int(
                values, "BRIDGE_CONVERSATION_TURN_MAX_CHARS", 120
            ),
            conversation_max_chars=_read_int(
                values, "BRIDGE_CONVERSATION_MAX_CHARS", 1_200
            ),
            conversation_retention_turns=_read_int(
                values, "BRIDGE_CONVERSATION_RETENTION_TURNS", 500
            ),
            conversation_vectors_enabled=_read_bool(
                values, "BRIDGE_CONVERSATION_VECTORS", False
            ),
            conversation_vector_service_url=values.get(
                "BRIDGE_CONVERSATION_VECTOR_URL", "http://127.0.0.1:8646"
            ).strip(),
            conversation_vector_model=values.get(
                "BRIDGE_CONVERSATION_VECTOR_MODEL", "multilingual-e5-small"
            ).strip(),
            conversation_vector_dims=_read_int(
                values, "BRIDGE_CONVERSATION_VECTOR_DIMS", 384
            ),
            conversation_vector_query_deadline_seconds=_read_float(
                values, "BRIDGE_CONVERSATION_VECTOR_QUERY_DEADLINE_SECONDS", 0.12
            ),
            conversation_vector_write_deadline_seconds=_read_float(
                values, "BRIDGE_CONVERSATION_VECTOR_WRITE_DEADLINE_SECONDS", 5.0
            ),
            conversation_vector_breaker_failures=_read_int(
                values, "BRIDGE_CONVERSATION_VECTOR_BREAKER_FAILURES", 3
            ),
            conversation_vector_breaker_cooldown_seconds=_read_float(
                values, "BRIDGE_CONVERSATION_VECTOR_BREAKER_COOLDOWN_SECONDS", 60.0
            ),
            conversation_vector_candidates=_read_int(
                values, "BRIDGE_CONVERSATION_VECTOR_CANDIDATES", 12
            ),
            conversation_vector_scan_cap=_read_int(
                values, "BRIDGE_CONVERSATION_VECTOR_SCAN_CAP", 500
            ),
            conversation_vector_min_similarity=_read_float(
                values, "BRIDGE_CONVERSATION_VECTOR_MIN_SIMILARITY", 0.0
            ),
            hermes_conversation_mode=values.get(
                "BRIDGE_HERMES_CONVERSATION_MODE", "persistent"
            ).strip(),
            hermes_conversation_rotate_turns=_read_int(
                values, "BRIDGE_HERMES_CONVERSATION_ROTATE_TURNS", 10
            ),
            motion_invention_enabled=_read_bool(
                values, "BRIDGE_MOTION_INVENTION", False
            ),
            motion_invention_retention=_read_int(
                values, "BRIDGE_MOTION_INVENTION_RETENTION", 50
            ),
            repertoire_database_path=(
                Path(values["BOCCO_BRIDGE_REPERTOIRE_DB"])
                if values.get("BOCCO_BRIDGE_REPERTOIRE_DB")
                else None
            ),
            event_memory_enabled=_read_bool(values, "BRIDGE_EVENT_MEMORY", False),
            event_extraction_delay_seconds=_read_float(
                values, "BRIDGE_EVENT_EXTRACTION_DELAY_SECONDS", 15.0
            ),
            event_extraction_min_interval_seconds=_read_float(
                values, "BRIDGE_EVENT_EXTRACTION_MIN_INTERVAL_SECONDS", 3.0
            ),
            event_memory_recalled=_read_int(
                values, "BRIDGE_EVENT_MEMORY_RECALLED", 3
            ),
            event_memory_max_chars=_read_int(
                values, "BRIDGE_EVENT_MEMORY_MAX_CHARS", 400
            ),
            event_memory_retention=_read_int(
                values, "BRIDGE_EVENT_MEMORY_RETENTION", 2_000
            ),
            event_database_path=(
                Path(values["BOCCO_BRIDGE_EVENT_DB"])
                if values.get("BOCCO_BRIDGE_EVENT_DB")
                else None
            ),
            public_port=_read_int(values, "BOCCO_BRIDGE_PUBLIC_PORT", 8787),
            max_webhook_body_bytes=_read_int(values, "BOCCO_BRIDGE_MAX_WEBHOOK_BYTES", 65_536),
            max_speech_chars=_read_int(values, "BOCCO_BRIDGE_MAX_SPEECH_CHARS", 200),
            outbound_echo_window_seconds=_read_float(
                values, "BOCCO_BRIDGE_ECHO_WINDOW_SECONDS", 600.0
            ),
            radar_cooldown_seconds=_read_float(
                values, "BOCCO_BRIDGE_RADAR_COOLDOWN_SECONDS", 1_800.0
            ),
            accel_active_cooldown_seconds=_read_float(
                values, "BOCCO_BRIDGE_ACCEL_ACTIVE_COOLDOWN_SECONDS", 5.0
            ),
            accel_default_cooldown_seconds=_read_float(
                values, "BOCCO_BRIDGE_ACCEL_DEFAULT_COOLDOWN_SECONDS", 300.0
            ),
            # Default 0: see the field declaration — mid-speech cues are
            # suspected of muting speech and the hypothesis is unconfirmed.
            motion_transport_lag_seconds=_read_float(
                values, "BRIDGE_MOTION_TRANSPORT_LAG_SECONDS", 0.0
            ),
            accel_lift_motion_name=values.get("BOCCO_BRIDGE_ACCEL_LIFT_MOTION", "surprise-realisation"),
            accel_beaten_motion_name=values.get("BOCCO_BRIDGE_ACCEL_BEATEN_MOTION", "sleepy-drift"),
            accel_shaken_motion_name=values.get("BOCCO_BRIDGE_ACCEL_SHAKEN_MOTION", "delight-burst"),
            illuminance_cooldown_seconds=_read_float(
                values, "BOCCO_BRIDGE_ILLUMINANCE_COOLDOWN_SECONDS", 28_800.0
            ),
            worker_poll_seconds=_read_float(values, "BOCCO_BRIDGE_WORKER_POLL_SECONDS", 0.1),
            worker_max_attempts=_read_int(values, "BOCCO_BRIDGE_WORKER_MAX_ATTEMPTS", 3),
            worker_retry_base_seconds=_read_float(
                values, "BOCCO_BRIDGE_WORKER_RETRY_BASE_SECONDS", 1.0
            ),
            cloudflared_path=values.get("BOCCO_BRIDGE_CLOUDFLARED", "/usr/bin/cloudflared"),
            tunnel_enabled=_read_bool(values, "BOCCO_BRIDGE_TUNNEL_ENABLED", True),
            tunnel_url_timeout_seconds=_read_float(
                values, "BOCCO_BRIDGE_TUNNEL_URL_TIMEOUT_SECONDS", 20.0
            ),
            tunnel_restart_seconds=_read_float(
                values, "BOCCO_BRIDGE_TUNNEL_RESTART_SECONDS", 2.0
            ),
            webhook_probe_enabled=_read_bool(
                values, "BRIDGE_WEBHOOK_PROBE", True
            ),
            webhook_probe_startup_delay_seconds=_read_float(
                values, "BRIDGE_WEBHOOK_PROBE_STARTUP_DELAY_SECONDS", 5.0
            ),
            webhook_probe_silence_seconds=_read_float(
                values, "BRIDGE_WEBHOOK_PROBE_SILENCE_SECONDS", 21_600.0
            ),
            webhook_probe_timeout_seconds=_read_float(
                values, "BRIDGE_WEBHOOK_PROBE_TIMEOUT_SECONDS", 20.0
            ),
            webhook_probe_max_attempts=_read_int(
                values,
                "BRIDGE_WEBHOOK_PROBE_MAX_ATTEMPTS",
                MAX_WEBHOOK_PROBE_ATTEMPTS,
            ),
            webhook_probe_backoff_seconds=_read_float(
                values, "BRIDGE_WEBHOOK_PROBE_BACKOFF_SECONDS", 15.0
            ),
            webhook_probe_recycle_timeout_seconds=_read_float(
                values, "BRIDGE_WEBHOOK_PROBE_RECYCLE_TIMEOUT_SECONDS", 45.0
            ),
            webhook_probe_active_start_hour=_read_int(
                values, "BRIDGE_WEBHOOK_PROBE_START_HOUR", 8
            ),
            webhook_probe_active_end_hour=_read_int(
                values, "BRIDGE_WEBHOOK_PROBE_END_HOUR", 22
            ),
            dependency_factory=values.get(
                "BOCCO_BRIDGE_DEPENDENCY_FACTORY",
                "bocco_bridge.integration:create_dependencies",
            ),
            extra={name: values[name] for name in client_names if name in values},
        )
