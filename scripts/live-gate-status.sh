#!/bin/sh

set -u

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
private_root=$(git -C "$script_directory/.." rev-parse --show-toplevel)
python="$private_root/stock.jp/.venv/bin/python"
validator="$private_root/stock.jp/scripts/live_gate_evidence.py"

if [ ! -x "$python" ]; then
  echo "Python environment is missing: $python" >&2
  exit 1
fi

if [ ! -f "$validator" ]; then
  echo "LIVE gate validator is missing: $validator" >&2
  exit 1
fi

overall=0

run_gate() {
  label=$1
  shift
  printf '\n[%s]\n' "$label"
  if ! "$python" "$validator" --root "$private_root/stock.jp" "$@"; then
    overall=1
  fi
}

run_gate point-in-time point-in-time
run_gate historical-replay historical-replay
run_gate paper-duration paper-duration
run_gate shadow-run shadow-run
run_gate official-coverage official-coverage
run_gate repository-recovery repository-recovery
run_gate personal-risk personal-risk
run_gate v04-promotion v04-promotion
run_gate final-live-promotion promote-live

exit "$overall"
