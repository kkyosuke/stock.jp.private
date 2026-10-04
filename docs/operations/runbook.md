# 日次運用

`config/policy.json` と `config/sources.json` を確認し、必要な市場データを
[復元](market-data.md)してから実行する。

```bash
python -m stock_jp operation state validate
python -m stock_jp operation policy validate
python -m stock_jp operation readiness check --at <JST日時>
python -m stock_jp operation run start --at <JST日時> --cutoff <JST日時>
```

`start` はrunを作り、観測・判断入力・注文票を準備する。開示のcutoff、価格日、
ルール版を固定し、実際に確認した一次資料を記録する。watchlistと人の判断入力は
`operations/inputs/`、生成物は `operations/runs/<run_id>/` に置く。

```bash
python -m stock_jp operation decision --help
python -m stock_jp operation orders --help
python -m stock_jp operation run finalize --help
python -m stock_jp operation run status
```

未確認の入力を承認済みにしない。最終報告・handoff・注文票を照合し、完了したrunだけを
`operations/run-history.csv` と `state.json` へ反映する。
同じrunの再開は既存のattempt・チェックポイントを使い、完了記録を二重に追加しない。
売買は台帳へ記録して照合する。取得の成功と日次runの成功は別の記録で管理する。

```bash
python -m stock_jp operation gates --help
python -m stock_jp automation health --persisted-only
```

監視は保存済みrunの欠落・陳腐化を報告する。実データの取得や日次判断を代行しない。
入力の鮮度・網羅性を確かめるには `operation readiness` を使う。

新しい空の作業領域の初期化には `operation state init` を使う。
雛形は初期化用で、既存の設定や台帳を置換しない。
