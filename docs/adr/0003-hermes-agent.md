# ADR 0003: Ambient Agent 基盤に Hermes Agent を採用する

- Status: Accepted
- Date: 2026-08-03

## Context

BOCCO emo の音声・センサーイベントと Discord の両方から呼び出せる Ambient Agent 基盤が必要。
5 日間のプロトタイプなので、Discord bot や会話セッション管理を自作せず、BOCCO 固有の bridge に
実装時間を集中したい。Pi イメージには Python 3.11、Node.js、ffmpeg を入れられる。

## 検討した選択肢

| 選択肢 | 却下理由 |
|---|---|
| OpenAI API を直接呼ぶ独自 agent | Discord、会話履歴、tool 実行、常駐 gateway まで自作する範囲が広すぎる |
| OpenClaw | Pi の公式情報は充実しているが、今回使いたい Python bridge と Hermes の API server / gateway の組み合わせを優先した |
| **Hermes Agent** (採用) | Python 3.11 で動き、Discord gateway、会話セッション、skills、localhost API server を利用できる |

## Decision

**Ambient Agent 基盤に [Hermes Agent](https://github.com/NousResearch/hermes-agent) を採用する。**

- BOCCO bridge は Python 3.11 で実装し、BOCCO の認証、Webhook、重複排除、発話を所有する
- BOCCO からの会話は Hermes API server の `/v1/responses` を localhost 経由で呼ぶ
- Discord は Hermes 内蔵 gateway を使い、独自 bot は作らない
- Discord から emo を発話させる操作は Hermes skill から bridge の非公開 API を呼ぶ
- Cloudflare Tunnel で公開するのは bridge の BOCCO Webhook だけとし、Hermes は公開しない

詳細は [bridge architecture](../design/bridge-architecture.md) に記録する。

## Consequences

- BOCCO と Discord の入口を分離しつつ、生成・memory・tool 実行を Hermes に集約できる
- BOCCO token を Hermes に渡さず、bridge だけで refresh token の rotation を管理できる
- BOCCO と Discord の会話セッションは最初は別。チャネルをまたぐ操作は明示的な skill で行う
- Pi 上での Hermes の導入、OpenAI provider、Discord gateway、API server の実機疎通を早期に確認する必要がある
- Hermes の更新で API が変わるリスクを抑えるため、動作確認したバージョンを固定する
