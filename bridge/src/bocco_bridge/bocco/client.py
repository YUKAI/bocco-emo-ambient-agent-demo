from __future__ import annotations

import asyncio
import http.client
import json
import math
import re
import secrets
import socket
import urllib.parse
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .models import (
    BoccoClientConfig,
    EmoSettings,
    MotionPreset,
    SentMessage,
    Stamp,
    TokenState,
    WebhookSetting,
)
from .token_store import InMemoryTokenStore, TokenStore


CUSTOM_MOTION_TRACKS = frozenset(
    {
        "head",
        "antenna",
        "led_cheek_l",
        "led_cheek_r",
        "led_play",
        "led_rec",
        "led_func",
    }
)

MAX_AUDIO_MESSAGE_BYTES = 1_000_000
_AUDIO_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}
_SAFE_AUDIO_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BoccoError(RuntimeError):
    pass


class BoccoTransportError(BoccoError):
    pass


class BoccoProtocolError(BoccoError):
    pass


class BoccoApiError(BoccoError):
    """An HTTP error that intentionally excludes response bodies and credentials."""

    def __init__(self, status: int, method: str, path: str) -> None:
        super().__init__(f"BOCCO API returned HTTP {status} for {method} {path}")
        self.status = status
        self.method = method
        self.path = path


class BoccoUnauthorizedError(BoccoApiError):
    pass


class BoccoRateLimitError(BoccoApiError):
    pass


class TokenRefreshError(BoccoError):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class AsyncHttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        ...


