"""Async client for Hermes' loopback-only Responses API."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..streaming import complete_sentence_prefix
from .response_parser import extract_final_output_text

BOCCO_SPEECH_INSTRUCTIONS = (
    "自然で短い日本語で、一回の発話として答えてください。"
    "Markdownや箇条書きは使わないでください。"
)


class HermesError(RuntimeError):
    """Base class for safe-to-log Hermes adapter errors."""


class HermesAPIError(HermesError):
    """Hermes API rejected a request or returned an invalid response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HermesTimeoutError(HermesError):
    """A Hermes request exceeded its configured timeout."""


class HermesIncompleteError(HermesError):
    """Hermes stopped generating before it finished a sentence.

    Raised when a response is reported ``incomplete`` — the model hit
    ``max_output_tokens`` — and nothing usable survives once the unfinished
    trailing sentence is dropped. The bridge must not speak that fragment
    as though it were the whole answer.
    """


@dataclass(frozen=True, slots=True)
class HermesConfig:
    """Explicit runtime configuration for :class:`HermesClient`.

    The runtime owns environment loading. This library only accepts already
    resolved values, keeping unit tests and secret handling deterministic.
    """

    api_key: str
    api_base_url: str = "http://127.0.0.1:8642"
    model: str = "hermes-agent"
    response_timeout_seconds: float = 60.0
    max_output_tokens: int = 128

    def __post_init__(self) -> None:
        for name in ("api_key", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        _validate_http_base_url(self.api_base_url, "api_base_url")
        value = self.response_timeout_seconds
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("response_timeout_seconds must be a positive finite number")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")


def bocco_conversation(room_uuid: str, *, epoch: int | None = None) -> str:
    """Build the Hermes named conversation for a BOCCO room.

    Without an ``epoch`` this is the historical stable name, which Hermes
    appends to forever. With one it is a rotating name: a new epoch is a new,
    empty Hermes conversation, which is how the stored prompt is kept from
    growing without bound.
    """

    if not isinstance(room_uuid, str) or not room_uuid.strip():
        raise ValueError("room_uuid must be a non-empty string")
    normalized = room_uuid.strip()
    if any(character.isspace() or character in "\r\n" for character in normalized):
        raise ValueError("room_uuid must not contain whitespace")
    if epoch is None:
        return f"bocco-room:{normalized}"
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("epoch must be a non-negative integer")
    return f"bocco-room:{normalized}:e{epoch}"


def _request_body(
    config: HermesConfig,
    conversation: str | None,
    text: str,
    instructions: str,
    *,
    stream: bool = False,
) -> dict[str, Any]:
    """Build a Responses request; omit conversation state when stateless."""

    body: dict[str, Any] = {
        "model": config.model,
        "input": text,
        "instructions": instructions,
        # Storing is only useful when a conversation carries the history
        # forward; a stateless turn must not leave one behind either.
        "store": conversation is not None,
        "max_output_tokens": config.max_output_tokens,
    }
    if conversation is not None:
        body["conversation"] = conversation
    if stream:
        body["stream"] = True
    return body


class HermesClient:
    """Call Hermes for BOCCO responses."""

    def __init__(
        self,
        config: HermesConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None

    async def __aenter__(self) -> HermesClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def respond(
        self,
        conversation: str | None,
        text: str,
        instructions: str,
    ) -> str:
        """Generate one final response, optionally in a named conversation.

        ``conversation=None`` makes the request stateless: Hermes is not asked
        to store anything and no prior turns are prepended, so the prompt is
        exactly what the caller supplied.
        """

        if conversation is not None:
            _require_non_empty(conversation, "conversation")
        _require_non_empty(text, "text")
        _require_non_empty(instructions, "instructions")

        body = _request_body(
            self._config, conversation, text, instructions
        )
        try:
            response = await self._http_client.post(
                _join_url(self._config.api_base_url, "/v1/responses"),
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                json=body,
                timeout=self._config.response_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise HermesTimeoutError("Hermes response request timed out") from exc
        except httpx.RequestError as exc:
            raise HermesAPIError("Hermes response request failed") from exc

        if not response.is_success:
            raise HermesAPIError(
                f"Hermes response request failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )

        try:
            payload: Any = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HermesAPIError("Hermes response was not valid JSON") from exc

        text = extract_final_output_text(payload)
        if not _is_incomplete(payload):
            return text
        # The model ran out of output budget mid-sentence. Speak only the
        # sentences it actually finished; never the dangling fragment.
        complete = complete_sentence_prefix(text).strip()
        if not complete:
            raise HermesIncompleteError(
                "Hermes stopped at the output token budget before finishing a sentence"
            )
        return complete

    async def respond_stream(
        self,
        conversation: str | None,
        text: str,
        instructions: str,
    ) -> AsyncIterator[str]:
        """Yield assistant text deltas from a streamed Hermes response.

        Hermes 0.19.1's ``POST /v1/responses`` accepts ``"stream": true`` and
        replies with OpenAI Responses SSE events. Text arrives as
        ``response.output_text.delta`` events; ``response.completed`` ends the
        stream and ``response.failed`` reports an agent error. Tool-call
        events are ignored. If the server answers with plain JSON instead of
        SSE, the final text is yielded once as a single delta.
        """

        if conversation is not None:
            _require_non_empty(conversation, "conversation")
        _require_non_empty(text, "text")
        _require_non_empty(instructions, "instructions")

        body = _request_body(
            self._config, conversation, text, instructions, stream=True
        )
        try:
            async with self._http_client.stream(
                "POST",
                _join_url(self._config.api_base_url, "/v1/responses"),
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                json=body,
                timeout=self._config.response_timeout_seconds,
            ) as response:
                if not response.is_success:
                    raise HermesAPIError(
                        "Hermes stream request failed with HTTP "
                        f"{response.status_code}",
                        status_code=response.status_code,
                    )
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    raw = await response.aread()
                    try:
                        payload: Any = json.loads(raw)
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise HermesAPIError(
                            "Hermes response was not valid JSON"
                        ) from exc
                    text = extract_final_output_text(payload)
                    incomplete = _is_incomplete(payload)
                    if incomplete:
                        text = complete_sentence_prefix(text)
                    if text.strip():
                        yield text
                    if incomplete:
                        raise HermesIncompleteError(
                            "Hermes stopped at the output token budget"
                        )
                    return

                completed = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        sse_event: Any = json.loads(data)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(sse_event, dict):
                        continue
                    event_type = sse_event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = sse_event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield delta
                    elif event_type == "response.completed":
                        if _is_incomplete(sse_event.get("response")):
                            raise HermesIncompleteError(
                                "Hermes stopped at the output token budget"
                            )
                        completed = True
                        break
                    elif event_type == "response.incomplete":
                        # The model hit max_output_tokens. Every delta so
                        # far is real, but the last sentence is unfinished.
                        raise HermesIncompleteError(
                            "Hermes stopped at the output token budget"
                        )
                    elif event_type == "response.failed":
                        raise HermesAPIError(
                            "Hermes stream reported a failed response"
                        )
                if not completed:
                    raise HermesAPIError(
                        "Hermes stream ended without completion"
                    )
        except httpx.TimeoutException as exc:
            raise HermesTimeoutError("Hermes stream request timed out") from exc
        except httpx.RequestError as exc:
            raise HermesAPIError("Hermes stream request failed") from exc


def _is_incomplete(payload: Any) -> bool:
    """True when a Responses payload reports a cut-short generation.

    The Responses API marks a generation that ran into ``max_output_tokens``
    with ``status: "incomplete"``. Some servers only populate
    ``incomplete_details``, so either signal counts. Anything else — an
    absent status, an unknown status — is treated as complete, so a reply
    is never discarded on a guess.
    """

    if not isinstance(payload, dict):
        return False
    if payload.get("status") == "incomplete":
        return True
    details = payload.get("incomplete_details")
    return isinstance(details, dict) and bool(details.get("reason"))


def _validate_http_base_url(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an HTTP URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an HTTP URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain credentials, query, or fragment")


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _require_non_empty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
