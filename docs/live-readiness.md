# LIVE GO/NO-GO 基準

## 現在の判定

**LIVE NO-GO**。`operation_bootstrap.py check` が `live_go: true` を返し、利用者が evidence を確認して明示承認するまで変更しません。CI、backtest、off-line smoke の成功だけでは GO にしません。

**LIMITED_LIVEも現在NO-GO**。専用資金は3,000,000円を上限として事前記録しましたが、
本人の累積損失停止率と承認、公式source coverage、layout revision 4のclean-clone、
個人checklistが未完了です。`limited_live.py status`が`eligible: true`になるまでPAPERを維持します。

## LIMITED_LIVEの条件

LIMITED_LIVEは通常LIVE前の少額観察用です。通常LIVE gateを真にせず、次を独立に強制します。

- 専用資金の絶対上限: 3,000,000円
- 専用資金を実際に分離した後、`capital-ledger.csv`へ`LIMITED_LIVE_FUNDING`を1件記録し、
  入金額・開始残高を3,000,000円へ一致させる
- 1 runの新規・追加注文: 最大1件
- 追加購入: 禁止
- 本人が決めた累積損失停止率へ到達: 新規購入停止
- MRSが`NORMAL`/`CAUTION`かつ会社assessmentが`PASS`: 購入の必須条件
- 公式source coverage、clean-clone、90日以内の個人checklist: すべて必須
- 発注: `HUMAN_ONLY`、翌朝8:45〜8:55の本人pre-trade check後の手入力のみ

2025年診断の最大DD `-27.38132%`と、この診断がforward証跡でないことを
`operations/private/evidence/limited-live-plan.json`で本人が確認します。通常LIVEへの昇格には、
引き続point-in-time履歴、履歴replay、forward holdout、最終LIVE承認が必要です。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/limited_live.py --root stock.jp status
stock.jp/.venv/bin/python stock.jp/scripts/limited_live.py --root stock.jp apply
~~~

## gate と完了条件

| policy key | 完了条件 | private evidence の例 |
| --- | --- | --- |
| `point_in_time_full_universe_validation` | 上場廃止等を含む当時点母集団で入口・出口を再生し、欠損を報告 | dataset manifest、coverage、run report、受入記録 |
| `historical_replay_2025_2026_accepted` | 未来情報なしの2025–2026 replayを人がreview | 成績、最大DD、trade list、欠損、承認記録 |
| `official_source_coverage` | 最新の完了済みPAPER runで必須一次情報源とrun integrityを確認 | provider health、source log、manual fallback記録 |
| `private_repository_recovery` | 現行layoutをclean cloneし、stateとledgerを照合 | clean-clone report、layout revision、private commitとcode tree SHA |
| `personal_risk_and_broker_check` | 本人が損失許容・税・broker仕様・securityを確認 | 本人のsigned checklist |
| `v04_holdout_promotion` | v0.4の集中riskをholdoutで受入 | comparison report、承認記録 |
| `approval` | 全gate後に本人がLIVE、日時、対象版を明示 | approval file とpolicyの3項目 |

gate を `true` にする場合は、`operations/private/evidence/` 内の型付き evidence file を同時に指定します。空ファイル、予定、口頭確認、チャット、真偽値だけの手編集は evidence にしません。

2026-10-03時点では、本人承認済みLight契約内の日次収集・上限付き履歴収集とPAPER準備に
J-Quantsを使用します。Standardへの変更や有料指数の導入は行いません。
収集許可はLIVE／forward holdoutへの採用承認ではなく、既存holdoutの
`jquants_will_not_be_used`条件は維持します。条件変更が必要なら別の事前計画・本人判断に分離し、
既存結果を遡って合格にしません。

### v0.4 holdoutの時系列制約

現行v0.4の発効日は2026年9月1日です。そのため、2025年1月1日〜2026年8月31日を
「v0.4を事前凍結したholdout」として承認することはできません。この期間の結果は回顧的replay／
stress testとして扱います。`predeclared: true`や2025年1月1日以前の架空の凍結日時を記録して
gateを通してはいけません。

正式なholdout昇格は、2026年9月1日のv0.4凍結後に未観測だった固定期間を
`operations/private/evidence/v04-holdout-plan.json`へ結果を見る前に記録して`FROZEN`にし、その期間中に
再調整を行わず、同一のデータcutoff・約定規則・手数料でv0.2とv0.4を比較してから本人が受け入れます。
validatorは2025〜2026を`RETROSPECTIVE_STRESS_TEST`、正式holdoutを`FORWARD_HOLDOUT`として分離し、
前者だけでは`v04_holdout_promotion`を通しません。結果・plan凍結・本人承認・参照証跡の
将来日時も明示的に拒否します。

