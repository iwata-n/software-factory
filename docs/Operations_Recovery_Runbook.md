# 停止・復旧演習

対象は評価専用Workspace、専用Repository、演習用の復元先です。実行担当者は開始前に対象IDと時刻、使用リビジョン、停止・復旧の理由を記録します。Workspace管理者は操作用トークンを、復元担当者はDB接続情報とバックアップ秘密鍵をそれぞれ管理します。証跡にはトークン、接続文字列、鍵を含めません。

## 演習の引き渡し

準備担当者は、実装を行っていない実行担当者に次の情報を渡します。認証情報は担当者の権限に応じて秘密ストアから取得します。

| 受け渡す情報 | 演習前に確認すること |
|---|---|
| 対象コミット、Control Plane URL、Workspace ID | 評価専用環境であり、対象を取り違えていない |
| 旧Worker ID・Lease ID・fence、Publisher ID | 失効と再発行を確認できる対象が各1件ある |
| 公開結果が不明なRun IDと対応する評価用Draft PR | PRの初期件数・head SHAを取得できる |
| Webhook未受信のCI待ちRun IDと対象チェック | ポーリングが受け取る実CI結果をGitHub側で照合できる |
| 期限切れArtifact IDと保持対象Artifact ID | 回収前は両方取得でき、期限切れ分だけが削除対象である |
| バックアップのパス・取得時刻・SHA-256と復元先 | 別の空DBと未作成のArtifactルートがあり、暗号化バックアップを復号できる |

準備担当者は上記の前提だけをそろえます。実行担当者が以下の停止・失効・復元・照合・回収・再同期を行い、コマンドやAPIの結果を記録します。

## 1. 停止と失効

1. ADMINで `GET /v1/workspaces/{id}/execution-control` を開き、現在のrevisionを取得します。同じパスへ `stopped=true`、`expected_revision`、`reason` をJSONで送ります。更新リクエストには操作ごとの `Idempotency-Key` を付け、応答が失われた場合は同じ内容とキーで再送します。
2. 停止状態で新規Claimと未開始の公開が拒否されることを確認します。Worker一覧の `lease_status` と `stop_confirmation` を記録します。通信断や `STOP_UNCONFIRMED` の場合は管理対象ホストでもコンテナを確認し、必要なら該当ホストを隔離します。
3. 演習で指定したWorkerの資格情報を、ADMINの `POST /v1/workspaces/{id}/workers/{worker_id}/revoke` で失効させます。JSONの `reason` と `Idempotency-Key` を付けます。旧資格情報が次の要求で401となることと、失効理由が監査に残ることを確認します。
4. Artifact回収や復元を始める前に、対象のWorker・PublisherとControl Planeを停止します。バックアップ先、復元先のDB、Artifactルートの取り違えを防ぐため、対象の識別子を記録します。

## 2. 復元と公開照合

1. [READMEのバックアップと復元手順](../README.md#バックアップと復元)に従い、空のDBと未作成のArtifactルートへ復元します。`STAGED_RECONCILIATION_REQUIRED`、復元開始時刻、バックアップ時刻、整合確認の結果を記録します。
2. 復元先のControl Planeを起動し、`GET /healthz` が200、Workspaceが停止中、旧WorkerとPublisherの資格情報が401となることを確認します。復元先で再発行したWorker資格情報を使う場合、旧Leaseへの報告は409となることを確認します。
3. 公開結果が不明なRunは、復元先用に再発行したPublisherで読み取り照合します。`GET /v1/publisher/reconciliations` と `GET /v1/runs/{run_id}/publication` を確認し、操作ID、既存PR、head SHA、照合前後のPR件数を記録します。照合できないRunはUNKNOWNまたはBLOCKEDの理由と必要な判断を記録します。
4. 復元時の公開照合が残る間は停止解除が409 `RESTORE_RECONCILIATION_REQUIRED` になることを確認します。確認後、解除は手順1のAPIで最新revision、`stopped=false`、解除理由を指定します。

## 3. Artifact回収とCI再同期

1. 復元先のWorker・PublisherとControl Planeを再度停止します。復元先DBと対応するArtifactルートを指定し、回収を2回実行します。

   ```sh
   uv run python scripts/factory_maintenance.py --offline \
     --artifact-root "$RESTORE_ARTIFACT_ROOT" collect
   ```

   回収件数と対象Runを記録します。期限切れの削除対象は404、稼働・承認待ち・公開結果不明の参照は取得可能であることを確認します。2回目の追加回収が0件となることを確認します。
2. Control Planeに評価Repository用の読み取りトークンとWebhook設定を読み込ませて起動します。Webhookを受けなかったCI待ちRunを対象にし、常駐ポーリングでの照合を待ちます。`GET /v1/runs/{run_id}/publication` のPRと、RunのGate・CI表示に記録された結果、対象base/head/tested SHA、照合時刻を照合します。証拠不足や期限切れの場合は、停止理由と必要な判断を記録します。

## 演習記録

各手順について、実行者、開始・終了時刻、対象、マスク済みコマンドまたはAPI、期待結果、実測結果、合否、証拠の保存先を残します。復旧時間は復元開始からhealthと整合確認がそろうまで、復旧時点は障害発生時刻と復元したバックアップ時刻の差として記録します。


| 確認項目 | 合格として記録する結果 |
|---|---|
| 停止と失効 | 停止後の新規Claimは204、旧Worker資格情報は401。停止・失効理由を監査記録で確認 |
| 復元 | ステージング完了、healthは200、旧Publisher資格情報は401、再発行後に旧Leaseを使うと409 |
| 公開照合 | 照合前の解除は409。既存PRのheadが一致し、照合前後で件数が同じ。判定不能時は理由と必要判断を記録 |
| Artifact回収 | 削除対象は404、保持対象は取得可能、2回目の追加回収は0件 |
| CI再同期 | 実GitHubの対象チェックとRunのbase・head・tested SHA・結果が一致 |
| 担当者と復旧時間 | 実装担当者と実行担当者を記録し、RPOは24時間以内、RTOは60分以内 |

失敗・未実施の項目は時刻、実測結果、理由をそのまま残します。
