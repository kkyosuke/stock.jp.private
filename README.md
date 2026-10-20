# stock.jp

日本株の調査、戦略の検証、日次運用を管理する個人用のprivateリポジトリです。
旧 `stock.jp/` のコード・資料・データを統合し、Pythonパッケージを1つにしました。
後方互換の入口やsubmoduleは使いません。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m stock_jp --help
.venv/bin/python -m stock_jp automation validate
```

通常はリポジトリルートから実行します。別のチェックアウトを対象にする場合は
`python -m stock_jp --root /path/to/checkout ...` を使います。

- [構成と正本](docs/architecture/repository-layout.md)
- [市場データの保存・取得](docs/operations/market-data.md)
- [日次運用](docs/operations/runbook.md)
- [自動化](docs/operations/automation.md)
- [復元](docs/operations/recovery.md)
- [LIVE判定](docs/operations/live-gates.md)
- [資料一覧](docs/README.md)

現在の設定・取得許可は `config/`、運用状態・台帳は `operations/` が正本です。
CIの成功は実データの鮮度やLIVE許可を意味しません。発注は `HUMAN_ONLY` です。

ローカルの全テストは `.venv/bin/python -m unittest discover -s tests -t .` で実行します。
CIはGitの全差分から検証内容を選びます。市場アーカイブ・観測CSV・夜間取得記録だけの
変更では、保存データのハッシュ・世代・スキーマ・取得記録・観測ビューを検証します。
通常のコードPRでは変更前後の依存関係から回帰テストを選び、compile・20日smokeも実行します。
設定・共有入口・判定できない変更は通常回帰を全件実行します。上限規模を確認する4件は週次へ移します。
全テストは週1回と手動実行に残し、mainへのmerge直後にはCIを重ねて起動しません。
通常の資料だけの変更は文書の契約テストを実行します。凍結ルールの変更は通常のコード検証です。
書式・テスト・PAPER検証は、既存の必須チェック `format` の1ジョブで実行します。
使用分数の月間見積もりと再試行方法は[自動化](docs/operations/automation.md#actionsの使用分数)を参照してください。
