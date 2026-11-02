# Software Factory

[設計書 v0.4](docs/Software_Factory_Design_v0.4.md) に基づくソフトウェアファクトリーの初期実装です。[実装計画](docs/Implementation_Plan.md) にタスク、依存関係、完了条件、受け入れ試験の対応を記載しています。

現在はFastAPIのControl Plane、PostgreSQL、Leaseで接続する別プロセスWorker、Fake Agentを実装しています。`PLAN → 計画検証 → IMPLEMENT` を模擬実行し、Candidate未実装のため `BLOCKED` で停止します。実コード生成、独立検証、PR作成、MVP全体の成功判定は後続です。

SF-08以降を実装中です。許可済みHTTPS Repositoryの別プロセス/コンテナ取得、Leaseに結び付いたArtifactの予約・アップロード・確定・取得・回収APIを追加しました。Assemblerは実コンテナでのpatch適用と検査まで追加し、Candidate永続化・工程への接続を進めています。タスク全体の受入判定は実装計画に従います。

## ローカル起動

Python 3.11以上、[uv](https://docs.astral.sh/uv/)、Docker Composeを使用します。コマンドはリポジトリ直下で実行してください。

```sh
uv sync --locked
docker compose up -d --wait db
export FACTORY_DATABASE_URL='postgresql+psycopg://factory:factory@127.0.0.1:5432/factory'
uv run alembic upgrade head
uv run factory bootstrap --name local
uv run factory serve
```

BootstrapはWorkspaceとUser資格情報を新規発行し、JSONで一度だけ返します。再実行すると別Workspaceを作ります。資格情報をリポジトリに保存しないでください。APIは `127.0.0.1:8000` で起動し、[APIドキュメント](http://127.0.0.1:8000/docs) と `/openapi.json` から実装済みの契約を確認できます。

別ターミナルからWorkspaceを指定してWorker登録用の一回限りTokenを発行します。有効期限は標準600秒です。

```sh
export FACTORY_DATABASE_URL='postgresql+psycopg://factory:factory@127.0.0.1:5432/factory'
uv run factory issue-enrollment --workspace-id '<bootstrapのworkspace_id>'
```

APIドキュメントの `POST /v1/worker/enroll` に `{"token":"<enrollment_token>","name":"local-worker"}` を送ります。返されたWorker資格情報をWorker専用ターミナルで設定します。

```sh
unset FACTORY_DATABASE_URL
export FACTORY_WORKER_TOKEN='<enrollで発行されたworker_token>'
uv run factory-worker
```

Workerの接続先は `FACTORY_API_URL` または `--api-url` で指定できます。HTTPを許可するのはループバックだけです。`--once` は1 Job、`--drain` は取得可能なJobがなくなるまで実行します。`--scenario infra-fail` と `--scenario inconclusive` で制御分岐を再現できます。

## デモTaskの作成

User資格情報を設定し、別ターミナルでデモを登録します。APIだけを使い、実Repositoryへの接続やコード変更は行いません。

```sh
export FACTORY_USER_TOKEN='<bootstrapのuser_token>'
uv run python scripts/create_demo.py --workspace-id '<bootstrapのworkspace_id>'
```

実行中Workerがあれば、Runは `BLOCKED / ASSEMBLE_CANDIDATE / CANDIDATE_PIPELINE_NOT_IMPLEMENTED` に到達します。出力されるRun URLはAPIリソースです。User認証付きのGETでスナップショット、停止理由、費用を確認できます。`/graph` は試行とLease状態、`/event-history?after_revision=N` は保存した履歴を返します。SSE配信は後続です。

## 検証

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q tests/test_contracts.py tests/test_worker.py
```

上記はDB不要の単体試験です。全試験には実PostgreSQL 17を指定します。SF-01〜07の受け入れ試験はDB未設定・別のメジャー版で失敗し、skipによる合格を認めません。テストはランダムな専用schemaを作成し、終了時にそのschemaだけを削除します。既存アプリケーションのschemaには書き込みません。

追加したSource/Assemblerの試験には実Dockerも必要です。最初にRunnerを構築し、実image digestを指定します。試験は専用の一時コンテナとネットワークを作成して終了時に削除します。

```sh
docker build -t software-factory-runner:dev runner
export FACTORY_RUNNER_IMAGE_DIGEST="$(docker image inspect software-factory-runner:dev --format '{{.Id}}')"
```

```sh
export TEST_DATABASE_URL='postgresql+psycopg://factory:factory@127.0.0.1:5432/factory'
mkdir -p .local/acceptance/current
uv run pytest -q --junitxml=.local/acceptance/current/junit.xml
uv run python scripts/verify_acceptance.py .local/acceptance/current --allow-ci-pending
```

Dockerを起動しないローカル検証では、インストール済みPostgreSQL 17のbinを指定できます。一時DBは子コマンド終了時に停止・削除します。通常ユーザーで共有メモリとローカルsocketを利用できるmacOS/Linuxを対象とします。

```sh
FACTORY_POSTGRES_BIN='/path/to/postgresql17/bin' \
  uv run python scripts/with_postgres.py pytest -q --junitxml=.local/acceptance/current/junit.xml
```

pgserver 0.1.4付属のPostgreSQL 16は既存回帰の補助用途に限ります。受け入れ試験には使用できません。

テスト対象は、計画の権限と条件検査、Run固定、20回ずつの同時Claimと予約、Leaseの期限直前/一致/直後、古いfence、完了再送、キャンセル、試行上限、再起動時の復元、Workspace越境、ロール分離、トランザクションのロールバックです。実HTTPの別プロセス、Worker強制終了、20秒のLong Poll中のDBロック、Workerイベントのsequence重複排除も確認します。

`.local/acceptance/current/` に対象コード・試験・設定のdigest、ACごとの期待/実測結果、環境、JUnitを保存します。独立した証拠検査が入力一致と実行結果を照合します。`--allow-ci-pending` で保留を許すのは実GitHub CIのSF-01-AC06だけで、他の未検証や失敗は非0終了します。

CIは同じ試験をPostgreSQL 17とDockerで実行し、`factory-acceptance` Artifactを保存します。完了後に実CIとダウンロード済みArtifactを照合します。

```sh
uv run python scripts/collect_ci_evidence.py \
  --repository iwata-n/software-factory --run-id '<GitHub Actionsのrun ID>' \
  --output .local/acceptance/github-run
```

この収集には対象Repositoryを読める `gh` 認証が必要です。確認前にSF-01-AC06を合格へ変更しません。

## 実装の配置

| パス | 責務 |
|---|---|
| `src/software_factory/contracts.py` | TaskSpec、Policy、Plan、Worker APIの入力契約 |
| `src/software_factory/domain.py` | 外部I/Oに依存しない状態遷移 |
| `src/software_factory/models.py`、`migrations/` | Workspace複合FKと一意制約を持つ永続モデル |
| `src/software_factory/service.py` | Compiler、割当、Lease、冪等性、予約、復旧のトランザクション |
| `src/software_factory/api.py` | Public/Worker API、復旧ループ |
| `src/software_factory/adapters.py`、`worker.py` | Agent契約、Fake Agent、HTTP専用Supervisor |
| `src/software_factory/cli.py` | ローカル管理と起動 |

## 管理上の境界

初期認証はWorkspace専用のローカルUser/Worker資格情報です。Enrollment Tokenと認証TokenはSHA-256のハッシュだけをDBに保存します。外部ユーザー向けSSO、User membership、認証ローテーション、Artifactの保持、バックアップ運用はまだ含みません。PostgreSQL資格情報はControl Planeとローカル管理CLIだけに渡します。

```sh
uv run factory revoke --principal-id '<userまたはworkerのID>'
uv run factory revoke-repository --repository-id '<repositoryのID>'
uv run factory reconcile
```

失効したTokenは新規要求を拒否します。実行中Workerの物理停止は断定せず、Lease失効または停止報告を記録します。失効Attemptの未確定消費は予約額を消費済みとして記録します。費用は整数のmicro USDによる模擬集計で、Providerの実課金を抑える保証ではありません。

基盤内ではWorkspace行を短時間ロックし、同一Workspaceの変更操作を直列化します。Long Pollingの待機はDBトランザクションの外で行います。Outboxは保存のみで、JiraやSlackへの配送はSF-22の対象です。

Repositoryの取消は既存Runのsnapshotより優先し、新規Claim・公開認可を停止します。ResolverはRun Compilerの外側に置き、解決待ちにDBトランザクションを維持しません。remote_url未指定の既存Repositoryは管理者登録済みSHAを使います。remote_urlがある場合はHTTP Source Runnerを必須とし、未設定時に登録済みSHAへ黙って退避しません。Planのsymlink検査には信頼されたWorkerの読取専用調査結果を使い、Agentの自己申告から許可範囲を増やしません。

## HTTPS Source Runner

管理者が `FACTORY_ALLOWED_REMOTES` にHTTPS URLを完全一致で許可します。Source Runnerは専用資格情報を持つ別プロセスとし、Control Plane/User/Publisherの資格情報は渡しません。次の環境設定例の専用資格情報は、十分にランダムな値を安全なローカル設定から渡してください。

```sh
export FACTORY_ALLOWED_REMOTES='https://github.com/iwata-n/software-factory.git'
export FACTORY_SOURCE_RUNNER_TOKEN='<Source Runner専用資格情報>'
export FACTORY_RUNNER_IMAGE_DIGEST='<上記で確認したsha256 digest>'
unset FACTORY_DATABASE_URL FACTORY_USER_TOKEN FACTORY_WORKER_TOKEN GH_TOKEN GITHUB_TOKEN
uv run factory-source-runner
```

Control Plane側にも同じ許可URLとSource Runner専用資格情報を設定し、`FACTORY_SOURCE_RUNNER_URL=http://127.0.0.1:8010` を指定します。Repository登録の `remote_url` に許可URLを設定すると、Run開始時のSHA解決とWorkerの `GET /v1/worker/leases/{lease_id}/source?fence=...` がRunnerへ接続します。取得前後にLease・取消・現在権限を確認します。現段階のHTTPS取得は公開Repositoryを対象とし、非公開Repository用の読み取り専用認証接続は後続の受け入れで確認します。

Gitはroot非書込・non-root・capabilityなし・資源上限付きのコンテナで実行し、global Git設定、credential helper、hook、LFS展開、submodule、HTTP redirectを許可しません。Assemblerは通信なしの別コンテナで実行します。[GitのHTTP設定](https://git-scm.com/docs/git-config) と [Dockerの実行オプション](https://docs.docker.com/reference/cli/docker/container/run/) を基準にし、試験では設定と実挙動を照合します。

## Artifact API

Workerは有効Leaseで `POST /v1/worker/leases/{lease_id}/artifacts` にkind・size_bytes・sha256・fenceを送り予約し、返されたIDの `/content?fence=...` にPUT、`/finalize` にPOSTします。変更操作にはIdempotency-Keyが必要です。最大16 MiB、予約期限1時間。確定済みIDだけをcompleteのartifact_idsに指定でき、任意URIや他AttemptのIDは採用しません。

Userは認証付きの `/v1/runs/{run_id}/artifacts`、`/v1/artifacts/{artifact_id}` と `/content` から取得します。確定前のbytesは取得できません。LOGは原文のサイズ/digestを検証後、秘密をマスクしたbytesだけを保存し、配信用stored_size_bytes/stored_sha256を別に返します。未確定・期限切れのbytesはReconcilerが回収します。

実装時に参照した公式資料: [FastAPIのテスト](https://fastapi.tiangolo.com/tutorial/testing/)、[SQLAlchemyの複合外部キーと制約](https://docs.sqlalchemy.org/en/20/core/constraints.html)、[PostgreSQLの行ロック](https://www.postgresql.org/docs/current/sql-select.html)。
