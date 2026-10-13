"""Adapter wrapping the existing Duolingo prompts + guards into the mano Pack
protocol. No solver here: the thin runner v0 drives a pure VLM single-step loop,
so matching / ordering exercises fall back to the visual loop. The deterministic
solvers stay in the baseline run.py until a per-step next_action adapter lands.
"""

from __future__ import annotations

import json

from packs.duolingo import guards, prompts


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
