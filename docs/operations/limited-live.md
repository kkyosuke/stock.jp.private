# 制限付き実売買

`LIMITED_LIVE`の正本は `config/policy.json` と
[`operation/limited_live.py`](../../src/stock_jp/operation/limited_live.py)。
通常LIVEに必要な正式な過去検証・holdoutとは別に、限定資金での実約定を確認するモード。
資金上限、累積損失停止、新規注文数、追加購入可否はpolicyに定義する。

`operations/evidence/limited-live-plan.json` に診断結果、対象範囲、損失停止率、
専用資金台帳、本人の承認を記録する。公式source coverageと個人リスク・証券会社の
確認が必要。復旧試験は[利用者の指示](../../operations/evidence/recovery-waiver-2026-10-05.json)で必須条件から除外した。

```bash
python -m stock_jp operation limited-live --help
python -m stock_jp operation gates --help
python -m stock_jp operation policy validate
```

本人の未入力項目や`NOT_DECIDED`を実行者が承認済みにしない。
合格した計画だけを適用し、計画のハッシュとpolicyを再照合する。
適用後も注文票と証券会社への発注を分け、発注はHUMAN_ONLYで行う。
通常LIVEへの昇格は[有効なLIVE条件](live-gates.md)に従う。
