"""Mirror of subnet's openfly_policy_io — kept verbatim so JSON shape matches the validator."""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name, str(default)) or str(default)).strip())
    except ValueError:
        return default


USER_INSTRUCTION_MAX_CHARS = max(256, _env_int("OPENFLY_INSTRUCTION_MAX_CHARS", 8000))

ACTION_LABELS_DRONE_NAV: dict[int, str] = {
    0: "exit",
    1: "forward",
    2: "turn_left_30",
    3: "turn_right_30",
    4: "up",
    5: "down",
    6: "strafe_left",
    7: "strafe_right",
    8: "forward_fast",
    9: "forward_faster",
    10: "make_photo",
    12: "send_to_user",
}


def canonicalize_exit_action_id(aid: int) -> int:
    return 0 if int(aid) == 13 else int(aid)


def normalize_user_instruction(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    return s[:USER_INSTRUCTION_MAX_CHARS]


def action_label_semantic(aid: int) -> str:
    c = canonicalize_exit_action_id(int(aid))
    return ACTION_LABELS_DRONE_NAV.get(c, f"unknown_id_{int(aid)}")


def structured_explain_discrete(
    *,
    backend: str,
    instruction: str,
    action_id: int,
    label_semantic: str,
    note: str | None = None,
) -> str:
    ins = normalize_user_instruction(instruction)
    mi = 400
    u = ins[:mi] + ("…" if len(ins) > mi else "")
    b = (backend or "model").strip().lower()
    tail = f"; note: {note}" if note else ""
    return f"OpenFly-{b} discrete: action_id={int(action_id)} ({label_semantic}); mission: {u}{tail}"
