# BOCCO emo × Ambient Agent

> [!NOTE]
> このリポジトリは、BOCCO emo Platform API を使って自宅で ambient agent を動かすための**デモ / 参考実装**です。YUKAI がホストするサービスではなく、各ユーザー自身の環境で動作し、外部サービスへのアクセスも利用者自身のアカウント・API キーで行われます。各サービスの利用規約の範囲でご利用ください。

Raspberry Pi 5 上で動く Ambient Agent を、BOCCO emo の音声と
[BOCCO 公式アプリ](https://www.bocco.me/application/)の text から呼び出すインターン課題。
BOCCO Platform API と Hermes Agent を接続し、sensor event にも自発的に反応する。

## Repository layout

| Path | 内容 |
|---|---|
| `bridge/` | `bocco-bridge` service (Python)。Webhook、SQLite queue、BOCCO client、motion |
| `hermes/` | Hermes Agent の設定例と、監査済み fast-route script |
| `systemd/` | Pi に入れる unit と `*.env.example` |
| `scripts/` | runtime の install / test / health check |
| `raspberry-img/` | Pi の SD イメージを build して焼くための一式 |
| `docs/` | ADR と設計文書 |

`scripts/` は repository 全体に対して実行するもの (runtime の導入・検証)、
`raspberry-img/scripts/` はイメージ build 専用で、隣の `Dockerfile` と `config/` と対で使う。

## Documents

| Document | Purpose |
|---|---|
| [ONEPAGER.md](ONEPAGER.md) | 課題の全体像。背景、Goals / Non-Goals、技術選定、ライセンス確認 |
| [docs/STATUS.md](docs/STATUS.md) | 実装の現在地。demo scenario、milestone、hardware validation、risk |
| [docs/design/bridge-architecture.md](docs/design/bridge-architecture.md) | BOCCO bridge と Hermes API の責務、flow、状態管理 |
| [docs/design/bridge-runtime.md](docs/design/bridge-runtime.md) | runtime 設定の全一覧、queue、導入、検証手順 |
| [docs/design/motion-speech-concurrency.md](docs/design/motion-speech-concurrency.md) | speech と motion の同時実行と timing |
| [docs/design/semantic-retrieval.md](docs/design/semantic-retrieval.md) | embedding 検索の設計、latency guarantee、実機導入手順 |
| [docs/design/replication-guide.md](docs/design/replication-guide.md) | 空の SD card から動く robot までの全手順と、再現できない箇所 |
| [docs/design/bocco-api-findings.md](docs/design/bocco-api-findings.md) | BOCCO Platform API の実測挙動と落とし穴 (証拠付き) |
| [docs/design/accel-voice-latency.md](docs/design/accel-voice-latency.md) | 触られてから喋り出すまでの latency 分解 |
| [docs/design/voice-latency-outliers.md](docs/design/voice-latency-outliers.md) | 音声応答の外れ値がどこで生じているか |
| [docs/design/missing-replies.md](docs/design/missing-replies.md) | 「返事が来ない」の全経路調査 |
| [raspberry-img/README.md](raspberry-img/README.md) | Pi image の build、flash、運用 |
| [docs/adr/README.md](docs/adr/README.md) | 設計判断と理由、この課題での ADR の書き方 |

## Current architecture

```text
BOCCO emo / official app
  -> BOCCO Cloud Webhook
  -> Cloudflare Quick Tunnel
  -> bocco-bridge :8787
  -> SQLite queue
  -> Hermes Responses API :8642
  -> BOCCO Platform API
  -> emo speech + app room history
```

`bocco-bridge` は Webhook 認証、request-ID deduplication、restart recovery、BOCCO token rotation、
delivery checkpoint、durable outbound `unique_id` correlation、radar cooldown、Quick Tunnel 再登録を所有する。
Pi-local minute scheduler も同じ SQLite queue と single worker を使う。user message は pending ambient/background generation より
常に先に claim し、schedule と reaction-bank refresh は最低 priority で処理する。
Hermes は loopback-only Responses API で短い日本語応答を生成する。bridge の音声向け基本ルールは常に適用され、
`BRIDGE_ROBOT_NICKNAME` と `BRIDGE_PERSONA` で robot の名前と性格を任意に追加できる。詳細は
[ADR 0004](docs/adr/0004-bocco-app-replaces-discord.md) を参照。

## Test

実値や live service を使わない全 test:

```sh
scripts/test-bridge.sh
```

fake-server integration は次を通す。

```text
BOCCO Webhook -> SQLite queue -> Hermes Responses API -> BOCCO text API
```

### CI

`.github/workflows/ci.yml` が pull request と `main` への push、および `workflow_dispatch`
による手動実行で次を実行する。secret も robot も要らない check だけを置く。実機に対する
確認は `scripts/check-bridge.sh` の担当。

| Job | 内容 |
|---|---|
| `tests (Python 3.11 / 3.14)` | `scripts/test-bridge.sh`、`LC_ALL=C` での再実行、`raspberry-img/tests`。3.11 は Pi (Debian 12) の実行環境、3.14 は開発機 |
| `shell scripts` | shebang ごとの `sh -n` / `bash -n` と ShellCheck |
| `hermes scripts are stdlib-only` | `scripts/check-hermes-stdlib.py` |

`LC_ALL=C` の再実行は locale への依存を検出する。`Path.read_text()` は encoding 省略時に
locale の既定 encoding を使い、C/POSIX ではそれが ASCII になる。この repository が読む
file には日本語が含まれるので、runner の locale 次第で初めて壊れる。`PYTHONCOERCECLOCALE=0`
は PEP 538 の C locale coercion を無効にする — それがないと検査対象そのものが隠れる。

`hermes/` 配下の script は bridge の subprocess (`fast_route_python`) と Hermes の
virtualenv が実行するもので、この repository はそこへ依存 package を install しない。
standard library の外を import すると CI ではなく実機で、しかも無言で壊れるので、CI が
import を静的に検査する。実行はしない (Open-Meteo / NHK / Gmail を呼ぶため)。

## Pi services

何もない Pi から動く robot までの全手順は
[docs/design/replication-guide.md](docs/design/replication-guide.md) にある。要約:

```sh
# 1. Hermes Agent 本体（pinned commit の checkout、venv、skills）。Pi 5 で約 15 分。
sudo scripts/install-hermes.sh "$PWD"

# 2. bridge、unit file、deterministic fast routes。
sudo scripts/install-services.sh "$PWD"

sudoedit /etc/bocco-bridge/bridge.env
sudoedit /etc/hermes-agent/hermes.env
sudo systemctl enable --now hermes-api.service bocco-bridge.service
scripts/check-bridge.sh
```

順序は必須で、かつ強制されている。`install-services.sh` は `hermes-api.service` を install/enable
するため、`/opt/hermes-agent/.venv/bin/python` が無い状態では実行を拒否する。無いまま enable すると
`status=203/EXEC` の restart loop になり、install 忘れではなく service の crash に見えてしまう。

`install-hermes.sh` は idempotent で、既存の `config.yaml` / `SOUL.md` / `hermes.env` を上書きしない。
`HERMES_PREFIX` / `HERMES_STATE` / `HERMES_ETC` を上書きすれば、稼働中の Pi に触れずに全体を予行演習できる。

実環境の token と Webhook secret は repository に置かず、Pi 上の `/etc` 配下だけに注入する。
library は repository-root `.env` を自動で読まない。

`OPENAI_API_KEY` は自分で取得する必要がある唯一の有料 credential。`API_SERVER_KEY` は取得先が無く、
自分で決めた乱数を `/etc/hermes-agent/hermes.env` と `/etc/bocco-bridge/bridge.env` の**両方**に
同じ値で書く。不一致は初回起動で最も多い失敗で、OpenAI 側の 401 に見えるが違う。

### Hermes skills

Hermes 同梱の約 180 skills は `.no-bundled-skills` marker で一括無効化し、実際に使う 8 個だけを入れる。
うち 5 個（`weather` `datetime` `units` `news` `email`）はこの project 用に書かれた stdlib-only の
custom skill で、upstream には存在しない。`hermes/skills/` に version 管理されている。
残り 3 個（`maps` `finance/stocks` `health/fitness-nutrition`）は pinned upstream checkout から複製する。

robot の identity/persona は bridge 環境で任意設定する。空または未設定なら従来どおりで、multi-line persona は
`BRIDGE_PERSONA` 内の literal `\n` で指定する。

公式アプリから `ペルソナ：明るく好奇心旺盛な性格`（`ペルソナ:`、case-insensitive `persona:` も可）と送ると、
500 文字以内の性格を SQLite に保存して次の対話から反映する。prefix の後を空にすると保存値を削除し、
`BRIDGE_PERSONA` default に戻る。command の確認 reply は Hermes を呼ばず、通常の BOCCO delivery/echo suppression を通る。
`のんびり`、`てれすけ`、`むかんしん` は BOCCO personality preset として、用意済みの instruction profile に展開する。

radar greeting の cooldown は `BOCCO_BRIDGE_RADAR_COOLDOWN_SECONDS` で変更でき、default は 1800 秒（30 分）。
radar は event-time に Hermes を呼ばず、persona hash と morning/day/evening bucket に対応する reaction bank から即時発話する。

## Touch and light reactions

`accel.detected` は debounce timer なしで、最初の event を durable reaction bank lookup -> BOCCO send へ直結する。event-time の
Hermes call はゼロ。bank は composed persona の SHA-256 と event kind ごとに 5 個の短文を持ち、startup と persona 変更後に
最低-priority background event が更新する。strict JSON parse に失敗した key は以前の bank を維持し、bank 未生成時も code 内の
neutral phrases で即時反応できる。

同じ room・同じ受信秒にすでに queue 済みの kind だけは待たずに 1 件へまとめ、`dropped > shaken > upside_down > lift > beaten`
の drama priority で選ぶ。後続 event は global/per-kind cooldown で無言になる。`beaten` / `lift` / `shaken` は default 120 秒、
`dropped` / `upside_down` は 300 秒。lesser reaction の 5 秒以内に来た初回 `dropped` だけは safety exception として fixed caring
response を許す。`dropped` / `upside_down` は persona bank ではなく固定の serious tone、`normal` / `lying_down` は常に無言。

`illuminance.changed` は Pi-local 05:00–10:00 の `brighter` に morning greeting、20:00 以降の `darker` に
goodnight greeting を 1 回生成し、共通 8 時間 cooldown を使う。それ以外の時間は無言。
deploy 後、user が実機のそばにいる状態で face tap、head pat、pickup が実際にどの accel kind を発生させるかを
journal/Webhook で characterization する。

## Motion choreography

音声は `recording.finished` の時点で `ALRIGHT_N_0` の短い nod を fire-and-forget し、その後 15 秒以内の audio
`message.received` と durable one-shot correlation して generation-time の二重 nod を抑える。同時に recording finish から message
timestamp までを `stt_latency` として INFO log する。preceding recording がない app text は従来どおり normal Hermes generation
開始時に nod する。speech や app history の filler message は作らない。persona/予定/memory command、radar、non-speech、
成功する fast route は message-time acknowledgment の対象外。`BRIDGE_ACK_MOTION`（default true）で無効化でき、
`BRIDGE_ACK_MOTION_NAME`（default `ALRIGHT_N_0`）で exact catalog name を変更できる。catalog に名前がない場合や共通の
8 calls/minute motion budget が満杯の場合は silent skip する。event ごとの durable attempted checkpoint により retry/restart でも
重複せず、POST failure は reply/Hermes を止めない。

ack motion は reply choreography とは別の best-effort gesture で、Hermes call を待たせない。reply cue が後から生成された場合も
通常どおり進む。default nod は約 1–2 秒で終わる想定なので通常は重ならないが、実機が running motion を interrupt できない場合は
後続 cue が platform 側で queue される可能性がある。

Hermes は返答本文の対応する位置へ最大 3 個の inline cue、`[motion:<name>]` を任意で入れられる。bridge は cue を
読み上げ前にすべて除去し、known cue の Unicode codepoint 位置だけを保持する。unknown cue は除去して無視し、cue がない
返答は text-only のまま。mood mapping は次のとおり。

| Cue | Preset motion family |
|---|---|
| `うれしい` | `GOOD_*` / `YES_*` |
| `こまった` | mild `WTF_*` |
| `びっくり` | `WHAT_*` |
| `うなずき` | `YES_*` / `ALRIGHT_*` |
| `いやいや` | `NO_*` |
| `ふつう` | no motion |

scene/nod whitelist は `Sunny`、`Rain_Little`、`Rain_Hard`、`Cloudy`、`Hot`、`Cold`、`GoodMorning`、
`GoodNight`、`tadaima`、`YES`、`NO`、`GOOD`、`WHAT`。radar と light reaction は local time に応じた
GoodMorning/GoodNight scene cue を追加できる。

preset catalog は startup に pagination して memory cache し、conversation 中に再取得しない。optional `BOCCO_ROOM_UUID` があれば
startup に `voice_speed` を 1 回取得する。cold start は delivery lag 0.5 秒、100% voice speed で 0.15 秒/文字。
API message では `emo_talk.finished` が届かない実機結果に対応し、`motion.finished(kind=newMessageMotion)` を message-arrival/speech-start
anchor として pending cue を rebase する。`emo_talk.finished` が届く talk type では anchor-to-finish から rate を更新し、anchor がない
場合は cold estimate だけで続行する。alpha 0.1 の EMA clamp は lag 0–5 秒、rate 0.05–0.4 秒/文字。
60 秒 timeout、restart 時 abandon、1 分 8 motion の budget で、後ろの cue から graceful に落とす。text は常に先に送り、
motion failure は reply completion に影響させない。

**実機で決着済み（2026-08-05 の live trial）: BOCCO emo は Platform API の motion を speech が終わるまで queue する。**
preset でも custom document でも同じで、speech は途中で切られも消されもしない。犠牲になるのは motion のほうで、
発話中に dispatch した gesture は失われずに「発話が終わってから」動く。したがって `cue_offset_seconds` が狙う
mid-speech の cue timing はこの API では実現できず、遅れて出る `かんがえちゅう` も scheduling bug ではなく
device の性質である。根拠と runs は [docs/design/motion-speech-concurrency.md](docs/design/motion-speech-concurrency.md)、
再現用の harness は `bridge/tools/motion_speech_trial.py`。

## Motion invention（default off）

`BRIDGE_MOTION_INVENTION=on`（default off）にすると、「おどって：うれしい気持ち」のような依頼に対して
model が catalog から選ぶのではなく **その場で振り付けを作る**。model が出すのは compact な choreography spec で、
bridge がそれを Motion Editor 形式の JSON document に render し、`custom_motions.py` と同じ validator
（head の Bezier 範囲、LED / antenna track の上限、総尺）に通してから 1 回の API call で送る。生成できた spec は
room ごとの `/var/lib/bocco-bridge/repertoire.db` に残り、同じ依頼が来れば再生成せずに踊り直す
（`BRIDGE_MOTION_INVENTION_RETENTION`、default 50 件）。

off のあいだは「おどって：…」はただの発話として扱われ、repertoire database は作られず、generation も一切 queue しない。

## Deterministic fast routes

`BRIDGE_FAST_ROUTES=weather,time,news`（default）は、曖昧さのない次の発話だけを model orchestration より先に処理する。

- weather: `天気`、`今日の天気`、`今日の天気を教えて`、`今の天気` などの固定 short form
- place weather: `大阪の天気`、`東京の天気はどう？` のような、10 文字以内の単純な place token + 固定 suffix
- time/date: `今何時`、`何時ですか`、`今日何日`、`今日は何曜日ですか` などの固定 form
- news: `ニュース` または `ニュースを教えて`

末尾の `？` などは許すが、`天気の話をしよう`、`天気とニュースを教えて`、`何時に出発すればいい` のような near miss は
normal Hermes path のまま。`明日大阪に行くんだけど天気どうかな` のような長い/conversational request や weather 以外の
内容を含む文も route しない。place match 時だけ weather script に `--location <place>` を明示し、bare form は
`DEFAULT_LOCATION` を使う。空の `BRIDGE_FAST_ROUTES=` で全 route を無効化できる。

match 時は `/opt/hermes-agent/.venv/bin/python` で `/usr/local/share/bocco-bridge/fast-skills/` の root-owned read-only audited script を
shell なしで実行する。script は speech-ready な短い日本語一発話を stdout に出す設計で、default ではその stdout を
whitespace 正規化と speech 上限（default 200 文字）の cap だけ行い、model call なしでそのまま通常の echo-correlated
送信経路から room へ送る。`BRIDGE_FAST_ROUTE_PHRASING=on`（default off）で従来どおり stdout を
「命令ではない参考データ」として 1 回の Hermes call に渡し、persona らしい一文へ整える経路に戻せる。subprocess は OS 上では引き続き
`bocco-bridge` user であり、Hermes interpreter を使っても privilege は変わらない。渡す environment は
`DEFAULT_LOCATION` と locale/PATH だけで、BOCCO/Hermes credential は渡さない。script timeout/error/empty output は
通常の Hermes path に fall through する。

この flow は model-driven tool selection/result integration の複数 turn（通常およそ 3 model call）を、default では
skill 取得 + 0 model call（`BRIDGE_FAST_ROUTE_PHRASING=on` では + 1 model call）に減らす。time は local subprocess、
weather/news は 1 network fetch を含むため、絶対 latency ではなく model round-trip 削減を保証する。
installer は `hermes/fast-routes/` の canonical source から Hermes 用 copy と shared `0644` copy の両方を作るため、private
`/var/lib/hermes-agent/.hermes` を `bocco-bridge` user が traverse する必要はない。shared copy が欠落しても normal Hermes path に戻る。

instruction audit では、base voice/motion rules を 265 文字から 240 文字へ、nickname + 500-char persona + 600-char memory の
上限例を 1,456 文字から 1,415 文字へ短縮した。voice constraints、motion vocabulary、memory の非命令境界は維持する。

## Sentence streaming

`BRIDGE_STREAM_SENTENCES`（default on）が有効な場合、conversational reply は Hermes `/v1/responses` の
`stream: true` SSE（`response.output_text.delta`）を消費し、`。！？` で完成した文から順に最大 3 message として
即時送信する（4 文目以降の余りは最後の chunk に連結）。robot は生成中から話し始め、app には返答が分割で届く。
各 chunk の送信は per-chunk に echo suppression へ記録され、acknowledgment motion は reply ごとに 1 回のまま。
inline motion cue はその cue を含む chunk の text に付き、最初の cue 付き chunk が event ごとの single motion chain を
claim する。送信前に stream が失敗した場合は従来の single-call path へ fall back し、部分送信後の失敗は送信済み内容を
確定して retry での重複発話を避ける。`BRIDGE_STREAM_SENTENCES=off`、または Hermes client が streaming 非対応の場合は
従来どおり 1 message で返す。fast route・persona/schedule/memory command・radar などの bank 返答は streaming の対象外。

## Proactive schedules

公式アプリから毎日の予定発話を管理できる。すべて Pi-local time。

```text
よてい：08:00 おはようと声をかけて
予定：08:30 ブリーフィング
schedule:list
よてい：けし 08:00
予定：削除 08:30
```

`ブリーフィング`（`briefing` も可）は当日の日付・曜日と Hermes の weather/news skills を使う短い朝のまとめ。
custom text はそのまま Hermes prompt になる。予定と daily fire guard は SQLite に残り、restart 中に過ぎた時刻は
catch-up せず、同じ local date には 2 回発火しない。Hermes failure は無言で skip する。

## Household memory

部屋ごとの明示的な長期記憶を公式アプリから管理できる。

```text
おぼえて：猫の名前はミケ
覚えて：母の誕生日は五月三日
remember: 犬の好物はさつまいも
わすれて：猫の名前
記憶：リスト
```

active な事実は private な `/var/lib/bocco-bridge/memory.db` に保存し、会話文と FTS5 で関連する上位 5 件だけを
Hermes instructions の persona 後へ最大 600 文字で追加する。記憶 command 自体は Hermes を呼ばず、通常の
delivery checkpoint と echo suppression を通る。同じ subject を覚え直すと旧事実を superseded にする。
会話からの暗黙的な fact extraction は引き続き TODO（明示的な command でしか覚えない）。embedding retrieval は
conversational memory 側で実装済み — 下の「Semantic retrieval」を参照。

## Conversational memory

Hermes は room ごとの named conversation を server 側で無限に伸ばすため、prompt が compression threshold
（8,000 tokens）を超えても止まらない。`BRIDGE_CONVERSATION_MEMORY=on`（default off）にすると、bridge 自身が
完了した exchange を private な `/var/lib/bocco-bridge/transcript.db`（`turns` + FTS5 trigram）へ room-scoped に
保存し、prompt を **直近 N 件の verbatim window + BM25 で検索した過去 turn** から組み立てる。household memory
とは lifetime が違うため table も database file も分ける（fact は superseded 方式で永続、turn は retention で
prune）。

- 保存は reply を送り終えた後に行い、reply path も single event worker も待たせない。fallback 応答
  （STT 失敗・Hermes 失敗）は turn として保存しない。
- injection 先は household memory と同じ `instructions`。`BRIDGE_CONVERSATION_MAX_CHARS`（default 1,200）で
  section 全体を hard cap し、超える場合は retrieved → 古い recent の順に落とす。recent window は relevance に
  関係なく必ず入る。
- `BRIDGE_HERMES_CONVERSATION_MODE` が Hermes 側の bound を決める。`persistent`（default）は従来どおり、
  `rotating` は `BRIDGE_HERMES_CONVERSATION_ROTATE_TURNS` ごとに `bocco-room:<uuid>:e<n>` へ切り替える、
  `stateless` は conversation を送らず `store` もしない（continuity は完全に bridge の transcript が担う）。
  `persistent` 以外は `BRIDGE_CONVERSATION_MEMORY` が必須で、reaction bank の conversation も同時に stateless
  になる（bank の prompt は元から self-contained）。
### Semantic retrieval（embedding、default off）

BM25 は綴りの一致しか見ない。実機の 187 turn では「君の好きな食べ物何?」→「あったかいスープが好きだよ」に対して
「ごはんの話したっけ」が **一文字も共有せず** 何も引けない、5〜10 文字の短い exchange（「元気？」→「元気だよ。」）は
lexical signal がほぼ無く事実上引けない、といった取りこぼしが確認できる。`BRIDGE_CONVERSATION_VECTORS=on`
（default off、`BRIDGE_CONVERSATION_MEMORY` が前提）で、loopback の embedding service を併用した hybrid 検索に
なる。BM25 の置き換えではなく併用で、固有名詞・数字のような rare exact token は BM25 のほうが強い。

- model は別 process（`bocco-embeddings.service`、llama.cpp + multilingual-e5-small Q8_0、127.0.0.1:8646）。
  bridge の venv は 43 MB／third-party 2 個のままで、依存は一つも増えない。
- **latency は退行させない。** query embedding には hard deadline（default 120 ms）があり、超過・接続不能・
  不正応答・例外はすべて「今日と同じ BM25 の結果」に落ちる。3 回連続で失敗すると circuit breaker が 60 秒開き、
  service が落ちている間の追加コストは 1 reply あたりではなく 1 分あたり 3 回分になる。measured な
  retrieval path のコストは +3 ms（p50/p95 とも、Apple M5、500 turn）。
- 書き込み側は reply 送出後の detached task。single event worker は待たない。
- 検索は 500 turn × 384 次元の総当たり cosine（numpy も FAISS も使わない）と、BM25 との
  Reciprocal Rank Fusion（k=60）。順序は決定的。
- 時刻対応検索とは共存する。embed するのは日付語を除いた residue で、window が効いているときは sweep も同じ
  `created_at` 範囲に閉じる。保存側は時刻ラベル抜きで embed する（ラベルは描画時刻に相対で、毎晩古くなるため）。

### 既知の不具合: 時刻対応検索（「昨日なにした？」）

`transcript.py` の time-aware retrieval は **実機で期待どおりに動いていない**。「昨日」「さっき」などから
時間 window を解決する部分と、turn に付ける時刻ラベルの生成は正しい。壊れているのは window 内で何を選ぶかで、
実際にその日あったことではなく、robot 自身の過去の「わからない」という返答や、一般的で内容の薄い turn が
上位に来てしまう。2026-08-06 の実機テストで確認済み。conversational memory 自体が default off なので
出荷時の挙動には影響しないが、`BRIDGE_CONVERSATION_MEMORY=on` にした人には見える。修正は別途対応中。
- 既存 turn は `bridge/tools/backfill_embeddings.py` で埋める（resumable / idempotent / rate-limited）。

詳細と実機での手順は [docs/design/semantic-retrieval.md](docs/design/semantic-retrieval.md)。

## ADR

[docs/adr/README.md](docs/adr/README.md) に索引と書き方をまとめている。

- [0001: 宣言的 Pi image と cloud-init](docs/adr/0001-raspberry-pi-image.md)
- [0002: Node.js version policy](docs/adr/0002-nodejs-version-policy.md)
- [0003: Hermes Agent を採用](docs/adr/0003-hermes-agent.md)
- [0004: BOCCO 公式アプリを text interface に採用](docs/adr/0004-bocco-app-replaces-discord.md)

実機で speech、app text、motion echo、radar の Webhook shape と shared sender UUID を確認済み。motion、stamp、image など
non-speech media の null text は無言で checkpoint し、audio media の failed STT だけ固定の聞き返しを返す。redacted payload は
[`bridge/tests/fixtures/app_webhook_events.json`](bridge/tests/fixtures/app_webhook_events.json) に置く。

## Hermes Agent の導入

**この repository は Hermes Agent を install しない。** `scripts/install-services.sh` は
`hermes-api.service` を配置するが、その `ExecStart` が要求する `/opt/hermes-agent/.venv/bin/python`
を用意する処理はどこにもなく、欠けていれば preflight が警告するだけである。実機で動かした構成は
Hermes Agent **0.19.1**（`aiohttp==3.14.1` を含む `messaging` extra 付き）で、手順は次のとおり。

```sh
sudo install -d -o root -g root -m 0755 /opt/hermes-agent
sudo python3 -m venv /opt/hermes-agent/.venv
sudo /opt/hermes-agent/.venv/bin/pip install --upgrade pip
sudo /opt/hermes-agent/.venv/bin/pip install 'hermes-agent[messaging]==0.19.1'
/opt/hermes-agent/.venv/bin/python -c 'import aiohttp, hermes_cli.main'
```

version 固定は必須。`messaging` extra なしで入れると gateway は起動するのに API adapter が
提供されず、`no adapter available for api_server` で止まる。この install 手順自体は clean な Pi
で再実行していない未検証ステップで、`hermes/config.example.yaml` も provider/model block を
省いている（実機では `model.provider: openai-api` を足す必要があった）。詳細と回避策は
[docs/design/replication-guide.md](docs/design/replication-guide.md) の 6 章・10 章にある。

## License

[Apache License 2.0](LICENSE)。Copyright 2026 Yukai Engineering Inc.（ユカイ工学株式会社）。

依存関係の商用利用可否は [ONEPAGER.md](ONEPAGER.md) の「ライセンス / 商用利用の確認結果」に
まとめてある。copyleft の依存はなく、Apache-2.0 と衝突するものはない。
