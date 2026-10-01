# private 運用手順

現在、両repoは非公開・本人限定です。publicという旧称は共通コード・データrepoのstock.jpを指します。
[現行の境界](../repository-layout.md)を優先します。

## 1. setup

private repository の root で実行します。

~~~bash
git submodule update --init --recursive
./scripts/setup-workspace.sh
python3 -m venv stock.jp/.venv
stock.jp/.venv/bin/pip install 'xlrd==2.0.2' 'openpyxl==3.1.5' 'pypdf==6.16.1' 'certifi==2026.7.22' PyYAML
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py migrate
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
~~~

credential は shell history に値を残す引数で渡しません。必要な場合だけ実行環境の変数へ設定します。

### providerと非公開保存

J-Quantsの契約範囲・安全な資格情報登録を確認後、別途provider実装と取得を有効化します。
このstorage変更では`source-config.json`のproviderとenabled flagを変更しません。
Yahoo bulk取得を停止します。credentialはチャットやGitへ保存せず、認可された実行時の
秘密情報管理経路で設定します。

raw市場データとmanifestは`operations/private/market-data/daily-prices/`へ保存し、
作業領域としてGit除外します。圧縮正本は非公開stock.jpの`data/jquants-archive/`へ保存します。
個人分析・選定・状態・台帳はstock.jp.privateへ置きます。旧archiveへのfallbackはありません。
[非公開保存・移行手順](private-market-data-storage.md)の取得条件と環境確認が未完了なら
全市場価格の処理はWAIT / NO-ACTIONとします。会社IR・TDnet・JPX等の一次資料調査は
独立して続けられますが、価格欠損を推測で埋めず、nightly完了やLIVE合格を宣言しません。
既存archiveの移動・削除、Git履歴の扱いは利用条件を確認したうえで別途決定します。

## 2. public update の採用

汎用コード・ルール・合成fixtureをpublic側の通常reviewで確定し、
merge済みpublic `main`のcommitだけを採用します。raw市場データはpublicへ公開しません。Actionsによる収集、同期PR、auto-mergeはありません。
[日次実行契約](assistant-daily-operation.md)と[submodule採用手順](automatic-submodule-update.md)を参照します。

code・rule・workflow変更は日次data差分と分けてreviewします。既存のdata-only validatorを
迂回しません。この構成変更のpublic commitも通常review/merge後、明示的なpointer PRで採用します。

~~~bash
git -C stock.jp fetch origin main
git -C stock.jp switch --detach origin/main
git diff --submodule=log -- stock.jp
~~~

この例はcheckoutの準備です。検証、private整合性確認、reviewを省略する承認ではありません。
未mergeのpublic branch tip、未pushのprivate run、未解消競合では新しいrunを開始しません。

## 3. 夜間 run 前

1. 別端末に未pushの runがないことを確認する
2. `git pull --ff-only` と `git submodule update --init` を実行する
3. `./scripts/setup-workspace.sh` を実行する
4. state/ledger validate を実行する
5. 設定済みprivate archiveのmanifest、日付、coverage、checksumを確認する
6. `operation_bootstrap.py check` の `paper_go` または `live_go` を確認する

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
stock.jp/.venv/bin/python stock.jp/scripts/nightly_operation.py status
~~~

PAPER は `paper_go: true`、LIVE は `live_go: true` が必要です。LIVE への昇格前は `operation_mode: PAPER` と `broker_submission: HUMAN_ONLY` を維持します。

### LIMITED_LIVE

少額限定実運用は3,000,000円の専用資金枠で行い、通常LIVEと分離します。
`limited-live-plan.json`の累積損失停止率と本人項目を確定し、公式coverage、
layout revision 3のclean-clone、個人checklistが合格した後だけ次を実行します。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/limited_live.py --root stock.jp status
stock.jp/.venv/bin/python stock.jp/scripts/limited_live.py --root stock.jp apply
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
~~~

資金を実際に分離した後、`apply`より前に`capital-ledger.csv`へ
`event_type=LIMITED_LIVE_FUNDING`を1件だけ追記します。`amount_private`と
`running_cash_private`はどちらも`3000000`とし、予定額や他戦略と混在した残高は記録しません。
資金記録がない、一致しない、または複数ある場合は昇格と新規買付を停止します。

`limited_live_go: true`を確認します。新規注文は1 run 1件、追加購入は禁止、発注は
必ず本人の手入力です。損失停止、公式source不足、MRS不明、assessment未完了では
新規購入を作らず、必要な縮小・売却を優先します。

## 4. run と commit

日次の全実行責務と失敗時の扱いは[アシスタント日次実行契約](assistant-daily-operation.md)に従います。
start/finalizeだけを連続実行せず、一次資料調査と全対象の成果物を完了してからfinalizeします。

`$japan-stock-operator` の汎用手順に従い、1つの run token を完了または失敗まで使います。注文候補は PAPER では `PAPER_PROPOSED`、LIVE でも `PROPOSED` に留め、broker へ自動送信しません。

