# 公式情報源スキャン v0.1

- 発効日: 2026年8月31日
- 目的: 夜間エージェントが読むべき一次資料を漏れなくキュー化し、取得不能を売買停止条件へ変換する
- 保存先: public Gitでは無視し、親のprivate Gitで追跡する`operations/private/runs/YYYY-MM-DD/`

## 1. 取得範囲

機械取得と一次サイト確認の境界は次のとおり。

| 情報源 | 取得内容 | 認証 | coverageへの効果 |
|---|---|---|---|
| EDINET API v2 | 日付別の提出書類一覧 | `EDINET_API_KEY` | 全日成功時に`edinet=CHECKED` |
| JPX上場銘柄一覧 | 内国株式の銘柄・市場・33/17業種をrun時点で保存 | なし | 取得不能時は候補選定を停止 |
| TDnet公開ページ | 適時開示と検索範囲0件の証跡 | なし | エージェント確認後に`tdnet=CHECKED` |
| 会社IR | 決算、KPI、株式数、コーポレートアクション | なし | 対象ごとの確認後に`company_ir=CHECKED` |
| JPX公開ページ | 売買停止、監理・整理、上場廃止、現物株休業日 | なし | 確認後に`jpx=CHECKED`、翌営業日確定 |
| `data/daily-prices/` | PR #14形式の候補探索用日足 | なし | 価格候補を作るが、単独では公式確認を閉じない |

TDnetと会社IRは、エージェントが一次サイトを確認する`PENDING`タスクとして毎回作る。公式価格、コーポレートアクション、JPX現物株カレンダーも一次サイト確認タスクとし、証跡が付くまで重大データ不足を開いたままにして注文票を作らない。この運用ではJ-Quantsを使用せず、設定の全dataset flagを`false`に保つ。

公式仕様の確認先:

- [EDINET公式のAPIキー案内](https://disclosure2.edinet-fsa.go.jp/week0020.aspx)
- [JPXのTDnet APIサービス](https://www.jpx.co.jp/markets/paid-info-listing/tdnet/02.html)
- [TDnet適時開示情報閲覧サービス](https://www.release.tdnet.info/inbs/I_main_00.html)
- [JPX休業日一覧](https://www.jpx.co.jp/corporate/about-jpx/calendar/)

## 2. 認証情報

EDINETのAPIキーは環境変数だけで渡す。JSON、チャット、ログ、コマンド引数、Git管理下へ値を書かない。J-QuantsのAPIキーは設定しない。

~~~bash
export EDINET_API_KEY='EDINETで発行した値'
~~~

変数名、エンドポイント、timeoutは`operations/private/source-config.json`で設定する。値そのものは保存しない。公開ページ確認には認証値を保存せず、URL、確認範囲、公開日時、取得日時を`sources.csv`へ残す。

## 3. 実行

`prepare`が返したrun IDとtokenをそのまま渡す。

~~~bash
.venv/bin/python scripts/official_source_scan.py \
  --run-id 2026-08-31 \
  --run-token '<prepareが返したrun_token>' \
  --cutoff 2026-08-31T18:30:00+09:00 \
  --at 2026-08-31T18:35:00+09:00
~~~

前回成功カットオフの10分前から当日カットオフまで、日付単位で重ねて取得する。初回だけ`initial_lookback_days`を使う。カットオフ後に公開された行はrawへ保存しても当日判断へ入れず、次回の重複期間で処理する。

取得結果は次へ保存する。

- `provider-health.json`: EDINETの成功・件数・認証不足と一次サイト確認の進捗
- `research-queue.json`: 新しい対象開示、TDnet、会社IR、JPX、公式価格・取引日確認
- `sources.csv`: query範囲の証跡と対象銘柄の一次資料ID
- `raw-sources/*.json`: privateの生レスポンス
- `reference-data/manifest.json`: 正規化ファイル、行数、取得元、SHA-256、private限定の来歴
- `reference-data/jpx-listed-master.csv.gz`: 認証不要のJPX内国株式スナップショット
- `trading-calendar.json`: JPX一次資料で本人が確認した翌営業日の判定入力
- `forecast-history.csv`、`share-count-history.csv`、`earnings-calendar-history.csv`: runをまたぐ重複排除済み履歴
- `corporate-actions.csv`: 会社IR・JPX一次資料で確認したイベントを未適用として追記
- `coverage.json`: 機械取得または一次サイト証跡が揃った情報源だけ`CHECKED`
- `handoff.json`: 未解決の重大データ不足

検索結果0件も、0件だった検索日・URL・取得日時をquery証跡として残す。0件と取得失敗を同じにしない。

## 4. エージェントが続ける作業

1. `research-queue.json`を優先度順に処理する
2. TDnet、対象会社IR、JPX告知、公式価格と取引日を一次サイトで確認し、`sources.csv`へ検索範囲付き証跡を追加する
3. 新規開示タスクは`S-A`を先に判定し、必要な判断ログを作る
4. 完了タスクを`COMPLETED`にし、`evidence_source_ids`を付ける
5. 翌日へ送る作業だけ`DEFERRED`にし、同じtask IDを`handoff.pending_reviews`へ入れる
6. 手動フォールバックで取得漏れを解消した場合は、該当gapを`RESOLVED`にして解決日時と証跡IDを残す
7. 全対象を確認後に`coverage.json`を`COMPLETED`へする

APIキー不足や取得失敗を推測で埋めない。公式価格、コーポレートアクション、取引日は一次資料で手動確認する。未解決の重大gap、`PENDING`タスク、一次資料のない`COMPLETED`タスクが1件でもあれば、日次実行の`complete`は拒否される。public側のfixtureは架空データだけを使う。
