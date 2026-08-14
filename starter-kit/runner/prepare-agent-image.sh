#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

[[ $# -eq 1 ]] || fail "prepare-agent-image.sh requires one Agent directory" 2
AGENT_DIR="$(cd "$1" && pwd)"
REQUIREMENTS="$AGENT_DIR/requirements.lock"

if ! grep -Ev '^[[:space:]]*(#|$)' "$REQUIREMENTS" >/dev/null; then
  printf '%s\n' "$BB_AGENT_IMAGE"
  exit 0
fi

hash="$(
  {
    printf '%s\n' "$BB_AGENT_IMAGE"
    cat "$REQUIREMENTS"
  } | sha256sum | awk '{print $1}'
)"
runtime_image="buildbench-managed-agent:${hash:0:16}"

if ! docker image inspect "$runtime_image" >/dev/null 2>&1; then
  info "Building managed Agent runtime $runtime_image" >&2
  docker build \
    --pull=false \
    --tag "$runtime_image" \
    --file - \
    "$AGENT_DIR" >&2 <<EOF
FROM $BB_AGENT_IMAGE
COPY requirements.lock /tmp/buildbench-requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/buildbench-requirements.lock \
    && rm -f /tmp/buildbench-requirements.lock
EOF
fi

printf '%s\n' "$runtime_image"
