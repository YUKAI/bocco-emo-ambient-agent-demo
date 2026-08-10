from __future__ import annotations

import json
import unittest

from _fake_server import CapturedRequest, FakeResponse, fake_server
from bocco_bridge.hermes import (
    BOCCO_SPEECH_INSTRUCTIONS,
    HermesAPIError,
    HermesClient,
    HermesConfig,
    HermesIncompleteError,
    HermesResponseError,
    HermesTimeoutError,
    bocco_conversation,
)


def incomplete_response(text: str) -> dict[str, object]:
    payload = completed_response(text)
    payload["status"] = "incomplete"
    payload["incomplete_details"] = {"reason": "max_output_tokens"}
    return payload


def completed_response(text: str) -> dict[str, object]:
    return {
        "id": "resp_test",
        "status": "completed",
        "output": [
            {"type": "function_call", "name": "memory"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        ],
    }


class HermesClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_respond_uses_auth_and_named_bocco_conversation(self) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            self.assertEqual(request.path, "/v1/responses")
            self.assertEqual(request.headers["Authorization"], "Bearer api-secret")
            payload = request.json()
            self.assertEqual(payload["conversation"], "bocco-room:room-123")
            self.assertEqual(payload["input"], "今日はどう？")
            self.assertEqual(payload["instructions"], BOCCO_SPEECH_INSTRUCTIONS)
            self.assertEqual(payload["model"], "hermes-agent")
            self.assertIs(payload["store"], True)
            self.assertEqual(payload["max_output_tokens"], 128)
            return FakeResponse.json(completed_response("元気だよ。"))

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(
                    api_key="api-secret",
                    api_base_url=base_url,
                )
            ) as client:
                result = await client.respond(
                    bocco_conversation("room-123"),
                    "今日はどう？",
                    BOCCO_SPEECH_INSTRUCTIONS,
                )

        self.assertEqual(result, "元気だよ。")

    async def test_stateless_respond_sends_no_conversation_and_stores_nothing(
        self,
    ) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            payload = request.json()
            self.assertNotIn("conversation", payload)
            self.assertIs(payload["store"], False)
            self.assertEqual(payload["input"], "今日はどう？")
            return FakeResponse.json(completed_response("元気だよ。"))

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                result = await client.respond(
                    None, "今日はどう？", BOCCO_SPEECH_INSTRUCTIONS
                )

        self.assertEqual(result, "元気だよ。")

    async def test_auth_failure_is_safe_and_preserves_status(self) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json(
                {"error": "server-body-must-not-leak"}, status=401
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(
                    api_key="api-secret",
                    api_base_url=base_url,
                )
            ) as client:
                with self.assertRaises(HermesAPIError) as caught:
                    await client.respond("conversation", "hello", "short reply")

        self.assertEqual(caught.exception.status_code, 401)
        self.assertNotIn("server-body-must-not-leak", str(caught.exception))
        self.assertNotIn("api-secret", str(caught.exception))

    async def test_rejects_malformed_output(self) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json({"status": "completed", "output": []})

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(
                    api_key="api-secret",
                    api_base_url=base_url,
                )
            ) as client:
                with self.assertRaises(HermesResponseError):
                    await client.respond("conversation", "hello", "short reply")

    async def test_incomplete_response_drops_the_unfinished_sentence(
        self,
    ) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json(
                incomplete_response("元気だよ。今日の予定は朝からずっと")
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                result = await client.respond(
                    "conversation", "hello", "short reply"
                )

        self.assertEqual(result, "元気だよ。")

    async def test_incomplete_response_without_any_sentence_is_rejected(
        self,
    ) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json(incomplete_response("今日の予定は朝から"))

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                with self.assertRaises(HermesIncompleteError):
                    await client.respond("conversation", "hello", "short reply")

    async def test_completed_response_without_punctuation_is_kept(self) -> None:
        # A short reply that simply lacks 。 is complete, not truncated.
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json(completed_response("うん"))

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                result = await client.respond(
                    "conversation", "hello", "short reply"
                )

        self.assertEqual(result, "うん")

    async def test_response_timeout_has_distinct_error(self) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json(
                completed_response("late"), delay_seconds=0.1
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(
                    api_key="api-secret",
                    api_base_url=base_url,
                    response_timeout_seconds=0.01,
                )
            ) as client:
                with self.assertRaises(HermesTimeoutError):
                    await client.respond("conversation", "hello", "short reply")


def sse_body(*events: dict[str, object]) -> bytes:
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
    return "".join(lines).encode("utf-8")


class HermesStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stateless_stream_sends_no_conversation_and_stores_nothing(
        self,
    ) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            payload = request.json()
            self.assertNotIn("conversation", payload)
            self.assertIs(payload["store"], False)
            self.assertIs(payload["stream"], True)
            return FakeResponse(
                body=sse_body(
                    {"type": "response.output_text.delta", "delta": "元気だよ。"},
                    {"type": "response.completed", "response": {}},
                ),
                content_type="text/event-stream",
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                deltas = [
                    delta
                    async for delta in client.respond_stream(
                        None, "今日はどう？", BOCCO_SPEECH_INSTRUCTIONS
                    )
                ]

        self.assertEqual(deltas, ["元気だよ。"])

    async def test_respond_stream_yields_deltas_until_completed(self) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            self.assertEqual(request.path, "/v1/responses")
            self.assertEqual(request.headers["Authorization"], "Bearer api-secret")
            payload = request.json()
            self.assertIs(payload["stream"], True)
            self.assertIs(payload["store"], True)
            self.assertEqual(payload["conversation"], "bocco-room:room-123")
            return FakeResponse(
                body=sse_body(
                    {"type": "response.created", "response": {}},
                    {"type": "response.output_text.delta", "delta": "こんにちは。"},
                    {"type": "response.output_text.delta", "delta": "元気"},
                    {"type": "response.output_text.delta", "delta": "だよ。"},
                    {"type": "response.output_text.done", "text": "こんにちは。元気だよ。"},
                    {"type": "response.completed", "response": {}},
                ),
                content_type="text/event-stream",
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                deltas = [
                    delta
                    async for delta in client.respond_stream(
                        bocco_conversation("room-123"),
                        "今日はどう？",
                        BOCCO_SPEECH_INSTRUCTIONS,
                    )
                ]

        self.assertEqual(deltas, ["こんにちは。", "元気", "だよ。"])

    async def test_respond_stream_failed_event_raises_after_partial_text(
        self,
    ) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse(
                body=sse_body(
                    {"type": "response.output_text.delta", "delta": "途中まで。"},
                    {"type": "response.failed", "response": {"error": {}}},
                ),
                content_type="text/event-stream",
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                deltas: list[str] = []
                with self.assertRaises(HermesAPIError):
                    async for delta in client.respond_stream(
                        "conversation", "hello", "short reply"
                    ):
                        deltas.append(delta)

        self.assertEqual(deltas, ["途中まで。"])

    async def test_respond_stream_truncated_stream_raises(self) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse(
                body=sse_body(
                    {"type": "response.output_text.delta", "delta": "きれた"},
                ),
                content_type="text/event-stream",
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                with self.assertRaisesRegex(HermesAPIError, "without completion"):
                    async for _ in client.respond_stream(
                        "conversation", "hello", "short reply"
                    ):
                        pass

    async def test_respond_stream_incomplete_event_raises_after_deltas(
        self,
    ) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse(
                body=sse_body(
                    {"type": "response.output_text.delta", "delta": "はい。"},
                    {"type": "response.output_text.delta", "delta": "続きは途中"},
                    {
                        "type": "response.incomplete",
                        "response": {
                            "status": "incomplete",
                            "incomplete_details": {
                                "reason": "max_output_tokens"
                            },
                        },
                    },
                ),
                content_type="text/event-stream",
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                deltas: list[str] = []
                with self.assertRaises(HermesIncompleteError):
                    async for delta in client.respond_stream(
                        "conversation", "hello", "short reply"
                    ):
                        deltas.append(delta)

        # Every delta is real text; the caller decides what is safe to speak.
        self.assertEqual(deltas, ["はい。", "続きは途中"])

    async def test_respond_stream_completed_with_incomplete_status_raises(
        self,
    ) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse(
                body=sse_body(
                    {"type": "response.output_text.delta", "delta": "はい。"},
                    {
                        "type": "response.completed",
                        "response": {"status": "incomplete"},
                    },
                ),
                content_type="text/event-stream",
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                with self.assertRaises(HermesIncompleteError):
                    async for _ in client.respond_stream(
                        "conversation", "hello", "short reply"
                    ):
                        pass

    async def test_respond_stream_non_sse_incomplete_yields_then_raises(
        self,
    ) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json(incomplete_response("はい。まだ途中"))

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                deltas: list[str] = []
                with self.assertRaises(HermesIncompleteError):
                    async for delta in client.respond_stream(
                        "conversation", "hello", "short reply"
                    ):
                        deltas.append(delta)

        self.assertEqual(deltas, ["はい。"])

    async def test_respond_stream_falls_back_to_plain_json_response(self) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json(completed_response("一括の返事です。"))

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                deltas = [
                    delta
                    async for delta in client.respond_stream(
                        "conversation", "hello", "short reply"
                    )
                ]

        self.assertEqual(deltas, ["一括の返事です。"])

    async def test_respond_stream_http_error_is_safe(self) -> None:
        def responder(request: CapturedRequest) -> FakeResponse:
            return FakeResponse.json(
                {"error": "server-body-must-not-leak"}, status=503
            )

        with fake_server(responder) as (base_url, _):
            async with HermesClient(
                HermesConfig(api_key="api-secret", api_base_url=base_url)
            ) as client:
                with self.assertRaises(HermesAPIError) as caught:
                    async for _ in client.respond_stream(
                        "conversation", "hello", "short reply"
                    ):
                        pass

        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("server-body-must-not-leak", str(caught.exception))


class HermesConfigTests(unittest.TestCase):
    def test_conversation_helper_rejects_empty_or_unsafe_room_ids(self) -> None:
        for value in ("", "   ", "room id", "room\nheader"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    bocco_conversation(value)

    def test_config_rejects_secrets_in_urls(self) -> None:
        with self.assertRaises(ValueError):
            HermesConfig(
                api_key="api-secret",
                api_base_url="http://api-secret@127.0.0.1:8642",
            )

    def test_config_requires_positive_output_cap(self) -> None:
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "max_output_tokens"
            ):
                HermesConfig(api_key="api-secret", max_output_tokens=value)
