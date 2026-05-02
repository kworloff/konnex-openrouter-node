#!/usr/bin/env bash
# Konnex multi-wallet miner installer (OpenRouter backend).
#
# Usage on a fresh Ubuntu 22.04+ VPS (as root or sudo user):
#   curl -fsSL https://raw.githubusercontent.com/kworloff/konnex-openrouter-node/main/konnex.miner.sh -o konnex.miner.sh \
#     && chmod +x konnex.miner.sh && ./konnex.miner.sh
#
# Env overrides (all optional — script will prompt if missing):
#   KONNEX_REPO_URL          git clone URL (default: this repo)
#   KONNEX_BRANCH            git branch (default: main)
#   KONNEX_INSTALL_DIR       install dir (default: $HOME/konnex-miner)
#   OPENROUTER_API_KEY       OpenRouter key (sk-or-...)
#   OPENROUTER_MODEL         default openai/gpt-4o-mini
#   NETUID                   subnet id
#   SUBTENSOR_CHAIN_ENDPOINT default wss://testnet-rpc1.konnex.world:39944
#   EXTERNAL_IP              public IP of this VPS (auto-detected if blank)
#   AXON_PORT_BASE           default 8091
#   MNEMONICS_PATH           path to a file with 1 mnemonic per line (will be copied in)
#   AUTO_BOOTSTRAP           "yes" to run wallet bootstrap immediately
#   AUTO_START               "yes" to start the miner under systemd at the end

set -euo pipefail

print_banner() {
  printf '\n\033[1;36m'
  cat <<'BANNER'
╔════════════════════════════════════╗
║                                    ║
║    __ __                           ║
║   / //_/___  ____  ____  ___  _  __║
║  / ,< / __ \/ __ \/ __ \/ _ \| |/_/║
║ / /| / /_/ / / / / / / /  __/>  <  ║
║/_/ |_\____/_/ /_/_/ /_/\___/_/|_|  ║
║                                    ║
╚════════════════════════════════════╝
                              by 0xKwo
BANNER
  printf '\033[0m\n'
}
print_banner

# --- defaults ---
: "${KONNEX_REPO_URL:=https://github.com/kworloff/konnex-openrouter-node.git}"
: "${KONNEX_BRANCH:=main}"
: "${KONNEX_INSTALL_DIR:=$HOME/konnex-miner}"
: "${OPENROUTER_MODEL:=openai/gpt-4o-mini}"
: "${SUBTENSOR_CHAIN_ENDPOINT:=wss://testnet-rpc1.konnex.world:39944}"
: "${AXON_PORT_BASE:=8091}"
: "${AUTO_BOOTSTRAP:=ask}"
: "${AUTO_START:=ask}"

