"""Cloudflare Quick Tunnel child-process supervision."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import re
from typing import Protocol

from .config import BridgeConfig
from .events import BoccoClient


LOGGER = logging.getLogger(__name__)
QUICK_TUNNEL_URL = re.compile(
    rb"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com(?![a-z0-9.-])",
    re.IGNORECASE,
)


class StreamLike(Protocol):
    async def readline(self) -> bytes: ...


class ProcessLike(Protocol):
    stdout: StreamLike | None
    stderr: StreamLike | None
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[], Awaitable[ProcessLike]]
WebhookSecretCallback = Callable[[str], None]
# Called after every successful registration, including re-registrations. The
# delivery monitor uses it to schedule a probe: a freshly registered Quick
# Tunnel hostname is exactly the moment BOCCO has been observed to accept a
# registration and then never deliver to it.
RegistrationCallback = Callable[[], None]


def extract_quick_tunnel_url(line: bytes) -> str | None:
    match = QUICK_TUNNEL_URL.search(line)
    return None if match is None else match.group(0).decode("ascii").lower()


class TunnelSupervisor:
    """Restart cloudflared and register every newly issued public URL."""

    def __init__(
        self,
        config: BridgeConfig,
        bocco: BoccoClient,
        process_factory: ProcessFactory | None = None,
        webhook_secret_updated: WebhookSecretCallback | None = None,
        registration_observed: RegistrationCallback | None = None,
    ) -> None:
        self.config = config
        self.bocco = bocco
        self._process_factory = process_factory or self._spawn_cloudflared
        self._webhook_secret_updated = webhook_secret_updated
        self._registration_observed = registration_observed
        self._stopping = asyncio.Event()
        # One registration at a time: the supervisor's own start-up
        # registration and a monitor-driven re-registration must never
        # interleave, or the rotated secret published to the listener could be
        # the older of the two responses.
        self._registration_lock = asyncio.Lock()
        self._process: ProcessLike | None = None
        self.current_url: str | None = None
        self.ready = False
        # Monotonically increasing; the recycle path waits on it rather than on
        # `ready`, which is briefly true both before and after a restart.
        self.registrations = 0

    async def _spawn_cloudflared(self) -> ProcessLike:
        return await asyncio.create_subprocess_exec(
            self.config.cloudflared_path,
            "tunnel",
            "--no-autoupdate",
            "--url",
            f"http://127.0.0.1:{self.config.public_port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def stop(self) -> None:
        self._stopping.set()
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()

    async def _register(self, url: str) -> None:
        """Register ``url``, publish the rotated secret, and announce it."""

        async with self._registration_lock:
            webhook_secret = await self.bocco.register_webhook(
                f"{url}{self.config.webhook_path}"
            )
            if self._webhook_secret_updated is not None:
                self._webhook_secret_updated(webhook_secret)
            self.registrations += 1
        # Outside the lock: the callback may synchronously wake a task that
        # wants to re-register, and holding the lock across it would deadlock.
        if self._registration_observed is not None:
            self._registration_observed()

    async def reregister(self) -> bool:
        """Re-send the current URL to the Platform API.

        The cheap half of the self-heal: it costs three API calls and has no
        physical effect, and it fixes the case where the original registration
        was accepted but never took on BOCCO's delivery side. It cannot fix a
        hostname BOCCO has decided not to deliver to at all — that is what
        :meth:`recycle` is for.
        """

        url = self.current_url
        if url is None or self._stopping.is_set():
            return False
        try:
            await self._register(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("tunnel_reregister_failed error_type=%s", type(exc).__name__)
            return False
        LOGGER.warning("tunnel_reregistered provider=quick_tunnel")
        return True

    async def recycle(self, timeout: float) -> bool:
        """Force a new Quick Tunnel hostname and wait for it to be registered.

        This is the expensive half of the self-heal, and it is what a manual
        bridge restart actually does: a quick tunnel mints a new random
        hostname on every start, so this is the only way to stop asking BOCCO
        to deliver to a hostname it is ignoring. The listener, the queue and
        every in-flight reply survive it — only cloudflared is replaced.
        """

        if self._stopping.is_set():
            return False
        before = self.registrations
        process = self._process
        if process is None or process.returncode is not None:
            return False
        LOGGER.warning("tunnel_recycling provider=quick_tunnel")
        process.terminate()
        deadline = asyncio.get_running_loop().time() + timeout
        while self.registrations == before:
            if (
                self._stopping.is_set()
                or asyncio.get_running_loop().time() >= deadline
            ):
                LOGGER.warning("tunnel_recycle_timeout provider=quick_tunnel")
                return False
            await asyncio.sleep(0.05)
        return True

    async def run(self) -> None:
        while not self._stopping.is_set():
            watchers: list[asyncio.Task[None]] = []
            self.current_url = None
            self.ready = False
            try:
                process = await self._process_factory()
                self._process = process
                url, watchers = await asyncio.wait_for(
                    self._discover_url(process),
                    timeout=self.config.tunnel_url_timeout_seconds,
                )
                self.current_url = url
                await self._register(url)
                self.ready = True
                LOGGER.info("tunnel_registered provider=quick_tunnel")
                returncode = await process.wait()
                if not self._stopping.is_set():
                    LOGGER.warning("tunnel_exited returncode=%d", returncode)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopping.is_set():
                    LOGGER.warning("tunnel_failed error_type=%s", type(exc).__name__)
            finally:
                self.ready = False
                self.current_url = None
                for watcher in watchers:
                    watcher.cancel()
                if watchers:
                    await asyncio.gather(*watchers, return_exceptions=True)
                await self._stop_process()
                self._process = None

            if not self._stopping.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.config.tunnel_restart_seconds
                    )
                except TimeoutError:
                    pass

    async def _discover_url(
        self, process: ProcessLike
    ) -> tuple[str, list[asyncio.Task[None]]]:
        loop = asyncio.get_running_loop()
        found: asyncio.Future[str] = loop.create_future()
        watchers = [
            asyncio.create_task(self._watch_stream(stream, found))
            for stream in (process.stdout, process.stderr)
            if stream is not None
        ]
        if not watchers:
            raise RuntimeError("cloudflared output is not available")
        succeeded = False
        try:
            while True:
                await asyncio.wait(
                    {found, *watchers}, return_when=asyncio.FIRST_COMPLETED
                )
                if found.done() and not found.cancelled():
                    succeeded = True
                    return found.result(), watchers
                for watcher in watchers:
                    if watcher.done() and not watcher.cancelled():
                        error = watcher.exception()
                        if error is not None:
                            raise error
                if all(watcher.done() for watcher in watchers):
                    raise RuntimeError("cloudflared exited before publishing a URL")
        finally:
            if not succeeded:
                if not found.done():
                    found.cancel()
                for watcher in watchers:
                    watcher.cancel()
                await asyncio.gather(*watchers, return_exceptions=True)

    @staticmethod
    async def _watch_stream(stream: StreamLike, found: asyncio.Future[str]) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            url = extract_quick_tunnel_url(line)
            if url is not None and not found.done():
                found.set_result(url)

    async def _stop_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            process.kill()
            await process.wait()
