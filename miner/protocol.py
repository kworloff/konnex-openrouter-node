"""DroneNavSynapse — must match the upstream subnet contract exactly."""
from __future__ import annotations

import json
import typing

import bittensor as bt

from miner.policy_io import ACTION_LABELS_DRONE_NAV

ACTION_LABELS: dict[int, str] = dict(ACTION_LABELS_DRONE_NAV)
ALLOWED_ACTION_IDS: typing.Tuple[int, ...] = tuple(sorted(ACTION_LABELS.keys()))


class DroneNavSynapse(bt.Synapse):
    version: str = "drone-nav-v1"
    instruction: str
    task_id: str
    synthetic_context_json: typing.Optional[str] = None
    frame_jpeg_b64: typing.Optional[str] = None

    action_id: typing.Optional[int] = None
    confidence: typing.Optional[float] = None
    miner_response_json: typing.Optional[str] = None
    miner_error: typing.Optional[str] = None

    def deserialize(self) -> typing.Dict[str, typing.Any]:
        raw = self.miner_response_json
        if not raw:
            return {
                "action_id": self.action_id,
                "confidence": self.confidence,
                "error": self.miner_error,
            }
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except (TypeError, ValueError):
            pass
        return {
            "action_id": self.action_id,
            "confidence": self.confidence,
            "error": self.miner_error,
            "raw": str(raw),
        }
