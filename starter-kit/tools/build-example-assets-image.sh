#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_CASE="${1:-}"
IMAGE="${BB_EXAMPLE_ASSETS_IMAGE:-buildbench-example-assets:v0}"
BASE_IMAGE="${BB_EXAMPLE_ASSETS_BASE_IMAGE:-ubuntu:24.04}"

if [[ -z "$BASE_CASE" || ! -d "$BASE_CASE" ]]; then
  printf 'usage: %s /absolute/path/to/self-contained-x86_64-base-case\n' "$0" >&2
  exit 2
fi
BASE_CASE="$(realpath "$BASE_CASE")"
[[ -f "$BASE_CASE/config/buildconfig" ]] || {
  printf 'base Case is missing config/buildconfig\n' >&2
  exit 2
}
[[ -d "$BASE_CASE/dependencies/rpms" ]] || {
  printf 'base Case is missing dependencies/rpms\n' >&2
  exit 2
}

CONTEXT="$ROOT/.assets-build"
[[ "$CONTEXT" == "$ROOT/.assets-build" ]] || {
  printf 'refusing to clean an unexpected asset context\n' >&2
  exit 2
}
rm -rf "$CONTEXT"
trap 'rm -rf "$CONTEXT"' EXIT
mkdir -p \
  "$CONTEXT/hello/input" \
  "$CONTEXT/hello/config" \
  "$CONTEXT/hello/dependencies"

cp "$BASE_CASE/config/buildconfig" "$CONTEXT/hello/config/buildconfig"
cp -a "$BASE_CASE/dependencies/rpms" "$CONTEXT/hello/dependencies/rpms"

cat >"$CONTEXT/hello/input/buildbench-hello.spec" <<'EOF'
Name:           buildbench-hello
Version:        1.0
Release:        1
Summary:        Minimal package for the Build-Bench Starter Kit
License:        MIT
BuildArch:      noarch

%description
A minimal package used by the trusted Build-Bench one-command demo.

%prep

%build
# BUILD-BENCH-DEMO-BROKEN
echo "intentional Build-Bench demo failure" >&2
exit 1

%install
mkdir -p %{buildroot}%{_datadir}/buildbench-hello
echo "Hello from Build-Bench" > %{buildroot}%{_datadir}/buildbench-hello/hello.txt

%files
%dir %{_datadir}/buildbench-hello
%{_datadir}/buildbench-hello/hello.txt
EOF

cat >"$CONTEXT/hello/manifest.json" <<'EOF'
{
  "schema_version": "1.0",
  "case_id": "hello-demo",
  "package_type": "rpm",
  "build": {
    "recipe": "input/buildbench-hello.spec",
    "buildconfig": "config/buildconfig",
    "dependency_dirs": ["dependencies/rpms"],
    "architecture": "x86_64",
    "timeout_seconds": 600,
    "jobs": 1,
    "vm_type": "docker:privileged",
    "backend": "obs-rpm"
  },
  "patch_policy": {
    "allowed_paths": ["input/**"],
    "forbidden_paths": ["manifest.json", "config/**", "dependencies/**"]
  },
  "expected_artifacts": {
    "binary": ["buildbench-hello-*.noarch.rpm"],
    "source": ["buildbench-hello-*.src.rpm"]
  },
  "security_mode": "privileged"
}
EOF

cat >"$CONTEXT/Dockerfile" <<EOF
FROM $BASE_IMAGE
COPY hello /opt/buildbench/example-cases/hello
LABEL org.opencontainers.image.title="Build-Bench Example Assets"
LABEL org.opencontainers.image.version="0.1.0"
EOF

docker build -t "$IMAGE" "$CONTEXT"
printf 'Built %s\n' "$IMAGE"
