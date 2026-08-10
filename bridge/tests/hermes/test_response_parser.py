from __future__ import annotations

import unittest

from bocco_bridge.hermes.response_parser import (
    HermesResponseError,
    extract_final_output_text,
)


class ResponseParserTests(unittest.TestCase):
    def test_ignores_tool_records_and_returns_assistant_output_text(self) -> None:
        payload = {
            "output": [
                {"type": "function_call", "name": "terminal"},
                {"type": "function_call_output", "output": "secret tool output"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "output_text", "text": "ignore me"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "おは"},
                        {"type": "annotation", "text": "ignore me too"},
                        {"type": "output_text", "text": "よう。"},
                    ],
                },
            ]
        }

        self.assertEqual(extract_final_output_text(payload), "おはよう。")

    def test_final_assistant_message_wins(self) -> None:
        payload = {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "draft"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "final"}],
                },
            ]
        }

        self.assertEqual(extract_final_output_text(payload), "final")

    def test_rejects_missing_or_empty_final_text(self) -> None:
        fixtures = [
            {},
            {"output": []},
            {"output": [{"type": "function_call", "name": "terminal"}]},
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "   "}],
                    }
                ]
            },
        ]

        for payload in fixtures:
            with self.subTest(payload=payload):
                with self.assertRaises(HermesResponseError):
                    extract_final_output_text(payload)
