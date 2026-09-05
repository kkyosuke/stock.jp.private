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
stock.jp/.venv/bin/pip install 'xlrd==2.0.2' 'openpyxl==3.1.5' 'pypdf==6.16.1' 'certifi==2026.7.22' PyYAML
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
- 会社別の希薄化・SAM/SOM・競合・KPI入力は`operations/private/research-inputs/investment-cases/`に置き、publicには値を出さない
- API key、password、token、cookie、MFA 回復コードは private Git にも保存しない
- GitHub Actions の public job へ private データを入力・出力しない
- PAPER は `PAPER_PROPOSED`、LIVE でも broker submission は `HUMAN_ONLY` のままにする
- readiness が1項目でも未達なら、新規・追加注文を作らない

## 運用開始後の全体フロー

オレンジ色の`👤`が利用者の作業です。詳しい自動更新条件、失敗時の扱い、定期review、LIVE移行手順は[運用開始後の詳細スケジュール](docs/operations/post-launch-operation-schedule.md)を参照してください。

~~~mermaid
flowchart TD
    A["自動｜平日18:30<br/>public株価を更新・検証・merge"] --> B{"更新成功?"}
    B -->|"いいえ"| STOP["👤 夜<br/>失敗を確認し、その日はWAIT"]
    B -->|"はい"| C["👤 夜<br/>最新dataを同期して<br/>『今日の夜間運用を実行して』と依頼"]
    C --> D["運用処理<br/>運用前check・公式情報確認・判定<br/>reportと注文候補を作成"]
    D --> E{"問題なし?"}
    E -->|"いいえ"| WAIT["👤 reportと失敗記録を確認<br/>解消するまで注文しない"]
    E -->|"はい"| F["👤 夜<br/>reportとprivate push完了を確認"]
    F --> G{"運用modeと注文候補"}
    G -->|"PAPER"| P["証券会社へ入力しない<br/>仮想注文だけ"]
    G -->|"LIVE・候補なし"| Q["NO-ACTION"]
    G -->|"LIVE・候補あり"| H["👤 翌営業日8:45〜8:55<br/>pre-trade check"]
    H --> I{"本人の判断"}
    I -->|"SUBMIT"| J["👤 証券会社へ手入力"]
    I -->|"CANCEL / WAIT"| Q
    J --> K["👤 判断・約定結果を<br/>運用記録へ反映"]
    P --> L["次の営業日まで待機"]
    Q --> L
    K --> L
    STOP --> L
    WAIT --> L
    L --> A
    L -.-> M["👤 定期作業<br/>週末・月末・決算時のreview<br/>LIVE移行前の確認と承認"]

    classDef human fill:#ffedd5,stroke:#ea580c,stroke-width:2px,color:#431407
    class STOP,C,WAIT,F,H,I,J,K,M human
~~~

### 人の作業一覧

| トリガー | 人がすること | しないこと |
| --- | --- | --- |
| 平日18:30のpublic data PRがmergeされた | 同日株価を使う場合はprivateの更新workflowを起動し、merge後に「今日の夜間運用を実行して」と依頼する | 未mergeのdataで運用しない |
| workflow、data、readinessのどれかが失敗した | エラーまたはreportを確認し、原因を解消するまで`WAIT`にする | 推測で続行したり注文したりしない |
| 夜間runが完了した | report、翌営業日のaction、privateへのpush完了を確認する | PAPERの注文候補を証券会社へ入力しない |
| `assessment-status.json`が`INCOMPLETE`または`STALE` | 表示された不足項目だけを一次資料から会社別入力へ補い、再実行する | 数値を推測で埋めない |
| LIVEで注文候補が作成された | 翌営業日8:45〜8:55にpre-trade checkを行い、`SUBMIT / CANCEL / WAIT`を判断する | 確認できない日は注文しない |
| `SUBMIT`と判断した | 証券会社へ注文を手入力し、判断・約定結果を運用記録へ反映する | 自動送信、未承認注文、裁量での成行化をしない |
| 週末・月末・決算・LIVE移行前の期限が来た | 表示されたreview項目を確認し、LIVE移行時は全証跡を見て最後に明示承認する | 日次の都合だけでruleやgateを変更しない |

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
- [MRS・希薄化・SAM/SOM・10倍経路の計算](stock.jp/docs/operations/decision-assessment-automation-v0.1.md)

Markdown は `npm run lint:markdown` で検査します。
