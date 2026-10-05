# 自動化

GitHub Actionsの入口は `.github/workflows/`、取得設定は
[`config/sources.json`](../../config/sources.json)、取得・保存処理は
[`automation/nightly.py`](../../src/stock_jp/automation/nightly.py) が正本。

| Workflow | 責務 |
| --- | --- |
| `ci.yml` | `format`、`test`、`guardrail`。静的検証と合成データのテスト |
| `market-data-manual.yml` | 毎日21:00 JSTに市場データを取得・検証・保存。21:20に再試行。取得後に一次資料収集とPAPER調査準備 |
| `operation-health.yml` | 保存済みPAPER runメタデータの監視結果をartifactへ保存 |
| `forward-holdout.yml` | 22:30 JSTに凍結したPAPER研究と採用済みデータを読み取り、不足・実装変更をartifactへ保存 |

依存関係は `pyproject.toml` を参照する。夜間取得は実行commitと採用済み市場データを
checkoutする。CIと運用監視は市場データをcheckoutせず、実データの準備完了を判定しない。
forward holdoutの観測は採用済みデータもcheckoutする。正式入力が不足した開始日以降は
失敗として記録するが、市場データの取得・保存・本人のLIVE承認は行わない。
固定した条件と現時点の不足は[正式検証の記録](../../research/formal-validation-2026-10-05/README.md)を参照する。

## 夜間取得

privateリポジトリのownerが管理するmainで動かす。GitHubへの保存・PR作成・mergeと
CI起動にはActionsが発行する一時的な `GITHUB_TOKEN` を使い、`AUTOMATION_PAT` は不要。
夜間workflowだけに `contents: write`、`pull-requests: write`、`actions: write` と
CI結果確認用の `checks: read` を付与する。CIと監視の権限は読み取り専用のまま。
repositoryの `Settings > Actions > General > Workflow permissions` で
`Allow GitHub Actions to create and approve pull requests` を有効にする。
GitHub Secretにはデータ取得用の `JQUANTS_API_KEY` だけを登録する。
取得の有効化は `config/sources.json` の `market_data_git.automatic_upload` で管理する。

1. 採用済み世代を復元・検証し、通信前に消費記録をcheckpoint branchへ保存する。
2. Light契約の公開時刻に従って日足・master・財務・カレンダーなどを取得する。
3. 財務・株数・予想・決算予定・価格調整の観測CSVを更新し、検証した保存世代と取得完了記録のPRを作る。
4. データbranchを指定して `ci.yml` を明示dispatchし、返されたrun IDを追跡する。
5. そのrunが同じPR headで実行され、対応するcheck suiteの `format`、`test`、`guardrail`
   が全て成功した後、同じheadをsquash mergeする。

