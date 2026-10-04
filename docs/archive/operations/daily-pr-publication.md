# 単一repoの日次PR

市場データも個人結果もstock.jp.privateのPRへ保存します。marketは
`stock.jp/data/jquants-archive/`、personalは`operations/private/`の限定生成ファイルです。
混在PR、コード、設定、policy、金融実行、過去履歴の変更は通常reviewへ戻します。
[共通validatorと詳細手順](pr-publication.md)を正本とします。
両種別ともformat・test・guardrailのexact-head成功、意味検証、最新head/baseとprivate可視性の
確認が必須です。PAPER_PROPOSED/HUMAN_ONLYを維持し、取得成功をnightly完了としません。
拒否済み新規licensed uploadはサポート解決まで再試行・迂回しません。

```sh
python stock.jp/scripts/daily_publication.py validate --root . --kind market \
  --run-id YYYY-MM-DD --base <main-sha> --head <daily-sha> --output .usagi/proof.json
```

市場データと個人結果で別のdaily branchを使い、常に一つのwriterで進めます。
本移行PRはコード・設定変更のdraftであり、auto-merge対象外です。
