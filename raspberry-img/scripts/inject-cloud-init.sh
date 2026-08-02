#!/bin/bash
set -eu

# cloud-init user-data, network-config をイメージの boot パーティションに書き込む
# Docker で losetup/mount するため OS 非依存

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
IMG_PATH="${1:-${PROJECT_DIR}/deploy/ambient-agent-pi.img}"
USERDATA_PATH="${2:-${PROJECT_DIR}/cloud-init/user-data}"
NETWORK_CONFIG_PATH="${3:-${PROJECT_DIR}/cloud-init/network-config}"

if [ ! -f "$IMG_PATH" ]; then
    echo "エラー: イメージが見つかりません: $IMG_PATH"
    echo "使い方: $0 [img_path] [user-data_path] [network-config_path]"
    exit 1
fi

if [ ! -f "$USERDATA_PATH" ]; then
    echo "エラー: user-data が見つかりません: $USERDATA_PATH"
    echo ""
    echo "テンプレートからコピーしてシークレットを記入してください:"
    echo "  cp cloud-init/user-data.template.yaml cloud-init/user-data"
    echo "  vim cloud-init/user-data"
    exit 1
fi

if [ ! -f "$NETWORK_CONFIG_PATH" ]; then
    echo "エラー: network-config が見つかりません: $NETWORK_CONFIG_PATH"
    echo ""
    echo "テンプレートからコピーしてください:"
    echo "  cp cloud-init/network-config.template.yaml cloud-init/network-config"
    exit 1
fi

echo "=== cloud-init 注入 ==="
echo "イメージ: ${IMG_PATH}"
echo "user-data: ${USERDATA_PATH}"
echo "network-config: ${NETWORK_CONFIG_PATH}"
echo ""

docker run --rm --privileged -u root \
    -v "${IMG_PATH}:/work/image.img" \
    -v "${USERDATA_PATH}:/work/user-data:ro" \
    -v "${NETWORK_CONFIG_PATH}:/work/network-config:ro" \
    rpi-imagegen:latest bash -c '
set -eu

LOOP=$(losetup --show -fP /work/image.img)
echo "Loop: $LOOP"

kpartx -av "$LOOP"
sleep 1

BOOT_DEV="/dev/mapper/$(basename ${LOOP})p1"
mkdir -p /mnt/boot
mount "$BOOT_DEV" /mnt/boot

cp /work/user-data /mnt/boot/user-data
cp /work/network-config /mnt/boot/network-config

# meta-data を配置 (NoCloud データソースに必須)
echo "instance-id: $(cat /proc/sys/kernel/random/uuid)" > /mnt/boot/meta-data

echo "user-data, network-config, meta-data を boot パーティションに配置しました"
echo ""
echo "=== 配置済みファイル確認 ==="
ls -la /mnt/boot/user-data /mnt/boot/network-config /mnt/boot/meta-data

umount /mnt/boot
kpartx -d "$LOOP"
losetup -d "$LOOP"
echo ""
echo "完了"
'

echo ""
echo "=== 注入完了 ==="
echo "このイメージを SD カードに書き込めば、初回起動時に cloud-init が自動設定します。"
echo ""
echo "次のステップ:"
echo "  1. SDカード検出:"
echo "     bash scripts/detect-sd.sh"
echo "  2. SDカードに書き込み:"
echo "     diskutil unmountDisk /dev/diskN"
echo "     sudo dd if=${IMG_PATH} of=/dev/rdiskN bs=4M status=progress"
echo "     diskutil eject /dev/diskN"
