#!/usr/bin/env bash
set -euo pipefail

missing=0

check_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required tool: $1" >&2
    missing=1
  fi
}

check_cmd git
check_cmd cargo
check_cmd node
check_cmd npm
check_cmd zip
check_cmd tar

if [[ "${missing}" -ne 0 ]]; then
  exit 1
fi
