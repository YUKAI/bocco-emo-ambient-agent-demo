#!/bin/sh
# Install the bridge and Hermes units on the Pi. Run as root from a checkout.
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "error: this installer must run as root" >&2
  echo "  sudo $0 ${1:-}" >&2
  exit 1
fi

repository_root=${1:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}

for required in \
  bridge/pyproject.toml \
  systemd/bocco-bridge.service \
  systemd/hermes-api.service \
  systemd/bocco-bridge.env.example \
  systemd/hermes.env.example \
  hermes/config.example.yaml \
  hermes/fast-routes/weather.py \
  hermes/fast-routes/time_info.py \
  hermes/fast-routes/news.py \
  docs/design/bridge-runtime.md
do
  if [ ! -f "$repository_root/$required" ]; then
    echo "error: $required not found under $repository_root" >&2
    echo "  pass the repository root as the first argument, e.g. sudo $0 \"\$PWD\"" >&2
    exit 1
  fi
done

for tool in python3 systemctl useradd groupadd getent; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: $tool is required but not installed" >&2
    exit 1
  fi
done

# This script installs and enables hermes-api.service, whose ExecStart is
# /opt/hermes-agent/.venv/bin/python. If that interpreter does not exist,
# systemd reports status=203/EXEC and restarts every three seconds forever —
# a symptom that looks like a crashing service rather than a missing install.
# Refuse to set that up. install-hermes.sh is the step that has been skipped.
if [ ! -x /opt/hermes-agent/.venv/bin/python ]; then
  cat >&2 <<'MISSING'
error: Hermes Agent is not installed.

  /opt/hermes-agent/.venv/bin/python does not exist, and it is what
  hermes-api.service execs. Install Hermes first, then re-run this script:

      sudo scripts/install-hermes.sh

  See docs/design/replication-guide.md for the full sequence.
MISSING
  exit 1
fi

if ! getent group bocco-bridge >/dev/null 2>&1; then
  groupadd --system bocco-bridge
fi
if ! id bocco-bridge >/dev/null 2>&1; then
  useradd --system --gid bocco-bridge --home-dir /var/lib/bocco-bridge \
    --shell /usr/sbin/nologin bocco-bridge
fi
if ! getent group hermes-agent >/dev/null 2>&1; then
  groupadd --system hermes-agent
fi
if ! id hermes-agent >/dev/null 2>&1; then
  useradd --system --gid hermes-agent --home-dir /var/lib/hermes-agent \
    --shell /usr/sbin/nologin hermes-agent
fi

install -d -o root -g root -m 0755 /opt/bocco-bridge
install -d -o root -g root -m 0755 /opt/bocco-bridge/docs/design
install -d -o root -g root -m 0755 /opt/hermes-agent/fast-routes
install -d -o root -g root -m 0755 /usr/local/share/bocco-bridge
install -d -o root -g root -m 0755 /usr/local/share/bocco-bridge/fast-skills
install -d -o bocco-bridge -g bocco-bridge -m 0700 /var/lib/bocco-bridge
install -d -o hermes-agent -g hermes-agent -m 0700 /var/lib/hermes-agent
install -d -o hermes-agent -g hermes-agent -m 0700 /var/lib/hermes-agent/.hermes
install -d -o root -g bocco-bridge -m 0750 /etc/bocco-bridge
install -d -o root -g hermes-agent -m 0750 /etc/hermes-agent

python3 -m venv /opt/bocco-bridge/.venv
/opt/bocco-bridge/.venv/bin/pip install --disable-pip-version-check "$repository_root/bridge"
install -m 0644 "$repository_root/docs/design/bridge-runtime.md" \
  /opt/bocco-bridge/docs/design/bridge-runtime.md
for skill_script in weather.py time_info.py news.py; do
  install -o root -g root -m 0555 \
    "$repository_root/hermes/fast-routes/$skill_script" \
    "/opt/hermes-agent/fast-routes/$skill_script"
  install -o root -g root -m 0644 \
    "$repository_root/hermes/fast-routes/$skill_script" \
    "/usr/local/share/bocco-bridge/fast-skills/$skill_script"
done
if [ ! -e /var/lib/hermes-agent/.hermes/config.yaml ]; then
  install -o hermes-agent -g hermes-agent -m 0600 \
    "$repository_root/hermes/config.example.yaml" \
    /var/lib/hermes-agent/.hermes/config.yaml
fi

install -m 0644 "$repository_root/systemd/bocco-bridge.service" \
  /etc/systemd/system/bocco-bridge.service
install -m 0644 "$repository_root/systemd/hermes-api.service" \
  /etc/systemd/system/hermes-api.service

if [ ! -e /etc/bocco-bridge/bridge.env ]; then
  install -o root -g bocco-bridge -m 0640 \
    "$repository_root/systemd/bocco-bridge.env.example" /etc/bocco-bridge/bridge.env
fi
if [ ! -e /etc/hermes-agent/hermes.env ]; then
  install -o root -g hermes-agent -m 0640 \
    "$repository_root/systemd/hermes.env.example" /etc/hermes-agent/hermes.env
fi

systemctl daemon-reload
if [ ! -x /opt/hermes-agent/.venv/bin/python ]; then
  echo "warning: /opt/hermes-agent/.venv/bin/python is missing;" \
    "hermes-api.service will fail to start until the Hermes virtualenv is installed" >&2
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "warning: install cloudflared before starting bocco-bridge.service" >&2
fi
echo "Edit /etc/bocco-bridge/bridge.env and /etc/hermes-agent/hermes.env before enabling services."
