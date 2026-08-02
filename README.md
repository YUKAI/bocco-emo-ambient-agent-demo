# BOCCO emo × Ambient Agent

Raspberry Pi 上で動く Ambient Agent を、会話ロボット [BOCCO emo](https://www.bocco.me/) と Discord の
両方から呼び出せるようにする 5 日間のインターン課題。成果は会社ブログで公開する。

## どこに何があるか

| 文書 | 内容 |
|---|---|
| [ONEPAGER.md](ONEPAGER.md) | 課題の全体像。Goals / Non-Goals、技術選定、日次マイルストーン、BOCCO emo Platform API の要点 |
| [raspberry-img/README.md](raspberry-img/README.md) | Pi のイメージをビルドして SD に焼く手順、Webhook の外部公開、運用で踏みやすい点 |
| [docs/adr/](docs/adr/) | 設計判断とその理由 |

まず読むのは ONEPAGER。手を動かし始めるのは `raspberry-img/README.md` から。

## 設計判断 (ADR)

- [0001: Pi の構築を宣言的イメージビルドにし、機密は初回起動時に注入する](docs/adr/0001-raspberry-pi-image.md)
- [0002: Node.js は公式 tarball で同梱し、LTS 系列を固定してパッチのみ追従する](docs/adr/0002-nodejs-version-policy.md)

### このプロジェクトでの ADR の書き方

長期プロジェクトの ADR は「一度書いたら編集せず、変更は新しい ADR で supersede する」のが通例だが、
5 日間でその運用を回すのは手間の方が大きい。**この課題では次のように簡略化する。**

- **直接編集してよい。** 履歴は git に任せる。Status は `Accepted` 固定で、`Superseded` 運用はしない
- **1 本 1 画面まで。** 長いと読まれないし、書くのに時間を取られる
- **検討した選択肢を必ず書く。** 却下した案とその理由は、コードからは復元できない唯一の情報。
  書かないと後で同じ案が再提案されて議論がやり直しになる。選択肢は 3 つ程度、却下理由は各 1 行でよい
- **書く対象を絞る。** 「選択肢があった判断」だけを書く。ツールの仕様上そうするしかないもの
  (例: この引数が無いと起動しない) は決定ではないので、該当コードの直上コメントに書く
- **Day 4 に Consequences へ「実際どうだったか」を 3 行追記する。** 事前の想定と実際のズレが、
  そのままブログ記事の中身になる

インターン生が自分で選定する agent 基盤についても、選定理由を ADR として残すこと (`0003-` から)。
