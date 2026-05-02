"""Restore N coldkey+hotkey pairs from a mnemonics file and register each on the subnet.

mnemonics file format: one mnemonic per line. Each mnemonic produces ONE coldkey AND ONE hotkey
(the same seed is used for both). Wallets are named "miner_0001", "miner_0002", ... and the
hotkey inside each wallet is named "default".

Output: writes wallets.json with [{name, hotkey, ss58, port, registered}].
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import bittensor as bt
from dotenv import load_dotenv

log = logging.getLogger("bootstrap")


def _read_mnemonics(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"mnemonics file not found: {path}")
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        words = s.split()
        if len(words) not in (12, 15, 18, 21, 24):
            log.warning("skipping line — not a 12/15/18/21/24-word mnemonic: %r", s[:40])
            continue
        out.append(" ".join(words))
    if not out:
        raise RuntimeError(f"no valid mnemonics found in {path}")
    return out


def _restore_wallet(name: str, hotkey: str, mnemonic: str, wallets_dir: Path) -> bt.wallet:
    w = bt.wallet(name=name, hotkey=hotkey, path=str(wallets_dir))
    if not w.coldkey_file.exists_on_device():
        w.regenerate_coldkey(mnemonic=mnemonic, use_password=False, overwrite=False)
    if not w.hotkey_file.exists_on_device():
        w.regenerate_hotkey(mnemonic=mnemonic, use_password=False, overwrite=False)
    return w


def _is_registered(subtensor: bt.subtensor, netuid: int, hotkey_ss58: str) -> bool:
    try:
        return bool(subtensor.is_hotkey_registered(netuid=netuid, hotkey_ss58=hotkey_ss58))
    except Exception:
        return False


def _btcli_register(name: str, hotkey: str, wallets_dir: Path, netuid: int, endpoint: str) -> bool:
    cmd = [
        "btcli", "subnet", "register",
        "--netuid", str(netuid),
        "--wallet.name", name,
        "--wallet.hotkey", hotkey,
        "--wallet.path", str(wallets_dir),
        "--subtensor.chain_endpoint", endpoint,
        "--no_prompt",
    ]
    log.info("btcli register: name=%s hotkey=%s", name, hotkey)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log.error("btcli register timed out for %s/%s", name, hotkey)
        return False
    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    if "Custom error: 6" in combined:
        # In Konnex's forked subtensor, "Custom error: 6" = registration
        # rate-limited (target_regs_per_interval reached). Need to wait for
        # the next adjustment_interval window (~72 min on default subnet config).
        log.warning(
            "btcli register: rate-limited by chain (Custom error: 6). "
            "Subnet only accepts ~1 registration per adjustment_interval (~72 min). "
            "Skipping for now — re-run bootstrap later to retry."
        )
        return False
    if "Insufficient balance" in combined or "InsufficientBalance" in combined:
        log.error("btcli register: insufficient balance on coldkey")
        return False
    if r.returncode != 0:
        log.error("btcli register failed (rc=%d): %s", r.returncode, combined[-500:])
        return False
    return True


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    netuid = int(os.environ["NETUID"])
    endpoint = os.environ.get(
        "SUBTENSOR_CHAIN_ENDPOINT", "wss://testnet-rpc1.konnex.world:39944"
    )
    mnemonics_file = Path(os.environ.get("MNEMONICS_FILE", "./mnemonics.txt")).resolve()
    wallets_dir = Path(os.environ.get("WALLETS_DIR", "./wallets")).resolve()
    manifest_path = Path(os.environ.get("WALLETS_MANIFEST", "./wallets.json")).resolve()
    port_base = int(os.environ.get("AXON_PORT_BASE", "8091"))
    register_delay = float(os.environ.get("REGISTER_DELAY_SECONDS", "12"))
    skip_if_registered = os.environ.get("REGISTER_SKIP_IF_REGISTERED", "true").lower() == "true"

    wallets_dir.mkdir(parents=True, exist_ok=True)

    mnemonics = _read_mnemonics(mnemonics_file)
    log.info("loaded %d mnemonics from %s", len(mnemonics), mnemonics_file)

    log.info("connecting to %s", endpoint)
    subtensor = bt.subtensor(network=endpoint)

    manifest: list[dict[str, object]] = []
    for i, mnemonic in enumerate(mnemonics):
        name = f"miner_{i + 1:04d}"
        hotkey = "default"
        port = port_base + i
        try:
            w = _restore_wallet(name, hotkey, mnemonic, wallets_dir)
            ss58 = w.hotkey.ss58_address
        except Exception as e:
            log.error("restore failed for %s: %s: %s", name, type(e).__name__, e)
            continue

        already = _is_registered(subtensor, netuid, ss58)
        registered = already
        if not already:
            ok = _btcli_register(name, hotkey, wallets_dir, netuid, endpoint)
            if ok:
                time.sleep(2)
                registered = _is_registered(subtensor, netuid, ss58)
                if not registered:
                    log.error(
                        "%s (%s): btcli reported success but on-chain check says not registered. "
                        "Most likely the coldkey doesn't have enough TAO for burn-fee.",
                        name, ss58,
                    )
            time.sleep(register_delay)
        elif skip_if_registered:
            log.info("already registered: %s (%s)", name, ss58)

        manifest.append(
            {
                "name": name,
                "hotkey": hotkey,
                "ss58": ss58,
                "port": port,
                "registered": registered,
            }
        )
        manifest_path.write_text(
            json.dumps({"netuid": netuid, "wallets": manifest}, indent=2),
            encoding="utf-8",
        )

    ok = sum(1 for e in manifest if e.get("registered"))
    log.info("done. registered=%d / total=%d. manifest -> %s", ok, len(manifest), manifest_path)


if __name__ == "__main__":
    main()
