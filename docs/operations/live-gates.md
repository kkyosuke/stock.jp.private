# LIVE判定と人の承認

有効な条件は [`config/policy.json`](../../config/policy.json)、判定処理は
[`operation/evidence.py`](../../src/stock_jp/operation/evidence.py) と
[`operation/policy.py`](../../src/stock_jp/operation/policy.py) が正本。
戦略の定義は [v0.4](../rules/tenbagger-rule-v0.4.md) を参照する。

```bash
python -m stock_jp operation gates --help
python -m stock_jp operation limited-live --help
python -m stock_jp operation policy validate
python -m stock_jp operation readiness check --at <JST日時>
```

証跡は `operations/evidence/` に保存する。データの網羅性、注文・約定の照合、
本人のリスク確認、承認範囲をコードが検証する。経過日数やrun数だけで昇格しない。
不足する証拠は不足として扱う。

復旧試験は利用者の2026年10月5日の指示で必須条件から除外した。
[指示の記録](../../operations/evidence/recovery-waiver-2026-10-05.json)を参照する。
LIVEとLIMITED_LIVEに共通し、試験済みという証跡は作らない。
過去検証の入力・実行方法は[履歴再生](historical-replay.md)を参照する。

運用モードは `config/policy.json` で確認する。`PAPER`、`LIMITED_LIVE`、`LIVE` を
区別し、注文票の作成と証券会社への発注を区別する。発注境界は `HUMAN_ONLY`。
本人の承認記録を自動生成・再解釈しない。

限定資金で先に実約定を確認する条件は[制限付き実売買](limited-live.md)を参照する。

配置変更は資金上限・消費予算・承認内容を変更しない。承認対象のコードや配置版が
変わった場合は、その証跡の検証が通るまでLIVE判定を停止する。
