from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from bocco_bridge.app import create_public_app
from bocco_bridge.bocco import SentMessage
from bocco_bridge.config import BridgeConfig
from bocco_bridge.db import EventDatabase
from bocco_bridge.events import EventProcessor, EventWorker
from runtime.fakes import FakeParser, wait_until
from timeouts import LIVENESS_TIMEOUT


class HttpBoccoClient:
    def __init__(self, session: ClientSession, base_url: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")

    async def send_text(self, room_uuid: str, text: str) -> SentMessage:
        async with self.session.post(
            f"{self.base_url}/rooms/{room_uuid}/messages/text", json={"text": text}
        ) as response:
            response.raise_for_status()
            payload = await response.json()
            return SentMessage(message_id=payload.get("unique_id"))

    async def register_webhook(self, public_url: str) -> str:
        async with self.session.put(
            f"{self.base_url}/webhook", json={"url": public_url}
        ) as response:
            response.raise_for_status()
            payload = await response.json()
            return payload["id"]


class HttpHermesClient:
    def __init__(self, session: ClientSession, base_url: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")

    async def respond(self, conversation: str, text: str, instructions: str) -> str:
        async with self.session.post(
            f"{self.base_url}/v1/responses",
            json={
                "conversation": conversation,
                "text": text,
                "instructions": instructions,
            },
        ) as response:
            response.raise_for_status()
            payload = await response.json()
            return payload["output_text"]

class FakeServerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.hermes_gate = asyncio.Event()
        self.hermes_requests: list[dict] = []
        self.bocco_messages: list[tuple[str, str]] = []

        async def respond(request: web.Request) -> web.Response:
            payload = await request.json()
            self.hermes_requests.append(payload)
            await self.hermes_gate.wait()
            return web.json_response({"output_text": "やあ、今日は元気？"})

        hermes_app = web.Application()
        hermes_app.router.add_post("/v1/responses", respond)
        self.hermes_server = TestServer(hermes_app)
        await self.hermes_server.start_server()

        async def send_text(request: web.Request) -> web.Response:
            payload = await request.json()
            self.bocco_messages.append((request.match_info["room_uuid"], payload["text"]))
            return web.json_response({"unique_id": "outbound-message-1"})

        async def register(request: web.Request) -> web.Response:
            await request.json()
            return web.json_response({"id": "webhook-1"})

        bocco_app = web.Application()
        bocco_app.router.add_post("/rooms/{room_uuid}/messages/text", send_text)
        bocco_app.router.add_put("/webhook", register)
        self.bocco_server = TestServer(bocco_app)
        await self.bocco_server.start_server()

        self.session = ClientSession()
        self.bocco = HttpBoccoClient(self.session, str(self.bocco_server.make_url("/")))
        self.hermes = HttpHermesClient(self.session, str(self.hermes_server.make_url("/")))
        self.config = BridgeConfig(
            webhook_secret="webhook-secret",
            agent_user_uuid="agent-1",
            database_path=Path(self.temporary.name) / "state.db",
            tunnel_enabled=False,
            worker_poll_seconds=0.01,
            worker_retry_base_seconds=0,
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        processor = EventProcessor(self.config, self.database, self.bocco, self.hermes)
        self.worker = EventWorker(self.config, self.database, processor)
        self.worker_task = asyncio.create_task(self.worker.run())
        app = create_public_app(
            self.config, self.database, FakeParser(), self.worker.notify
        )
        self.webhook_client = TestClient(TestServer(app))
        await self.webhook_client.start_server()

    async def asyncTearDown(self) -> None:
        self.hermes_gate.set()
        await self.worker.stop()
        await asyncio.wait_for(self.worker_task, timeout=LIVENESS_TIMEOUT)
        await self.webhook_client.close()
        await self.session.close()
        await self.hermes_server.close()
        await self.bocco_server.close()
        self.temporary.cleanup()

    async def test_speech_webhook_returns_before_llm_then_delivers_once(self) -> None:
        payload = {
            "request_id": "end-to-end-1",
            "event": "message.received",
            "data": {
                "room_uuid": "room-1",
                "sender_uuid": "person-1",
                "speech_text": "今日は元気？",
            },
        }
        headers = {"X-Platform-API-Secret": "webhook-secret"}
        response = await asyncio.wait_for(
            self.webhook_client.post("/webhooks/bocco", json=payload, headers=headers),
            timeout=0.25,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(self.bocco_messages, [])
        await wait_until(lambda: len(self.hermes_requests) == 1)

        self.hermes_gate.set()
        await wait_until(lambda: len(self.bocco_messages) == 1)
        event = await self.database.get_event("end-to-end-1")
        deadline = asyncio.get_running_loop().time() + 1
        while event is not None and event.status != "completed":
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("event did not complete")
            await asyncio.sleep(0.01)
            event = await self.database.get_event("end-to-end-1")

        self.assertEqual(
            self.hermes_requests[0]["conversation"], "bocco-room:room-1"
        )
        self.assertEqual(self.bocco_messages, [("room-1", "やあ、今日は元気？")])

        duplicate = await self.webhook_client.post(
            "/webhooks/bocco", json=payload, headers=headers
        )
        self.assertEqual(await duplicate.json(), {"accepted": False, "duplicate": True})
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.hermes_requests), 1)
        self.assertEqual(len(self.bocco_messages), 1)


if __name__ == "__main__":
    unittest.main()
