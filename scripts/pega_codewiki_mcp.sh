#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
env_file=${CODEWIKI_PEGA_ENV_FILE:-"$repo_root/.env.local"}
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

if [[ -n "${CODEWIKI_BIN:-}" ]]; then
  codewiki_bin=$CODEWIKI_BIN
else
  runner_cli="$repo_root/../codewiki_test/codewiki-runner/.venv/bin/codewiki"
  if [[ -x "$runner_cli" ]]; then
    codewiki_bin=$runner_cli
  else
    codewiki_bin=$(command -v codewiki || true)
  fi
fi
if [[ -z "$codewiki_bin" || ! -x "$codewiki_bin" ]]; then
  printf 'Set CODEWIKI_BIN to the CodeWiki executable for the fork.\n' >&2
  exit 2
fi

export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
cd "$repo_root"
exec "$codewiki_bin" mcp
