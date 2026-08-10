from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "render_config.py"
SPEC = importlib.util.spec_from_file_location("render_config", MODULE_PATH)
assert SPEC and SPEC.loader
render_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_config)


VALID_VALUES = {
    "PI_HOSTNAME": "ambient-pi",
    "PI_PASSWORD": "a-long-unique-password",
    "WIFI_SSID": "Example Lab",
    "WIFI_PASSWORD": "wifi#pass;with=characters",
    "OPENAI_API_KEY": "test-openai-key",
    "BOCCO_REFRESH_TOKEN": "refresh=test#token",
    "BOCCO_ACCESS_TOKEN": "",
    "BRIDGE_PORT": "8787",
    "DISCORD_BOT_TOKEN": "discord-test-token",
    "AGENT_WEBHOOK_TOKEN": "agent-test-token",
}


class RenderConfigTests(unittest.TestCase):
    def test_parse_dotenv_preserves_special_characters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                'TOKEN="value with spaces # and = signs"\nRAW=dollar$and#hash\n',
                encoding="utf-8",
            )
            parsed = render_config.parse_dotenv(env_path)

        self.assertEqual(parsed["TOKEN"], "value with spaces # and = signs")
        self.assertEqual(parsed["RAW"], "dollar$and#hash")

    def test_validate_rejects_placeholders(self) -> None:
        values = dict(VALID_VALUES)
        values["PI_PASSWORD"] = "CHANGE_ME"
        with self.assertRaises(render_config.ConfigError):
            render_config.validate(values)

    def test_render_keeps_runtime_secrets_out_of_plain_yaml(self) -> None:
        rendered = render_config.render_user_data(dict(VALID_VALUES))
        self.assertNotIn(VALID_VALUES["OPENAI_API_KEY"], rendered)
        self.assertNotIn(VALID_VALUES["BOCCO_REFRESH_TOKEN"], rendered)

        # The first Base64 content is Wi-Fi; the runtime content is the final
        # one, so split from the right.
        encoded = rendered.rsplit("content: ", 1)[1].splitlines()[0]
        runtime = base64.b64decode(encoded).decode("utf-8")
        self.assertIn('OPENAI_API_KEY="test-openai-key"', runtime)
        self.assertIn('BOCCO_REFRESH_TOKEN="refresh=test#token"', runtime)
        self.assertNotIn("WIFI_PASSWORD", runtime)
        self.assertNotIn("PI_PASSWORD", runtime)

    def test_ethernet_only_is_supported(self) -> None:
        values = dict(VALID_VALUES)
        values["WIFI_SSID"] = ""
        values["WIFI_PASSWORD"] = ""
        warnings = render_config.validate(values)
        rendered = render_config.render_user_data(values)
        self.assertTrue(any("Ethernet" in warning for warning in warnings))
        self.assertNotIn("wifi.nmconnection", rendered)


if __name__ == "__main__":
    unittest.main()
