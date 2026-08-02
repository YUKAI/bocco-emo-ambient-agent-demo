#!/bin/bash
set -eu

# SDカードを抜き差しで判別するスクリプト

echo "=== SDカード検出 ==="
echo ""
echo "SDカードを抜いた状態でEnterを押してください..."
read -r

BEFORE=$(diskutil list | grep -E "^/dev/disk" | awk '{print $1}')

echo "SDカードを挿入してEnterを押してください..."
read -r

MAX_RETRY=10
RETRY_INTERVAL=1
NEW_DISK=""

for i in $(seq 1 $MAX_RETRY); do
    AFTER=$(diskutil list | grep -E "^/dev/disk" | awk '{print $1}')
    NEW_DISK=$(comm -13 <(echo "$BEFORE" | sort) <(echo "$AFTER" | sort))

    if [ -n "$NEW_DISK" ]; then
        break
    fi

    echo "ディスク認識待ち... (リトライ $i/$MAX_RETRY)"
    sleep $RETRY_INTERVAL
done

if [ -z "$NEW_DISK" ]; then
    echo "エラー: 新しいディスクが検出されませんでした"
    exit 1
fi

DISK_COUNT=$(echo "$NEW_DISK" | wc -l | tr -d ' ')
if [ "$DISK_COUNT" -gt 1 ]; then
    echo "警告: 複数のディスクが検出されました:"
    echo "$NEW_DISK"
    exit 1
fi

RAW_DISK="${NEW_DISK/disk/rdisk}"

echo ""
echo "=== 検出結果 ==="
echo "SDカード: ${NEW_DISK}"
echo "rawデバイス: ${RAW_DISK}"
echo ""
diskutil info "$NEW_DISK" | grep -E "Device / Media Name|Total Size|Volume Name"
echo ""
echo "=== 書き込みコマンド例 ==="
echo "diskutil unmountDisk ${NEW_DISK}"
echo "sudo dd if=deploy/ambient-agent-pi.img of=${RAW_DISK} bs=4M status=progress"
echo "diskutil eject ${NEW_DISK}"
