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
3. 完了したデータをpack・検証し、データと取得完了記録だけのPRを作る。
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

## 手動実行・再開

Actionsの「Nightly market data」で対象日を指定する。未指定なら実行時のJST日付。
未完了の対象日がある場合は先にその日を再開する。J-Quantsの認証失敗は
`JQUANTS_API_KEY` を修正し、GitHubの権限不足はworkflowのpermissionsとrepositoryの
PR作成許可を確認してから、対象日を指定して手動実行する。消費記録を消して再試行しない。

`preview=true` は取得・checkpoint保存・世代の検証後に、検証用draftデータPRを作って
CIを確認する。mainへ採用しない。修正branchでもownerが手動実行できる。
検証用branchは `daily/market/<日付>-preview`。確認後、draft PRを閉じる。
previewでも実際の通信は消費として残り、本番の再実行で完了済み取得を再利用する。

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
