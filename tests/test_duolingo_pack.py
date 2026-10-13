"""Offline checks for the DuolingoPack adapter: guard mapping + protocol shape.
No device, no VLM. Guard result strings must match the baseline run.py results.
"""

from __future__ import annotations

from mano.pack import Pack
from packs.duolingo.pack import DuolingoPack


def test_is_a_pack():
    assert isinstance(DuolingoPack(), Pack)
    assert DuolingoPack().package == "com.duolingo"


def test_guard_left_app():
    pack = DuolingoPack()
    assert pack.guard("<hierarchy/>", "com.android.launcher") == "left_app"


def test_guard_same_package_is_none():
    pack = DuolingoPack()
    assert pack.guard("<hierarchy/>", "com.duolingo") is None


def test_guard_paywall():
    pack = DuolingoPack()
    xml = '<node text="免费试用"/>'
    assert pack.guard(xml, "com.duolingo") == "paywall_handed_to_human"


def test_guard_energy_depleted():
    pack = DuolingoPack()
    xml = '<node text="能量用完了"/>'
    assert pack.guard(xml, "com.duolingo") == "energy_depleted"


def test_guard_lesson_done():
    pack = DuolingoPack()
    xml = '<node text="开始新的挑战"/>'
    assert pack.guard(xml, "com.duolingo") == "lesson_done"


def test_guard_clean_screen_is_none():
    pack = DuolingoPack()
    assert pack.guard('<node text="继续"/>', "com.duolingo") is None


def test_instruction_truncates_history_and_is_str():
    pack = DuolingoPack()
    history = [{"action": "click", "coordinate": [i, i]} for i in range(20)]
    text = pack.instruction(history)
    assert isinstance(text, str)
    # only the last 8 history entries are serialized into the instruction
    assert '"coordinate": [12, 12]' in text
    assert '"coordinate": [11, 11]' not in text


def test_system_prompt_mentions_resolution():
    pack = DuolingoPack()
    text = pack.system_prompt(1080, 1920)
    assert isinstance(text, str)
    assert "1080x1920" in text
