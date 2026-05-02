#!/usr/bin/env bash
# Konnex multi-wallet miner installer for Ubuntu 22.04+ VPS.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
  echo "Run as root or install sudo." >&2
  exit 1
fi
SUDO=""
if [[ "${EUID}" -ne 0 ]]; then SUDO="sudo"; fi

echo "[1/5] apt deps"
$SUDO apt-get update -y
$SUDO apt-get install -y --no-install-recommends \
  python3.11 python3.11-venv python3.11-dev \
  build-essential pkg-config libssl-dev curl git ca-certificates

echo "[2/5] venv"
cd "$(dirname "$0")"
python3.11 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[3/5] pip"
pip install --upgrade pip wheel
pip install -r requirements.txt
# Resolve scalecodec <-> cyscale namespace conflict in newer bittensor stacks
pip uninstall -y scalecodec cyscale >/dev/null 2>&1 || true
pip install --force-reinstall --quiet cyscale

echo "[4/5] .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env — edit it (OPENROUTER_API_KEY, EXTERNAL_IP, NETUID, ...) before running."
fi

echo "[5/5] firewall hint"
PORT_BASE="$(grep -E '^AXON_PORT_BASE=' .env | cut -d= -f2 | tr -d '\r' || echo 8091)"
N=$(wc -l < mnemonics.txt 2>/dev/null || echo 0)
if [[ "$N" -gt 0 ]]; then
  END=$((PORT_BASE + N - 1))
  echo "Open TCP ports ${PORT_BASE}-${END} on the VPS firewall before bootstrap."
else
  echo "Once mnemonics.txt has N lines, open TCP ${PORT_BASE}..${PORT_BASE}+N-1."
fi

echo
echo "Done. Next:"
echo "  1) put 1000 mnemonics into mnemonics.txt (one per line)"
echo "  2) edit .env (OPENROUTER_API_KEY, EXTERNAL_IP, NETUID)"
echo "  3) ./run.sh bootstrap   # restores + registers all wallets"
echo "  4) ./run.sh miner       # starts the multi-wallet miner"
