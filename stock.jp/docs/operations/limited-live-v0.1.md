# LIMITED_LIVE v0.1

`LIMITED_LIVE`は、v0.4を通常LIVEへ昇格する前に、本人が明示した小さな専用資金枠だけで
実約定と運用手順を観察するための制限モードである。回顧的backtestをforward holdoutと
みなさず、通常LIVEのgateも免除・書換えしない。

## 強制する制限

- `capital_limit_jpy`を保有取得原価、未約定買付、次の買付の合計に対する絶対上限とする
- `capital-ledger.csv`に`LIMITED_LIVE_FUNDING`を1行だけ記録し、入金額と開始残高を
  `capital_limit_jpy`へ一致させる
- 1 runに作成できる新規・追加注文は合計1件とする
- `additional_purchases_enabled`は`false`に固定し、追加購入を禁止する
- 本人が決めた`maximum_total_loss_pct`へ到達したら新規・追加注文を拒否する
- MRSが`NORMAL`または`CAUTION`で、対象assessmentが`PASS`の場合だけ買付候補を作る
- 公式source、run integrity、cash、portfolio、未照合注文のいずれかが不明なら買付を拒否する
- broker送信は常に`HUMAN_ONLY`とし、翌朝のpre-trade確認後に本人が手入力する
- `SELL`と`REDUCE`はriskを減らすため、買付用のpilot制限到達後も作成できる

## 昇格前提

`operations/private/evidence/limited-live-plan.json`を本人が確定する。planは2025年の
allocation diagnosticのhashと最大DD、専用資金上限、累積損失停止率、本人承認へ拘束する。
さらに次の3条件を元データから再判定する。

1. 最新の完了済みv0.4 PAPER runの公式source coverage
2. 現行repository layoutのclean-clone確認
3. 90日以内の本人risk・税・broker・security checklist

資金を実際に分離した後、昇格前に次の形式で`capital-ledger.csv`へ記録する。未入金の予定額や
他戦略と混在した残高は記録しない。`amount_private`と`running_cash_private`の両方がplanの
上限と一致しなければ昇格しない。

~~~csv
limited-live-funding,2026-09-06T09:00:00+09:00,LIMITED_LIVE_FUNDING,3000000,3000000,,,operations/private/evidence/limited-live-plan.json,dedicated limited live sleeve
~~~

上記のどれかが失効した場合、`LIMITED_LIVE`の新規・追加注文は停止する。過去診断は
`ALLOCATION_DIAGNOSTIC_ONLY`かつ`forward_paper_gate_satisfied: false`でなければならず、
本人がその制約と最大DDを確認する。

~~~bash
.venv/bin/python scripts/limited_live.py status
.venv/bin/python scripts/limited_live.py apply
.venv/bin/python scripts/operation_bootstrap.py check
~~~

`apply`はplanと3条件がすべて合格した場合だけ、PAPER policyを原子的に
`LIMITED_LIVE`へ変更する。policyはplanのSHA-256と制限値を保持し、以後のbootstrapと
買付注文作成で再検証する。planや参照証跡を変更した場合はhash不一致で停止する。

## 通常LIVEとの関係

LIMITED_LIVEの損益、約定数、経過期間だけでは通常LIVEへ自動昇格しない。通常LIVEには、
point-in-time全母集団、2025〜2026回顧的replay、公式coverage、clean-clone、本人checklist、
v0.4 forward holdout、最終LIVE承認の全条件が引き続き必要である。

通常LIVEへ進む場合は`promote-live`が`PAPER`または`LIMITED_LIVE`からのみ昇格を許可し、
全証跡を再計算する。LIMITED_LIVEを終了するだけならpolicyを`PAUSED`へ戻し、未照合注文を
解消してから資金を移動する。
