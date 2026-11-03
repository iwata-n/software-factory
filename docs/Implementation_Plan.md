# ソフトウェアファクトリー 実装計画

設計基準は [設計書 v0.4](Software_Factory_Design_v0.4.md)。最終到達点は、固定したCandidateについて必要な検証とCIを通過し、検証済みDraft PRを引き渡すこととする。マージと本番デプロイは含めない。

2026年11月2日更新。全24タスクを151件の受け入れ条件と共通6条件に具体化し、前提・操作・期待結果・検証方法を定義した。以下の数値と選択肢は個人用MVPの受け入れ基準であり、現行コードの動作保証や外部提供のSLAではない。実行設定や実データの保持期間は、この文書の更新だけでは変更しない。

前段のSF-01〜07再評価では、不足する基盤実装とAC付き試験を追加し、ローカルと実GitHub CIのPostgreSQL 17で各327ケースが成功した。全43 ACと共通6条件の証拠49件を対象SHAへ照合して受け入れた。現在の既定Fake実行は `PLAN → VALIDATE → IMPLEMENT` を模擬し、patchを生成しないため `ASSEMBLE_CANDIDATE` で `BLOCKED / CHANGE_ARTIFACT_MISSING` に停止する。PATCHを提出するWorkerには管理Assemblerを接続する。

SF-01〜17は過去の対象revision `edfc4abdb626d61dcd0c5440c5a7d43a07de76a4` で、1,115ケースと112件の条件別証拠を実CIまで照合して受け入れた。SF-18〜24は統合版に実装を進めており、受け入れの確定は7件残っている。最新候補の状態は末尾の「統合候補の検証状況」を参照する。コード・設定が変わった場合は影響する条件を対象revisionで再評価する。

## マイルストーンと依存関係

| 段階 | 到達点 | 含むタスク | 次段階へ進む条件 |
|---|---|---|---|
| M1 制御基盤 | Fake AgentをHTTPで割り当て、状態を永続化して停止・復旧できる | SF-01〜SF-07 | 基盤テスト、同時Claim、完了再送、Lease失効を検証する |
| M2 Candidateと品質 | Candidateと証拠を固定し、Gateが独立して判定する | SF-08〜SF-12 | 古い候補・欠落証拠・検証改変から合格できない |
| M3 修正と介入 | 制限内の修正・計画再生成・人間の介入を扱う | SF-13〜SF-16 | 同一候補、無進展、予算超過、古い承認で停止する |
| M4 外部公開 | Draft PRとCIを冪等に照合する | SF-17〜SF-19 | 応答消失、古いCI、キャンセル競合で誤公開・誤合格しない |
| M5 実タスク | 実AgentとSandboxを接続し、小タスクを完走する | SF-20〜SF-24 | 必須受け入れ試験と運用手順を満たし、実Draft PRを引き渡す |

M1は全ACと共通条件を満たし受け入れ済み。設計書17章の「成功分岐」はM2〜M4の証拠・公開契約と合わせて完成させる。各マイルストーンは対象タスクの全ACがPASSになったときに受け入れる。Fake AgentだけでMVP受け入れ完了とはしない。

## タスク一覧

各タスクはコード、必要なマイグレーション、API契約、失敗系テスト、起動・運用手順を含む。実装状態と受け入れ状態は別に記録する。SF-01〜17は実CIとArtifact照合まで完了した。対象revisionと証拠は末尾の受入確定に記載する。

