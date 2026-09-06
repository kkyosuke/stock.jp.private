# LIVE GO/NO-GO 基準

## 現在の判定

**LIVE NO-GO**。`operation_bootstrap.py check` が `live_go: true` を返し、利用者が evidence を確認して明示承認するまで変更しません。CI、backtest、off-line smoke の成功だけでは GO にしません。

## gate と完了条件

| policy key | 完了条件 | private evidence の例 |
| --- | --- | --- |
| `point_in_time_full_universe_validation` | 上場廃止等を含む当時点母集団で入口・出口を再生し、欠損を報告 | dataset manifest、coverage、run report、受入記録 |
| `historical_replay_2025_2026_accepted` | 未来情報なしの2025–2026 replayを人がreview | 成績、最大DD、trade list、欠損、承認記録 |
| `official_source_coverage` | 最新の完了済みPAPER runで必須一次情報源とrun integrityを確認 | provider health、source log、manual fallback記録 |
| `private_repository_recovery` | 現行layoutをclean cloneし、stateとledgerを照合 | clean-clone report、layout revision、private/public commit |
| `personal_risk_and_broker_check` | 本人が損失許容・税・broker仕様・securityを確認 | 本人のsigned checklist |
| `v04_holdout_promotion` | v0.4の集中riskをholdoutで受入 | comparison report、承認記録 |
| `approval` | 全gate後に本人がLIVE、日時、対象版を明示 | approval file とpolicyの3項目 |

gate を `true` にする場合は、`operations/private/evidence/` 内の型付き evidence file を同時に指定します。空ファイル、予定、口頭確認、チャット、真偽値だけの手編集は evidence にしません。

J-Quantsは使用せず、新規取得もLIVE証跡への採用も行いません。point-in-timeと履歴再生は、
再利用可能な公式公開データまたは一次資料から再構築できた範囲だけを証跡にします。

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
3. 現行layout revision 2のclean-clone確認と、90日以内の本人checklistを合格させる
4. v0.4 holdout reviewを本人が`PROMOTE_V0_4_TO_LIVE`と明示する
5. `operation_state.py validate`を成功させる
6. `live-approval.json`に全証跡hash、private/public commit、現在のPAPER policy hash、本人の`LIVE`と日時を記録する
7. `live_gate_evidence.py --root stock.jp promote-live`が`eligible: true`であることを確認する
8. 同じ入力のまま`promote-live --apply`を1回だけ実行する
9. `operation_bootstrap.py check`の`live_go: true`を確認する
10. 初回は個人checklistで定めた上限以下かつ戦略上許容される最小lotで、人間のpre-trade checkと手入力を行う

`--apply`は全条件を元データから再計算してから、policyのmode、5 gate、v0.4 promotion、証跡path、承認を1回のatomic writeで更新します。不合格ならpolicyを変更しません。詳細なschemaとコマンドは[publicのLIVE gate証跡仕様](../stock.jp/docs/operations/live-gate-evidence.md)を正本とします。

### 最終承認のcommit順

最終承認とpolicy変更を同じ未追跡作業のまま進めず、次の3段階をprivate Gitへ残します。

1. 6つの合格証跡と、最終承認以外のreviewをcommitしてprotected remoteへpushする
2. そのprivate commit、採用public commit、PAPER policyと6証跡のSHA-256を `live-approval.json` に記入し、別commitでpushする
3. `promote-live` をread-onlyで再実行し、本人が出力を確認してから `--apply` し、変更されたpolicyと `live-readiness.json` を3つ目のcommitでpushする

commitとhashは次のread-only commandで取得します。出力を公開Issue、public PR、public Actionsへ貼りません。

~~~bash
git rev-parse HEAD
git -C stock.jp rev-parse HEAD
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
