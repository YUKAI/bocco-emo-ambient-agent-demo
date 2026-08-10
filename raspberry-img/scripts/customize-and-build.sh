#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$PROJECT_DIR")"
ENV_FILE="${REPO_ROOT}/.env"
IMAGE_PATH="${PROJECT_DIR}/deploy/ambient-agent-pi.img"
REUSE_IMAGE=false

case "${1:-}" in
    "") ;;
    --reuse-image) REUSE_IMAGE=true ;;
    *)
        echo "Usage: $0 [--reuse-image]" >&2
        exit 2
        ;;
esac

command -v python3 >/dev/null || {
    echo "ERROR: python3 is required" >&2
    exit 1
}
command -v docker >/dev/null || {
    echo "ERROR: Docker Desktop is required" >&2
    exit 1
}
docker info >/dev/null 2>&1 || {
    echo "ERROR: Docker Desktop is installed but not running" >&2
    exit 1
}

echo "[1/4] Validating ${ENV_FILE}"
python3 "${SCRIPT_DIR}/render_config.py" --env-file "${ENV_FILE}" --check

if [ "$REUSE_IMAGE" = true ]; then
    [ -f "$IMAGE_PATH" ] || {
        echo "ERROR: --reuse-image requested but image is missing: ${IMAGE_PATH}" >&2
        exit 1
    }
    echo "[2/4] Reusing the existing Raspberry Pi image"
else
    echo "[2/4] Building the Raspberry Pi image"
    bash "${SCRIPT_DIR}/build.sh"
fi

echo "[3/4] Rendering cloud-init from the local .env"
python3 "${SCRIPT_DIR}/render_config.py" --env-file "${ENV_FILE}"

echo "[4/4] Injecting cloud-init into the image"
bash "${SCRIPT_DIR}/inject-cloud-init.sh"

echo
echo "Customized image ready:"
echo "  ${PROJECT_DIR}/deploy/ambient-agent-pi.img"
echo
echo "Next: detect the SD card and review the printed device path:"
echo "  cd ${PROJECT_DIR}"
echo "  bash scripts/detect-sd.sh"
echo
echo "The image now contains secrets. Do not upload, share, or back it up."
