#!/bin/sh

set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <public-repository> <current-commit> <target-commit>" >&2
  exit 2
fi

public_root=$1
current_public_sha=$2
target_public_sha=$3

if ! git -C "$public_root" merge-base --is-ancestor \
  "$current_public_sha" "$target_public_sha"; then
  echo "public main is not a fast-forward from the pinned submodule" >&2
  exit 1
fi

git -C "$public_root" diff --name-only \
  "$current_public_sha" "$target_public_sha" |
  while IFS= read -r changed_path; do
    case "$changed_path" in
      data/daily-prices/latest.json | data/daily-prices/*/*.csv)
        ;;
      *)
        echo "manual review required for public change: $changed_path" >&2
        exit 1
        ;;
    esac
  done
