# 日次PRとnative auto-merge

本人は日次データ・個人分析結果について、毎回の手動merge待ちを避けるため、PR作成と
native auto-mergeを承認しました。mainへ直接pushしません。コード、依存更新、設定、ルール、
risk設定、policy、securityの変更は通常のdraft PRとreviewに残し、自動mergeの対象にしません。
repoはprivate・本人限定です。日次スケジュールが作成済みであることは意味しません。

## 対象と境界

| 種別 | repo / branch | 許可する変更 | 必須CI |
| --- | --- | --- | --- |
| market | stock.jp.private / `daily/market/YYYY-MM-DD` | `stock.jp/data/jquants-archive/` の新規immutable gzipとcurrent.json | `format`、`test`、`guardrail` |
| personal | stock.jp.private / `daily/personal/YYYY-MM-DD` | 当日runの生成済み成果物、当日新規decision、限定runtime state、append-only run-history、monotonic watermark、再計算済みMRS生成証拠/log追記 | `format`、`test`、`guardrail` |

`daily_publication.py` のallowlistを正本とします。ファイル削除・rename・symlink・実行属性変更、
source-config・operation-policy・schedule・コードtree・金融台帳・過去runの変更は拒否します。例外は当日orders.csvと一対一に一致する
`trade-event-ledger.csv`への`PAPER_PROPOSED`追記だけです。既存行を保持し、約定・fee・broker・
資金移動fieldを拒否します。仮想約定の反映もこの例外には含めません。
新しいwatchlist、資金、保有、約定等の変更を日次結果へ紛れ込ませず、別の通常reviewで扱います。
個人結果はPAPER/HUMAN_ONLYと既存run-integrity検証を通過した新しいcompleted runだけです。
stateはbaseとcompleted handoffから既存`_persist_completed_state`を隔離再実行し、全fieldを
一致確認します。pending queue、次回時刻、連続成功回数等を独立に書き換えられません。
queueの更新規則は既存transitionどおりhandoffを正本とし、baseとの独自unionは追加しません。

`market-regime-log.csv`と`research-inputs/market-regime/YYYY-MM-DD.json`は、当日runが
生成した一組だけを許可します。前月までの最新archive sessionを対象とし、既存logを保持して
canonical appendと完全一致確認します。既存証拠の修正・過去risk履歴の変更は通常reviewです。
新規証拠からMRSを再計算し、assessment-statusの値・由来・計算時刻を照合します。
計算thresholdはbaseのsource-configと一致必須です。価格はその設定が明示するprivate archiveを
read-onlyで隔離copyし、使用fileのhash集合をproofへ含め、gateでも再確認します。
archiveがない場合や生成が失敗した証拠だけの場合は自動mergeせず、network fallbackしません。

失敗・中断・同日retryの監査記録は保存用PRを作成してよいものの、自動mergeは有効にしません。

市場データの新規gzipはcurrent generationまたはそこから参照されるobjectに限ります。
一つのPRで一世代だけを公開し、孤立object・中間generationは拒否します。
baseに存在する過去世代は保持します。manifestの余分なfieldや重複keyも拒否します。
市場データは復元・全checksum・秘密情報/個人field・容量・provider/plan・statusを検証します。
初期sampleやpending_resumeは`partial_acquisition`として保存可能ですが、全履歴完了や運用価格の
coverage/readiness合格にはなりません。価格鮮度・全市場98%・active target100%の既存gateは別です。
750 MiBはproject budgetで、GitHubのhard limitではありません。上限を黙って増やしません。

## アシスタントが実行する手順

1. 採用済みvalidatorを使い、単一repoの現在main、未保存成果物、writer/PRの競合を確認する。
   認証は既存の認可済みGitHub connectorを使う。ActionsのAUTOMATION_PATを取り出さない。
2. mainから専用daily branchを作る。同じ日・種類のopen PRがあれば二重作成せず再利用する。
   force pushせず、baseが先行したら競合を解消して意味検証とCIをやり直す。
3. 変更をcommitし、base/headの40桁SHAでローカルの`validate`を実行する。
   合格した内容だけprivate branchへ保存し、remoteのhead SHAを確認する。
