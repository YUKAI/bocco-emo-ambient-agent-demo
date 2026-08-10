import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from bocco_bridge.app import WebhookSecretStore, create_public_app
from bocco_bridge.config import BridgeConfig
from bocco_bridge.db import EventDatabase
from runtime.fakes import FakeInboundEvent, FakeParser, wait_until
from timeouts import LIVENESS_TIMEOUT


class PublicHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = BridgeConfig(
            webhook_secret="correct-secret",
            agent_user_uuid="agent-1",
            database_path=Path(self.temporary.name) / "state.db",
            tunnel_enabled=False,
            max_webhook_body_bytes=256,
        )
        self.database = EventDatabase(self.config.database_path)
        await self.database.initialize()
        self.parser = FakeParser()
        self.notifications = 0
        self.deliveries = 0
        self.webhook_secrets = WebhookSecretStore(self.config.webhook_secret)

        def notify() -> None:
            self.notifications += 1

        def observe_delivery() -> None:
            self.deliveries += 1

        app = create_public_app(
            self.config,
            self.database,
            self.parser,
            notify,
            self.webhook_secrets,
            readiness=lambda: False,
            delivery_observed=observe_delivery,
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temporary.cleanup()

    async def test_invalid_secret_is_rejected_before_body_parsing(self) -> None:
        private_text = "body-that-must-not-be-logged"
        with self.assertLogs("bocco_bridge.app", level="WARNING") as logs:
            response = await self.client.post(
                "/webhooks/bocco",
                json={"request_id": "bad", "event": private_text},
                headers={"X-Platform-API-Secret": "wrong"},
            )
        self.assertEqual(response.status, 401)
        self.assertEqual(self.parser.calls, 0)
        self.assertNotIn(private_text, "\n".join(logs.output))

    async def test_missing_or_oversized_secret_header_is_rejected(self) -> None:
        for headers in (
            {},
            {"X-Platform-API-Secret": "x" * 513},
        ):
            with self.subTest(headers=sorted(headers)):
                response = await self.client.post(
                    "/webhooks/bocco",
                    json={"request_id": "bad", "event": "ignored"},
                    headers=headers,
                )
                self.assertEqual(response.status, 401)
        self.assertEqual(self.parser.calls, 0)

    async def test_valid_webhook_is_durable_and_duplicate_is_noop(self) -> None:
        payload = {
            "request_id": "request-1",
            "event": "message.received",
            "data": {
                "room_uuid": "room-1",
                "sender_uuid": "person-1",
                "speech_text": "こんにちは",
            },
        }
        headers = {"X-Platform-API-Secret": "correct-secret"}
        first = await self.client.post("/webhooks/bocco", json=payload, headers=headers)
        second = await self.client.post("/webhooks/bocco", json=payload, headers=headers)
        self.assertEqual(first.status, 202)
        self.assertEqual(await first.json(), {"accepted": True, "duplicate": False})
        self.assertEqual(await second.json(), {"accepted": False, "duplicate": True})
        self.assertEqual(self.notifications, 1)
        event = await self.database.get_event("request-1")
        assert event is not None
        self.assertEqual(event.speech_text, "こんにちは")

    async def test_stamp_webhook_waits_for_durable_echo_id(self) -> None:
        self.assertTrue(
            await self.database.enqueue(
                FakeInboundEvent(
                    request_id="stamp-source",
                    event_type="recording.finished",
                    speech_text=None,
                    message_id=None,
                    message_media=None,
                )
            )
        )
        payload = {
            "request_id": "stamp-echo",
            "event": "message.received",
            "data": {
                "room_uuid": "room-1",
                "sender_uuid": "person-1",
                "speech_text": "疑問",
                "message_id": "stamp-message-id",
                "message_media": "stamp",
            },
        }
        headers = {"X-Platform-API-Secret": "correct-secret"}

        async with self.database.outbound_stamp_delivery("room-1"):
            response_task = asyncio.create_task(
                self.client.post(
                    "/webhooks/bocco", json=payload, headers=headers
                )
            )
            await wait_until(lambda: self.parser.calls == 1)
            self.assertFalse(response_task.done())
            await self.database.record_stamp_delivery(
                "stamp-source",
                "room-1",
                "stamp-message-id",
                600,
                sent_at=1_000,
            )

        response = await asyncio.wait_for(response_task, timeout=LIVENESS_TIMEOUT)
        self.assertEqual(response.status, 202)
        self.assertEqual(
            await self.database.consume_outbound_echo(
                "room-1",
                "stamp-message-id",
                "疑問",
                600,
                now=1_001,
            ),
            "message_id",
        )

    async def test_delivery_is_observed_before_a_payload_is_understood(self) -> None:
        headers = {"X-Platform-API-Secret": "correct-secret"}
        # A body this bridge cannot parse still proves that BOCCO reached it,
        # which is the only question the delivery monitor is asking.
        rejected = await self.client.post(
            "/webhooks/bocco", data=b"not-json", headers=headers
        )
        self.assertEqual(rejected.status, 400)
        self.assertEqual(self.deliveries, 1)
        # A request that fails authentication is not proof of anything: it did
        # not come from a platform holding the secret we registered.
        unauthorized = await self.client.post(
            "/webhooks/bocco", data=b"not-json", headers={"X-Platform-API-Secret": "no"}
        )
        self.assertEqual(unauthorized.status, 401)
        self.assertEqual(self.deliveries, 1)

    async def test_malformed_and_oversized_payloads_are_rejected(self) -> None:
        headers = {"X-Platform-API-Secret": "correct-secret"}
        malformed = await self.client.post(
            "/webhooks/bocco", data=b"not-json", headers=headers
        )
        oversized = await self.client.post(
            "/webhooks/bocco", data=b"x" * 257, headers=headers
        )
        self.assertEqual(malformed.status, 400)
        self.assertEqual(oversized.status, 413)

    async def test_public_health_has_no_runtime_details(self) -> None:
        response = await self.client.get("/healthz")
        self.assertEqual(await response.json(), {"status": "ok"})

    async def test_public_readiness_discloses_only_status(self) -> None:
        response = await self.client.get("/readyz")
        self.assertEqual(response.status, 503)
        self.assertEqual(await response.json(), {"status": "not_ready"})

    async def test_registered_secret_replaces_startup_secret(self) -> None:
        self.webhook_secrets.replace("rotated-secret")
        payload = {"request_id": "rotated", "event": "radar.detected"}
        old = await self.client.post(
            "/webhooks/bocco",
            json=payload,
            headers={"X-Platform-API-Secret": "correct-secret"},
        )
        current = await self.client.post(
            "/webhooks/bocco",
            json=payload,
            headers={"X-Platform-API-Secret": "rotated-secret"},
        )
        self.assertEqual(old.status, 401)
        self.assertEqual(current.status, 202)
if __name__ == "__main__":
    unittest.main()
