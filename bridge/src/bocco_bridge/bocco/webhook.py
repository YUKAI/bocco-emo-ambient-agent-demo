from __future__ import annotations

import hmac
from collections.abc import Container, Mapping
from datetime import UTC, datetime
from typing import Any

from .models import InboundEvent, STT_FAILURE_PLACEHOLDERS


class WebhookValidationError(ValueError):
    """A safe, body-free error for invalid Webhook authentication or payloads."""


class InvalidWebhookSecret(WebhookValidationError):
    pass


class MalformedWebhookPayload(WebhookValidationError):
    pass


def validate_webhook_secret(provided: str | None, expected: str) -> bool:
    """Compare the platform secret in constant time for valid string inputs."""

    if not isinstance(provided, str) or not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def require_webhook_secret(provided: str | None, expected: str) -> None:
    if not validate_webhook_secret(provided, expected):
        raise InvalidWebhookSecret("Invalid BOCCO Webhook secret")


def normalize_speech_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedWebhookPayload("message text must be a string")
    normalized = value.strip()
    if not normalized or normalized in STT_FAILURE_PLACEHOLDERS:
        return None
    return normalized


def is_self_echo(event: InboundEvent, agent_user_uuid: str | None) -> bool:
    return bool(
        agent_user_uuid
        and event.event_type == "message.received"
        and event.sender_uuid == agent_user_uuid
    )


def is_duplicate_request(
    event: InboundEvent, known_request_ids: Container[str]
) -> bool:
    """Pure helper; the runtime's durable queue remains the deduplication authority."""

    return event.request_id in known_request_ids


def parse_inbound_event(payload: Mapping[str, Any]) -> InboundEvent:
    """Strictly reduce a raw BOCCO payload to the shared inbound-event contract."""

    if not isinstance(payload, Mapping):
        raise MalformedWebhookPayload("Webhook payload must be an object")

    request_id = _required_string(payload, "request_id")
    event_type = _required_string(payload, "event")
    room_uuid = _optional_string(payload, "uuid")
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise MalformedWebhookPayload("timestamp must be a Unix timestamp")
    try:
        received_at = datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise MalformedWebhookPayload("timestamp is out of range") from exc

    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise MalformedWebhookPayload("data must be an object")

    sender_uuid: str | None = None
    speech_text: str | None = None
    message_id: str | None = None
    message_media: str | None = None
    event_detail: str | None = None
    if event_type == "message.received":
        if room_uuid is None:
            raise MalformedWebhookPayload("message event is missing room uuid")
        message = _required_mapping(data, "message")
        message_id = _required_string(message, "unique_id")
        user = _required_mapping(message, "user")
        sender_uuid = _required_string(user, "uuid")
        localized_message = _required_mapping(message, "message")
        if "ja" not in localized_message:
            raise MalformedWebhookPayload("message event is missing Japanese text")
        speech_text = normalize_speech_text(localized_message["ja"])
        raw_media = message.get("media")
        if raw_media is not None and (
            not isinstance(raw_media, str) or not raw_media.strip()
        ):
            raise MalformedWebhookPayload("message media must be a non-empty string")
        audio_url = message.get("audio_url")
        if audio_url is not None and not isinstance(audio_url, str):
            raise MalformedWebhookPayload("message audio_url must be a string")
        has_audio_url = isinstance(audio_url, str) and bool(audio_url.strip())
        normalized_media = (
            raw_media.strip().casefold() if isinstance(raw_media, str) else None
        )
        if normalized_media == "stamp":
            message_media = "stamp"
        elif has_audio_url:
            message_media = "audio"
        elif normalized_media is not None:
            message_media = normalized_media
        elif speech_text is not None:
            message_media = "text"
    elif event_type == "radar.detected":
        if room_uuid is None:
            raise MalformedWebhookPayload("radar event is missing room uuid")
        radar = _required_mapping(data, "radar")
        flags = ("begin", "end", "near_begin", "near_end")
        present_flags = [radar[name] for name in flags if name in radar]
        if not present_flags or not all(isinstance(value, bool) for value in present_flags):
            raise MalformedWebhookPayload("radar event contains invalid state flags")
        if not any(present_flags):
            raise MalformedWebhookPayload("radar event has no active state flag")
    elif event_type == "emo_talk.finished":
        if room_uuid is None:
            raise MalformedWebhookPayload("talk event is missing room uuid")
        emo_talk = _required_mapping(data, "emo_talk")
        event_detail = _required_string(emo_talk, "talk")
    elif event_type == "motion.finished":
        if room_uuid is None:
            raise MalformedWebhookPayload("motion event is missing room uuid")
        motion = _required_mapping(data, "motion")
        event_detail = _required_string(motion, "kind")
    elif event_type in {"recording.started", "recording.finished"}:
        if room_uuid is None:
            raise MalformedWebhookPayload("recording event is missing room uuid")
        recording = _required_mapping(data, "recording")
        event_detail = _required_string(recording, "performed_by").casefold()
    elif event_type == "accel.detected":
        if room_uuid is None:
            raise MalformedWebhookPayload("accel event is missing room uuid")
        accel = _required_mapping(data, "accel")
        event_detail = _required_string(accel, "kind").casefold()
    elif event_type == "illuminance.changed":
        if room_uuid is None:
            raise MalformedWebhookPayload("illuminance event is missing room uuid")
        illuminance = _required_mapping(data, "illuminance")
        event_detail = _required_string(illuminance, "kind").casefold()

    return InboundEvent(
        request_id=request_id,
        event_type=event_type,
        room_uuid=room_uuid,
        sender_uuid=sender_uuid,
        speech_text=speech_text,
        received_at=received_at,
        message_id=message_id,
        message_media=message_media,
        event_detail=event_detail,
    )


def _required_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise MalformedWebhookPayload(f"{key} must be an object")
    return value


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MalformedWebhookPayload(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    if key not in payload or payload[key] is None:
        return None
    return _required_string(payload, key)
