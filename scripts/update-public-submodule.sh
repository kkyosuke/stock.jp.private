#!/bin/sh

set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
private_root=$(git -C "$script_directory/.." rev-parse --show-toplevel)
public_root="$private_root/stock.jp"
bootstrap_output=$(mktemp)
trap 'rm -f "$bootstrap_output"' EXIT HUP INT TERM
if command -v python3 >/dev/null 2>&1; then
  python_executable=python3
else
  python_executable=python
fi

if [ ! -e "$public_root/.git" ]; then
  git -C "$private_root" submodule update --init --recursive -- stock.jp
fi
"$private_root/scripts/setup-workspace.sh"

current_public_sha=$(git -C "$public_root" rev-parse HEAD)
git -C "$public_root" fetch origin main
target_public_sha=$(git -C "$public_root" rev-parse origin/main)
"$private_root/scripts/validate-public-auto-update.sh" \
  "$public_root" "$current_public_sha" "$target_public_sha"

git -C "$public_root" switch --detach "$target_public_sha"

public_sha=$(git -C "$public_root" rev-parse HEAD)
remote_main=$(git -C "$public_root" ls-remote origin refs/heads/main | awk '{print $1}')
if [ "$public_sha" != "$remote_main" ]; then
  echo "public submodule does not match origin/main" >&2
  exit 1
fi

"$python_executable" "$public_root/scripts/operation_state.py" validate
if "$python_executable" "$public_root/scripts/operation_bootstrap.py" check >"$bootstrap_output"; then
  :
else
  bootstrap_status=$?
  cat "$bootstrap_output"
  echo "public submodule readiness check failed (exit $bootstrap_status)" >&2
  exit "$bootstrap_status"
fi
cat "$bootstrap_output"
"$python_executable" - "$bootstrap_output" <<'PY'
import json
from pathlib import Path
import sys

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if result.get("paper_go") is not True:
    raise SystemExit("PAPER readiness failed after the public submodule update")
if result.get("broker_submission") != "HUMAN_ONLY":
    raise SystemExit("broker submission boundary changed from HUMAN_ONLY")
PY

if ! git -C "$private_root" diff --quiet -- operations/private || [ -n "$(
  git -C "$private_root" ls-files --others --exclude-standard -- operations/private
)" ]; then
  echo "validation unexpectedly changed private operation data" >&2
  exit 1
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'public_sha=%s\n' "$public_sha" >>"$GITHUB_OUTPUT"
fi
printf 'public submodule validated: %s\n' "$public_sha"
