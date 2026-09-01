# stock.jp.private

`stock.jp` を実資金運用へ段階的に移行するための private workspace です。現在の判定は **LIVE NO-GO** です。履歴再生、最新PAPER runの公式情報源coverage、現行構成のclean-clone確認、利用者固有の確認と最終承認が完了するまで実注文を行いません。PAPERの経過日数と連続run数には最低条件を設けません。

## 構成

~~~text
stock.jp.private/
├── docs/                         # private固有の設計、GO/NO-GO、レビュー
├── .github/workflows/             # 毎朝のpublic submodule自動更新
├── operations/private/           # 状態、台帳、run、判断証跡の正本
├── scripts/live-gate-status.sh    # 全LIVE条件のread-only確認
├── scripts/setup-workspace.sh     # submoduleとsymlinkの安全な初期化
└── stock.jp/                      # publicコード・公開データのsubmodule
~~~

public/private の境界は[全体構成](docs/repository-layout.md)、実行順は[private 運用手順](docs/operations/private-operation-runbook.md)、LIVE 条件は[GO/NO-GO 基準](docs/live-readiness.md)を参照してください。

平日18:30のpublic日次データPRは検証成功時だけauto-mergeし、privateは毎朝7:00にpublic
`main`の日次株価差分だけをsubmodule pointer PRで検証してauto-mergeします。codeやruleの
変更と検証失敗時はmainを変更せず、手動reviewへ戻します。

## 初回セットアップ

~~~bash
git clone --recurse-submodules git@github.com:kkyosuke/stock.jp.private.git
cd stock.jp.private
./scripts/setup-workspace.sh
python3 -m venv stock.jp/.venv
stock.jp/.venv/bin/pip install 'xlrd==2.0.2' 'certifi==2026.7.22' PyYAML
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py migrate
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
./scripts/live-gate-status.sh
~~~

`scripts/setup-workspace.sh` は `stock.jp/operations/private -> ../../operations/private` を作り、private データの権限を補正します。既存の別ファイルを上書きしません。

## 運用原則

- public は汎用コード、公開データ、ルール、研究、CIを持つ
- private は実保有、資金、注文、判断、状態、GO/NO-GO 証跡を持つ
- private データは暗号化 archive にせず、private Git の通常ファイルとして追跡する
- API key、password、token、cookie、MFA 回復コードは private Git にも保存しない
- GitHub Actions の public job へ private データを入力・出力しない
- PAPER は `PAPER_PROPOSED`、LIVE でも broker submission は `HUMAN_ONLY` のままにする
- readiness が1項目でも未達なら、新規・追加注文を作らない

## 運用開始後のスケジュール

PAPERの最低継続日数や最低連続run数は設けません。日次運用を続けながらLIVE gateを並行して確認し、すべての証跡と本人承認が揃った日を最短のLIVE移行日とします。

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
    H -->|"正常"| I["利用者が<br/>「今日の夜間運用を実行して」と依頼"]
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
~~~

初回の正常なPAPER runが完了したら、そのrunの公式情報源coverageとrun integrityをLIVE証跡候補として確認します。LIVEの公式情報源coverageにはEDINET APIの実疎通が必要です。API keyがない間も手動一次資料fallbackでPAPERを続けられますが、このLIVE gateは完了しません。

日次運用と並行してpoint-in-time全母集団検証、2025–2026履歴再生、clean-clone、v0.4 holdout reviewを進め、LIVE予定日の90日以内に個人・証券会社checklistを再確認します。365日または20営業日の経過を待つ必要はありません。全条件が合格した後だけ本人が最終承認し、`live_go: true`を確認してLIVEへ移行します。

LIVE移行後も夜間runが行うのは`PROPOSED`注文票の作成までです。注文候補がある翌営業日の8:45〜8:55に本人がpre-trade checkを行い、`SUBMIT`と判断した注文だけを証券会社へ手入力します。確認時間を確保できない日、情報源を確認できない日、またはreadinessに異常がある日は`WAIT` / `NO-ACTION`とします。

## 日次の最小確認

~~~bash
git pull --ff-only
git submodule update --init
./scripts/setup-workspace.sh
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
~~~

成功 run の状態、台帳、採用した public submodule commit を同じ private commit に含め、remote へ push してから別端末や次の run へ進みます。競合した台帳を機械的に片側採用しません。

`live-gate-status.sh` は判定専用で、policyやevidenceを変更しません。未達項目が1つでもあれば終了code 1を返します。現在はLIVE条件が未達なので、この終了codeが正常です。

## 暗号化を使わない方針

`age`、`.zip.age`、backup 用 GitHub Release、復号鍵は新しい運用に使いません。LIVE 前に一度、または repository layout の変更後に、private repository の clean clone、submodule 初期化、状態検証、台帳照合を確認します。別mirrorと日数による再確認は要求しません。旧 archive を廃棄する場合は、必要な内容が private Git に移行済みで clean clone から復元できることを先に確認します。

## ドキュメント

- [private ドキュメント索引](docs/README.md)
- [現在の readiness review](docs/reviews/operation-readiness-review-2026-09-01.md)
- [public の汎用ドキュメント](stock.jp/docs/README.md)

Markdown は `npm run lint:markdown` で検査します。
