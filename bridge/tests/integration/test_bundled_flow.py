from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bocco_bridge.app import create_public_app
from bocco_bridge.bocco import (
    BoccoClient,
    BoccoClientConfig,
    InMemoryTokenStore,
    TokenState,
)
from bocco_bridge.config import BridgeConfig
from bocco_bridge.db import EventDatabase
from bocco_bridge.events import EventProcessor, EventWorker
from bocco_bridge.hermes import HermesClient, HermesConfig
from bocco_bridge.integration import BoccoWebhookParser
from runtime.fakes import wait_until
from timeouts import LIVENESS_TIMEOUT


class BundledFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.hermes_gate = asyncio.Event()
        self.hermes_requests: list[dict] = []
        self.bocco_messages: list[tuple[str, str]] = []

        async def respond(request: web.Request) -> web.Response:
            self.hermes_requests.append(await request.json())
            await self.hermes_gate.wait()
            return web.json_response(
                {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "本物のクライアント経由です。"}
                            ],
                        }
                    ]
                }
            )

        hermes_app = web.Application()
        hermes_app.router.add_post("/v1/responses", respond)
        self.hermes_server = TestServer(hermes_app)
        await self.hermes_server.start_server()

        async def send_text(request: web.Request) -> web.Response:
            payload = await request.json()
            self.bocco_messages.append((request.match_info["room_uuid"], payload["text"]))
            return web.json_response({"unique_id": "outbound-message-1"})

        bocco_app = web.Application()
        bocco_app.router.add_post(
            "/v1/rooms/{room_uuid}/messages/text", send_text
        )
        self.bocco_server = TestServer(bocco_app)
        await self.bocco_server.start_server()

        self.bocco = BoccoClient(
            BoccoClientConfig(
                base_url=str(self.bocco_server.make_url("/")).rstrip("/"),
                timeout_seconds=2,
            ),
            InMemoryTokenStore(TokenState("access", "refresh")),
        )
        self.hermes = HermesClient(
            HermesConfig(
                api_key="api-key",
                api_base_url=str(self.hermes_server.make_url("/")).rstrip("/"),
                response_timeout_seconds=2,
            )
        )
        self.config = BridgeConfig(
            webhook_secret="platform-webhook-secret",
            agent_user_uuid="agent-1",
            database_path=Path(self.temporary.name) / "state.db",
            tunnel_enabled=False,
            worker_poll_seconds=0.01,
            worker_retry_base_seconds=0,
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        processor = EventProcessor(
            self.config, self.database, self.bocco, self.hermes
        )
        self.worker = EventWorker(self.config, self.database, processor)
        self.worker_task = asyncio.create_task(self.worker.run())
        webhook_app = create_public_app(
            self.config,
            self.database,
            BoccoWebhookParser(),
            self.worker.notify,
        )
        self.webhook_client = TestClient(TestServer(webhook_app))
        await self.webhook_client.start_server()

    async def asyncTearDown(self) -> None:
        self.hermes_gate.set()
        await self.worker.stop()
        await asyncio.wait_for(self.worker_task, timeout=LIVENESS_TIMEOUT)
        await self.webhook_client.close()
        await self.hermes.aclose()
        await self.hermes_server.close()
        await self.bocco_server.close()
        self.temporary.cleanup()

    async def test_real_clients_deliver_then_suppress_exact_webhook_echo(self) -> None:
        payload = {
            "request_id": "bundled-1",
            "uuid": "room-1",
            "timestamp": 1_722_470_400,
            "event": "message.received",
            "data": {
                "message": {
                    "unique_id": "inbound-message-1",
                    "user": {"uuid": "person-1"},
                    "message": {"ja": "統合できていますか？"},
                }
            },
        }
        headers = {"X-Platform-API-Secret": "platform-webhook-secret"}
        response = await asyncio.wait_for(
            self.webhook_client.post("/webhooks/bocco", json=payload, headers=headers),
            timeout=0.25,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(self.bocco_messages, [])
        await wait_until(lambda: len(self.hermes_requests) == 1)

        self.hermes_gate.set()
        await wait_until(lambda: len(self.bocco_messages) == 1)
        self.assertEqual(
            self.hermes_requests[0]["conversation"], "bocco-room:room-1"
        )
        self.assertEqual(
            self.bocco_messages,
            [("room-1", "本物のクライアント経由です。")],
        )

        duplicate = await self.webhook_client.post(
            "/webhooks/bocco", json=payload, headers=headers
        )
        self.assertEqual(await duplicate.json(), {"accepted": False, "duplicate": True})
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.hermes_requests), 1)
        self.assertEqual(len(self.bocco_messages), 1)

        echo_payload = {
            "request_id": "bundled-echo-1",
            "uuid": "room-1",
            "timestamp": 1_722_470_401,
            "event": "message.received",
            "data": {
                "message": {
                    "unique_id": "outbound-message-1",
                    "user": {"uuid": "person-1"},
                    "message": {"ja": "本物のクライアント経由です。"},
                }
            },
        }
        echo_response = await self.webhook_client.post(
            "/webhooks/bocco", json=echo_payload, headers=headers
        )
        self.assertEqual(echo_response.status, 202)
        deadline = asyncio.get_running_loop().time() + 1
        while True:
            echo = await self.database.get_event("bundled-echo-1")
            if echo is not None and echo.status == "completed":
                break
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("echo event did not complete")
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.hermes_requests), 1)
        self.assertEqual(len(self.bocco_messages), 1)

    async def test_motion_webhook_with_null_text_completes_silently(self) -> None:
        fixture_path = (
            Path(__file__).resolve().parents[1]
            / "fixtures/app_webhook_events.json"
        )
        payloads = json.loads(fixture_path.read_text(encoding="utf-8"))
        motion = next(payload for payload in payloads if payload["kind"] == "motion")
        headers = {"X-Platform-API-Secret": "platform-webhook-secret"}

        response = await self.webhook_client.post(
            "/webhooks/bocco", json=motion, headers=headers
        )

        self.assertEqual(response.status, 202)
        deadline = asyncio.get_running_loop().time() + 1
        while True:
            event = await self.database.get_event(motion["request_id"])
            if event is not None and event.status == "completed":
                break
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("motion event did not complete")
            await asyncio.sleep(0.01)
        self.assertEqual(event.message_media, "motion")
        self.assertIsNone(event.speech_text)
        self.assertEqual(self.hermes_requests, [])
        self.assertEqual(self.bocco_messages, [])


if __name__ == "__main__":
    unittest.main()
