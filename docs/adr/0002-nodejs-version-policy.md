# ADR 0002: Node.js は公式 tarball で同梱し、LTS 系列を固定してパッチのみ追従する

- Status: Accepted
- Date: 2026-07-31

## Context

agent 基盤の選定はインターン生に委ねているが、候補はいずれも Node.js 22 以上を要求する。
Debian bookworm の `nodejs` パッケージは v18 で要件を満たさない。

## 検討した選択肢

### 導入方法

| 選択肢 | 却下理由 |
|---|---|
| apt の `nodejs` | v18 で要件を満たさない |
| NodeSource の apt リポジトリ | 導入手順が `curl \| bash` で、ビルド中に外部スクリプトを実行することになる |
| **公式 tarball + チェックサム検証** (採用) | — |

### バージョン追従

| 選択肢 | 却下理由 |
|---|---|
| 完全固定 (`v24.18.1`) | セキュリティパッチも取り込まれない |
| 完全動的 (常に最新 LTS) | LTS 系列が上がった瞬間、ビルドが黙って major を跨ぐ |
| **系列固定 + パッチ追従 (`latest-v24.x`)** (採用) | — |

## Decision

**`https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt` で系列内の最新パッチを解決し、
そのバージョンに固定した URL から tarball を取得して `sha256sum -c` で検証し、
`/usr/local` へ展開する** (`layer/base/agent-base-setup.yaml`)。

系列を固定するのは、major の silent な bump を避けるため。インターン期間中に LTS が次の系列へ移った場合、
完全動的だと予告なく major が上がり、agent 基盤が動かなくなっても原因が分かりにくい。

## Consequences

- SD を焼いて起動した時点で `node` / `npm` が使える (`/usr/local/bin` は既定 PATH に含まれる)
- イメージサイズが約 0.4GB 増える
- **LTS 系列が上がったら手で更新する必要がある。** 放置を検知する仕組みは無いので、
  hook に次の系列の予定日をコメントで書いてある
- 手動アップグレード時の注意 (古い `node_modules` の削除) は `raspberry-img/README.md`
