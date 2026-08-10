"""Safe extraction of the final assistant text from a Hermes response."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class HermesResponseError(ValueError):
    """Raised when Hermes returns no usable final assistant text."""


def extract_final_output_text(payload: Mapping[str, Any]) -> str:
    """Return the last assistant ``output_text`` from a Responses payload.

    Tool calls and tool results are deliberately ignored. If a response has
    more than one assistant message, the final message wins. Multiple
    ``output_text`` parts within that message are concatenated in order.
    """

    if not isinstance(payload, Mapping):
        raise HermesResponseError("Hermes response must be a JSON object")

    output = payload.get("output")
    if not isinstance(output, list):
        raise HermesResponseError("Hermes response has no output list")

    final_text: str | None = None
    for item in output:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue

        content = item.get("content")
        if not isinstance(content, list):
            continue

        parts: list[str] = []
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)

        candidate = "".join(parts).strip()
        if candidate:
            final_text = candidate

    if final_text is None:
        raise HermesResponseError("Hermes response has no final assistant output_text")
    return final_text
