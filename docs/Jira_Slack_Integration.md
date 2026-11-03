# Jira 入力と Slack 通知

## Web の連携設定

Control Plane に `FACTORY_INTEGRATION_KEY` を設定して再起動する。キーは
`uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`
で生成し、DB バックアップとは別の秘密ストアに保管する。復元先にも同じキーが必要。
`/operations` に管理者の User トークンで接続し、「連携設定」を開く。
認証情報の入力には HTTPS または同じ端末のループバックを使う。

- **Jira Cloud** — Jira サイト、プロジェクトキー、TaskSpec 欄の ID、取り込み先の
  Factory Project を指定する。トークン方式では Jira アカウントのメールアドレスと API
  トークンを保存する。保存後は課題キーを指定して取り込める。
- **Slack Bot** — Slack のチーム ID と通知先チャンネル ID を指定する。
  トークン方式では Bot トークンを保存する。Bot を通知先チャンネルに参加させる。
  保存後は Control Plane が保留中の通知を配送する。設定画面からも 1 件ずつ配送できる。
- **GitHub CI** — CI 読み取り用 PAT を保存する。対象リポジトリと Webhook の
  関連付けは `FACTORY_GITHUB_WEBHOOKS` で指定する。関連付けごとの署名シークレットも
  画面から登録できる。Web の設定を解除すると対応する環境設定に戻る。
  Publisher の書き込み認証情報と Worker の Codex CLI ログインは各ホストで管理する。

### OAuth

`FACTORY_OAUTH_ORIGIN` にブラウザから到達可能な HTTPS オリジンを指定する。
プロバイダー側で OAuth アプリを作成し、次のコールバック URL を登録する。
リバースプロキシで元のホストと HTTPS を伝え、コールバックのクエリをアクセスログに出さない。

| プロバイダー | コールバックのパス | 権限 |
| --- | --- | --- |
| Jira Cloud | `/v1/operations/integrations/jira/oauth/callback` | `read:jira-work`。認可要求には `offline_access` も含む |
| Slack | `/v1/operations/integrations/slack/oauth/callback` | Bot の `chat:write`、`channels:history`、`groups:history` |

画面で OAuth を選択し、Client ID と Client Secret を保存する。「OAuth 認可リンクを発行」
から別タブで認可し、設定画面に戻って「状態を更新」を押す。
期限付きトークンはプロバイダーが発行したリフレッシュトークンで更新する。
Web 側の設定削除と、プロバイダー側のアプリ認可取り消しは別操作。

## Jira

評価対象は環境ごとに指定する専用Jira Project。Control Plane は信頼済み
リレーが専用欄から取得した構造化 TaskSpec を受け取る。Jira 側のイベント受信、Jira API での
認証と専用欄の取得、版の採番はリレー側の責務。

`FACTORY_JIRA_MAPPING` に `integration_id`、`site`、`project_key`、Factory の
`workspace_id`、`project_id`、有効な USER の `principal_id`、Jira の `field_id` を
JSON オブジェクトで指定する。リレー用の `FACTORY_JIRA_RELAY_TOKEN` は秘密ストアに置く。
設定を外して Control Plane を再起動すると Jira の経路が無効になる。Factory のユーザー認証情報
をリレーに渡す必要はない。

リレーは `POST /v1/integrations/jira/{integration_id}/issues` に Bearer 認証付きで、
`site`、`project_key`、`issue_id`、`issue_key`、`source_version`、
`fields` を送る。`source_version` は同一 Issue 内で順序を比較できる整数で、再配送時は
変えない。`fields` は専用欄の ID と TaskSpec オブジェクトまたはその JSON 文字列だけを含む。
自由記述欄をリレーの入力に含めない。

`IMPORTED` は Factory の Task ID と Spec 版を返す。実行開始には通常の Run API を使う。
`NEEDS_INPUT` または `QUARANTINED` を修正したら Jira の新しい版として送る。
`STALE` は既に処理した版より古いことを示す。Issue の参照は Jira 台帳に保存される。

## Slack

評価対象のチャンネルは非公開の環境設定で指定する。配送プロセスには
`FACTORY_DATABASE_URL`、`FACTORY_SLACK_WORKSPACE_ID`（Factory の workspace ID）、
`FACTORY_SLACK_CHANNEL_ID` と秘密ストアの `FACTORY_SLACK_BOT_TOKEN` を設定する。
チャンネルへの書き込み権限と、障害時の照合に必要な履歴・メタデータの読み取り権限を
Slack アプリに付与する。設定済みの環境で `python -m software_factory.slack_delivery` を
定期実行すると、その時点で配送可能なレコードを処理する。

`GET /v1/runs/{run_id}/notifications` で配送キー、試行回数、配送状態、Slack メッセージ ID
を確認できる。明確に拒否された一時エラーは最大 3 回試行する。結果が不明な場合は
チャンネルの直近履歴からメタデータを照合し、確認できなければ `UNKNOWN` で止める。
`UNKNOWN` と `FAILED` は運用者が確認する。

SF-19 の公開成功処理では、成功を確定するトランザクション内で
`slack_delivery.enqueue_result(session, run, pr_url=..., ci_urls=...)` を一度呼ぶ。
失敗と BLOCKED への遷移は Control Plane が同じ Outbox 契約で記録する。

## 実サービスの評価手順

1. Jiraの評価用Issueの専用欄に評価対象のTaskSpec JSONを入れる。リレー実行環境に `FACTORY_JIRA_MAPPING`、`FACTORY_JIRA_RELAY_TOKEN`、`FACTORY_JIRA_EMAIL`、`FACTORY_JIRA_API_TOKEN` を読み込ませる。Jiraの認証は対象Projectを読み取れるアカウントで行う。
2. `uv run python -m software_factory.jira_relay "$JIRA_ISSUE_KEY" --api-url "$FACTORY_API_URL"` を実行し、`IMPORTED`、Issue URL、Task ID、Spec版を記録する。同じIssue版を再送し、TaskとSpecの重複がないことを確認する。
3. インポートしたTaskからRunを作成する。GitHub CIまで確定した後、上記の配送コマンドを評価Workspaceとチャンネルで実行し、Run APIで `SENT` とメッセージIDを確認する。Slack上のメッセージが対象Run・PR・CI・費用を示すことを照合する。
4. 固定のCodex評価3件では、評価用チャンネルとBotトークンを設定して `scripts/evaluate_codex.py` に `--deliver-slack` を追加する。専用の一時DBに記録した通知を既存の配送処理で送り、Run APIのメッセージIDをSlack履歴と照合する。結果は各評価出力の `evaluation.json` に保存する。秘密は実行環境に読み込ませ、証拠ファイルや設定のコミットに含めない。