class StdlibHttpTransport:
    """Dependency-free async transport backed by a small keep-alive pool.

    A lease is held for the complete request/response exchange, so an
    ``HTTPConnection`` is never touched by two worker threads concurrently.
    The bounded pool preserves genuine concurrent calls (including concurrent
    401s) while sequential hot-path calls reuse one warm TLS connection.
    """

    _MAX_RESPONSE_BYTES = 1_048_576
    _DEFAULT_MAX_CONNECTIONS = 4
    _DEFAULT_MAX_IDLE_SECONDS = 30.0

    def __init__(
        self,
        *,
        max_connections: int = _DEFAULT_MAX_CONNECTIONS,
        max_idle_seconds: float = _DEFAULT_MAX_IDLE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_connections < 1:
            raise ValueError("max_connections must be at least one")
        if not math.isfinite(max_idle_seconds) or max_idle_seconds <= 0:
            raise ValueError("max_idle_seconds must be positive and finite")
        self._max_connections = max_connections
        self._max_idle_seconds = max_idle_seconds
        self._monotonic = monotonic
        self._pool_changed = threading.Condition()
        self._pool: list[_PooledHttpConnection] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        return await asyncio.to_thread(
            self._request_sync,
            method,
            url,
            dict(headers),
            body,
            timeout_seconds,
        )

    def _request_sync(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise BoccoTransportError("BOCCO API request URL is invalid")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        origin = (parsed.scheme, parsed.hostname, port)
        target = urllib.parse.urlunsplit(
            ("", "", parsed.path or "/", parsed.query, "")
        )
        pooled = self._acquire(origin)
        try:
            for attempt in range(2):
                connection = self._connection_for(pooled, timeout_seconds)
                try:
                    connection.request(method, target, body=body, headers=headers)
                    response = connection.getresponse()
                    will_close = response.will_close
                    with response:
                        response_body = self._read_bounded(response)
                        result = HttpResponse(
                            status=response.status,
                            headers={
                                key.lower(): value
                                for key, value in response.headers.items()
                            },
                            body=response_body,
                        )
                    if will_close:
                        self._close_connection(pooled)
                    return result
                except BoccoProtocolError:
                    self._close_connection(pooled)
                    raise
                except (
                    http.client.HTTPException,
                    TimeoutError,
                    socket.timeout,
                    OSError,
                ) as exc:
                    self._close_connection(pooled)
                    if attempt == 0:
                        continue
                    raise BoccoTransportError("BOCCO API request failed") from exc
        finally:
            self._release(pooled)

    def _acquire(self, origin: tuple[str, str, int]) -> _PooledHttpConnection:
        with self._pool_changed:
            while True:
                now = self._monotonic()
                for pooled in self._pool:
                    if pooled.in_use or pooled.origin != origin:
                        continue
                    if (
                        pooled.connection is not None
                        and pooled.last_used_at is not None
                        and now - pooled.last_used_at >= self._max_idle_seconds
                    ):
                        self._close_connection(pooled)
                    pooled.in_use = True
                    return pooled

                if len(self._pool) < self._max_connections:
                    pooled = _PooledHttpConnection(origin=origin, in_use=True)
                    self._pool.append(pooled)
                    return pooled

                # A transport instance normally serves one BOCCO origin. If it
                # is reused for another, recycle an idle foreign-origin slot.
                for pooled in self._pool:
                    if not pooled.in_use:
                        self._close_connection(pooled)
                        pooled.origin = origin
                        pooled.in_use = True
                        return pooled
                self._pool_changed.wait()

    def _release(self, pooled: _PooledHttpConnection) -> None:
        with self._pool_changed:
            pooled.last_used_at = self._monotonic()
            pooled.in_use = False
            self._pool_changed.notify()

    def _connection_for(
        self,
        pooled: _PooledHttpConnection,
        timeout_seconds: float,
    ) -> http.client.HTTPConnection:
        connection = pooled.connection
        if connection is None:
            scheme, hostname, port = pooled.origin
            connection_type = (
                http.client.HTTPSConnection
                if scheme == "https"
                else http.client.HTTPConnection
            )
            connection = connection_type(hostname, port, timeout=timeout_seconds)
            pooled.connection = connection
        else:
            connection.timeout = timeout_seconds
            if connection.sock is not None:
                connection.sock.settimeout(timeout_seconds)
        return connection

    @staticmethod
    def _close_connection(pooled: _PooledHttpConnection) -> None:
        connection = pooled.connection
        pooled.connection = None
        pooled.last_used_at = None
        if connection is not None:
            connection.close()

    def _read_bounded(self, response: Any) -> bytes:
        body = response.read(self._MAX_RESPONSE_BYTES + 1)
        if len(body) > self._MAX_RESPONSE_BYTES:
            raise BoccoProtocolError("BOCCO API response is too large")
        return body


@dataclass(slots=True)
class _PooledHttpConnection:
    origin: tuple[str, str, int]
    connection: http.client.HTTPConnection | None = None
    last_used_at: float | None = None
    in_use: bool = False


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds cannot be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")

    def delay_for(
        self,
        retry_number: int,
        retry_after: str | None,
        *,
        now: datetime | None = None,
    ) -> float:
        """Return bounded Retry-After or exponential delay for a 1-based retry."""

        if retry_number < 1:
            raise ValueError("retry_number must be at least one")
        parsed = parse_retry_after(retry_after, now=now)
        if parsed is not None:
            return min(parsed, self.max_delay_seconds)
        exponential = self.base_delay_seconds * (2 ** (retry_number - 1))
        return min(exponential, self.max_delay_seconds)


def parse_retry_after(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        seconds = float(stripped)
    except ValueError:
        try:
            target = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        seconds = (target - current).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


class BoccoClient:
    """Async BOCCO Platform API client with safe token rotation."""

    def __init__(
        self,
        config: BoccoClientConfig,
        token_store: TokenStore,
        *,
        transport: AsyncHttpTransport | None = None,
        rate_limit_policy: RateLimitPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        parsed_base = urllib.parse.urlsplit(config.base_url)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed_base.query or parsed_base.fragment:
            raise ValueError("base_url cannot include a query or fragment")

        self.config = config
        self._base_url = config.base_url.rstrip("/")
        self._token_store = token_store
        self._tokens = token_store.load()
        self._transport = transport or StdlibHttpTransport()
        self._rate_limit_policy = rate_limit_policy or RateLimitPolicy()
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def from_tokens(
        cls,
        config: BoccoClientConfig,
        tokens: TokenState,
        **kwargs: Any,
    ) -> BoccoClient:
        """Convenience constructor for tests; production should inject durable storage."""

        return cls(config, InMemoryTokenStore(tokens), **kwargs)

    @property
    def token_state(self) -> TokenState:
        return self._tokens

    async def send_text(self, room_uuid: str, text: str) -> SentMessage:
        room = _required_argument(room_uuid, "room_uuid")
        message = _required_argument(text, "text")
        encoded_room = urllib.parse.quote(room, safe="")
        payload = await self._authorized_json(
            "POST",
            f"/v1/rooms/{encoded_room}/messages/text",
            {"text": message},
        )
        return SentMessage(message_id=_optional_identifier(payload.get("unique_id")))

    async def send_audio(
        self,
        room_uuid: str,
        audio: bytes,
        filename: str,
        *,
        immediate: bool = False,
    ) -> SentMessage:
        """Post one bounded MP3/M4A audio message without reading arbitrary files."""

        room = _required_argument(room_uuid, "room_uuid")
        body, content_type = _encode_audio_multipart(
            audio,
            filename,
            immediate=immediate,
        )
        encoded_room = urllib.parse.quote(room, safe="")
        payload = await self._authorized_body_json(
            "POST",
            f"/v1/rooms/{encoded_room}/messages/audio",
            body=body,
            content_type=content_type,
        )
        message_id = _optional_identifier(payload.get("unique_id"))
        if message_id is None:
            raise BoccoProtocolError("BOCCO audio response omitted unique_id")
        return SentMessage(message_id=message_id)

    async def send_stamp(
        self, room_uuid: str, stamp_uuid: str, text: str | None = None
    ) -> SentMessage:
        """Post one native stamp, optionally with explicit synthesized text."""

        room = _required_argument(room_uuid, "room_uuid")
        stamp = _required_argument(stamp_uuid, "stamp_uuid")
        body = {"uuid": stamp}
        if text:
            body["text"] = _required_argument(text, "text")
        encoded_room = urllib.parse.quote(room, safe="")
        payload = await self._authorized_json(
            "POST",
            f"/v1/rooms/{encoded_room}/messages/stamp",
            body,
        )
        message_id = _optional_identifier(payload.get("unique_id"))
        if message_id is None:
            raise BoccoProtocolError("BOCCO stamp response omitted unique_id")
        return SentMessage(message_id=message_id)

    async def list_stamps(self) -> tuple[Stamp, ...]:
        """Fetch the complete native-stamp catalog using offset pagination."""

        stamps: list[Stamp] = []
        offset = 0
        while True:
            payload = await self._authorized_json("GET", f"/v1/stamps?offset={offset}")
            listing = payload.get("listing")
            raw_stamps = payload.get("stamps")
            if not isinstance(listing, Mapping) or not isinstance(raw_stamps, list):
                raise BoccoProtocolError("BOCCO returned an invalid stamp catalog")
            page_offset = _non_negative_int(listing.get("offset"), "stamp offset")
            page_limit = _positive_int(listing.get("limit"), "stamp limit")
            total = _non_negative_int(listing.get("total"), "stamp total")
            if page_offset != offset or len(raw_stamps) > page_limit:
                raise BoccoProtocolError("BOCCO returned inconsistent stamp pagination")
            for item in raw_stamps:
                if not isinstance(item, Mapping):
                    raise BoccoProtocolError("BOCCO returned an invalid stamp entry")
                name = _optional_identifier(item.get("name"))
                stamp_uuid = _optional_identifier(item.get("uuid"))
                summary = item.get("summary", "")
                image = item.get("image", "")
                if (
                    name is None
                    or stamp_uuid is None
                    or not isinstance(summary, str)
                    or not isinstance(image, str)
                ):
                    raise BoccoProtocolError("BOCCO returned an invalid stamp entry")
                stamps.append(
                    Stamp(
                        name=name,
                        uuid=stamp_uuid,
                        summary=summary,
                        image=image,
                    )
                )
            next_offset = page_offset + page_limit
            if next_offset >= total:
                return tuple(stamps)
            if not raw_stamps:
                raise BoccoProtocolError("BOCCO stamp pagination did not advance")
            offset = next_offset

    async def list_motions(self) -> tuple[MotionPreset, ...]:
        """Fetch the complete preset catalog using the API's offset pagination."""

        motions: list[MotionPreset] = []
        offset = 0
        while True:
            payload = await self._authorized_json("GET", f"/v1/motions?offset={offset}")
            listing = payload.get("listing")
            raw_motions = payload.get("motions")
            if not isinstance(listing, Mapping) or not isinstance(raw_motions, list):
                raise BoccoProtocolError("BOCCO returned an invalid motion catalog")
            page_offset = _non_negative_int(listing.get("offset"), "motion offset")
            page_limit = _positive_int(listing.get("limit"), "motion limit")
            total = _non_negative_int(listing.get("total"), "motion total")
            if page_offset != offset or len(raw_motions) > page_limit:
                raise BoccoProtocolError("BOCCO returned inconsistent motion pagination")
            for item in raw_motions:
                if not isinstance(item, Mapping):
                    raise BoccoProtocolError("BOCCO returned an invalid motion entry")
                name = _optional_identifier(item.get("name"))
                motion_uuid = _optional_identifier(item.get("uuid"))
                if name is None or motion_uuid is None:
                    raise BoccoProtocolError("BOCCO returned an invalid motion entry")
                motions.append(MotionPreset(name=name, uuid=motion_uuid))
            next_offset = page_offset + page_limit
            if next_offset >= total:
                return tuple(motions)
            if not raw_motions:
                raise BoccoProtocolError("BOCCO motion pagination did not advance")
            offset = next_offset

    async def send_motion(self, room_uuid: str, motion_uuid: str) -> SentMessage:
        room = _required_argument(room_uuid, "room_uuid")
        preset = _required_argument(motion_uuid, "motion_uuid")
        encoded_room = urllib.parse.quote(room, safe="")
        payload = await self._authorized_json(
            "POST",
            f"/v1/rooms/{encoded_room}/motions/preset",
            {"uuid": preset},
        )
        return SentMessage(message_id=_optional_identifier(payload.get("unique_id")))

    async def send_head_angle(
        self, room_uuid: str, angle: int, vertical_angle: int
    ) -> SentMessage:
        """Point the head via POST /motions/move_to (no completion webhook)."""

        room = _required_argument(room_uuid, "room_uuid")
        encoded_room = urllib.parse.quote(room, safe="")
        payload = await self._authorized_json(
            "POST",
            f"/v1/rooms/{encoded_room}/motions/move_to",
            {
                "angle": _bounded_int(angle, "angle", -45, 45),
                "vertical_angle": _bounded_int(
                    vertical_angle, "vertical_angle", -20, 20
                ),
            },
        )
        return SentMessage(message_id=_optional_identifier(payload.get("unique_id")))

    async def send_led_color(
        self, room_uuid: str, red: int, green: int, blue: int
    ) -> SentMessage:
        """Flash the cheeks one color for three seconds via POST /motions/led_color."""

        room = _required_argument(room_uuid, "room_uuid")
        encoded_room = urllib.parse.quote(room, safe="")
        payload = await self._authorized_json(
            "POST",
            f"/v1/rooms/{encoded_room}/motions/led_color",
            {
                "red": _bounded_int(red, "red", 0, 255),
                "green": _bounded_int(green, "green", 0, 255),
                "blue": _bounded_int(blue, "blue", 0, 255),
            },
        )
        return SentMessage(message_id=_optional_identifier(payload.get("unique_id")))

    async def send_custom_motion(
        self, room_uuid: str, document: Mapping[str, Any]
    ) -> SentMessage:
        """Play one authored motion document via POST /motions.

        The document is the Motion Editor export format: all seven element
        tracks must be present (empty lists allowed). Deep validation is the
        caller's concern; this only rejects structurally unusable payloads.
        """

        room = _required_argument(room_uuid, "room_uuid")
        if not isinstance(document, Mapping):
            raise ValueError("document must be a mapping")
        missing = CUSTOM_MOTION_TRACKS - set(document)
        if missing:
            raise ValueError(
                "document is missing motion tracks: " + ", ".join(sorted(missing))
            )
        if not all(isinstance(document[track], list) for track in CUSTOM_MOTION_TRACKS):
            raise ValueError("motion tracks must be lists")
        encoded_room = urllib.parse.quote(room, safe="")
        payload = await self._authorized_json(
            "POST",
            f"/v1/rooms/{encoded_room}/motions",
            {track: document[track] for track in sorted(CUSTOM_MOTION_TRACKS)},
        )
        return SentMessage(message_id=_optional_identifier(payload.get("unique_id")))

    async def get_emo_settings(self, room_uuid: str) -> EmoSettings:
        room = _required_argument(room_uuid, "room_uuid")
        encoded_room = urllib.parse.quote(room, safe="")
        payload = await self._authorized_json(
            "GET", f"/v1/rooms/{encoded_room}/emo/settings"
        )
        voice_speed = _positive_int(payload.get("voice_speed"), "voice speed")
        return EmoSettings(voice_speed=voice_speed)

    async def get_webhook(self) -> WebhookSetting:
        payload = await self._authorized_json("GET", "/v1/webhook")
        return self._parse_webhook(payload)

    async def create_webhook(
        self, public_url: str, *, description: str | None = None
    ) -> WebhookSetting:
        url = _validate_public_url(public_url)
        payload = await self._authorized_json(
            "POST",
            "/v1/webhook",
            {
                "url": url,
                "description": description or self.config.webhook_description,
            },
        )
        return self._parse_webhook(payload)

    async def update_webhook(
        self, public_url: str, *, description: str | None = None
    ) -> WebhookSetting:
        url = _validate_public_url(public_url)
        payload = await self._authorized_json(
            "PUT",
            "/v1/webhook",
            {
                "url": url,
                "description": description or self.config.webhook_description,
            },
        )
        return self._parse_webhook(payload)

    async def set_webhook_events(
        self, events: tuple[str, ...] | list[str]
    ) -> WebhookSetting:
        if not events or not all(isinstance(event, str) and event for event in events):
            raise ValueError("events must contain non-empty strings")
        payload = await self._authorized_json(
            "PUT", "/v1/webhook/events", {"events": list(events)}
        )
        return self._parse_webhook(payload)

    async def register_webhook(self, public_url: str) -> str:
        """Create or update the sole Webhook, subscribe events, and return its secret."""

        try:
            await self.get_webhook()
        except BoccoApiError as exc:
            if exc.status != 404:
                raise
            await self.create_webhook(public_url)
        else:
            await self.update_webhook(public_url)
        registered = await self.set_webhook_events(self.config.webhook_events)
        return registered.secret

    async def _authorized_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._authorized_body_json(
            method,
            path,
            body=_encode_payload(payload),
            content_type="application/json",
        )

    async def _authorized_body_json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        content_type: str,
    ) -> dict[str, Any]:
        observed = self._tokens.access_token
        if self._tokens.is_expired(
            now=self._now(), skew=self.config.token_expiry_skew
        ):
            await self._refresh_tokens(observed, force=False)

        refreshed_after_401 = False
        rate_limit_retries = 0
        while True:
            request_token = self._tokens.access_token
            response = await self._transport.request(
                method,
                self._base_url + path,
                headers=_headers(request_token, content_type=content_type),
                body=body,
                timeout_seconds=self.config.timeout_seconds,
            )
            if response.status == 401:
                if refreshed_after_401:
                    raise BoccoUnauthorizedError(response.status, method, path)
                await self._refresh_tokens(request_token, force=True)
                refreshed_after_401 = True
                continue
            if response.status == 429:
                if rate_limit_retries >= self._rate_limit_policy.max_retries:
                    raise BoccoRateLimitError(response.status, method, path)
                rate_limit_retries += 1
                delay = self._rate_limit_policy.delay_for(
                    rate_limit_retries,
                    response.headers.get("retry-after"),
                    now=self._now(),
                )
                await self._sleep(delay)
                continue
            if response.status < 200 or response.status >= 300:
                raise BoccoApiError(response.status, method, path)
            return _decode_json_object(response.body)

    async def _refresh_tokens(self, observed_access_token: str, *, force: bool) -> None:
        async with self._refresh_lock:
            if self._tokens.access_token != observed_access_token:
                return
            if not force and not self._tokens.is_expired(
                now=self._now(), skew=self.config.token_expiry_skew
            ):
                return

            response = await self._request_refresh()
            if response.status != 200:
                raise TokenRefreshError(
                    f"BOCCO token refresh failed with HTTP {response.status}"
                )
            payload = _decode_json_object(response.body)
            access_token = payload.get("access_token")
            refresh_token = payload.get("refresh_token")
            if not isinstance(access_token, str) or not access_token:
                raise TokenRefreshError("BOCCO token refresh omitted access_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                raise TokenRefreshError("BOCCO token refresh omitted refresh_token")

            expires_at: datetime | None = None
            expires_in = payload.get("expires_in")
            if expires_in is not None:
                if (
                    isinstance(expires_in, bool)
                    or not isinstance(expires_in, (int, float))
                    or expires_in <= 0
                ):
                    raise TokenRefreshError("BOCCO token refresh returned invalid expiry")
                expires_at = self._now() + timedelta(seconds=float(expires_in))

            rotated = TokenState(
                access_token=access_token,
                refresh_token=refresh_token,
                access_expires_at=expires_at,
            )
            # The save must finish before in-memory publication and request retry.
            self._token_store.save(rotated)
            self._tokens = rotated

    async def _request_refresh(self) -> HttpResponse:
        body = _encode_payload({"refresh_token": self._tokens.refresh_token})
        retry_count = 0
        while True:
            response = await self._transport.request(
                "POST",
                self._base_url + "/oauth/token/refresh",
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                body=body,
                timeout_seconds=self.config.timeout_seconds,
            )
            if response.status != 429:
                return response
            if retry_count >= self._rate_limit_policy.max_retries:
                return response
            retry_count += 1
            delay = self._rate_limit_policy.delay_for(
                retry_count,
                response.headers.get("retry-after"),
                now=self._now(),
            )
            await self._sleep(delay)

    def _parse_webhook(self, payload: Mapping[str, Any]) -> WebhookSetting:
        try:
            return WebhookSetting.from_payload(payload)
        except ValueError as exc:
            raise BoccoProtocolError("BOCCO returned an invalid Webhook response") from exc


def _headers(
    access_token: str, *, content_type: str = "application/json"
) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": content_type,
    }


def _encode_payload(payload: Mapping[str, Any] | None) -> bytes | None:
    if payload is None:
        return None
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _encode_audio_multipart(
    audio: bytes,
    filename: str,
    *,
    immediate: bool,
) -> tuple[bytes, str]:
    if not isinstance(audio, bytes) or not audio:
        raise ValueError("audio must be non-empty bytes")
    if len(audio) > MAX_AUDIO_MESSAGE_BYTES:
        raise ValueError(
            f"audio must not exceed {MAX_AUDIO_MESSAGE_BYTES} bytes"
        )
    if not isinstance(filename, str) or not _SAFE_AUDIO_FILENAME.fullmatch(filename):
        raise ValueError("filename must contain only safe ASCII filename characters")
    extension = filename.lower().rsplit(".", 1)
    suffix = f".{extension[-1]}" if len(extension) == 2 else ""
    media_type = _AUDIO_CONTENT_TYPES.get(suffix)
    if media_type is None:
        raise ValueError("filename must end in .mp3 or .m4a")
    if not isinstance(immediate, bool):
        raise ValueError("immediate must be a boolean")

    boundary = f"----BoccoBridge{secrets.token_hex(16)}"
    marker = boundary.encode("ascii")
    body = b"".join(
        (
            b"--" + marker + b"\r\n",
            (
                'Content-Disposition: form-data; name="audio"; '
                f'filename="{filename}"\r\n'
            ).encode("ascii"),
            f"Content-Type: {media_type}\r\n\r\n".encode("ascii"),
            audio,
            b"\r\n--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="immediate"\r\n\r\n',
            b"true" if immediate else b"false",
            b"\r\n--" + marker + b"--\r\n",
        )
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _decode_json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoccoProtocolError("BOCCO returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BoccoProtocolError("BOCCO returned a non-object JSON response")
    return payload


def _required_argument(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BoccoProtocolError(f"BOCCO returned an invalid {name}")
    integer = int(value)
    if integer != value or integer < 0:
        raise BoccoProtocolError(f"BOCCO returned an invalid {name}")
    return integer


def _positive_int(value: object, name: str) -> int:
    integer = _non_negative_int(value, name)
    if integer == 0:
        raise BoccoProtocolError(f"BOCCO returned an invalid {name}")
    return integer


def _validate_public_url(value: str) -> str:
    url = _required_argument(value, "public_url")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("public_url cannot contain credentials or a fragment")
    return url
