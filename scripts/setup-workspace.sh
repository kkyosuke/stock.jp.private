#!/bin/sh

set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
private_root=$(git -C "$script_directory/.." rev-parse --show-toplevel)
public_root="$private_root/stock.jp"
private_data="$private_root/operations/private"
public_link="$public_root/operations/private"
expected_target='../../operations/private'

if ! git -C "$public_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$private_root" submodule update --init --recursive
fi

if [ ! -d "$public_root/operations" ]; then
  echo "public operations directory is missing: $public_root/operations" >&2
  exit 1
fi

mkdir -p "$private_data"

if [ -L "$public_link" ]; then
  actual_target=$(readlink "$public_link")
  if [ "$actual_target" != "$expected_target" ]; then
    echo "unexpected symlink target: $public_link -> $actual_target" >&2
    exit 1
  fi
elif [ -e "$public_link" ]; then
  echo "refusing to replace existing path: $public_link" >&2
  exit 1
else
  ln -s "$expected_target" "$public_link"
fi

find "$private_data" -type d -exec chmod 700 {} +
find "$private_data" -type f -exec chmod 600 {} +

echo "workspace ready"
echo "private data: $private_data"
echo "public link: $public_link -> $expected_target"
