#!/bin/sh
# Verify that both loopback services answer their health endpoints.
set -eu

if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl is required but not installed" >&2
  exit 1
fi

probe() {
  name=$1
  url=$2
  unit=$3
  if ! curl --fail --silent --show-error --max-time 5 "$url" >/dev/null; then
    echo "error: $name did not answer $url" >&2
    echo "  systemctl status $unit" >&2
    echo "  journalctl -u $unit -n 50 --no-pager" >&2
    return 1
  fi
}

status=0
probe "bocco-bridge" "http://127.0.0.1:8787/readyz" "bocco-bridge.service" || status=1
probe "Hermes API" "http://127.0.0.1:8642/health" "hermes-api.service" || status=1
if [ "$status" -ne 0 ]; then
  exit 1
fi
echo "bocco-bridge and Hermes API are ready"
