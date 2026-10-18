# 過去日の計画と既存runの再開

この入口はPR94の外部writer排他と共有日次checkpointに依存する。
PR94の承認済みheadは変更せず、両方の修正がmainに採用されてから使う。
手動とActionsは同じ日付検証・collector・journal・共有12要求上限を使う。
未採用コードからの実取得、Actions IDの偽装、台帳reset、暗黙の予算追加は行わない。

**未作成の過去日への新規取得は、予算帰属の定義が必要なため未対応。**
`date-grants.json`の旧固定履歴campaignは別物で、今回の欠損日へ拡張しない。
過去の日数に12を掛けた枠を新設せず、既存の課金basisを持つrunだけを再開する。
この入口の追加だけでは10月13〜16日の新規取得が可能になったとは扱わない。

## 手動入口

```bash
python -m stock_jp.automation.market_backfill --root <checkout> --session recovery --from-date 2026-10-13 --through-date 2026-10-16 --plan-only
```

単日は`--target-date YYYY-MM-DD`を使う。範囲指定との併用は禁止。
計画を確認して既存runだけの範囲を再開する場合は`--plan-only`を外す。
固定範囲は両端を含む31暦日以内、現在の論理日より前、設定済みLight/Standard契約の
各日付指定datasetの保存対象期間内でなければ停止する。契約設定は実権利の証明ではなく、
providerの401/403/429は既存collectorの停止条件を維持する。

## Actions入口

既存`market-data-manual.yml`のowner手動実行で`backfill=true`にする。
`target_date`が開始日、`backfill_through`が終了日。終了日を空にすると単日。
`backfill_plan_only=true`が既定で、確認後にfalseで既存runを再開する。
実際のActions環境内では次の同じmoduleを使う。

```bash
python -m stock_jp.automation.market_backfill --root "$GITHUB_WORKSPACE" --mode actions --target-date 2026-10-13 --plan-only
```

定時実行と通常の日次入口は変更しない。backfillとfinancial-history/preview/PAPER指定は
併用しない。backfill後にPAPER調査を自動起動しない。結果は
`.work/market-backfill.json`と標準出力へ保存する。PRの通常CI以外をdispatchしない。

## 計画と実行の区別

| 計画action | 意味 |
| --- | --- |
| `VERIFY_COMPLETE` | journal上は完了。実行時に原本・receipt・checksumを再検証し、取得しない |
| `RESUME` | 既存runと課金basisを保持し、保存済みページから再開する |
| `BUDGET_ALLOCATION_REQUIRED` | runがなく、過去日用の枠を新設できない |
| `SOURCE_SCOPE_REQUIRED` | 現在分のみのfeedの保存済みreceiptがなく、過去資料として再取得できない |
| `OPERATOR_RECONCILIATION_REQUIRED` | 既存予算枯渇・認証/検証停止等。自動解除しない |

範囲内に停止項目があれば、範囲全体をprovider通信・remote checkpoint更新前に停止する。
計画の`PLAN_READY`は原本検証済みや取得完了を意味しない。
実行は古い日から順に進め、未完了日を飛ばさない。休日は既存calendar判定を使い、
取引日を作らない。保存済みの最新価格日を古い完了runの再確認で巻き戻さない。
新しいsnapshotがある日より古い未完了runは従来の安全停止を維持する。
過去の財務暫定値の後日補完は既存collectorと実取得時刻に従い、cutoffを捏造しない。

失敗・ページ・再試行・不明応答を含む予約は通信前に永続保存する。
PAPER消費・過去charge・receipt・run設定・課金basisは変更しない。
別sessionや同時Actionsを拒否し、手動からActionsへの切替はPR94の明示handoffで
所有者fenceのみ解除する。receiptは所有者識別に使うため他checkoutへコピーしない。

完了時は既存の非圧縮JSON/CSV保存形式を検証し、mainを親とするデータ専用PRを作る。
この経路は手動/ActionsともCI dispatch・承認・mergeを行わず、必須条件の確認待ちとする。
コードや契約設定、日付別台帳はデータPRへ混在させない。

## 未作成日の安全案（未実装・未承認）

新規の過去日を取得する場合は「対象データ日」と「実行の論理日」を分ける案がある。
各HTTP予約を実行論理日の通常市場・PAPERと合計12回以内に含め、対象日の既存消費も
照合する。複数過去日を指定しても実行日の枠を増やさない。再起動・モード切替で
既存予約の帰属を変更しない。この案を実装するには、共有checkpointのschemaと
全writerの課金照合を更新し、二重計上/計上漏れがないことを検証する必要がある。

また`earnings_calendar`は現在分のみのendpointで、過去日指定では取得できない。
過去日補完を日付指定可能な市場datasetに限定し、このfeedを別の不足項目として
扱う案も、通常日次の完了状態との区別を定義してから実装する。
現在分の応答を過去日の一次資料として代用しない。

実行論理日への帰属と履歴dataset範囲が決まるまで、新規過去日の取得は停止する。
98%基準、PAPER/HUMAN_ONLY、凍結研究、PIT/source権利/レビュー/risk承認は変更しない。