`GITHUB_TOKEN` で作成したPRのCIは承認待ちになるため、自動の `pull_request` 起動に
依存しない。`workflow_dispatch` は標準トークンでも起動できる。
詳細は[GitHubのトークン仕様](https://docs.github.com/en/actions/concepts/security/github_token#when-github_token-triggers-workflow-runs)を参照する。
dispatch失敗、run ID不明、対象headの不一致、CI失敗・未完了ではmergeしない。
標準トークンのAPI上限に合わせた通信予算は
[`automation/rate.py`](../../src/stock_jp/automation/rate.py) で管理し、再実行にも引き継ぐ。

J-Quantsは直列・最低1.05秒間隔、日付別に最大12回。失敗・不明応答も消費に含め、
再実行で予算をリセットしない。過去の `date-grants.json` の日付別消費も引き継ぐ。
取引がない日にはカレンダー等を確認し、架空の日足を作らない。

消費記録と取得途中の応答は `automation/nightly-market-data` branchに保存する。
削除すると予算の根拠が失われるため、削除しない。途中でmainが更新された場合、
認証失敗、429、不正な応答、予算不足、CI失敗の場合は処理を停止し、保存済み状態を残す。
実行結果は `nightly-market-data-<run ID>` artifactのJSONで確認する。

一時トークンからのイベントの扱いは
[GitHubのWorkflow起動仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
を参照する。

## 手動実行・再開

Actionsの「Nightly market data」で対象日を指定する。未指定なら実行時のJST日付。
未完了の対象日がある場合は先にその日を再開する。J-Quantsの認証失敗は
`JQUANTS_API_KEY` を修正し、GitHubの権限不足はworkflowのpermissionsとrepositoryの
PR作成許可を確認してから、対象日を指定して手動実行する。消費記録を消して再試行しない。

ownerの手動再実行では、公開前に作った`WAIT_PUBLICATION`について、復元したjournalと
checkpointがともに要求0・task0・過去消費0である場合だけ現行設定へ接続できる。
予算は変えず、旧状態と新設定のハッシュを`jquants/rebindings/`へ保存する。
要求・取得結果があるrunや定時実行は設定変更を越えて再開しない。

`preview=true` は取得・checkpoint保存・世代の検証後に、検証用draftデータPRを作って
CIを確認する。mainへ採用しない。修正branchでもownerが手動実行できる。
検証用branchは `daily/market/<日付>-preview`。確認後、draft PRを閉じる。
previewでも実際の通信は消費として残り、本番の再実行で完了済み取得を再利用する。

## 一次資料とPAPER調査準備

市場データjobが成功した後、読み取り権限の`paper-research` jobが最新mainを復元する。
企業IR・VI・景気統計の取得元は`operations/inputs/primary-sources.json`、原本保存は
`market_data/primary_sources.py`、取得記録は`research/forward_collection.py`が管理する。
公開資料も直列・最低1.05秒間隔、1回最大60要求。J-Quantsの取得とは別の消費記録である。
J-Quantsを使うsource scanは既存の最低1.05秒間隔・最大12要求とpage checkpointを使う。
同じworkflowのconcurrencyで市場データ取得と調査準備の同時実行を防ぐ。

```bash
python -m stock_jp.research.forward_collection --output .work/paper-research/collection.json
python -m stock_jp.operation.paper_preparation --output .work/paper-research/preparation.json
```

`paper_research=true`の手動実行は市場データ取得を省略して、この2処理だけを実行する。
PR branchでの確認は`preview=true`も指定する。Secretsは既存の`JQUANTS_API_KEY`と
`EDINET_API_KEY`を使う。PAPER/HUMAN_ONLYを必須とし、本人の承認は生成しない。

準備処理は`operation run start`と同じ入口で実データを調べ、未完了のレビューを
保存する。`REVIEW_REQUIRED`はexit 1であり、成功した開示cutoffやholdout回数を進めない。
調査・判断が終わるまで`run finalize`を呼ばない。取得job成功と研究jobの未完了は
Actionsのjobごとに確認する。

`paper-research-<run ID>` artifactはレビュー用の受け取り口で、400日保存する。
run・原本・forward記録・J-Quants page checkpointを含む。取り込み後に再検証し、
privateのPRで採用したGitの入力と結果を正本にする。artifactの保存だけで採用済みにはしない。

## 財務履歴の初回補完

同じ「Nightly market data」を手動実行し、`target_date=2026-10-02`、
`financial_history=true` を指定する。修正branchで確認する場合は `preview=true` も指定する。
Light契約で取得できる2021-10-02〜2026-10-02の財務サマリーと決算予定をbulkで補完する。
株数と業績予想は財務サマリーから同じ変換処理で抽出する。

要求は直列・最低1.05秒間隔、最大256回。失敗も消費に含め、再開でリセットしない。
既存の日足履歴の600回予算は変更しない。nightlyと同じconcurrency groupで同時取得を防ぐ。
生CSVのgzip・サイズ・ハッシュを検証し、解析前に保存する。
同じ対象日で再実行すると保存済みファイルを再利用する。

中間保存先は `automation/financial-history` branch、結果と入力は
`financial-history-<run ID>` artifact。branchは消費記録を含むため削除しない。
初回補完はmainへ自動採用せず、artifactの `market-data/jquants/financial-history/` を
`.work/market-data/jquants/financial-history/` に取り込み、
`data observations`、`data stage`、`data verify` を通してprivateのPRで採用する。
採用後の日次追加は通常の夜間処理が行う。

この入口は市場データの取得を担当する。調査・売買判断・PAPER runの完了は別工程で、
取得結果の `analysis_readiness` は `NOT_EVALUATED`。
運用は `PAPER` / `HUMAN_ONLY` を維持する。

旧 `config/acquisition.json`、`config/automation.json` と引退記録は過去の承認・消費の
証跡として保持する。夜間入口は旧deployment pinや `STOCK_ACTIONS_*` 変数を使わない。

## 監視

```bash
python -m stock_jp automation validate
python -m stock_jp automation health --persisted-only --output .work/operation-health.json
```

監視は保存済み運用記録を読み、日次runの未完了を報告する。
市場データの取得成功だけで未完了の運用記録を成功扱いに変更しない。
