"""Hermes-facing adapter for the BOCCO bridge."""

from .client import (
    BOCCO_SPEECH_INSTRUCTIONS,
    HermesAPIError,
    HermesClient,
    HermesConfig,
    HermesError,
    HermesIncompleteError,
    HermesTimeoutError,
    bocco_conversation,
)
from .response_parser import HermesResponseError, extract_final_output_text

__all__ = [
    "BOCCO_SPEECH_INSTRUCTIONS",
    "HermesAPIError",
    "HermesClient",
    "HermesConfig",
    "HermesError",
    "HermesIncompleteError",
    "HermesResponseError",
    "HermesTimeoutError",
    "bocco_conversation",
    "extract_final_output_text",
]
