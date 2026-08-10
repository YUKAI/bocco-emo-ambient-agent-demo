#!/bin/sh
# Install the local embedding service that backs semantic conversational
# memory. Separate from install-services.sh on purpose: the bridge and Hermes
# are the product, this is an optional accelerator for one feature that is off
# by default, and a Pi that never enables it should never build llama.cpp.
#
# Idempotent. Re-running rebuilds nothing that is already present and never
# overwrites /etc/bocco-embeddings/embeddings.env.
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

repository_root=${1:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}
# Check every input up front. The build below takes minutes on a Pi and the
# model download is 132 MB; discovering a missing file after both have
# succeeded costs a quarter of an hour and teaches nothing.
for required in \
  "$repository_root/systemd/bocco-embeddings.service" \
  "$repository_root/systemd/bocco-embeddings.env.example" \
  "$repository_root/docs/design/semantic-retrieval.md"
do
  if [ ! -f "$required" ]; then
    echo "error: missing $required" >&2
    echo "  pass the repository root as the first argument if you are not" >&2
    echo "  running this from a checkout." >&2
    exit 1
  fi
done

# Pinned rather than "master". A build that silently changes under you is not a
# deployment step, and llama.cpp moves fast enough that it matters — the
# /v1/embeddings endpoint has broken and been fixed more than once. b10278 is
# the release current on 2026-08-05; override LLAMA_CPP_TAG to move it, and
# re-run bench_retrieval.py --service afterwards, because the deadline was
# chosen against a measured encode and a new build is a new measurement.
LLAMA_CPP_TAG=${LLAMA_CPP_TAG:-b10278}
MODEL_REPO=${MODEL_REPO:-cstr/multilingual-e5-small-GGUF}
MODEL_FILE=${MODEL_FILE:-multilingual-e5-small-q8_0.gguf}
BUILD_DIR=${BUILD_DIR:-/tmp/llama.cpp-build}
# This script runs as root and `rm -rf`s BUILD_DIR twice. An empty or
# careless override would delete something that is not a build tree, so
# require an absolute path at least two components deep and refuse anything
# outside the directories a scratch build belongs in.
case "$BUILD_DIR" in
  /tmp/?*/*|/tmp/?*|/var/tmp/?*/*|/var/tmp/?*|/opt/bocco-embeddings/build/?*|/opt/bocco-embeddings/build)
    ;;
  *)
    echo "error: refusing to use BUILD_DIR=$BUILD_DIR" >&2
    echo "  it is removed with 'rm -rf' as root; use a path under /tmp or" >&2
    echo "  /var/tmp, e.g. BUILD_DIR=/tmp/llama.cpp-build" >&2
    exit 1
    ;;
esac
case "$BUILD_DIR" in
  *..*)
    echo "error: BUILD_DIR must not contain '..'" >&2
    exit 1
    ;;
esac

if ! getent group bocco-embeddings >/dev/null 2>&1; then
  groupadd --system bocco-embeddings
fi
if ! id bocco-embeddings >/dev/null 2>&1; then
  useradd --system --gid bocco-embeddings --home-dir /opt/bocco-embeddings \
    --shell /usr/sbin/nologin bocco-embeddings
fi

install -d -o root -g root -m 0755 /opt/bocco-embeddings
install -d -o root -g root -m 0755 /opt/bocco-embeddings/bin
install -d -o root -g root -m 0755 /opt/bocco-embeddings/models
install -d -o root -g bocco-embeddings -m 0750 /etc/bocco-embeddings