4. connectorの`create_pull_request`で **draft: false**、base: mainのPRを作る。
   titleに日付と種類、bodyに取得範囲・partial/complete・検証結果・不足を記載する。
   licensed raw、個人の値、credentialを本文・logへ出さない。
5. exact headの全check、workflow run、legacy statusがterminal successになるまで待つ。
   expected CI名だけでなく、発行appがgithub-actions、期待workflow、全pagination、失敗した
   追加checkも確認する。名前だけが同じ別appのcheckや前のheadの成功を採用しない。
6. GitHubから最新PR・main SHA・private visibility・auto-merge利用可否・全変更path・CIを
   読み取り、下記evidenceを作る。repo管理APIの拒否を迂回しない。
   保護設定を確認できなくてもassistant側で全CI完了を待つ。settings/rulesetは変更しない。
7. `gate`はローカル意味検証を再実行し、30分以内のproofと60秒以内のremote snapshotを照合する。
   head/baseが動いたら停止して再検証する。enable直前にもhead/baseとcheck状態を再読取する。
   一つのwriterだけでこの手順を行い、enable中にbranchを更新しない。
   connectorの`enable_auto_merge`にはexpected head/base SHA引数がないため、再読取と
   single writerはbest-effortです。readとenable間の競合をatomicに防ぐ保証ではありません。
   この制限をrepo保護・review要件の変更で回避せず、競合を観測したら停止して再検証します。
8. eligibleのときだけconnectorの`enable_auto_merge`を呼ぶ。直接merge、admin bypass、
   branch protectionやreview要件の変更はしない。保護が薄いrepoではenableが直ちにmergeを
   発生させ得るため、**未完了CIの段階でenableしない**。native側のrequired reviewは維持する。
9. 戻り値だけでmergedと言わず、PRを再読取してauto_merge/merged、実際のmerge SHAを報告する。
   review待ちは待機中と報告する。enable失敗を直接mergeで回避しない。

コード変更は日次allowlist外です。市場データと個人結果は同じrepoの別PRとして扱います。
利用したprivate commitと市場データ世代hashをrunの由来に記録します。
validatorの--rootはstock.jp.privateのrootを指定し、stock.jp/を指定しません。

Actionsはguardrailのみで、PR作成、auto-merge設定、収集、運用成果物更新は行いません。
このvalidator自体の導入PRや依存更新PRにauto-mergeを付けません。導入は通常review後です。

## ローカルCLI

出力はGit除外の`.usagi/`等へ保存します。proof/evidenceに秘密値やraw行を含めません。

```sh
python scripts/daily_publication.py validate --root <target-checkout> \
  --kind market --run-id YYYY-MM-DD --base <main-sha> --head <daily-sha> \
  --output <ignored-proof.json>
python scripts/daily_publication.py gate --root <target-checkout> \
  --proof <ignored-proof.json> --evidence <ignored-github-evidence.json>
```

personalは同じrepoの採用済みscriptを使い、rootにstock.jp.privateを指定します。
validatorはtarget HEADにあるファイルから隔離copyを作り、canonical stateを変更しません。
個人結果の再開可能性と金融ledgerの整合性は既存run validatorに従います。

## GitHub evidenceの正規化

入力をPR本文・外部コメントから作らず、認可済みGitHub read toolの応答から構成します。
`get_repo`、`get_pr_info`、main ref、PR files、headのcheck-runs/statuses、workflow runsと
reviewsを全page取得します。同workflowの再実行は最新attemptを使い、未知/欠損は失敗とします。
証拠JSONの必須fieldは`allow_auto_merge`の引数schema（コード/合成test）を参照します。

- repository、visibility、auto_merge_allowed、main_sha、fetched_at
- pr: number、state、draft、merged、base_ref、base_sha、head_ref、head_sha、head_repository、
  author_login、mergeable、review_decision（changes requestedがあれば必ず反映）
- reviews_complete（全review取得確認）
- changed_pathsとfiles_complete
- checks: name、head_sha、status、conclusion、app_slug。checks_completeもtrue
- statuses: sha、state。statuses_completeもtrue
- workflow_runs: path、head_sha、status、conclusion、event。runs_completeもtrue

evidence file自体はGitHubの署名証明ではありません。信頼できるassistantがAPIから作る一時記録で、
人手で成功値を埋めません。移動したheadの安全性を過去proofから推測しません。
