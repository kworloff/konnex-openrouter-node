#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .venv/bin/activate

cmd="${1:-miner}"
case "$cmd" in
  bootstrap)
    exec python -m scripts.bootstrap_wallets
    ;;
  miner)
    exec python -m miner.multi_miner
    ;;
  *)
    echo "usage: $0 {bootstrap|miner}" >&2
    exit 2
    ;;
esac
