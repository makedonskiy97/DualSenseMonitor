#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <version>"
  echo "Example: $0 1.0.0"
  exit 1
fi

version="$1"
tag="v${version#v}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required"
  exit 1
fi

echo "Preparing release tag: ${tag}"

git fetch --tags
if git rev-parse "${tag}" >/dev/null 2>&1; then
  echo "Tag ${tag} already exists locally."
  exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/${tag}" >/dev/null 2>&1; then
  echo "Tag ${tag} already exists on origin."
  exit 1
fi

git tag -a "${tag}" -m "Release ${tag}"
git push origin "${tag}"

echo
echo "Tag pushed: ${tag}"
echo "GitHub Actions will build Windows EXE and attach it to the Release."
