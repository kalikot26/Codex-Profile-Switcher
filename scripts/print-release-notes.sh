#!/usr/bin/env bash
set -euo pipefail

version="${1:-}"
out_dir="${2:-dist}"

if [[ -z "${version}" ]]; then
  version=$(python3 - <<'PY'
import json
with open("package.json", "r", encoding="utf-8") as fh:
    print(json.load(fh)["version"])
PY
  )
fi

version="${version#v}"

last_tag=""
if git rev-parse --git-dir >/dev/null 2>&1; then
  last_tag=$(git describe --tags --abbrev=0 2>/dev/null || true)
fi

echo "Release v${version}"
echo ""
echo "Changes"
if [[ -n "${last_tag}" ]]; then
  git log --oneline "${last_tag}..HEAD"
else
  git log --oneline
fi
echo ""
echo "Artifacts"
if [[ -d "${out_dir}/release" ]]; then
  ls -1 "${out_dir}/release"
fi
if [[ -d "${out_dir}/npm-packages" ]]; then
  ls -1 "${out_dir}/npm-packages"
fi
if [[ -d "${out_dir}/cargo" ]]; then
  ls -1 "${out_dir}/cargo"
fi
if [[ -d "${out_dir}/homebrew" ]]; then
  ls -1 "${out_dir}/homebrew"
fi
if [[ -d "${out_dir}/checksums" ]]; then
  ls -1 "${out_dir}/checksums"
fi
