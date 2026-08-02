# Raspberry Pi イメージビルド

Ambient Agent 用 Raspberry Pi 5 の OS イメージ (headless) を [rpi-image-gen](https://github.com/raspberrypi/rpi-image-gen) でビルドする一式。方式の背景・選定理由は [../ONEPAGER.md](../ONEPAGER.md) の「Raspberry Pi 環境構築方式」参照。

## 前提

- macOS + Docker Desktop (Apple Silicon 推奨。amd64 ホストは qemu + binfmt-support が必要)
- SD カード 32GB 以上

## 使い方

1. イメージビルド (`deploy/ambient-agent-pi.img` が生成される):

   ```bash
   bash scripts/build.sh
   ```

2. cloud-init 設定を作成 (機密入り、コミット禁止・`.gitignore` 済み):

   ```bash
   cp cloud-init/user-data.template.yaml cloud-init/user-data
   cp cloud-init/network-config.template.yaml cloud-init/network-config
   vim cloud-init/user-data   # パスワード・Wi-Fi SSID/PSK・API キーを記入
   ```

3. cloud-init を `.img` に注入:

   ```bash
   bash scripts/inject-cloud-init.sh
   ```

   代替: SD 書き込み後に boot パーティション (FAT32) を Mac でマウントし、`user-data` / `network-config` / `meta-data` を直接コピーしてもよい (`meta-data` は `instance-id: <任意のユニーク文字列>` の 1 行)。

4. SD カード検出と書き込み:

   ```bash
   bash scripts/detect-sd.sh
   diskutil unmountDisk /dev/diskN
   sudo dd if=deploy/ambient-agent-pi.img of=/dev/rdiskN bs=4M status=progress
   diskutil eject /dev/diskN
   ```

5. Pi を起動して SSH 接続を確認:

   ```bash
   ssh pi@ambient-pi.local
   ```

   `ambient-pi.local` が解決しないことがある (ネットワーク側の mDNS 伝播の問題で、Pi 自体は
   正常なことが多い)。その場合は ARP かルーターの DHCP 一覧から IP を直接見つける:

   ```bash
   arp -a | grep -i 2c:cf:67   # 2c:cf:67 は Raspberry Pi の MAC OUI
   ```

## Webhook を外部公開する

BOCCO emo の Webhook を Pi で受けるには公開 URL が要る。Cloudflare Quick Tunnel なら
ドメインもアカウントも不要:

```bash
curl -fsSLO https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared-linux-arm64.deb

# まずは前面で起動する。URL は接続確立後にバナーで表示される (Ctrl-C で終了)
cloudflared tunnel --url http://127.0.0.1:8787
```

常駐させるときは、**cloudflared を受信側プロセスの子として起動する**。Quick Tunnel の URL は
起動のたびにランダムに変わり、`cloudflared` の起動時の出力にしか現れない。別の systemd unit に
分けると受信側が現在の URL を知る手段が無くなる。子プロセスにすれば出力からそのまま URL を取れて、
トンネルが落ちたことも検知できる。systemd に載せるのは受信側サービス 1 つでよい。

- **URL は起動のたびに変わる**。Platform API に登録できる Webhook URL は 1 つだけなので、
  受信側が起動時に cloudflared の出力から URL を取り、`PUT /v1/webhook` で登録し直す
- 独立プロセスとして起動して**あとから journal で URL を調べる形にしない**。journal には
  前回起動分の URL も残るうえ、URL はトンネル確立後に出るため起動直後にはまだ無い。
  どちらの場合も死んだ URL を掴み、webhook が来ない理由が分からないまま時間を溶かす
- トンネルは指定ポートをそのまま公開する。同じポートに管理用のエンドポイントを生やすと
  外から叩けてしまうので、公開する口と内部用の口はポートを分ける
- 公式には "testing and development only" (SLA なし・同時 200 リクエスト上限)。
  常設運用に移すなら named tunnel (要アカウント + ドメイン) へ

## カスタマイズ

- **パッケージ追加**: `layer/base/agent-packages.yaml` (apt で入るものはここ。Node.js は同梱済みで、導入方法は [ADR 0002](../docs/adr/0002-nodejs-version-policy.md))
- **静的ファイル・systemd unit**: `rootfs-overlay/` に配置すると、rpi-image-gen が customize フェーズの最後にイメージのルートへコピーする (例: `rootfs-overlay/etc/systemd/system/agent.service` → `/etc/systemd/system/agent.service`)。**このコピーは customize-hooks より後に走る**ため、配置したファイルを加工する処理は `layer/suite/ambient-agent.yaml` の cleanup-hooks に置く。機密はここに置かず `cloud-init/user-data` で注入する
- **機密の注入**: `cloud-init/user-data` の `write_files` (イメージには焼き込まない)

## 注意

- `cloud-init/user-data` と `cloud-init/network-config` は機密を含むためコミットしない (テンプレートのみコミット)
- 初回起動後も `/boot/firmware/user-data` に機密が**平文で残る**。機材の返却・貸与時は必ず削除する
- `inject-cloud-init.sh` は `deploy/ambient-agent-pi.img` 自体に機密を書き込む。`.gitignore` 済みなので
  コミットはされないが、**この .img を他人に渡したりバックアップに含めたりしない**。
  焼き終わったら削除するか、`build.sh` で注入前の状態に作り直す
- **`/usr/sbin` が `pi` の PATH に入っていない**。`iw` などはフルパス (`/usr/sbin/iw`) で叩く
- **Node.js を手動でバージョンアップするときは先に `/usr/local/lib/node_modules` を削除する**。
  tarball を上書き展開すると古い npm の残骸が混ざり `npm install` が壊れる
  (バージョン方針は [ADR 0002](../docs/adr/0002-nodejs-version-policy.md))
- 5GHz が繋がらないときは規制ドメインを疑う。`/usr/sbin/iw reg get` が `country JP` を返すか、
  `/usr/sbin/iw phy | grep 5540` でチャネルに `no IR` が付いていないかを見る
  (カーネル引数での設定理由は [ADR 0001](../docs/adr/0001-raspberry-pi-image.md))
