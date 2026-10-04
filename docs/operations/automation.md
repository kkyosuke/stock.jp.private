# 自動化

GitHub Actionsの入口は `.github/workflows/`、取得設定は
[`config/sources.json`](../../config/sources.json)、取得・保存処理は
[`automation/nightly.py`](../../src/stock_jp/automation/nightly.py) が正本。

| Workflow | 責務 |
| --- | --- |
| `ci.yml` | `format`、`test`、`guardrail`。静的検証と合成データのテスト |
| `market-data-manual.yml` | 毎日21:00 JSTに市場データを取得・検証・保存。手動再実行も同じ入口 |
| `operation-health.yml` | 保存済みPAPER runメタデータの監視結果をartifactへ保存 |

依存関係は `pyproject.toml` を参照する。夜間取得は実行commitと採用済み市場データを
checkoutする。CIと監視は市場データをcheckoutせず、実データの準備完了を判定しない。

## 夜間取得

privateリポジトリのownerが管理するmainで動かす。GitHub Secretの
`JQUANTS_API_KEY` と、実行時だけ発行される `GITHUB_TOKEN` を使う。
WorkflowがContents・Pull requests・Actionsの書き込み、Checksの読み取り権限を指定する。
取得前にprivate・owner・書き込み権限を確認する。
取得の有効化は `config/sources.json` の `market_data_git.automatic_upload` で管理する。

1. 採用済み世代を復元・検証し、通信前に消費記録をcheckpoint branchへ保存する。
2. Light契約の公開時刻に従って日足・master・財務・カレンダーなどを取得する。
3. 財務・株数・予想・決算予定・価格調整の観測CSVを更新し、検証した保存世代と取得完了記録のPRを作る。
4. 一時トークンで作ったPRはCIを自動起動しないため、同じbranchのCIを明示的に起動する。
5. PRのheadで `format`、`test`、`guardrail` が全て成功した後、同じheadをsquash mergeする。

J-Quantsは直列・最低1.05秒間隔、日付別に最大12回。失敗・不明応答も消費に含め、
再実行で予算をリセットしない。過去の `date-grants.json` の日付別消費も引き継ぐ。
取引がない日にはカレンダー等を確認し、架空の日足を作らない。

消費記録と取得途中の応答は `automation/nightly-market-data` branchに保存する。
削除すると予算の根拠が失われるため、削除しない。途中でmainが更新された場合、
認証失敗、429、不正な応答、予算不足、CI失敗の場合は処理を停止し、保存済み状態を残す。
実行結果は `nightly-market-data-<run ID>` artifactのJSONで確認する。

## 手動実行・再開

Actionsの「Nightly market data」で対象日を指定する。未指定なら実行時のJST日付。
未完了の対象日がある場合は先にその日を再開する。認証失敗はSecretを修正してから
対象日を指定して手動実行する。消費記録を消して再試行しない。

`preview=true` は取得・checkpoint保存・世代の検証後に、検証用draftデータPRを作って
CIを確認する。mainへ採用しない。修正branchでもownerが手動実行できる。
検証用branchは `daily/market/<日付>-preview`。確認後、draft PRを閉じる。
previewでも実際の通信は消費として残り、本番の再実行で完了済み取得を再利用する。

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
