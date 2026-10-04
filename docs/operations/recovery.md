# 復元と復旧証跡

コード、設定、台帳、採用済み市場データは同じprivateリポジトリから復元する。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m stock_jp data verify
.venv/bin/python -m stock_jp data restore
.venv/bin/python -m stock_jp automation validate
.venv/bin/python -m stock_jp operation readiness check --at <JST日時>
```

認証情報はリポジトリに含めない。実データが不足している場合は、検証結果に従って補う。
古い取得キャッシュを検証せずに使わない。

```bash
python -m stock_jp operation recovery --help
```

復旧訓練は実際にcloneしたcommit、コードツリー、台帳のハッシュ、採用済み世代、
復元データ、選択したrunの検証結果を記録する。正本は
`operations/evidence/recovery-drill.json`。配置版は `workspace.py` が定義する。

過去の復旧証跡は当時の記録として残す。配置変更後の条件を満たすには、新しい配置で
訓練を行う必要がある。過去の成功フラグやハッシュを新しいコードに書き換えない。
