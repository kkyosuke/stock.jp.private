# point-in-time履歴再生 v0.1

## 目的と境界

`historical_replay.py`は、privateへ手動投入した利用可能な公式一次データから、全月末・全銘柄の
point-in-time入力を検証し、同じ候補・約定機会でv0.2とv0.4を再生する。データのdownload機能は
持たず、J-QuantsとYahoo等の非公式価格は正式replay入力として拒否する。

JPXの無料「株価情報（過去分）」は、特定日の個別確認向けで、手動取得が基本とされ、掲載ファイルの
転載が禁止されている。このため自動bulk取得やpublic Gitへの保存は行わない。使用する場合は利用目的と
保存方法が許されることを本人が確認し、raw fileをprivateの
`operations/private/historical-replay/<period>/raw/`へ置く。このraw領域はGit無視対象とする。正規化入力を
private Gitへ保存できるのは、その保存と再生がsourceの利用条件で許される場合だけであり、許されなければ
入力もlocal-onlyとし、証跡にはsource情報とhashだけを残す。

- [JPX: 株価情報（過去分）](https://www.jpx.co.jp/markets/statistics-equities/daily/03.html)
- [JPX: 東証上場銘柄一覧](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html)
- [金融庁: EDINET](https://disclosure2.edinet-fsa.go.jp/)

## 初期化

2025〜2026の回顧的stress test用の空bundleは次で作る。既存directoryは上書きしない。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/historical_replay.py \
  --root stock.jp init \
  --kind RETROSPECTIVE_STRESS_TEST \
  --from 2025-01-01 \
  --through 2026-08-31
~~~

生成されるmanifestは`DRAFT`であり、空入力のまま合格しない。全datasetを投入し、hash、provider、
`official`、`replay_authorized`、8つのcertificationを確認してから`READY`へ変更する。

## 入力契約

入力は`operations/private/historical-replay/<period>/`以下に置く。

| role | 必須内容 |
| --- | --- |
| `source_register` | source ID、provider、公表日時、公式URL、内容hash、利用可否 |
| `security_master` | 永続issue ID、code、market、sector、有効期間、上場・廃止・合併・code変更 |
| `trading_calendar` | 期間内の全暦日、東証営業日か否か、当時の既知日時 |
| `daily_prices` | 全営業日×全有効銘柄のOHLCV、売買代金、取引可否、利用可能日時 |
| `corporate_actions` | 分割・併合、現金/株式合併、code変更、上場廃止、公表日時、廃止時の公式終端精算価格、端数処理価格 |
| `review_events` | 決算・重大開示のevent ID、発生日、review期限、QUARTERLY/IMMEDIATE区分 |
| `assessments` | 全月末×全銘柄のhard gate、100点score、SAM/SOM、逆算、追加・出口判定 |
| `market_regime` | MRS-v0.1の状態・倍率と当時点source |
| `benchmark` | 全営業日のTOPIX終値と利用可能日時 |

価格は`AS_TRADED_UNADJUSTED`で統一し、数量と取得単価をcorporate actionで接続する。分割・併合・株式合併で
端数が生じる場合は、`fractional_cash_price`へ一次資料で確認した端数処理価格を入れる。上場廃止は
`cash_consideration`へ一次資料で確認した終端精算価格を入れ、0や推測値なら停止する。後日修正された
最新値を過去へ戻さない。assessmentの`latest_source_published_at_jst`または登録sourceの公表日時が
判断日時より後ならlook-aheadとして停止する。

取引日カレンダーは期間内の休日を含む全暦日を持つ。benchmarkを営業日定義の代用にはせず、営業日の
benchmark欠損と非営業日のbenchmark混入をどちらも停止する。

月末assessmentは`COMPLETE_PASS`または`COMPLETE_FAIL`のどちらかを全銘柄に記録する。
`INCOMPLETE`や行自体の欠損は、除外ではなくhard-gate入力欠損としてreplay全体を停止する。
100点scoreは市場余地、逆算、その他の3成分と一致しなければならない。`review_events`にある
決算・重大開示は、指定期限の`QUARTERLY`または`IMMEDIATE` assessmentがなければ停止する。
JSONLの各行は
[`historical-replay-assessment-template.json`](../../operations/templates/historical-replay-assessment-template.json)
を列契約として使い、templateそのものやplaceholderを入力に混ぜない。

## 再生する規則

- hard gate、70点・市場8点・逆算10点、同点優先順位
- 翌営業日の115%指値、100株単位、手数料、slippage、流動性参加率
- MRS倍率、初回・追加、1銘柄・候補群・業種・保有数上限
- S-A、S-B、S-C1〜C6、S-D1〜D6、四半期連続判定
- 分割・併合、現金/株式合併、code変更、上場廃止、売買停止、未約定、63営業日再購入禁止
- 費用控除後NAV、最大DD、単一銘柄・業種の最大損失寄与、TOPIX比較

S-C2/C4/C5で再採点が必要な場合は、該当日に`MILESTONE` assessmentがなければ停止する。
出口を有利に省略したり、取引不能日を前日価格で約定したことにしない。

## validateとrun

~~~bash
replay_manifest=operations/private/historical-replay/2025-2026/input-manifest.json
stock.jp/.venv/bin/python stock.jp/scripts/historical_replay.py \
  --root stock.jp \
  --manifest "$replay_manifest" \
  validate

stock.jp/.venv/bin/python stock.jp/scripts/historical_replay.py \
  --root stock.jp \
  --manifest "$replay_manifest" \
  run
~~~

成功時だけprivate outputへpoint-in-time manifest、universe検証、quality、trade log、日次metrics、
月次return、v0.2/v0.4比較を保存する。すべてSHA-256で相互拘束される。

## 回顧的replayとforward holdout

2025-01-01〜2026-08-31は`RETROSPECTIVE_STRESS_TEST`、`holdout_claimed: false`とする。
これは本人が履歴再生成績とデータ制約を受け入れるための結果であり、v0.4の予測性能holdoutではない。

v0.4昇格用の正式holdoutは、v0.4凍結後かつ未観測の期間について、本人が先に
`v04-holdout-plan.json`を`FROZEN`へする。期間、最低月次評価数、最低取引数、最大DD・単一銘柄・
業種損失のfloor、入力・約定規則を結果を見る前に固定する。変更した場合は新しい期間で最初から行う。

forward bundleも同じ`init` commandで作成できる。periodはplanと一致させる。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/historical_replay.py \
  --root stock.jp init \
  --kind FORWARD_HOLDOUT \
  --from <FROZEN_PLANより後の未観測日> \
  --through <固定終了日>
~~~
