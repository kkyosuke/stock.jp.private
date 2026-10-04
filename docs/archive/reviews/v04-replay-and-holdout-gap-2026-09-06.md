# v0.4履歴再生・holdout受入 gap — 2026-09-06

## 結論

再生器、入力validator、証跡validator、2025〜2026回顧的replayとforward holdoutの分離は
実装済みである。現在不足しているのは、利用条件を本人が確認したJ-Quants非依存の公式履歴入力、
その入力から生成する実成績、v0.4凍結後の未観測期間の完了、本人による2段階の受入である。
これらがない間はreview JSONを`ACCEPT` / `PROMOTE_V0_4_TO_LIVE`へ変更しない。

## 現在ある成績

`stock.jp/data/tenbagger-v0.4-allocation-replay-2025-summary.json`は
`ALLOCATION_DIAGNOSTIC_ONLY`である。12の代理候補、MRS=`NORMAL`固定、観測できない追加購入条件を
すべて合格と仮定し、資金配分の差だけを比較している。

| 版 | return | 最大DD | 期末待機資金比率 |
| --- | ---: | ---: | ---: |
| v0.2 | +0.402434% | -4.028261% | 86.321846% |
| v0.4 | +4.949967% | -27.381320% | 0.408639% |

この結果はv0.4で損失振幅が増える可能性を示すが、正式な候補選定、追加条件、出口、
単一銘柄・業種別の最大損失寄与を検証していないため、受入対象にはできない。

## 実装済み

- `historical_replay.py`のprivate限定・非download入力契約
- J-Quants、Yahoo、hash不一致、利用可否未確認、look-aheadを拒否するvalidator
- 全暦日の公式取引日カレンダーと、全営業日×全有効銘柄、全月末×全母集団、決算・重大開示reviewの欠損検出
- v0.2/v0.4の入口、追加、S-A/S-B/S-C/S-D、集中上限、未約定、費用、企業イベント再生
- point-in-time、trade、日次・月次metrics、v0.2/v0.4比較のhash拘束出力
- 2025〜2026を`RETROSPECTIVE_STRESS_TEST`、正式holdoutを`FORWARD_HOLDOUT`として分離するgate
- forward holdoutの期間・最低観測数・損失floor・再調整禁止を事前固定するplan schema

## 履歴再生の受入前に不足している外部入力

1. J-Quants非依存で再利用可能な公式point-in-time入力
   - 上場廃止、合併、コード変更、再上場を含む日次銘柄母集団
   - 期間全体のOHLCV、売買代金、取引日、分割・併合等のcorporate action
   - 公表日時付き財務・会社予想・決算予定・重大開示event、当時点の発行済・希薄化後株式数
   - 当時利用可能だったSAM/SOM、競合、経営品質の一次資料
   - TOPIXとMRS入力の当時点履歴、TOB・合併対価・売買停止・上場廃止の処理
2. 上記入力を`READY`にした後に生成するhash固定成果物
   - `point-in-time-validation.json`
   - `replay-result-2025-2026.json`
   - source snapshot、trade log、metrics、monthly returns
   - hard-gate入力欠損0件、look-ahead違反0件の検証結果
3. v0.2 / v0.4比較値
   - return、最大DD、最大単一銘柄損失寄与、最大業種損失寄与

既存のJ-Quants由来archiveは、新規取得およびLIVE証跡への採用を行わない。従って、上記入力を
別の利用可能な公式経路で置換できるまで、履歴再生gateはblockedとする。空の入力ひな形は
`operations/private/historical-replay/2025-2026/`に作成し、未取得を`DRAFT`として可視化する。

## 解消したholdout条件の矛盾

- v0.4の発効日: 2026-09-01
- 旧validatorが固定していたholdout: 2025-01-01〜2026-08-31
- 旧validatorが要求していた凍結日: 2025-01-01より前

v0.4はholdout終了後に成立したため、この期間について`predeclared: true`、`retuning_count: 0`、
2025年以前の凍結日時を正当に証明できない。本人が結果を確認しても、この事実は承認で補えない。

validatorはこの矛盾を解消し、2025〜2026を回顧的stress testとして受け入れ、v0.4凍結後の
未観測期間だけを正式holdoutとして事前登録する。本人は次を結果を見る前に固定する。

- 開始日・終了日、最低月次評価数、最低取引件数
- 使用するデータ版、候補選定、約定、手数料、比較対象
- 許容最大DD、単一銘柄・業種損失寄与等の昇格／棄却基準
- 期間中の再調整禁止と、変更時にholdoutを再開始する規則

2025〜2026結果は`historical_replay_2025_2026_accepted`で回顧的riskを受け入れる対象に限る。
これだけでは`v04_holdout_promotion`は合格せず、holdout合格や予測性能の検証とは主張しない。

## 本人が最後に行うこと

公式データの利用権確認と入力投入は本人の責任で行う。正式replay完成後、本人は`historical-replay-review.json`でresult hash、最大DD、集中損失、
データ制約を確認して`ACCEPT`を記録する。正式holdout完成後は`v04-holdout-review.json`で
v0.4の損失増幅、最大DD、単一銘柄・業種損失寄与、待機資金が安全資産でないこと、再調整0回を
確認し、採用する場合だけ`PROMOTE_V0_4_TO_LIVE`を記録する。承認日時は対象成績と機械証跡の
生成後でなければならず、両reviewはそれぞれのSHA-256へ拘束する。
