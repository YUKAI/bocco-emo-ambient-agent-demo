from datetime import UTC, datetime
import os
from pathlib import Path
import stat
import tempfile
import unittest

from bocco_bridge.app import RuntimeDependencies
from bocco_bridge.bocco import AtomicFileTokenStore, BoccoClient, TokenState
from bocco_bridge.config import BridgeConfig
from bocco_bridge.hermes import HermesClient
from bocco_bridge.integration import BoccoWebhookParser, create_dependencies


class DependencyFactoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.token_path = root / "oauth.json"
        self.config = BridgeConfig(
            webhook_secret="initial-webhook-secret",
            agent_user_uuid="agent-1",
            database_path=root / "state.db",
            tunnel_enabled=False,
            extra={
                "BOCCO_ACCESS_TOKEN": "initial-access",
                "BOCCO_REFRESH_TOKEN": "initial-refresh",
                "BOCCO_TOKEN_FILE": str(self.token_path),
                "BOCCO_PLATFORM_BASE_URL": "https://platform-api.example",
                "API_SERVER_KEY": "hermes-api-key",
                "HERMES_MAX_OUTPUT_TOKENS": "96",
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_factory_builds_all_contracts_and_private_token_state(self) -> None:
        dependencies = create_dependencies(self.config)
        self.assertIsInstance(dependencies, RuntimeDependencies)
        self.assertIsInstance(dependencies.bocco, BoccoClient)
        self.assertIsInstance(dependencies.hermes, HermesClient)
        self.assertEqual(dependencies.hermes._config.max_output_tokens, 96)
        self.assertIsInstance(dependencies.webhook_parser, BoccoWebhookParser)
        self.assertEqual(stat.S_IMODE(os.stat(self.token_path).st_mode), 0o600)
        self.assertEqual(
            AtomicFileTokenStore(self.token_path).load(),
            TokenState("initial-access", "initial-refresh"),
        )
        await dependencies.aclose()

    async def test_existing_rotated_tokens_override_initial_environment(self) -> None:
        first = create_dependencies(self.config)
        await first.aclose()
        rotated = TokenState("rotated-access", "rotated-refresh")
        AtomicFileTokenStore(self.token_path).save(rotated)

        restarted = create_dependencies(self.config)
        self.assertEqual(restarted.bocco.token_state, rotated)
        await restarted.aclose()

    def test_real_parser_adapter_uses_worker_one_normalization(self) -> None:
        event = BoccoWebhookParser().parse(
            {
                "request_id": "request-1",
                "uuid": "room-1",
                "timestamp": 1_722_470_400,
                "event": "message.received",
                "data": {
                    "message": {
                        "unique_id": "inbound-message-1",
                        "user": {"uuid": "person-1"},
                        "message": {"ja": " こんにちは "},
                    }
                },
            },
            datetime.now(UTC),
        )
        self.assertEqual(event.room_uuid, "room-1")
        self.assertEqual(event.speech_text, "こんにちは")
        self.assertEqual(event.received_at, datetime.fromtimestamp(1_722_470_400, UTC))


if __name__ == "__main__":
    unittest.main()
