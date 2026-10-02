# 非公開データ

正本はprivateの `kkyosuke/stock.jp.private` 内の `stock.jp/data/` です。`jquants-archive/` は本人限定の契約市場データ保存先です。
旧検証データとは別管理し、公開配布やActionsへの入力をしません。
[保存・復元・削除条件](../docs/private-git-market-data.md)を参照してください。

## `tenbagger-survivor-price-episodes-2016-2026.csv`

2026年7月末の東証上場銘柄一覧に含まれる内国普通株3,713銘柄を対象に、2016年8月から2026年8月までの価格系列から最初の `10x-2Y` / `10x-3Y` エピソードを抽出した派生データである。

- 上場銘柄: JPX「東証上場銘柄一覧」2026年7月末版
- 株価: Yahoo Finance chart endpointの日次終値
- 株価最終日: 2026年8月28日
- 評価日: 各月末最終取引日
- 投資開始価格: 評価日の翌取引日終値
- 成功: 2年または3年以内の月末終値が投資開始価格の10倍以上
- 収録単位: 銘柄ごと、各期間で最初の成功エピソード

列名の `peak` は判定期間内の月末最高値、`lifetime_peak` は投資開始後からデータ最終日までの日次最高値を表す。`latest_multiple`、`post_hit_max_drawdown` などは10倍到達後の結果確認用で、候補選定には使っていない。

このデータは現在も上場している銘柄だけを含む。上場廃止、合併、コード変更を完全には追跡しておらず、全母集団の発生率、適合率、再現率、リフトは計算できない。Yahoo Financeは候補探索用の二次データであり、投資判断または最終バックテストではJPXの調整株価とコーポレートアクションで再照合する。

## `tenbagger-survivor-price-episodes-2016-2026.quality.csv`

コード再利用、再上場、価格調整不整合の疑いがあるため保守的に除外した27コードの監査ログである。出来高0の休場日プレースホルダーを除いたうえで、隣接する調整後終値が2.5倍を超えるか0.4倍未満、または実際に価格が付いた日の間隔が45暦日を超える場合に記録する。正当な株式分割、長期売買停止、極端に売買が少ない銘柄を含む可能性があるため、「失敗銘柄」ではなく要手動確認として扱う。

## 再生成

~~~bash
python scripts/tenbagger_price_scan.py \
  --jpx-xls path/to/data_j.xls \
  --output data/tenbagger-survivor-price-episodes-2016-2026.csv \
  --scan-start 2016-08-01 \
  --data-start 2015-07-01 \
  --data-end 2026-08-30
~~~

JPXの月次Excelは差し替えられるため、厳密な再現には取得日と原本の保存が必要である。生の株価キャッシュとJPX Excelはリポジトリへ収録していない。

## `tenbagger-v0.2-price-only-pnl-2016-2026.csv`

上記の現存テンバガー83銘柄へ、v0.2のうち価格系列だけで確定できる売却条件を適用した銘柄別損益である。各銘柄の初期投資を1単位、`Q0 = 1` として正規化し、売却代金と残存株の最終日評価額を分けて記録する。

このCSVは、事後に判明した成功銘柄に対する売却ルールの診断であり、v0.2の銘柄選択またはポートフォリオのバックテストではない。購入時の100点スコア、非成功銘柄、上場廃止、財務・開示依存の売却条件、費用と税を含まない。前提、合計損益、適用条件は[参考損益レポート](../docs/research/tenbagger-v0.2-price-only-pnl-2016-2026.md)を参照する。

~~~bash
.venv/bin/python scripts/tenbagger_v02_price_exit_sim.py
~~~

## `tenbagger-v0.4-allocation-replay-2025-*`

完全取得済みの2025年公式日足243営業日を使い、2024年末までの価格・流動性だけで固定した代理12銘柄へv0.2とv0.4の資金配分を適用した12か月診断である。

- `candidates.csv`: 将来価格を使わず固定した代理候補と順位
- `trades.csv`: 100株単位・指値・各配分上限・0.15%費用を反映したモデル約定
- `daily.csv`: v0.2/v0.4の日次資産、現金、最大DD計算用系列
- `summary.json`: 機械可読の前提と集計

point-in-time財務・開示・希薄化・SAM/SOM・MRSがないため、完全なテンバガールールのバックテストでも、最低12か月の前向きPAPERの代替でもない。詳細は[実験レポート](../docs/research/tenbagger-v0.4-allocation-replay-2025.md)を参照する。

~~~bash
.venv/bin/python scripts/tenbagger_v04_allocation_replay.py
~~~
