# LIVE GO/NO-GO 基準

## 現在の判定

**LIVE NO-GO**。`operation_bootstrap.py check` が `live_go: true` を返し、利用者が evidence を確認して明示承認するまで変更しません。CI、backtest、off-line smoke の成功だけでは GO にしません。

## gate と完了条件

| policy key | 完了条件 | private evidence の例 |
| --- | --- | --- |
| `point_in_time_full_universe_validation` | 上場廃止等を含む当時点母集団で入口・出口を再生し、欠損を報告 | dataset manifest、coverage、run report、受入記録 |
| `historical_replay_2025_2026_accepted` | 未来情報なしの2025–2026 replayを人がreview | 成績、最大DD、trade list、欠損、承認記録 |
| `minimum_12_month_paper_trade` | v0.4の有効な前向きPAPERを12か月以上完了 | 開始日、月次集計、失敗・逸脱・変更履歴 |
| `twenty_day_shadow_run` | 実dataで20営業日連続、重大取得漏れ・重複注文0 | run-history、coverage、incident review |
| `official_source_coverage` | 必須一次情報源を毎run網羅し、欠損時fail-close | provider health、source log、manual fallback記録 |
| `private_repository_recovery` | clean clone/mirrorからstateとledgerを復旧 | recovery report、private/public commit、照合結果 |
| `personal_risk_and_broker_check` | 本人が損失許容・税・broker仕様・securityを確認 | 本人のsigned checklist |
| `v04_holdout_promotion` | v0.4の集中riskをholdoutで受入 | comparison report、承認記録 |
| `approval` | 全gate後に本人がLIVE、日時、対象版を明示 | approval file とpolicyの3項目 |

gate を `true` にする場合は、`operations/private/` 内の空でない evidence file を同時に指定します。予定、口頭確認、チャットだけを evidence にしません。

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

1. すべての gate evidence を独立reviewする
2. `operation_state.py validate` を成功させる
3. `operation_bootstrap.py check` の `live_blockers` を0件にする
4. v0.4、`HUMAN_ONLY`、開始日、専用資金、停止条件を本人が最終確認する
5. approval evidence を作り、policy を `LIVE` に更新する
6. 再度 check し、`live_go: true` を確認する
7. 初回は最小の許容lotで、人間のpre-trade checkと手入力を行う

## 即時 NO-GO / PAUSED 条件

- state/ledger 不整合、未照合注文、重複ticket
- 価格、corporate action、必須開示、取引日の不一致
- public data PR 未merge、checksum/coverage/freshness 不合格
- private remoteへ未pushの別run、またはmerge conflict
- credential漏えい、不審login、不審注文、broker障害
- 最大損失または本人の停止条件への到達
- rule version、policy、approval、evidence の不一致

障害時は安全側の既定値を `WAIT` / `NO-ACTION` とし、状況を推測して発注しません。
