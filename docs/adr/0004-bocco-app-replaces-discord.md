# ADR 0004: テキスト操作には Discord ではなく BOCCO 公式アプリを使う

- Status: Accepted
- Date: 2026-08-03
- Related: [ADR 0003: Hermes Agent](0003-hermes-agent.md)

## Context

Discord 対応を含む bridge baseline は完成したが、BOCCO emo には room のメッセージ送受信と履歴表示を行う
公式アプリがある。プロトタイプで別の chat ecosystem、bot credential、gateway、private speech API を維持すると、
ユーザー体験と運用箇所が増える。Hermes の応答生成には messaging gateway は不要で、localhost Responses API
だけを使える。

## 検討した選択肢

| 選択肢 | 判断 |
|---|---|
| BOCCO アプリと Discord を両方残す | 動作済みだが、credential、権限、障害点、説明コストが増える |
| **BOCCO 公式アプリに統一する** | 採用。音声と text を同じ BOCCO room / Webhook flow で扱える |
| 独自 Web UI を作る | 5 日間のデモ範囲を超え、公式アプリと機能が重複する |

## Decision

**ユーザーの text interface は BOCCO 公式アプリに統一し、Discord integration を削除する。**

- Hermes は `127.0.0.1:8642` の Responses API だけを提供する
- bridge が外部公開するのは `127.0.0.1:8787` の BOCCO Webhook だけとする
- app/emo の `message.received`、radar、queue、deduplication、token rotation、BOCCO delivery は維持する
- radar は BOCCO 発話成功と cooldown 永続化後に完了する

## Consequences

- bot credential、Hermes Webhook adapter、private speech API、専用 skill が不要になる
- service は `bocco-bridge.service` と `hermes-api.service` の 2 つになる
- 既存 DB の `discord_sent` column は互換性のため残すが、runtime は読み書きしない
- app-originated Webhook の room/sender/text と app 履歴への応答表示は、実機での最終確認が必要
- 旧 Discord-capable baseline は本リポジトリの履歴には含まれない
