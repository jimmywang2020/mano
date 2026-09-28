"""Android UI-hierarchy XML parsing helpers (uiautomator dump)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Callable


BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("[img]", "")).strip()


def parse_bounds(value: str) -> tuple[int, int, int, int]:
    """Parse a uiautomator ``bounds`` string ``[l,t][r,b]`` into a pixel tuple."""
    match = BOUNDS_RE.fullmatch(value)
    if not match:
        return (0, 0, 0, 0)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def node_texts(node: ET.Element) -> list[str]:
    """Aggregate the de-duplicated non-empty ``text`` of a node and its descendants."""
    values: list[str] = []
    for child in node.iter("node"):
        value = normalize_text(child.attrib.get("text", ""))
        if value and value not in values:
            values.append(value)
    return values


def clickable_nodes_matching(
    root: ET.Element,
    predicate: Callable[[list[str]], bool],
) -> list[tuple[ET.Element, list[str]]]:
    """Clickable nodes whose aggregated texts satisfy ``predicate``, largest area first."""
    candidates: list[tuple[int, int, ET.Element, list[str]]] = []
    for order, node in enumerate(root.iter("node")):
        if node.attrib.get("clickable") != "true":
            continue
        texts = node_texts(node)
        if not predicate(texts):
            continue
        left, top, right, bottom = parse_bounds(node.attrib.get("bounds", ""))
        if right <= left or bottom <= top:
            continue
        candidates.append(((right - left) * (bottom - top), -order, node, texts))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [(node, texts) for _, _, node, texts in candidates]
