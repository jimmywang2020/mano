"""Parse VLM ``mobile_use`` tool calls and map normalized coordinates to pixels."""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import ManoError


TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)

# Generic completion phrases (EN + zh) used only to recover a missing terminate
# status from the model's narrative. Keep these app-agnostic.
_FAILURE_PATTERNS = ("terminate failure", "mark as failure", "task failed", "标记为失败", "任务失败", "无法完成任务")
_SUCCESS_PATTERNS = ("terminate success", "mark as success", "task complete", "标记为成功", "任务已完成", "符合任务要求")


def parse_tool_call(text: str) -> dict[str, Any]:
    match = TOOL_CALL_RE.search(text)
    if not match:
        raise ManoError(f"no tool_call in response: {text[:1000]}")
    try:
        call = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ManoError(f"invalid tool_call JSON: {match.group(1)}") from exc
    if call.get("name") != "mobile_use" or not isinstance(call.get("arguments"), dict):
        raise ManoError(f"malformed tool_call: {call}")
    action = call["arguments"]
    if action.get("action") == "terminate" and action.get("status") not in {"success", "failure"}:
        narrative = text[: match.start()].lower()
        if any(pattern.lower() in narrative for pattern in _FAILURE_PATTERNS):
            action["status"] = "failure"
            action["status_inferred_from_text"] = True
        elif any(pattern.lower() in narrative for pattern in _SUCCESS_PATTERNS):
            action["status"] = "success"
            action["status_inferred_from_text"] = True
    return action


def int_coord(value: Any, width: int, height: int) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ManoError(f"bad coordinate: {value!r}")
    norm_x, norm_y = float(value[0]), float(value[1])
    if not (0 <= norm_x <= 1000 and 0 <= norm_y <= 1000):
        raise ManoError(f"normalized coordinate out of range: {(norm_x, norm_y)}")
    x = min(width - 1, max(0, round(norm_x / 1000 * width)))
    y = min(height - 1, max(0, round(norm_y / 1000 * height)))
    return x, y


def mapped_action(action: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    mapped = dict(action)
    for key in ("coordinate", "coordinate2"):
        if key in mapped:
            mapped[key] = list(int_coord(mapped[key], width, height))
    return mapped


def action_signature(
    action: dict[str, Any], width: int, height: int
) -> tuple[Any, ...] | None:
    kind = action.get("action")
    if kind == "click":
        return (kind, *int_coord(action.get("coordinate"), width, height))
    if kind == "swipe":
        return (
            kind,
            *int_coord(action.get("coordinate"), width, height),
            *int_coord(action.get("coordinate2"), width, height),
        )
    if kind == "type":
        return (kind, action.get("text"))
    return None


# --- Single source of truth for the action surface ---------------------------
# Every caller (runner resolve/verify validation, execute_action, Adb.type_text /
# Adb.keyevent) reuses these so the whitelist can never drift into two copies.

ALLOWED_ACTIONS = frozenset(
    {"click", "swipe", "type", "key", "system_button", "wait", "terminate"}
)

# type input is routed through `adb shell input text`; keep it ASCII/safe-shell.
TYPE_TEXT_RE = re.compile(r"[A-Za-z0-9 +._-]{1,80}")

# key / system_button both map to a keyevent; value -> Android keycode.
KEYEVENT_CODES = {"BACK": "4", "HOME": "3", "ENTER": "66", "CLEAR": "28"}

# Coordinate fields (verify may re-resolve these on the latest page) and the
# non-coordinate execution side-effect fields the executor actually consumes.
_COORD_FIELDS: dict[str, tuple[str, ...]] = {
    "click": ("coordinate",),
    "swipe": ("coordinate", "coordinate2"),
}
_EXEC_FIELDS: dict[str, tuple[str, ...]] = {
    "type": ("text",),
    "key": ("text",),
    "system_button": ("button",),
    "wait": ("time",),
    "terminate": ("status",),
}

# Known non-execution diagnostic fields, allowed only on the stated action; they
# never enter the execution payload / anti-swap and verify need not echo them.
_ALLOWED_META: dict[str, frozenset[str]] = {
    "terminate": frozenset({"status_inferred_from_text"}),
}

# Device-modifying actions that carry a cross-step semantic identity (used by the
# anti-loop guards). wait and terminate are excluded on purpose.
_SEMANTIC_LOOP_ACTIONS = frozenset({"click", "swipe", "type", "key", "system_button"})


def _coord_ok(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 2:
        return False
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return False
    return 0.0 <= x <= 1000.0 and 0.0 <= y <= 1000.0


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def validate_action(action: Any) -> str | None:
    """Reject untrusted Pack output before it reaches signatures / approve / verify
    / the device. Returns a stable error code (for the journal / BLOCK reason) or
    ``None`` when the action is a safe, executable instruction.

    Checks: dict shape; action in the allowed set; no unknown execution fields
    (a known diagnostic meta field is permitted only on its action); click/swipe
    coordinates present and within 0-1000; type/key/system_button payload fields
    present and whitelisted; terminate status in {success, failure}.
    """
    if not isinstance(action, dict):
        return "not_a_dict"
    kind = action.get("action")
    if kind not in ALLOWED_ACTIONS:
        return "unknown_action"

    allowed = {"action"}
    allowed.update(_COORD_FIELDS.get(kind, ()))
    allowed.update(_EXEC_FIELDS.get(kind, ()))
    allowed.update(_ALLOWED_META.get(kind, ()))
    for field_name in action:
        if field_name not in allowed:
            return "unknown_field"

    if "status_inferred_from_text" in action and not isinstance(
        action["status_inferred_from_text"], bool
    ):
        return "bad_meta_field"

    for coord_field in _COORD_FIELDS.get(kind, ()):
        if not _coord_ok(action.get(coord_field)):
            return "bad_" + coord_field

    if kind == "type":
        text = action.get("text")
        if not isinstance(text, str) or TYPE_TEXT_RE.fullmatch(text) is None:
            return "bad_text"
    elif kind == "key":
        text = action.get("text")
        if not isinstance(text, str) or text.upper() not in KEYEVENT_CODES:
            return "bad_key"
    elif kind == "system_button":
        button = action.get("button")
        if not isinstance(button, str) or button.upper() not in KEYEVENT_CODES:
            return "bad_button"
    elif kind == "wait":
        if "time" in action and not _is_number(action["time"]):
            return "bad_time"
    elif kind == "terminate":
        if action.get("status") not in {"success", "failure"}:
            return "bad_status"
    return None


def execution_payload(action: dict[str, Any]) -> dict[str, Any]:
    """The fields the executor actually uses and that verify must NOT change:
    the action type plus its non-coordinate side-effect fields. Coordinates and
    metadata are excluded, so verify is free to re-resolve the coordinate only."""
    kind = action.get("action")
    payload: dict[str, Any] = {"action": kind}
    for field_name in _EXEC_FIELDS.get(kind, ()):
        if field_name in action:
            payload[field_name] = action[field_name]
    return payload


def semantic_signature(
    semantic_target: str, action: dict[str, Any]
) -> tuple[Any, ...] | None:
    """Cross-step identity of a device-modifying action: ``semantic_target`` +
    action type + non-coordinate side-effect fields. Coordinates are deliberately
    excluded, so a target whose pixel position drifts a little is still recognized
    as the same semantic action by the repeat / oscillation guards. Returns None
    for non-device actions (wait / terminate), which never enter the loop history.
    """
    if action.get("action") not in _SEMANTIC_LOOP_ACTIONS:
        return None
    return (semantic_target, *sorted(execution_payload(action).items()))
