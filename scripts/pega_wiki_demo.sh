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
for variable in CUSTOMER_MODEL_API_KEY CUSTOMER_MODEL_BASE_URL CUSTOMER_MODEL_ID PEGA_PROJECT_ID PEGA_KB_ROOT PEGA_PYTHON; do
  if [[ -z "${!variable:-}" ]]; then
    printf '%s is missing from %s.\n' "$variable" "$env_file" >&2
    exit 2
  fi
done
PEGA_RELEASE=${PEGA_RELEASE:-lead}

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
  --release "$PEGA_RELEASE"
  --mcp-command "$PEGA_PYTHON"
  --mcp-arg=-m
  --mcp-arg=pega_kb.mcp_server
  --mcp-cwd "$PEGA_KB_ROOT"
  --model "$CUSTOMER_MODEL_ID"
  --model-base-url "$CUSTOMER_MODEL_BASE_URL"
  --api-key-env CUSTOMER_MODEL_API_KEY
  --output "$output_dir"
)
if [[ -n "${PEGA_SEED_ID:-}" ]]; then
  arguments+=(--seed-id "$PEGA_SEED_ID")
elif [[ -n "${PEGA_SEED_NAME:-}" ]]; then
  arguments+=(--seed-name "$PEGA_SEED_NAME")
fi
if [[ -n "${PEGA_DEPTH:-}" ]]; then
  arguments+=(--depth "$PEGA_DEPTH")
fi
if [[ -n "${PEGA_CACHE_DIR:-}" ]]; then
  arguments+=(--cache-dir "$PEGA_CACHE_DIR")
fi
if [[ -n "${PEGA_PLAN_FILE:-}" ]]; then
  arguments+=(--plan-file "$PEGA_PLAN_FILE")
fi

exec "$codewiki_bin" "${arguments[@]}"
