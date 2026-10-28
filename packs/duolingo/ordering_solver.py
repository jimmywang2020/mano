"""多邻国「翻译组句」题的确定性 solver:XML 解析词库 + 1 次纯文字定序 + ADB 逐词点击。

设计要点(为什么绕开 VLM 视觉循环):
- 组句题的每个候选词块在 uiautomator XML 里都是「无 resource-id 的 TextView + 非空 text + 精确 bounds」,
  与带 resource-id 的进度数字/提示/检查按钮天然区分,坐标由代码算,不靠模型估。
- 词块自身 clickable=false(text 在不可点的 TextView 上),但 ADB 按坐标 tap 词块中心仍命中其上的可点父容器。
- 只调 1 次 VLM 做纯文字定序(不看图、不给坐标),点击循环由代码控制——治「漏词早交」:代码保证把
  目标词全部放进答案区、词库清空后才交还主循环去点「检查」,VLM 不会只放两三个词就提交。
- 已放入答案区的词块 cy 上移(在源句下方),仍是无 resource-id 的 TextView,靠 cy 阈值把它们排除出词库。

⚠️ 第一版,需真机迭代(EF-04):TITLE_MARKERS 变体、答案区/词库的 cy 分界、重复词(同一词出现多次)
   目前按唯一 text 记账——若一题里同词重复,第二个会被误判「已放」,待真机遇到再补。
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable

from mano.adb_executor import Adb
from mano.errors import ManoError
from mano.gui_plus_client import GuiPlus
from mano.ui_xml import normalize_text, parse_bounds


TITLE_MARKERS = ("翻译这句话", "翻译此句", "Write this in", "Translate this")
_SUBMIT_ID = "com.duolingo:id/submitButton"
_HINT_ID_SUFFIX = "hintablePrompt"
_BANK_Y_RATIO = 0.6  # cy 超过屏高 60% 视为底部词库(未使用);之上为源句/答案区,不重复点击
_MAX_WORD_LEN = 20


def _screen_height(root: ET.Element) -> int:
    height = 0
    for node in root.iter("node"):
        _, _, _, bottom = parse_bounds(node.attrib.get("bounds", ""))
        height = max(height, bottom)
    return height or 2560


def _word_chips(root: ET.Element) -> list[dict[str, Any]]:
    """候选词块:无 resource-id、单一短 text 的 TextView(排除带 id 的按钮/提示/计数)。"""
    chips: list[dict[str, Any]] = []
    for node in root.iter("node"):
        if node.attrib.get("class") != "android.widget.TextView":
            continue
        if node.attrib.get("resource-id"):  # 词块无 resource-id;有 id 的是按钮/提示/进度
            continue
        text = normalize_text(node.attrib.get("text", ""))
        if not text or len(text) > _MAX_WORD_LEN:
            continue
        left, top, right, bottom = parse_bounds(node.attrib.get("bounds", ""))
        if right <= left or bottom <= top:
            continue
        chips.append({"text": text, "cx": (left + right) // 2, "cy": (top + bottom) // 2})
    return chips


def _bank_chips(root: ET.Element) -> list[dict[str, Any]]:
    """仍在底部词库(未使用)的词块——按 cy 阈值把已放入答案区的词排除。"""
    limit = _BANK_Y_RATIO * _screen_height(root)
    return [c for c in _word_chips(root) if c["cy"] > limit]


def _source_sentence(root: ET.Element) -> str:
    for node in root.iter("node"):
        if node.attrib.get("resource-id", "").endswith(_HINT_ID_SUFFIX):
            text = normalize_text(node.attrib.get("text", ""))
            if text:
                return text
    return ""


def detect(xml_text: str) -> bool:
    """是否为「进行中」的组句题:标题在 且 有检查按钮 且 底部词库还有≥2 个词块。

    词全部放进答案区后词库变空 → 判 False,好让主循环把控制权交还 VLM 去点「检查」。
    """
    if not any(marker in xml_text for marker in TITLE_MARKERS):
        return False
    if _SUBMIT_ID not in xml_text:
        return False
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False
    return len(_bank_chips(root)) >= 2


def _parse_order(text: str) -> list[str]:
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise ManoError(f"定序结果无法解析为 JSON: {text[:300]}")
    data = json.loads(match.group(0))
    return [str(item).strip() for item in data if str(item).strip()]


def _order_words(client: GuiPlus, source: str, candidates: list[str]) -> list[str]:
    system = (
        "你是翻译组句助手。给你一个源句和一组被打乱的候选词块，"
        "请按目标译文的正确语序，输出应当依次点击的词块。"
        "只输出 JSON 字符串数组，按点击顺序排列，只包含要用到的词、原样照抄不要改写，"
        "忽略多余的干扰词。不要输出任何多余文字、解释或代码块标记。"
    )
    user = (
        f"源句: {json.dumps(source, ensure_ascii=False)}\n"
        f"候选词块: {json.dumps(candidates, ensure_ascii=False)}\n"
        "请输出点击顺序的 JSON 字符串数组。"
    )
    response = client.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=300,
    )
    return _parse_order(client.content(response))


def solve(
    adb: Adb,
    client: GuiPlus,
    log: Callable[[dict], None],
    max_rounds: int = 12,
    settle_s: float = 0.6,
) -> str:
    """dump→解析词库/源句→1 次定序→逐词点击,每点一词 re-dump 收敛,直到目标词全部放入。"""
    xml = adb.ui_dump().decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        log({"solver": "ordering", "event": "xml_parse_error", "error": str(exc)[:200]})
        return "ordering_xml_error"

    bank = _bank_chips(root)
    if not bank:
        log({"solver": "ordering", "event": "no_words"})
        return "ordering_no_words"
    source = _source_sentence(root)
    if not source:
        log({"solver": "ordering", "event": "no_source"})
        return "ordering_no_source"

    candidates = [c["text"] for c in bank]
    try:
        order = _order_words(client, source, candidates)
    except ManoError as exc:
        log({"solver": "ordering", "event": "order_error", "error": str(exc)[:300]})
        return "ordering_vlm_error"

    placed: list[str] = []
    for rnd in range(max_rounds):
        xml = adb.ui_dump().decode("utf-8", errors="replace")
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            log({"solver": "ordering", "round": rnd, "event": "xml_parse_error", "error": str(exc)[:200]})
            return "ordering_placed" if placed else "ordering_xml_error"

        by_text = {c["text"]: c for c in _bank_chips(root)}
        remaining = [w for w in order if w not in placed]
        next_word = next((w for w in remaining if w in by_text), None)
        if next_word is None:  # 目标词都不在词库了 → 已全部放入(或解析与界面不符)
            log(
                {
                    "solver": "ordering",
                    "round": rnd,
                    "source": source,
                    "order": order,
                    "placed": placed,
                    "event": "placed" if placed else "stuck",
                }
            )
            return "ordering_placed" if placed else "ordering_stuck"

        chip = by_text[next_word]
        adb.tap(chip["cx"], chip["cy"])
        placed.append(next_word)
        time.sleep(settle_s)

    log({"solver": "ordering", "source": source, "order": order, "placed": placed, "event": "max_rounds"})
    return "ordering_placed" if placed else "ordering_stuck"
