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

保存済みPAPER runの一次資料レビューだけを再開する場合は、次を使う。

```bash
python -m stock_jp operation run resume-review --run-id <YYYY-MM-DD> --at <JST日時>
```

この入口は既存runのleaseを取得し、失敗attemptを再開する。provider通信、資料の再取得、
採点・MRSの再計算、レビュー成果物の再生成は行わない。元のcutoffと未完了課題を保つ。
有効なleaseを持つ本人は `--run-token` で続行でき、別writerは `locked` で停止する。
`investment_readiness=NOT_EVALUATED` は購入可能を意味しない。
これは採用済み作業領域の再開用で、source inboxからstateや台帳を自動復元する機能ではない。

再開後は原本の内容、公表時刻、cutoff、対象の網羅性を実際に照合し、`sources.csv`、
`coverage.json`、`research-queue.json`、`work-plan.json`、調査結果、翌日行動、handoffへ
根拠を記録する。未確認資料を `COMPLETED` に変更しない。繰越可能な課題だけを
`DEFERRED` として `pending_reviews` へ残し、必須の開示確認やblocking gapは解消する。
最後に返されたtokenと元のcutoffで `run finalize` を実行する。従来の完了検証は全て残る。

注文0件の `WAIT` でも、必要な日次確認と根拠・繰越記録が揃えば日次runは完了できる。
これは投資可能判定、全課題の解消、holdout評価完了、LIVE承認のいずれも意味しない。
未レビュー・網羅性不足のrunは、注文が0件でも完了にしない。

```bash
python -m stock_jp operation gates --help
python -m stock_jp automation health --persisted-only
```

監視は保存済みrunの欠落・陳腐化を報告する。実データの取得や日次判断を代行しない。
入力の鮮度・網羅性を確かめるには `operation readiness` を使う。

新しい空の作業領域の初期化には `operation state init` を使う。
雛形は初期化用で、既存の設定や台帳を置換しない。