run 後に次を照合します。

- `state.json` と `run-history.csv`
- `handoff.json` と未完了review
- `orders.csv` と `unreconciled_ticket_ids`
- trade/capital/portfolio ledger の数量と残高
- source coverage と data gap
- `assessment-status.json`と会社別の希薄化・SAM/SOM・10倍経路。`INCOMPLETE`/`STALE`なら不足項目を一次資料から`research-inputs/investment-cases/<code>.json`へ補う
- 変更した private file と public submodule commit

実行の成功・失敗を private Git の1 commitへ記録し、push成功後に次の端末・runへ進みます。認証情報、一時 download、巨大な raw data が混入していないことを diff で確認します。

## 5. 競合

state や ledger の競合で片側を機械的に採用しません。

1. 両 branch の run ID、run token、source cutoff を一覧化する
2. order proposal、fill、cancel、expire を event ID 単位で照合する
3. portfolio と capital を event ledger から再計算する
4. 未照合 ticket と次回 handoff を統合する
5. validate と readiness を再実行する
6. 解消根拠を private review logへ残す

同じ口座を複数端末から同時運用しないことを基本とします。

## 6. point-in-time履歴再生

2025〜2026の入力ひな形は`operations/private/historical-replay/2025-2026/`に置きます。これは
`DRAFT`で、データを自動取得せず、空のまま合格しません。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/historical_replay.py \
  --root stock.jp \
  --manifest operations/private/historical-replay/2025-2026/input-manifest.json \
  validate
~~~

公式データの利用可否を本人が確認した後、source register、期間全暦日の取引日カレンダー、当時点の
銘柄master、全営業日×全有効銘柄の非調整OHLCV、corporate action、決算・重大開示event、
全月末×全母集団および各event期限のassessment、MRS、TOPIXを投入します。manifestのhash、
provider、`official`、`replay_authorized`と8つの
certificationを更新し、すべて事実なら`READY`にします。J-QuantsまたはYahooのprovider、欠損、
look-ahead、未確認の上場廃止精算価格はvalidatorが拒否します。

validate成功後だけ次を実行します。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/historical_replay.py \
  --root stock.jp \
  --manifest operations/private/historical-replay/2025-2026/input-manifest.json \
  run
~~~

2025〜2026結果は回顧的stress testです。本人がその成績と制約を
`historical-replay-review.json`で受け入れても、v0.4のforward holdoutを代替しません。forward
holdoutは`v04-holdout-plan.json`へ未観測期間と基準を先に記入し、5つのacknowledgementを確認して
`FROZEN`にしてから同じ再生器で開始します。期間開始後に規則・入力・基準を変えた場合は、新しい
未観測期間で再開始します。

## 7. clean-clone 確認

LIVE昇格前に一度、稼働中とは別の空ディレクトリで実施します。setup、submodule、状態schema、台帳構成を変えた場合はpublic validatorの `REPOSITORY_LAYOUT_REVISION` を上げ、変更後に再実施します。日次run、データ更新、経過日数だけでは再実施しません。

1. 通常のprivate repositoryをcloneする
2. submodule と symlink を setup する
3. Python environment を再構築し、利用条件と承認済み手順に従ってprivate local archiveを別途準備する
4. state/ledger validate と readiness check を実行する。Git cloneだけではraw archiveは復元されない
5. 最新成功 run、handoff、未照合ticket、全ledgerを稼働中と照合する
6. layout revision、private/public commit、実施日時、結果、差異をevidenceに残す

別媒体のmirrorは要求しません。稼働中のdirectoryを上書きしません。clean cloneできない間、またはGitHubを利用できない間は、PAPER/LIVEとも新しい注文候補を作りません。

今回のLIMITED_LIVE policy schemaと証跡追加によりlayout revisionは3です。過去の既存証跡を編集して
流用せず、変更をcommit・pushした後に`REPOSITORY_LAYOUT_CHANGE`として再実施します。

## 8. access control

- repository visibility を Private に固定する
- collaborator と GitHub App を定期reviewする
- GitHub account と broker account で強い固有passwordとMFA/passkeyを使う
- broker site はbookmarkから開き、URLを確認する
- 不審なlogin、注文、出金を確認したら取引を止めてbrokerへ連絡する

口座securityは strategy code の gate とは別に、[personal risk and broker check](../live-readiness.md#personal-risk-and-broker-check)の証跡へ記録します。

## 9. 公式参照

- [金融庁: インターネット取引サービスへの不正アクセス・不正取引への対策](https://www.fsa.go.jp/ordinary/chuui/chuui_phishing.html)
- [JPX: 内国株の売買制度・売買時間](https://www.jpx.co.jp/equities/trading/domestic/01.html)
- [JPX: 休業日一覧](https://www.jpx.co.jp/corporate/about-jpx/calendar/)
- [JPX: 株価情報（過去分）](https://www.jpx.co.jp/markets/statistics-equities/daily/03.html)
- [JPX: JPxData Portal](https://www.jpx.co.jp/markets/data-catalog/)
