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
