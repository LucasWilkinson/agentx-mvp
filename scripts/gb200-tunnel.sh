#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=${ENV_FILE:-.env.gb200}
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
: "${GB200_BASTION:?set GB200_BASTION in the ignored $ENV_FILE}"
: "${GB200_SSH_USER:?set GB200_SSH_USER in the ignored $ENV_FILE}"
: "${GB200_SSH_KEY:?set GB200_SSH_KEY in the ignored $ENV_FILE}"

readonly listen_host=127.0.0.1
readonly listen_port=1080

if [[ ! -f "$GB200_SSH_KEY" ]]; then
  echo "ERROR: GB200 SSH key not found: $GB200_SSH_KEY" >&2
  exit 2
fi

if lsof -nP -iTCP:"$listen_port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: TCP port $listen_port already has a listener; refusing to replace it." >&2
  lsof -nP -iTCP:"$listen_port" -sTCP:LISTEN >&2
  exit 2
fi

echo "Opening the GB200 SOCKS tunnel on ${listen_host}:${listen_port}."
echo "Active kube context remains: $(kubectl config current-context 2>/dev/null || echo '<unavailable>')"
echo "Leave this command running; use another terminal for: just gb200-status"

exec ssh -D "${listen_host}:${listen_port}" -N \
  -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -i "$GB200_SSH_KEY" \
  "${GB200_SSH_USER}@${GB200_BASTION}"
