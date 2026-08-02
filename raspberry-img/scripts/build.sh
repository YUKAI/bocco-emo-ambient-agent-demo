#!/bin/bash
# pipefail が無いと、下のイメージコピーで ls が 0 件だったり docker cp が失敗しても
# 成功扱いになり、前回ビルドの古いイメージが deploy に残ったまま「完了」と表示される
set -euo pipefail

# rpi-image-gen Docker ビルドスクリプト
# macOS (Apple Silicon) から Docker 経由でイメージをビルド

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONTAINER_NAME="rpi-imagegen-build"
BUILD_USER="imagegen"
IMAGE_NAME="ambient-agent-pi"
DEPLOY_DIR="${PROJECT_DIR}/deploy"

mkdir -p "$DEPLOY_DIR"

cleanup() {
    echo ""
    echo "コンテナを停止・削除中..."
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
}

trap cleanup EXIT

cd "$PROJECT_DIR"

# .DS_Store を削除 (rootfs-overlay に混入するとイメージに焼き込まれる)
find "$PROJECT_DIR" -name .DS_Store -delete 2>/dev/null || true

echo "=== rpi-image-gen カスタムイメージビルド (Docker) ==="
echo "プロジェクト: ${PROJECT_DIR}"
echo ""

echo "[1/5] Docker イメージをビルド中..."
docker compose build rpi_imagegen

echo "[2/5] apt キャッシュプロキシを起動中..."
docker compose up -d apt_cache
APT_PROXY_URL="http://apt_cache:3142"

echo "[3/5] コンテナを起動中..."
docker compose run --name "${CONTAINER_NAME}" -d rpi_imagegen

# apt_cache は起動後にコンテナ内で apt-cacher-ng を install するため数十秒応答しない。
# 待たずに進むと rpi-image-gen が "proxy unreachable" で即死するので、同じ判定で応答を待つ
docker exec -e PROXY="${APT_PROXY_URL}" "${CONTAINER_NAME}" bash -c '
    for _ in $(seq 90); do
        curl --silent --head --max-time 2 "$PROXY" >/dev/null && exit 0
        sleep 2
    done
    echo "apt キャッシュプロキシが起動しませんでした: $PROXY" >&2
    exit 1'

# rpi-image-gen build がコンテナ内 sudo を要するため NOPASSWD を付与
docker exec -u root "${CONTAINER_NAME}" bash -c \
    "echo '${BUILD_USER} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/${BUILD_USER}"

echo "[4/5] イメージ生成中..."
docker exec "${CONTAINER_NAME}" bash -c "\
    cd ~/rpi-image-gen && \
    sudo ./rpi-image-gen build -S /home/${BUILD_USER}/project -c image.yaml \
        -- IGconf_sys_apt_proxy_http=${APT_PROXY_URL}"

# regdom 欠落は 2.4GHz では繋がるため気づけない (静かに壊れる)
# deploy へコピーする前に検査する: 逆順だと壊れたイメージが直前の成功分を上書きする
echo "[検査] カーネルコマンドライン"
docker exec -i "${CONTAINER_NAME}" bash -s <<'CHECK'
set -eu
cmdline=$(ls ~/rpi-image-gen/work/*/filesystem/boot/firmware/cmdline.txt 2>/dev/null | head -1)
# 引数の欠落と「検査自体が壊れている」を区別する (glob が外れた場合の偽陽性を防ぐ)
[ -n "$cmdline" ] || { echo "cmdline.txt が見つからず検査できません" >&2; exit 1; }
# 部分一致では ds=nocloud-invalid のような無効値も通るため空白区切りの引数として照合する
grep -qE '(^| )ds=nocloud( |$)' "$cmdline" \
    || { echo "ds=nocloud がありません (cloud-init が起動しなくなります)" >&2; exit 1; }
grep -qE '(^| )cfg80211\.ieee80211_regdom=JP( |$)' "$cmdline" \
    || { echo "regdom の指定がありません (5GHz と 2.4GHz ch12-13 が使えなくなります)" >&2; exit 1; }
echo "OK: $cmdline"
CHECK

echo "[5/5] イメージをホストにコピー中..."
DEPLOY_SRC="/home/${BUILD_USER}/rpi-image-gen/work/deploy-*"
docker exec "${CONTAINER_NAME}" bash -c "ls -d ${DEPLOY_SRC}" | while read -r dir; do
    docker cp "${CONTAINER_NAME}:${dir}/${IMAGE_NAME}.img" "${DEPLOY_DIR}/${IMAGE_NAME}.img"
done

echo ""
echo "=== ビルド完了 ==="
echo "出力: ${DEPLOY_DIR}/${IMAGE_NAME}.img"
ls -lh "${DEPLOY_DIR}/${IMAGE_NAME}.img"

echo ""
echo "次のステップ:"
echo "  1. cloud-init 設定ファイルを作成 (テンプレートから):"
echo "     cp cloud-init/user-data.template.yaml cloud-init/user-data"
echo "     cp cloud-init/network-config.template.yaml cloud-init/network-config"
echo "  2. cloud-init 注入:"
echo "     bash scripts/inject-cloud-init.sh"
echo "  3. SDカード検出:"
echo "     bash scripts/detect-sd.sh"
echo "  4. SDカードに書き込み:"
echo "     diskutil unmountDisk /dev/diskN"
echo "     sudo dd if=deploy/${IMAGE_NAME}.img of=/dev/rdiskN bs=4M status=progress"
echo "     diskutil eject /dev/diskN"
