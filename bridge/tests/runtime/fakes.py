from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from bocco_bridge.bocco import EmoSettings, MotionPreset, SentMessage, Stamp
from bocco_bridge.hermes import HermesIncompleteError


@dataclass(frozen=True)
class FakeInboundEvent:
    request_id: str
    event_type: str
    room_uuid: str | None = "room-1"
    sender_uuid: str | None = "person-1"
    speech_text: str | None = "こんにちは"
    received_at: datetime = datetime(2026, 8, 3, tzinfo=timezone.utc)
    message_id: str | None = "inbound-message-1"
    message_media: str | None = "text"
    event_detail: str | None = None


class FakeParser:
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, payload: Mapping[str, Any], received_at: datetime) -> FakeInboundEvent:
        self.calls += 1
        request_id = payload.get("request_id")
        event_type = payload.get("event")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("invalid request_id")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("invalid event")
        data = payload.get("data", {})
        if not isinstance(data, Mapping):
            raise ValueError("invalid data")
        return FakeInboundEvent(
            request_id=request_id,
            event_type=event_type,
            room_uuid=data.get("room_uuid") if isinstance(data.get("room_uuid"), str) else None,
            sender_uuid=(
                data.get("sender_uuid") if isinstance(data.get("sender_uuid"), str) else None
            ),
            speech_text=(
                data.get("speech_text") if isinstance(data.get("speech_text"), str) else None
            ),
            received_at=received_at,
            message_id=(
                data.get("message_id") if isinstance(data.get("message_id"), str) else None
            ),
            message_media=(
                data.get("message_media")
                if isinstance(data.get("message_media"), str)
                else None
            ),
            event_detail=(
                data.get("event_detail")
                if isinstance(data.get("event_detail"), str)
                else None
            ),
        )


class FakeBocco:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.registered: list[str] = []
        self.send_failures = 0
        self.message_ids: list[str | None] = []
        self.audio_attempts: list[tuple[str, bytes, str, bool]] = []
        self.audio_sent: list[tuple[str, bytes, str, bool]] = []
        self.audio_failures = 0
        self.audio_message_ids: list[str | None] = []
        self.ack_actions: list[str] = []
        self.motions: list[MotionPreset] = []
        self.motion_catalog_calls = 0
        self.stamps: list[Stamp] = []
        self.stamp_catalog_calls = 0
        self.stamp_attempts: list[tuple[str, str, str | None]] = []
        self.stamp_sent: list[tuple[str, str, str | None]] = []
        self.stamp_failures = 0
        self.stamp_message_ids: list[str | None] = []
        self.motion_attempts: list[tuple[str, str]] = []
        self.motion_sent: list[tuple[str, str]] = []
        self.motion_failures = 0
        self.motion_message_ids: list[str | None] = []
        self.custom_motion_attempts: list[tuple[str, Mapping[str, Any]]] = []
        self.custom_motion_sent: list[tuple[str, Mapping[str, Any]]] = []
        self.custom_motion_failures = 0
        self.head_angles_sent: list[tuple[str, int, int]] = []
        self.led_colors_sent: list[tuple[str, int, int, int]] = []
        self.voice_speed = 100
        self.settings_calls: list[str] = []

    async def send_text(self, room_uuid: str, text: str) -> SentMessage:
        if self.send_failures:
            self.send_failures -= 1
            raise RuntimeError("fake BOCCO failure")
        self.sent.append((room_uuid, text))
        message_id = (
            self.message_ids.pop(0)
            if self.message_ids
            else f"fake-outbound-{len(self.sent)}"
        )
        return SentMessage(message_id=message_id)

    async def list_motions(self) -> tuple[MotionPreset, ...]:
        self.motion_catalog_calls += 1
        return tuple(self.motions)

    async def list_stamps(self) -> tuple[Stamp, ...]:
        self.stamp_catalog_calls += 1
        return tuple(self.stamps)

    async def send_stamp(
        self, room_uuid: str, stamp_uuid: str, text: str | None = None
    ) -> SentMessage:
        self.stamp_attempts.append((room_uuid, stamp_uuid, text))
        if self.stamp_failures:
            self.stamp_failures -= 1
            raise RuntimeError("fake BOCCO stamp failure")
        self.stamp_sent.append((room_uuid, stamp_uuid, text))
        message_id = (
            self.stamp_message_ids.pop(0)
            if self.stamp_message_ids
            else f"fake-stamp-{len(self.stamp_sent)}"
        )
        return SentMessage(message_id=message_id)

    async def send_audio(
        self,
        room_uuid: str,
        audio: bytes,
        filename: str,
        *,
        immediate: bool = False,
    ) -> SentMessage:
        self.audio_attempts.append((room_uuid, audio, filename, immediate))
        self.ack_actions.append("audio")
        if self.audio_failures:
            self.audio_failures -= 1
            raise RuntimeError("fake BOCCO audio failure")
        self.audio_sent.append((room_uuid, audio, filename, immediate))
        message_id = (
            self.audio_message_ids.pop(0)
            if self.audio_message_ids
            else f"fake-audio-{len(self.audio_sent)}"
        )
        return SentMessage(message_id=message_id)

    async def send_motion(self, room_uuid: str, motion_uuid: str) -> SentMessage:
        self.motion_attempts.append((room_uuid, motion_uuid))
        self.ack_actions.append("motion")
        if self.motion_failures:
            self.motion_failures -= 1
            raise RuntimeError("fake BOCCO motion failure")
        self.motion_sent.append((room_uuid, motion_uuid))
        message_id = (
            self.motion_message_ids.pop(0)
            if self.motion_message_ids
            else f"fake-motion-{len(self.motion_sent)}"
        )
        return SentMessage(message_id=message_id)

    async def send_custom_motion(
        self, room_uuid: str, document: Mapping[str, Any]
    ) -> SentMessage:
        self.custom_motion_attempts.append((room_uuid, document))
        if self.custom_motion_failures:
            self.custom_motion_failures -= 1
            raise RuntimeError("fake BOCCO custom motion failure")
        self.custom_motion_sent.append((room_uuid, document))
        return SentMessage(
            message_id=f"fake-custom-motion-{len(self.custom_motion_sent)}"
        )

    async def send_head_angle(
        self, room_uuid: str, angle: int, vertical_angle: int
    ) -> SentMessage:
        self.head_angles_sent.append((room_uuid, angle, vertical_angle))
        return SentMessage(
            message_id=f"fake-head-angle-{len(self.head_angles_sent)}"
        )

    async def send_led_color(
        self, room_uuid: str, red: int, green: int, blue: int
    ) -> SentMessage:
        self.led_colors_sent.append((room_uuid, red, green, blue))
        return SentMessage(
            message_id=f"fake-led-color-{len(self.led_colors_sent)}"
        )

    async def get_emo_settings(self, room_uuid: str) -> EmoSettings:
        self.settings_calls.append(room_uuid)
        return EmoSettings(voice_speed=self.voice_speed)

    async def register_webhook(self, public_url: str) -> str:
        self.registered.append(public_url)
        return "webhook-1"


