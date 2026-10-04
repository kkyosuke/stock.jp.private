# 自動化と取得承認

GitHub Actionsの定義は `.github/workflows/`、新経路の取得許可は `config/acquisition.json`（承認後に追加）、既存承認は
[`config/automation.json`](../../config/automation.json)、引退の証跡は
[`operations/evidence/automation-retirement.json`](../../operations/evidence/automation-retirement.json) にある。

| Workflow | 責務 |
| --- | --- |
| `ci.yml` | `format`、`test`、`guardrail`。静的検証と合成データのテスト |
| `market-data-manual.yml` | ownerが対象日の予約と、承認されたattemptの取得を実行 |
| `operation-health.yml` | 保存済みrunメタデータを読み、監視結果をartifactへ保存 |

依存関係は `pyproject.toml` を参照する。CIと監視は採用済み市場データをcheckoutせず、
実データの準備完了を判定しない。submodule更新、Cloudflare scheduler、旧取得workflowは廃止した。

## 手動取得

```bash
python -m stock_jp automation readiness --help
python -m stock_jp automation acquire --target-date <YYYY-MM-DD>
```

手動取得は次の順で行う。

1. ownerが対象日を指定して起動し、予約専用draft PRを作る。この段階のprovider呼び出しは0件。
2. 予約内容とCIを確認してmainへ採用する。予約はrun ID、次のattempt、対象日、消費枠に固定される。
3. 同じrunの「Re-run all jobs」で予約されたattemptを実行する。
4. 各送信前にPENDING記録を保存し、応答を検証して結果専用draft PRを作る。
5. 結果を採用する。同日再実行は採用済み応答のhashを確認して取得を省略する。

正本は `operations/acquisition/date-grants.json`。実際の送信数と保守的な予約枠を区別する。
失敗、不明応答、branchの削除、別attemptへの変更でも枠を返却・再発行しない。
不完全な日足は生応答を保持するが、成功結果PRを作らない。状態は `RAW_COLLECTED` または
`INCOMPLETE`、分析readinessは常に `NOT_EVALUATED`。日次判断・注文には直接使わない。

承認のschemaは `automation/date_authorization.py` が定義する。新経路には、統合後のmain・
初期台帳・workflowに固定した `config/acquisition.json` と独立したowner承認が必要。
既存の `config/automation.json` と引退記録は過去の承認・消費の根拠として保持する。
このリファクタリングはscope、承認comment、secret、実行設定を生成・更新しない。

認証は既存の `AUTOMATION_PAT` と `JQUANTS_API_KEY`、実行設定は
`STOCK_ACTIONS_ENABLED` と `STOCK_ACTIONS_APPROVAL_SHA256` を使う。
公開前検証はbase/head、変更ファイル、検証結果、重複、mainの費用履歴を照合する。
旧checkpoint経路は手動workflowの入口から呼ばない。

## 監視

```bash
python -m stock_jp automation validate
python -m stock_jp automation health --persisted-only --output .work/operation-health.json
```

検査は一時領域で行い、設定・台帳・観測記録を変更しない。
監視の成功はLIVE許可にならない。