不足データ、実装成果物、本人受入までの順序は
[v0.4履歴再生・holdout受入 gap](reviews/v04-replay-and-holdout-gap-2026-09-06.md)に固定します。

PAPERの経過日数と連続run数には最低条件を設けません。365日PAPERと実data20営業日のshadow結果は任意diagnosticとして保存できますが、LIVE判定を阻害しません。

現在地は次で確認します。このコマンドはファイルを変更しません。

~~~bash
./scripts/live-gate-status.sh
~~~

## personal risk and broker check

金額はこの文書へ記載せず、private evidence に残します。少なくとも次を本人が決定します。

- このstrategy専用資金と、全損しても生活へ影響しない上限
- 1銘柄・1業種・全体の損失時に停止する条件
- 税区分、手数料、100株単位、指値・失効・部分約定の扱い
- 翌営業日8:45〜8:55に人間が確認できない日の `NO-ACTION`
- broker障害、価格不一致、売買停止、corporate action 時の連絡・停止手順
- broker/APIに自動発注権限を与えないこと
- account MFA/passkey、固有password、通知、緊急連絡先

## GO の手順

1. 公式履歴入力を投入してpoint-in-time、2025〜2026回顧的履歴再生、最新PAPER runの公式coverageの各コマンドを合格させ、`--write-evidence`で保存する
2. v0.4凍結後の未観測期間と受入基準を`v04-holdout-plan.json`で事前固定し、期間終了まで再調整なしでforward replayを完了する
3. 現行layout revision 4のclean-clone確認と、90日以内の本人checklistを合格させる
4. v0.4 holdout reviewを本人が`PROMOTE_V0_4_TO_LIVE`と明示する
5. `operation_state.py validate`を成功させる
6. `live-approval.json`に全証跡hash、private commitとcode tree SHA、現在のPAPER policy hash、本人の`LIVE`と日時を記録する
7. `live_gate_evidence.py --root stock.jp promote-live`が`eligible: true`であることを確認する
8. 同じ入力のまま`promote-live --apply`を1回だけ実行する
9. `operation_bootstrap.py check`の`live_go: true`を確認する
10. 初回は個人checklistで定めた上限以下かつ戦略上許容される最小lotで、人間のpre-trade checkと手入力を行う

`--apply`は全条件を元データから再計算してから、policyのmode、5 gate、v0.4 promotion、証跡path、承認を1回のatomic writeで更新します。不合格ならpolicyを変更しません。詳細なschemaとコマンドは[publicのLIVE gate証跡仕様](../stock.jp/docs/operations/live-gate-evidence.md)を正本とします。

### 最終承認のcommit順

最終承認とpolicy変更を同じ未追跡作業のまま進めず、次の3段階をprivate Gitへ残します。

1. 6つの合格証跡と、最終承認以外のreviewをcommitしてprotected remoteへpushする
2. そのprivate commitを `private_commit_reviewed`、stock.jp/のcode tree SHAを `code_tree_reviewed`、PAPER policyと6証跡のSHA-256を `live-approval.json` に記入し、別commitでpushする
3. `promote-live` をread-onlyで再実行し、本人が出力を確認してから `--apply` し、変更されたpolicyと `live-readiness.json` を3つ目のcommitでpushする

commitとhashはstock.jp.privateのrepository rootから次のread-only commandで取得します。出力を公開Issue、public PR、public Actionsへ貼りません。

~~~bash
git rev-parse HEAD          # private_commit_reviewed
git rev-parse HEAD:stock.jp # code_tree_reviewed（commitではなくtree）
shasum -a 256 operations/private/operation-policy.json
shasum -a 256 operations/private/evidence/{point-in-time,historical-replay,official-coverage,repository-recovery,personal-risk,v04-promotion}.json
~~~

最終承認後に入力、policy、証跡を変更した場合はhash不一致になるため、影響する判定と承認をやり直します。force pushや履歴改変で一致させません。

## 即時 NO-GO / PAUSED 条件

- state/ledger 不整合、未照合注文、重複ticket
- 価格、corporate action、必須開示、取引日の不一致
- public data PR 未merge、checksum/coverage/freshness 不合格
- private remoteへ未pushの別run、またはmerge conflict
- credential漏えい、不審login、不審注文、broker障害
- 最大損失または本人の停止条件への到達
- rule version、policy、approval、evidence の不一致

障害時は安全側の既定値を `WAIT` / `NO-ACTION` とし、状況を推測して発注しません。