class FakeHermes:
    def __init__(self, response: str = "こんにちは！") -> None:
        self.response = response
        self.responded: list[tuple[str, str, str]] = []
        self.response_failures = 0
        self.incomplete_responses = 0

    async def respond(self, conversation: str, text: str, instructions: str) -> str:
        self.responded.append((conversation, text, instructions))
        if self.incomplete_responses:
            self.incomplete_responses -= 1
            raise HermesIncompleteError("fake Hermes output budget truncation")
        if self.response_failures:
            self.response_failures -= 1
            raise RuntimeError("fake Hermes failure")
        return self.response


class FakeStreamingHermes(FakeHermes):
    """FakeHermes that also exposes the optional respond_stream method."""

    def __init__(
        self,
        deltas: tuple[str, ...] = ("短い返事です。",),
        response: str = "短い返事です。",
    ) -> None:
        super().__init__(response)
        self.deltas = list(deltas)
        self.stream_calls: list[tuple[str, str, str]] = []
        self.stream_failures = 0
        self.fail_after_deltas: int | None = None

    async def respond_stream(
        self, conversation: str, text: str, instructions: str
    ):
        self.stream_calls.append((conversation, text, instructions))
        if self.stream_failures:
            self.stream_failures -= 1
            raise RuntimeError("fake Hermes stream failure")
        for index, delta in enumerate(self.deltas):
            if (
                self.fail_after_deltas is not None
                and index >= self.fail_after_deltas
            ):
                raise RuntimeError("fake Hermes stream interruption")
            yield delta


class FakeFastRouteSkills:
    def __init__(self) -> None:
        self.outputs = {
            "weather": "東京の今の天気は晴れです。気温は25度です。",
            "time": "今日は2026年8月3日、月曜日です。時刻は9時30分です。",
            "news": "NHKニュースの主な見出しです。1つ目、見出し一。2つ目、見出し二。",
        }
        self.calls: list[tuple[str, str, str | None]] = []
        self.failures = 0

    async def run(
        self, route: str, utterance: str, *, location: str | None = None
    ) -> str:
        self.calls.append((route, utterance, location))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("fake fast-route failure")
        return self.outputs[route]

class FakeStream:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()

    async def write(self, line: bytes) -> None:
        await self.lines.put(line)


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = FakeStream()
        self.stderr = FakeStream()
        self.returncode: int | None = None
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def exit(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self._finished.set()

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)
