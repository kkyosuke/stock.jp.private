# 市場データの非公開保存・移行手順

## この変更の範囲

raw市場データとmanifestをpublic repositoryから切り離し、削除可能なprivate local領域へ
保存・参照するための構成です。汎用コード・ルール・合成fixtureはpublicに残します。
取得元を閲覧できることと、自動取得・保存・再配布できることは同じではありません。
private保存への変更だけで取得・分析が許可されるとは扱いません。

この変更は新しい収集、API実行、データの移動・削除、履歴書換え、remote保存を行いません。
既存public archiveとその履歴は残っているため、設定変更だけで過去の公開を取り消したとは
報告しません。必要な対応は利用条件・既存データ・保存先を確認し、別途承認して実施します。

J-Quants Lightの契約確認に基づき、providerと取得設定を有効化します。実取得には認可された
実行環境への安全な資格情報登録と永続保存先が必要です。risk gateは変更せず、Yahoo bulk取得は停止します。
秘密情報はGit・チャット・logに保存せず、認可された実行時の秘密情報管理経路だけで利用します。

## 設定と保存先

正本の設定は`operations/private/source-config.json`です。schemaは`1.5`です。

~~~json
{
  "price_source": {
    "storage": "private_local",
    "archive_root": "operations/private/market-data/daily-prices",
    "manifest_path": "latest.json"
  }
}
~~~

上記はstorage項目だけの抜粋です。設定ファイル全体をこれで上書きしません。
`archive_root`はpublicコードrootからの相対pathです。通常のworkspaceでは
`stock.jp/operations/private -> ../../operations/private`を通してprivate正本へ解決します。
archiveは解決後の`operations/private/market-data/`内に限り、publicや別領域への脱出を拒否します。
設定・archive・manifestが欠けてもpublic `data/daily-prices/`へfallbackしません。

raw CSVと`latest.json`は同じarchive内に置き、全体をprivate `.gitignore`で除外します。
manifestも取得データの一部としてGit履歴やActions artifactへ保存しません。
状態・台帳・判断証跡は従来のprivate Git保存を維持しますが、派生物にも取得元の利用条件が
適用される場合はそれに従います。秘密情報はこのraw領域にも置きません。

## 移行前に確認すること

1. 契約対象のprovider、取得可能な期間・dataset・更新時刻・件数制限を確認する
2. 自動取得、private保存、AI等での分析、派生物、削除義務、アクセス可能な人の条件を確認する
3. 安全な資格情報登録と、認可済み実行環境からの参照方法を確認する
4. private localの保存先・利用者・容量・永続性・必要な削除手順を確認する
5. 過去のpublic archiveとGit履歴を別件として確認し、移動・削除等が必要なら承認を得る
6. provider実装・設定のreview後、許可された範囲の履歴と差分だけを取得する
7. coverage、鮮度、checksum、PAPER readinessを検証し、不足はWAITとして記録する

既存public rawを自動copyして穴埋めしません。新しい取得、既存データの移動、外部backup、
remote uploadはそれぞれ許可された範囲だけで実施します。
配置前にGit ignoreが効くことを`git check-ignore`等で確認します。既にGit追跡されたファイルは
ignore追加だけでは追跡解除されないため、追跡状況の確認も必要です。

## CI、保存済み運用記録、実行環境の検証境界

required `format`は`python scripts/validate_checkout.py`で追跡済みstate/schemaと
`PAPER` / `HUMAN_ONLY`、LIVE注文不許可だけを隔離copyで検証します。
raw市場データを読み込まず、暗黙migrationは採用せず失敗にします。
別stepの20日smokeは合成fixtureだけです。成功しても実データreadinessではなく、
出力は`runtime_readiness: NOT_EVALUATED`、`paper_go: null`です。

定期`operation-health` Actionsは`--persisted-only`を使い、保存済みrun state、
履歴、最終成功時刻、cutoff、次回予定、未解決gap、policy、public pointer差分を監視します。
runが存在しない、失敗、履歴と状態の不一致、未来時刻、96時間超の古い成功記録はATTENTIONです。
古いrunが最新データの検証を代替することはありません。このモードも
`PERSISTED_RUN_METADATA_ONLY` / `NOT_EVALUATED` / `paper_go: null`を明示します。
有効な最近の記録があればraw未配置のclean cloneでも記録監視は成功できます。
新しい成功記録やruntime evidenceを今回捏造・更新しません。

実行環境では`python scripts/operation_health.py --offline --output <private-report.json>`
または通常のbootstrapを使い、明示したprivate archiveを厳密に検証します。
health既定モードはpublic helperで解決したarchiveだけを使い捨てprivate領域へcopyします。
archive内symlinkを拒否し、状態migrationや一時archiveの変更を検出して失敗します。
これは合成検証や保存済み記録監視とは別の検証です。欠損時はATTENTION/非zeroであり、
public archiveへfallbackしません。coverage・鮮度・checksum等のruntime基準は変更しません。

clean cloneから実運用する際は許可されたprivate dataを別途配置してください。
rawをGit、ログ、artifact、cacheへ置いてCIを通すことは禁止します。
コードmerge、契約プラン確認、安全なcredential設定、データ取得、runtime検証、
定時実行の成功確認はそれぞれ別の完了条件です。

## J-Quants Light runtime configuration

The owner confirmed the Light plan. The private source configuration is schema
1.5, with J-Quants V2 enabled and `plan: light`; no paid add-ons are assumed.
The generic public implementation retains an unset plan template. Full-history
acquisition and incremental correction storage use the private sibling archive
`operations/private/market-data/jquants`; normalized daily prices use
`operations/private/market-data/daily-prices`.

Run `python scripts/jquants_ingest.py --catalog` and then
`python scripts/jquants_ingest.py` from the linked public-code checkout in the
approved persistent runtime. The catalog is offline; the collection command
requires a replacement key securely registered as `JQUANTS_API_KEY` in that
runtime. A GitHub repository secret does not become readable by an external
collector. Do not store the key in source configuration or command history.
See `stock.jp/docs/jquants-private-ingestion.md` for coverage, resumption,
correction, private-storage and licensing details.

No live authenticated request or licensed data download was performed during
implementation. Missing raw data and unverified latest evidence must remain
ATTENTION/blocked in runtime health checks. GitHub Actions remain guardrail-only;
these configuration changes do not authorize hosting licensed records in Actions
artifacts, public Git, public buckets or third-party model training.
