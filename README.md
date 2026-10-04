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
