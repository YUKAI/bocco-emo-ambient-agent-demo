#!/bin/sh
# Run the whole bridge suite against fakes only: no real tokens, no network.
set -eu

repository_root=${1:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required but not installed" >&2
  exit 1
fi
if [ ! -d "$repository_root/bridge/tests" ]; then
  echo "error: $repository_root/bridge/tests not found" >&2
  echo "  run this from a checkout, or pass the repository root as the first argument" >&2
  exit 1
fi

cd "$repository_root"

for test_directory in bocco hermes runtime integration; do
  PYTHONPATH=bridge/src:bridge/tests \
    python3 -m unittest discover -s "bridge/tests/$test_directory" -p 'test_*.py' -v
done
