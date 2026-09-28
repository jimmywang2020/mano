"""多邻国「选择配对」题的确定性 solver:XML 解析定位词块 + 1 次纯文字配对判断 + ADB 直接点击。

设计要点(为什么绕开 VLM 视觉循环):
- 配对题的每个词块在 uiautomator XML 里都带 text + 精确像素 bounds;坐标由代码算,不靠模型估。
- 已配对的词块会失去 clickable(或从树上消失),用 clickable 过滤天然识别"还剩哪些"。
- 只调 1 次 VLM 做纯文字配对(不看图、不给坐标),点击循环由代码控制,状态记在代码里,消除 VLM 跨步震荡。
- 对 XML 结构鲁棒:text 在节点自身还是子节点都能取(node_texts 聚合后代),matched 是否失活都能靠代码记账 + 重复 dump 收敛。
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
from mano.ui_xml import node_texts, parse_bounds


TITLE_MARKERS = ("选择配对", "Tap the matching", "点击配对")
_FULL_WIDTH_RATIO = 0.6  # 超过屏宽 60% 视为整行按钮(检查/继续),不当配对词块
_TOP_MARGIN_RATIO = 0.12  # 顶部状态栏/进度条区域(XP 计数等),不当配对词块
_BOTTOM_MARGIN_RATIO = 0.92  # 底部横幅/导航区域,不当配对词块


def _screen_size(root: ET.Element) -> tuple[int, int]:
    width = height = 0
    for node in root.iter("node"):
        _, _, right, bottom = parse_bounds(node.attrib.get("bounds", ""))
        width = max(width, right)
        height = max(height, bottom)
    return width or 1440, height or 2560


def _word_cells(root: ET.Element, width: int, height: int) -> list[dict[str, Any]]:
    """可点击、单一短文本、非整行按钮、在答题区内的候选词块。"""
    width_limit = _FULL_WIDTH_RATIO * width
    top_limit = _TOP_MARGIN_RATIO * height
    bottom_limit = _BOTTOM_MARGIN_RATIO * height
    cells: list[dict[str, Any]] = []
    for node in root.iter("node"):
        if node.attrib.get("clickable") != "true" or node.attrib.get("enabled") != "true":
            continue
        texts = node_texts(node)
        if len(texts) != 1:
            continue
        word = texts[0]
        if not word or len(word) > 40:
            continue
        left, top, right, bottom = parse_bounds(node.attrib.get("bounds", ""))
        if right <= left or bottom <= top or (right - left) > width_limit:
            continue
        cy = (top + bottom) // 2
        if cy < top_limit or cy > bottom_limit:
            continue
        cells.append(
            {
                "text": word,
                "cx": (left + right) // 2,
                "cy": cy,
            }
        )
    return cells


def _split_columns(cells: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    if not cells:
        return [], []
    xs = [c["cx"] for c in cells]
    mid = (min(xs) + max(xs)) / 2
    left = sorted((c for c in cells if c["cx"] < mid), key=lambda c: c["cy"])
    right = sorted((c for c in cells if c["cx"] >= mid), key=lambda c: c["cy"])
    return left, right


def detect(xml_text: str) -> bool:
    """是否为「进行中」的配对题:标题在 且 两列各有≥3 个短词块且明显分列。

    必须同时满足标题与词块:配对完成后标题仍在但词块已变灰消失,此时须判 False,
    好让主循环把控制权交还 VLM 去点绿色「继续」(否则会在 solver 空转卡死)。
    """
    if not any(marker in xml_text for marker in TITLE_MARKERS):
        return False
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False
    width, height = _screen_size(root)
    left, right = _split_columns(_word_cells(root, width, height))
    if len(left) < 3 or len(right) < 3 or abs(len(left) - len(right)) > 1:
        return False
    max_left = max(c["cx"] for c in left)
    min_right = min(c["cx"] for c in right)
    return (min_right - max_left) > 0.15 * width


def _parse_pairs(text: str) -> list[tuple[str, str]]:
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise ManoError(f"配对判断无法解析为 JSON: {text[:300]}")
    data = json.loads(match.group(0))
    pairs: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        a = str(item.get("a", "")).strip()
        b = str(item.get("b", "")).strip()
        if a and b:
            pairs.append((a, b))
    return pairs


def _pair_texts(client: GuiPlus, col_a: list[str], col_b: list[str]) -> list[tuple[str, str]]:
    system = (
        "你是词汇配对助手。给你 A、B 两列词，它们一一对应（通常是中文与其英文翻译）。"
        "只输出 JSON 数组，每个元素形如 {\"a\":\"A列里的词\",\"b\":\"B列里的词\"}，"
        "覆盖所有能确定的配对。a 必须来自 A 列、b 必须来自 B 列，原样照抄不要改写。"
        "不要输出任何多余文字、解释或代码块标记。"
    )
    user = (
        f"A列: {json.dumps(col_a, ensure_ascii=False)}\n"
        f"B列: {json.dumps(col_b, ensure_ascii=False)}\n"
        "请输出配对 JSON 数组。"
    )
    response = client.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=500,
    )
    return _parse_pairs(client.content(response))


def solve(
    adb: Adb,
    client: GuiPlus,
    log: Callable[[dict], None],
    max_rounds: int = 6,
    settle_s: float = 0.6,
) -> str:
    """反复:dump→解析两列→1 次配对判断→逐对点击→再 dump 收敛，直到无待配对词块。"""
    matched_total = 0
    for rnd in range(max_rounds):
        xml = adb.ui_dump().decode("utf-8", errors="replace")
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            log({"solver": "matching", "round": rnd, "event": "xml_parse_error", "error": str(exc)[:200]})
            return "matching_xml_error"

        width, height = _screen_size(root)
        left, right = _split_columns(_word_cells(root, width, height))
        if not left or not right:
            log(
                {
                    "solver": "matching",
                    "round": rnd,
                    "event": "cleared" if matched_total else "no_cells",
                    "matched_total": matched_total,
                }
            )
            return "matching_cleared" if matched_total else "matching_no_cells"

        col_a = [c["text"] for c in left]
        col_b = [c["text"] for c in right]
        try:
            pairs = _pair_texts(client, col_a, col_b)
        except ManoError as exc:
            log({"solver": "matching", "round": rnd, "event": "pair_error", "error": str(exc)[:300]})
            return "matching_pair_error"

        by_a = {c["text"]: c for c in left}
        by_b = {c["text"]: c for c in right}
        tapped = 0
        for a, b in pairs:
            ca, cb = by_a.get(a), by_b.get(b)
            if ca is None or cb is None:  # 容忍模型把 a/b 写反
                ca = ca or by_a.get(b)
                cb = cb or by_b.get(a)
            if ca is None or cb is None:
                continue
            adb.tap(ca["cx"], ca["cy"])
            time.sleep(0.25)
            adb.tap(cb["cx"], cb["cy"])
            time.sleep(settle_s)
            tapped += 1
            matched_total += 1

        log(
            {
                "solver": "matching",
                "round": rnd,
                "col_a": col_a,
                "col_b": col_b,
                "pairs": pairs,
                "tapped": tapped,
                "matched_total": matched_total,
            }
        )
        if tapped == 0:  # 一对都没点成 → 解析/配对与真实界面不符,交还主循环
            return "matching_stuck"
    return "matching_max_rounds"
