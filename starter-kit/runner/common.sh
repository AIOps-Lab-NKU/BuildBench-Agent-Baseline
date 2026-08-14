#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BB_AGENT_IMAGE="${BB_AGENT_IMAGE:-python:3.11.9-slim-bookworm}"
BB_VALIDATOR_IMAGE="${BB_VALIDATOR_IMAGE:-buildbench-validator-runtime:v0}"
BB_EXAMPLE_ASSETS_IMAGE="${BB_EXAMPLE_ASSETS_IMAGE:-buildbench-example-assets:v0}"
BB_CLEANUP_IMAGE="${BB_CLEANUP_IMAGE:-ubuntu:24.04}"

export BB_AGENT_IMAGE
export BB_VALIDATOR_IMAGE
export BB_EXAMPLE_ASSETS_IMAGE
export BB_CLEANUP_IMAGE

ok() {
  printf '✓ %s\n' "$1"
}

info() {
  printf '  %s\n' "$1"
}

fail() {
  printf '✗ %s\n' "$1" >&2
  exit "${2:-1}"
}

have_command() {
  command -v "$1" >/dev/null 2>&1
}

ensure_image() {
  local image="$1"
  if docker image inspect "$image" >/dev/null 2>&1; then
    return 0
  fi
  info "Pulling $image"
  docker pull "$image" >/dev/null
}

run_managed_python() {
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e PYTHONDONTWRITEBYTECODE=1 \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    -v "$ROOT:$ROOT" \
    -w "$ROOT" \
    "$BB_AGENT_IMAGE" \
    python "$@"
}

run_managed_python_with_mount() {
  local mounted_path="$1"
  shift
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e PYTHONDONTWRITEBYTECODE=1 \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    -v "$ROOT:$ROOT" \
    -v "$mounted_path:$mounted_path:ro" \
    -w "$ROOT" \
    "$BB_AGENT_IMAGE" \
    python "$@"
}

json_field() {
  local json_file="$1"
  local field="$2"
  run_managed_python "$ROOT/runner/read_json.py" "$json_file" "$field"
}

archive_demo_run() {
  local demo_dir="$ROOT/runs/demo"
  if [[ ! -e "$demo_dir" ]]; then
    return 0
  fi
  local archive_root="$ROOT/runs/archive"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$archive_root"
  mv "$demo_dir" "$archive_root/demo-$stamp"
}
