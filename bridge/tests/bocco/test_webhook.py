from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bocco_bridge.bocco import (  # noqa: E402
    STT_FAILURE_PLACEHOLDERS,
    InvalidWebhookSecret,
    MalformedWebhookPayload,
    is_duplicate_request,
    is_self_echo,
    parse_inbound_event,
    require_webhook_secret,
    validate_webhook_secret,
)


def message_payload(text: str = "今日はいい天気ですね。") -> dict[str, object]:
    return {
        "request_id": "request-message-1",
        "uuid": "room-uuid",
        "serial_number": "serial",
        "nickname": "emo",
        "timestamp": 1_722_470_400,
        "event": "message.received",
        "data": {
            "message": {
                "unique_id": "message-1",
                "user": {
                    "uuid": "sender-uuid",
                    "user_type": "emo",
                    "nickname": "emo",
                },
                "message": {"ja": text},
                "media": "audio",
                "audio_url": "https://platform.invalid/private-audio",
            }
        },
        "receiver": "receiver",
    }


class WebhookTests(unittest.TestCase):
    def test_secret_uses_constant_time_compare(self) -> None:
        with patch(
            "bocco_bridge.bocco.webhook.hmac.compare_digest", return_value=True
        ) as compare:
            self.assertTrue(validate_webhook_secret("provided", "expected"))
        compare.assert_called_once_with(b"provided", b"expected")

    def test_invalid_secret_and_malformed_payload_log_no_body(self) -> None:
        with self.assertNoLogs(level="WARNING"):
            with self.assertRaises(InvalidWebhookSecret):
                require_webhook_secret("wrong", "expected")
            with self.assertRaises(MalformedWebhookPayload):
                parse_inbound_event({"private_text": "must never be logged"})

    def test_message_event_is_strictly_normalized(self) -> None:
        event = parse_inbound_event(message_payload("  こんにちは  "))

        self.assertEqual(event.request_id, "request-message-1")
        self.assertEqual(event.event_type, "message.received")
        self.assertEqual(event.room_uuid, "room-uuid")
        self.assertEqual(event.sender_uuid, "sender-uuid")
        self.assertEqual(event.message_id, "message-1")
        self.assertEqual(event.message_media, "audio")
        self.assertEqual(event.speech_text, "こんにちは")
        self.assertEqual(event.received_at, datetime.fromtimestamp(1_722_470_400, UTC))

    def test_real_redacted_fixture_accepts_speech_app_text_motion_and_radar(self) -> None:
        fixture_path = (
            Path(__file__).resolve().parents[1] / "fixtures/app_webhook_events.json"
        )
        payloads = json.loads(fixture_path.read_text(encoding="utf-8"))

        parsed = {payload["kind"]: parse_inbound_event(payload) for payload in payloads}

        self.assertEqual(set(parsed), {"speech", "app_text", "motion", "radar"})
        self.assertEqual(
            parsed["speech"].message_id,
            payloads[0]["data"]["message"]["unique_id"],
        )
        self.assertEqual(
            parsed["app_text"].message_id,
            payloads[1]["data"]["message"]["unique_id"],
        )
        self.assertEqual(parsed["speech"].sender_uuid, parsed["app_text"].sender_uuid)
        self.assertEqual(parsed["speech"].speech_text, "テストです。")
        self.assertEqual(parsed["app_text"].speech_text, "テストです。")
        self.assertEqual(parsed["speech"].message_media, "audio")
        self.assertEqual(parsed["app_text"].message_media, "text")
        self.assertEqual(parsed["motion"].message_media, "motion")
        self.assertIsNone(parsed["motion"].speech_text)
        self.assertIsNone(parsed["radar"].message_id)
        self.assertEqual(parsed["radar"].event_type, "radar.detected")

    def test_empty_and_known_placeholder_are_stt_failures(self) -> None:
        for text in ("", "   ", *STT_FAILURE_PLACEHOLDERS):
            with self.subTest(text=text):
                event = parse_inbound_event(message_payload(text))
                self.assertIsNone(event.speech_text)
                self.assertEqual(event.message_media, "audio")

    def test_non_speech_media_accepts_null_text_without_becoming_audio(self) -> None:
        payload = message_payload()
        payload["data"]["message"]["message"]["ja"] = None
        payload["data"]["message"]["media"] = "motion"
        payload["data"]["message"]["audio_url"] = ""

        event = parse_inbound_event(payload)

        self.assertIsNone(event.speech_text)
        self.assertEqual(event.message_media, "motion")

    def test_usable_audio_url_marks_message_as_audio(self) -> None:
        payload = message_payload("")
        payload["data"]["message"]["media"] = "motion"
        payload["data"]["message"]["audio_url"] = "https://platform.invalid/audio"

        event = parse_inbound_event(payload)

        self.assertEqual(event.message_media, "audio")

    def test_stamp_with_native_audio_remains_stamp_media(self) -> None:
        payload = message_payload("疑問")
        payload["data"]["message"]["media"] = "stamp"
        payload["data"]["message"]["audio_url"] = (
            "https://platform.invalid/native-stamp-audio"
        )

        event = parse_inbound_event(payload)

        self.assertEqual(event.message_media, "stamp")
        self.assertEqual(event.speech_text, "疑問")

    def test_self_echo_uses_message_sender_uuid(self) -> None:
        event = parse_inbound_event(message_payload())

        self.assertTrue(is_self_echo(event, "sender-uuid"))
        self.assertFalse(is_self_echo(event, "another-user"))
        self.assertFalse(is_self_echo(event, None))

    def test_radar_event_is_normalized_without_message_fields(self) -> None:
        event = parse_inbound_event(
            {
                "request_id": "request-radar-1",
                "uuid": "room-uuid",
                "timestamp": 1_722_470_401,
                "event": "radar.detected",
                "data": {
                    "radar": {
                        "begin": True,
                        "end": False,
                        "near_begin": False,
                        "near_end": False,
                    }
                },
            }
        )

        self.assertEqual(event.event_type, "radar.detected")
        self.assertEqual(event.room_uuid, "room-uuid")
        self.assertIsNone(event.sender_uuid)
        self.assertIsNone(event.speech_text)

    def test_talk_and_motion_finished_signals_are_strictly_normalized(self) -> None:
        cases = (
            (
                "emo_talk.finished",
                {"emo_talk": {"talk": "短い返事です。"}},
                "短い返事です。",
            ),
            ("motion.finished", {"motion": {"kind": "GOOD_01"}}, "GOOD_01"),
        )
        for index, (event_type, data, detail) in enumerate(cases):
            with self.subTest(event_type=event_type):
                event = parse_inbound_event(
                    {
                        "request_id": f"finished-{index}",
                        "uuid": "room-uuid",
                        "timestamp": 1_722_470_402 + index,
                        "event": event_type,
                        "data": data,
                    }
                )
                self.assertEqual(event.event_type, event_type)
                self.assertEqual(event.room_uuid, "room-uuid")
                self.assertEqual(event.event_detail, detail)
                self.assertIsNone(event.speech_text)

    def test_recording_telemetry_uses_performed_by(self) -> None:
        for index, event_type in enumerate(
            ("recording.started", "recording.finished")
        ):
            with self.subTest(event_type=event_type):
                event = parse_inbound_event(
                    {
                        "request_id": f"recording-{index}",
                        "uuid": "room-uuid",
                        "timestamp": 1_722_470_405 + index,
                        "event": event_type,
                        "data": {"recording": {"performed_by": "RECORD_BUTTON"}},
                    }
                )

                self.assertEqual(event.event_type, event_type)
                self.assertEqual(event.room_uuid, "room-uuid")
                self.assertEqual(event.event_detail, "record_button")
                self.assertIsNone(event.speech_text)

    def test_recording_telemetry_requires_performed_by(self) -> None:
        for event_type in ("recording.started", "recording.finished"):
            with self.subTest(event_type=event_type), self.assertRaises(
                MalformedWebhookPayload
            ):
                parse_inbound_event(
                    {
                        "request_id": f"bad-{event_type}",
                        "uuid": "room-uuid",
                        "timestamp": 1_722_470_407,
                        "event": event_type,
                        "data": {"recording": {}},
                    }
                )

    def test_accel_and_illuminance_kinds_are_strictly_normalized(self) -> None:
        cases = (
            ("accel.detected", {"accel": {"kind": "SHAKEN"}}, "shaken"),
            (
                "illuminance.changed",
                {"illuminance": {"kind": "DARKER"}},
                "darker",
            ),
        )
        for index, (event_type, data, detail) in enumerate(cases):
            with self.subTest(event_type=event_type):
                event = parse_inbound_event(
                    {
                        "request_id": f"ambient-{index}",
                        "uuid": "room-uuid",
                        "timestamp": 1_722_470_410 + index,
                        "event": event_type,
                        "data": data,
                    }
                )
                self.assertEqual(event.event_type, event_type)
                self.assertEqual(event.event_detail, detail)
                self.assertIsNone(event.speech_text)

    def test_ambient_events_require_a_kind(self) -> None:
        for event_type, data in (
            ("accel.detected", {"accel": {}}),
            ("illuminance.changed", {"illuminance": {"kind": ""}}),
        ):
            with self.subTest(event_type=event_type):
                with self.assertRaises(MalformedWebhookPayload):
                    parse_inbound_event(
                        {
                            "request_id": f"bad-{event_type}",
                            "uuid": "room-uuid",
                            "timestamp": 1_722_470_420,
                            "event": event_type,
                            "data": data,
                        }
                    )

    def test_duplicate_request_ids_remain_stable_for_durable_queue_dedup(self) -> None:
        first = parse_inbound_event(message_payload())
        duplicate_payload = copy.deepcopy(message_payload("different replay body"))
        duplicate = parse_inbound_event(duplicate_payload)

        self.assertEqual(first.request_id, duplicate.request_id)
        self.assertTrue(is_duplicate_request(duplicate, {first.request_id}))

    def test_malformed_message_and_radar_payloads_fail_closed(self) -> None:
        bad_message = message_payload()
        bad_message["data"] = {"message": {"message": {"ja": "hello"}}}
        bad_radar = {
            "request_id": "radar",
            "uuid": "room",
            "timestamp": 1_722_470_401,
            "event": "radar.detected",
            "data": {"radar": {"begin": "yes"}},
        }

        for payload in (bad_message, bad_radar):
            with self.subTest(payload=payload):
                with self.assertRaises(MalformedWebhookPayload):
                    parse_inbound_event(payload)


if __name__ == "__main__":
    unittest.main()
