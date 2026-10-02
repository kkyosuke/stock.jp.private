# 運用開始後の詳細スケジュール

日次実行の正本は[アシスタント日次実行契約](assistant-daily-operation.md)です。
目標は毎晩21:00（Asia/Tokyo）。スケジュール、実行環境、保存の動作確認が揃うまでは未稼働です。
GitHub Actionsの成功やこの文書のmergeを、日次実行の稼働証拠にしません。

## 担当と期限

| 時期 | アシスタントの作業 | 利用者の作業 |
| --- | --- | --- |
| 毎晩21:00頃 | 許可された市場データのprivate local収集、一次資料調査、検証済みpublicコード採用、PAPER run、private保存と報告 | 不足権限・人の判断が必要なblockerだけ対応 |
| 失敗・期限超過 | WAIT / NO-ACTION、失敗証跡、未完了task、次回handoffを保存・報告 | 必要な承認や接続を復旧 |
| 最初の4週間の週末 | 実行漏れ、data不足、重複処理、未照合注文のreviewを準備 | 必要な判断を確認 |
| 月末値の確定後 | 候補、価格・期限、集中、MRSのreview | rule変更は別途承認 |
| 四半期開示後5営業日以内 | hard gate、score、KPI、希薄化、資金余力を一次資料でreview | 人の判断が必要な事項を確認 |
| 本決算後10営業日以内 | SAM、SOM、競合、出口倍率、3年逆算の更新 | 不確実性・不足を確認 |
| LIVE移行前 | 全証跡の不足を報告 | 個人・証券会社checklist、資金・損失停止、明示承認 |
| 将来LIMITED_LIVE/LIVEで候補がある翌営業日8:45〜8:55 | 承認された範囲でpre-trade資料を準備 | 本人がSUBMIT / CANCEL / WAITを判断し、証券会社へ手入力 |

市場休業日は公式取引日カレンダーを確認し、新しい価格日を捏造しません。期限到来の開示・reviewは
省略しません。起動漏れをActions/Cloudflareが書込みrunで補完することはありません。

## LIVE移行の扱い

LIMITED_LIVEは、専用資金3,000,000円、1 run 1新規注文、追加購入禁止、本人の累積損失停止を
強制する少額観察段階です。実際の資金分離後に`LIMITED_LIVE_FUNDING`を台帳へ記録し、
最新PAPER runの公式coverage、layout revision 4のclean-clone、個人checklist、
2025年diagnosticの制約と最大DDを本人が確認した後だけ昇格します。
通常LIVE gateは未完了のまま明示し、LIMITED_LIVEの経過だけで通常LIVEへ自動昇格しません。

PAPERの最低継続日数や最低連続run数は設けません。初回の正常なPAPER runが完了したら、そのrunの公式情報源coverageとrun integrityをLIVE証跡候補として確認します。LIVEの公式情報源coverageにはEDINET APIの実疎通が必要です。API keyがない間も手動一次資料fallbackでPAPERを続けられますが、このLIVE gateは完了しません。

日次運用と並行してpoint-in-time全母集団検証、2025–2026回顧的履歴再生、clean-clone、v0.4 forward holdoutを進めます。一律の365日PAPERまたは20営業日shadowを待つ条件はありませんが、本人が事前固定したforward holdoutの期間と最低観測数は完了が必要です。全条件が合格した後だけ本人が最終承認し、`live_go: true`を確認してLIVEへ移行します。

LIVE移行後も夜間runが行うのは`PROPOSED`注文票の作成までです。確認時間を確保できない日、情報源を確認できない日、またはreadinessに異常がある日は`WAIT` / `NO-ACTION`とします。
