"""Detect and repair a BOCCO Webhook delivery stall.

THE FAILURE THIS EXISTS FOR
---------------------------
The bridge is reached through a Cloudflare *quick* tunnel, which mints a new
random hostname on every start. On startup the bridge registers that hostname
with the BOCCO Platform API. Usually the registration takes. Sometimes it is
accepted and then never used: BOCCO delivers nothing, forever.

What that looks like from inside the house is the worst part. The bridge is
healthy, the tunnel is up, the registered URL answers correctly from the public
internet (401 on a wrong secret, 400 on a malformed body — both verified from
two networks during one incident), the secret matches, the event queue is
empty, and nothing at all appears in any log. The robot simply stops
responding. It happened three times in one day and took about twenty minutes to
diagnose the first time. Restarting the bridge — which mints a *new* hostname
and re-registers — cleared it every time.

WHY DETECTION NEEDS A PROBE
---------------------------
"No webhook has arrived recently" is not evidence of failure: a quiet house
legitimately produces none for hours. The only way to tell a silent room from a
severed delivery path is to make an event happen and see whether it comes back.

THE PROBE
---------
``POST /v1/rooms/{room}/motions`` echoes back to the bridge as a
``message.received`` webhook with ``media=motion`` — 25 such echoes appear in
the retained echo-suppression audit, so the echo is a measured behaviour, not a
hope. Custom motion documents are silent (the document ``sound`` key is inert
in the tested firmware) and emit no ``motion.finished``, so the echo is the
*only* signal they produce. The document we send commands nothing: one head
transition with every control point null, which the device reads as "hold the
posture you already have". See ``custom_motions.silent_probe_document``.

WHAT COUNTS AS PROOF
--------------------
Any authenticated inbound webhook that arrives after the probe was dispatched,
not specifically the probe's own echo. Correlating on the returned message id
would be more precise about *which* event came back and no more accurate about
the only question being asked — is delivery working — while being strictly less
sensitive: if a person happens to speak during the probe window, that proves
delivery just as conclusively, and we should accept it and stop.

THE LADDER
----------
Probe. If it fails, re-register the same URL (cheap, no physical effect) and
probe again. If that fails, recycle the tunnel so a new hostname is minted and
registered — which is what a manual restart does — and probe again. Then stop
and say so loudly. Never more attempts than that, never a restart loop, and
never a flood of Platform API calls.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
import logging
import time
from typing import Any, Protocol

from .config import WEBHOOK_PROBE_REPAIR_LADDER, BridgeConfig
from .custom_motions import silent_probe_document


LOGGER = logging.getLogger(__name__)

REGISTRATION_REASON = "registration"
SILENCE_REASON = "silence"


class ProbeSender(Protocol):
    """The one Platform API call the monitor makes on the happy path."""

    async def send_custom_motion(
        self, room_uuid: str, document: Mapping[str, Any]
    ) -> Any: ...


# Both return True when the remedy was applied. Neither promises it worked;
# that is what the following probe is for.
Remedy = Callable[[], Awaitable[bool]]


class TunnelLike(Protocol):
    """The repair surface of TunnelSupervisor, named structurally.

    Stated as a Protocol rather than importing the class so the dependency
    stays one-way: tunnel.py already knows how to announce a registration, and
    nothing here needs to know how cloudflared is spawned.
    """

    async def reregister(self) -> bool: ...

    async def recycle(self, timeout: float) -> bool: ...


class _ProbeInconclusive(Exception):
    """The probe could not be sent, so nothing was learned about delivery.

    Distinct from a failed probe on purpose: a Platform API we cannot reach at
    all is not evidence that BOCCO has stopped delivering, and re-registering
    or recycling the tunnel in response would spend the whole heal budget on a
    diagnosis we never made.
    """


class WebhookDeliveryMonitor:
    """Prove Webhook delivery is alive, and repair it when it is not."""

    def __init__(
        self,
        config: BridgeConfig,
        bocco: ProbeSender,
        *,
        reregister: Remedy | None = None,
        recycle: Remedy | None = None,
        local_now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.bocco = bocco
        self._reregister = reregister
        self._recycle = recycle
        self._local_now = local_now
        self._monotonic = monotonic
        self._stopping = asyncio.Event()
        # Set by observe() from the Webhook handler; cleared immediately before
        # a probe is dispatched so the wait cannot be satisfied by a webhook
        # that arrived before the probe existed.
        self._inbound = asyncio.Event()
        self._registered = asyncio.Event()
        self._last_inbound = 0.0
        self.ready = False
        # Public because it is the one piece of monitor state an operator (or a
        # test) may legitimately want to read: "this bridge has stopped trying".
        self.gave_up = False
        # Test/diagnostic counters. Nothing branches on them.
        self.probes = 0
        self.heal_cycles = 0

        room = config.motion_room_uuid.strip()
        # Every reason the monitor can be off, decided once and stated once,
        # rather than discovered later as an unexplained absence of probes.
        if not config.webhook_probe_enabled:
            self.disabled_reason: str | None = "configuration"
        elif not config.motions_enabled:
            # The probe is a motion post. Somebody who switched motions off
            # asked for no motion traffic at all, and this is motion traffic.
            self.disabled_reason = "motions_disabled"
        elif not room:
            self.disabled_reason = "no_room"
        else:
            self.disabled_reason = None
        self.room_uuid = room

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def attach_tunnel(self, tunnel: TunnelLike) -> None:
        """Adopt a supervisor's repair actions after both objects exist.

        The monitor must be constructed first so the supervisor can be given
        :meth:`notify_registered`; this closes the other half of that loop
        without either class importing the other.
        """

        self._reregister = tunnel.reregister
        self._recycle = lambda: tunnel.recycle(
            self.config.webhook_probe_recycle_timeout_seconds
        )

    def observe(self) -> None:
        """Record that BOCCO reached this bridge. Called on the hot path.

        Deliberately trivial and synchronous: it runs inside the Webhook
        handler, ahead of everything that decides a reply, and may not cost the
        request measurable time.
        """

        self._last_inbound = self._monotonic()
        self._inbound.set()
        if self.gave_up:
            # Delivery came back on its own — the tunnel restarted, the
            # platform unstuck itself, or a person power-cycled something. Say
            # so, and re-arm: the loud "stopped trying" line above it in the
            # journal would otherwise be the last word on a working system.
            self.gave_up = False
            LOGGER.warning("webhook_delivery_restored source=inbound_traffic")

    def notify_registered(self) -> None:
        """Called by the tunnel supervisor after every successful registration."""

        self._registered.set()
        # A brand new hostname deserves a fresh verdict even if we previously
        # gave up on the old one.
        self.gave_up = False

    async def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        if not self.enabled:
            LOGGER.info("webhook_probe_disabled reason=%s", self.disabled_reason)
            self.ready = True
            await self._stopping.wait()
            self.ready = False
            return

        self._last_inbound = self._monotonic()
        self.ready = True
        LOGGER.info(
            "webhook_probe_armed silence_s=%d timeout_s=%d max_attempts=%d",
            round(self.config.webhook_probe_silence_seconds),
            round(self.config.webhook_probe_timeout_seconds),
            self.config.webhook_probe_max_attempts,
        )
        try:
            while not self._stopping.is_set():
                reason = self._due_reason()
                if reason is not None:
                    try:
                        await self._trigger(reason)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # A self-heal that crashes the loop would leave the
                        # bridge worse off than the failure it was written for.
                        LOGGER.warning(
                            "webhook_probe_cycle_failed reason=%s error_type=%s",
                            reason,
                            type(exc).__name__,
                        )
                        self._registered.clear()
                        self._last_inbound = self._monotonic()
                await self._pause(self._tick_seconds())
        finally:
            self.ready = False

    def _tick_seconds(self) -> float:
        """How often the loop looks for a reason to probe.

        One second at the shipped settings — the loop is never the slow part of
        anything, and both triggers tolerate a second of lag (the registration
        probe deliberately waits five). Scaled down when the probe timeout is
        configured very short, so a test-sized configuration behaves like one.
        """

        return min(1.0, max(0.02, self.config.webhook_probe_timeout_seconds / 4))

    def _due_reason(self) -> str | None:
        if self._registered.is_set():
            return REGISTRATION_REASON
        if self.gave_up:
            return None
        silent_for = self._monotonic() - self._last_inbound
        if silent_for < self.config.webhook_probe_silence_seconds:
            return None
        if not self._within_active_hours():
            return None
        return SILENCE_REASON

    def _within_active_hours(self) -> bool:
        hour = self._local_now().hour
        return (
            self.config.webhook_probe_active_start_hour
            <= hour
            < self.config.webhook_probe_active_end_hour
        )

    async def _trigger(self, reason: str) -> None:
        if reason == REGISTRATION_REASON:
            self._registered.clear()
            mark = self._last_inbound
            await self._pause(self.config.webhook_probe_startup_delay_seconds)
            if self._stopping.is_set():
                return
            if self._last_inbound != mark:
                # Real traffic landed while we were waiting, which proves
                # delivery for free. Sending a probe now would be pure cost.
                LOGGER.info(
                    "webhook_probe_skipped reason=%s cause=inbound_traffic", reason
                )
                self._last_inbound = self._monotonic()
                return
        await self._heal(reason)

    async def _heal(self, reason: str) -> None:
        """One bounded detect-remedy-verify cycle."""

        self.heal_cycles += 1
        started = self._monotonic()
        try:
            confirmed = await self._probe(reason, 1)
        except _ProbeInconclusive:
            self._finish_cycle()
            return
        if confirmed:
            self._finish_cycle()
            return

        attempt = 1
        while attempt < self.config.webhook_probe_max_attempts:
            attempt += 1
            # Re-registration first because it is free and covers the most
            # likely cause; the tunnel recycle is held back because it throws
            # away a hostname and costs a cloudflared restart. Indexing the
            # ladder rather than testing the attempt number means the loop can
            # never invent a rung: the config bound is derived from this same
            # tuple, so the two cannot drift apart.
            action = WEBHOOK_PROBE_REPAIR_LADDER[attempt - 2]
            LOGGER.warning(
                "webhook_delivery_stalled reason=%s failed_attempts=%d action=%s",
                reason,
                attempt - 1,
                action,
            )
            if not await self._apply(action):
                LOGGER.warning(
                    "webhook_repair_unavailable action=%s attempt=%d", action, attempt
                )
            # Exponential, and bounded by max_attempts rather than by a cap:
            # 15s then 30s at the defaults, which is deliberately generous
            # against a platform that has just been told a new URL.
            await self._pause(
                self.config.webhook_probe_backoff_seconds * (2 ** (attempt - 2))
            )
            if self._stopping.is_set():
                self._finish_cycle()
                return
            try:
                confirmed = await self._probe(reason, attempt)
            except _ProbeInconclusive:
                self._finish_cycle()
                return
            if confirmed:
                LOGGER.warning(
                    "webhook_delivery_recovered reason=%s attempts=%d action=%s "
                    "outage_s=%d",
                    reason,
                    attempt,
                    action,
                    round(self._monotonic() - started),
                )
                self._finish_cycle()
                return

        # Loud, and then quiet: repeating this every tick would bury the one
        # line that matters and keep posting motions into somebody's living
        # room. Delivery arriving on its own re-arms the monitor (see observe).
        LOGGER.error(
            "webhook_delivery_unrecoverable reason=%s attempts=%d outage_s=%d "
            "hint=restart_bocco_bridge_service",
            reason,
            self.config.webhook_probe_max_attempts,
            round(self._monotonic() - started),
        )
        self.gave_up = True
        self._finish_cycle()

    def _finish_cycle(self) -> None:
        """Re-arm the silence timer and drop any registration raised mid-cycle.

        A recycle registers a new hostname and therefore raises the
        registration flag from inside this very cycle; acting on it afterwards
        would start a second cycle for a URL this one has already probed. The
        cost is that a coincidental cloudflared restart during a heal cycle
        does not earn its own probe — the silence trigger still covers it, and
        ping-ponging between the two triggers would be far worse.
        """

        self._registered.clear()
        self._last_inbound = self._monotonic()

    async def _apply(self, action: str) -> bool:
        remedy = self._reregister if action == "reregister" else self._recycle
        if remedy is None:
            # No tunnel supervisor: someone is publishing this bridge another
            # way. Detection and the loud log still work; repair does not.
            return False
        try:
            return await remedy()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "webhook_repair_failed action=%s error_type=%s",
                action,
                type(exc).__name__,
            )
            return False

    async def _probe(self, reason: str, attempt: int) -> bool:
        """Send the silent motion and wait for any webhook to come back."""

        LOGGER.info("webhook_probe_started reason=%s attempt=%d", reason, attempt)
        # Cleared before the send so that only a webhook the platform delivers
        # from here on can satisfy the wait.
        self._inbound.clear()
        started = self._monotonic()
        try:
            await self.bocco.send_custom_motion(
                self.room_uuid, silent_probe_document()
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "webhook_probe_send_failed reason=%s attempt=%d error_type=%s",
                reason,
                attempt,
                type(exc).__name__,
            )
            raise _ProbeInconclusive() from exc
        self.probes += 1
        try:
            await asyncio.wait_for(
                self._inbound.wait(),
                timeout=self.config.webhook_probe_timeout_seconds,
            )
        except TimeoutError:
            LOGGER.warning(
                "webhook_probe_timeout reason=%s attempt=%d waited_s=%d",
                reason,
                attempt,
                round(self.config.webhook_probe_timeout_seconds),
            )
            return False
        LOGGER.info(
            "webhook_probe_confirmed reason=%s attempt=%d elapsed_ms=%d",
            reason,
            attempt,
            round((self._monotonic() - started) * 1000),
        )
        return True

    async def _pause(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown."""

        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass
