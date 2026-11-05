# Software Factory

[設計書 v0.4](docs/Software_Factory_Design_v0.4.md) に基づくソフトウェアファクトリーの初期実装です。[実装計画](docs/Implementation_Plan.md) にタスク、依存関係、完了条件、受け入れ試験の対応を記載しています。

FastAPIのControl Plane、PostgreSQL、Leaseで接続する別プロセスWorker、Source Runner、Artifact、不変Candidateと管理対象Assemblerを実装しています。既定のFake Agentは `PLAN → 計画検証 → IMPLEMENT` を模擬実行し、変更patchを作らないため `BLOCKED / CHANGE_ARTIFACT_MISSING` で停止します。Codex CLIによるPlan・変更patch・修正・独立レビュー、Verifier、決定論的品質Gate、専用PublisherによるDraft PR公開を実装しています。実CI同期とMVP全体の成功判定は後続です。

有効な変更patchを受理したRunは、管理対象Assemblerでbase/tree/digestを照合し、不変CandidateとVERIFY/REVIEWの入力Jobを作成します。Verifierは固定Profileで実行し、ReviewerはAC別証拠を提出します。Local GateのPASSは公開Jobを作成します。Run成功は実公開・CI同期・最終照合を実装してから判定します。タスク全体の受入判定は実装計画に従います。

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

Workerトークンは発行から24時間で失効します。期限前に現在のBearerトークンと新しい `Idempotency-Key` を使って `POST /v1/worker/credentials/rotate` を呼び、応答の `worker_token` に差し替えます。Worker IDと実行枠は引き継がれ、旧トークンも更新後60秒間使えます。応答が失われた場合は、その猶予内に旧トークンと同じキーで再送できます。ローカル管理者が `python -m software_factory.cli revoke --principal-id <worker_id> --reason '<理由>'` を実行すると、両方のトークンが即時失効します。

自動更新するWorkerは、登録時のJSON応答を所有者だけが読めるディレクトリ（0700）のファイル（0600）へ保存し、`python -m software_factory.worker --credential-file <path>` で起動します。資格情報ファイルと `FACTORY_WORKER_TOKEN` はどちらか一方を指定します。資格情報ファイルを使用するWorkerは、残り1時間以内で次のJobを受け取る前に更新します。更新途中で再起動した場合はファイルから復旧し、同じWorker IDを引き継ぎます。


## デモTaskの作成

User資格情報を設定し、別ターミナルでデモを登録します。APIだけを使い、実Repositoryへの接続やコード変更は行いません。

```sh
export FACTORY_USER_TOKEN='<bootstrapのuser_token>'
uv run python scripts/create_demo.py --workspace-id '<bootstrapのworkspace_id>'
```

実行中Workerがあれば、Runは `BLOCKED / ASSEMBLE_CANDIDATE / CHANGE_ARTIFACT_MISSING` に到達します。出力されるRun URLはAPIリソースです。User認証付きのGETでスナップショット、停止理由、費用を確認できます。`/graph` は試行とLease状態、`/event-history?after_revision=N` は保存した履歴を返します。SSE配信は後続です。

## カンバンにアカウントでログインする

既存のDBには先に `uv run alembic upgrade head` を適用してから Control Plane を再起動します。
最初のアカウントは、`/operations` の「トークンで接続」に管理者の User トークンを入力し、
「アカウント管理」で追加します。以後はユーザー名とパスワードでログインできます。
ログイン状態は7日間保持され、ログアウト、アカウントの無効化、パスワードの再設定で失効します。
ユーザー名は同じ Control Plane 内で一意です。大文字と小文字は区別しません。

管理者は同じ Workspace 内で閲覧者、操作者、管理者のアカウントを追加できます。
アカウントを管理するブラウザは HTTPS または同じ端末のループバックから接続してください。
CLI・Workerの認証は従来のトークンを使います。

## Software Factory 自身の改善を試す

`scripts/serve_self_host.py` は一時 PostgreSQL に Software Factory 専用 Workspace を作り、
`iwata-n/software-factory` の指定ブランチをコミット SHA で固定します。変更できるのは
`docs/Operations_Recovery_Runbook.md` です。既存の Codex CLI 認証で PLAN、IMPLEMENT、
REVIEW を実行し、候補とは別に定義したテストで復旧手順書の主要な節が残っていることを確認します。

固定した Linux Codex バイナリと、対応する隔離 Runner の manifest を用意して起動します。

```sh
export FACTORY_CODEX_AUTH_FILE=~/.codex/auth.json
uv run --locked python scripts/with_postgres.py python scripts/serve_self_host.py \
  --base-ref codex/sf24-acceptance-ci \
  --codex-binary /path/to/linux/codex \
  --codex-manifest /path/to/isolated-manifest.json \
  --output .local/self-host-first
```

出力されたローカル URL を開き、出力されたファイルから Operator トークンを入力します。
`--admin-username factory-admin` を付けると、代わりにログイン用アカウントを作成し、
初期パスワードを所有者だけが読めるファイルに保存します。
「新規タスク」で受け入れ条件を登録し、「作成したタスクを実行」を押します。
カンバンの Run 詳細で Check・Gate を確認し、「変更パッチ」から候補を取得できます。
Publisher を起動していないため、Local Gate 通過後は公開待ちになります。
試用DBと接続トークンは起動したセッションの間だけ有効です。

## 実Codex CLIの利用

`--adapter codex_cli` は既存のCodex CLIのChatGPTログインを使い、PLAN／IMPLEMENT／REPAIRを実行します。管理対象pipeline Workerの `--reviewer codex_cli` は同じ候補を別のCodex呼出でレビューします。Codex選択時にFakeへ切り替える処理はありません。

[engine manifest](config/codex_cli.json)でCLIの版・バイナリSHA-256・モデル・推論設定・入出力上限・timeoutを固定します。現在は `gpt-6.1-sol`、CLI `0.166.0-alpha.5.1`。CLI更新後は管理者がmanifestを更新し、既存Runは再利用せず、新しいmanifest hashを持つProject／Runを作成します。任意のCLI引数をTaskやRepositoryから追加できません。

通常のWorkerと実GitHubに公開する評価は `execution_mode=isolated_linux` を必須にします。管理者は固定されたLinux用CodexバイナリのSHA-256とCLI版をmanifestへ設定し、`isolation.image_digest` にローカルRunnerの固定digest、`isolation.allowed_hosts` に必要なモデル接続先の完全修飾ホスト名を指定します。Workerには `FACTORY_CODEX_BINARY`、`FACTORY_CODEX_MANIFEST`、所有者のみが読める既存認証ファイルの `FACTORY_CODEX_AUTH_FILE` を設定します。公開を伴わないローカル評価では、評価ハーネスがループバック上の使い切りWorkerにホスト版manifestを明示的に許可します。

隔離Runnerで内部ツールの呼び出しも記録する場合は、同じCLI配布物の `codex-code-mode-host` を `FACTORY_CODEX_BINARY` と同じディレクトリに置き、manifestにそのSHA-256を `code_mode_host_sha256` として指定します。この設定ではApp Serverを使い、TaskSpecの条件を読み取るツールだけに応答します。利用量台帳には呼び出しIDとツール名を保存し、トークン料金はターン合計から計算します。

Project Policyは `agent_adapter=codex_cli`、`allow_external_model=true`、`agent_configuration.engine_manifest_hash` にmanifestの正規化hash、`pricing_manifest` に[予算用推計設定](config/codex_pricing.json)を指定します。Model Workerは管理者が `--managed --usage-authority --adapter codex_cli --kinds PLAN IMPLEMENT REPAIR` で登録し、管理対象pipeline Workerは同じadapterの `ASSEMBLE_CANDIDATE VERIFY REVIEW` を担当します。

Codexは空の一時作業領域で、shell・apps・plugins・browser・画像・sub-agent・hooksを無効にして非対話実行します。Repositoryは許可されたUTF-8ソースのデータとしてだけ渡し、hostへcheckout・実行しません。CP／Worker／GitHub／APIキーの環境変数をCodexプロセスへ渡しません。認証はこの信頼されたCLIクライアントだけが利用します。モデルが返した変更は許可path・保護path・採用Planと照合してpatch化し、適用・実行・PASSの判断は管理対象の隔離Assembler／Verifier／Gateが担当します。OSによる全Sandbox保証の受け入れはSF-21に残ります。

カンバンの「メニュー → 連携設定 → Codex」で、管理者は「既存のログインを使う」を選択できます。
「ChatGPT で接続する」から新しく認可する場合は、表示されたコードを ChatGPT の認可画面で入力します。
この方法を使うには ChatGPT 側でデバイスコード認証が許可されている必要があります。

`scripts/serve_self_host.py` は、ホスト用 Codex CLI があればこの設定を有効にします。
別の構成では Control Plane に `FACTORY_CODEX_WORKSPACE_ID`、`FACTORY_CODEX_AUTH_ROOT`、
`FACTORY_CODEX_LOGIN_BINARY`（ホスト用CLIの絶対パス）、`FACTORY_CODEX_AUTH_FILE` を渡します。
同じホストの Worker にも同じ `FACTORY_CODEX_AUTH_ROOT` と `FACTORY_CODEX_AUTH_FILE` を渡します。
既存のログイン先は `FACTORY_CODEX_AUTH_FILE` で指定し、Web から新しく認可した場合のキャッシュは
ホスト上の所有者専用ディレクトリ `FACTORY_CODEX_AUTH_ROOT` に保存します。
設定できるのは指定 Workspace の管理者です。実行する Project は既存の Codex 用ポリシーを使います。

Run 詳細の「Codex とチャット」では、タスクの目的、受け入れ条件、停止理由を踏まえて
相談できます。会話は開いている Run に保存します。計画に質問がある場合は「要対応」で
Codex と1問ずつ仕様を確認します。Codex がまとめた目的と受け入れ条件を確認し、
「この仕様を採用」で保存します。実行する場合は「採用した仕様で実行」を選びます。
チャットと仕様確認の使用量は Run の実行費用とは別に記録します。
独自の構成では管理対象 Codex Worker に `--task-chat` を追加してください。


実測トークン数をusage受領APIで精算してから成果物を確定します。記録するUSDはAPI公開単価を参照した **予算用の推計** で、ChatGPT契約の請求額や残利用枠ではありません。usage欠落を架空のゼロ円に変換せず、UNKNOWNで停止します。

実Codexを含む再現用評価は、一時DB・実HTTP Worker・公開Repositoryの固定ソース・Linux Runnerを使います。空状態のgolden testがbaseで失敗することを確認し、Codex生成だけの変更を独立検証してLocal Gateまで進めます。PR／Slack送信はこの評価の対象に含みません。

```sh
sh scripts/try_codex.sh
```

実行ごとに新しい保存先を作り、終了時に評価結果と生成コードのパスを表示します。`evaluation.json`、実モデルのpatch、base／生成後JUnit、Candidate・Gate・費用台帳をローカルに保持します。PostgreSQL 17 の配置が異なる場合は `FACTORY_POSTGRES_BIN` を指定します。CLIが別の場所にある場合は `--codex-binary`、別の管理manifestは `--codex-manifest` で指定します。

同じWi-Fiに接続したスマホから試す場合は、MacのLANアドレスを指定して起動します。

```sh
uv run --locked python scripts/serve_codex_demo.py --host "$(ipconfig getifaddr en0)"
```

ターミナルに表示された試用リンクをスマホで開き、「デモを実行」を押します。実行状況と生成コードが画面に表示されます。このHTTPサーバーは信頼できるLANでの試用向けです。ターミナルで Ctrl+C を押すと終了します。

