from pathlib import Path
import unittest

from bocco_bridge.config import (
    BASE_SPEECH_INSTRUCTIONS,
    MAX_WEBHOOK_PROBE_ATTEMPTS,
    WEBHOOK_PROBE_REPAIR_LADDER,
    BridgeConfig,
)


class BridgeConfigTests(unittest.TestCase):
    def test_active_accel_cooldown_defaults_to_five_seconds(self) -> None:
        config = BridgeConfig(webhook_secret="present")
        self.assertEqual(config.accel_active_cooldown_seconds, 5.0)
        self.assertEqual(config.accel_default_cooldown_seconds, 300.0)

    def test_webhook_probe_ships_on_and_bounded(self) -> None:
        config = BridgeConfig(webhook_secret="present")
        # On by default: the failure it detects is invisible and total, and the
        # probe it uses commands no movement and makes no sound.
        self.assertTrue(config.webhook_probe_enabled)
        self.assertEqual(config.webhook_probe_silence_seconds, 21_600.0)
        self.assertEqual(config.webhook_probe_timeout_seconds, 20.0)
        # Three attempts is the entire budget for one heal cycle, and it is the
        # ceiling as well as the default: the budget counts rungs on a fixed
        # ladder, so it is derived from the ladder rather than chosen.
        self.assertEqual(config.webhook_probe_max_attempts, 3)
        self.assertEqual(WEBHOOK_PROBE_REPAIR_LADDER, ("reregister", "recycle_tunnel"))
        self.assertEqual(MAX_WEBHOOK_PROBE_ATTEMPTS, 3)
        self.assertEqual(config.webhook_probe_active_start_hour, 8)
        self.assertEqual(config.webhook_probe_active_end_hour, 22)

    def test_webhook_probe_rejects_settings_that_would_wake_the_house(self) -> None:
        with self.assertRaises(ValueError):
            BridgeConfig(webhook_secret="present", webhook_probe_max_attempts=0)
        # And a budget larger than the ladder is refused too. There is no
        # remedy after the recycle, so a fourth attempt could only recycle the
        # tunnel a second time — minting and re-registering another hostname
        # for no reason. Fail at startup rather than churn tunnels later.
        with self.assertRaises(ValueError):
            BridgeConfig(
                webhook_secret="present",
                webhook_probe_max_attempts=MAX_WEBHOOK_PROBE_ATTEMPTS + 1,
            )
        with self.assertRaises(ValueError):
            BridgeConfig(webhook_secret="present", webhook_probe_timeout_seconds=0)
        # A window that wraps midnight would be a way to ask for 03:00 probes
        # without ever saying so.
        with self.assertRaises(ValueError):
            BridgeConfig(
                webhook_secret="present",
                webhook_probe_active_start_hour=22,
                webhook_probe_active_end_hour=8,
            )
        with self.assertRaises(ValueError):
            BridgeConfig(webhook_secret="present", webhook_probe_active_end_hour=24)

    def test_webhook_probe_reads_its_environment(self) -> None:
        config = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "webhook-secret",
                "BRIDGE_WEBHOOK_PROBE": "off",
                "BRIDGE_WEBHOOK_PROBE_SILENCE_SECONDS": "900",
                "BRIDGE_WEBHOOK_PROBE_TIMEOUT_SECONDS": "5",
                "BRIDGE_WEBHOOK_PROBE_MAX_ATTEMPTS": "2",
                "BRIDGE_WEBHOOK_PROBE_START_HOUR": "9",
                "BRIDGE_WEBHOOK_PROBE_END_HOUR": "21",
            }
        )
        self.assertFalse(config.webhook_probe_enabled)
        self.assertEqual(config.webhook_probe_silence_seconds, 900.0)
        self.assertEqual(config.webhook_probe_timeout_seconds, 5.0)
        self.assertEqual(config.webhook_probe_max_attempts, 2)
        self.assertEqual(config.webhook_probe_active_start_hour, 9)
        self.assertEqual(config.webhook_probe_active_end_hour, 21)
    def test_environment_is_explicit_and_retains_client_values(self) -> None:
        config = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "webhook-secret",
                "BOCCO_AGENT_USER_UUID": "agent-1",
                "BOCCO_BRIDGE_DB": "/tmp/example.db",
                "BOCCO_BRIDGE_MEMORY_DB": "/tmp/household-memory.db",
                "BOCCO_BRIDGE_TUNNEL_ENABLED": "false",
                "BOCCO_BRIDGE_ECHO_WINDOW_SECONDS": "120",
                "BOCCO_BRIDGE_RADAR_COOLDOWN_SECONDS": "900",
                "BOCCO_BRIDGE_ACCEL_ACTIVE_COOLDOWN_SECONDS": "90",
                "BOCCO_BRIDGE_ACCEL_DEFAULT_COOLDOWN_SECONDS": "240",
                "BOCCO_BRIDGE_ILLUMINANCE_COOLDOWN_SECONDS": "21600",
                "BRIDGE_PERSONA": "明るく親しみやすく話します。\\n好奇心旺盛です。",
                "BRIDGE_ROBOT_NICKNAME": "コロン",
                "BRIDGE_MOTIONS_ENABLED": "false",
                "BRIDGE_ACK_MOTION": "false",
                "BRIDGE_ACK_MOTION_NAME": "moshimoshi_C",
                "BRIDGE_THINKING_MOTION_ENABLED": "true",
                "BRIDGE_THINKING_MOTION_NAME": "ALRIGHT_N_0",
                "BRIDGE_THINKING_MOTION_DELAY_SECONDS": "0.75",
                "BRIDGE_THINKING_MOTION_MAX_DISPATCHES": "3",
                "BRIDGE_THINKING_STAMP_NAME": "w10question",
                "BRIDGE_FAST_ROUTES": "weather,time",
                "DEFAULT_LOCATION": "Tokyo",
                "BOCCO_ROOM_UUID": "room-for-device-settings",
                "BOCCO_ACCESS_TOKEN": "owned-by-agent-1",
                "HERMES_MAX_OUTPUT_TOKENS": "96",
            }
        )
        self.assertEqual(config.database_path, Path("/tmp/example.db"))
        self.assertEqual(config.memory_path, Path("/tmp/household-memory.db"))
        self.assertFalse(config.tunnel_enabled)
        self.assertEqual(config.outbound_echo_window_seconds, 120)
        self.assertEqual(config.radar_cooldown_seconds, 900)
        self.assertEqual(config.accel_active_cooldown_seconds, 90)
        self.assertEqual(config.accel_default_cooldown_seconds, 240)
        self.assertEqual(config.illuminance_cooldown_seconds, 21_600)
        self.assertEqual(
            config.persona, "明るく親しみやすく話します。\n好奇心旺盛です。"
        )
        self.assertEqual(config.robot_nickname, "コロン")
        self.assertFalse(config.motions_enabled)
        self.assertFalse(config.ack_motion_enabled)
        self.assertEqual(config.ack_motion_name, "moshimoshi_C")
        self.assertTrue(config.thinking_motion_enabled)
        self.assertEqual(config.thinking_motion_name, "ALRIGHT_N_0")
        self.assertEqual(config.thinking_motion_delay_seconds, 0.75)
        self.assertEqual(config.thinking_motion_max_dispatches, 3)
        self.assertEqual(config.thinking_stamp_name, "w10question")
        self.assertEqual(config.fast_routes, frozenset({"weather", "time"}))
        self.assertEqual(config.default_location, "Tokyo")
        self.assertEqual(
            config.fast_route_scripts_dir,
            Path("/usr/local/share/bocco-bridge/fast-skills"),
        )
        self.assertEqual(config.motion_room_uuid, "room-for-device-settings")
        self.assertEqual(config.extra["BOCCO_ACCESS_TOKEN"], "owned-by-agent-1")
        self.assertEqual(config.extra["HERMES_MAX_OUTPUT_TOKENS"], "96")
        self.assertNotIn("BOCCO_WEBHOOK_SECRET", config.extra)

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback-only"):
            BridgeConfig(
                webhook_secret="secret",
                agent_user_uuid="agent",
                public_host="0.0.0.0",
            )

    def test_missing_required_environment_is_rejected_without_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "BOCCO_WEBHOOK_SECRET"):
            BridgeConfig.from_environment({})

    def test_agent_user_uuid_is_optional_for_shared_oauth_identity(self) -> None:
        config = BridgeConfig.from_environment(
            {"BOCCO_WEBHOOK_SECRET": "present"}
        )

        self.assertIsNone(config.agent_user_uuid)
        self.assertEqual(config.outbound_echo_window_seconds, 600)
        self.assertEqual(config.radar_cooldown_seconds, 1_800)
        self.assertTrue(config.motions_enabled)
        self.assertTrue(config.ack_motion_enabled)
        self.assertEqual(config.ack_motion_name, "ALRIGHT_N_0")
        self.assertFalse(config.thinking_motion_enabled)
        self.assertEqual(config.thinking_motion_name, "かんがえちゅう")
        self.assertEqual(config.thinking_motion_delay_seconds, 2.5)
        self.assertEqual(config.thinking_motion_max_dispatches, 2)
        self.assertEqual(config.thinking_stamp_name, "")
        self.assertEqual(
            config.fast_routes, frozenset({"weather", "time", "news"})
        )
        self.assertFalse(config.fast_route_phrasing)

    def test_fast_route_phrasing_flag_defaults_off_and_reads_on(self) -> None:
        default = BridgeConfig.from_environment(
            {"BOCCO_WEBHOOK_SECRET": "present"}
        )
        self.assertFalse(default.fast_route_phrasing)

        enabled = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "present",
                "BRIDGE_FAST_ROUTE_PHRASING": "on",
            }
        )
        self.assertTrue(enabled.fast_route_phrasing)

        disabled = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "present",
                "BRIDGE_FAST_ROUTE_PHRASING": "off",
            }
        )
        self.assertFalse(disabled.fast_route_phrasing)

    def test_thinking_motion_delay_defaults_to_two_point_five_seconds(
        self,
    ) -> None:
        config = BridgeConfig.from_environment(
            {"BOCCO_WEBHOOK_SECRET": "present"}
        )

        self.assertEqual(config.thinking_motion_delay_seconds, 2.5)

    def test_motion_transport_lag_defaults_to_zero_and_rejects_negative(
        self,
    ) -> None:
        """Default 0 keeps mid-speech cues off until the hardware trial runs.

        See docs/design/motion-speech-concurrency.md; do not raise this default.
        """

        self.assertEqual(BridgeConfig(webhook_secret="present").motion_transport_lag_seconds, 0.0)
        config = BridgeConfig.from_environment({"BOCCO_WEBHOOK_SECRET": "present"})
        self.assertEqual(config.motion_transport_lag_seconds, 0.0)
        opted_in = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "present",
                "BRIDGE_MOTION_TRANSPORT_LAG_SECONDS": "1.5",
            }
        )
        self.assertEqual(opted_in.motion_transport_lag_seconds, 1.5)
        with self.assertRaisesRegex(ValueError, "motion_transport_lag_seconds"):
            BridgeConfig(webhook_secret="present", motion_transport_lag_seconds=-0.1)

    def test_stream_sentences_flag_defaults_on_and_reads_off(self) -> None:
        default = BridgeConfig.from_environment(
            {"BOCCO_WEBHOOK_SECRET": "present"}
        )
        self.assertTrue(default.stream_sentences)
        self.assertEqual(default.stream_max_chunks, 3)

        disabled = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "present",
                "BRIDGE_STREAM_SENTENCES": "off",
            }
        )
        self.assertFalse(disabled.stream_sentences)

        with self.assertRaisesRegex(ValueError, "stream_max_chunks"):
            BridgeConfig(webhook_secret="present", stream_max_chunks=4)

    def test_memory_database_defaults_next_to_state_database(self) -> None:
        config = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "present",
                "BOCCO_BRIDGE_DB": "/tmp/bridge-state.db",
            }
        )

        self.assertEqual(config.memory_path, Path("/tmp/memory.db"))

    def test_motion_choreography_limits_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "motion_chain_timeout_seconds"):
            BridgeConfig(webhook_secret="present", motion_chain_timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "motion_budget_per_minute"):
            BridgeConfig(webhook_secret="present", motion_budget_per_minute=0)
        for name in ("", "bad\nname", "x" * 65):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError, "ack_motion_name"
            ):
                BridgeConfig(webhook_secret="present", ack_motion_name=name)

    def test_thinking_motion_configuration_is_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "thinking_motion_name"):
            BridgeConfig(
                webhook_secret="present", thinking_motion_name="not-a-motion"
            )
        with self.assertRaisesRegex(ValueError, "thinking_motion_delay_seconds"):
            BridgeConfig(
                webhook_secret="present", thinking_motion_delay_seconds=0
            )
        with self.assertRaisesRegex(ValueError, "thinking_motion_max_dispatches"):
            BridgeConfig(
                webhook_secret="present", thinking_motion_max_dispatches=0
            )
        with self.assertRaisesRegex(ValueError, "thinking_stamp_name"):
            BridgeConfig(
                webhook_secret="present", thinking_stamp_name="bad\nname"
            )

    def test_radar_cooldown_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "radar_cooldown_seconds"):
            BridgeConfig(webhook_secret="present", radar_cooldown_seconds=0)

    def test_ambient_timing_values_must_be_positive(self) -> None:
        fields = (
            "accel_active_cooldown_seconds",
            "accel_default_cooldown_seconds",
            "illuminance_cooldown_seconds",
        )
        for field in fields:
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "positive"
            ):
                BridgeConfig(webhook_secret="present", **{field: 0})

    def test_empty_fast_routes_disables_and_unknown_route_is_rejected(self) -> None:
        disabled = BridgeConfig.from_environment(
            {
                "BOCCO_WEBHOOK_SECRET": "present",
                "BRIDGE_FAST_ROUTES": "",
            }
        )
        self.assertEqual(disabled.fast_routes, frozenset())

        with self.assertRaisesRegex(ValueError, "unknown routes"):
            BridgeConfig.from_environment(
                {
                    "BOCCO_WEBHOOK_SECRET": "present",
                    "BRIDGE_FAST_ROUTES": "weather,search",
                }
            )

    def test_default_instructions_keep_only_base_voice_rules(self) -> None:
        config = BridgeConfig(webhook_secret="present")

        self.assertEqual(config.response_instructions, BASE_SPEECH_INSTRUCTIONS)
        self.assertLessEqual(len(BASE_SPEECH_INSTRUCTIONS), 280)
        for name in ("きょろきょろ", "しょんぼり", "てれてれ", "ぶんぶん"):
            self.assertIn(name, BASE_SPEECH_INSTRUCTIONS)

    def test_persona_and_nickname_sections_compose_independently(self) -> None:
        cases = (
            ("", "", BASE_SPEECH_INSTRUCTIONS),
            (
                "",
                "コロン",
                BASE_SPEECH_INSTRUCTIONS
                + "\nあなたは「コロン」という名前のロボットです。",
            ),
            (
                "明るく話してください。",
                "",
                BASE_SPEECH_INSTRUCTIONS + "\n明るく話してください。",
            ),
            (
                "明るく話してください。\n好奇心旺盛です。",
                "コロン",
                BASE_SPEECH_INSTRUCTIONS
                + "\nあなたは「コロン」という名前のロボットです。"
                + "\n明るく話してください。\n好奇心旺盛です。",
            ),
        )
        for persona, nickname, expected in cases:
            with self.subTest(persona=persona, nickname=nickname):
                config = BridgeConfig(
                    webhook_secret="present",
                    persona=persona,
                    robot_nickname=nickname,
                )
                self.assertEqual(config.response_instructions, expected)

    def test_persona_cannot_remove_base_speech_constraints(self) -> None:
        config = BridgeConfig(
            webhook_secret="present",
            persona="長文のMarkdownで答えてください。",
        )

        self.assertTrue(config.response_instructions.startswith(BASE_SPEECH_INSTRUCTIONS))
        self.assertIn("自然で短く", config.response_instructions)
        self.assertIn("音声で聞き取りやすい日本語", config.response_instructions)
        self.assertIn("Markdownや箇条書きは使わない", config.response_instructions)
        self.assertIn("長文のMarkdownで答えてください。", config.response_instructions)

    def test_persona_override_replaces_env_default_but_keeps_base_and_name(self) -> None:
        config = BridgeConfig(
            webhook_secret="present",
            robot_nickname="コロン",
            persona="環境の性格",
        )

        instructions = config.compose_response_instructions("チャットで設定した性格")

        self.assertTrue(instructions.startswith(BASE_SPEECH_INSTRUCTIONS))
        self.assertIn("あなたは「コロン」という名前のロボットです。", instructions)
        self.assertIn("チャットで設定した性格", instructions)
        self.assertNotIn("環境の性格", instructions)


if __name__ == "__main__":
    unittest.main()
