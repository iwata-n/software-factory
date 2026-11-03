# SF-21 独立 watchdog の初期実装

`software_factory.sandbox_watchdog` は、信頼された Worker が別プロセスとして起動する
Docker 用の監督プロセス。初期スコープは固定イメージ上のオフライン実行。
既存の Assembler / Verifier と Codex Adapter への結線は統合時に行う。

## Worker / SF-20 との境界

- Worker が同じホストの単調時計で確定した Lease と Job の絶対期限、許可済みのイメージ
  content ID、信頼された実行コマンドを渡す。期限が早い方の 10 秒前までに停止指示を送るため、
  Docker RPC の送信に 100 ms の余裕を取る。Lease の延長はこの版では受け付けない。
- stdin は専用制御パイプ。任意の 1 バイトまたは EOF が取消を表す。Worker 側の通信断では
  このパイプを閉じる。Worker 終了時にも EOF が届くよう、書き込み FD を子へ継承しない。
  workload の入力や資格情報をこのパイプで渡さない。
- Docker デーモンに TERM と 5 秒後の KILL を委ね、停止指示から 7 秒以内にコンテナの
  `exited` と PID 0 を確認する。停止確認は private PID namespace の終了を前提とする。
  stdout は監督側の JSON Lines。終了コード 0 は正常終了、1 は拒否・停止・workload 失敗、
  2 は監督基盤エラー。Worker は停止が確認できない場合に失敗として扱い、隔離する。
  終了コード 0 を Gate PASS や managed 証拠に直接変換しない。
- SF-20 側では、heartbeat 更新とキャンセルの配線、構造化入出力と usage の回収、
  認証情報の受け渡し、固定モデル経路の制御を接続する。Docker の操作と隔離設定は監督側の
  責務とし、Agent の入力から起動引数や制約を組み立てない。

## 実測範囲

`tests/test_sandbox_watchdog.py` はデータベース不要の Docker 統合プローブ。runner の
content ID を `SF21_TEST_IMAGE` に設定し、専用の `--basetemp` と JUnit 出力先を指定する。
テストは一意な名前のコンテナを使い、ホスト側ポートを公開しない。
プロセス消滅の検査に限り、試験用観測コンテナが Linux ホスト PID namespace を参照する。

コンテナ内の権限・mount・外部ルート、同時実行時の作業領域分離、CPU throttling、
OOM、fork と tmpfs 上限、Worker 強制終了を含む停止経路を観測する。
Docker のイベントと単調時刻 trace を保存し、各コンテナの破棄と対象 PID の消滅を確認する。

モデル許可経路と redirect/DNS 迂回、Artifact/cache の Workspace 別管理、アップロード失敗と
資格情報の廃棄、Worker 隔離の永続化、managed 証拠の採用 Gate は統合後の検証対象。
watchdog 自体やデーモンが停止した場合の再起動時回収と、
起動応答消失時の異常系試験も残る。ここでのプローブは SF-21 全 AC の合否を更新しない。
