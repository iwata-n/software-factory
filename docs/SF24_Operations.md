# SF-24 保持・バックアップ・復元手順

この手順の対象は専用の PostgreSQL データベースとローカル Artifact ストレージを使う構成。バックアップにはすべての workspace のデータと資格情報ハッシュが含まれる。運用担当者の権限で実行し、Worker には DB 接続情報・バックアップ先・復号鍵を渡さない。

## 停止と日次バックアップ

1. 新規リクエストの入口を閉じ、稼働中の実行を停止する。Control Plane、Worker、Publisher、CI 受信・同期、通知処理を停止し、DB と Artifact ストレージに対する書き込みが止まったことを確認する。DB サーバーはバックアップ担当だけが接続できる状態で稼働させる。
2. ソース専用 DB の `FACTORY_DATABASE_URL` を管理用シークレットから設定する。ソースと同じメジャー版の PostgreSQL クライアントと AES-256-GCM の CMS に対応する OpenSSL 3 を用意する。必要なら `FACTORY_POSTGRES_BIN` にクライアントのディレクトリを設定する。
3. `TMPDIR` をアクセス制限付きの暗号化ローカルボリュームに設定する。平文の DB dump と一時アーカイブを格納できる空き容量を確保する。保存先はソースと別のボリュームに作り、所有者だけがアクセスできる権限（0700）にする。
4. 24時間ごとの停止枠で以下を実行する。`--offline` は停止確認をした運用担当者の宣言。証明書にはバックアップ専用公開鍵を使い、対応する秘密鍵は保存先から分離する。

```sh
umask 077
PYTHONPATH=src python scripts/factory_maintenance.py \
  --offline --artifact-root "$SOURCE_ARTIFACTS" \
  backup "$BACKUP_VOLUME" --certificate "$BACKUP_CERT"
```

出力の世代名、取得時刻、件数、暗号化ファイルの SHA-256 とサイズを運用記録に残す。成功したバックアップだけが公開され、専用保存先の最新7世代を残す。失敗時は最新成功時刻を確認し、24時間を超える前に再実行する。異常終了で残った一時ファイルは、処理の終了を確認してから運用担当者が回収する。バックアップの保存先は運用担当者だけに書き込みを許可し、復元前に運用記録の SHA-256 を照合する。

## Artifact 回収

停止中に同じソース DB と Artifact ストレージを指定する。

```sh
PYTHONPATH=src python scripts/factory_maintenance.py \
  --offline --artifact-root "$SOURCE_ARTIFACTS" collect
```

予約の有効期限に達した未確定 Artifact と、終端 Run の最後のイベントから30日経過した確定 Artifact を回収する。終端イベントの時刻を後続イベントが上回る場合は保守的に保持する。公開が未確定の Run の確定 Artifact は保持する。DB に削除マークと監査行をコミットした後でファイルを削除する。途中で失敗した場合は同じコマンドを再実行し、回収件数と監査理由を照合する。確定証拠の内容取得は期限切れコード付きの404を返す。

## 復元と再開判定

1. 障害発生時刻を記録し、旧環境への通信と実行を遮断する。新規の空 DB と存在しない Artifact ディレクトリを用意する。復元中は DB・Artifact 両方を隔離する。Artifact の親ディレクトリは暗号化ボリューム上に事前作成し、運用担当だけにアクセスを許可する。
2. `FACTORY_DATABASE_URL` を復元専用 DB に設定して以下を実行する。バックアップ台帳のハッシュに一致する、信頼済みの世代を使う。

```sh
PYTHONPATH=src python scripts/factory_maintenance.py \
  --offline --artifact-root "$RESTORED_ARTIFACTS" \
  restore "$BACKUP_FILE" --certificate "$BACKUP_CERT" --key "$BACKUP_KEY"
```

3. `STAGED_RECONCILIATION_REQUIRED` と件数を確認する。復元処理は DB と Artifact インベントリ・保存ハッシュを照合し、Worker・Publisher 資格情報と未使用の登録資格情報を無効化し、旧 Lease の期限を到達させる。失敗した場合は隔離を維持し、別の空 DB・新規ストレージを用意してやり直す。DB の復元成功後に処理が失敗すると、復元先 DB にデータが残る場合がある。
4. Worker・Publisher・通知処理の再接続を保留する。取得時刻以降に失効した User 資格情報を台帳から再適用する。個別の失効は `factory revoke --principal-id "$PRINCIPAL_ID"` を使う。旧実行が停止したことを確かめ、`factory reconcile` の結果と費用台帳を照合する。
5. バックアップ取得後に外部で起きた PR 作成・更新、CI 結果、通知を照合する。DB 側が公開前の状態でも同じ Run の外部操作を確認する。照合不能な対象は隔離を継続し、人間が判断する。CI 再同期・通知照合・再割当の製品手順は SF-19 と SF-22 の統合時に演習する。
6. 隔離した Control Plane の health、Artifact ハッシュ、費用・状態・旧資格情報拒否を確認する。復元時点から障害時刻までを RPO、復元開始から health と整合確認までを RTO として記録する。コマンドの `staging_seconds` は復元処理の所要時間。照合完了後に専用の新しい資格情報を発行し、配布先を確認して再開する。

## 検証範囲

`tests/test_factory_maintenance.py` は小規模な一時 PostgreSQL を対象に、暗号化・世代回転・誤った鍵の拒否・復元照合・旧 Worker 認証拒否・保持期限を検証する。別担当者による運用演習、指定規模での RPO/RTO、外部照合、90日保持と冪等性台帳の削除後再送は、残りの受け入れ項目として記録する。

## 実Codex評価の集計

同一バッチの3件について、成功・失敗を問わず評価出力のディレクトリを指定します。

```sh
uv run python scripts/summarize_codex_batch.py "$EVAL_01" "$EVAL_02" "$EVAL_03"
```

集計は同一リビジョン・入力digest・エンジンmanifestの3件を照合し、実CI確認済みのDraft PR数と、通知の配送記録までそろった件数を分けて出力します。再評価時は別ディレクトリを使用し、旧バッチの出力と集計を保持します。故障注入試験は条件別の受け入れ台帳で管理します。