CLI契約は[公式の非対話実行](https://learn.chatgpt.com/docs/non-interactive-mode)、推計の参照単価は[モデルの公式情報](https://developers.openai.com/api/docs/models/gpt-6.1-sol)に基づきます。

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

基盤内ではWorkspace行を短時間ロックし、同一Workspaceの変更操作を直列化します。Long Pollingの待機はDBトランザクションの外で行います。Run結果はOutboxに保存し、Slack配送は別プロセスで処理します。

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

PATCHはUTF-8 unified diffだけを受け付け、binary patch/archive/NULを422で拒否します。SOURCEはAssemblerが作る確定コードの圧縮Artifactです。

Userは認証付きの `/v1/runs/{run_id}/artifacts`、`/v1/artifacts/{artifact_id}` と `/content` から取得します。確定前のbytesは取得できません。LOGは原文のサイズ/digestを検証後、秘密をマスクしたbytesだけを保存し、配信用stored_size_bytes/stored_sha256を別に返します。未確定・期限切れのbytesはReconcilerが回収します。

## 管理対象AssemblerとCandidate

Project Policyの `execution_image_digest` を実Runnerのdigestに固定し、Source Runner接続と有効patchを用意します。既定Fakeはpatchを作成しません。Assembler専用Workerは、管理CLIで信頼と担当工程を明示して登録します。

```sh
uv run factory issue-enrollment --workspace-id '<workspace_id>' \
  --managed --adapter fake --kinds ASSEMBLE_CANDIDATE
# /v1/worker/enrollで上記Tokenを登録し、専用Worker資格情報を得る
unset FACTORY_DATABASE_URL FACTORY_USER_TOKEN GH_TOKEN GITHUB_TOKEN
export FACTORY_WORKER_TOKEN='<管理対象Worker資格情報>'
export FACTORY_RUNNER_IMAGE_DIGEST='<Project Policyと同じ実digest>'
uv run factory-worker --adapter pipeline
```

信頼するWorkerかどうかは管理者発行のEnrollmentから固定し、登録要求/Agent本文の自己申告では変更できません。通常WorkerはASSEMBLE/VERIFY/REVIEWをClaimできません。各工程への入力ArtifactはJobの固定payloadに列挙し、`/v1/worker/leases/{lease_id}/inputs/{artifact_id}/content?fence=...` から割当対象だけ取得します。

Assemblerはbase commit/tree/digestとpatchの実差分を検査します。保護manifest、CI/認証/課金/migration、検証設定・conftest、path/link脱出を拒否し、再現可能なhead SHAを生成します。Code digestはGit管理情報を除くpath/mode/bytesの正規化hashです。生成後はUser認証付きの `/v1/runs/{run_id}/candidates` と `/v1/candidates/{candidate_id}` で確認できます。Candidateの更新/削除は409で拒否し、DBでも追記後の変更を禁止します。

管理対象Runnerは個別のkernel lockで所有者を示します。Supervisorをkillした場合、次のSupervisorは同じstate領域の所有者不在のコンテナだけを削除します。生存中の所有者のコンテナは削除しません。`FACTORY_RUNNER_STATE_ROOT` の標準値は `.local/runner/owners` です。これはSF-10の再試行回復であり、独立watchdogと全Sandbox停止保証はSF-21の受け入れで検証します。

実装時に参照した公式資料: [FastAPIのテスト](https://fastapi.tiangolo.com/tutorial/testing/)、[SQLAlchemyの複合外部キーと制約](https://docs.sqlalchemy.org/en/20/core/constraints.html)、[PostgreSQLの行ロック](https://www.postgresql.org/docs/current/sql-select.html)。


## 独立Verifierとレビュー証拠

Project Policyの `verification_commands` にチェックID、argv、`expected_test_ids`、timeout、JUnit相対pathを設定し、`protected_manifest` に必須テスト/設定のSHA-256を登録します。Run開始時に全体をProfile/hashとして固定します。argvは管理者が設定するリストで、Agentの結果本文からcommandを採用しません。JUnit pathは `/work` からの相対path、候補コードは `/candidate` です。pytestを使う場合は固定imageに含む9.2.0を使用し、`--rootdir=/candidate` と明示した候補内テストpath、書込可能なJUnit pathを指定します。

管理対象pipeline Workerの `VERIFY` は新しいネットワークなしcontainerで実行します。CandidateはDocker管理volumeへroot所有で配置し、実行時はread-only mountにします。CP/Publisher/Worker資格情報、host bind mount、Docker socketを渡しません。実行前にhead/tree/code digest、file manifest、保護manifestを再計算します。JUnitとLOGを別Artifactに確定し、CPがJUnit bytes・終了code・必須IDからCheckを導出します。timeout/起動失敗はERROR、不正/欠落/0件/全skip/必須テスト欠落はINCONCLUSIVEです。

`REVIEW` は同じ固定候補を読取専用で検査し、AC別判定と位置/証拠付きFindingを保存します。実モデルbackendはSF-20で接続します。backend未設定、AC欠落、具体的な証拠参照のない判定はINCONCLUSIVEとして保存し、合格にはしません。originはWorkerの管理区分とJob種別からCPが付与し、自己申告は拒否します。古い候補・別Profile/head/image等の証拠は出所付き履歴へ保存し、`eligible=false` にします。

User認証付きの `GET /v1/candidates/{id}/checks` と `/findings` で結果を取得できます。Check/FindingはDBでも追記後の変更を禁止します。VERIFY/REVIEWの両方が完了すると、CPがLocal Gateを評価します。


## 決定論的品質Gate

Gateは必須Jobの完了を待ち、対象/出所不一致→INCONCLUSIVE、基盤ERROR→ERROR、確定FAIL/critical・high Finding→FAIL、証拠不足/判断不能→INCONCLUSIVE、全必須条件PASS→PASSの順で判定します。任意チェック未提出や古いAttemptの失敗を必須証拠に混ぜません。採用する証拠ID集合/hash、対象tuple、Policy版を不変のGate行へ保存し、費用・次Job・Outboxと同じTXで確定します。

PASSは `PUBLISH_DRAFT`、FAILは上限内の `REPAIR` へ進み、ERRORは同じ検証Job/inputをインフラ上限内で再試行します。INCONCLUSIVEはBLOCKEDです。Repairは正規化した失敗集合の厳密な減少を確認します。実PublisherはSF-17で接続します。現時点で公開Job作成は公開要求の保存を意味します。

Project Policyの `required_ci_checks` でprovider/app ID/check名を固定できます。最終Gateの契約はLocal PASS・Draft PR・CIの対象を照合し、headそのもの、または固定したmerge SHAとbase/head parentsを確認します。待機deadlineはDBへ一度だけ保存し、再起動/再試行で延長しません。CI基盤ERRORは同じ対象のretry要求をOutboxへ保存し、期限中もWorker slotや費用予約を持ちません。実GitHubの公開・CI取得はSF-17〜19で受け入れます。

`GET /v1/candidates/{id}/verification` は証拠とGate、`GET /v1/gates/{id}` は判定を返します。User/AgentによるGate変更は拒否し、所有Workspace内の変更要求を監査に記録します。過去のPASSを別Candidate/Profile/Policy/証拠集合へ流用できません。


## 修正上限と進展

RepairはRunあたり最大2 Jobです。同じRepairのインフラ障害はAttemptだけを増やし、round、親Candidate、採用証拠、制約digestを保持します。patchは親Candidateの確定Archiveへ適用し、元Runのbase SHAを残したまま新しいhead/世代を作ります。旧Candidateと証拠は変更しません。

失敗集合は必須check/test ID、criterion、安定error code、severity、相対path、symbolから作り、行番号や自由文・Finding行IDは同一性に使いません。新集合が旧集合の真部分集合なら進展、同一/新規失敗追加は `NO_PROGRESS`、比較不能は `REPAIR_COMPARISON_INCONCLUSIVE` で停止します。同じコードの再提出は `NO_CODE_CHANGE`、2回後は `REPAIR_LIMIT_REACHED` です。Agentの進展・制約変更の申告は判定に使いません。

必須検証を変えるpatchは引き続き保護されます。新Specが必要な構造化Findingは `SPEC_CHANGE_REQUIRED` で停止します。Final Gateから始めたRepairは、Local PASS後も進展判定を保留し、新しい対象のFinal証拠で比較します。実CI結果はSF-18/19で取り込みます。


## Plan版と承認

構造・AC網羅に不備のあるPlanは、TaskSpecの `max_replans` の範囲で最大1回だけ別Jobから再生成します。各版のbody/hashと生成Jobを保存し、採用したPlan ID/hashをIMPLEMENT/Assembler/Candidate/Repairへ引き継ぎます。質問待ちや保護pathの問題は自動で再生成しません。Plan本文はDBでも不変です。

Project Policyの `require_plan_approval=true` では、妥当なPlanの後に `WAITING_EXTERNAL / WAIT_PLAN_APPROVAL` で待ちます。実行Job・Lease・予約を持ちません。User認証で `GET /v1/runs/{id}/approvals` と `GET /v1/approvals/{id}` から対象を確認し、`POST /v1/approvals/{id}/decisions` へ `expected_run_revision`、`target_hash`、`decision`（approve/deny）、reasonをIdempotency-Key付きで提出します。

承認はPlan/hash、Spec、base SHA、Profile、Policy、Run revision、現Candidate/証拠（未作成ならnull）と24時間の期限へ固定します。古い対象は409で具体的なstale理由を返し、denyは `PLAN_REJECTED` で停止します。一般のPlan承認で予算・権限・検証・merge/deployを変更できません。

`POST /v1/runs/{id}/replan` は現在のrevisionとreasonで同じSpecの再計画を要求します。目的/必須AC/Repositoryを変える `requested_task` は既存snapshotを更新せず `SPEC_CHANGE_REQUIRED` にします。稼働中なら停止を要求して実行権終了後にBLOCKEDへ移り、変更仕様は新Spec・新Runから実行します。

## 費用台帳と予算変更

費用は整数microUSDで記録し、予約・解放・実費・暫定額・調整を追記専用台帳に残します。`GET /v1/runs/{id}/costs` は台帳から計算したspent/reserved/availableと明細を返します。Workerの `POST /v1/worker/attempts/{id}/usage` は自身のAttemptだけを対象とし、provider delivery IDと累積sequenceで重複・逆順を処理します。実Providerの計測には管理対象Workerの `usage:trusted` 権限が必要です。

Lease失効時は予約額を保守的に計上し、後日の確定usageで調整します。これは失効した成果物の採用権を復活させません。取得不能は `USAGE_UNKNOWN`、上限超過は実額を保持して `BUDGET_EXCEEDED` で停止します。実Agentと価格表の接続はSF-20で検証します。

増額は `POST /v1/runs/{id}/budget-requests` に現revision・現予算・新上限・reasonを提出し、返された対象hashを通常のapprovals/decisionsで承認します。承認は24時間で失効し、元snapshotを保持したまま新予算版を追加します。消費+予約未満の上限、古い対象、稼働中の変更を拒否します。増額で不明usageや品質停止は解消できず、Plan待ちには新予算に固定した再承認が必要です。

## 人間の介入

`POST /v1/runs/{id}/interventions` は `action`、`subject_id`、整数の `expected_run_revision`、`reason` を必須とします。許可actionは `approve_plan`、`approve_budget`、`cancel`、`resolve_blocked` です。承認対象はApproval IDで、`target_hash` と `decision=approve/deny` を追加します。個別decisions APIと同じ認可・対象検証・意思決定・再送台帳を使い、同じキーによる経路間再送も同じ結果を返します。取消はRun IDを対象とし、両経路とも202を返します。

`GET /v1/runs/{id}/interventions` は停止解除用の現対象/hashを返します。`resolve_blocked` はそのhashを固定し、台帳・現Spec/Policy/Profile・稼働Lease・停止原因を再確認します。費用が確定し残額が十分なら同じJobを上限内で再試行します。証拠不足は現Candidateの信頼済み証拠からGateを再計算し、解消を確認できる場合にだけ再開します。CIの金融停止を解除しても元の待機期限を保持します。

一般の介入では、不変候補・無進展・修正/試行上限・保護差分・Spec変更・CI期限切れを解除できません。許可された介入はactor、action、固定対象、前後revision、reason、時刻を状態と同じTXで監査します。

## 専用Publisher（SF-17検証中）

PublisherはUser/Workerと別の認証主体です。ローカル管理者が `factory issue-publisher --workspace-id <id> --repository-id <id> --name <name>` で発行し、許可Repositoryを固定します。発行JSONの `publisher_token` だけを所有者のみ読める0600のファイルへ保存します。GitHub資格情報はPublisherプロセスだけに渡します。

```sh
factory-publisher --publisher-token-file .local/publisher.token \
  --github-use-gh --allowed-remote https://github.com/iwata-n/software-factory.git \
  --control-plane-url http://127.0.0.1:8000 --once
```

`--github-token-file` で同様の0600ファイルを使う方法もあります。Publisherは固定Candidateのarchive/tree/raw commit/保護差分を再照合し、コードのcheckout・build・LLM実行を行いません。書込先は `codex/factory/<run_id>` とDraft PR作成だけです。既知のremote headに固定したpush条件が一致しなければ止まります。

`GET /v1/runs/{id}/publication` で操作台帳と外部PRを確認できます。公開開始前の取消は送信を止め、開始後の外部結果は取消Runとは別に履歴へ残します。Publisherは送信前後を永続化し、同じ認証の別起動とも共有します。開始後に結果を確認できなければ `UNKNOWN` とし、盲目的に再公開しません。UNKNOWNの定期照合はSF-19で実装します。現在は契約・ローカルbare Git試験までで、SF-17の実GitHub受け入れは未完了です。

## Workspaceの操作権限

`python -m software_factory.cli issue-user --workspace-id <id> --access VIEWER` で閲覧専用の資格情報を発行します。`OPERATOR` と `ADMIN` も指定できます。既存Userは移行時にADMINを引き継ぎます。

| 操作 | VIEWER | OPERATOR | ADMIN |
|---|---|---|---|
| Workspace内のRun・証拠・Workerの閲覧 | 可 | 可 | 可 |
| Task・Spec作成、Run開始・取消・承認・予算操作 | | 可 | 可 |
| Project・Repository登録、Worker登録・失効 | | | 可 |

Worker登録用の単回トークンは `POST /v1/workspaces/{id}/worker-enrollments` で発行します。Workerはこれを使い従来どおり `POST /v1/worker/enroll` で登録します。失効は `POST /v1/workspaces/{id}/workers/{worker_id}/revoke` に理由と `Idempotency-Key` を付けて実行します。発行したトークンは発行応答で受け取り、Worker一覧は資格情報を返さない設計です。

### 緊急停止と解除

`GET /v1/workspaces/{id}/execution-control` で現在の状態とrevisionを確認します。管理者が同じパスに `stopped`、`expected_revision`、`reason` を指定し、`Idempotency-Key` 付きでPOSTすると停止・解除できます。停止中は新規Claimと公開開始が保留され、稼働中のWorkerにはHeartbeatで停止を要求します。解除には現在のrevisionが必要です。

## GitHub CIのWebhook受信

`alembic upgrade head` でInboxを追加します。Control Planeの起動時に、環境変数 `FACTORY_GITHUB_WEBHOOKS` にIntegration UUIDをキーとするJSONオブジェクトを渡すと受信を有効化できます。各エントリの項目は `workspace_id`、`repository_id`、`github_repository_id`（GitHubの数値ID）、`github_repository`（owner/repo）、`secret`（32文字以上）です。署名用secretはローカルの秘密設定から渡します。

受信先は `POST /v1/webhooks/github/<integration UUID>` です。GitHubのJSON配送を署名とリポジトリで照合し、同じ配送を1件として保存します。対象は `ping`、`workflow_run`、`check_run`、`check_suite` です。配送を受けると、次の照合ループで GitHub API から状態を取得します。配送がない場合も60秒間隔で照合します。

照合を有効にするには、同じ Integration UUID をキーに、読み取り専用トークンを値とする JSON オブジェクトを環境変数 `FACTORY_GITHUB_CI_READ_TOKENS` に設定します。GitHub の対象リポジトリに Pull requests、Contents、Checks の読み取り権限が必要です。固定された Draft PR と base/head SHA を再確認し、公開時に merge SHA がある場合はその親も確認します。merge SHA がない場合は固定 head 上で対象 PR の関連が確認できるチェックを使います。判定は Profile の GitHub App ID とチェック名に一致するチェックだけを対象とします。


GitHub Actions（App ID `15368`）の必須チェックには、Policy の `required_ci_checks` で `app_id` と `check_name` に加えて `github_workflow_id` と `github_workflow_path` を指定します。トークンには Actions の読み取り権限も必要です。ワークフローのSHA-256を同じPolicyの `protected_manifest` に登録します。Control Planeはbase/head両方のワークフローファイル、現在のPR、`pull_request` イベント、ジョブと最新の実行試行を照合します。

固定するワークフローは `pull_request` を起点とし、検証対象のジョブでPRのheadを明示的にcheckoutしてから `Verify candidate head` ステップを実行します。テストはその作業ツリーで実行してください。検証ステップの成功を確認できない終端チェックは判定不能とし、GitHubが報告したSHAと実際に検証できたSHAを別に記録します。

```yaml
- uses: actions/checkout@v4
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    persist-credentials: false
- name: Verify candidate head
  env:
    EXPECTED_HEAD: ${{ github.event.pull_request.head.sha }}
  run: test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"
```

## Jira取り込みとSlack配送

Jira Cloudの対象Projectに専用の複数行テキスト欄を用意し、TaskSpecのJSONを入力します。Control Planeと取り込みプロセスに同じ設定を渡します。

```sh
export FACTORY_JIRA_MAPPING='{"integration_id":"swf","site":"https://example.atlassian.net","project_key":"SWF","workspace_id":"WORKSPACE_ID","project_id":"FACTORY_PROJECT_ID","principal_id":"OPERATOR_PRINCIPAL_ID","field_id":"customfield_12345"}'
```

`FACTORY_JIRA_RELAY_TOKEN`（32文字以上）は秘密情報として両プロセスに設定します。取り込みプロセスにだけ、課題の読み取り権限を持つ `FACTORY_JIRA_EMAIL` と `FACTORY_JIRA_API_TOKEN` を設定します。

```sh
uv run python -m software_factory.jira_relay SWF-123 --api-url http://127.0.0.1:8000
```

返された `task_id` と `spec_version` を使い、認証付きの `POST /v1/tasks/{task_id}/runs` でRunを開始します。同じJira課題の更新は同じTaskの新しいSpecとして取り込まれます。

Slack配送プロセスには対象のFactory Workspace IDを `FACTORY_SLACK_WORKSPACE_ID`、送信先チャンネルIDを `FACTORY_SLACK_CHANNEL_ID`、Bot認証を `FACTORY_SLACK_BOT_TOKEN` として設定します。Control Planeと同じDBを指定し、`uv run python -m software_factory.slack_delivery` を定期実行すると、配送期限に達した通知を処理します。応答が不明な配送を照合するため、Botには対象チャンネルの履歴の読み取りも許可します。

## バックアップと復元

バックアップではPostgreSQLの同一スナップショットをDBダンプとArtifact台帳に使用し、Artifact本体のサイズとSHA-256を照合します。暗号化先はArtifactルートとは別の、所有者専用のディレクトリを指定します。日次運用では、停止時間帯に以下のバックアップを24時間ごとに実行し、結果の時刻・サイズ・失敗を記録します。7世代を保持します。

```sh
uv run python scripts/factory_maintenance.py --offline --artifact-root "$SOURCE_ARTIFACT_ROOT" \
  backup "$BACKUP_DIR" --certificate "$BACKUP_CERTIFICATE"
```

復元する際は、稼働サービスから分離した空DBを `FACTORY_DATABASE_URL` に設定し、存在しない新しいArtifactルートを指定します。秘密鍵は復元担当者だけが参照できる場所から渡します。

```sh
uv run python scripts/factory_maintenance.py --offline --artifact-root "$RESTORE_ARTIFACT_ROOT" \
  restore "$BACKUP_BUNDLE" --certificate "$BACKUP_CERTIFICATE" --key "$BACKUP_PRIVATE_KEY"
```

`STAGED_RECONCILIATION_REQUIRED` を確認してから、復元先のControl Planeを隔離状態で起動します。復元は全Workspaceを停止状態にし、旧Worker・Publisher資格情報と旧Leaseの実行権を失効させます。Workerを同じIDで復旧する場合は、ローカル管理者が復元先を指定して `python -m software_factory.cli reissue-worker --worker-id <id> --reason '<理由>'` を実行します。新しい資格情報で旧Leaseが拒否されることを確認します。復元時に公開中だった操作は `UNKNOWN` に戻り、Publisherによる読み取り照合の対象になります。

Publisherも同じIDとRepository権限を保持して再発行します。復元先を指定し、`python -m software_factory.cli reissue-publisher --publisher-id <id> --reason '<理由>'` の結果を所有者専用ファイルに保存します。復元先では新しい `--state-dir` と再発行したトークンでPublisherを起動し、`GET /v1/publisher/reconciliations` とRunの公開履歴を確認します。未照合の公開処理が残っている間、停止解除は `RESTORE_RECONCILIATION_REQUIRED` で拒否されます。照合で確定できず `BLOCKED` になったRunは、解除後も個別に対応します。
