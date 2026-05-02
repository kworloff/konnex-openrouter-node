# Konnex multi-wallet miner (OpenRouter)

A miner for the `knx-subnet-drone-navigation` subnet. Uses **OpenRouter** as the
inference backend and supports running **up to 1000 wallets** from a single
process on one VPS.

## Architecture

- One Python process, sharing a single `subtensor` + `metagraph` + `aiohttp`
  session across all wallets.
- For every hotkey a dedicated `bt.axon` is bound to its own port
  (`AXON_PORT_BASE`, `AXON_PORT_BASE+1`, …).
- All axons share the same `forward / blacklist / priority` handlers (they only
  depend on the shared metagraph, not on which axon received the request).
- For every validator request the miner runs **3 candidates** against
  OpenRouter (temperatures 0.2 / 0.8 / 0.8) **in parallel** and returns the one
  with the highest `confidence` — matching the original miner's competition
  logic.
- If OpenRouter fails or returns invalid JSON, the miner falls back to a
  keyword heuristic.

## One-liner install (Ubuntu 22.04+ VPS)

```bash
curl -fsSL https://raw.githubusercontent.com/kworloff/konnex-openrouter-node/main/konnex.miner.sh \
  -o konnex.miner.sh && chmod +x konnex.miner.sh && ./konnex.miner.sh
```

The installer will:

1. Install system packages (`python3.11`, `git`, `build-essential`, `ufw`, …).
2. Clone this repo into `~/konnex-miner/`.
3. Create a `.venv` and install `requirements.txt`.
4. Prompt interactively for `OPENROUTER_API_KEY`, `NETUID`, `EXTERNAL_IP`
   (auto-detected via ipify).
5. Copy in your `mnemonics.txt` (or wait for you to upload it).
6. Generate `.env` (`chmod 600`).
7. Open TCP ports `AXON_PORT_BASE … AXON_PORT_BASE+N-1` via `ufw`.
8. Raise file descriptor limit to 65535.
9. Install a **systemd unit** `konnex-miner.service` with auto-restart and logs
   in `~/konnex-miner/miner.log`.
10. Optionally: run wallet bootstrap and start the service.

## Non-interactive install

```bash
OPENROUTER_API_KEY=sk-or-v1-xxx \
NETUID=4 \
EXTERNAL_IP=1.2.3.4 \
MNEMONICS_PATH=/root/mnemonics.txt \
AUTO_BOOTSTRAP=yes \
AUTO_START=yes \
bash <(curl -fsSL https://raw.githubusercontent.com/kworloff/konnex-openrouter-node/main/konnex.miner.sh)
```

## Manual deployment

```bash
git clone https://github.com/kworloff/konnex-openrouter-node.git
cd konnex-openrouter-node
chmod +x install.sh run.sh
./install.sh
```

Then:

1. Put 1000 mnemonics into `mnemonics.txt` (one per line).
2. Edit `.env`:
   - `OPENROUTER_API_KEY` — your OpenRouter key
   - `EXTERNAL_IP` — public IP of this VPS
   - `NETUID`, `SUBTENSOR_CHAIN_ENDPOINT` — subnet parameters
   - `AXON_PORT_BASE` — first axon port (default `8091`)
3. Open TCP ports `AXON_PORT_BASE … AXON_PORT_BASE+N-1` on the VPS firewall.
4. Restore wallets and register them on the subnet:
   ```bash
   ./run.sh bootstrap
   ```
   The script reads `mnemonics.txt`, regenerates a coldkey + hotkey from each
   seed, registers the hotkey via `btcli subnet register`, sleeps
   `REGISTER_DELAY_SECONDS` between registrations, and incrementally writes a
   `wallets.json` manifest.
5. Start the miner:
   ```bash
   ./run.sh miner
   ```

## Service management

```bash
systemctl status konnex-miner          # status
systemctl restart konnex-miner         # restart
systemctl stop konnex-miner            # stop
tail -f ~/konnex-miner/miner.log       # live logs
journalctl -u konnex-miner -f          # alternative log view
```

The service is configured with `Restart=on-failure` and `LimitNOFILE=65535`,
and is independent of your SSH session.

## System requirements

- Ubuntu 22.04+, Python 3.11.
- ~1.5–2 GB RAM for the process plus ~3–5 MB per axon (1000 hotkeys ≈ 4–6 GB
  total). 8+ GB RAM VPS recommended for 1000 wallets.
- One open TCP port per hotkey.
- `ulimit -n 65535` (handled by the installer via `/etc/security/limits.d/`).

## OpenRouter configuration

| Env var | Default | Notes |
|---|---|---|
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | Any OpenRouter slug |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `OPENROUTER_CONCURRENCY` | `200` | Global semaphore across all wallets |
| `OPENROUTER_TIMEOUT_SECONDS` | `45` | Per HTTP request |
| `OPENROUTER_HTTP_REFERER` | `https://konnex.world` | OpenRouter ranking header |
| `OPENROUTER_X_TITLE` | `konnex-miner` | OpenRouter ranking header |

## Registration cost

Registering 1000 hotkeys on the subnet requires TAO (burn registration fee).
The bootstrap script registers sequentially with a `REGISTER_DELAY_SECONDS`
pause (default 12s) between registrations to avoid chain rate limits and
ImmunityPeriod conflicts. Plan your TAO budget in advance.

## Project layout

```
miner/
  protocol.py          # DroneNavSynapse (subnet contract — byte-for-byte upstream)
  policy_io.py         # action labels + helpers from openfly_policy_io
  openrouter_client.py # async OpenRouter client, 3-candidate competition, fallback
  multi_miner.py       # runs N axons in a single process
scripts/
  bootstrap_wallets.py # restore + register all hotkeys from mnemonics
install.sh             # local Ubuntu installer
konnex.miner.sh        # one-liner installer (curl-able)
run.sh                 # ./run.sh bootstrap | miner
.env.example
mnemonics.example.txt
```
