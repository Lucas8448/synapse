#!/usr/bin/env bash
# up.sh — start the synapse gateway container with secrets pulled from Proton Pass
# at deploy time. There are deliberately NO secrets in this file and NO .env on disk.
#
# Prereqs (see README → "Deploy (VPS)"):
#   * Docker, plus the shared external network:  docker network create cortex
#   * Proton Pass CLI (`pass-cli`) installed and logged in:  pass-cli login
#
# The two keys are exported as Proton Pass secret *references* (pass:// URIs), never
# as plaintext. `pass-cli run` resolves them from the `env` vault and injects the
# real values into the environment of `docker compose`, which the compose file
# requires via ${VAR:?run ./up.sh}. Values stay masked in output.
set -euo pipefail

cd "$(dirname "$0")"

command -v pass-cli >/dev/null 2>&1 || {
  echo "up.sh: 'pass-cli' not found — install the Proton Pass CLI first:" >&2
  echo "       https://protonpass.github.io/pass-cli/get-started/installation/" >&2
  exit 1
}

# vault: env   items: OPENROUTER / LITELLM   field: "API Key"
export OPENROUTER_API_KEY='pass://env/OPENROUTER/API Key'
export LITELLM_MASTER_KEY='pass://env/LITELLM/API Key'

pass-cli run -- docker compose up -d "$@"

echo "up.sh: gateway up. Verify with:  curl localhost:4000/health/liveliness" >&2
