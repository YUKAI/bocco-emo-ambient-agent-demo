"""Concrete assembly of the BOCCO client, Hermes client and webhook parser."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .app import RuntimeDependencies
from .bocco import (
    AtomicFileTokenStore,
    BoccoClient,
    BoccoClientConfig,
    InboundEvent,
    TokenState,
    parse_inbound_event,
)
from .config import BridgeConfig
from .hermes import HermesClient, HermesConfig


class BoccoWebhookParser:
    """Adapt Worker 1's strict parser to the runtime parser boundary."""

    def parse(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> InboundEvent:
        del received_at
        return parse_inbound_event(payload)


def create_dependencies(config: BridgeConfig) -> RuntimeDependencies:
    """Construct Worker 1 and Worker 2 clients from injected configuration."""

    token_path = Path(
        config.extra.get(
            "BOCCO_TOKEN_FILE", str(config.database_path.with_name("oauth.json"))
        )
    )
    token_store = AtomicFileTokenStore(token_path)
    if not token_path.exists():
        token_store.save(
            TokenState(
                access_token=_required(config.extra, "BOCCO_ACCESS_TOKEN"),
                refresh_token=_required(config.extra, "BOCCO_REFRESH_TOKEN"),
                access_expires_at=_optional_datetime(
                    config.extra.get("BOCCO_ACCESS_EXPIRES_AT")
                ),
            )
        )

    bocco = BoccoClient(
        BoccoClientConfig(
            base_url=config.extra.get(
                "BOCCO_PLATFORM_BASE_URL", "https://platform-api.bocco.me"
            ),
            timeout_seconds=_positive_float(
                config.extra, "BOCCO_API_TIMEOUT_SECONDS", 10.0
            ),
        ),
        token_store,
    )
    hermes = HermesClient(
        HermesConfig(
            api_key=_required(config.extra, "API_SERVER_KEY"),
            api_base_url=config.extra.get(
                "HERMES_API_URL", "http://127.0.0.1:8642"
            ),
            model=config.extra.get("HERMES_MODEL", "hermes-agent"),
            response_timeout_seconds=_positive_float(
                config.extra, "HERMES_RESPONSE_TIMEOUT_SECONDS", 60.0
            ),
            max_output_tokens=_positive_int(
                config.extra, "HERMES_MAX_OUTPUT_TOKENS", 128
            ),
        )
    )
    return RuntimeDependencies(
        bocco=bocco,
        hermes=hermes,
        webhook_parser=BoccoWebhookParser(),
    )


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required client configuration: {name}")
    return value.strip()


def _positive_float(
    values: Mapping[str, str], name: str, default: float
) -> float:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _optional_datetime(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("BOCCO_ACCESS_EXPIRES_AT must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("BOCCO_ACCESS_EXPIRES_AT must include a timezone")
    return parsed
