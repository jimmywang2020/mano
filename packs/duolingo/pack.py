"""Adapter wrapping the existing Duolingo prompts + guards into the mano Pack
protocol. No solver here: the thin runner v0 drives a pure VLM single-step loop,
so matching / ordering exercises fall back to the visual loop. The deterministic
solvers stay in the baseline run.py until a per-step next_action adapter lands.

``resolve`` is a *migration-only* passthrough: it hands the model action back
unchanged with a ``legacy:`` semantic target derived deterministically from the
action's side-effect fields. It does NOT interpret Duolingo controls and there is
NO ``verify``, so the runner uses the ``legacy_raw_xml_fallback`` path. A
production pack that needs action-relevant validation must implement genuine
resolve + verify and must not rely on this fallback.
"""

from __future__ import annotations

import json

from mano.pack import ALLOW, ResolveDecision
from packs.duolingo import guards, prompts


def _legacy_semantic_target(action: dict) -> str:
    """Deterministic, non-random identity for the migration fallback. Not a real
    semantic target -- just a stable signature of the action's side-effect fields."""
    parts = [str(action.get("action", "?"))]
    for key in ("text", "button", "status"):
        if key in action:
            parts.append(f"{key}={action[key]}")
    for key in ("coordinate", "coordinate2"):
        coord = action.get(key)
        if isinstance(coord, list) and len(coord) == 2:
            parts.append(f"{key}={coord[0]},{coord[1]}")
    return "legacy:" + "|".join(parts)


class DuolingoPack:
    package = "com.duolingo"

    def system_prompt(self, width: int, height: int) -> str:
        return prompts.duolingo_system_prompt(width, height)

    def instruction(self, history: list[dict]) -> str:
        return prompts.duolingo_instruction(json.dumps(history[-8:], ensure_ascii=False))

    def guard(self, xml: str, foreground: str | None) -> str | None:
        if foreground and foreground != self.package:
            return "left_app"
        if guards.paywall_detected(xml):
            return "paywall_handed_to_human"
        if guards.energy_depleted(xml):
            return "energy_depleted"
        if guards.bonus_offer_detected(xml):
            return "lesson_done"
        return None

    def resolve(self, *, proposed_action: dict, observation, model_text: str) -> ResolveDecision:
        # Migration passthrough: no real resolution, no verify (legacy fallback).
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target=_legacy_semantic_target(proposed_action),
            resolved_action=dict(proposed_action),
            reason="",
            metadata={"mode": "legacy_passthrough"},
        )
