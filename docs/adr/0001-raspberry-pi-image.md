# ADR 0001: Pi の構築を宣言的イメージビルドにし、機密は初回起動時に注入する

- Status: Accepted
- Date: 2026-07-31

## Context

agent を常時動かす Pi が要る。壊れても短時間で同じ状態に戻せること、Wi-Fi パスワードや
API キーをリポジトリに置かないこと、ヘッドレスで動くことが条件。

## 検討した選択肢

| 選択肢 | 却下理由 |
|---|---|
| Raspberry Pi Imager の詳細設定で手作業 | 設定内容がリポジトリに残らず、再現も引き継ぎもできない |
| 素の OS + Ansible 等で構成管理 | 構成は残るが初回の SSH 到達が前提。ヘッドレスで Wi-Fi が入る前は流せない |
| pi-gen (Raspberry Pi OS の公式ビルダ) | シェルスクリプトの段階実行で、宣言的に差分を読めない |
| **rpi-image-gen + cloud-init** (採用) | — |

## Decision

**rpi-image-gen で宣言的にイメージをビルドし、機密は cloud-init (NoCloud) で初回起動時に注入する。**

イメージに何を含めるかは、次の基準で分けている。

- **焼き込む**: 全個体で同じもの (タイムゾーン、ロケール、Wi-Fi 規制ドメイン、パッケージ、systemd unit)
- **初回起動時に注入する**: 個体ごとに違うもの・機密 (Wi-Fi の SSID/PSK、パスワード、API キー)

規制ドメインは「全個体で同じ」だけでなく **Wi-Fi の association より前に効く必要がある**ため、
cloud-init では遅い。カーネルコマンドラインで指定している。

## Consequences

- `bash raspberry-img/scripts/build.sh` で誰でも同じイメージを作れる
- 機密はビルドしたイメージには入らない。ただし `inject-cloud-init.sh` を実行した時点で
  手元の `deploy/*.img` に平文で焼き込まれ、焼いた後の SD の boot パーティションにも残り続ける。
  どちらも消す手順は `raspberry-img/README.md`
- **この方式はカーネルコマンドライン引数 2 つ (`ds=nocloud` / `cfg80211.ieee80211_regdom=JP`) に依存する。**
  どちらも消すと壊れるため、理由は `layer/suite/ambient-agent.yaml` の該当行にコメントで、
  消えていないことの保証は `scripts/build.sh` の assert で担保している
- Pi 5 (`device.layer: rpi5`) 前提。別モデルでは layer の変更が要る