if [ ! -x /opt/bocco-embeddings/bin/llama-server ]; then
  echo "building llama.cpp $LLAMA_CPP_TAG (a few minutes on a Pi 5)..."
  apt-get update
  apt-get install -y --no-install-recommends build-essential cmake git libcurl4-openssl-dev
  rm -rf "$BUILD_DIR"
  git clone --depth 1 --branch "$LLAMA_CPP_TAG" \
    https://github.com/ggml-org/llama.cpp "$BUILD_DIR"
  # BUILD_SHARED_LIBS=OFF is load-bearing, not a preference. llama.cpp defaults
  # to shared libraries, which makes llama-server a ~72 KB launcher linked
  # against libllama-server-impl.so, libllama.so and libggml*.so. This script
  # installs exactly one file and then deletes the build tree, so a dynamic
  # build leaves a binary whose libraries no longer exist:
  #   llama-server: error while loading shared libraries:
  #   libllama-server-impl.so: cannot open shared object file
  # and systemd restart-loops with status=127. Do not remove this flag without
  # also changing what gets installed.
  cmake -S "$BUILD_DIR" -B "$BUILD_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=ON -DGGML_NATIVE=ON \
    -DBUILD_SHARED_LIBS=OFF
  cmake --build "$BUILD_DIR/build" --target llama-server -j "$(nproc)"
  install -o root -g root -m 0555 \
    "$BUILD_DIR/build/bin/llama-server" /opt/bocco-embeddings/bin/llama-server
  rm -rf "$BUILD_DIR"
else
  echo "llama-server already installed; skipping build."
fi

# Prove the binary runs before anything downstream depends on it. Without this
# a broken build is only discovered as a systemd restart loop, minutes later,
# with the build tree already deleted.
if ! /opt/bocco-embeddings/bin/llama-server --version >/dev/null 2>&1; then
  echo "error: /opt/bocco-embeddings/bin/llama-server does not run" >&2
  /opt/bocco-embeddings/bin/llama-server --version >&2 || true
  echo "  if this reports missing shared libraries, the binary was built" >&2
  echo "  dynamically; rebuild with -DBUILD_SHARED_LIBS=OFF." >&2
  echo "  remove /opt/bocco-embeddings/bin/llama-server and re-run to rebuild." >&2
  exit 1
fi

if [ ! -f "/opt/bocco-embeddings/models/$MODEL_FILE" ]; then
  echo "fetching $MODEL_REPO/$MODEL_FILE (132 MB)..."
  curl -fL --retry 3 -o "/opt/bocco-embeddings/models/$MODEL_FILE" \
    "https://huggingface.co/$MODEL_REPO/resolve/main/$MODEL_FILE?download=true"
  chmod 0444 "/opt/bocco-embeddings/models/$MODEL_FILE"
else
  echo "model already present; skipping download."
fi

install -m 0644 "$repository_root/systemd/bocco-embeddings.service" \
  /etc/systemd/system/bocco-embeddings.service
# The unit's Documentation= points here; keep the pointer honest.
install -d -o root -g root -m 0755 /opt/bocco-bridge/docs/design
install -m 0644 "$repository_root/docs/design/semantic-retrieval.md" \
  /opt/bocco-bridge/docs/design/semantic-retrieval.md
if [ ! -e /etc/bocco-embeddings/embeddings.env ]; then
  install -o root -g bocco-embeddings -m 0640 \
    "$repository_root/systemd/bocco-embeddings.env.example" \
    /etc/bocco-embeddings/embeddings.env
fi

systemctl daemon-reload
cat <<'NEXT'

Installed. Next:

  systemctl enable --now bocco-embeddings.service
  curl -s http://127.0.0.1:8646/v1/embeddings \
    -H 'Content-Type: application/json' \
    -d '{"model":"multilingual-e5-small","input":["query: ごはんの話"]}' \
    | head -c 200

Expect a JSON body whose data[0].embedding has 384 elements. Then measure the
encode latency before choosing to trust the deadline:

  PYTHONPATH=bridge/src python3 bridge/tools/bench_retrieval.py --service

Only after that, turn the feature on in /etc/bocco-bridge/bridge.env and run
the backfill. See docs/design/semantic-retrieval.md for the full sequence.
NEXT
