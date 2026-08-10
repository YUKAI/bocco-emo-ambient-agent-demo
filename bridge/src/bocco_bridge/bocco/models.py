from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping


# What BOCCO puts in message.ja when speech recognition failed. The full-width
# form is the one the platform has been observed to send; the half-width
# variant is defensive. This is the only definition — the runtime imports it
# rather than restating it, so the two cannot drift apart.
STT_FAILURE_PLACEHOLDERS = frozenset(
    {
        "（文字起こしできませんでした。音声を聴いて直接ご確認ください）",
        "(文字起こしできませんでした。音声を聴いて直接ご確認ください)",
    }
)


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """The minimal, non-sensitive representation of a BOCCO Webhook event."""

    request_id: str
    event_type: str
    room_uuid: str | None
    sender_uuid: str | None
    speech_text: str | None
    received_at: datetime
    message_id: str | None = None
    message_media: str | None = None
    event_detail: str | None = None


@dataclass(frozen=True, slots=True)
class SentMessage:
    """The correlation data returned after a successful BOCCO message post."""

    message_id: str | None


@dataclass(frozen=True, slots=True)
class MotionPreset:
    """One preset motion returned by the Platform API catalog."""

    name: str
    uuid: str


@dataclass(frozen=True, slots=True)
class Stamp:
    """One native stamp returned by the Platform API catalog."""

    name: str
    uuid: str
    summary: str = ""
    image: str = ""


@dataclass(frozen=True, slots=True)
class EmoSettings:
    """The device setting needed to estimate spoken duration."""

    voice_speed: int


@dataclass(frozen=True, slots=True)
class TokenState:
    """The complete OAuth state that must be persisted as one unit."""

    access_token: str
    refresh_token: str
    access_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.access_token, str) or not self.access_token:
            raise ValueError("access_token must be a non-empty string")
        if not isinstance(self.refresh_token, str) or not self.refresh_token:
            raise ValueError("refresh_token must be a non-empty string")
        expires_at = self.access_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            raise ValueError("access_expires_at must be timezone-aware")

    def is_expired(
        self,
        *,
        now: datetime | None = None,
        skew: timedelta = timedelta(),
    ) -> bool:
        if self.access_expires_at is None:
            return False
        current = now or datetime.now(UTC)
        return self.access_expires_at <= current + skew


@dataclass(frozen=True, slots=True)
class WebhookSetting:
    url: str
    description: str
    events: tuple[str, ...]
    status: str
    secret: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> WebhookSetting:
        try:
            url = payload["url"]
            description = payload["description"]
            events = payload["events"]
            status = payload["status"]
            secret = payload["secret"]
        except KeyError as exc:
            raise ValueError("Webhook response is missing a required field") from exc

        if not isinstance(url, str) or not url:
            raise ValueError("Webhook response contains an invalid url")
        if not isinstance(description, str):
            raise ValueError("Webhook response contains an invalid description")
        if not isinstance(events, list) or not all(
            isinstance(event, str) and event for event in events
        ):
            raise ValueError("Webhook response contains invalid events")
        if not isinstance(status, str):
            raise ValueError("Webhook response contains an invalid status")
        if not isinstance(secret, str) or not secret:
            raise ValueError("Webhook response contains an invalid secret")
        return cls(
            url=url,
            description=description,
            events=tuple(events),
            status=status,
            secret=secret,
        )


@dataclass(frozen=True, slots=True)
class BoccoClientConfig:
    base_url: str = "https://platform-api.bocco.me"
    timeout_seconds: float = 10.0
    token_expiry_skew: timedelta = timedelta(seconds=30)
    webhook_events: tuple[str, ...] = (
        "message.received",
        "recording.started",
        "recording.finished",
        "radar.detected",
        "emo_talk.finished",
        "motion.finished",
        "accel.detected",
        "illuminance.changed",
    )
    webhook_description: str = "bocco-emo ambient agent"

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url:
            raise ValueError("base_url must be a non-empty string")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.token_expiry_skew < timedelta():
            raise ValueError("token_expiry_skew cannot be negative")
        if not self.webhook_events or not all(
            isinstance(event, str) and event for event in self.webhook_events
        ):
            raise ValueError("webhook_events must contain non-empty strings")
