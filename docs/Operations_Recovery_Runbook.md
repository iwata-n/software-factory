# 停止・復旧演習

対象は評価専用Workspace、専用Repository、演習用の復元先です。実行担当者は開始前に対象IDと時刻、使用リビジョン、停止・復旧の理由を記録します。Workspace管理者は操作用トークンを、復元担当者はDB接続情報とバックアップ秘密鍵をそれぞれ管理します。証跡にはトークン、接続文字列、鍵を含めません。

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
