#!/usr/bin/env python3
"""Render secret-bearing cloud-init files from the repository's root .env."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sys
import tempfile


SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_DIR = SCRIPT_DIR.parent
REPO_ROOT = IMAGE_DIR.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"
USER_DATA_TEMPLATE = IMAGE_DIR / "cloud-init" / "user-data.template.yaml"
USER_DATA_OUTPUT = IMAGE_DIR / "cloud-init" / "user-data"
NETWORK_CONFIG_TEMPLATE = IMAGE_DIR / "cloud-init" / "network-config.template.yaml"
NETWORK_CONFIG_OUTPUT = IMAGE_DIR / "cloud-init" / "network-config"

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,63}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"
)
PLACEHOLDER_MARKERS = ("CHANGE_ME", "YOUR_", "REPLACE_ME")
PROVISIONING_KEYS = {
    "PI_HOSTNAME",
    "PI_PASSWORD",
    "WIFI_SSID",
    "WIFI_PASSWORD",
}


class ConfigError(ValueError):
    """Raised when the local configuration cannot safely be rendered."""


def parse_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigError(
            f"Missing {path}. Copy .env.example to .env and fill in the values."
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{line_number}: expected KEY=value")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ConfigError(f"{path}:{line_number}: invalid variable name {key!r}")
        if key in values:
            raise ConfigError(f"{path}:{line_number}: duplicate variable {key}")

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            if value[0] == '"':
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ConfigError(
                        f"{path}:{line_number}: invalid double-quoted value for {key}"
                    ) from exc
            else:
                value = value[1:-1]

        if "\n" in value or "\r" in value:
            raise ConfigError(f"{path}:{line_number}: {key} cannot contain a newline")
        values[key] = value

    return values


def looks_unset(value: str) -> bool:
    upper = value.upper()
    return not value or any(marker in upper for marker in PLACEHOLDER_MARKERS)


def validate(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []

    for key in ("PI_HOSTNAME", "PI_PASSWORD", "OPENAI_API_KEY", "BOCCO_REFRESH_TOKEN"):
        if looks_unset(values.get(key, "")):
            errors.append(f"{key} must be set to a real value")

    hostname = values.get("PI_HOSTNAME", "")
    if hostname and not HOSTNAME_PATTERN.fullmatch(hostname):
        errors.append(
            "PI_HOSTNAME must be 1-63 lowercase letters, numbers, or hyphens"
        )

    password = values.get("PI_PASSWORD", "")
    if password and not looks_unset(password) and len(password) < 12:
        errors.append("PI_PASSWORD must be at least 12 characters")

    wifi_ssid = values.get("WIFI_SSID", "")
    wifi_password = values.get("WIFI_PASSWORD", "")
    if bool(wifi_ssid) != bool(wifi_password):
        errors.append("WIFI_SSID and WIFI_PASSWORD must both be set or both be blank")
    elif not wifi_ssid:
        warnings.append("Wi-Fi is blank; the image will require Ethernet on first boot")

    bridge_port = values.get("BRIDGE_PORT", "8787")
    try:
        port = int(bridge_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        errors.append("BRIDGE_PORT must be an integer from 1 to 65535")

    if not values.get("DISCORD_BOT_TOKEN", ""):
        warnings.append("DISCORD_BOT_TOKEN is blank; Discord integration will be disabled")
    if not values.get("AGENT_WEBHOOK_TOKEN", ""):
        warnings.append(
            "AGENT_WEBHOOK_TOKEN is blank; set one before exposing an agent webhook"
        )

    if errors:
        raise ConfigError("Configuration errors:\n- " + "\n- ".join(errors))
    return warnings


def keyfile_escape(value: str) -> str:
    """Escape NetworkManager keyfile separators and backslashes."""
    return value.replace("\\", "\\\\").replace(";", "\\;")


def build_wifi_write_file(values: dict[str, str]) -> str:
    ssid = values.get("WIFI_SSID", "")
    if not ssid:
        return ""

    connection = f"""[connection]
id=wifi
type=wifi
interface-name=wlan0
autoconnect=true

[wifi]
mode=infrastructure
ssid={keyfile_escape(ssid)}

[wifi-security]
key-mgmt=wpa-psk
psk={keyfile_escape(values['WIFI_PASSWORD'])}

[ipv4]
method=auto

[ipv6]
method=auto
"""
    encoded = base64.b64encode(connection.encode("utf-8")).decode("ascii")
    return (
        "  - path: /etc/NetworkManager/system-connections/wifi.nmconnection\n"
        "    owner: root:root\n"
        "    permissions: '0600'\n"
        "    encoding: b64\n"
        f"    content: {encoded}"
    )


def build_runtime_env(values: dict[str, str]) -> str:
    lines: list[str] = []
    for key, value in values.items():
        if key not in PROVISIONING_KEYS:
            # JSON string quoting is compatible with common dotenv parsers and
            # prevents spaces or '#' characters from changing the value.
            lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def render_user_data(values: dict[str, str]) -> str:
    template = USER_DATA_TEMPLATE.read_text(encoding="utf-8")
    runtime_env = build_runtime_env(values)
    replacements = {
        "__PI_HOSTNAME_YAML__": json.dumps(values["PI_HOSTNAME"]),
        "__PI_PASSWORD_YAML__": json.dumps(values["PI_PASSWORD"]),
        "__WIFI_WRITE_FILE__": build_wifi_write_file(values),
        "__RUNTIME_ENV_B64__": base64.b64encode(
            runtime_env.encode("utf-8")
        ).decode("ascii"),
    }
    rendered = template
    for marker, replacement in replacements.items():
        rendered = rendered.replace(marker, replacement)
    if re.search(r"__[A-Z0-9_]+__", rendered):
        raise ConfigError("The user-data template contains an unresolved placeholder")
    return rendered


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary_path = Path(handle.name)
        # Set the mode on the open descriptor rather than by path after the
        # fact.  fchmod cannot be redirected to a different file, whereas a
        # path-based chmod acts on whatever the name resolves to at that
        # moment.  (The temp file is not world-readable in the interim either
        # way: NamedTemporaryFile opens via mkstemp, which passes 0o600
        # explicitly and so ignores the umask.)
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
    temporary_path.replace(path)


def render_files(values: dict[str, str]) -> None:
    atomic_write(USER_DATA_OUTPUT, render_user_data(values))
    atomic_write(
        NETWORK_CONFIG_OUTPUT,
        NETWORK_CONFIG_TEMPLATE.read_text(encoding="utf-8"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"dotenv input (default: {DEFAULT_ENV_PATH})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate only; do not generate cloud-init files",
    )
    args = parser.parse_args()

    try:
        # Secure the file even when later validation fails because placeholders
        # are still present or a value is malformed.
        if not args.env_file.is_file():
            raise ConfigError(
                f"Missing {args.env_file}. Copy .env.example to .env and fill in the values."
            )
        os.chmod(args.env_file, 0o600)
        values = parse_dotenv(args.env_file)
        warnings = validate(values)
    except (ConfigError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for warning in warnings:
        print(f"WARNING: {warning}")

    if args.check:
        print(f"Configuration is valid: {args.env_file}")
        return 0

    render_files(values)
    print("Generated secret-bearing cloud-init files (mode 0600):")
    print(f"- {USER_DATA_OUTPUT}")
    print(f"- {NETWORK_CONFIG_OUTPUT}")
    print("No secret values were printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
