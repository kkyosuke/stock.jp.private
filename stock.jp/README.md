# stock.jp

日本株戦略の再現可能なコード、共有市場データ、ルール、運用仕様を管理するstock.jp.private内の通常フォルダです。情報収集・分析・PAPER処理はチャットのアシスタントが担当し、GitHub Actions は合成fixtureの検証・回帰テストだけを行います。個人の保有数量、資金、注文、判断記録は含めません。

## リポジトリの責務

| ディレクトリ | 内容 |
| --- | --- |
| `.github/workflows/` | 移行元の無効なコピー（有効なCIはrepo root） |
| `.agents/skills/` | 個人情報を含まない運用手順 |
| `data/` | 既存検証データと本人専用の圧縮市場データ |
| `docs/architecture/` | 共通データ・個人分析の境界と全体設計 |
| `docs/operations/` | PAPER/LIVE の運用仕様 |
| `docs/rules/` | 凍結した戦略ルール |
| `docs/research/` | 検証結果と再生成手順 |
| `operations/templates/` | 秘密情報を含まない台帳テンプレート |
| `scripts/`, `tests/` | 実装と回帰テスト |

実運用では単一private repo内の通常フォルダとして管理し、`stock.jp/operations/private` を private 側の `operations/private` へ接続します。暗号化アーカイブは使わず、private Git の履歴と現行構成の clean-clone 確認を運用証跡にします。詳細は[リポジトリ境界](docs/architecture/repository-boundaries.md)を参照してください。

## はじめに

~~~bash
python3 -m venv .venv
.venv/bin/pip install 'xlrd==2.0.2' 'openpyxl==3.1.5' 'pypdf==6.16.1' 'pdfplumber==0.11.8' 'certifi==2026.7.22' PyYAML
.venv/bin/python -m unittest discover -s tests -v
~~~

初回状態を作る場合は private リポジトリから実行してください。単独で PAPER 用の一時環境を試す場合だけ、`operations/private/` をローカルに作成して次を実行します。

~~~bash
.venv/bin/python scripts/operation_state.py migrate
.venv/bin/python scripts/operation_state.py validate
.venv/bin/python scripts/operation_bootstrap.py check
~~~

`paper_go: true` になるまで PAPER を開始せず、`live_go: true` になるまで実注文を行いません。コードは注文候補を作りますが、証券会社へ自動送信しません。

## ドキュメント

- [アシスタントの株価収集とActionsガードレール](docs/operations/daily-stock-price-actions-v0.1.md)
- [ドキュメント索引](docs/README.md)
- [共通データ・個人分析の境界](docs/architecture/repository-boundaries.md)
- [運用ガバナンス](docs/operations/operation-governance-v0.1.md)
- [LIVE 運用手順](docs/operations/live-operation-playbook-v0.1.md)
- [MRS・希薄化・SAM/SOM・10倍経路の自動計算](docs/operations/decision-assessment-automation-v0.1.md)
- [PAPER/LIVE 準備判定](docs/operations/operation-readiness-v0.1.md)
- [現行 PAPER ルール v0.4](docs/rules/tenbagger-rule-v0.4.md)
- [検証用データと再生成方法](data/README.md)

## セキュリティ境界

公開 Issue、PR、Actions artifact、ログへ、口座情報、API キー、保有数量、取得原価、資金額、注文 ID、実運用の判断記録を出力しないでください。秘密情報を検知した場合は履歴からの除去と認証情報の失効を優先します。

## 市場データの非公開保存

新規raw市場データはGit除外の作業領域で取得し、明示設定・本人限定アクセス確認後、repo root基準の `stock.jp/data/jquants-archive/` へ圧縮保存します。指定が無い場合や欠損時は停止します。CIはコード・合成fixtureだけを検証します。Yahoo一括collectorは停止し、J-Quants V2の全契約対象データを履歴・差分・訂正ごとに非公開保存する実装を用意しています。実取得には確認済みプランと実行環境への安全な資格情報設定が必要です。[J-Quants取得手順](docs/jquants-private-ingestion.md)を参照してください。[設定・移行手順](docs/operations/daily-stock-price-actions-v0.1.md)を参照してください。既存publicデータ・履歴の削除は今回行いません。
