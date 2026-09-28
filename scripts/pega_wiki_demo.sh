#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
env_file="$repo_root/.env.local"
if [[ ! -f "$env_file" ]]; then
  printf 'Create %s from .env.local.example and fill its values.\n' "$env_file" >&2
  exit 2
fi

set -a
source "$env_file"
set +a
for variable in CUSTOMER_MODEL_API_KEY CUSTOMER_MODEL_BASE_URL CUSTOMER_MODEL_ID PEGA_PROJECT_ID PEGA_SNAPSHOT_DIR; do
  if [[ -z "${!variable:-}" ]]; then
    printf '%s is missing from %s.\n' "$variable" "$env_file" >&2
    exit 2
  fi
done

codewiki_bin=${CODEWIKI_BIN:-"$repo_root/../codewiki_test/codewiki-runner/.venv/bin/codewiki"}
if [[ ! -x "$codewiki_bin" ]]; then
  printf 'CodeWiki CLI is missing; set CODEWIKI_BIN in %s.\n' "$env_file" >&2
  exit 2
fi

cd "$repo_root"
output_dir=${1:-"runs/pega-wiki-$(date -u +%Y%m%dT%H%M%SZ)"}
export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
export MERMAID_VALIDATE=0

arguments=(
  pega-generate
  --project "$PEGA_PROJECT_ID"
  --snapshot-dir "$PEGA_SNAPSHOT_DIR"
  --model "$CUSTOMER_MODEL_ID"
  --model-base-url "$CUSTOMER_MODEL_BASE_URL"
  --api-key-env CUSTOMER_MODEL_API_KEY
  --output "$output_dir"
)
if [[ -n "${PEGA_PLAN_FILE:-}" ]]; then
  arguments+=(--plan-file "$PEGA_PLAN_FILE")
fi

exec "$codewiki_bin" "${arguments[@]}"
