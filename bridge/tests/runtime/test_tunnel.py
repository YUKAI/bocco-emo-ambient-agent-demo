from unittest.mock import AsyncMock, patch
import asyncio
import unittest

from bocco_bridge.config import BridgeConfig
from bocco_bridge.tunnel import TunnelSupervisor, extract_quick_tunnel_url
from runtime.fakes import FakeBocco, FakeProcess, wait_until
from timeouts import LIVENESS_TIMEOUT


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **overrides) -> BridgeConfig:
        values = {
            "webhook_secret": "webhook-secret",
            "agent_user_uuid": "agent-1",
            "public_port": 8787,
            "tunnel_url_timeout_seconds": 1,
            "tunnel_restart_seconds": 0.1,
        }
        values.update(overrides)
        return BridgeConfig(**values)

    def test_url_extraction_accepts_only_quick_tunnel_host(self) -> None:
        self.assertEqual(
            extract_quick_tunnel_url(
                b"INF Your quick Tunnel has been created! https://Demo-123.trycloudflare.com"
            ),
            "https://demo-123.trycloudflare.com",
        )
        self.assertIsNone(extract_quick_tunnel_url(b"https://example.com"))
        self.assertIsNone(
            extract_quick_tunnel_url(b"https://bad.trycloudflare.com.attacker.example")
        )

    async def test_restart_clears_dead_url_and_registers_new_url(self) -> None:
        bocco = FakeBocco()
        first = FakeProcess()
        second = FakeProcess()
        await first.stderr.write(b"https://first.trycloudflare.com ready\n")
        await second.stderr.write(b"https://second.trycloudflare.com ready\n")
        processes = [first, second]

        async def factory():
            return processes.pop(0)

        registered_secrets: list[str] = []
        supervisor = TunnelSupervisor(
            self.config(), bocco, factory, registered_secrets.append
        )
        task = asyncio.create_task(supervisor.run())
        await wait_until(lambda: len(bocco.registered) == 1)
        self.assertEqual(supervisor.current_url, "https://first.trycloudflare.com")
        first.exit(1)
        await wait_until(lambda: supervisor.current_url is None)
        await wait_until(lambda: len(bocco.registered) == 2)
        self.assertEqual(
            bocco.registered,
            [
                "https://first.trycloudflare.com/webhooks/bocco",
                "https://second.trycloudflare.com/webhooks/bocco",
            ],
        )
        self.assertEqual(registered_secrets, ["webhook-1", "webhook-1"])
        await supervisor.stop()
        await asyncio.wait_for(task, timeout=LIVENESS_TIMEOUT)

    async def _started(
        self, bocco: FakeBocco, *hostnames: str, **kwargs
    ) -> tuple[TunnelSupervisor, asyncio.Task[None], list[FakeProcess]]:
        processes = []
        for hostname in hostnames:
            process = FakeProcess()
            await process.stderr.write(f"https://{hostname} ready\n".encode())
            processes.append(process)
        queued = list(processes)

        async def factory():
            return queued.pop(0)

        supervisor = TunnelSupervisor(self.config(), bocco, factory, **kwargs)
        task = asyncio.create_task(supervisor.run())
        await wait_until(lambda: supervisor.registrations == 1)
        return supervisor, task, processes

    async def test_reregister_resends_the_same_url_and_republishes_the_secret(
        self,
    ) -> None:
        bocco = FakeBocco()
        secrets: list[str] = []
        registrations: list[int] = []
        supervisor, task, _ = await self._started(
            bocco,
            "first.trycloudflare.com",
            webhook_secret_updated=secrets.append,
            registration_observed=lambda: registrations.append(1),
        )
        self.assertTrue(await supervisor.reregister())
        self.assertEqual(
            bocco.registered,
            [
                "https://first.trycloudflare.com/webhooks/bocco",
                "https://first.trycloudflare.com/webhooks/bocco",
            ],
        )
        # The secret the listener authenticates against must follow every
        # registration, not just the first.
        self.assertEqual(secrets, ["webhook-1", "webhook-1"])
        self.assertEqual(supervisor.registrations, 2)
        self.assertEqual(registrations, [1, 1])
        await supervisor.stop()
        await asyncio.wait_for(task, timeout=1)

    async def test_recycle_replaces_the_hostname_the_platform_was_given(self) -> None:
        bocco = FakeBocco()
        supervisor, task, _ = await self._started(
            bocco, "first.trycloudflare.com", "second.trycloudflare.com"
        )
        # The point of the recycle: a quick tunnel cannot be re-pointed, so the
        # only way to stop asking BOCCO to deliver to a hostname it is ignoring
        # is to obtain a different one.
        self.assertTrue(await supervisor.recycle(2.0))
        self.assertEqual(supervisor.current_url, "https://second.trycloudflare.com")
        self.assertEqual(supervisor.registrations, 2)
        self.assertEqual(
            bocco.registered[-1], "https://second.trycloudflare.com/webhooks/bocco"
        )
        await supervisor.stop()
        await asyncio.wait_for(task, timeout=1)

    async def test_repairs_are_refused_when_there_is_nothing_to_repair(self) -> None:
        supervisor = TunnelSupervisor(self.config(), FakeBocco())
        self.assertFalse(await supervisor.reregister())
        self.assertFalse(await supervisor.recycle(0.1))

    async def test_a_failed_reregistration_reports_rather_than_raises(self) -> None:
        class RefusingBocco(FakeBocco):
            async def register_webhook(self, public_url: str) -> str:
                if self.registered:
                    raise RuntimeError("platform refused the re-registration")
                return await super().register_webhook(public_url)

        bocco = RefusingBocco()
        supervisor, task, _ = await self._started(bocco, "first.trycloudflare.com")
        with self.assertLogs("bocco_bridge.tunnel", level="WARNING") as logs:
            self.assertFalse(await supervisor.reregister())
        self.assertIn("tunnel_reregister_failed", "\n".join(logs.output))
        # Degraded to today's behaviour, not to a crashed supervisor.
        self.assertTrue(supervisor.ready)
        await supervisor.stop()
        await asyncio.wait_for(task, timeout=1)

    async def test_cloudflared_receives_only_public_loopback_port(self) -> None:
        fake_process = FakeProcess()
        create = AsyncMock(return_value=fake_process)
        supervisor = TunnelSupervisor(self.config(), FakeBocco())
        with patch("asyncio.create_subprocess_exec", create):
            await supervisor._spawn_cloudflared()
        arguments = create.await_args.args
        self.assertIn("http://127.0.0.1:8787", arguments)
        self.assertNotIn("8642", " ".join(str(value) for value in arguments))


if __name__ == "__main__":
    unittest.main()
