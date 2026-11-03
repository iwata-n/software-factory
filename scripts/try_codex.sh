#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
FACTORY_POSTGRES_BIN=${FACTORY_POSTGRES_BIN:-/private/tmp/software-factory-pg17/pgsql/bin}
if [ ! -x "$FACTORY_POSTGRES_BIN/postgres" ]; then
    echo "FACTORY_POSTGRES_BIN に PostgreSQL 17 の bin ディレクトリを指定してください。" >&2
    exit 1
fi
FACTORY_RUNNER_IMAGE_DIGEST=$(docker image inspect software-factory-runner:dev --format '{{.Id}}')
export FACTORY_POSTGRES_BIN FACTORY_RUNNER_IMAGE_DIGEST

mkdir -p .local/evaluation
run_dir=$(mktemp -d .local/evaluation/codex-demo-XXXXXXXX)
output="$run_dir/result"
printf '結果の保存先: %s/%s\n' "$PWD" "$output"
uv run --locked python scripts/with_postgres.py python scripts/evaluate_codex.py --output "$output" "$@"
printf '評価結果: %s/%s/evaluation.json\n生成コード: %s/%s/generated-app.py\n' "$PWD" "$output" "$PWD" "$output"
