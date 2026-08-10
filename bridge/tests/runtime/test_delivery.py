"""Webhook delivery detection and self-heal.

The failure under test is silent by construction: nothing errors, nothing is
logged, and the only observable is that no webhook ever arrives. Every test
here therefore drives the monitor through a fake platform that simply does or
does not call ``observe()``.
"""

import asyncio
from datetime import datetime
import unittest

from bocco_bridge.config import BridgeConfig
from bocco_bridge.custom_motions import (
    MOTION_TRACKS,
    silent_probe_document,
    validate_motion_document,
)
from bocco_bridge.delivery import WebhookDeliveryMonitor
from runtime.fakes import FakeBocco, wait_until


class FakeTunnel:
    """The two repair actions, counted."""

    def __init__(self) -> None:
        self.reregisters = 0
        self.recycles = 0
        self.reregister_result = True
        self.recycle_result = True

    async def reregister(self) -> bool:
        self.reregisters += 1
        return self.reregister_result

    async def recycle(self, timeout: float) -> bool:
        self.recycles += 1
        return self.recycle_result


class DeliveryMonitorTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **overrides) -> BridgeConfig:
        values = {
            "webhook_secret": "webhook-secret",
            "motion_room_uuid": "room-1",
            "webhook_probe_startup_delay_seconds": 0.0,
            # Far away by default, so only an explicit registration triggers a
            # probe and no test is racing a background timer it did not ask for.
            "webhook_probe_silence_seconds": 3_600.0,
            "webhook_probe_timeout_seconds": 0.15,
            "webhook_probe_backoff_seconds": 0.0,
            "webhook_probe_recycle_timeout_seconds": 0.5,
            "webhook_probe_max_attempts": 3,
        }
        values.update(overrides)
        return BridgeConfig(**values)

    def monitor(
        self,
        config: BridgeConfig,
        bocco: FakeBocco,
        tunnel: FakeTunnel | None = None,
        **overrides,
    ) -> WebhookDeliveryMonitor:
        created = WebhookDeliveryMonitor(config, bocco, **overrides)
        if tunnel is not None:
            created.attach_tunnel(tunnel)
        return created

    async def drive(self, monitor: WebhookDeliveryMonitor) -> asyncio.Task[None]:
        task = asyncio.create_task(monitor.run())
        await wait_until(lambda: monitor.ready)
        return task

    async def shutdown(
        self, monitor: WebhookDeliveryMonitor, task: asyncio.Task[None]
    ) -> None:
        await monitor.stop()
        await asyncio.wait_for(task, timeout=2)

    async def test_the_probe_document_commands_nothing(self) -> None:
        document = silent_probe_document()
        validate_motion_document(document)
        self.assertEqual(set(document), set(MOTION_TRACKS))
        # One head element, holding: every control point null means "keep the
        # posture you already have", so nothing moves.
        self.assertEqual(len(document["head"]), 1)
        head = document["head"][0]
        for point in ("p0", "p1", "p2", "p3"):
            self.assertEqual(head[point], [None, None])
        # Everything else empty: no antenna flutter, no LED anywhere.
        for track in MOTION_TRACKS:
            if track != "head":
                self.assertEqual(document[track], [])
        # And no sound key at all, inert or otherwise.
        self.assertNotIn("sound", document)
        # Callers get a fresh copy they cannot use to poison the next probe.
        document["head"].clear()
        self.assertEqual(len(silent_probe_document()["head"]), 1)

    async def test_a_confirmed_probe_needs_no_repair(self) -> None:
        bocco = FakeBocco()
        tunnel = FakeTunnel()
        monitor = self.monitor(self.config(), bocco, tunnel)
        task = await self.drive(monitor)
        with self.assertLogs("bocco_bridge.delivery", level="INFO") as logs:
            monitor.notify_registered()
            await wait_until(lambda: len(bocco.custom_motion_sent) == 1)
            # The platform delivers: any inbound webhook is the proof.
            monitor.observe()
            await wait_until(lambda: monitor.heal_cycles == 1 and monitor.probes == 1)
            # `probes` is incremented before the probe logs its result, so the
            # counter reaching 1 does not mean the confirmation has been written.
            # Wait for the line rather than sleeping a guessed interval at it.
            await wait_until(
                lambda: any("webhook_probe_confirmed" in line
                            for line in logs.output),
                timeout=3,
            )
        await self.shutdown(monitor, task)
        self.assertEqual(monitor.probes, 1)
        self.assertEqual(tunnel.reregisters, 0)
        self.assertEqual(tunnel.recycles, 0)
        output = "\n".join(logs.output)
        self.assertIn("webhook_probe_confirmed reason=registration attempt=1", output)
        self.assertNotIn("webhook_delivery_stalled", output)

    async def test_a_stalled_platform_is_repaired_by_reregistration(self) -> None:
        bocco = FakeBocco()
        tunnel = FakeTunnel()
        monitor = self.monitor(self.config(), bocco, tunnel)
        task = await self.drive(monitor)

        async def answer_from_the_second_probe() -> None:
            # The first probe is met with the silence that is the whole bug;
            # the re-registration makes the platform start delivering again.
            await wait_until(lambda: tunnel.reregisters == 1, timeout=2)
            await wait_until(lambda: len(bocco.custom_motion_sent) == 2, timeout=2)
            monitor.observe()

        with self.assertLogs("bocco_bridge.delivery", level="INFO") as logs:
            helper = asyncio.create_task(answer_from_the_second_probe())
            monitor.notify_registered()
            await wait_until(lambda: monitor.heal_cycles == 1 and monitor.probes == 2,
                             timeout=3)
            await helper
            # `probes == 2` is true the moment the second probe *starts*, and
            # heal_cycles counts cycles begun, not finished — so neither says the
            # repair has concluded. Leaving the capture block here races the
            # monitor to its own log line, which is what CI caught: the recovery
            # was asserted against output ending at "probe_started attempt=2".
            # Wait for the thing being asserted.
            await wait_until(
                lambda: any("webhook_delivery_recovered" in line
                            for line in logs.output),
                timeout=3,
            )
        await self.shutdown(monitor, task)
        self.assertEqual(monitor.probes, 2)
        self.assertEqual(tunnel.reregisters, 1)
        # The expensive remedy is never reached when the cheap one works.
        self.assertEqual(tunnel.recycles, 0)
        output = "\n".join(logs.output)
        self.assertIn("webhook_probe_timeout reason=registration attempt=1", output)
        self.assertIn(
            "webhook_delivery_stalled reason=registration failed_attempts=1 "
            "action=reregister",
            output,
        )
        self.assertIn("webhook_delivery_recovered", output)

    async def test_repair_escalates_to_a_recycle_then_stops_trying(self) -> None:
        bocco = FakeBocco()
        tunnel = FakeTunnel()
        monitor = self.monitor(self.config(), bocco, tunnel)
        task = await self.drive(monitor)
        with self.assertLogs("bocco_bridge.delivery", level="INFO") as logs:
            monitor.notify_registered()
            await wait_until(lambda: monitor.gave_up, timeout=3)
            # Several more ticks must produce nothing at all: a self-heal that
            # keeps posting motions into a living room forever is its own bug.
            await asyncio.sleep(0.3)
        await self.shutdown(monitor, task)
        self.assertEqual(monitor.probes, 3)
        self.assertEqual(monitor.heal_cycles, 1)
        # Each rung of the ladder is climbed exactly once. The recycle in
        # particular must never repeat: every recycle throws away a working
        # hostname and makes the Platform accept a new registration, so a
        # ladder that repeated its last rung would churn tunnels indefinitely.
        self.assertEqual(tunnel.reregisters, 1)
        self.assertEqual(tunnel.recycles, 1)
        output = "\n".join(logs.output)
        self.assertEqual(output.count("action=recycle_tunnel"), 1)
        self.assertIn("action=recycle_tunnel", output)
        self.assertIn(
            "webhook_delivery_unrecoverable reason=registration attempts=3", output
        )
        self.assertIn("hint=restart_bocco_bridge_service", output)

    async def test_giving_up_is_re_armed_when_delivery_returns(self) -> None:
        bocco = FakeBocco()
        tunnel = FakeTunnel()
        monitor = self.monitor(self.config(), bocco, tunnel)
        task = await self.drive(monitor)
        monitor.notify_registered()
        await wait_until(lambda: monitor.gave_up, timeout=3)
        with self.assertLogs("bocco_bridge.delivery", level="WARNING") as logs:
            monitor.observe()
        self.assertFalse(monitor.gave_up)
        await self.shutdown(monitor, task)
        self.assertIn(
            "webhook_delivery_restored source=inbound_traffic", "\n".join(logs.output)
        )

    async def test_traffic_during_the_startup_delay_replaces_the_probe(self) -> None:
        bocco = FakeBocco()
        monitor = self.monitor(
            self.config(webhook_probe_startup_delay_seconds=0.2), bocco
        )
        task = await self.drive(monitor)
        with self.assertLogs("bocco_bridge.delivery", level="INFO") as logs:
            monitor.notify_registered()
            await asyncio.sleep(0.05)
            monitor.observe()
            await asyncio.sleep(0.4)
        await self.shutdown(monitor, task)
        self.assertEqual(monitor.probes, 0)
        self.assertEqual(bocco.custom_motion_attempts, [])
        self.assertIn(
            "webhook_probe_skipped reason=registration cause=inbound_traffic",
            "\n".join(logs.output),
        )

    async def test_a_house_that_keeps_talking_is_never_probed(self) -> None:
        bocco = FakeBocco()
        monitor = self.monitor(self.config(webhook_probe_silence_seconds=0.2), bocco)
        task = await self.drive(monitor)
        for _ in range(12):
            monitor.observe()
            await asyncio.sleep(0.05)
        await self.shutdown(monitor, task)
        self.assertEqual(monitor.probes, 0)

    async def test_silence_probes_only_inside_waking_hours(self) -> None:
        bocco = FakeBocco()
        hour = 3
        monitor = self.monitor(
            self.config(webhook_probe_silence_seconds=0.1),
            bocco,
            local_now=lambda: datetime(2026, 8, 6, hour, 0),
        )
        task = await self.drive(monitor)
        await asyncio.sleep(0.3)
        # 03:00 in a sleeping house: a stall waits for morning rather than
        # posting into the family chat in the middle of the night.
        self.assertEqual(monitor.probes, 0)
        hour = 10
        await wait_until(lambda: monitor.probes == 1, timeout=2)
        await self.shutdown(monitor, task)
        self.assertEqual(bocco.custom_motion_attempts[0][0], "room-1")

    async def test_an_unsendable_probe_does_not_spend_the_repair_budget(self) -> None:
        bocco = FakeBocco()
        bocco.custom_motion_failures = 5
        tunnel = FakeTunnel()
        monitor = self.monitor(self.config(), bocco, tunnel)
        task = await self.drive(monitor)
        with self.assertLogs("bocco_bridge.delivery", level="WARNING") as logs:
            monitor.notify_registered()
            await wait_until(lambda: monitor.heal_cycles == 1, timeout=2)
            await asyncio.sleep(0.15)
        await self.shutdown(monitor, task)
        # A Platform API we cannot reach is not evidence that BOCCO stopped
        # delivering, so nothing is re-registered and no tunnel is thrown away.
        self.assertEqual(monitor.probes, 0)
        self.assertEqual(tunnel.reregisters, 0)
        self.assertEqual(tunnel.recycles, 0)
        output = "\n".join(logs.output)
        self.assertIn("webhook_probe_send_failed", output)
        self.assertNotIn("webhook_delivery_unrecoverable", output)

    async def test_detection_still_reports_without_a_tunnel_to_repair(self) -> None:
        bocco = FakeBocco()
        monitor = self.monitor(self.config(), bocco)
        task = await self.drive(monitor)
        with self.assertLogs("bocco_bridge.delivery", level="WARNING") as logs:
            monitor.notify_registered()
            await wait_until(lambda: monitor.gave_up, timeout=3)
        await self.shutdown(monitor, task)
        self.assertEqual(monitor.probes, 3)
        output = "\n".join(logs.output)
        self.assertIn("webhook_delivery_stalled", output)
        self.assertIn("webhook_repair_unavailable action=reregister", output)
        self.assertIn("webhook_delivery_unrecoverable", output)

    async def test_the_probe_is_off_when_it_would_be_unwelcome(self) -> None:
        for overrides, expected in (
            ({"motion_room_uuid": ""}, "no_room"),
            ({"motions_enabled": False}, "motions_disabled"),
            ({"webhook_probe_enabled": False}, "configuration"),
        ):
            with self.subTest(expected=expected):
                bocco = FakeBocco()
                monitor = self.monitor(self.config(**overrides), bocco)
                self.assertFalse(monitor.enabled)
                self.assertEqual(monitor.disabled_reason, expected)
                with self.assertLogs("bocco_bridge.delivery", level="INFO") as logs:
                    task = await self.drive(monitor)
                    monitor.notify_registered()
                    await asyncio.sleep(0.1)
                    await self.shutdown(monitor, task)
                self.assertEqual(monitor.probes, 0)
                self.assertEqual(bocco.custom_motion_attempts, [])
                self.assertIn(
                    f"webhook_probe_disabled reason={expected}", "\n".join(logs.output)
                )

    async def test_no_log_line_can_carry_the_tunnel_url_or_the_secret(self) -> None:
        bocco = FakeBocco()
        tunnel = FakeTunnel()
        monitor = self.monitor(
            self.config(webhook_secret="super-secret-value"), bocco, tunnel
        )
        task = await self.drive(monitor)
        with self.assertLogs("bocco_bridge.delivery", level="INFO") as logs:
            monitor.notify_registered()
            await wait_until(lambda: monitor.gave_up, timeout=3)
        await self.shutdown(monitor, task)
        output = "\n".join(logs.output)
        self.assertNotIn("super-secret-value", output)
        self.assertNotIn("trycloudflare", output)
        self.assertNotIn("https://", output)


if __name__ == "__main__":
    unittest.main()
