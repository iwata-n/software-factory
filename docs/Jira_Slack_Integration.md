# Jira 入力と Slack 通知

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
