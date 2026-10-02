# 日本株運用・継続稼働準備 v0.1

- 目的: 夜1回の依頼を長期間、安全に繰り返せるかを機械判定する
- 対象: [夜間ワンショット運用](nightly-operation-v0.1.md)
- 原則: `paper_go: true` になるまで PAPER を開始せず、`live_go: true` になるまで実注文を行わない

## 1. 初回チェック

private リポジトリで workspace を準備し、public submodule 内のコマンドを実行する。

~~~bash
./scripts/setup-workspace.sh
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py migrate
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
~~~

public リポジトリを単独で試す場合は、同じコマンドを public root から `.venv/bin/python scripts/...` として実行できる。

PAPER の機械ゲートは、状態 schema、運用 policy、明示設定した非公開株価 archive を検証する。active 対象0件は許可し、`GLOBAL / NO-ACTION` と初回全市場候補レビューだけを行う。EDINET API key がない PAPER は手動一次資料 fallback を許すが、確認できない判断は `WAIT` または `fail` にする。API key はファイルやチャットへ保存せず、実行環境の `EDINET_API_KEY` へ設定する。

株価 archive は `operations/private/market-data/daily-prices/latest.json` と対応 CSV を使い、SHA-256、全市場98%以上、active 対象100%、未来日でないこと、7日以内の鮮度を検証する。provider identityを検証し、注文判断には価格・corporate action と会社一次資料を別途確認する。J-Quantsの取得成功だけで全ての意思決定gateを通過したとは扱わない。

このチェックは外部スケジューラが起動できることまでは保証しない。対象端末、private checkout、実行環境、毎晩21:00頃の確認スケジュール（通常runは平日）、通知経路を別途確認する。

## 2. 20営業日のoff-line状態遷移試験

~~~bash
.venv/bin/python scripts/operation_smoke.py \
  --days 20 --start-date 2026-09-01
~~~

この試験は一時フォルダだけを使い、ネットワーク接続、実データ、実注文を使わない。`completed_runs: 20`、`consecutive_successful_runs: 20`、`broker_orders_submitted: 0` を確認する。これは状態遷移の回帰testであり、LIVEリリースの待機日数または実データrun数を要求するものではない。

## 3. 実行漏れと古い lock

~~~bash
.venv/bin/python scripts/operation_watchdog.py check \
  --at 2026-09-01T21:00:00+09:00
~~~

- `OK`: 次回日時より前
- `DUE`: 次回日時を過ぎたが猶予内
- `RUNNING`: 正規の夜間実行が進行中
- `MISSED`: 次回日時から既定120分を超えて成功実行がない
- `STALE_RUN`: 未完了実行の lease が切れている、または lease がない
- `NEEDS_FIRST_RUN`: まだ成功実行がない

`MISSED` でも前回の成功 cutoff から差分を再取得し、確認範囲を飛ばさない。`STALE_RUN` は既存フォルダを run token で再開し、新しい注文票を別に作らない。

## 4. private リポジトリの同期

実行状態、台帳、日次成果物は private リポジトリの `operations/private/` が正本である。application-level の暗号化 archive、`age`、GitHub Release backup は使わない。

各成功 run の後に次を確認する。

1. state と全台帳の検証が成功している
2. 実行途中の一時ファイルや認証情報が含まれていない
3. private 変更と採用した public submodule commit が同じ commit に記録されている
4. protected private remote へ push 済みである

未照合注文、検証失敗、push 失敗がある間は次の注文候補を増やさない。強制 push や履歴改変はしない。

## 5. clean-clone 確認

LIVE 昇格前に一度、別ディレクトリへの clean clone で現行構成を再現できることを確認する。setup、コード配置、状態 schema、台帳構成を変更した場合は `REPOSITORY_LAYOUT_REVISION` を上げ、変更後に再確認する。日次run、データ更新、単なる経過日数では再確認しない。

~~~bash
git clone --recurse-submodules <private-repository-url> <new-directory>
cd <new-directory>
./scripts/setup-workspace.sh
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
~~~

稼働中の checkout は上書きしない。最新 `state.json`、成功 run、handoff、全台帳、未照合注文、private commit、public submodule commit を照合し、結果を private 側の LIVE gate evidence に記録する。別媒体の mirror は要求しない。GitHub を利用できない間は新しい注文を止め、復旧後に状態を再検証する。

少額の実約定で運用手順を先に観察する場合は、通常LIVEの判定値を変えず
`LIMITED_LIVE`を使う。資金上限、損失停止、追加購入禁止、1 run 1新規注文、手入力発注は
[専用仕様](limited-live-v0.1.md)で別途fail-close判定する。

## 6. CI

`.github/workflows/operation-tests.yml` は PR と main 更新時に、全単体テスト、Python compile、skill 構成、20営業日 simulation を実行する。CI は公開 fixture だけを使い、API key や `operations/private/` をアップロードしない。

CI 成功は実情報源の疎通や投資成績を保証しない。初回チェック、夜間 report のデータ欠損、現行構成の clean-clone 確認を併用する。

## 7. 継続運用の最小周期

| 時期 | 実施内容 |
| --- | --- |
| 毎晩21:00頃 | 平日は`$japan-stock-operator`で通常runを1回実行。土日は期限到来レビューと未完了項目のみ |
| 翌朝8:45〜8:55 | 昇格済み LIVE の `PROPOSED` 注文だけ人間が確認 |
| 各成功 run 後 | state/台帳を検証し、private remote との同期を確認 |
| 毎週 | `run-history.csv` の失敗・漏れ・未照合注文を確認 |
| 四半期 | 戦略成績 review |
| LIVE前・repository layout変更後 | clean-clone確認 |
| ルール・schema変更前 | private commit/push、回帰test、必要に応じたshadow評価 |

重大データ不足、実行漏れ、復旧不能、未照合注文が残る場合は、新しい注文票を増やさず問題を先に解消する。

## 8. LIVE 昇格の必須ゲート

次を独立した必須ゲートにする。PAPER期間と連続run数には最低値を設けない。

- point-in-time 全母集団の履歴再生を受け入れ済み
- 最新の完了済みPAPER runの公式情報源coverageを受け入れ済み
- private repository の clean-clone 復旧を確認済み
- 利用者固有の損失許容、税、証券会社仕様、注文可能時間を確認済み
- v0.4を LIVE 適用ルールへ昇格済み
- 利用者が `LIVE` と日付を明示して承認済み

365日の経過と20営業日の連続runは任意の運用品質diagnosticとして記録できるが、LIVEを阻害しない。コード実行だけで本人のリスク判断と証券会社設定を代替しない。すべての gate と evidence が有効でも、発注は必ず人間が最終確認する。

各 gate の証跡形式と機械判定コマンドは [LIVE gate 証跡の機械判定](live-gate-evidence.md) を正本とする。未達の判定結果は証跡ファイルとして保存できず、真偽値だけを手で変更しても LIVE 昇格を許可しない。

## 非公開入力への移行

[非公開株価入力とActions](daily-stock-price-actions-v0.1.md)が現在の保存先・取得制限の正本。Yahoo一括取得CLIは停止中。J-Quants接続の安全な設定完了までは、指定private archiveが欠損ならpublic archiveへfallbackせずWAITとする。IR・開示の調査だけを別途継続しても、nightly成功とは記録しない。