SUDO=""
if [[ "${EUID}" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: run as root or install sudo." >&2
    exit 1
  fi
  SUDO="sudo"
fi

log() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
err() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; }
ask() {
  # ask "Prompt" "default"  -> echoes user input (or default)
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " reply </dev/tty || true
  else
    read -r -p "$prompt: " reply </dev/tty || true
  fi
  echo "${reply:-$default}"
}

# --- 1. system deps ---
log "Installing system packages"
$SUDO apt-get update -y
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3.11 python3.11-venv python3.11-dev \
  build-essential pkg-config libssl-dev curl git ca-certificates jq ufw

# --- 2. clone or update repo ---
log "Cloning $KONNEX_REPO_URL ($KONNEX_BRANCH) into $KONNEX_INSTALL_DIR"
if [[ -d "$KONNEX_INSTALL_DIR/.git" ]]; then
  git -C "$KONNEX_INSTALL_DIR" fetch --all --quiet
  git -C "$KONNEX_INSTALL_DIR" checkout "$KONNEX_BRANCH" --quiet
  git -C "$KONNEX_INSTALL_DIR" pull --ff-only --quiet
else
  mkdir -p "$(dirname "$KONNEX_INSTALL_DIR")"
  git clone --branch "$KONNEX_BRANCH" --depth 1 "$KONNEX_REPO_URL" "$KONNEX_INSTALL_DIR"
fi
cd "$KONNEX_INSTALL_DIR"

# --- 3. python venv ---
log "Creating venv and installing requirements"
python3.11 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip wheel >/dev/null
pip install -r requirements.txt

# --- 4. interactive config (only ask for things not in env) ---
log "Configuration"

if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  OPENROUTER_API_KEY="$(ask "OpenRouter API key (sk-or-...)" "")"
fi
if [[ -z "$OPENROUTER_API_KEY" ]]; then
  err "OPENROUTER_API_KEY is required"; exit 1
fi

if [[ -z "${NETUID:-}" ]]; then
  NETUID="$(ask "NETUID" "4")"
fi

if [[ -z "${EXTERNAL_IP:-}" ]]; then
  detected="$(curl -fsS https://api.ipify.org || true)"
  EXTERNAL_IP="$(ask "Public IP of this VPS" "$detected")"
fi
if [[ -z "$EXTERNAL_IP" ]]; then
  err "EXTERNAL_IP is required"; exit 1
fi

# --- 5. mnemonics ---
log "Mnemonics (one per line — each line = 1 wallet)"
if [[ -n "${MNEMONICS_PATH:-}" && -f "$MNEMONICS_PATH" ]]; then
  cp "$MNEMONICS_PATH" "$KONNEX_INSTALL_DIR/mnemonics.txt"
  chmod 600 "$KONNEX_INSTALL_DIR/mnemonics.txt"
  echo "Copied $MNEMONICS_PATH -> mnemonics.txt"
elif [[ -f "$KONNEX_INSTALL_DIR/mnemonics.txt" ]]; then
  echo "Using existing mnemonics.txt"
else
  echo "mnemonics.txt is missing."
  echo "Upload it now (e.g. via scp) to: $KONNEX_INSTALL_DIR/mnemonics.txt"
  echo "Or set MNEMONICS_PATH=/path/to/file and rerun this script."
  reply="$(ask "Continue without mnemonics? bootstrap will be skipped" "no")"
  if [[ "$reply" != "yes" ]]; then exit 1; fi
fi

WALLET_COUNT=0
if [[ -f "$KONNEX_INSTALL_DIR/mnemonics.txt" ]]; then
  WALLET_COUNT="$(grep -cvE '^\s*(#|$)' "$KONNEX_INSTALL_DIR/mnemonics.txt" || true)"
fi
echo "Wallet count: $WALLET_COUNT"

# --- 6. .env ---
log "Writing .env"
ENV_FILE="$KONNEX_INSTALL_DIR/.env"
cat > "$ENV_FILE" <<EOF
NETUID=$NETUID
SUBTENSOR_CHAIN_ENDPOINT=$SUBTENSOR_CHAIN_ENDPOINT

OPENROUTER_API_KEY=$OPENROUTER_API_KEY
OPENROUTER_MODEL=$OPENROUTER_MODEL
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_HTTP_REFERER=https://konnex.world
OPENROUTER_X_TITLE=konnex-miner
OPENROUTER_CONCURRENCY=200
OPENROUTER_TIMEOUT_SECONDS=45

WALLETS_DIR=$KONNEX_INSTALL_DIR/wallets
MNEMONICS_FILE=$KONNEX_INSTALL_DIR/mnemonics.txt
WALLETS_MANIFEST=$KONNEX_INSTALL_DIR/wallets.json
EXTERNAL_IP=$EXTERNAL_IP
AXON_PORT_BASE=$AXON_PORT_BASE

REGISTER_DELAY_SECONDS=12
REGISTER_SKIP_IF_REGISTERED=true

METAGRAPH_SYNC_SECONDS=120
LOG_LEVEL=INFO
EOF
chmod 600 "$ENV_FILE"
echo "Wrote $ENV_FILE"

# --- 7. firewall ---
if [[ "$WALLET_COUNT" -gt 0 ]]; then
  END=$((AXON_PORT_BASE + WALLET_COUNT - 1))
  log "Opening TCP ports $AXON_PORT_BASE-$END via ufw"
  $SUDO ufw allow "${AXON_PORT_BASE}:${END}/tcp" >/dev/null || true
  $SUDO ufw allow OpenSSH >/dev/null || true
  yes | $SUDO ufw enable >/dev/null 2>&1 || true
fi

# --- 8. file descriptor limit ---
log "Raising file descriptor limit"
if ! grep -q "konnex-miner-nofile" /etc/security/limits.d/*.conf 2>/dev/null; then
  echo "* soft nofile 65535 # konnex-miner-nofile" | $SUDO tee /etc/security/limits.d/konnex.conf >/dev/null
  echo "* hard nofile 65535 # konnex-miner-nofile" | $SUDO tee -a /etc/security/limits.d/konnex.conf >/dev/null
fi

# --- 9. systemd unit ---
log "Installing systemd service konnex-miner.service"
SERVICE_USER="${SUDO_USER:-$USER}"
UNIT_PATH=/etc/systemd/system/konnex-miner.service
$SUDO tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Konnex multi-wallet miner (OpenRouter)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$KONNEX_INSTALL_DIR
EnvironmentFile=$KONNEX_INSTALL_DIR/.env
ExecStart=$KONNEX_INSTALL_DIR/.venv/bin/python -m miner.multi_miner
Restart=on-failure
RestartSec=10
LimitNOFILE=65535
StandardOutput=append:$KONNEX_INSTALL_DIR/miner.log
StandardError=append:$KONNEX_INSTALL_DIR/miner.log

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload

# --- 10. bootstrap wallets (registration) ---
RUN_BOOTSTRAP="$AUTO_BOOTSTRAP"
if [[ "$RUN_BOOTSTRAP" == "ask" ]]; then
  if [[ "$WALLET_COUNT" -gt 0 ]]; then
    RUN_BOOTSTRAP="$(ask "Run wallet bootstrap (restore + register $WALLET_COUNT hotkeys) now? (yes/no)" "no")"
  else
    RUN_BOOTSTRAP="no"
  fi
fi
if [[ "$RUN_BOOTSTRAP" == "yes" ]]; then
  log "Bootstrapping wallets — this can take hours and costs TAO per registration"
  set -a; # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  python -m scripts.bootstrap_wallets
fi

# --- 11. start miner ---
RUN_START="$AUTO_START"
if [[ "$RUN_START" == "ask" ]]; then
  RUN_START="$(ask "Enable and start konnex-miner.service now? (yes/no)" "yes")"
fi
if [[ "$RUN_START" == "yes" ]]; then
  log "Enabling and starting konnex-miner.service"
  $SUDO systemctl enable --now konnex-miner.service
  sleep 2
  $SUDO systemctl --no-pager --full status konnex-miner.service || true
fi

cat <<EOF

============================================================
Konnex miner installed at: $KONNEX_INSTALL_DIR

Logs:    tail -f $KONNEX_INSTALL_DIR/miner.log
Status:  systemctl status konnex-miner
Stop:    systemctl stop konnex-miner
Start:   systemctl start konnex-miner
Manual bootstrap (re-run wallet registration):
         cd $KONNEX_INSTALL_DIR && ./run.sh bootstrap
============================================================
EOF
