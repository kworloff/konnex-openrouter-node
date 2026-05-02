"""Async OpenRouter client. Mirrors the 3-candidate competition logic of the original miner,
but runs the candidates concurrently and shares one HTTP session across all wallets.

Uses httpx instead of aiohttp because bittensor's axon (uvicorn) runs request handlers in
a separate event loop from the one we set up at startup — aiohttp.ClientSession is loop-bound
and would raise "Timeout context manager should be used inside a task". httpx is loop-agnostic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import typing

import httpx

from miner.policy_io import (
    canonicalize_exit_action_id,
    normalize_user_instruction,
    structured_explain_discrete,
)
from miner.protocol import ACTION_LABELS, ALLOWED_ACTION_IDS

log = logging.getLogger("openrouter")

CANDIDATE_TEMPERATURES = (0.2, 0.8, 0.8)


def _expected_action_heuristic(instruction: str) -> int:
    t = (instruction or "").lower()
    if "stop" in t or "hold" in t or "wait" in t:
        return 0
    if "strafe left" in t:
        return 6
    if "strafe right" in t:
        return 7
    if "left" in t:
        return 2
    if "right" in t:
        return 3
    if "up" in t or "ascend" in t:
        return 4
    if "down" in t or "descend" in t:
        return 5
    if "photo" in t or "snapshot" in t:
        return 10
    return 1


def _rule_based_candidate(instruction: str, *, tag: str) -> dict[str, typing.Any]:
    ins = normalize_user_instruction(instruction)
    aid = _expected_action_heuristic(ins)
    lab = ACTION_LABELS.get(aid, f"unknown_{aid}")
    return {
        "action_id": aid,
        "label": lab,
        "confidence": 0.55,
        "explain": structured_explain_discrete(
            backend="heuristic",
            instruction=ins,
            action_id=aid,
            label_semantic=lab,
            note=tag,
        ),
    }


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        http_referer: str = "",
        x_title: str = "",
        concurrency: int = 200,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._http_referer = http_referer
        self._x_title = x_title
        self._timeout_seconds = timeout_seconds
        self._concurrency = concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "OpenRouterClient":
        limits = httpx.Limits(
            max_connections=max(self._concurrency * 2, 100),
            max_keepalive_connections=max(self._concurrency, 50),
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout_seconds),
            limits=limits,
            headers=self._headers(),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        # Lazily create the semaphore in the event loop that actually uses it
        # (axon worker loop, not the startup loop).
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._concurrency)
        return self._semaphore

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if self._http_referer:
            h["HTTP-Referer"] = self._http_referer
        if self._x_title:
            h["X-Title"] = self._x_title
        return h

    async def _call_candidate(
        self,
        *,
        instruction: str,
        synthetic_context_json: str | None,
        frame_jpeg_b64: str | None,
        temperature: float,
    ) -> dict[str, typing.Any]:
        ins = normalize_user_instruction(instruction)
        if not self._api_key or self._client is None:
            return _rule_based_candidate(ins, tag=f"temp={temperature:.1f}:no_key")
        sys_prompt = (
            "You are an OpenFly drone policy miner. Return strict JSON object only with keys: "
            "action_id (int), confidence (0..1), explain (short string). "
            f"Allowed action_id values: {list(ALLOWED_ACTION_IDS)}."
        )
        user_blob = {
            "instruction": ins,
            "synthetic_context_json": synthetic_context_json or "",
            "has_frame": bool(frame_jpeg_b64),
        }
        messages: list[dict[str, typing.Any]] = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": json.dumps(user_blob, ensure_ascii=False)},
        ]
        if frame_jpeg_b64:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Optional vision context frame."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{frame_jpeg_b64}"},
                        },
                    ],
                }
            )
        body = {
            "model": self._model,
            "temperature": float(temperature),
            "max_tokens": 220,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        sem = self._get_semaphore()
        try:
            async with sem:
                resp = await self._client.post("/chat/completions", json=body)
            if resp.status_code >= 400:
                log.warning("openrouter %s: %s", resp.status_code, resp.text[:300])
                return _rule_based_candidate(ins, tag=f"temp={temperature:.1f}:http{resp.status_code}")
            payload = resp.json()
            raw = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            obj = _parse_json_loose(raw)
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
            log.warning("openrouter candidate failed: %s: %s", type(e).__name__, e)
            return _rule_based_candidate(ins, tag=f"temp={temperature:.1f}:exc")

        try:
            aid = canonicalize_exit_action_id(int(obj.get("action_id", _expected_action_heuristic(ins))))
        except (TypeError, ValueError):
            aid = _expected_action_heuristic(ins)
        if aid not in ALLOWED_ACTION_IDS:
            aid = _expected_action_heuristic(ins)
        try:
            conf = max(0.0, min(1.0, float(obj.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        exp = str(obj.get("explain", "") or "").strip()[:500]
        lab = ACTION_LABELS.get(aid, f"unknown_{aid}")
        explain_out = exp or structured_explain_discrete(
            backend="openrouter",
            instruction=ins,
            action_id=aid,
            label_semantic=lab,
            note="model returned empty explain",
        )
        return {
            "action_id": aid,
            "label": lab,
            "confidence": conf,
            "explain": explain_out,
        }

    async def mine(
        self,
        *,
        instruction: str,
        synthetic_context_json: str | None,
        frame_jpeg_b64: str | None,
    ) -> dict[str, typing.Any]:
        tasks = [
            self._call_candidate(
                instruction=instruction,
                synthetic_context_json=synthetic_context_json,
                frame_jpeg_b64=frame_jpeg_b64,
                temperature=t,
            )
            for t in CANDIDATE_TEMPERATURES
        ]
        miners = await asyncio.gather(*tasks)
        for i, (cand, t) in enumerate(zip(miners, CANDIDATE_TEMPERATURES)):
            cand["miner_index"] = i
            cand["temperature"] = t
        winner = max(miners, key=lambda x: float(x.get("confidence", 0.0)))
        return {
            "mode": "competition",
            "winner_miner_index": int(winner["miner_index"]),
            "miners": miners,
            "action_id": int(winner["action_id"]),
            "label": str(winner["label"]),
            "confidence": float(winner.get("confidence", 0.0)),
            "explain": str(winner.get("explain", "")),
        }


def _parse_json_loose(raw: str) -> dict[str, typing.Any]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def client_from_env() -> OpenRouterClient:
    return OpenRouterClient(
        api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        model=os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip(),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        http_referer=os.environ.get("OPENROUTER_HTTP_REFERER", "").strip(),
        x_title=os.environ.get("OPENROUTER_X_TITLE", "").strip(),
        concurrency=int(os.environ.get("OPENROUTER_CONCURRENCY", "200")),
        timeout_seconds=float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "45")),
    )
