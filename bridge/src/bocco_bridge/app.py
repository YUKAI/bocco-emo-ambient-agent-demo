"""HTTP applications and lifecycle wiring for the bridge service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import importlib
import inspect
import json
import logging
import signal
import time
from typing import Any, cast

from aiohttp import web

from .config import BridgeConfig
from .db import EventDatabase
from .delivery import WebhookDeliveryMonitor
from .event_extraction import EventExtractor
from .event_memory import EventMemory
from .events import BoccoClient, EventProcessor, EventWorker, HermesClient, WebhookParser
from .memory import HouseholdMemory
from .motion_invention import MotionInventionGenerator
from .motions import MotionCatalog
from .reaction_generation import HermesPriorityGate, ReactionBankGenerator
from .repertoire import MotionRepertoire
from .scheduler import ProactiveScheduler
from .stamps import StampCatalog
from .transcript import ConversationTranscript
from .tunnel import ProcessFactory, TunnelSupervisor


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """BOCCO and Hermes clients injected into the runtime through shared contracts."""

    bocco: BoccoClient
    hermes: HermesClient
    webhook_parser: WebhookParser

    async def aclose(self) -> None:
        """Close optional client-owned transports without extending contracts."""

        closed: set[int] = set()
        for dependency in (self.hermes, self.bocco):
            if id(dependency) in closed:
                continue
            closed.add(id(dependency))
            close = getattr(dependency, "aclose", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result


ReadinessCheck = Callable[[], bool]


async def _read_limited_body(request: web.Request, limit: int) -> bytes:
    content_length = request.content_length
    if content_length is not None and content_length > limit:
        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=content_length)
    body = await request.read()
    if len(body) > limit:
        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(body))
    return body


MAX_WEBHOOK_SECRET_HEADER_CHARS = 512


def _secret_matches(provided: str | None, expected: str) -> bool:
    # compare_digest runs in time proportional to its input, so cap the header
    # before comparing. A secret this long is never legitimate, and rejecting it
    # early keeps a junk header from doing measurable work. Normal-length input
    # still takes the constant-time path.
    if not isinstance(provided, str) or len(provided) > MAX_WEBHOOK_SECRET_HEADER_CHARS:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


class WebhookSecretStore:
    """In-memory current secret, rotated after every Webhook registration."""

    def __init__(self, secret: str) -> None:
        self._secret = ""
        self.replace(secret)

    def replace(self, secret: str) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("registered Webhook secret must not be empty")
        self._secret = secret

    def matches(self, provided: str | None) -> bool:
        return _secret_matches(provided, self._secret)


def create_public_app(
    config: BridgeConfig,
    database: EventDatabase,
    webhook_parser: WebhookParser,
    notify_worker: Callable[[], None] | None = None,
    webhook_secrets: WebhookSecretStore | None = None,
    readiness: ReadinessCheck | None = None,
    delivery_observed: Callable[[], None] | None = None,
) -> web.Application:
    """Create the loopback listener that Quick Tunnel is allowed to publish."""

    app = web.Application(client_max_size=config.max_webhook_body_bytes)
    current_secrets = webhook_secrets or WebhookSecretStore(config.webhook_secret)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def ready(_: web.Request) -> web.Response:
        is_ready = True if readiness is None else readiness()
        return web.json_response(
            {"status": "ready" if is_ready else "not_ready"},
            status=200 if is_ready else 503,
        )

    async def webhook(request: web.Request) -> web.Response:
        started = asyncio.get_running_loop().time()
        if not current_secrets.matches(request.headers.get("X-Platform-API-Secret")):
            LOGGER.warning("webhook_rejected reason=authentication")
            raise web.HTTPUnauthorized()
        # The earliest point at which BOCCO has demonstrably reached this
        # bridge with the secret it was given, and therefore the honest place
        # to record proof of delivery. Before parsing on purpose: a payload
        # this bridge cannot decode still proves the delivery path is alive.
        if delivery_observed is not None:
            delivery_observed()
        try:
            body = await _read_limited_body(request, config.max_webhook_body_bytes)
            decoded = json.loads(body)
            if not isinstance(decoded, Mapping):
                raise ValueError("Webhook body must be an object")
            event = webhook_parser.parse(decoded, datetime.now(timezone.utc))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            LOGGER.warning("webhook_rejected reason=validation")
            raise web.HTTPBadRequest() from None

        if (
            event.event_type == "message.received"
            and event.room_uuid is not None
        ):
            if event.message_media == "audio":
                await database.wait_for_outbound_audio(event.room_uuid)
            elif event.message_media == "stamp":
                await database.wait_for_outbound_stamp(event.room_uuid)
        inserted = await database.enqueue(event)
        if inserted and notify_worker is not None:
            notify_worker()
        LOGGER.info(
            "webhook_enqueued request_id=%s event_type=%s duplicate=%s duration_ms=%d",
            event.request_id,
            event.event_type,
            str(not inserted).lower(),
            round((asyncio.get_running_loop().time() - started) * 1000),
        )
        return web.json_response(
            {"accepted": inserted, "duplicate": not inserted}, status=202
        )

    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    app.router.add_post(config.webhook_path, webhook)
    return app


class BridgeRuntime:
    """Own the database, public HTTP listener, worker, and optional tunnel."""

    def __init__(
        self,
        config: BridgeConfig,
        dependencies: RuntimeDependencies,
        *,
        tunnel_process_factory: ProcessFactory | None = None,
    ) -> None:
        self.config = config
        self.dependencies = dependencies
        self.database = EventDatabase(config.database_path)
        self.memory = HouseholdMemory(config.memory_path)
        self.transcript = ConversationTranscript(config.transcript_path)
        self.repertoire = MotionRepertoire(config.repertoire_path)
        self.events = EventMemory(config.event_path)
        self.motion_catalog = MotionCatalog()
        self.stamp_catalog = StampCatalog()
        self.hermes_gate = HermesPriorityGate(dependencies.hermes)
        self.processor = EventProcessor(
            config,
            self.database,
            dependencies.bocco,
            self.hermes_gate.foreground,
            motion_catalog=self.motion_catalog,
            stamp_catalog=self.stamp_catalog,
            memory=self.memory,
            transcript=self.transcript,
            repertoire=self.repertoire,
            events=self.events,
        )
        self.worker = EventWorker(config, self.database, self.processor)
        self.reaction_bank_generator = ReactionBankGenerator(
            self.database,
            self.hermes_gate,
            poll_seconds=config.worker_poll_seconds,
            retry_base_seconds=config.worker_retry_base_seconds,
            # The bank's prompts are self-contained, so whenever the bridge is
            # asked to bound Hermes at all, the bank stops storing entirely.
            stateless_conversation=(
                config.hermes_conversation_mode != "persistent"
            ),
        )
        self.motion_invention_generator = (
            MotionInventionGenerator(
                self.database,
                self.hermes_gate,
                self.repertoire,
                self.processor,
                poll_seconds=config.worker_poll_seconds,
                retry_base_seconds=config.worker_retry_base_seconds,
                max_attempts=config.worker_max_attempts,
                retention=config.motion_invention_retention,
            )
            if config.motion_invention_enabled
            else None
        )
        if self.motion_invention_generator is not None:
            self.processor.motion_invention_notify = (
                self.motion_invention_generator.notify
            )
        self.event_extractor = (
            EventExtractor(
                self.database,
                self.hermes_gate,
                self.events,
                poll_seconds=config.worker_poll_seconds,
                retry_base_seconds=config.worker_retry_base_seconds,
                max_attempts=config.worker_max_attempts,
                retention=config.event_memory_retention,
                min_interval_seconds=(
                    config.event_extraction_min_interval_seconds
                ),
            )
            if config.event_memory_enabled
            else None
        )
        if self.event_extractor is not None:
            self.processor.event_extraction_notify = self.event_extractor.notify
        self.scheduler = ProactiveScheduler(self.database, self.worker.notify)
        self.webhook_secrets = WebhookSecretStore(config.webhook_secret)
        # Built before the tunnel so the supervisor can announce registrations
        # straight into it; the two are otherwise independent, and the monitor
        # still detects and reports a stall when no tunnel is supervised here.
        self.delivery_monitor = WebhookDeliveryMonitor(config, dependencies.bocco)
        self.tunnel = (
            TunnelSupervisor(
                config,
                dependencies.bocco,
                process_factory=tunnel_process_factory,
                webhook_secret_updated=self.webhook_secrets.replace,
                registration_observed=self.delivery_monitor.notify_registered,
            )
            if config.tunnel_enabled
            else None
        )
        if self.tunnel is not None:
            self.delivery_monitor.attach_tunnel(self.tunnel)
        self._public_runner: web.AppRunner | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._reaction_bank_task: asyncio.Task[None] | None = None
        self._motion_invention_task: asyncio.Task[None] | None = None
        self._event_extraction_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._motion_timeout_task: asyncio.Task[None] | None = None
        self._tunnel_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._database_ready = False
        self._dependencies_closed = False

    def is_ready(self) -> bool:
        # Deliberately not including the delivery monitor. Readiness gates
        # /readyz, and a bug in a self-heal that only exists to make a failure
        # visible must never be able to report the whole bridge as down — or,
        # worse, invite a supervisor to restart it in a loop.
        tunnel_ready = self.tunnel is None or self.tunnel.ready
        invention_ready = (
            self.motion_invention_generator is None
            or self.motion_invention_generator.ready
        )
        extraction_ready = (
            self.event_extractor is None or self.event_extractor.ready
        )
        return (
            self._database_ready
            and self.worker.ready
            and self.reaction_bank_generator.ready
            and invention_ready
            and extraction_ready
            and self.scheduler.ready
            and tunnel_ready
        )

    async def start(self) -> None:
        await self.database.initialize()
        await self.memory.initialize()
        if self.config.conversation_memory_enabled:
            # Only ever touched when the feature is on: with it off no
            # transcript database is created at all.
            await self.transcript.initialize()
        if self.config.motion_invention_enabled:
            # Same rule: with the feature off no repertoire database is created.
            await self.repertoire.initialize()
        if self.config.event_memory_enabled:
            # And again: with the feature off no event database is created.
            await self.events.initialize()
        await self.database.abandon_motion_chains_for_restart()
        await self.processor.schedule_reaction_bank_refresh(
            f"startup:{time.time_ns()}", self.config.motion_room_uuid or None
        )
        self.reaction_bank_generator.notify()
        if self.config.motions_enabled:
            try:
                await self.motion_catalog.load(self.dependencies.bocco)
                LOGGER.info(
                    "motion_catalog_loaded motion_count=%d",
                    self.motion_catalog.size,
                )
            except Exception as exc:
                LOGGER.warning(
                    "motion_catalog_unavailable error_type=%s", type(exc).__name__
                )
            try:
                await self.stamp_catalog.load(self.dependencies.bocco)
                LOGGER.info(
                    "stamp_catalog_loaded stamp_count=%d",
                    self.stamp_catalog.size,
                )
            except Exception as exc:
                LOGGER.warning(
                    "stamp_catalog_unavailable error_type=%s", type(exc).__name__
                )
            if self.config.motion_room_uuid:
                try:
                    settings = await self.dependencies.bocco.get_emo_settings(
                        self.config.motion_room_uuid
                    )
                    self.processor.set_voice_speed(settings.voice_speed)
                    LOGGER.info("motion_voice_speed_loaded")
                except Exception as exc:
                    LOGGER.warning(
                        "motion_voice_speed_unavailable error_type=%s",
                        type(exc).__name__,
                    )
        self._database_ready = True
        self._worker_task = asyncio.create_task(self.worker.run(), name="event-worker")
        self._reaction_bank_task = asyncio.create_task(
            self.reaction_bank_generator.run(), name="reaction-bank-generator"
        )
        if self.motion_invention_generator is not None:
            self._motion_invention_task = asyncio.create_task(
                self.motion_invention_generator.run(), name="motion-invention-generator"
            )
        if self.event_extractor is not None:
            self._event_extraction_task = asyncio.create_task(
                self.event_extractor.run(), name="event-extractor"
            )
        self._scheduler_task = asyncio.create_task(
            self.scheduler.run(), name="proactive-scheduler"
        )
        self._motion_timeout_task = asyncio.create_task(
            self._sweep_motion_timeouts(), name="motion-timeout-sweeper"
        )
        # Its own task, never the event worker's: the probe waits up to twenty
        # seconds for an echo and must not be able to hold up a reply.
        self._delivery_task = asyncio.create_task(
            self.delivery_monitor.run(), name="webhook-delivery-monitor"
        )

        public_app = create_public_app(
            self.config,
            self.database,
            self.dependencies.webhook_parser,
            self._notify_webhook_event,
            self.webhook_secrets,
            self.is_ready,
            self.delivery_monitor.observe,
        )
        try:
            self._public_runner = web.AppRunner(public_app, access_log=None)
            await self._public_runner.setup()
            await web.TCPSite(
                self._public_runner, self.config.public_host, self.config.public_port
            ).start()

            if self.tunnel is not None:
                self._tunnel_task = asyncio.create_task(
                    self.tunnel.run(), name="tunnel-supervisor"
                )
        except BaseException:
            await self.stop()
            raise
        LOGGER.info("bridge_started listener=loopback")

    async def stop(self) -> None:
        self._stopping.set()
        await self.scheduler.stop()
        await self.reaction_bank_generator.stop()
        if self.motion_invention_generator is not None:
            await self.motion_invention_generator.stop()
        if self.event_extractor is not None:
            await self.event_extractor.stop()
        if self.tunnel is not None:
            await self.tunnel.stop()
        await self.delivery_monitor.stop()
        await self.worker.stop()

        tasks = [
            task
            for task in (
                self._delivery_task,
                self._tunnel_task,
                self._scheduler_task,
                self._motion_timeout_task,
                self._worker_task,
                self._reaction_bank_task,
                self._motion_invention_task,
                self._event_extraction_task,
            )
            if task is not None
        ]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=5.0
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        await self.processor.stop_background_tasks()

        if self._public_runner is not None:
            await self._public_runner.cleanup()
            self._public_runner = None
        self._database_ready = False
        if not self._dependencies_closed:
            try:
                await self.dependencies.aclose()
            except Exception as exc:
                LOGGER.warning("dependency_close_failed error_type=%s", type(exc).__name__)
            self._dependencies_closed = True
        LOGGER.info("bridge_stopped")

    def _notify_webhook_event(self) -> None:
        """Wake the worker and immediately yield background Hermes capacity."""

        self.hermes_gate.interrupt_background()
        self.worker.notify()

    async def _sweep_motion_timeouts(self) -> None:
        interval = min(5.0, self.config.motion_chain_timeout_seconds)
        while not self._stopping.is_set():
            await self.database.abandon_expired_motion_chains()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except TimeoutError:
                pass


async def _load_dependencies(config: BridgeConfig) -> RuntimeDependencies:
    factory_path = config.dependency_factory
    if not factory_path or ":" not in factory_path:
        raise RuntimeError(
            "BOCCO_BRIDGE_DEPENDENCY_FACTORY must name a dependency factory "
            "as 'module:function'"
        )
    module_name, function_name = factory_path.rsplit(":", 1)
    factory = getattr(importlib.import_module(module_name), function_name)
    result = factory(config)
    if inspect.isawaitable(result):
        result = await cast(Awaitable[Any], result)
    if not isinstance(result, RuntimeDependencies):
        raise TypeError("dependency factory must return RuntimeDependencies")
    return result


async def _serve() -> None:
    config = BridgeConfig.from_environment()
    dependencies = await _load_dependencies(config)
    runtime = BridgeRuntime(config, dependencies)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            pass
    await runtime.start()
    try:
        await stopping.wait()
    finally:
        await runtime.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
