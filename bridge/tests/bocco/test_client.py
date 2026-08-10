from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bocco_bridge.bocco import (  # noqa: E402
    BoccoClient,
    BoccoClientConfig,
    BoccoProtocolError,
    BoccoUnauthorizedError,
    InMemoryTokenStore,
    RateLimitPolicy,
    Stamp,
    StdlibHttpTransport,
    TokenState,
)


Response = tuple[int, dict[str, str], dict[str, Any] | bytes | None]
RequestBody = dict[str, Any] | bytes | None
Callback = Callable[[str, str, dict[str, str], RequestBody], Response]


class DropConnection(Exception):
    """Tell the local test server to close without writing an HTTP response."""


class FakePlatformServer:
    def __init__(self, callback: Callback, *, keep_alive: bool = False) -> None:
        self.callback = callback
        self.connection_count = 0
        self.request_connection_numbers: list[int] = []
        self._connection_lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if keep_alive else "HTTP/1.0"

            def setup(self) -> None:
                super().setup()
                with owner._connection_lock:
                    owner.connection_count += 1
                    self.connection_number = owner.connection_count

            def do_GET(self) -> None:
                self._dispatch()

            def do_POST(self) -> None:
                self._dispatch()

            def do_PUT(self) -> None:
                self._dispatch()

            def _dispatch(self) -> None:
                with owner._connection_lock:
                    owner.request_connection_numbers.append(self.connection_number)
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length) if content_length else b""
                content_type = self.headers.get("Content-Type", "")
                request_body = (
                    json.loads(raw_body)
                    if raw_body and content_type.startswith("application/json")
                    else raw_body or None
                )
                try:
                    status, headers, response_body = owner.callback(
                        self.command,
                        self.path,
                        {key.lower(): value for key, value in self.headers.items()},
                        request_body,
                    )
                except DropConnection:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if isinstance(response_body, dict):
                    encoded = json.dumps(response_body).encode("utf-8")
                    self.send_header("Content-Type", "application/json")
                else:
                    encoded = response_body or b""
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class RecordingStore(InMemoryTokenStore):
    def __init__(self, tokens: TokenState, order: list[str] | None = None) -> None:
        super().__init__(tokens)
        self.order = order
        self.saved: list[TokenState] = []

    def save(self, tokens: TokenState) -> None:
        if self.order is not None:
            self.order.append("save")
        self.saved.append(tokens)
        super().save(tokens)


class BoccoClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(
        self,
        server: FakePlatformServer,
        store: InMemoryTokenStore,
        **kwargs: Any,
    ) -> BoccoClient:
        return BoccoClient(
            BoccoClientConfig(
                base_url=server.url,
                timeout_seconds=2,
                token_expiry_skew=timedelta(),
            ),
            store,
            **kwargs,
        )

    async def test_send_text_returns_platform_unique_id(self) -> None:
        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/rooms/room/messages/text")
            self.assertEqual(body, {"text": "hello"})
            return 200, {}, {"unique_id": "outbound-message-1"}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        sent = await client.send_text("room", "hello")

        self.assertEqual(sent.message_id, "outbound-message-1")

    async def test_send_text_allows_missing_id_for_hash_fallback(self) -> None:
        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            return 200, {}, {}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        sent = await client.send_text("room", "hello")

        self.assertIsNone(sent.message_id)

    async def test_sequential_requests_reuse_one_persistent_connection(self) -> None:
        requests = 0

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            nonlocal requests
            requests += 1
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/rooms/room/messages/text")
            response_headers = {"Connection": "close"} if requests == 3 else {}
            return 200, response_headers, {"unique_id": f"message-{requests}"}

        server = FakePlatformServer(callback, keep_alive=True)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        sent = [
            await client.send_text("room", f"message {index}")
            for index in range(3)
        ]

        self.assertEqual(
            [message.message_id for message in sent],
            ["message-1", "message-2", "message-3"],
        )
        self.assertEqual(server.connection_count, 1)
        self.assertEqual(server.request_connection_numbers, [1, 1, 1])

    async def test_connection_drop_reconnects_and_replays_request_once(self) -> None:
        attempts = 0

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return 200, {}, {"unique_id": "primed"}
            if attempts == 2:
                raise DropConnection
            return 200, {"Connection": "close"}, {"unique_id": "recovered"}

        server = FakePlatformServer(callback, keep_alive=True)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        primed = await client.send_text("room", "prime keep-alive")
        sent = await client.send_text("room", "retry me")

        self.assertEqual(primed.message_id, "primed")
        self.assertEqual(sent.message_id, "recovered")
        self.assertEqual(attempts, 3)
        self.assertEqual(server.connection_count, 2)
        self.assertEqual(server.request_connection_numbers, [1, 1, 2])

    async def test_concurrent_requests_lease_distinct_connections(self) -> None:
        requests_arrived = threading.Barrier(2)

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            requests_arrived.wait(timeout=2)
            assert isinstance(body, dict)
            return 200, {"Connection": "close"}, {"unique_id": body["text"]}

        server = FakePlatformServer(callback, keep_alive=True)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        first, second = await asyncio.gather(
            client.send_text("room", "first"),
            client.send_text("room", "second"),
        )

        self.assertEqual({first.message_id, second.message_id}, {"first", "second"})
        self.assertEqual(server.connection_count, 2)
        self.assertEqual(len(set(server.request_connection_numbers)), 2)

    async def test_idle_connection_is_replaced_before_next_request(self) -> None:
        clock = [0.0]
        requests = 0

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            nonlocal requests
            requests += 1
            response_headers = {"Connection": "close"} if requests == 2 else {}
            return 200, response_headers, {"unique_id": f"message-{requests}"}

        server = FakePlatformServer(callback, keep_alive=True)
        server.start()
        self.addCleanup(server.stop)
        transport = StdlibHttpTransport(
            max_idle_seconds=10,
            monotonic=lambda: clock[0],
        )
        client = self.make_client(
            server,
            InMemoryTokenStore(TokenState("access", "refresh")),
            transport=transport,
        )

        await client.send_text("room", "before idle")
        clock[0] = 11.0
        await client.send_text("room", "after idle")

        self.assertEqual(server.connection_count, 2)
        self.assertEqual(server.request_connection_numbers, [1, 2])

    async def test_send_audio_posts_bounded_mp3_multipart(self) -> None:
        audio = b"ID3\x04\x00\x00test-audio"

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: RequestBody,
        ) -> Response:
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/rooms/room%2Fone/messages/audio")
            content_type = headers["content-type"]
            self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
            self.assertIsInstance(body, bytes)
            assert isinstance(body, bytes)
            boundary = content_type.split("boundary=", 1)[1].encode("ascii")
            self.assertIn(b"--" + boundary + b"\r\n", body)
            self.assertIn(b'name="audio"; filename="ack.mp3"', body)
            self.assertIn(b"Content-Type: audio/mpeg\r\n\r\n" + audio, body)
            self.assertIn(b'name="immediate"\r\n\r\ntrue', body)
            self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))
            return 200, {}, {"unique_id": "audio-message-1", "media": "audio"}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        sent = await client.send_audio(
            "room/one", audio, "ack.mp3", immediate=True
        )

        self.assertEqual(sent.message_id, "audio-message-1")

    async def test_send_audio_rejects_unsafe_or_oversized_inputs(self) -> None:
        server = FakePlatformServer(
            lambda method, path, headers, body: (200, {}, {})
        )
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        invalid = (
            (b"", "empty.mp3", False),
            (b"audio", "cue.wav", False),
            (b"audio", "../cue.mp3", False),
            (b"audio", "cue name.mp3", False),
            (b"audio", "cue.mp3", 1),
            (b"x" * 1_000_001, "large.m4a", False),
        )
        for audio, filename, immediate in invalid:
            with self.subTest(filename=filename, immediate=immediate):
                with self.assertRaises(ValueError):
                    await client.send_audio(
                        "room", audio, filename, immediate=immediate
                    )

    async def test_send_audio_requires_platform_correlation_id(self) -> None:
        server = FakePlatformServer(
            lambda method, path, headers, body: (200, {}, {"media": "audio"})
        )
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        with self.assertRaisesRegex(BoccoProtocolError, "omitted unique_id"):
            await client.send_audio("room", b"ID3audio", "cue.mp3")

    async def test_motion_catalog_paginates_and_preset_send_returns_id(self) -> None:
        requested_paths: list[str] = []

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            requested_paths.append(path)
            if path == "/v1/motions?offset=0":
                self.assertEqual(method, "GET")
                return 200, {}, {
                    "listing": {"offset": 0, "limit": 2, "total": 3},
                    "motions": [
                        {"name": "GOOD_01", "uuid": "motion-1", "preview": ""},
                        {"name": "GOOD_02", "uuid": "motion-2", "preview": ""},
                    ],
                }
            if path == "/v1/motions?offset=2":
                self.assertEqual(method, "GET")
                return 200, {}, {
                    "listing": {"offset": 2, "limit": 2, "total": 3},
                    "motions": [
                        {"name": "Hello_01", "uuid": "motion-3", "preview": ""},
                    ],
                }
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/rooms/room%2Fone/motions/preset")
            self.assertEqual(body, {"uuid": "motion-3"})
            return 200, {}, {"unique_id": "motion-message-1", "media": "motion"}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        motions = await client.list_motions()
        sent = await client.send_motion("room/one", motions[-1].uuid)

        self.assertEqual(
            [(motion.name, motion.uuid) for motion in motions],
            [
                ("GOOD_01", "motion-1"),
                ("GOOD_02", "motion-2"),
                ("Hello_01", "motion-3"),
            ],
        )
        self.assertEqual(sent.message_id, "motion-message-1")
        self.assertEqual(
            requested_paths,
            [
                "/v1/motions?offset=0",
                "/v1/motions?offset=2",
                "/v1/rooms/room%2Fone/motions/preset",
            ],
        )

    async def test_stamp_catalog_paginates_and_send_omits_optional_text(
        self,
    ) -> None:
        requested: list[tuple[str, str, dict[str, Any] | None]] = []

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            del headers
            requested.append((method, path, body))
            if path == "/v1/stamps?offset=0":
                return 200, {}, {
                    "listing": {"offset": 0, "limit": 1, "total": 2},
                    "stamps": [
                        {
                            "uuid": "stamp-question",
                            "name": "w10question",
                            "summary": "疑問",
                            "image": "https://example.invalid/question.png",
                        }
                    ],
                }
            if path == "/v1/stamps?offset=1":
                return 200, {}, {
                    "listing": {"offset": 1, "limit": 1, "total": 2},
                    "stamps": [
                        {
                            "uuid": "stamp-happy",
                            "name": "w01happy",
                            "summary": "喜び",
                            "image": "https://example.invalid/happy.png",
                        }
                    ],
                }
            self.assertEqual(
                path, "/v1/rooms/room%2Fone/messages/stamp"
            )
            unique_id = f"stamp-message-{sum(item[0] == 'POST' for item in requested)}"
            return 200, {}, {"unique_id": unique_id, "media": "stamp"}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        stamps = await client.list_stamps()
        sound_only = await client.send_stamp("room/one", stamps[0].uuid)
        with_text = await client.send_stamp(
            "room/one", stamps[1].uuid, "やったね"
        )

        self.assertEqual(
            stamps,
            (
                Stamp(
                    "w10question",
                    "stamp-question",
                    "疑問",
                    "https://example.invalid/question.png",
                ),
                Stamp(
                    "w01happy",
                    "stamp-happy",
                    "喜び",
                    "https://example.invalid/happy.png",
                ),
            ),
        )
        self.assertEqual(sound_only.message_id, "stamp-message-1")
        self.assertEqual(with_text.message_id, "stamp-message-2")
        self.assertEqual(
            requested[-2:],
            [
                (
                    "POST",
                    "/v1/rooms/room%2Fone/messages/stamp",
                    {"uuid": "stamp-question"},
                ),
                (
                    "POST",
                    "/v1/rooms/room%2Fone/messages/stamp",
                    {"uuid": "stamp-happy", "text": "やったね"},
                ),
            ],
        )

    async def test_send_stamp_requires_platform_correlation_id(self) -> None:
        server = FakePlatformServer(
            lambda method, path, headers, body: (200, {}, {"media": "stamp"})
        )
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        with self.assertRaisesRegex(BoccoProtocolError, "omitted unique_id"):
            await client.send_stamp("room", "stamp-question")

    async def test_send_head_angle_posts_move_to_with_validated_body(self) -> None:
        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/rooms/room%2Fone/motions/move_to")
            self.assertEqual(body, {"angle": -45, "vertical_angle": 20})
            return 200, {}, {"unique_id": "move-message-1", "media": "motion"}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        sent = await client.send_head_angle("room/one", -45, 20)

        self.assertEqual(sent.message_id, "move-message-1")
        for angle, vertical in ((46, 0), (-46, 0), (0, 21), (0, -21), (True, 0)):
            with self.assertRaises(ValueError):
                await client.send_head_angle("room/one", angle, vertical)

    async def test_send_led_color_posts_rgb_with_validated_body(self) -> None:
        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/rooms/room-1/motions/led_color")
            self.assertEqual(body, {"red": 255, "green": 0, "blue": 128})
            return 200, {}, {"unique_id": "led-message-1"}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        sent = await client.send_led_color("room-1", 255, 0, 128)

        self.assertEqual(sent.message_id, "led-message-1")
        with self.assertRaises(ValueError):
            await client.send_led_color("room-1", 256, 0, 0)
        with self.assertRaises(ValueError):
            await client.send_led_color("room-1", 0, -1, 0)

    async def test_send_custom_motion_posts_all_seven_tracks(self) -> None:
        document = {
            "head": [
                {
                    "duration": 500,
                    "p0": [None, None],
                    "p1": [None, None],
                    "p2": [30, 5],
                    "p3": [30, 5],
                    "ease": [0, 0, 1, 1],
                }
            ],
            "antenna": [],
            "led_cheek_l": [],
            "led_cheek_r": [],
            "led_play": [],
            "led_rec": [],
            "led_func": [],
        }

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/rooms/room-1/motions")
            self.assertEqual(body, document)
            return 200, {}, {"unique_id": "custom-motion-1", "media": "motion"}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        sent = await client.send_custom_motion("room-1", document)

        self.assertEqual(sent.message_id, "custom-motion-1")
        with self.assertRaises(ValueError):
            await client.send_custom_motion("room-1", {"head": []})
        with self.assertRaises(ValueError):
            await client.send_custom_motion(
                "room-1", {**document, "antenna": "not-a-list"}
            )

    async def test_emo_settings_exposes_voice_speed_for_cold_start_timing(self) -> None:
        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/v1/rooms/room%2Fone/emo/settings")
            return 200, {}, {
                "nickname": "emo",
                "voice_speed": 125,
                "timezone": "Asia/Tokyo",
            }

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        settings = await client.get_emo_settings("room/one")

        self.assertEqual(settings.voice_speed, 125)

    async def test_rotated_refresh_token_is_saved_before_original_retry(self) -> None:
        order: list[str] = []
        bodies: list[dict[str, Any]] = []

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            if path == "/oauth/token/refresh":
                order.append("refresh")
                self.assertEqual(body, {"refresh_token": "refresh-old"})
                return 200, {}, {
                    "access_token": "access-new",
                    "refresh_token": "refresh-new",
                    "expires_in": 3600,
                }
            self.assertEqual(path, "/v1/rooms/room%2Fone/messages/text")
            bodies.append(body or {})
            token = headers["authorization"]
            order.append(f"request:{token}")
            if token == "Bearer access-old":
                return 401, {}, {}
            self.assertEqual(order[-2], "save")
            return 200, {}, {}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        store = RecordingStore(
            TokenState("access-old", "refresh-old"), order=order
        )
        client = self.make_client(server, store)

        await client.send_text("room/one", "こんにちは")

        self.assertEqual(
            order,
            [
                "request:Bearer access-old",
                "refresh",
                "save",
                "request:Bearer access-new",
            ],
        )
        self.assertEqual(bodies, [{"text": "こんにちは"}, {"text": "こんにちは"}])
        self.assertEqual(store.saved[0].refresh_token, "refresh-new")

    async def test_concurrent_401_responses_refresh_only_once(self) -> None:
        initial_requests = threading.Barrier(2)
        lock = threading.Lock()
        refresh_count = 0
        request_count = 0

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            nonlocal refresh_count, request_count
            if path == "/oauth/token/refresh":
                with lock:
                    refresh_count += 1
                return 200, {}, {
                    "access_token": "new",
                    "refresh_token": "rotated",
                }
            with lock:
                request_count += 1
            if headers["authorization"] == "Bearer old":
                initial_requests.wait(timeout=2)
                return 401, {}, {}
            return 200, {}, {}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, RecordingStore(TokenState("old", "refresh"))
        )

        await asyncio.gather(
            client.send_text("room", "first"),
            client.send_text("room", "second"),
        )

        self.assertEqual(refresh_count, 1)
        self.assertEqual(request_count, 4)

    async def test_request_retries_only_once_after_401(self) -> None:
        counts = {"request": 0, "refresh": 0}

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            if path == "/oauth/token/refresh":
                counts["refresh"] += 1
                return 200, {}, {
                    "access_token": "new",
                    "refresh_token": "rotated",
                }
            counts["request"] += 1
            return 401, {}, {}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(server, InMemoryTokenStore(TokenState("old", "r")))

        with self.assertRaises(BoccoUnauthorizedError):
            await client.send_text("room", "hello")

        self.assertEqual(counts, {"request": 2, "refresh": 1})

    async def test_expired_access_token_refreshes_before_request(self) -> None:
        authorization_headers: list[str] = []
        refresh_count = 0

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            nonlocal refresh_count
            if path == "/oauth/token/refresh":
                refresh_count += 1
                return 200, {}, {
                    "access_token": "fresh",
                    "refresh_token": "rotated",
                }
            authorization_headers.append(headers["authorization"])
            return 200, {}, {}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        expired = datetime.now(UTC) - timedelta(seconds=1)
        client = self.make_client(
            server,
            InMemoryTokenStore(TokenState("expired", "refresh", expired)),
        )

        await client.send_text("room", "hello")

        self.assertEqual(refresh_count, 1)
        self.assertEqual(authorization_headers, ["Bearer fresh"])

    async def test_429_uses_retry_after_without_real_sleep(self) -> None:
        requests = 0
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            nonlocal requests
            requests += 1
            if requests == 1:
                return 429, {"Retry-After": "3"}, {}
            return 200, {}, {}

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server,
            InMemoryTokenStore(TokenState("access", "refresh")),
            rate_limit_policy=RateLimitPolicy(max_retries=1, max_delay_seconds=10),
            sleep=fake_sleep,
        )

        await client.send_text("room", "hello")

        self.assertEqual(requests, 2)
        self.assertEqual(delays, [3.0])

    async def test_register_webhook_creates_then_subscribes_events(self) -> None:
        requests: list[tuple[str, str, dict[str, Any] | None]] = []

        def webhook_payload(url: str, events: list[str]) -> dict[str, Any]:
            return {
                "url": url,
                "description": "bocco-emo ambient agent",
                "events": events,
                "status": "enabled",
                "secret": "webhook-secret",
            }

        def callback(
            method: str,
            path: str,
            headers: dict[str, str],
            body: dict[str, Any] | None,
        ) -> Response:
            requests.append((method, path, body))
            if method == "GET":
                return 404, {}, {}
            if path == "/v1/webhook/events":
                return 200, {}, webhook_payload(
                    "https://example.trycloudflare.com/webhooks/bocco",
                    body["events"] if body else [],
                )
            return 200, {}, webhook_payload(body["url"] if body else "", [])

        server = FakePlatformServer(callback)
        server.start()
        self.addCleanup(server.stop)
        client = self.make_client(
            server, InMemoryTokenStore(TokenState("access", "refresh"))
        )

        secret = await client.register_webhook(
            "https://example.trycloudflare.com/webhooks/bocco"
        )

        self.assertEqual(secret, "webhook-secret")
        self.assertEqual(
            [(method, path) for method, path, _ in requests],
            [
                ("GET", "/v1/webhook"),
                ("POST", "/v1/webhook"),
                ("PUT", "/v1/webhook/events"),
            ],
        )
        self.assertEqual(
            requests[-1][2],
            {
                "events": [
                    "message.received",
                    "recording.started",
                    "recording.finished",
                    "radar.detected",
                    "emo_talk.finished",
                    "motion.finished",
                    "accel.detected",
                    "illuminance.changed",
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
