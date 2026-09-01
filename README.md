# stock.jp.private

`kkyosuke/stock.jp`の非公開運用状態を、`age`で暗号化して遠隔保存するためのPrivateリポジトリです。

## このリポジトリに保存するもの

GitHub Releasesへ、次の形式の暗号化済みバックアップだけを添付します。

```text
operation-YYYYMMDDTHHMMSS+0900.zip.age
```

バックアップ本体はGitへcommitしません。このREADMEだけを通常のGit履歴で管理します。

## 保存してはいけないもの

- 暗号化前の`.zip`
- 展開済みの`operations/private/`
- `age`秘密鍵
- APIキー、口座番号、認証情報
- 公開用ソースコードや株価データの複製

秘密鍵はこのリポジトリ、GitHub Actions Secrets、チャットのいずれにも保存しません。リポジトリのVisibilityは常に`Private`とします。

## 公開リポジトリとの関係

- 公開側: [`kkyosuke/stock.jp`](https://github.com/kkyosuke/stock.jp)
- 非公開側: `kkyosuke/stock.jp.private`
- 接続方法: GitHub Releaseのupload/downloadだけ
- submodule: 使用しない
- clone: バックアップ運用には不要

ルール、プログラム、テンプレート、公開可能な株価データは公開側を正本とします。保有数量、取得原価、資金、税、注文履歴、実行ログは公開側checkoutにあるGit管理外の`operations/private/`を正本とします。このリポジトリは、その暗号化スナップショットだけを保持します。

## アップロード

公開リポジトリのルートで暗号化と復号検証を完了してから実行します。

```bash
BACKUP_ARCHIVE='operations/private/backups/operation-YYYYMMDDTHHMMSS+0900.zip.age'
BACKUP_TAG='backup-YYYYMMDDTHHMMSS+0900'
gh release create "$BACKUP_TAG" "$BACKUP_ARCHIVE" \
  --repo kkyosuke/stock.jp.private \
  --title "$BACKUP_TAG" \
  --notes 'age-encrypted stock.jp operation backup; plaintext and identity are not included'
```

同じ日時のReleaseを再作成せず、既存assetを上書きしません。

## ダウンロードと検証

公開リポジトリのルートで実行します。

```bash
mkdir -p operations/private/backups/remote-verify
gh release download 'backup-YYYYMMDDTHHMMSS+0900' \
  --repo kkyosuke/stock.jp.private \
  --pattern 'operation-YYYYMMDDTHHMMSS+0900.zip.age' \
  --dir operations/private/backups/remote-verify

.venv/bin/python scripts/operation_backup.py verify \
  --archive operations/private/backups/remote-verify/operation-YYYYMMDDTHHMMSS+0900.zip.age \
  --age-identity /安全なリポジトリ外の場所/stock-jp-backup-key.txt
```

`valid: true`と、ローカル作成物・再ダウンロード物のSHA-256一致を確認して初めて、遠隔コピーを検証済みと扱います。

## 復元

ダウンロードした暗号化ファイルを、公開側の復元コマンドで検証用ディレクトリへ展開します。

```bash
.venv/bin/python scripts/operation_backup.py restore \
  --archive operations/private/backups/remote-verify/operation-YYYYMMDDTHHMMSS+0900.zip.age \
  --age-identity /安全なリポジトリ外の場所/stock-jp-backup-key.txt \
  --destination operations/private/restores/drill-YYYYMMDD
```

稼働中の`operations/private/`へ直接上書きしません。復元後に`state.json`、最新`handoff.json`、各台帳、未照合注文を比較します。

詳しい作成手順と役割分担は、公開側の`docs/repository-roles-and-private-backup-v0.1.md`を正本とします。
