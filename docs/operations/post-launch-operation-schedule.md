# 運用開始後の詳細スケジュール

この文書は、[READMEの全体フロー](../../README.md#運用開始後の全体フロー)を実行時刻、停止条件、定期review、LIVE移行まで展開した別紙です。日々はREADMEだけを確認し、失敗時や期限到来時にこの文書と[private運用手順](private-operation-runbook.md)を参照します。フロー図のオレンジ色は利用者が担当する作業です。

## 詳細フロー

~~~mermaid
flowchart TD
    A["平日18:30<br/>publicが全市場の日次株価を取得"] --> B{"検証済みdata PRが<br/>auto-mergeされたか"}
    B -->|"失敗・未merge"| STOP["STOP<br/>その日の新しい運用を開始しない"]
    B -->|"merge済み"| C{"同日の株価で<br/>今夜運用するか"}
    C -->|"はい"| D["privateのupdate-public-submoduleを<br/>手動起動"]
    C -->|"いいえ"| E["翌朝7:00<br/>privateが自動確認"]
    D --> F{"日次株価だけの<br/>安全な差分か"}
    E --> F
    F -->|"code・rule変更<br/>または検証失敗"| REVIEW["STOP<br/>auto-mergeせず手動review"]
    F -->|"合格"| G["pointer PRをauto-merge<br/>checkoutとsubmoduleを同期"]
    G --> H{"state・台帳・data・<br/>readinessは正常か"}
    H -->|"異常あり"| WAIT["WAIT / NO-ACTION<br/>原因を解消してから再開"]
    H -->|"正常"| I["利用者が<br/>『今日の夜間運用を実行して』と依頼"]
    I --> J["公式情報源・期限到来task・<br/>翌営業日actionを確認"]
    J --> K{"運用mode"}
    K -->|"PAPER"| P["PAPER_PROPOSEDまで<br/>証券会社へ入力しない"]
    K -->|"LIVE"| L["PROPOSEDまで<br/>自動送信しない"]
    P --> R["夜間reportを確認<br/>state・台帳・run・public commitを照合"]
    L --> R
    R --> S["1つのprivate commitとしてpush"]
    L -.->|"注文候補がある翌営業日"| M["8:45〜8:55<br/>本人がpre-trade check"]
    M --> N{"本人の判断"}
    N -->|"SUBMIT"| O["証券会社へ手入力"]
    N -->|"CANCEL / WAIT"| Q["注文しない"]
    O --> T["判断・約定結果を台帳へ反映してpush"]
    Q --> T
    S -.->|"最初の4週間は週末"| W["run-historyを5分review"]
    S -.->|"月末値確定後"| X["候補・価格・集中・MRSをreview"]
    S -.->|"四半期開示後5営業日以内"| Y["hard gate・score・KPIをreview"]
    S -.->|"本決算後10営業日以内"| Z["SAM・SOM・競合・出口を全面review"]
    S -.->|"PAPERと並行"| V1["LIVE準備<br/>point-in-time・履歴再生・公式source<br/>clean-clone・v0.4 holdout"]
    V1 --> V2["LIVE予定日の90日以内<br/>個人・証券会社checklist"]
    V2 --> V3{"全証跡と本人承認が<br/>揃ったか"}
    V3 -->|"未達"| A
    V3 -->|"合格"| V4["promote-live --apply<br/>live_go: trueを確認"]
    V4 --> A
    T --> A
    S --> A

    classDef human fill:#ffedd5,stroke:#ea580c,stroke-width:2px,color:#431407
    class D,REVIEW,I,R,M,N,O,Q,T,W,X,Y,Z,V2,V3 human
~~~

## 利用者が行うこと

| 時期 | 作業 |
| --- | --- |
| 平日18:30以降 | public data PRの成功を確認する。同日株価を使う場合はprivateの`update-public-submodule` workflowを手動起動し、pointer PRのmerge後にcheckoutを同期する |
| 同期後の夜 | 「今日の夜間運用を実行して」と依頼し、完了reportとprivate pushを確認する。PAPER注文票は証券会社へ入力しない |
| LIVEで注文候補がある翌営業日8:45〜8:55 | pre-trade checkを行い、`SUBMIT`だけを証券会社へ手入力する。`CANCEL / WAIT`なら注文しない |
| 注文判断・約定後 | 判断、約定数量、価格、費用を運用記録へ反映する |
| 最初の4週間の週末 | `run-history.csv`を5分確認し、実行漏れ、失敗、data不足、重複処理、未照合注文を確認する |
| 月末値の確定後 | 候補抽出、価格・期限、集中、`MRS-v0.1`の結果をreviewする |
| 四半期開示後5営業日以内 | hard gate、score、主要KPI、希薄化、資金余力の判定をreviewする |
| 本決算後10営業日以内 | SAM、SOM、競合、出口倍率、3年逆算の更新をreviewする |
| LIVE移行前 | 全証跡を確認し、予定日の90日以内に個人・証券会社checklistを再確認して、最後に本人が明示承認する |

## LIVE移行の扱い

PAPERの最低継続日数や最低連続run数は設けません。初回の正常なPAPER runが完了したら、そのrunの公式情報源coverageとrun integrityをLIVE証跡候補として確認します。LIVEの公式情報源coverageにはEDINET APIの実疎通が必要です。API keyがない間も手動一次資料fallbackでPAPERを続けられますが、このLIVE gateは完了しません。

日次運用と並行してpoint-in-time全母集団検証、2025–2026履歴再生、clean-clone、v0.4 holdout reviewを進めます。365日または20営業日の経過を待つ必要はありません。全条件が合格した後だけ本人が最終承認し、`live_go: true`を確認してLIVEへ移行します。

LIVE移行後も夜間runが行うのは`PROPOSED`注文票の作成までです。確認時間を確保できない日、情報源を確認できない日、またはreadinessに異常がある日は`WAIT` / `NO-ACTION`とします。
