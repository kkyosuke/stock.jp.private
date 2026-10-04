# 構成と正本

1つのリポジトリ、1つのPythonパッケージで管理する。個人開発のため、旧配置の互換層は設けない。
パスの定義は [`workspace.py`](../../src/stock_jp/workspace.py)、コマンドの定義は
[`cli.py`](../../src/stock_jp/cli.py) を正本とする。

```text
src/stock_jp/
  market_data/             取得、正規化、検証、保存・復元
  strategy/                銘柄・地合い・流動性・配分の判定
  operation/               日次run、台帳、注文票、承認・復旧証跡
  research/                履歴再生・実験
  automation/              GitHub連携、取得予算、PR、監視
  cli.py                   操作用の入口
  workspace.py             ルートと保存先
config/                    sources.json / policy.json / automation.json / acquisition.json
data/market/              採用済み応答・履歴価格・観測記録
operations/                state.json / source-watermarks.json / run-history.csv
  ledgers/                 資金・保有・売買・ルールレビュー
  inputs/                  watchlist・日次判断入力
  runs/                    run成果物
  decisions/               個別判断
  evidence/                承認・復旧・引退記録
  acquisition/             取得取引・消費予算
research/<experiment>/     入力定義・結果・README
templates/                初期値・CSVヘッダー・帳票の雛形
tests/                    オフライン検証
docs/                     現行構成・手順・ルール
  archive/                 過去の設計・レビュー・移行記録
.agents/skills/            操作ガイド
.github/workflows/         CI / 手動取得 / 保存済みrunの監視
.work/                     取得途中・復元・ステージング（Git対象外）
.cache/                    再生成できるキャッシュ（Git対象外）
```

## 責務

`market_data` は外部データを事実として保存する。投資判断は `strategy`、状態の更新と
人の承認の検証は `operation` が行う。`research` は実験結果を管理し、日次運用状態へ
直接書き込まない。`automation` は実行・公開を調整し、戦略や発注権限を持たない。

市場データから戦略の判定、日次run・台帳へ情報が流れる。自動化はそれぞれの公開入口を呼ぶ。
Pythonの依存関係とCLIのインストール定義は [`pyproject.toml`](../../pyproject.toml) に置く。
`package.json` はMarkdownの検査とGit hookだけを管理する。

## SSoT

| 対象 | 正本 | 更新方法 |
| --- | --- | --- |
| 保存先・ルート | `src/stock_jp/workspace.py` | コード変更 |
| 有効な取得設定 | `config/sources.json` | 設定変更 |
| 運用モード・上限・LIVE条件 | `config/policy.json` | 設定変更 |
| 手動取得の承認範囲 | `config/acquisition.json` | 人が確認した承認 |
| 既存取得の承認・予算根拠 | `config/automation.json` | 当時の承認を保持 |
| 対象日別の予約・消費枠 | `operations/acquisition/date-grants.json` | mainへ採用した予約。返金しない |
| 運用の現在値・日次履歴 | `operations/state.json`、`source-watermarks.json`、`run-history.csv` | 運用コマンド |
| 資金・保有・売買の事実 | `operations/ledgers/` | 台帳更新・照合 |
| 観測した財務・株数・予想・決算予定・企業行動 | `data/market/observations/` | 生応答から再生成する年別履歴と最新値 |
| 手動取得の生応答 | `data/market/manual-dates/<date>/` | 結果PRで採用 |
| 採用済みJ-Quants世代 | `data/market/jquants/current.json` と参照先 | 検証済みデータPR |
| 判断・承認の証拠 | `operations/decisions/`、`evidence/` | 証跡の追加 |
| 実験 | `research/<experiment>/` | 入力固定・再計算 |
| 初期化用の雛形 | `templates/` | 初期化時のみ参照 |
| 戦略の文章定義 | `docs/rules/tenbagger-rule-v0.4.md` | ルールレビュー |

資料には保存先と手順を示し、可変の上限・予算・運用状態を転記しない。
雛形の変更は既存の設定や台帳を上書きしない。過去の証跡のcommit、URL、ハッシュは
当時の観測値として保管し、現在のコードへの承認として読み替えない。

`data/market/legacy-prices/` は過去の日足と取得記録。実運用の価格入力は
`config/sources.json` が明示する `.work/market-data/` の復元済みデータに限定する。
取得不能時に過去の価格を自動で代用しない。
