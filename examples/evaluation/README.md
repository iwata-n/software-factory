# 固定評価fixture

`app.py` は実装計画のEVAL-01〜03向けの不完全なbaseです。既存の非対象APIは `stable_label` です。

`checks` は管理者が固定するgolden testです。各RunのProfileは対象のcheckと `check_compat.py` を選択し、全ファイルのSHA-256を保護manifestへ固定します。Agentには `app.py` だけの変更を許可します。通常のプロジェクトpytest対象には含めません。

baseでは対象golden testが失敗し、compatが成功することを先に記録します。実Agentによる変更後は同じProfileで再実行し、Candidate/Artifact/実CI/通知までを評価batchへ記録します。fixtureの追加は実タスク3件の合格実績には数えません。

実GitHub Actionsでは `.github/workflows/factory-evaluation.yml` を評価用baseに含めます。baseブランチは `codex/factory-evaluation/` 配下、同一Repositoryの候補ブランチは `codex/factory/` 配下です。3件を独立したジョブで実行するため、各Runは自身の対象ジョブ（`factory-eval-empty`、`factory-eval-count`、`factory-eval-spaces` のいずれか）を必須チェックに指定します。対象ジョブと互換性テストの結果を使い、他の未修正fixtureはそのRunの判定から分離します。

ProfileにはGitHubが発行したworkflow IDと `.github/workflows/factory-evaluation.yml` を設定し、保護manifestに同ファイルのSHA-256を含めます。workflowはPR headを明示的に取得し、`Verify candidate head` の成功後に固定golden testを実行します。