| ID | タスクと成果物 | 依存 | 受け入れ条件 | 実装状態 | 受入状態 |
|---|---|---|---|---|---|
| [SF-01](#sf-01-開発環境とマイグレーション) | 開発環境・DB移行・CI・起動手順 | なし | SF-01-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-02](#sf-02-workspaceとtaskspec版の境界) | Workspace・Task・仕様版・認証境界 | SF-01 | SF-02-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-03](#sf-03-run-compilerと固定スナップショット) | Run Compiler・固定snapshot | SF-02 | SF-03-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-04](#sf-04-純粋な遷移とplan検証) | 純粋な遷移・Plan Validator | SF-03 | SF-04-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-05](#sf-05-jobとleaseの永続制御) | Job・Attempt・Lease・Scheduler・Reconciler | SF-04 | SF-05-AC01〜07 | 実装済み | 受け入れ済み |
| [SF-06](#sf-06-http-workerとfake-agent) | HTTP Worker・Agent契約・Fake Agent | SF-05 | SF-06-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-07](#sf-07-キャンセルと永続イベント) | 取消・Graph・イベント・Outbox | SF-05、SF-06 | SF-07-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-08](#sf-08-repository接続とsha解決) | Repository接続・実SHA解決 | SF-03 | SF-08-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-09](#sf-09-artifactのライフサイクル) | Artifact予約・確定・取得・回収 | SF-02、SF-05 | SF-09-AC01〜07 | 実装済み | 受け入れ済み |
| [SF-10](#sf-10-隔離assemblerとcandidate) | 隔離Assembler・不変Candidate | SF-08、SF-09 | SF-10-AC01〜07 | 実装済み | 受け入れ済み |
| [SF-11](#sf-11-独立検証とレビュー証拠) | Verifier・Reviewer・Check・Finding | SF-10 | SF-11-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-12](#sf-12-決定論的な品質gate) | Local Gate・最終Gate | SF-11 | SF-12-AC01〜07 | 実装済み | 受け入れ済み |
| [SF-13](#sf-13-修正上限と進展の判定) | Repair・Candidate世代・早期打切り | SF-12 | SF-13-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-14](#sf-14-plan版と対象を固定する承認) | Plan版・再計画・計画承認 | SF-04、SF-12 | SF-14-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-15](#sf-15-費用台帳と予算変更) | 費用/予約台帳・精算・予算変更 | SF-05、SF-13 | SF-15-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-16](#sf-16-人間の介入コマンド) | 介入と承認の共通処理 | SF-14、SF-15 | SF-16-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-17](#sf-17-trusted-publisherとdraft-pr) | Trusted Publisher・Draft PR・操作台帳 | SF-10、SF-12、SF-16 | SF-17-AC01〜06 | 実装済み | 受け入れ済み |
| [SF-18](#sf-18-ci同期とwebhookの重複排除) | CI・Webhook Inbox・定期照合 | SF-17 | SF-18-AC01〜06 | 実装済み | 未判定 |
| [SF-19](#sf-19-公開結果unknownの復旧とrun成功) | 公開UNKNOWN復旧・Run成功 | SF-18 | SF-19-AC01〜06 | 実装済み | 未判定 |
| [SF-20](#sf-20-実agent-adapterと利用量計測) | 実Agent・利用量計測 | SF-06、SF-15 | SF-20-AC01〜06 | ホスト上で実Codex接続済み | 未判定 |
| [SF-21](#sf-21-sandboxとプロセス停止) | Sandbox・資源/通信制限・停止 | SF-09、SF-20 | SF-21-AC01〜07 | 管理対象Runnerを実装済み、実Agent隔離は作業中 | 未判定 |
| [SF-22](#sf-22-jira入力とslack通知) | Jira入力・Slack通知・Outbox配送 | SF-07、SF-19 | SF-22-AC01〜06 | 取り込み・配送を実装済み | 未判定 |
| [SF-23](#sf-23-運用表示と認証更新と監査) | 運用表示・SSE・認証更新・監査 | SF-16、SF-19、SF-21 | SF-23-AC01〜06 | 運用画面・認証・監査を実装済み | 未判定 |
| [SF-24](#sf-24-保持と復元とmvp総合受け入れ) | 保持・復元・全AT・実タスク評価 | SF-19〜SF-23 | SF-24-AC01〜08 | 保持・復元・評価基盤を実装済み | 未判定 |

SF-15は利用・予約・暫定精算・調整の追記台帳、遅延usageの照合、対象付き予算承認まで受け入れた。実Provider接続はSF-20で検証する。

## 受け入れ判定の共通規則

ACのIDは `SF-番号-AC連番` とし、各行の期待結果をすべて満たせばPASS、1つでも反すればFAILとする。未実行、試験環境不足、秘密情報・接続先不足は未検証として残す。skip、xfail、ログだけの目視推測をPASSに数えない。全ACが同一の対象revisionでPASSになったタスクだけを「受け入れ済み」にする。

検証欄のUTは外部I/Oを使わない単体試験、ITは実PostgreSQL・Storage・OSを使う統合試験、CTはAPI/Adapter契約試験、E2Eは独立プロセスまたは実サービスを接続する試験を表す。Mock/Fakeの結果は契約・故障分岐の証拠とし、実GitHub・実Agent・OS隔離の成功の代用にはしない。

SF-01〜09の受け入れ試験は `tests/acceptance/test_sf01.py`〜`test_sf09.py` に実装した。pytestの `ac` markerで条件IDを対応付け、1つのACに複数ケースがある場合は全ケースを実行する。SF-10は `tests/acceptance/test_sf10*.py` と `tests/test_assembler.py` に実Assembler・API・DB・別PID故障復旧の試験を実装し、実CIまで照合済み。以後の試験追加先は計画段階であり、実行済みとして扱わない。

### 共通の検証と証拠

| ID | 全タスクに適用する合格条件 | 検証方法と必要な証拠 |
|---|---|---|
| C-01 | Taskの全AC、関係する既存回帰試験、lint・formatが成功する。DB変更時は空DBへのupgrade、モデル差分0、前版からのupgrade後のデータ一致を満たす | 実PostgreSQL 17でpytestのJUnit XML、Ruff結果、Alembic履歴と差分を保存。受入試験はDB未指定や必要設定欠落なら開始前に非0終了し、skipで成功させない |
| C-02 | 非公開データに未認証401、User/Workerのロール違い403、別Workspaceおよび別WorkerのLeaseに404。拒否時に業務データ・予約・外部送信を増やさない | 全追加APIをA/Bの2Workspace、User/Worker/失効TokenでCT。DB差分と送信回数を照合 |
| C-03 | 作成・版追加・Run開始・介入・取消・完了・失敗報告は同一主体/操作/キー/内容で同じHTTP status・body・Locationを返し、副作用は1回。同キー別内容409。キー欠落422 | 逐次再送と2同時要求をIT。Claim/Heartbeatは現在のLeaseを照合し、Enrollmentは一回限り、イベント・Webhookは固有配送IDで冪等化 |
| C-04 | 状態、次Job、処理済みイベント、Outbox、費用更新を同一TXで保存する。commit前障害は全てロールバックし、commit後の応答消失は同じ結果を返す | 各書込境界で例外・応答消失を注入。再起動後の行数・revision・残額と外部送信台帳を照合 |
| C-05 | 証拠は対象revision、AC ID、試験ID、入力fixtureのdigest、設定版、実行日時、環境版/digest、期待値・実測値・結果を持つ。Run試験はWorkspace/Run/Candidate/Attempt IDも記録 | `.local/acceptance/<revision>/<task-id>/` に結果JSON、JUnit、マスク済みログ、必要なDB/Artifact/API記録を保存。CIでは同じ証拠をArtifactとして保存しURLを台帳に記録 |
| C-06 | Token・鍵・認証ヘッダ・秘密を含むURLを平文でDB業務ログ、イベント、Artifact、通知、画面に残さない。期限や権限の失効後は新しい処理権を与えない | テスト専用の秘密文字列を入力し、保存先と送信内容を走査して出現0。失効と処理の競合をITで確認。認証発行応答の一度限りの秘密返却は除く |

`revision` は対象コードのcommit SHAを使う。未commitの検証では、基準SHAと未追跡ファイルを含む差分・入力ファイル集合のSHA-256を記録する。未commitの結果を、異なるcommitの合格証拠へ付け替えない。

### MVPの固定試験設定

以下は設計書の仮値を受け入れ試験用に具体化したもの。変更する場合は先に本計画とPolicy版を更新し、影響するACを再検証する。

| 設定 | 受け入れ試験の値と境界 |
|---|---|
| 実行制御 | Worker 1スロット、Run内1 Job、Unit 1つ。Lease 90秒、Heartbeat 15秒、Reconciler周期5秒、Long Poll最大20秒。Job上限2700秒、インフラ最大3試行、修正最大2回、再計画最大1回。Policyによる縮小を許す |
| 時計と競合 | CPはDB時刻、Workerの残時間は単調時計。`now >= expires_at` は失効。境界を−1ms/一致/+1msで試験。競合試験はBarrierで2要求を同時解放する試行を20回実行し、重複0。待機・予算・承認中はLeaseを確保しない |
| 復旧と外部待ち | CI照合60秒周期、CI待ち上限1800秒で `BLOCKED / CI_TIMEOUT`。公開結果UNKNOWNは1/2/4秒後の3照合で一意に確定できなければ `BLOCKED / PUBLISH_UNKNOWN`。CI待機期限はDBに保存し再起動でリセットしない |
| Artifact | SHA-256、上限16 MiB。16 MiBちょうどは許可、+1 byteは413。予約有効期限1時間、回収周期60秒。AVAILABLE化はサイズ・digest・所属・有効Lease照合後だけ。Agent提出の任意URIは不許可 |
| 初期Repository | 接続先は管理者が許可したHTTPSのhostとowner/repo完全一致。試験用ローカルbare repoはテスト設定でのみ許可。file/ssh/git/ext方式、資格情報入りURL、許可外redirect、未許可submodule/LFSは拒否。Git操作はWorker側の隔離Runnerで実施 |
| 保護対象 | 検証Profileで必須テスト・設定のpathとdigestを列挙。CI、migration、auth、billingのpathをPolicyに列挙する。通常ラインでは内容/削除/rename/mode変更をBLOCKEDにする。必須チェック追加は許可し、削除・skip化・対象縮小は許可しない |
| 検証Profile | commandのargv、チェックID、必須テストID/件数、ACとの対応、timeout、実行image digest、信頼するevidence_originとCI provider/app/check識別子を版固定。shell自由文をAgentから受け取らない |
| 費用 | 整数micro USD。fixtureは上限1,000,000、Job予約100,000。開始可能額＝上限−確定/暫定消費−実行中予約。料金はDecimalで呼出単位に計算し、1 micro USD未満を切上げてから合算する。実Providerの料金表・通貨・計測方式は実Agentの設定manifestに固定し、架空のゼロ円に変換しない |
| 隔離と停止 | Linuxの使い捨てcontainerを個人用MVPの対象とする。non-root、read-only root、2 CPU、2 GiB、128 PID、作業領域256 MiB、権限追加なし。Verifier/Assemblerの通信は拒否、Agentは許可したModel経路だけ。実プロセスは確認済みLeaseとJob期限の早い方の10秒前に停止を開始し、TERM猶予5秒、KILL後2秒以内に全子プロセス停止。残時間10秒以下では起動しない |
| 保持と復元 | 未確定Artifactは予約失効後に回収。確定Artifact・ログ・証拠はRun終了後30日、Task/Run/監査/費用は90日保持。冪等性台帳は再送先の親Resourceの存続中と削除後90日保持し、その間は認証後に保存応答を返す。台帳廃棄後も削除済み対象を再生成せず404（認証主体失効時は401）とする。稼働中・承認待ち・UNKNOWN照合中の参照データは削除しない。24時間ごとのDB/Artifact整合バックアップを7世代保持。MVP試験目標はRPO 24時間以内、RTO 60分以内 |
| 更新と失効 | Enrollment 600秒・一回限り。Worker資格情報は24時間有効、更新後の旧資格情報は60秒で失効。管理者による明示失効は猶予なしで次要求から拒否する |

SF-10はAssembler用の最小隔離Runnerを含み、SF-11/20はそれを再利用する。SF-21でAgent、リソース制限、通信、強制停止を一括検証する。SF-12の最終Gateは合成CI証拠で先に試験し、実CI接続はSF-18/19で確認する。この分担により後続タスクとの循環依存を作らない。

## タスク別の受け入れ条件

各ACに共通規則C-01〜C-06も適用する。期待するHTTP codeや停止理由は実装すべき契約であり、現行実装との差分がある場合は実装を直してから受け入れる。

### SF-01 開発環境とマイグレーション

依存なし。試験追加先は `tests/acceptance/test_sf01.py`。既存試験候補は `tests/test_control_plane.py::test_migration_matches_models` と `tests/test_processes.py`。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-01-AC01 | 空のvenvにPython 3.11で `uv sync --locked` を実行 | 終了0、lock変更0、パッケージとCLIをimport/起動できる | IT：インストールログ、Python版、依存一覧、lockの前後digestを保存 |
| SF-01-AC02 | 空のPostgreSQL 17に `alembic upgrade head` と `alembic check` を実行 | headに一致しモデル差分0。別のDB/既存schemaに書込0 | IT：schema一覧、revision、check出力を照合 |
| SF-01-AC03 | 直前のschema版に代表データを保存してupgrade。初版は空schemaを直前版とする | Task/Run/所属/金額/仕様hashが移行前と一致し、upgrade再実行で増減0 | IT：移行前後の正規化dumpを比較。データ破壊時はFAIL |
| SF-01-AC04 | 移行済みDBでCP起動、health/OpenAPI取得。続いてDBを切断 | 正常時health 200とAPI契約を返し、DB切断時は200を返さない。SQLiteへ自動退避しない | CT/E2E：HTTP status、起動/停止ログ、DB種別を保存 |
| SF-01-AC05 | 対象revisionにlint・format・全既存試験・本タスクACを実行 | 全て終了0、必要試験のskip/xfail 0 | IT：Ruff結果、JUnit、収集試験一覧。警告は種類と対応方針を併記 |
| SF-01-AC06 | 同じrevisionをGitHub CIで実行し、READMEの起動手順を新規環境で再現 | PostgreSQL 17のCI成功と証拠Artifactを取得でき、手順外のコード修正なしで起動する | E2E：実CI run URL、保存証拠、手順実行記録。workflowファイル存在だけでは未検証 |

### SF-02 WorkspaceとTaskSpec版の境界

依存SF-01。試験追加先は `tests/acceptance/test_sf02.py`。既存試験候補は `tests/test_control_plane.py` の越境・ロール・Enrollment・DB制約試験。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-02-AC01 | A所属UserがProject、Repository、Taskを順に作成しGET | 各作成201、全親子のworkspace_idとproject_idが一致し、Taskの初期仕様版は1 | CT/IT：要求応答と親子JOIN結果を比較 |
| SF-02-AC02 | 仕様v1のTaskにv2を追加し、並行して別キーで版追加 | v1のbody/hashは不変。版番号はTask内で連続・一意。再送は新しい版を作らない | IT：2並行要求と全仕様版のbody/hashを照合 |
| SF-02-AC03 | BのID、同じWorkspaceの別ProjectのRepositoryを親として指定。DBへ直接不正FKも挿入 | APIは404、DBは整合性違反。中途半端なTask/仕様版が残らない | CT/IT：A/BとProject X/Yの組合せ表、rollback後の行数 |
| SF-02-AC04 | 未認証・失効Token・Worker TokenでPublic API、User TokenでWorker APIを呼ぶ | 401/403を共通規則どおり返す。主張したworkspace_idで認可先を切り替えない | CT：全対象エンドポイント×認証主体の表を保存 |
| SF-02-AC05 | 同じEnrollmentを2並行で使用。期限直前/一致/直後にも登録 | 有効Tokenの登録成功は1回だけ201。再利用・期限一致以降は401。Worker能力/所属はサーバー指定値 | IT：Worker/Principal行数、Token hash、境界時刻の結果。秘密返却は成功時だけ |
| SF-02-AC06 | 不明フィールド、重複AC ID、Unit数2、merge許可要求、空目的をTaskSpecに入れる。A/Bを同じ接続poolで交互参照 | 全不正入力422で保存0。接続再利用後もA/Bのデータ混入0 | CT/IT：入力一覧とDB差分、pool再利用100回の所属照合 |

### SF-03 Run Compilerと固定スナップショット

依存SF-02。試験追加先は `tests/acceptance/test_sf03.py`。既存試験候補は `test_run_snapshot_and_spec_version_are_fixed`、`test_effective_limits_are_capped_by_server_policy`。remote解決はSF-08が担当し、ここでは信頼されたresolverの固定出力を使用する。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-03-AC01 | Spec v1とresolverのSHA AでRun開始 | 202とLocationを返し、Task/Spec/hash、Repository/base_sha、workflow版、Agent設定、image digest、検証Profile/hash、Policy版/hash、実効上限を保存 | CT/IT：入力とsnapshot全項目の一致、hash再計算、最初のPLAN Job数1を確認 |
| SF-03-AC02 | 開始後にSpec v2、Project設定、参照先SHA Bを登録 | 既存Runのsnapshotとbase_shaは変化0。新Runだけ新設定を使う | IT：変更前後のsnapshotをbyte単位で比較 |
| SF-03-AC03 | 同じ開始要求を逐次/同時再送し、同キー別spec_versionでも送信 | 同内容は同Run/同応答、別内容409、PLAN Job/Outboxは初回分だけ | CT/IT：応答とRun/Job/Outbox/冪等性行数を照合 |
| SF-03-AC04 | SpecがPolicy以上の試行・時間を要求。必須チェック削除、別Profile、存在しない版も要求 | 実効試行・時間は両者の小さい値。検証緩和と別Profileは422、存在しない版404でRun作成0 | CT：上限の下/同値/超過ケースとDB差分 |
| SF-03-AC05 | resolverが参照なし・取得失敗・不正SHAを返す | 未固定のRunをQUEUEDにせず、Job作成0。参照なし422、取得基盤障害503を返し、失敗理由を記録 | CT/IT：resolver障害注入とRun/Job行数。未解決base_refでの実行はFAIL |
| SF-03-AC06 | Run開始後にWorker認証またはRepository権限を取り消してClaim/公開認可判定を要求 | 古いsnapshotを根拠に割当・公開許可を出さない。Worker失効401、権限取消RunはBLOCKED、外部送信0 | IT/CT：現在権限とsnapshotを比較。公開先は境界Fakeで送信0を検証、実公開競合はSF-17で再検証 |

### SF-04 純粋な遷移とPlan検証

依存SF-03。試験追加先は `tests/acceptance/test_sf04.py`。既存試験候補は `tests/test_contracts.py` と不正Plan完了試験。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-04-AC01 | 同一Snapshot/Event/Policy/明示時刻を100回decideへ入力し、ネットワーク・時計・DB呼出を禁止 | Decisionが全回一致し、外部I/O回数0 | UT：状態遷移表の全行をparameter化し、I/O呼出時に失敗させる |
| SF-04-AC02 | 単一Unit、全ACを一度ずつ含む、依存なし、許可パスのPlanを提出 | Plan版を採用しIMPLEMENT Jobを1つ作る。Agentの予算/権限/検証変更を採用しない | CT/IT：採用Plan、実効設定、次Job数を比較 |
| SF-04-AC03 | Unit 0/2、重複/欠落/未知AC、自己依存・未知参照・循環、未知工程を提出 | 422または保存済みPlanのvalidation_errorで拒否し、採用Plan/IMPLEMENT Jobを作らない | UT/CT：全不正fixtureを列挙。API入力とAgent完了ペイロードの両経路を確認 |
| SF-04-AC04 | `../`、絶対path、backslash、symlink経由、許可prefixに似た別名、保護path/その親を提案 | 許可パスに展開できない/保護対象を含むPlanはBLOCKED。実行開始0 | UT/IT：path fixture一覧と実ファイル解決の結果を照合 |
| SF-04-AC05 | open_questionsが残るPlan、認証/課金/移行/CI/必須テスト変更のPlanを提出 | 通常実装に進めず、質問または保護pathを停止理由に記録。単なる計画承認で解除できない | CT/IT：BLOCKED理由、未解決項目、次Job数0 |
| SF-04-AC06 | 終端Runへ遅延完了、不正phaseの完了、取消中の正常進行イベントを投入 | 終端状態を巻き戻さず、不正遷移は拒否、取消中に次Jobを作らない。Agent完了の自己申告だけでRun成功にしない | UT/IT：状態/phase/revision/Jobの前後比較 |

### SF-05 JobとLeaseの永続制御

依存SF-04。試験追加先は `tests/acceptance/test_sf05.py`。既存試験候補はLease、同時Claim、インフラ試行上限、ロールバックの各試験。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-05-AC01 | 依存未完了/完了Job、未来available_at、能力不一致、取消Run、異なるpriorityを用意してClaim | 未依存完了はPENDING、完了後READY。認可/能力/時刻/Run状態を満たすJobだけpriority降順・available_at/作成時刻/ID昇順で取得 | IT：選択順、非選択理由、Job/Attempt行を照合 |
| SF-05-AC02 | 1 Workerへ2 Run、2 Workerへ同一Job、同一Run内2 READY Jobを同時Claim | Worker/Runの有効Leaseは各1以下。選択JobのAttempt/予約は1回。空きなしは204 | IT：各競合を20回。Attempt、active slot、予約額をSQL集計 |
| SF-05-AC03 | 有効LeaseをHeartbeat、fence違い・別Worker・失効境界でも報告 | 有効な主体だけ期限を更新。延長先はDB時刻+90秒とJob期限の小さい方。別Worker404、古い世代/期限409 | IT：DB時計の−1ms/一致/+1ms、期限前後の行とHTTP結果 |
| SF-05-AC04 | Workerを強制終了してLease期限を進め、Reconcilerを2回起動 | EXPIRED Attemptを1つ残し、同Jobを上限内でRETRY_WAITにする。次Attemptで番号/fenceが増え、旧報告409 | IT：実プロセス終了と時刻制御の両方。停止確認は「権利失効」で物理停止と混同しない |
| SF-05-AC05 | 同じ入力のインフラ失敗/失効を繰り返し、再試行時刻前後にClaim | 試行1/2後は2秒/4秒待機、3回目失敗でFAILED、4回目を作らない。Repair Job/修正回数を増やさない | IT：Attempt入力digest、available_at、試行数とRun理由を比較 |
| SF-05-AC06 | Long Pollを20秒待機中に別接続から同Workspaceの取消/作成を実行 | 待機中のDB TX/行ロック0。別要求をPoll終了まで待たせない。空なら20秒±1秒で204 | IT：2接続のBarrier、DBロックビュー、応答時間を記録 |
| SF-05-AC07 | ClaimのAttempt作成・予約・イベント保存直後の各点でcommit前障害を注入 | Job/Attempt/slot/予約/監査/Outboxが全て戻り、再要求で1回だけClaim可能 | IT：障害点ごとの前後dumpと復旧後の行数 |

### SF-06 HTTP WorkerとFake Agent

依存SF-05。試験追加先は `tests/acceptance/test_sf06.py`。既存試験候補は `tests/test_worker.py`、`tests/test_processes.py`。実プロセス群の停止保証はSF-21で検証する。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-06-AC01 | DB/User/GitHub資格情報を除いた環境でWorkerを別プロセス起動 | 外向きAPIだけでClaim/Heartbeat/完了でき、DB接続・CPの秘密へのアクセス0 | E2E：環境のキー一覧、通信先記録、CP/Workerの別PIDを確認 |
| SF-06-AC02 | 変更Artifactを作らない既定Fake Agentをsuccess/infra-fail/inconclusiveで実行 | successはPLAN→IMPLEMENT後BLOCKED。対象revisionでCandidate機能未実装ならCANDIDATE_PIPELINE_NOT_IMPLEMENTED、実装後はCHANGE_ARTIFACT_MISSING。infraは同Job再試行、inconclusiveは判断待ち。実コード/PR/課金を作らない | CT/E2E：対象revisionに該当する停止理由、Run/Job履歴、作業tree差分0、外部送信0 |
| SF-06-AC03 | 完了保存後の応答だけを落とし、Workerに再送させる | 同一キー・bodyで再送し、受理済み結果を取得。完了/次Job/精算を重複させない | CT/IT：送信body/keyとDB行数、失われた応答の注入点を保存 |
| SF-06-AC04 | 起動前に取消通知、実行中に通信断/期限切れを発生させる | 起動前はstart 0回。実行中は確認済み期限前にcancelを呼び、失効成果物を新規完了として提出しない | UT/CT：注入単調時計、start/cancel/completeの呼出時刻を比較 |
| SF-06-AC05 | remote HTTP、資格情報入りURL、非対応protocol/Job/Adapter、認証401/403を返す | 非対応ではAgentを開始せず終了非0。HTTPはloopbackのみ。恒久エラーを無期限再試行しない | CT：URL/応答ケース一覧とstart数0、終了code |
| SF-06-AC06 | 429/5xx、Heartbeat応答遅延、CP再起動を発生。Adapterのstart/events/cancel/resultを呼ぶ | 期限を超えてAgentを走らせず、再送は同一入力。resume非対応なら途中再開を主張しない | CT：時刻付きAdapterトレース、Capabilities、再送回数と期限比較 |

### SF-07 キャンセルと永続イベント

依存SF-05/06。試験追加先は `tests/acceptance/test_sf07.py`。既存試験候補は取消・再起動・同時完了の各試験。SSE配送はSF-23で追加する。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-07-AC01 | QUEUED/RETRY_WAITのRunを現在revisionで取消 | 202、未実行JobはCANCELLED、active LeaseがなければRunもCANCELLED。以後Claim 204 | CT/IT：Run/Job/予約の前後差分、取消の再送結果 |
| SF-07-AC02 | 実行中Runを取消しHeartbeat/正常完了/停止報告を競合させる | CANCELLING中はLeaseを延長せずcancel_requested=true。完了しても次Jobを作らず、停止報告後CANCELLED | IT：競合20回、期限不変と次Job数0、停止報告の記録 |
| SF-07-AC03 | 取消後Workerが応答しないままLeaseを失効 | ReconcilerでCANCELLEDにするが、物理停止確認はfalse、停止種別は実行権失効として保存 | IT：明示DB時刻、Attempt停止種別、イベント/画面用データの一致 |
| SF-07-AC04 | 古いrevision/他Workspace/終端Runに取消。受理済み取消を再送 | 古いrevision/終端は409、越境404、既受理同内容は同応答。無効要求による状態変更0 | CT：状態別の要求応答、revisionと監査差分 |
| SF-07-AC05 | 実行中・再試行待ち・BLOCKED・承認/CI待ちの各段階でCPをkillし再起動 | 状態/phase/理由/期限/候補参照/予算/Jobを復元し、重複Job/待機用Leaseを作らない | E2E：同一DBと別CP PIDのbefore/after。未接続の承認/CIは契約Fakeで待機状態を作る。実接続復旧はSF-14/18/19で別検証 |
| SF-07-AC06 | 有効LeaseのWorkerイベントをsequence 1,3,2,2で提出し、同sequence別内容/未知種別も送る | 同Attempt/sequenceの保存1回、別内容409、未知種別422。表示順はサーバーrevisionで一意。Graphと履歴の所属が一致 | CT/IT：受信要求、イベント行、Graph/履歴JSON、再起動後再送を照合 |

### SF-08 Repository接続とSHA解決

依存SF-03。試験追加先は `tests/acceptance/test_sf08.py`。実装前にHTTPSの試験用remoteを設定する。資格情報がない場合、ローカルbare repo試験だけでremote接続を合格にしない。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-08-AC01 | 許可したHTTPS Repositoryのbranch/tag/full SHAを解決し取得 | 実在するcommit SHAとtree digestを返し、Run/base_shaと取得treeが一致 | IT/E2E：fixtureの既知commit、取得結果、実remoteの解決ログを照合 |
| SF-08-AC02 | SHA Aの解決直後にbranchをBへ進めてcheckout/再試行 | 既存RunはAを取得、新RunのみBを使う。A消失時にBへ黙って切り替えない | IT：bare repoでref移動/commit削除を再現し、treeとRun snapshotを比較 |
| SF-08-AC03 | 未許可host/repo、別Workspace repo、file/ssh/git/ext方式、URL内資格情報、許可外redirectを指定 | 越境404、他の禁止設定422または実行時POLICY拒否。禁止先への接続/資格情報転送0 | CT/IT：接続記録可能な2サーバーと不正URL一覧を使用 |
| SF-08-AC04 | `.gitmodules`、LFS参照、悪意あるhook、外部Git設定、path脱出を含むRepositoryを取得 | submodule/LFSを自動展開せずBLOCKED。hook/外部command実行0、隔離領域外への書込0 | IT：canaryファイル、process/通信記録、Runner mount一覧で確認 |
| SF-08-AC05 | 認証拒否、参照不存在、接続timeout、転送中断、read権限取消を発生 | Run作成前は参照不正422/基盤障害503で作成0。確定SHAの取得中は権限不足BLOCKED、基盤障害は同Job最大3試行。write/merge資格情報を要求しない | CT/IT：原因と発生段階別のエラー/試行数、取得中断後の作業領域を確認 |
| SF-08-AC06 | 取得を意図的に遅延し、並行して取消/他Task作成。resolver完了直前にも権限取消 | Git待機中のDB行ロック0。取消/失効後の結果でJobを開始しない。CP内でGitや非信頼コードを実行しない | IT：DBロック、CP/Runnerのprocess tree、送信/割当履歴を保存 |

### SF-09 Artifactのライフサイクル

依存SF-02/05。試験追加先は `tests/acceptance/test_sf09.py`。保存先はサーバー発行IDに対応するWorkspace専用領域とし、初期Storageはローカルディスクを使う。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-09-AC01 | 有効Leaseで予約→アップロード→finalizeし、確定IDをcompleteに渡す | 予約201、確定前はAVAILABLEでなく、全照合成功後だけAVAILABLE。Workspace/Attempt/size/digestを保存 | CT/IT：状態遷移、実ファイルhash、完了要求との所属を比較 |
| SF-09-AC02 | 同じfinalizeを逐次/同時再送し、確定後の上書きも試す | AVAILABLE化は1回、同内容は同応答、確定bytesの変更は409。新内容には新Artifact IDが必要 | IT：Storageのbytes/digest、DB版数、競合結果 |
| SF-09-AC03 | 宣言サイズ違い、digest違い、上限/+1byte、途中中断を提出 | 上限ちょうどのみ受理。超過413、size/digest不一致409でAVAILABLEにしない。途中bytesをダウンロードできない | CT/IT：実測bytesと状態、HTTP response、隔離/削除記録 |
| SF-09-AC04 | 任意URI、`../`/絶対path、symlink保存先、別Workspace/別AttemptのArtifactを指定 | 任意保存先を採用せず、越境404。偽装所属や未確定completeは409でJob状態不変 | CT/IT：canary path、Storage root外の差分0、Attempt/Artifact JOIN |
| SF-09-AC05 | 期限一致以後/古いfence/取消中に新規予約・確定を要求 | 失効Lease409。取消中は新規提出を停止し409。受理済み同内容の再送だけ保存済み結果を返す | CT/IT：期限境界、取消との競合、AVAILABLE数の前後比較 |
| SF-09-AC06 | 1時間を超えた未確定予約と参照中/期限前のArtifactに回収を2回実行 | 失効未確定bytesを次の60秒周期内に削除、期限前/有効参照データは維持。回収再送で誤削除0 | IT：注入時計、ファイル一覧、削除台帳と監査記録 |
| SF-09-AC07 | User/Worker/A/Bから一覧・取得・ログ参照。資格情報を含むログも提出 | 認可対象だけ取得可能。Workerは割当Attemptの範囲だけ。秘密canaryはマスクし、検疫が必要な原文は暗号化して生データを配信しない | CT/IT：認可表、DB/Storage/応答のcanary走査 |

### SF-10 隔離AssemblerとCandidate

依存SF-08/09。試験追加先は `tests/acceptance/test_sf10.py`。初期変更形式はUTF-8 unified diffとし、未対応binary patch/任意archiveは422で拒否する。Agentの変更をCPの作業treeに直接適用しない。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-10-AC01 | base SHA AとAVAILABLE patchを新規Runnerで組み立てる | 期待treeのhead SHAを持つCandidateを1つ生成。base/head、source digest、Spec hash、Profile hash、image digestの5組を固定 | IT：fixtureの期待tree、再計算hash、Candidate行を比較 |
| SF-10-AC02 | 同じ組立完了を再送/同時提出し、確定Candidateを更新/削除しようとする | 同一組立操作のCandidateは1つ。更新・上書きは409、旧Candidateのコード/メタデータ/証拠は不変 | CT/IT：操作キーと行数、旧Candidateの取得結果/digest |
| SF-10-AC03 | 許可内の編集/新規テスト追加と、許可外/保護pathの編集・削除・rename・mode変更を提出 | 通常編集だけ採用。必須検証変更は `BLOCKED / PROTECTED_VERIFICATION_CHANGE`、その他の禁止pathは `BLOCKED / POLICY_DENIED` で検証/公開へ進まない | IT：変更種別ごとのpatch、必須テスト/設定digest、次Job数 |
| SF-10-AC04 | path traversal、symlink/hardlink脱出、hook、`.git`設定変更、改ざんbaseのpatchを提出 | BLOCKED、hook/任意commandの実行0、Runner外の読取/書込0。不正コードを現Candidateにしない | IT：悪意fixture、host canary、process/FS trace、Candidate参照を確認 |
| SF-10-AC05 | 必須テストの削除、skip/ignore設定、件数縮小、認証/課金/CI変更を提案外で混入 | Plan採用済みでも実差分検査で停止。Agentの自己テスト成功や一般の計画承認で通過しない | IT：Profile保護manifestと実差分を照合し、停止理由を記録 |
| SF-10-AC06 | patch取得中/適用後/commit前/報告直後にAssemblerをkillし再試行 | 中途treeをCandidateとして公開しない。同一base/Artifactで再開し、確定Candidateと次Jobを各1つにする | IT：各障害点、作業dir回収、Candidate/Jobの前後行数 |
| SF-10-AC07 | Runnerのmount/env/networkと実image digestを検査 | 作業dir以外のhost領域、CP/Publisher資格情報、Docker socketを持たず、通信0。実digestがsnapshotと異なればERROR | IT：container inspect、canaryアクセス試験、通信記録。image名の一致だけでは不可 |

### SF-11 独立検証とレビュー証拠

依存SF-10。試験追加先は `tests/acceptance/test_sf11.py`。Reviewのblocking severityはcritical/highとし、必須ACのFAILはseverityにかかわらず不合格とする。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-11-AC01 | Implementerが残した作業dir/環境変数/改ざんcommandを使おうとして検証開始 | 新規RunnerにCandidateを取得し、サーバー固定Profileのargv/timeout/imageを使う。実装時の可書領域を共有しない | IT：Runner ID/mount/env、実command、Candidate tree/digestを照合 |
| SF-11-AC02 | 全必須チェックとAC対応レビューを実行し、test exit 0/1も作る | 各CheckにCandidate/head/Spec/Profile/image/origin、開始終了、exit code、実行テストID/件数、証拠IDを保存。EXECUTEDとPASSを区別 | CT/IT：期待失敗を含むfixtureと保存Check/Artifactの照合 |
| SF-11-AC03 | timeout、runner起動失敗、parse不能、テスト0件/全skip、結果欠落を注入 | timeout/起動故障はERROR、parse不能/0件/欠落はINCONCLUSIVE。PASS数へ混入0 | IT：原因別Check状態、ログ/終了code、想定件数との比較 |
| SF-11-AC04 | Reviewerへ満たす/満たさない/判断不能のACと構造化Findingを提出 | AC別PASS/FAIL/INCONCLUSIVEを記録。FindingはID/Candidate/criterion/severity/location/証拠IDが必須。不正参照は拒否 | CT：schema正負例、ファイル位置とArtifactの実在/所属を検証 |
| SF-11-AC05 | 旧Candidate結果、別Profile、別head、未信頼Worker由来のPASSを提出 | 履歴には出所付きで残せるが現在の必須証拠に採用しない。originをbodyの自己申告で昇格できない | IT：A/B Candidateと管理/非管理Workerの比較、採用証拠集合 |
| SF-11-AC06 | Reviewerに曖昧指摘だけ、またはImplementer自己テストだけで検証完了させる | 必須AC/証拠がそろわずINCONCLUSIVE。Reviewerが候補コードやProfileを編集する権限を持たない | CT/IT：roleごとのmount/操作拒否、採用Check、コード差分0 |

### SF-12 決定論的な品質Gate

依存SF-11。試験追加先は `tests/acceptance/test_sf12.py`。最終Gateの合成CIは契約試験用とし、実GitHubの受け入れはSF-18/19へ分離する。

Gateは必須Jobが処理中なら待機を続ける。全必須Jobが終端または期限到達後に、対象/出所不一致→INCONCLUSIVE、基盤ERRORあり→ERROR、確定FAILまたはblocking Findingあり→FAIL、欠落/判断不能あり→INCONCLUSIVE、それ以外で全必須条件PASS→PASSの順に評価する。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-12-AC01 | 同一Candidateの全必須Check/ACがPASS、blocking FindingなしでLocal Gateを評価 | PASSと採用証拠集合/hash/Policy版を保存し、公開工程へ1回だけ進む。追加の任意チェック未提出で妨げない | UT/IT：判定表、入力証拠ID集合と次Job数を比較 |
| SF-12-AC02 | FAIL/ERROR/欠落/対象不一致を単独と組合せで与える | 上記優先順と一致。FAILは修正候補、ERRORは同入力インフラ再試行、INCONCLUSIVEはBLOCKED。どれもPASSにしない | UT：全組合せの期待結果表とDecision比較。後続Job種別も確認 |
| SF-12-AC03 | 必須Job処理中、期限内のCI未完了と期限超過を評価 | 処理中は待機しWorker slot/予約を持たない。CI期限到達は `BLOCKED / CI_TIMEOUT`、欠落証拠の偽PASSを作らない | IT：注入時計、待機状態、slot/予約SQL、再起動後deadline一致 |
| SF-12-AC04 | VERIFY/REVIEWの完了順を入れ替え、同時・重複イベントを送る | 各必須証拠の確定後だけGateを確定し、同一Candidate/stage/証拠集合の判定と次工程を1回だけ作る | IT：全到着順と競合20回のGate/Job/Outbox数 |
| SF-12-AC05 | Gate合格後に現Candidate、必須Profile、Policy、承認対象の証拠を変える | 旧Gate/承認を新しい対象に流用しない。再検証/再承認が必要となり、古い公開要求409 | UT/IT：変更前後の対象tupleと採用Gate/承認を照合 |
| SF-12-AC06 | 最終GateへLocal PASS、Draft PR情報、必須CIを与え、head違い/merge SHA違いも与える | 同じ対象の全条件PASSだけ成功候補。不一致・未完了は成功0。実検証SHAとbase/headの関係を保存 | UT/CT：CI tupleの正負例。実サービス試験はSF-18/19 |
| SF-12-AC07 | Agent/Userが結果本文や介入APIでGate PASS、必須検証削除を要求 | Gate正本を変更できず、拒否と監査を記録。Gate自身によるコード編集0 | CT/IT：公開API全経路の権限表、候補tree/採用Profileの差分0 |

### SF-13 修正上限と進展の判定

依存SF-12。試験追加先は `tests/acceptance/test_sf13.py`。`code_digest` はGit管理情報を除くpath/mode/内容の正規化tree hashとし、commit messageやpatch表現が違っても同じコードを識別する。

失敗集合Bは、必須Check/ACのFAILとcritical/high Findingを、Profile固定の `(check又はcriterion ID, stable error code又はtest ID, severity, relative path, symbol)` で正規化した集合とする。行番号や自由文だけの差は別失敗にしない。修正後の `B_new` が `B_old` の真部分集合なら進展ありとし、同一集合・新規失敗追加・比較不能は打ち切る。比較不能はINCONCLUSIVEとし、Agentの自己申告で進展にしない。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-13-AC01 | Local/Final GateをFAILにし、修正回数0/1/2で評価 | 0/1だけ新Repair Jobを1つ作り、2では `BLOCKED / REPAIR_LIMIT_REACHED`。3回目の修正を作らない | UT/IT：各回数、Job種別、Candidate世代、停止理由を確認 |
| SF-13-AC02 | Repair中にWorker障害を起こし同Jobを再試行 | Attemptだけ増え、Repair roundは不変。入力Candidate/Findings/制約digestが全試行同じ | IT：障害注入とAttempt/Jobの入力比較 |
| SF-13-AC03 | 修正後code_digestが前候補と同じ。patch形式/commit messageだけ変える | `BLOCKED / NO_CODE_CHANGE`。新候補を現候補に昇格せず、次Repair/公開0 | IT：同一treeを作る複数patch、候補参照とJob数 |
| SF-13-AC04 | Bを `{a,b}` から `{a}`、`{a,b}`、`{a,c}`、比較不能へ変える | 順に進展あり、NO_PROGRESS、NO_PROGRESS、INCONCLUSIVEで判断。上限内でも後3ケースは自動修正停止 | UT/IT：正規化fingerprintと集合差分を保存 |
| SF-13-AC05 | 修正で必須テストを無効化/保護path編集、または新Specが必要な指摘を提出 | 通常修正を停止し、新Spec/判断が必要と表示。必須証拠を減らしてBを縮小扱いにしない | IT：保護manifest/AC集合の前後digestと停止理由 |
| SF-13-AC06 | RepairがBを空にし、旧Candidateの遅延結果も届く | 新Candidateを独立再検証し、その証拠でGateへ進む。旧候補/失敗/修正履歴は不変、現候補への流用0 | IT：世代別tree/Check/Findingと修正対応IDを照合 |

### SF-14 Plan版と対象を固定する承認

依存SF-04/12。試験追加先は `tests/acceptance/test_sf14.py`。Plan承認を要求するかはProject Policyで固定し、承認待ちは `WAITING_EXTERNAL / WAIT_PLAN_APPROVAL` とする。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-14-AC01 | 初回Planが構造/網羅不足で不採用となり、max_replans=1で再生成 | 別Plan版2を保存し旧版を保持。再計画は1回まで、次の不採用はBLOCKED。権限不足/質問待ちを無限再生成しない | IT：Plan全版、再計画カウンター、入力Spec hashとJob数 |
| SF-14-AC02 | 承認必須Policyで有効Planを採用する | 対象Plan ID/hash、Spec/base SHA、Policy版、Run revision、有効期限24時間の承認要求を保存。IMPLEMENT/Lease/予約は0 | IT：承認待ち行、Job/予約SQL、再起動後の待機復元 |
| SF-14-AC03 | 同Workspaceの承認権限Userが現在対象へreason付きapprove | 対象一致時だけ承認しIMPLEMENTを1回作成。再送は同結果、denyは `BLOCKED / PLAN_REJECTED` | CT/IT：approve/deny/replayの応答と監査/次Job数 |
| SF-14-AC04 | Plan版/Spec/Policy/Run revisionを変えた後、旧承認を提出。期限一致後にも提出 | 全て409、旧承認で処理再開0。何が古いかを認可済みUserへ表示 | CT/IT：対象tupleの各要素を1つずつ変える境界試験 |
| SF-14-AC05 | 一般Plan承認で必須テスト削除、権限追加、予算増額、merge/deployを許可しようとする | 承認しても実効制約を緩和せず、禁止変更の実装/公開0 | CT/IT：承認前後のPolicy/Profile/予算と実差分検査結果 |
| SF-14-AC06 | 目的/必須AC/Repositoryが本質的に変わる再計画を要求 | 実行中Specを上書きせず `BLOCKED / SPEC_CHANGE_REQUIRED`。新Spec・新Runからのみ変更仕様を実行 | CT/IT：旧Run snapshotと全Spec/Plan版を比較 |

### SF-15 費用台帳と予算変更

依存SF-05/13。試験追加先は `tests/acceptance/test_sf15.py`。既存試験候補は予算不足、usage超過、同時Claim試験。金額の訂正は台帳への調整行で表し、元の記録を上書きしない。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-15-AC01 | 上限1,000,000で100,000を予約し、25,000のusageで完了 | 予約中available=900,000、終了後reserved=0/spent=25,000。予約とusage台帳から同額を再計算できる | IT：台帳JOINとRun集計、整数・通貨・Attempt所属を照合 |
| SF-15-AC02 | 残予算99,999/100,000でClaim。同時に2つの100,000予約を残額100,000へ要求 | 不足時はJob開始0でBUDGET_EXHAUSTED、同額は1つだけ開始。重複予約/マイナスavailableを作らない | IT：境界値と20回競合、予約台帳/slot/残額を比較 |
| SF-15-AC03 | 同じusage deliveryを重複/逆順報告。負数/浮動小数/異通貨/別Attemptも提出 | 同一usageの加算は1回。不正値422、越境404。費用重複や別Runへの加算0 | CT/IT：provider delivery ID付きusage一覧と再計算集計 |
| SF-15-AC04 | Lease失効時の予約100,000に、後日確認済み実費20,000を報告 | 失効時は暫定100,000を計上。確認後は調整−80,000を1回追加しspent=20,000。旧Worker成果物は依然409 | IT：失効→遅延usage→再送の台帳差分と候補採用0 |
| SF-15-AC05 | 実費が上限を超過、または実Agentがusageを取得できない | 超過額を切り捨てず記録しBUDGET_EXCEEDEDで次Job停止。計測不能はUSAGE_UNKNOWNでBLOCKED、ゼロ費用扱いにしない | CT/IT：usage障害注入、spentと次Job数、画面用費用状態 |
| SF-15-AC06 | Userが対象Run/revision/現予算/新上限/reasonを固定して増額承認。古い承認/下限未満も送る | 現対象への正当な承認だけ新予算版を追加。古い対象409、消費+予約未満422。元snapshotと変更履歴を保持 | CT/IT：承認台帳、現予算、再開後予約と監査。Agentによる増額0 |

### SF-16 人間の介入コマンド

依存SF-14/15。試験追加先は `tests/acceptance/test_sf16.py`。actionはapprove_plan、approve_budget、cancel、resolve_blockedに限定する。汎用force_pass、merge、deployは受け付けない。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-16-AC01 | 同じ承認をinterventionsとapprovals/decisionの両経路から実行 | 共通の認可/対象検証/状態遷移を通り、結果と拒否条件が一致。二重実行0 | CT/IT：両経路の要求matrix、内部command/監査/次Job数 |
| SF-16-AC02 | action/subject_id/expected_revision/reasonを欠落。未知action、Worker/別Workspaceからも要求 | 欠落・未知action422、Worker403、越境404。状態/権限/予算変化0 | CT：全フィールド欠落と認証主体ごとの応答/DB差分 |
| SF-16-AC03 | 承認対象のCandidate、Plan、証拠集合hash、Policy版、Run revisionを一つずつ変更 | 対象に関係する要素が不一致なら409。初回Plan承認のCandidate未作成はnullで固定し、後で現れた候補へ転用しない | CT/IT：action別の対象tuple、各変異と採用承認の照合 |
| SF-16-AC04 | approve/denyを同時送信、受理後再送、24時間の期限境界で提出 | 1つだけ意思決定を確定、競合409、受理済み再送は同結果。期限到達後の新規承認409 | IT：20回競合、decision数/revision/Job数、時刻境界 |
| SF-16-AC05 | resolve_blockedで証拠不足・不変候補・修正上限を理由なしに解除。条件を解消した要求も提出 | 一般承認でGate/上限を上書きできない。対象条件の解消を再確認できた場合だけ許可工程から再開 | CT/IT：停止理由別の拒否/再開表、再評価記録、同入力試行と修正の区別 |
| SF-16-AC06 | 承認/拒否/取消/予算変更を行い、処理途中で障害を注入 | actor/action/対象/前後revision/reason/時刻を監査し、状態と同時commit。承認待ちはWorker slot0 | IT：監査とRun差分、rollback、Outbox/slotのSQL確認 |

### SF-17 Trusted PublisherとDraft PR

依存SF-10/12/16。試験追加先は `tests/acceptance/test_sf17.py`。送信開始の順序点は、Run確認と同じ短いTXで公開操作をIN_FLIGHTにする時点とする。その後の取消は「送信済みの可能性あり」として照合する。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-17-AC01 | 現CandidateのLocal PASSと必要承認をそろえて公開 | 専用Publisherが `codex/factory/<run_id>` branchに候補と同じheadをpushし、Draft PRを1つ作成。base repo/branchも固定条件と一致 | CT＋実GitHub E2E：operation key、remote head、PR ID/isDraft/baseを照合 |
| SF-17-AC02 | 同じRun/Candidate/操作を再送・並行送信 | Repository/Run/Candidate/操作種別の台帳keyで一意化し、同じPRを返す。重複branch/PR/通知Outbox 0 | IT：GitHub契約stubの送信回数と実remoteのPR件数を検証 |
| SF-17-AC03 | 修正後Candidateを公開、同時に人間の追加commitをPRへ投入 | 通常修正は同PRを新headへ更新。想定外headを発見した場合BLOCKEDで止まり、人間のcommitを強制上書きしない | CT/E2E：remote旧/新head、PR ID不変、head競合とpush条件 |
| SF-17-AC04 | IN_FLIGHT化の直前/直後で取消・権限失効・Candidate変更を競合させる | 直前に取消が確定すれば外部送信0。開始後は結果を台帳に残して照合し、取消Runを成功へ変えない | IT：各順序をBarrierで再現し、DB時刻/dispatch/送信回数を照合 |
| SF-17-AC05 | Agent/VerifierからPublisher資格情報、merge/deploy、別Repository書込を要求 | 資格情報の取得0、許可外操作拒否、merge/deploy API送信0。PublisherはLLMや非信頼buildを実行しない | CT/IT：env/mount/通信先と操作allowlist、秘密canary走査 |
| SF-17-AC06 | 未検証/旧Candidate、偽Gate、保護差分、base/head不一致を公開しようとする | 409で送信0。差分適用は隔離Runner、source/tree/証拠一致を再確認してから公開する | CT/IT：不正組合せと候補/採用Gate、CPのprocess/FS trace |

### SF-18 CI同期とWebhookの重複排除

依存SF-17。試験追加先は `tests/acceptance/test_sf18.py`。外部CIはProfileのprovider/app/check IDに一致するものだけ信頼する。同じ表示名だけでは一致としない。

先行実装で、GitHub Webhookの署名とリポジトリの照合、配送の永続化、再送と同時配送の重複排除を追加した。Inboxには配送ID、イベント種別、指紋、受信時刻を保存する。GitHub APIの定期照合とCandidateへの証拠の紐付けを追加した。実Draft PR #4では同じhead SHAに同一App・同名の `pull_request` と `push` のCheckが各1件あり、GitHubが返した仮merge SHAのCheckは0件だった。Actions用Profileに固定workflowのID・パスと保護manifestを設定し、対象PRのpull_request実行、最新試行、head確認ステップを照合する。既存の実Draft PR #4でpushを除外できることを確認した。専用workflow ID `405481217` を固定し、同一base `2e914e63c4042b81459209e2310688f0fb45d443` の実Draft PRで、[失敗ジョブ](https://github.com/iwata-n/software-factory/actions/runs/41607028930/job/125819299458)と[成功ジョブ](https://github.com/iwata-n/software-factory/actions/runs/41607033739/job/125819316009)を再現した。製品のGitHub readerが両ジョブの検証SHAをそれぞれのPR headと照合し、fixture Local Gateを使ったFinal Gate判定はFAIL/PASSとなった。証拠は `.local/verification/sf18-real-fixtures/` に保存した。追加の実Draft PR #7では、fixture Agent/Reviewer、実Docker Verifier、実Publisherから、再起動したControl Planeの常駐pollで[実CIジョブのPASS](https://github.com/iwata-n/software-factory/actions/runs/41607219807/job/125819955212)を取得し、Run `12ca7bbd-b4fc-44a1-94d1-079fde2ff28e` を `VERIFIED_DRAFT_PR` として成功確定した。再度再起動しても結果とSlack通知予約1件は同一だった。証拠は `.local/evaluation/live-ci-poll-01/evaluation.json` に保存した。失敗経路では、空状態だけを修正したfixtureに件数検証のCIを必須指定し、実Draft PR #9の[失敗ジョブ](https://github.com/iwata-n/software-factory/actions/runs/41610585746/job/125831173132)を常駐pollが取り込んだ。Local Gate PASS、Final Gate FAIL / CI_FAILED、RunはREPAIR待ちとなり、再起動後も結果と成功通知の未作成を確認した。証拠は `.local/evaluation/live-ci-failure-01/evaluation.json`。Webhookによる前倒しと通常pollは照合イベントのtriggerで区別する。実Webhookの演習では受信パスだけを一時公開し、[成功ジョブ](https://github.com/iwata-n/software-factory/actions/runs/41611021515/job/125832617493)と[失敗ジョブ](https://github.com/iwata-n/software-factory/actions/runs/41611203250/job/125833238734)でGitHubから署名付き配送を受信した。両Runの最終CI照合がWEBHOOKとなり、常駐pollと同じPASS/FAIL判定を確認した。一部の配送は一時公開経路で500となったが、受理した配送をDBに記録した。終了時にWebhookと一時公開ルートを削除した。証拠は `.local/evaluation/live-webhook-{success,failure}-01/`。0022で署名済みcheck_runが旧候補の固定PR/base/headまたは仮merge SHAと必須Appに一致した場合、候補別の観測として保存する。`GET /v1/candidates/{id}/ci-history` で参照できる。観測はGate採用不可とし、現候補の正本は従来どおりGitHub APIから再照合する。署名付き再送、base/SHA/Appの変異、候補AからBへの切替を契約試験で確認した。実Agentからの通し試験は追跡する。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-18-AC01 | 正当/不正な署名、別Integration/RepositoryのWebhookを送信 | 正当な配送だけInboxに保存。不正署名401、署名正当でも対象不一致404、状態変更0 | CT/IT：署名fixture、配送IDとInbox/Run差分、秘密マスク |
| SF-18-AC02 | provider/integration/delivery IDが同じ配送を重複/同時/逆順送信 | 受理1回、同ID別内容409。古い状態通知で最新CI状態を巻き戻さず、Gate/Outbox重複0 | IT：配送順の全組合せ、provider実行ID/更新時刻とDB状態 |
| SF-18-AC03 | Candidate AのCIを、現候補B、別base、別head、別merge SHA、未信頼appへ遅延配送 | Aの履歴へ記録しBの必須証拠にしない。実検証SHAとbase/headの組が一致するCIだけ採用 | UT/IT：対象tupleの各変異、候補別Check/Gate行 |
| SF-18-AC04 | CIがqueued/running/success/failure/timeout/cancelled/skipped/neutralを返す | 前2つは待機、successだけPASS、failureはFAIL、timeoutはERROR、cancelled/skipped/neutralはINCONCLUSIVE。必須CIを省略しない | CT：状態変換表、採用証拠とGate入力を保存 |
| SF-18-AC05 | Webhookを欠落させ、CP再起動/Provider 429/5xxも発生 | 60秒周期の照合で回復。CI待ちはWorker slot0、期限1800秒は再起動で延長しない。到達時CI_TIMEOUTで停止 | IT：Fake時計と実HTTP stub、poll/Inbox/期限/予約の記録 |
| SF-18-AC06 | 試験用GitHubで実CIを成功/失敗させ、Webhook経由とpoll経由で取得 | 実PR headと同じ対象のCI結果・証拠URLを保存。両経路で同じ判定となる | E2E：実PR/CI URLとhead/base/tested SHA、受信/照合ログ。stubだけでは未検証 |

### SF-19 公開結果UNKNOWNの復旧とRun成功

公開UNKNOWNの読み取り照合、期限・履歴の永続化、取消競合を実装した。CIポーラーはFinal Gate合格後にGitHubの現在のPR/headと必須CIを再照合する。その観測を現在のRun revision・候補・Local Gateと照合し、成功snapshot（0015）、Run終端、監査イベント、Slack通知Outboxを同時に確定する。成功時の記録は `GET /v1/runs/{run_id}/result` で参照できる。運用画面は成功時の記録と `GET /v1/runs/{run_id}/result/current-pr` の現在PR観測を別に表示する。実GitHubでのE2Eは引き続き追跡する。

依存SF-18。試験追加先は `tests/acceptance/test_sf19.py`。UNKNOWN照合はbranch、操作識別子、Repository、Run、Candidate/headを使う。単なるPRタイトル一致で復旧しない。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-19-AC01 | ProviderがPRを作成した直後、応答保存前にPublisherをkill | 再起動後UNKNOWN→RECONCILINGとなり、既存の一意なPRへ紐付ける。新規create送信0 | IT＋実GitHub E2E：障害点、operation履歴、PR全件と送信回数 |
| SF-19-AC02 | UNKNOWN照合で0件/複数件/識別不一致/通信不能を返す | 1/2/4秒後の3照合で一意に確定できなければPUBLISH_UNKNOWNでBLOCKED。0件や404を理由に即再作成しない | CT/IT：poll回数/時刻、operation状態、create数0 |
| SF-19-AC03 | 現CandidateのLocal/Final PASS、正しいDraft PR、必須CI、未取消をそろえる | RunはSUCCEEDED、成果種別VERIFIED_DRAFT_PR。候補/証拠/Policy/PR/CI/headを固定した結果と通知Outboxを1回保存 | IT/E2E：成功tupleとremote実状態、終端/Outbox/監査の一致 |
| SF-19-AC04 | 成功直前にCandidate/PR head変更、取消、必須CI欠落・失敗を発生 | 不一致・取消・証拠不足では成功0。送信済みPRの外部状態は別記録し、CANCELLEDを成功へ変更しない | IT：Final Gateと取消の全順序、remote状態とRun履歴を照合 |
| SF-19-AC05 | Run成功後に人間が追加commit、CI再実行、PR close/mergeを実施 | 過去の成功tupleは不変。現在headが違えば「現在コードは未検証」。工場がmerge/deployを自動実行しない | CT/E2E：履歴snapshotと現在PR状態の別フィールド、外部送信記録 |
| SF-19-AC06 | CP/Publisherを外部待ち・UNKNOWN・成功直後に再起動し、同通知を再送 | 成功を重複計上せず、待機状態/照合期限/PR IDを復元。判断不能は理由と人間の必要操作を表示 | E2E：各停止点のbefore/after、成功件数とoperation/notification key |

### SF-20 実Agent Adapterと利用量計測

ユーザーの実Codex接続の指示によりSF-18/19より先に進める。CLI既存ChatGPT認証の構造化応答と実測usageの接続を確認した。PLAN/IMPLEMENT/REPAIRのHTTP Workerと独立REVIEW backend、固定engine manifest、許可差分だけのpatch化、usageの冪等送信を実装中。信頼されたhost CLIはソースをデータとして受け取り、コード実行はLinuxのAssembler/Verifierが担当する。実engine自体のLinux隔離・SF-21の全停止保証・実CI同期・実Agentによる3タスク完走までを受け入れ済みにしない。全ACの期待値は変更しない。

依存SF-06/15。試験追加先は `tests/acceptance/test_sf20.py`。最初の実Agentは単一CLIプロセス方式とし、engine/version、起動argv、モデル設定、イベントschema、料金計算版、許可接続先、image digestを設定manifestに固定する。manifest未記入なら受け入れ不可。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-20-AC01 | 同manifestでstart/events/cancel/resultとCapabilities取得を実行 | Jobごとに実行IDを1つ発行し、構造化結果か分類済み失敗を返す。非対応resume/roleは開始前に拒否 | CT：記録済みCLI入出力fixtureと実engineの両方で契約を照合 |
| SF-20-AC02 | PLAN/IMPLEMENT/REVIEW/REPAIRの各roleに許可入力だけを渡す | Plan/変更Artifact/AC別評価/修正対応IDを各schemaで返す。Agent自己申告をCandidate確定やGate PASSへ直接変換しない | CT/IT：roleごとの入力・出力・権限matrix、採用成果物 |
| SF-20-AC03 | stdout破損、未知必須イベント、異常終了、timeout、起動途中取消を発生 | parse不能はINCONCLUSIVE、基盤故障はERROR、取消は停止結果。空出力を成功にせず、孤立processを残さない | CT/IT：process故障fixture、終了code、状態とイベントを照合 |
| SF-20-AC04 | モデル呼出と内部tool利用を含むusageを取得し、同配送を再送 | 呼出/Attempt単位で量・計測/推計区分・料金版・micro USDを台帳へ1回記録。fixture単価100/200で入力2・出力3なら800 | UT/CT：料金fixtureと利用量明細の再計算。実料金とfixture単価を混同しない |
| SF-20-AC05 | 外部モデル送信禁止Projectと許可Projectを実行し、Repo内に秘密取得指示を置く | 禁止時は外部モデル通信0でPOLICY_DENIED。許可時もCP/GitHub/host秘密をAgentへ渡さず、Repo指示で制限を変えない | IT：送信canary、env/mount/通信先、停止理由を確認 |
| SF-20-AC06 | 固定manifestの実engineを隔離Runnerで小タスクに接続し、CPを一時切断 | 実出力をArtifact化でき、期限内に制御を返す。モデル/engine版・実測利用量・非対応能力を記録 | 実Agent E2E：engine出力、Artifact/digest、料金明細、停止トレース。Fakeのみでは未検証 |

### SF-21 Sandboxとプロセス停止

依存SF-09/20。試験追加先は `tests/acceptance/test_sf21.py`。Linuxの実container runtimeで試験し、設定JSONの確認だけで隔離合格とはしない。外部ユーザー向け隔離は別リリースの対象とする。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-21-AC01 | Agent/Verifierからhost home、SSH鍵、CP/Publisher Token、Docker socket、他process名前空間へアクセス | 全アクセス拒否、host/秘密canary漏出0。non-root/read-only root/権限追加なしを実測確認 | IT：実mount/user/capability/namespace一覧と侵入試験出力 |
| SF-21-AC02 | 別WorkspaceのArtifact/cache/一時dir、symlink脱出先へ読書き | Workspaceを跨ぐ読書き0。共有cacheを介しても秘密/コードを混在させず、拒否を記録 | IT：A/Bの異なるcanaryとpath攻撃fixture、FS差分 |
| SF-21-AC03 | 外部任意host・私有IP・metadata宛通信、許可Model経路を試す | Assembler/Verifierの外向き通信0。Agentは固定許可経路だけ成功し、redirect/DNS切替で迂回できない | IT：制御された通信先とegress記録、許可/拒否matrix |
| SF-21-AC04 | CPU占有、2 GiB超メモリ、128 PID超fork、256 MiB超作業領域書込を試す | cgroup等の実上限を越えてhostを消費せず、制限到達はERROR/分類済み停止、PASSにしない | IT：runtime制限値とpeak使用量/終了code、host側影響を保存 |
| SF-21-AC05 | TERMを無視する親子/孫processで取消・通信断・Lease/Job期限を発生 | 早い方の期限10秒前までに停止開始、TERM5秒後KILL、さらに2秒以内に全子process停止。残時間10秒以下では開始0 | IT：実PID/cgroup一覧と単調時刻trace。停止未確認ならERRORとWorker隔離、確認済みと表示しない |
| SF-21-AC06 | 正常完了/失敗/kill/アップロード失敗後にcleanupを実行 | 停止済みcontainer、作業領域、一時資格情報を60秒以内に破棄。永続証拠だけ保持し、再実行で誤削除0 | IT：全終了経路のcontainer/volume/秘密canary一覧、cleanup監査 |
| SF-21-AC07 | image tagの中身変更、Agentから制約変更、外部ユーザーWorker由来の証拠を提出 | 実image digest不一致は実行前ERROR。制約変更を無視せず拒否。未管理Workerをmanaged証拠に昇格しない | CT/IT：digest不一致fixture、inspect、evidence_originと採用Gate |

管理対象Workerは、確認済みLeaseの期限10秒前に停止処理を開始する。コンテナの停止または実行スレッドの終了を確認できなければ `STOP_UNCONFIRMED` を報告し、以後のClaimを停止する。Control Planeが報告を受理するとWorker資格情報を失効させ、運用表示にも停止未確認を残す。同じRunner状態ディレクトリで再起動する場合、以前のコンテナの停止・回収を確認できない間は起動を拒否する。実DockerとPostgreSQLによる境界試験を追加した。実Codexの隔離実行・通信制限・通信断を含むSF-20/21の受け入れは継続する。

### SF-22 Jira入力とSlack通知

依存SF-07/19。試験追加先は `tests/acceptance/test_sf22.py`。Jira入力はMVPでは専用欄の構造化TaskSpecを使う。実接続は評価専用Project/通知先を設定した環境で検証し、一般の業務データへ試験送信しない。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-22-AC01 | 同じTaskSpecをAPI/CLIとJira専用欄から取り込み | source参照を除くSpec hash・受け入れ条件・実効Policyが一致。Jira未設定でもコア実行可能 | CT/IT：正規化TaskSpecとsnapshot比較、Jira無効設定でコア試験 |
| SF-22-AC02 | Jira配送を重複/逆順で受信、同Issueの仕様を更新 | Integration/Issue/版でTask/Specを冪等化。更新は新Spec、実行中Runは不変。AC欠落なら実行せず要入力とする | CT/IT：配送一覧とTask/Spec/Run行数、hashの前後比較 |
| SF-22-AC03 | 別Jira Project/Workspace IDを入力で主張、接続失効や秘密入り本文を送る | 許可したIntegration mappingだけ採用、越境拒否。秘密をマスク/検疫し、権限不足を自動昇格しない | CT/IT：入力攻撃fixture、mapping、DB/ログ/通知のcanary走査 |
| SF-22-AC04 | Run成功/失敗/BLOCKEDを通知し、Outbox保存後や送信応答直後に配送処理をkill | 同じ配送keyを保持。送信確定時は再送しない。結果不明時は照合し、不能ならUNKNOWNで止め、盲目的に新規通知を送らない | CT/IT：送信記録、配送台帳、Outboxと受信側message ID。exactly-onceの外部保証は仮定しない |
| SF-22-AC05 | 429/5xx/恒久認証エラー、3回の配送失敗を発生 | 一時障害は初回を含む最大3試行、待機2/4秒（Retry-After指定時は長い方）。恒久エラーは即停止。未達/要介入を表示し、Run成功履歴を書き換えない | CT/IT：retry時計、delivery状態、Run終端/通知状態の別記録 |
| SF-22-AC06 | 実JiraからTaskを受信し、実Slack評価先に結果を通知 | 正しいTask/Run/候補/PR・CI参照、費用、停止理由/必要判断を確認できる。認証不要のArtifact URLや秘密を送らない | 実サービスE2E：Issue URL、Run/PR URL、通知message IDとマスク済み内容。Mockだけでは未検証 |

Jira REST取り込みコマンドを追加した。専用欄を含む対象課題の読み取り、ADFからのTaskSpec正規化、再取り込みの冪等性をHTTP契約試験とPostgreSQLで確認する。評価先SWFのTask種別では専用欄の設定を確認できていない。実Jiraでの取り込みには専用欄と読み取り認証、製品からの実Slack配送には配送プロセスへのBot認証設定が必要。SF-22-AC06は未検証。

### SF-23 運用表示と認証更新と監査

依存SF-16/19/21。試験追加先は `tests/acceptance/test_sf23.py`。既存証拠候補は履歴/Graph/資格情報失効試験。Userはローカル資格情報のまま操作権限を分離し、SSO・外部ユーザー課金は含めない。

`GET /v1/runs/{id}/events` は永続Run revisionを `<run-id>:<revision>` のSSE IDとして配信する。Run詳細は切断後に最後に表示したIDを送信し、再送を排除する。別RunのIDは404、保持済み履歴との間に欠落がある場合は410と再取得先を返す。PostgreSQLの試験では別アプリ起動後の再開と削除境界を、Chromeの1440px/375pxではfixture応答による再接続・重複排除・履歴再取得を確認した。実運用での履歴回収はSF-24で追跡する。

Worker一覧は最後の試行のHeartbeatとLease期限を表示する。標準15秒のHeartbeat間隔を2回超えた時点（30秒）で遅延、期限到達で実行権失効、停止報告を受けたときは報告済みと区別する。同一Workerの状態遷移と越境拒否をPostgreSQL、表示切替をChrome幅1440px/375pxのfixtureで確認した。

0016でWorkerトークンに24時間の期限を設定し、更新後の旧トークンに60秒の猶予を設けた。明示失効は現・旧双方に適用される。更新はWorker IDとLeaseを維持し、同一キーの再送で追加発行しない。更新・失効は追記専用の監査に残す。0017でUserをVIEWER・OPERATOR・ADMINに分け、Workspace内のWorker登録と失効をADMIN限定にした。既存UserはADMINに移行する。APIの操作権限と越境拒否をPostgreSQLで検証した。資格情報ファイルを使うWorkerは期限1時間前から新規Claimの前に自動更新する。更新応答を消失し旧トークンの猶予後に再起動しても、同一Worker IDで復旧することをPostgreSQLで確認した。運用画面を含む権限別の通し検証は引き続き追跡する。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-23-AC01 | 成功/失敗/待機/取消/UNKNOWNのRunを運用画面で開く | status/phase/revision、理由、現候補、Check/Gate、PR/CI、消費/予約、試行/修正、必要な判断をDB/APIと同値表示 | UI E2E：状態別fixture、表示値assertion、desktop/モバイル幅の画面証拠 |
| SF-23-AC02 | Workerが未応答、権利失効、停止報告済みとなる | 最終Heartbeat/Lease期限と停止種別を区別表示。通信不能を物理停止確認済みにしない | UI/IT：同じWorkerの3状態、API/DB/画面の比較 |
| SF-23-AC03 | SSEを途中切断しLast-Event-IDで再接続、別Run/期限外cursorも指定 | 保存済みサーバー番号から欠落なく再開。重複表示0、越境404、回収済みcursorは410で再取得手順を返す | CT/UI E2E：番号列の比較、切断/再起動/保持期限境界の試験 |
| SF-23-AC04 | Worker資格情報を24時間内に更新、60秒猶予後と明示失効後に旧Tokenを使う | Worker ID/slotは不変。猶予後旧Token401、明示失効は次要求401。更新・失効が監査に残る | CT/IT：期限境界と更新/Claim/Heartbeatの競合、Principal/credential行 |
| SF-23-AC05 | read-only Userが変更/承認、運用UserがWorker管理、WorkerがUser操作を要求 | 操作権限表に従い403。Workspace管理権限だけが登録/失効を実行でき、ID変更やpool再利用で越境不可 | CT/UI E2E：全API/操作×権限の表、A/B切替、秘密非表示を確認 |
| SF-23-AC06 | 緊急停止・解除・承認・予算変更・Worker失効を行い、監査改変も試す | 緊急停止は新Claim/未開始公開を抑止し、実行中へ停止要求。解除は対象固定の明示操作のみ。監査は追記専用でactor/理由/時刻/前後revisionを保存 | IT/UI E2E：各競合と送信0、監査更新拒否、再起動後の停止継続 |

0018でWorkspaceの緊急停止状態とrevisionを永続化した。ADMINが現在revisionと理由を指定して停止・解除する。停止中は新規Claimと公開開始を拒否し、実行中Runは既存LeaseのHeartbeatで停止要求を受ける。公開準備中はPublisherの入力取得を拒否し、既に外部送信された結果の照合・履歴記録は受け付ける。解除後は待機中のJobが再び割り当て可能になる。運用カンバンからADMINが理由を入力して停止・解除できる。VIEWERは停止状態を閲覧できる。Chrome幅375pxから一時Control Plane/PostgreSQLにつなぎ、停止、VIEWERでの再接続、解除を確認した。幅1440px/375pxのfixture試験では旧revisionからの解除拒否と状態の再取得も確認した。別担当者による実運用演習はSF-24で追跡する。

### SF-24 保持と復元とMVP総合受け入れ

依存SF-19〜23。試験追加先は `tests/acceptance/test_sf24.py`。E2E対象はPython 3.11の評価専用Repositoryとし、次の3要求を固定base SHA・独立した非変更テストで評価する。Repository ID/remote、Profile、image、Agent manifest、費用上限を試験前に記録する。

| 評価Task | 固定する要求と入力例 | 合格に必要な独立検証 |
|---|---|---|
| EVAL-01 空状態 | `describe_items([])` は `データがありません`、`["A"]` は `A`、`["A","B"]` は `A, B` を返す。非空配列の既存挙動を維持 | 0件/1件/複数件のgolden testとAC別reviewがPASS |
| EVAL-02 入力検証 | `parse_count(s)` はASCII数字1〜9桁だけを整数化。`"3"`→3、`"0"`→0、`"999999999"`→999999999。空文字/負数/英字/小数/10桁/前後空白はValueError | 正常/境界/不正入力の各クラスのgolden testと既存API互換テストがPASS |
| EVAL-03 空白整形 | U+0020だけを前後除去・連続圧縮。`" a  b "`→`a b`、空/半角空白だけ→空文字、`" あ  い "`→`あ い`。tab/改行/全角空白は維持 | ASCII/日本語/空入力/非対象空白のgolden testと既存非対象関数の回帰がPASS |

各baseは対象の要求で失敗し、既存の非対象テストは成功する状態を用意する。golden testとProfileはAgentの変更領域外に保存する。実Agentが偶然既存実装を通しただけのケースを成功実績にしない。

| AC ID | 前提と操作 | 合格となる期待結果 | 検証方法と証拠 |
|---|---|---|---|
| SF-24-AC01 | EVAL-01〜03のbase、入力TaskSpec、golden test、実Agent/検証/費用manifestを固定 | 実行前の想定失敗と非対象回帰成功を再現。全ACが独立チェックに対応し、再実行時に同fixtureを使用 | IT：fixture digest、baseのJUnit、AC→check対応表、全manifest |
| SF-24-AC02 | 同一対象revisionで3 Taskを各1 Run、実Agent→検証→Draft PR→実CI→結果通知まで実行 | 3 Run全てVERIFIED_DRAFT_PR、人間のコード修正0、修正各2回以内、merge/deploy 0。実候補とPR headが一致 | 実サービスE2E：3 Run全履歴、Artifact、Gate、実PR/CI/通知URL、編集主体と介入記録 |
| SF-24-AC03 | AT-01〜12を、下記対応ACの試験として全件実行 | 全12件PASS。必要な実process/Artifact/PR競合をFake結果だけで代用せず、skip/未検証0 | IT/E2E：AT→AC→JUnit case→証拠の対応台帳、各障害の注入点 |
| SF-24-AC04 | 100 Tasks/100 Runs/30 Candidates/1,000 Artifacts合計1 GiB、DB dump 1 GiB以下を用意して24時間ごとに整合backup | DBとArtifact manifestの参照/digestが整合し、7世代を保持。秘密を含むbackupはアクセス制限・暗号化し別の保存領域へ置く | IT：backup時刻/manifest/hash/サイズ、アクセス拒否・復号/整合確認結果 |
| SF-24-AC05 | 障害発生後、直近backupを別の空DB/Storageへ手順どおり復元 | 復元対象時点の全参照/hash/費用一致、RPO≤24時間、復元開始からhealthと整合確認までRTO≤60分。旧Leaseは失効させ、再認証/公開照合前に再割当しない | 復元演習：開始/終了/backup時刻、整合SQL、旧Token401/再認証後の旧Lease報告409、remote PR照合と重複作成0 |
| SF-24-AC06 | 未確定1時間、確定証拠30日、Task等90日の各期限前/一致/後に回収を2回実行 | 期限到達した削除可能物だけ回収。稼働/承認待ち/UNKNOWN参照は保持。削除後GET 404、証拠期限切れを明示。backup内残存は最大7世代で解消 | IT：注入時計、DB/Storage一覧、参照保護、削除監査。現行データをこの文書更新で削除しない |
| SF-24-AC07 | 別担当者が停止/復旧/鍵失効/Artifact回収/CI再同期のrunbookを実行 | 手順外のコード/DB直接修正なしで復旧し、判定不能はBLOCKED/UNKNOWNと必要判断を表示。秘密取得権限を一般Workerに渡さない | 運用演習：実行者、全commandのマスク済みログ、各手順の期待/実測結果 |
| SF-24-AC08 | 評価batchの全Runと故障注入試験から集計・最終チェックリスト作成 | 実Task batchと故障試験を分けて全件表示。成功率の分母・期間・タスク種別・取消/仕様変更・費用/時間/修正/介入を欠落なく報告。全Task AC合格までMVP完了にしない | IT/集計照合：全Run一覧から再計算。失敗後の再評価も旧batchを削除せず記録 |

0019でRun終了時刻、証拠回収時刻、回収済みのイベント番号を永続化した。終了後30日でArtifactと期限到達済みの履歴・Workerログを回収し、稼働中のRun・Leaseと未解決の公開処理は保護する。SSEは回収済みの番号を410とし、後から追加されたイベントは保持期限まで再開できる。0020で終了Runと関連する費用・証拠を90日後に回収し、Runを持たないTaskと期限到達済み監査も回収する。既存Taskの期限は移行日時から起算する。削除された親の冪等性応答を削除日からさらに90日保持し、認証と同一操作を照合する。Jira由来Taskの回収後は再取込を防ぐ識別情報を残す。同規模でのバックアップ・復元は後述の演習で確認した。別担当者による運用演習は追跡中。

対象 `ec60857` で100 Tasks、100 Runs、30 Candidates、1,000 Artifacts（1 GiB）を一時DB・Storageに生成し、8回暗号化して7世代を保持した。別の空DBとStorageに復元し、全ArtifactのサイズとSHA-256、DBの件数、費用台帳と残高を照合した。演習時の復元ポイント差は3.108秒、復元開始からhealth/照合まで6.211秒。旧Worker Tokenは401、同一Workerの再認証後に旧Leaseは409、停止中の新規Claimは204だった。証拠は `.local/verification/sf24-scale/observation.json` に保存した。8世代は連続実行であり、24時間ごとの運用は別途検証する。

0021で復元時のIN_FLIGHTをUNKNOWNへ戻し、照合完了までWorkspace解除を拒否する。ローカル管理者が同じPublisher IDで資格情報を再発行できる。一時DBの復元、実GitオブジェクトとGitHub API fixtureを使い、照合前の解除409、旧Publisher 401、再発行後の既存PR復旧、追加push/PR作成0、照合後の解除を確認した。さらに対象 `9ba4518` で実[Draft PR #8](https://github.com/iwata-n/software-factory/pull/8)を作成し、結果送信前のIN_FLIGHTを暗号化バックアップから復元した。旧Publisherは401、照合前の解除は409、再発行後は実GitHubへのGET 4件だけで同じPRとheadを回復した。対象PR数は前後とも1件で、照合後に停止解除できた。証拠は `.local/evaluation/live-publication-restore-01/evaluation.json` に保存した。

## 要件の対応

| 設計要件 | 主なタスク |
|---|---|
| FR-01 管理・認可 | SF-02、SF-08、SF-23 |
| FR-02 要求の版管理 | SF-03 |
| FR-03 計画 | SF-04、SF-14 |
| FR-04 永続Job | SF-05、SF-07 |
| FR-05 隔離Agent | SF-06、SF-20、SF-21 |
| FR-06 Candidate | SF-09、SF-10 |
| FR-07 品質 | SF-11、SF-12 |
| FR-08 修正 | SF-13、SF-14 |
| FR-09 PRとCI | SF-17〜SF-19 |
| FR-10 人間の介入 | SF-07、SF-16 |
| FR-11 監査と費用 | SF-07、SF-15、SF-23 |
| FR-12 外部連携 | SF-22 |

## 設計書の受け入れ試験との対応

SF-24-AC03では、以下の対象ACに列挙したケースを全て実行する。初回58テストで部分的に確認した項目も、追加した異常系・実環境・証拠要件まで満たしてからATをPASSとする。

| AT ID | 対象AC | 合格判定 |
|---|---|---|
| AT-01 Worker強制終了 | SF-05-AC04、SF-21-AC05 | 実Worker停止/Lease失効から上限内再割当し、停止確認と実行権失効を区別する |
| AT-02 完了応答消失 | SF-06-AC03、C-03 | 受理済み結果を同一再送へ返し、Job/予約/次工程が重複しない |
| AT-03 旧Attempt遅延 | SF-05-AC03、SF-05-AC04、SF-09-AC05 | 回収前でも期限後拒否、再割当後も旧fence拒否。現候補/証拠へ採用0 |
| AT-04 検証・レビュー同時完了 | SF-12-AC04 | 全到着順/同時/重複で必要証拠をそろえ、Gate/次Jobが一回だけ確定 |
| AT-05 旧候補CI | SF-18-AC03 | head/base/tested SHAの違う結果を現候補の合格に使わない |
| AT-06 必須テスト削除 | SF-04-AC05、SF-10-AC05、SF-11-AC03 | 計画と実差分の両方で検出し、削除/skip/対象縮小をPASSにしない |
| AT-07 修正後も同一候補 | SF-13-AC03、SF-13-AC04 | 同一コード/無進展/比較不能で上限前でも停止し、次修正を作らない |
| AT-08 PR直後Publisher停止 | SF-19-AC01、SF-19-AC02 | 実PRと照合し重複createを送らず、判定不能はUNKNOWNで止める |
| AT-09 取消・公開競合 | SF-07-AC02、SF-17-AC04、SF-19-AC04 | 未開始送信0、開始済みは照合。取消Runを成功にせず外部結果を別表示 |
| AT-10 CP再起動 | SF-07-AC05、SF-19-AC06 | 実行/再試行/承認/CI/UNKNOWN/終端を復元し、期限と履歴を失わない |
| AT-11 別Workspace認証 | C-02、SF-02-AC03、SF-09-AC04、SF-09-AC07、SF-21-AC02 | Task/ログ/Artifact/cache/Lease/API/DB参照を越境させない |
| AT-12 同時Claim・予算 | SF-05-AC02、SF-05-AC07、SF-15-AC02 | Worker/Run slot・Job・予約が二重確保されず、障害時に全体rollback |

## 既存実装の再評価と証拠台帳

初回58テストは基盤の回帰試験として保持する。以下はSF-01〜07再評価時点で把握していた不足一覧の履歴である。最新の実装・受入状態はタスク一覧と末尾の作業記録を参照する。

| 対象 | 既存試験の候補 | 新条件で追加実装または再確認する範囲 |
|---|---|---|
| SF-01 | `tests/test_control_plane.py::test_migration_matches_models`、`tests/test_processes.py` | PostgreSQL 17の実CI、移行前データ保持、AC別証拠保存とREADME再現 |
| SF-02 | `tests/test_control_plane.py` の越境/ロール/FK/Enrollment、`tests/test_contracts.py` | Enrollment期限/並行使用、並行Spec版追加、pool再利用の認可 |
| SF-03 | snapshot固定、上限縮小、必須チェック拒否の試験 | resolver故障、全snapshot項目、権限取消、HTTPヘッダを含む冪等性 |
| SF-04 | `tests/test_contracts.py` と不正Plan完了試験 | Planの重複AC ID、保護分類、symlink解決、不正状態表の全行 |
| SF-05 | Lease失効/古いfence/同時Claim/rollbackの試験 | 依存・priority、Run slot、期限一致、20回競合、Long Poll中ロック、実kill |
| SF-06 | `tests/test_worker.py`、`tests/test_processes.py` | 全Adapter操作、非対応protocol、429/5xx、接続再開と秘密/通信監査 |
| SF-07 | 取消/履歴/Graph/再起動の試験 | Worker sequence受付、後続追加後の全待機状態、取消競合とOutboxの全障害点 |
| SF-15 | 予算不足、usage超過、同時Claim、失効時暫定計上の試験 | 個別usage/予約/調整台帳、遅延訂正、計測不能、予算承認 |
| SF-23 | 履歴/Graph/Worker一覧とToken失効の試験 | 運用画面、SSE、Token更新/期限、操作別権限、緊急停止と追記監査 |
| その他 | 対象機能未実装 | 試験追加先の作成から実装し、必要な実接続試験まで実施 |

AC単位の証拠台帳は少なくとも `ac_id, revision, test_case_ids, fixture_digest, expected, observed, result, executed_at, evidence_paths, external_urls` を持つ。resultはPASS/FAIL/未検証、タスクの受入状態は未判定/不合格/受け入れ済みとする。全ACに証拠がそろったときだけ受入状態を更新し、コード/設定変更時は影響するACを未判定へ戻す。

タスク単体の実行形は以下とする。revisionを設定し、実装済みの01〜09からtaskを選び、TEST_DATABASE_URLと必要なDocker Runnerを用意する。10〜24は対象試験の実装後に実行する。単体実行では他タスクの未実行条件を未検証と記録する。最終受け入れは全既存回帰を含む一括実行と証拠検査、必要な実CI検証で確定する。

```sh
factory_acceptance_revision='対象revisionの識別子'
factory_acceptance_task='01'
mkdir -p ".local/acceptance/${factory_acceptance_revision}"
FACTORY_ACCEPTANCE_OUTPUT=".local/acceptance/${factory_acceptance_revision}" \
  uv run pytest "tests/acceptance/test_sf${factory_acceptance_task}.py" -q \
  --junitxml=".local/acceptance/${factory_acceptance_revision}/junit.xml"
uv run ruff check .
uv run ruff format --check .
```

上記に加えC-01の既存回帰を実行し、C-05のresult JSON・環境・DB/Artifact/API・実サービス証拠を保存する。JUnitだけでは、対象コードや実GitHubとの一致を証明したことにしない。

## 初期実装で固定した判断

- Control Planeは同期SQLAlchemyとFastAPI、DBはPostgreSQL。WorkerはAPIだけを使う別エントリーポイントとする。
- Tenant内の変更操作は短いWorkspace行ロックで直列化する。初期の処理量に合わせた選択であり、並列Unit導入時にWorker・Run・予算ロックへ細分化する。Long Pollingの待機中はトランザクションを持たない。
- 認証はハッシュ保存するWorkspace専用ローカルBearer資格情報。SF-23でローカル操作権限とWorker更新/失効を追加する。SSO・チームmembership・外部ユーザー認証はMVP外。資格情報は初回発行時だけ返す。
- remote_url未指定のRepositoryは管理者固定の `base_ref → base_sha` を使う。remote_url指定時はSF-08の別Source Runnerで解決し、SHA・tree/digest・symlink調査結果をRunへ固定する。Git取得や生成コードをControl Plane内で実行しない。
- 実行イメージdigestはスナップショットに保存する。Fake Agentはコンテナを起動せず、digestの実環境との一致検証はSF-21で行う。
- 費用単位は整数のmicro USD。失効Attemptは予約額を消費済みとして保守的に記録する。実課金上限を保証する計測ではない。
- 作成・仕様版追加・Run開始・取消・完了・失敗報告には `Idempotency-Key` を要求する。Claim・Heartbeatは現在の実行権を返す操作とし、登録には一回限りのEnrollment Tokenを使う。登録応答消失時は管理者が新Tokenを発行する。
- Workerイベントのsequence付きバッチ受付を追加し、同Attempt/sequenceの同内容を重複保存せず、別内容は409で拒否する。Artifact、SSE、承認、Publisher用APIは後続とし、受理できない成果物IDを黙って無視しない。

## 次の実装単位

SF-01〜14は実CIとArtifact照合まで完了した。次の機能実装単位はSF-10〜12とする。隔離Assemblerを不変Candidateへ接続し、独立検証・Review・決定論的Gateをつなぐ。以後も残るSF-13〜24まで全受け入れ条件を検証する。

受け入れ設定は上記の値で固定する。実環境の設定manifestとして、評価用HTTPS Repository/Integration ID、実Agentのengine/model/料金版、image digest、許可接続先、検証command/AC対応、通知先、backup保存先を担当タスクの開始時に記入する。未設定や資格情報不足は該当ACを未検証とし、代替Fakeで合格扱いにしない。値の変更はPolicy/manifestの新版と影響ACの再検証を必要とする。

## 初回実装の検証結果

2026年11月2日、Python 3.11.15と一時PostgreSQL 16.2で全58テストが通過した。Lease失効・完了再送・越境拒否・同時Claimに加え、管理CLIの発行と失効、デモ登録、DB資格情報を持たない別プロセスWorker、Control Planeのプロセス再起動を含む。Ruffのlint・format検査、依存ロックとの一致、マイグレーションとモデルの一致も確認した。

この初回検証ではStarletteのTestClientからhttpx利用に対する非推奨警告が1件出た。アプリWorkerのHTTP通信はhttpxを使用する。この時点ではPostgreSQL 17向けworkflowの追加までで、GitHub上の実行は未実施だった。今回の実行結果は以下に記録する。

この58件は初回実装時の記録であり、新ACの受け入れ結果とは分けて保持する。受け入れ条件を具体化した段階では文書だけを変更し、今回の再評価で以下の実装・試験を追加した。既存の実データに対する削除や本番設定変更は行っていない。

受け入れ条件を具体化した時点の文書検証では、24タスク/151 AC/共通6条件/AT 12件の網羅、IDの一意性と連番、一覧のAC範囲、全参照の解決、タスク依存の非循環、表の列数、節リンク、既存試験名、実行例のshell構文を確認した。この文書検証自体には追加ACの実行・実サービス接続試験を含めていなかった。

## SF-01〜07の再評価結果

2026年11月2日、ローカルのPython 3.11.15・実PostgreSQL 17.11で追加269ケースと既存58ケース、合計327ケースが成功した（53.75秒）。実GitHub ActionsでもPython 3.11.18・PostgreSQL 17.11で同じ327ケースが成功した（94.95秒）。両環境ともskip/xfailは0。Ruffのlint/format、空schema/0001からのupgrade、モデル差分0も成功した。

条件別証拠は43 AC＋共通6条件の49件。取得した実CI Artifactに対して、入力コード/設定digest、JUnit case、fixture digest、環境、期待/実測、GitHub実runのhead SHAを独立した証拠検査で照合し、49件すべてPASS・未検証0を確認した。SF-01-AC06も実CI成功とREADMEの新規環境再現を満たした。

| 対象 | 補完した実装と主な検証 | 最終受け入れ結果 |
|---|---|---|
| SF-01 | 0001→0002のデータ保持、空schemaの移行、health 503、空venvのlocked installとREADMEの別プロセス再現、実CI Artifact取得 | AC01〜06 PASS |
| SF-02 | 全Public/Worker APIの401/403/404 matrix、別Workspace/ProjectのFK、並行仕様版、Enrollment 20回競合と期限境界、100回pool切替 | 6 AC PASS |
| SF-03 | DB TX外のresolver境界、解決失敗時のRun/Job作成0と監査、全snapshot項目、現在のRepository/Worker権限優先、同時開始20回と応答再送 | 6 AC PASS |
| SF-04 | 決定論的遷移のI/O禁止と100回反復、重複AC/依存/未知工程拒否、保護pathと実symlink調査、終端/待機からの進行拒否 | 6 AC PASS |
| SF-05 | 永続Job依存・priority順、Worker/Run slot競合を各20回、Lease期限境界、実Supervisor kill→再割当、2/4秒再試行、20秒PollのTXなし、各commit前障害 | 7 AC PASS |
| SF-06 | HTTP-onlyプロセス、Fakeの3分岐、受理後応答消失の同一再送、期限監視と非同期Heartbeat、非対応/恒久エラー停止、Adapter eventsと429/503再送 | 6 AC PASS |
| SF-07 | queued/retry/active取消、40回のHeartbeat/完了競合、論理失効と物理停止の区別、5状態の実CP再起動、sequence 1/3/2/2と競合rollback、再起動後再送 | 6 AC PASS |
| 共通 | 変更API全種のキー必須/同内容再送/別内容409、監査/Outbox/費用の同TX、入力一致と証拠検査、秘密値/秘密を含むキー/エラー応答のマスク | C-01〜06 PASS |

証拠生成の実行コマンドは次のとおり。`FACTORY_POSTGRES_BIN` はPostgreSQL 17の実binを指定する。CIではPostgreSQL 17 serviceとTEST_DATABASE_URLを使って同じpytestを実行する。

```sh
FACTORY_POSTGRES_BIN='/path/to/postgresql17/bin' \
  uv run python scripts/with_postgres.py pytest -q --tb=short \
  --junitxml=.local/acceptance/current/junit.xml
uv run python scripts/verify_acceptance.py .local/acceptance/current --allow-ci-pending
```

`current` はローカル検証中の保存先とし、実CIの証拠は以下の入力revision別ディレクトリへ確定保存した。`--allow-ci-pending` はCI実行中にSF-01-AC06だけを未検証として許すオプション。最終検査はこのオプションなしで実行し、未検証を残さない。

| 証拠の識別子 | 確認した値 |
|---|---|
| 実装・試験のcommit SHA | `e4fe06835e424d8e2a3c84ffe4c60ead23b5eeb5` |
| コード・試験・設定・AC定義のSHA-256 | `4df51eb3d448fde8dfa799cfc3486150b7b2ea79d8297d745f337d9727ac9243` |
| 実CI | [control-plane / run 41371266984](https://github.com/iwata-n/software-factory/actions/runs/41371266984)、push起動、対象head一致、conclusion=success |
| 実CI Artifact | [sf01-07-acceptance / 13108974860](https://github.com/iwata-n/software-factory/actions/runs/41371266984/artifacts/13108974860)、取得時非失効、保存期間30日 |
| 取得ZIPのSHA-256 | `683587266e07f8459cb6cce366e16abf0b660cbf99e281dcc8f205348241cecc`。GitHub APIのArtifact digestと実ファイルdigestが一致 |
| 確定保存先 | `.local/acceptance/e4fe06835e424d8e2a3c84ffe4c60ead23b5eeb5+inputs-4df51eb3d448fde8/` |
| 条件別結果 | 上記保存先の `SF-01/`〜`SF-07/` と `common/` に49件のJSON。全ACをpytest `ac` markerと実JUnit caseへ対応付けた |
| 全体結果 | `summary.json` は49件PASS、`external_pending=[]`。`manifest.json` に環境/時刻/入力一覧、`junit.xml` に327件、`github-verification.json` に実run/Artifact照合結果 |

```sh
uv run python scripts/collect_ci_evidence.py \
  --repository iwata-n/software-factory --run-id 41371266984 \
  --output .local/acceptance/e4fe06835e424d8e2a3c84ffe4c60ead23b5eeb5+inputs-4df51eb3d448fde8
uv run python scripts/verify_acceptance.py \
  .local/acceptance/e4fe06835e424d8e2a3c84ffe4c60ead23b5eeb5+inputs-4df51eb3d448fde8
# {"verified_records": 49, "external_pending": []}
```

証拠の入力digestには対象コード、試験、workflow、依存lock、README、AC定義、固定試験設定を含む。結果追記や一覧の受入状態だけの文書変更は入力から除外する。今回の結果追記でAC定義・固定試験設定・実装は変更していない。ローカルの未commit検証とCIのcommit検証は別のmanifestに保存し、実CIの証拠は上記commitの結果として扱う。

警告はテストでStarlette TestClientのhttpx利用非推奨が1件、ActionsでNode 20対象actionのNode 24強制実行とubuntu-latest移行の通知がある。いずれも実行成功を確認した。依存/action/runnerの更新は別変更としてlock・環境を記録して再評価する。

SF-03の公開認可とSF-07の承認/CI待機の復旧は境界Fakeの契約試験。実Repository取得、Candidate、実Agent、OS隔離、公開・外部CI同期の受け入れはSF-08以降に残る。今回のGitHub Actions成功はこの基盤コードのCI検証であり、SF-18の製品機能を実装済みにしたものではない。

## SF-08以降の作業記録

評価先は `config/evaluation.json` に保存する。ユーザー指定の既存Codex CLI認証、Jira `https://pokeca-chips.atlassian.net` のSWF、Slack `C0B4FJX81TM` を使用する。JiraサイトとSlackチャンネルの読取接続、Codex CLI 0.166.0-alpha.5.1のChatGPTログイン状態を確認済み。モデル/料金/実Agent image等の未確定manifestを架空の値で埋めて受け入れない。

- SF-08：Source RunnerをCPとは別のHTTPサービスとして追加。Gitを実Docker内で実行し、HTTPS allowlist、固定SHA、tree/code digest、submodule/LFS/hook/外部設定/pathの拒否、取得前後の権限確認を実装。実HTTPS branch/tag/full SHA、移動/消失するbare ref、2つの実TLSサーバーでredirect先接続0・認証拒否・転送中断・timeoutを検証した。実別PIDとCPのprocess起動禁止、同Jobの3試行・2/4秒待機・入力固定、POLICY拒否時BLOCKEDも確認した。固定評価先は公開HTTPSであり、未接続の非公開Repositoryまで検証済みとはしない。現行revisionの全回帰と実CI照合を継続する。
- SF-09：0004でLease/Workspace/Run/Job/Attemptに結び付いたArtifactを追加。16 MiB、1時間予約、予約→upload→finalize、認証付き取得、immutable bytes、symlink/hardlink対策、未確定GC、LOGの秘密マスクを実装。20回競合、サイズ・期限境界、取消、受理済み再送を含む追加試験を実行中。現行revisionの全回帰と実CIまで未判定を維持する。
- SF-10：通信なし、non-root/read-only root、host mountなしの実Assemblerを追加。base commit/tree/digest照合、UTF-8 patch、実差分の保護manifest/許可path照合、脱出拒否、固定parentの再現可能commitとcode digestを8件の実コンテナ試験で確認した。0005で不変Candidateと管理Workerを追加し、Artifact input grant、Attempt fence、VERIFY/REVIEWの各1 Job生成、受理済み再送を接続した。実Workerをpatch取得中/適用後/commit前/報告直後にSIGKILLし、新Supervisorによる孤立container回収と同入力再試行を確認した。20回同時確定、保護pathの編集/削除/rename/mode変更、hook・symlink/hardlink脱出拒否も実行した。全回帰と実CI照合まで未判定を維持する。

新たな証拠台帳は実装済みの受入試験モジュールを対象とし、未実装タスクはこの計画上で未判定のまま残す。入力digestにはRunnerコード・Dockerfile・評価設定と全AC定義も含める。試験が成功しても、対象ACが要求する実接続・競合・故障点を満たす証拠がなければ受け入れ済みにしない。

2026年11月2日の現行変更のローカル一括検証は477件PASS（92.53秒）、skip/xfail 0。Python 3.11.15・PostgreSQL 17.11・Runner実digest `sha256:e8d309533f5803ab87488789adbae97ae0a14b888d6a3d524015e88947ae27b6` を使用した。入力digestは `cf0ec21260e1904d24950e3c81afdb3ffec48b8250ce6ed7349b43970cfdc71b`。SF-01〜09の56 ACと共通6条件の62件を照合し、実CI証拠を要するSF-01-AC06だけを未検証で保持している。CI Artifact名は対象拡張に合わせ `factory-acceptance` とした。

追加確認は、実SupervisorのSource取得→起動・失敗時start 0・連続Job、取得中の期限監視、取得時に固定したsymlink metadataのPlan検証、新設8 API×6主体の認可matrix、実TCPの部分upload切断、保存bytesの改ざん検出、別AttemptのAVAILABLE成果物拒否を含む。Candidate API等の未完了部分をこの477件で受け入れたことにはしない。

### SF-08/09の受入確定

対象commit `512d89bad04723aa2e2d57baf8d3254b72853dbf` の[実CI run 41377137326](https://github.com/iwata-n/software-factory/actions/runs/41377137326)が成功し、477ケースPASS（146.59秒）、skip/xfail 0を確認した。CIはPython 3.11.18・PostgreSQL 17.11、Runner実digest `sha256:305360437fe7aeb48d2c03efb07f67b5b3e88c4dd47dab95fbfb1eda04d2c931`。ローカルと同じコード入力digest `cf0ec21260e1904d24950e3c81afdb3ffec48b8250ce6ed7349b43970cfdc71b` に一致する。

[取得済みfactory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41377137326/artifacts/13112320047)のZIP SHA-256は `12bdcc0a46efb0f74a6ab055cb3adb2803272f5300d13ffb889864bf8a405682`。GitHub APIのdigestと一致し、head SHA・JUnit・全条件JSON・fixture digestを検査して `verified_records=62, external_pending=[]` となった。確定保存先は `.local/acceptance/512d89bad04723aa2e2d57baf8d3254b72853dbf+inputs-cf0ec21260e1904d/`。

| 対象 | 判定 | 実接続・異常系の証拠 |
|---|---|---|
| SF-01〜07 | 43 AC再評価PASS | 既存327ケースを維持し、現在の0004 schemaと追加APIを含む全回帰が成功 |
| SF-08 | AC01〜06 PASS | 実HTTPS branch/tag/full SHA、bare ref移動・消失、実TLS 2サーバー、読取認証拒否、3試行上限、Source別PID、CP起動禁止、実Supervisorの取得/期限/取消境界 |
| SF-09 | AC01〜07 PASS | 実Storageと16 MiB境界、20回確定競合、48認可組合せ、Lease期限一致・取消・受理済み再送、実TCP中断、bytes改ざん・保存先脱出・別Attempt拒否、GC、秘密マスク |
| 共通 | C-01〜06 PASS | 全回帰・DB移行・認可・冪等性・TX rollback・秘密非露出と、実CI Artifactを対象revisionへ照合 |

この受入確定はSF-01〜09の対象revisionに対する結果である。SF-10を含む現在の変更は全回帰・実CIで再評価し、以下に別の対象revisionとして記録する。残り15タスク全体の完了を示すものではない。


### SF-10の受入確定

対象commit `b38ef0c6e0d4ea4d35f2372ba07f193b31a8db8b` の[実CI run 41384422711](https://github.com/iwata-n/software-factory/actions/runs/41384422711)が成功した。ローカル544件PASS（179.59秒）、CI544件PASS（239.97秒）、両環境skip/xfail 0。OpenAPI操作IDの重複も解消した。残る警告はStarlette TestClientのhttpx利用非推奨1件。

入力digestは `658262014cf2c52a5f920870d3df99269d112f5b0b6121ed0566089234d2299b`。[factory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41384422711/artifacts/13114677411)のZIP SHA-256 `cbef2fa31329e15477affd590dc621afe2b991609b0377185b065db25532d177` がGitHub APIのdigestと一致した。全63 ACと共通6条件の69件を対象SHA・JUnit・fixtureへ照合し、`verified_records=69, external_pending=[]` を確認した。保存先は `.local/acceptance/b38ef0c6e0d4ea4d35f2372ba07f193b31a8db8b+inputs-658262014cf2c52a/`。

SF-10の追加64ケースは、実Assemblerからの固定CandidateとVERIFY/REVIEW入力、20回同時確定、API/SQL不変制約、権限・fence・期限境界、全TX rollback、実差分攻撃、4地点でのWorker SIGKILL・孤立container回収・同入力再試行を含む。既存SF-01〜09も全回帰した。CIの実image digestは `sha256:2a6c47e1296aebdfe8f168e4b10ec64b9a4e958dcd51c3514d2ec7d8b5c60096`。起動時回収は確認済みだが、独立watchdogによる停止期限保証はSF-21で引き続き実装・検証する。


### SF-11の実装と検証中の記録

0006で追記専用Check/Finding、管理Workerに限定した証拠提出、Candidate/head/Spec/Profile/image/originの固定、実JUnit bytesからのサーバー判定、AC別レビューと実在位置/Artifactの照合を追加した。Verifierは新しい使い捨てcontainerでコードを再検算し、root所有のDocker volumeをread-onlyでmountする。ログ/JUnitの書込先だけを別に持つ。管理対象Profileのargv/timeout/必須IDを固定し、Agent自己申告のcommand/PASS/originは採用しない。

ローカルのSF-11試験52ケースがPASS。実pytestの成功/失敗、実timeout/起動不能、不正/欠落XML、0件/全skip/必須ID欠落、5種類の対象tuple不一致、AC別3判定、位置/証拠不正8種類、20回同時完了、全TX rollback、DB不変制約、新API認可を含む。レビューのモデル呼出そのものはSF-20で検証する。現段階のFixture Reviewerは契約検証用であり、実Agentの代用にはしない。全回帰と対象revisionの実CIが完了するまでSF-11は未判定を維持する。


### SF-11の受入確定

対象commit `5e4648476a03e69576061e30c15d8babf8b3311b` の[実CI run 41386563418](https://github.com/iwata-n/software-factory/actions/runs/41386563418)が成功した。固定したdetached worktreeでのローカル全597件PASS（280.94秒）、CI597件PASS（368.53秒）、両環境skip/xfail 0。0001と直前0005からのデータ保持、0006のモデル差分0も含む。

入力digest `1cb35082ebd42b4a8d9d4f9f39e45becde990a5a7dd0f3ba4d66cc8bfacb48fe`、[factory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41386563418/artifacts/13115900947)のZIP SHA-256 `e7224b26c7d9ef2e2a6e1693c6861b9e724cf29d04d140a92c7daea0384b988e` が実GitHub APIと一致した。全69 ACと共通6条件を対象SHA・JUnit・fixtureへ照合し、`verified_records=75, external_pending=[]` を確認した。保存先は `.local/acceptance/5e4648476a03e69576061e30c15d8babf8b3311b+inputs-1cb35082ebd42b4a/`。SF-11の受け入れは独立検証とレビューの証拠契約に対するもので、実モデル呼出はSF-20に残る。


### SF-12の実装と検証中の記録

0007で不変Gate台帳を追加し、純粋なLocal/Final判定をCPへ接続した。必須Job中は待機し、固定優先順、最新Attemptのみの採用、対象/出所照合、採用集合/hash/Policy版、次工程1回、確定TXを実装した。FAILは上限内Repair要求、ERRORは同じJob/inputのインフラretry、INCONCLUSIVEはBLOCKEDとする。合格したLocal Gateは公開要求を保存し、実Publisherの接続はSF-17に残る。

ローカルのSF-12先行128ケースがPASS。全判定表、到着順2種類と20回の完了/reconcile競合、全TX rollback、API/SQL不変性、拒否監査、stale対象の公開拒否、同一入力3試行、DB永続CI期限の−1ms/一致/+1ms、head/merge SHAとbase/headの合成CI証拠を含む。CI基盤ERRORの固定対象retry要求・上限・期限も追加検証する。実GitHub CIの製品内同期はSF-18/19で確認し、この合成CIの結果で代用しない。全回帰と対象revisionの実CI照合までSF-12は未判定を維持する。


### SF-12の受入確定

対象commit `d5be902db764d5bf018441d6977ec4b9f8b835cb` の[実CI run 41388602952](https://github.com/iwata-n/software-factory/actions/runs/41388602952)が成功した。固定worktreeのローカル727件PASS（380.00秒）、CI727件PASS（482.54秒）、両環境skip/xfail 0。0007のモデル一致と、0001/直前0006からのデータ保持を含む。

入力digest `61b62c94faae97e98cd0afcb47ab462551a5e1c87da127051ce0c32082c5501d`、[factory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41388602952/artifacts/13117035062)のZIP SHA-256 `da0deb1c35f0c14d250841acfcc02df6fb424a4ec1ae3eae3e2b20a4b439f123` が実GitHub APIと一致した。全76 ACと共通6条件を対象SHA・JUnit・fixtureへ照合し、`verified_records=82, external_pending=[]`。保存先は `.local/acceptance/d5be902db764d5bf018441d6977ec4b9f8b835cb+inputs-61b62c94faae97e9/`。

SF-12追加130ケースは、判定表、到着順・20回競合、同じ入力の検証再試行、CI再試行中の固定deadline・上限、期限境界、固定head/merge/provider/app/check、stale公開拒否、拒否監査、全TX rollbackを含む。M2（SF-08〜12）の全ACを受け入れた。実Publisher/外部CI同期による製品の成功判定はSF-17〜19に残る。


### SF-13の実装と検証中の記録

0008でFindingの安定codeとSpec変更区分を追加し、必須Check/AC/Findingの正規化失敗集合、真部分集合による進展、2回上限をLocal/Final双方へ接続した。Repairは親Candidate/証拠/制約digestを固定し、インフラretryではroundを増やさない。Assemblerは親Archiveを再検算して適用し、root base SHAと親headを別に記録する。同一コードの昇格拒否、保護検証の変更拒否、新Spec要求の停止を実装した。

SF-13の実DB/Runner試験40ケースがPASS。実3世代・2修正と上限、同じRepairの再試行、2 patch形式とcommit messageだけ異なる同一コード、B集合の全比較、実行結果での同一/新規/比較不能、行番号・自由文変更、保護差分、旧結果再送、Local/Final再検証、20回同時完了、全TX rollbackを含む。Final CI部分は固定した合成証拠による契約試験であり、実接続はSF-18/19に残す。全回帰と実CI照合までSF-13は未判定。


### SF-13の受入確定

対象commit `633c17825a5befbf6bbe390352f32c81faffb1a2` の[実CI run 41390810231](https://github.com/iwata-n/software-factory/actions/runs/41390810231)が成功した。固定worktreeのローカル767件PASS（493.89秒）、CI767件PASS（503.86秒）、両環境skip/xfail 0。0008のモデル一致と、0001/直前0007からのデータ保持を含む。

入力digest `a76c9c6dd5ea7f36061df6ddbd95793d6e4da56cbe5d48e1ae8018241d0f16d1`、[factory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41390810231/artifacts/13117089057)のZIP SHA-256 `ffe427d3d9badcde876efffa678b31420e5a0b618aee840330816504890f39a9` が実GitHub APIと一致した。全82 ACと共通6条件を対象SHA・JUnit・fixtureへ照合し、`verified_records=88, external_pending=[]`。保存先は `.local/acceptance/633c17825a5befbf6bbe390352f32c81faffb1a2+inputs-a76c9c6dd5ea7f36/`。

SF-13追加40ケースは実Local/Final修正、2回上限、同一入力retry、code digest同一拒否、Bの厳密比較、保護変更・新Spec停止、旧結果不採用、20回競合と全TX rollbackを含む。Final CIは契約用の合成入力で、実CI同期はSF-18/19に残る。


### SF-14の実装と検証中の記録

0009でPlan body/hashの不変性・生成Jobの一意性、固定対象の承認要求と追記専用決定を追加した。旧Planのbodyを保持してhashを補完する。構造/AC網羅の再計画を最大1回に制限し、採用Plan ID/hashを後続工程に固定した。承認必須Policyでは24時間待機し、Plan/Spec/base/Profile/Policy/revision/Candidate/証拠/現予算を照合する。異なる目的/AC/Repositoryの要求はsnapshotを上書きせず新Spec・新Runを必要とする。

SF-14の59ケースがPASS。Plan版2採用と再不採用、max_replans=0、質問/保護path停止、待機復元、approve/denyの20回競合と再送、8種類のstale対象、期限−1ms/一致/+1ms、受理済み期限後再送、新設4 APIの認可、全TX rollback、旧0008の実Plan backfill、承認後の実Assembler保護差分、実行中の仕様変更停止を含む。既存の不正Plan試験は、採用しないことと再計画上限の新契約へ合わせて回帰した。全回帰・実CI照合までSF-14は未判定。


### SF-14の受入確定

対象commit `57850d625b35bea57a9616b4edbe7423b701cb7b` の[実CI run 41394045864](https://github.com/iwata-n/software-factory/actions/runs/41394045864)が成功した。固定worktreeのローカル831件PASS（500.58秒）、CI831件PASS（472.05秒）、両環境skip/xfail 0。0009のモデル一致、0001/直前0008からのデータ保持、実在する旧Planのhash backfillも含む。

入力digest `6363e314c36a26cba30923d2c4f5e8777cf78b2067b6a76137c64bba7b0d81c7`、[factory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41394045864/artifacts/13119290708)のZIP SHA-256 `2168ffd50332f845f5fba2e9c28d80de50f9f6cf4dcd32caccb042280ff9379c` が実GitHub APIと一致した。全88 ACと共通6条件を対象SHA・JUnit・fixtureへ照合し、`verified_records=94, external_pending=[]`。保存先は `.local/acceptance/57850d625b35bea57a9616b4edbe7423b701cb7b+inputs-6363e314c36a26cb/`。

SF-14追加59ケースと純粋遷移5ケースを含め全回帰した。1回再計画、Plan版固定、24時間の対象付き承認、20回競合、stale拒否、一般承認で保護差分を許さないこと、新Specが必要な仕様変更と停止を受け入れた。M3の残りはSF-15/16。

### SF-15/16の実装・再検証中の範囲

SF-15は0010で費用/予約/調整とusage受領、予算版を追記台帳へ移行した。累積sequence/delivery IDを照合し、失効時の暫定額から確定実費へ1回だけ調整する。元snapshotを保持する対象付き予算承認と、未知usage/超過時の停止を追加した。旧列保存の移行照合を修正し、遅延usageが別の稼働Attemptを超過させる場合も停止させ、先行するUser取消を保持する。

SF-16は介入APIと個別承認を同じ内部コマンド・認可・対象検証・再送台帳へ集約した。approve_plan/approve_budget/cancel/resolve_blockedだけを許可し、取消のHTTP statusも両経路202とする。停止解除は固定した現対象を再検証し、費用確定・必要残額・同じJobの試行上限、または現Candidateの信頼済み証拠からのGate再評価を要求する。品質/Spec/保護差分/上限を一般承認で上書きしない。CI再開は元deadlineを保持する。全actionの監査・状態・Outboxを同時commitする。

### SF-15の受入確定

対象commit `363d9d81d37b054721f5b10e2f9c19022fb0ae69` の[実CI run 41402970114](https://github.com/iwata-n/software-factory/actions/runs/41402970114)が成功した。固定worktreeのローカル917件PASS（528.81秒）、CI917件PASS（657.68秒）、両環境skip/xfail 0。0010の空DB/モデル一致、直前0009の実残高保持、遅延訂正を含む。

入力digest `6f6c98832c6732814c5b50abf7f3949c3962f4f0e6d224f899379d288727b1d1`、[factory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41402970114/artifacts/13122711999)のZIP SHA-256 `b39b918e666dbf4b0b964a5ad32a22cb6719e429b9e0cb3519baa08b53e9821c` が実GitHub APIと一致した。全94 ACと共通6条件を対象SHA・JUnit・fixtureへ照合し、`verified_records=100, external_pending=[]`。保存先は `.local/acceptance/363d9d81d37b054721f5b10e2f9c19022fb0ae69+inputs-6f6c98832c673281/`。

SF-15追加86ケースで、境界値、20回の予約/承認競合、予約・実費・遅延調整の再計算、未知usage、現在の別Attempt停止、取消保持、元snapshotを変えない予算版、Plan再承認、全TX rollbackを確認した。残りはSF-16〜24の9タスク。

### SF-16の受入確定

対象commit `ffde9f84778bc7f3bf148de2859ba68063c4533a` の[実CI run 41403832101](https://github.com/iwata-n/software-factory/actions/runs/41403832101)が成功した。固定worktreeのローカル1,007件PASS（560.45秒）、CI1,007件PASS（741.24秒）、両環境skip/xfail 0。DBモデル差分0、既存マイグレーションと台帳・承認の全回帰も含む。

入力digest `a3d8a1b1cb96bd3819914d4eec9d5935f5da81387f8b5f40e507b6bf278f2d28`、[factory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41403832101/artifacts/13122748504)のZIP SHA-256 `c31af973be091f971847cddb224a32bad6ee62c0089f46c9bb24c0937c853a02` が実GitHub APIと一致した。全100 ACと共通6条件を対象SHA・JUnit・fixtureへ照合し、`verified_records=106, external_pending=[]`。保存先は `.local/acceptance/ffde9f84778bc7f3bf148de2859ba68063c4533a+inputs-a3d8a1b1cb96bd38/`。

SF-16追加90ケースは、両承認経路の共通認可・対象変異・再送、20回の経路間approve/deny競合、期限境界、全actionの監査・rollback、固定null Candidateの転用拒否、実Verifier証拠不足の拒否、信頼済み証拠の再評価、費用解消後の同入力retry、CI期限保持を含む。M3（SF-13〜16）の全ACを受け入れた。残りはSF-17〜24の8タスク。

### SF-17の実装と検証経過

0011で専用Publisher主体・Repository grant、固定identityの公開操作、追記専用の外部観測、Runあたり1つのDraft PRを追加した。CPは公開開始の認可をTXで確定し、Git/外部APIは別Publisherプロセスで実行する。汎用WorkerはPUBLISH_DRAFTをClaimできない。取消・権限/Policy/Candidate変更後も開始済みの外部結果を記録し、Run成功へ変換しない。

PublisherはCandidateのarchive/digest/tree/raw commit/親chain/保護差分を再照合する。Git objectだけを再構成して既知remote headへの明示的なlease条件でpushし、想定外commitを上書きしない。GitHub書込は対象RepositoryのDraft PR作成だけを許す。モデル・checkout・build・merge/deployは呼ばない。個別・共有journalを送信前後に永続化し、同じ認証の異なる作業dirでの起動・応答消失・再起動も二重公開を防ぐ。

先行44ケース（API/DB 7、実bare GitとGitHub契約12、journal/応答消失/20回同時起動25）が通過した。さらに全ACの条件別54ケースと外部証拠照合を追加した。空DB upgrade/downgrade後のモデル差分0も確認した。`7d91dbec019d8b869d7c0ce7fca9ccd8cc75dbe6` のローカル全1,113件が通過し、実GitHubでDraft PR作成・同PR更新・人間commitの保護を確認した。PR head反映遅延を実検証で発見し、既知の旧headだけ読み取り再試行し、解消しなければUNKNOWNとする修正を加えた。この時点では最終照合前のため未判定とし、後述の対象revisionで受け入れを確定した。

SF-17-AC01〜03は契約試験だけでPASSにせず、対象commitの実GitHub照合証明を必須とした。詳細なDB/Artifact/API履歴はローカルに保持する。CIへ渡す対象は、既に公開されているPR/branch/commitの固定whitelist項目と公開実装のSHA/digestだけとし、private Repository・追加フィールドを拒否する。実装PRの対象SHA付き参照からCIが公開情報を取得・再照合し、証明をArtifactへ保存する。参照がなければ未検証のまま残す。DB/Artifact一式の公開Git blobへの転送は、公開範囲が未確認という自動承認審査の拒否を受け、実行せずpublic情報だけに縮小した。

SF-24の固定評価用baseとgolden testを `examples/evaluation/` に追加し、ホスト上で対象10件の想定失敗・12件成功（非対象API含む）を確認した。実Runnerでのbase照合と実Agent評価は未実行である。SF-17の実GitHub試験はfixture Agent/Reviewerと契約CI入力を使うtransport評価であり、SF-24の実Agent評価batchには数えない。

GitHubの既定ブランチが `codex/sf01-07-acceptance` であることを確認したため、後続実装は新しい `codex/sf17-publisher` に分離した。既定ブランチの更新・mergeは行わず、実公開試験は専用の `codex/factory/<run_id>` を使用する。

### SF-17の受入確定

対象commit `edfc4abdb626d61dcd0c5440c5a7d43a07de76a4` の[実CI run 41419500089](https://github.com/iwata-n/software-factory/actions/runs/41419500089)が成功した。固定worktreeのローカル1,115件PASS（831.89秒）、CI1,115件PASS（812.26秒）、両環境skip/xfail 0。0011の移行・モデル一致と既存全回帰を含む。

入力digest `ad820b0d4effc93b4fef1d556fcbeefe42eb105757f49ae301ea5f0bdb31ef79`、[factory-acceptance Artifact](https://github.com/iwata-n/software-factory/actions/runs/41419500089/artifacts/13130106725)のZIP SHA-256 `c36078181ca5c943e25771785789897a92e98b9d3d4d1dc22a519195a4c51111` が実GitHub APIと一致した。全106 ACと共通6条件を対象SHA・JUnit・fixtureへ照合し、`verified_records=112, external_pending=[]`。保存先は `.local/acceptance/edfc4abdb626d61dcd0c5440c5a7d43a07de76a4+inputs-ad820b0d4effc93b/`。同保存先の `local-regression/` に固定worktreeのローカル試験証拠を保存した。

[実Draft PR #4](https://github.com/iwata-n/software-factory/pull/4)で初回公開、同じPRの修正更新、人間commitとの競合停止を確認し、対象SHA/digest付きの公開GitHub情報を実CI内でも一次APIから再照合した。詳細DB・Artifactのローカル証明は `.local/evaluation/publication-edfc4ab/verified-local/` に保持する。専用Publisherの境界・取消競合・再送と実公開の全条件を受け入れた。この試験のAgent/ReviewerおよびCI判定入力はfixtureであり、SF-18の製品内CI同期とSF-20/24の実Agent評価は引き続き未検証。残りはSF-18〜24の7タスク。

### SF-20の実Codex接続と動作確認

`codex_engine.py` / `codex_adapter.py` を追加し、HTTP Workerの `--adapter codex_cli` から実PLAN/IMPLEMENT/REPAIR、管理pipelineの `--reviewer codex_cli` から独立REVIEWを呼べるようにした。既存CLIのChatGPT認証を使い、APIキーは追加しない。engine manifestでCLI版・binary SHA-256・モデル・推論設定・timeout・入出力上限を固定する。Codexはソースをデータとして受け取り、hostでRepositoryのコードを実行しない。モデルのshell/apps/plugins/browser等を無効にし、CP/Worker/GitHub資格情報をCLIの環境へ渡さない。許可pathと採用Planを超える変更、保護差分、秘密を含む入力、未知イベント、不正/空の結果を拒否する。

実測usageを最終受領APIへ冪等送信してから成果物を確定する。金額はAPI公開単価に基づく予算用推計と明記し、ChatGPT契約の請求額や残枠と混同しない。計測不能を0円に変換しない。既存レビューbackendの入力契約を保つため、CodexだけがJob contextを要求する明示的なbindingを使う。

対象入力digest `912e007d0a2318594def756b6f5b60953424d74b2c4b081ee026b6e0fd901a27`（基準commit `edfc4abdb626d61dcd0c5440c5a7d43a07de76a4` に未commitの変更を含む）で、`scripts/evaluate_codex.py` が実Codex CLI `0.166.0-alpha.5.1` / `gpt-6.1-sol` を実行した。Run `7b5cb487-3203-4573-9a75-2f285dd0ef7d`、Candidate `a048cf59-49a8-450c-8d50-f79cfa5aac55`。固定baseの独立golden testは4件中1件が想定どおり失敗し、実CodexのPlan・patch・別呼出レビューを経て、Linux Verifierの同4件とAC-1/AC-2のレビューがPASS、Local Gate PASS / `PUBLISH_DRAFT` に到達した。人のコード修正0、対象外関数のAST変更0、実測usage受領3件、消費推計72,728 microUSD・予約0・cost_state KNOWN。GitHubへの書込・通知は0。

同じ入力digestの全回帰1,157件PASS（839.25秒）、failure/error/skip 0。Ruff checkとformat（116ファイル）も成功。保存先は `.local/evaluation/codex-live-02/`（実モデルpatch・base/生成後JUnit・API/Artifact/usage証拠）と `.local/verification/codex-integration/final-regression/`（全回帰）。先行評価と互換修正前の記録は別ディレクトリに保持する。

これは「実Codexで変更を生成し、独立検証へ渡せる」という接続部分の動作確認である。CLIクライアント自体のLinux隔離、通信切断を含むSF-20全条件の実接続証拠、SF-21の独立watchdog/全停止保証、製品内の実CI同期、3タスクの総合評価は未受入のままとし、SF-20全体およびMVPの受入完了とはしない。

### 実Codexから実GitHub CIまでの空状態評価

対象 `962b9d3`、固定base `2e914e63c4042b81459209e2310688f0fb45d443` のEVAL-01で、実CodexのPlan・変更・独立レビュー、実Docker Verifier、実Publisher、[Draft PR #12](https://github.com/iwata-n/software-factory/pull/12)、[必須CI PASS](https://github.com/iwata-n/software-factory/actions/runs/41611793809/job/125835242794)を通した。Run `4f4e95a1-ce77-4f99-8126-2774d694c27e` は `VERIFIED_DRAFT_PR`、PR headとCI検証SHAが一致、人のコード修正0、usage受領3件、予算用推計74,456 microUSD。結果とSlack通知予約は再起動後も維持された。証拠は `.local/evaluation/codex-live-publication-01/evaluation.json`。Codex CLIは既存のホスト接続を使用した。この実行は隔離された実Agent、Slackへの配送、同一revisionでのEVAL-01〜03一括受け入れを含まない。

### 固定3評価の同一revisionでの実CodexとGitHub CI

`8e9efacfd8da6e567ab0cad56b01db5b8e106d55+inputs-9e7800e1a75f9ea0`、固定base `2e914e63c4042b81459209e2310688f0fb45d443` でEVAL-01〜03を実行した。独立Linux Runnerでbaseの想定失敗1/5/4件と互換性テストの成功を確認し、実CodexのPlan・実装・レビュー後は全3 Runが `VERIFIED_DRAFT_PR`。各PR headは必須CIの検証SHAと一致し、人によるコード修正・修正ラウンド・merge/deployはいずれも0件だった。

| 評価 | 実Draft PR | 実CI | 予算用推計（microUSD） | 所要時間（秒） |
|---|---|---|---:|---:|
| EVAL-01 空状態 | [#14](https://github.com/iwata-n/software-factory/pull/14) | [PASS](https://github.com/iwata-n/software-factory/actions/runs/41612219553/job/125836679340) | 73,580 | 131.910 |
| EVAL-02 入力検証 | [#15](https://github.com/iwata-n/software-factory/pull/15) | [PASS](https://github.com/iwata-n/software-factory/actions/runs/41612223673/job/125836692939) | 75,528 | 133.831 |
| EVAL-03 空白整形 | [#13](https://github.com/iwata-n/software-factory/pull/13) | [PASS](https://github.com/iwata-n/software-factory/actions/runs/41612218754/job/125836676511) | 74,130 | 131.595 |

証拠は `.local/evaluation/codex-three-profile-batch-01/summary.json` と各RunのTaskSpec/Policy・JUnit・利用量・Artifact・PR/CI記録。Codex CLIはホスト接続を使用し、Slackは全件通知予約までを確認した。隔離下での実Agent、実Slack配送、障害注入を含むATの全件照合は引き続き追跡する。

### SF-18〜24の並行実装の統合

7タスクの着手分を統合ブランチに集約した。DB移行はWebhook Inbox 0012、公開復旧 0013、Jira／Slack 0014の順。着手分の試験は `test_sf*_boundaries.py` と各機能の回帰試験としてCIで実行する。全条件を備えた受け入れ試験モジュールと実サービス証拠は各タスクの確定時に追加する。

統合対象はCI照合、公開UNKNOWN復旧、実Codex利用量、Linux watchdog、Jira／Slack、運用カンバン、保持と復元の着手分。実CIでの受入、Run成功の確定と通知への結線、実Codexの隔離、運用画面の操作権限、復元演習と総合受け入れは各タスクの残作業として追跡する。

### 統合候補の検証状況

2026年11月3日。統合候補 `50a0e8e` まで取り込んだ。先行版 `81de9c2` の全回帰は1,278件中1,276件が成功した。失敗した2件は旧DB移行試験で、新規保持期限列と既存列を分けて照合するように修正した。修正を含む `01c11d8` の関連回帰は210件成功。別Workspaceの管理対象Workerを実Dockerで並行実行する試験を追加し、`50a0e8e` の組み立て関連16件が成功した。候補系列の全回帰と実CIの照合を継続する。

| タスク | 確認済みの範囲 | 受け入れに必要な残作業 |
|---|---|---|
| SF-18 | 実GitHubの成功・失敗をWebhookとポーリングの両経路で確認 | 現行revisionの条件別証拠と実CI照合 |
| SF-19 | 実Draft PRの読み取り復旧と二重公開防止を確認 | 現行revisionでの復元、取消競合、成功後のPR変更を条件別に照合 |
| SF-20 | ホスト上の実Codexで3件が検証済みDraft PRまで到達 | 隔離Runnerでの実Codex、通信断、利用量証拠の照合 |
| SF-21 | 実Dockerで停止・回収・越境拒否、PostgreSQLで停止未確認時の永続隔離を確認 | 実Agentの許可経路だけを通す通信制限と、実Codexを含む停止経路 |
| SF-22 | Jira REST取り込み・Slack配送の境界試験が成功 | Jira専用欄と読み取り認証、Slack Bot認証を設定し実サービスを通しで確認 |
| SF-23 | 運用カンバン、SSE、資格情報更新、緊急停止・監査の境界を確認 | 現行候補で、状態別・権限別の表示と監査を通しで照合 |
| SF-24 | 暗号化バックアップ復元と実Codexの3件評価を確認 | 日次バックアップの実測、実Slack配送、別担当者による[復旧演習](Operations_Recovery_Runbook.md)、AT-01〜12の照合 |
