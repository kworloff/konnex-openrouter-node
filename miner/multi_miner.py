"""Multi-wallet miner runner.

One process owns N hotkeys. We share a single subtensor + metagraph + OpenRouter HTTP session
across all of them. Each hotkey gets its own bt.axon bound to a distinct port (AXON_PORT_BASE+i),
all of them attach the same forward/blacklist/priority handlers — those only depend on the shared
metagraph, not on which axon received the request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import typing
from pathlib import Path

import bittensor as bt
from dotenv import load_dotenv

from miner.openrouter_client import OpenRouterClient, client_from_env
from miner.protocol import DroneNavSynapse

log = logging.getLogger("multi_miner")


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bt.logging.set_info()


class MultiWalletMiner:
    def __init__(
        self,
        *,
        netuid: int,
        chain_endpoint: str,
        wallets_dir: Path,
        manifest_path: Path,
        external_ip: str,
        port_base: int,
        metagraph_sync_seconds: int,
        client: OpenRouterClient,
        allow_non_registered: bool = False,
        force_validator_permit: bool = False,
    ) -> None:
        self.netuid = netuid
        self.chain_endpoint = chain_endpoint
        self.wallets_dir = wallets_dir
        self.manifest_path = manifest_path
        self.external_ip = external_ip
        self.port_base = port_base
        self.metagraph_sync_seconds = metagraph_sync_seconds
        self.client = client
        self.allow_non_registered = allow_non_registered
        self.force_validator_permit = force_validator_permit

        self.subtensor: bt.subtensor | None = None
        self.metagraph: bt.metagraph | None = None
        self.axons: list[bt.axon] = []
        self._stop = asyncio.Event()

    def _load_manifest(self) -> list[dict[str, typing.Any]]:
        if not self.manifest_path.exists():
            raise FileNotFoundError(
                f"wallets manifest not found at {self.manifest_path}. "
                "Run scripts/bootstrap_wallets.py first."
            )
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        wallets = data.get("wallets") or []
        if not wallets:
            raise RuntimeError("manifest has no wallets")
        return wallets

    async def _sync_metagraph_loop(self) -> None:
        while not self._stop.is_set():
            try:
                assert self.metagraph is not None and self.subtensor is not None
                self.metagraph.sync(subtensor=self.subtensor)
                log.info("metagraph synced (n=%d)", len(self.metagraph.hotkeys))
            except Exception as e:
                log.warning("metagraph sync failed: %s: %s", type(e).__name__, e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.metagraph_sync_seconds)
            except asyncio.TimeoutError:
                pass

    async def _forward(self, synapse: DroneNavSynapse) -> DroneNavSynapse:
        instruction = str(synapse.instruction or "").strip()
        if not instruction:
            synapse.miner_error = "empty instruction"
            synapse.action_id = None
            synapse.confidence = 0.0
            synapse.miner_response_json = json.dumps(
                {"ok": False, "error": "empty instruction"}, ensure_ascii=False
            )
            return synapse
        pack = await self.client.mine(
            instruction=instruction,
            synthetic_context_json=synapse.synthetic_context_json,
            frame_jpeg_b64=synapse.frame_jpeg_b64,
        )
        synapse.action_id = int(pack["action_id"])
        synapse.confidence = float(pack["confidence"])
        synapse.miner_error = None
        synapse.miner_response_json = json.dumps(pack, ensure_ascii=False)
        log.info(
            "DRONE_MINER task=%s action_id=%s conf=%.2f",
            synapse.task_id,
            synapse.action_id,
            float(synapse.confidence or 0.0),
        )
        return synapse

    async def _blacklist(self, synapse: DroneNavSynapse) -> tuple[bool, str]:
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            return True, "Missing dendrite or hotkey"
        assert self.metagraph is not None
        hotkeys = self.metagraph.hotkeys
        hk = synapse.dendrite.hotkey
        if hk not in hotkeys:
            if not self.allow_non_registered:
                return True, "Unrecognized hotkey"
            return False, "Unregistered allowed"
        uid = hotkeys.index(hk)
        if self.force_validator_permit and not bool(self.metagraph.validator_permit[uid]):
            return True, "Non-validator hotkey"
        return False, "Hotkey recognized"

    async def _priority(self, synapse: DroneNavSynapse) -> float:
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            return 0.0
        assert self.metagraph is not None
        hk = synapse.dendrite.hotkey
        if hk not in self.metagraph.hotkeys:
            return 0.0
        uid = self.metagraph.hotkeys.index(hk)
        try:
            return float(self.metagraph.S[uid])
        except Exception:
            return 0.0

    def _build_axon(self, wallet: bt.wallet, port: int) -> bt.axon:
        axon = bt.axon(wallet=wallet, port=port, ip=self.external_ip or None)
        axon.attach(
            forward_fn=self._forward,
            blacklist_fn=self._blacklist,
            priority_fn=self._priority,
        )
        return axon

    async def run(self) -> None:
        manifest = self._load_manifest()
        log.info("loading %d wallets from manifest", len(manifest))

        self.subtensor = bt.subtensor(network=self.chain_endpoint)
        self.metagraph = self.subtensor.metagraph(self.netuid)
        log.info(
            "connected to %s, netuid=%d, metagraph hotkeys=%d",
            self.chain_endpoint,
            self.netuid,
            len(self.metagraph.hotkeys),
        )

        registered_hotkeys = set(self.metagraph.hotkeys)
        served = 0
        for entry in manifest:
            name = entry["name"]
            hotkey_name = entry["hotkey"]
            port = int(entry.get("port") or (self.port_base + served))
            try:
                wallet = bt.wallet(
                    name=name, hotkey=hotkey_name, path=str(self.wallets_dir)
                )
                hk_ss58 = wallet.hotkey.ss58_address
            except Exception as e:
                log.error("wallet load failed name=%s hotkey=%s: %s", name, hotkey_name, e)
                continue
            if hk_ss58 not in registered_hotkeys:
                log.warning("hotkey %s not registered on netuid %d — skipping", hk_ss58, self.netuid)
                continue
            try:
                axon = self._build_axon(wallet, port)
                axon.serve(netuid=self.netuid, subtensor=self.subtensor)
                axon.start()
                self.axons.append(axon)
                served += 1
                if served % 50 == 0:
                    log.info("served %d axons so far", served)
            except Exception as e:
                log.error("axon start failed for %s on port %d: %s", hk_ss58, port, e)

        if not self.axons:
            raise RuntimeError("no axons started — check registration and ports")
        log.info("all axons up: %d serving from port %d", len(self.axons), self.port_base)

        sync_task = asyncio.create_task(self._sync_metagraph_loop())
        await self._stop.wait()
        sync_task.cancel()
        for ax in self.axons:
            try:
                ax.stop()
            except Exception:
                pass

    def request_stop(self) -> None:
        self._stop.set()


async def _amain() -> None:
    load_dotenv()
    _setup_logging()

    netuid = int(os.environ["NETUID"])
    chain_endpoint = os.environ.get(
        "SUBTENSOR_CHAIN_ENDPOINT", "wss://testnet-rpc1.konnex.world:39944"
    )
    wallets_dir = Path(os.environ.get("WALLETS_DIR", "./wallets")).resolve()
    manifest_path = Path(os.environ.get("WALLETS_MANIFEST", "./wallets.json")).resolve()
    external_ip = os.environ.get("EXTERNAL_IP", "").strip()
    port_base = int(os.environ.get("AXON_PORT_BASE", "8091"))
    sync_seconds = int(os.environ.get("METAGRAPH_SYNC_SECONDS", "120"))

    async with client_from_env() as client:
        miner = MultiWalletMiner(
            netuid=netuid,
            chain_endpoint=chain_endpoint,
            wallets_dir=wallets_dir,
            manifest_path=manifest_path,
            external_ip=external_ip,
            port_base=port_base,
            metagraph_sync_seconds=sync_seconds,
            client=client,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, miner.request_stop)
            except NotImplementedError:
                pass
        await miner.run()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
