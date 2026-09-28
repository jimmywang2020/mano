"""交付③:v0 最小笼子 — 付费墙检测 + 前台包名(防跳出多邻国)。"""

from __future__ import annotations

import re

# 命中即"停+交人"(安全失败方向,宁误拦不误点付费)。首跑后按实际 XML 微调。
PAYWALL_MARKERS = (
    "Super", "超级多邻国", "免费试用", "订阅", "购买", "家庭方案", "free trial",
)


def paywall_detected(xml_text: str) -> bool:
    return any(marker in xml_text for marker in PAYWALL_MARKERS)


# 单节课已完成的信号:课后会推"新挑战/跳级测试/加油宝箱"等新循环,命中即视为本节课结束。
LESSON_DONE_MARKERS = (
    "准备开始", "开始新的挑战", "开始挑战", "挑战测试", "跳级", "加油宝箱", "超级加油",
)


def bonus_offer_detected(xml_text: str) -> bool:
    return any(marker in xml_text for marker in LESSON_DONE_MARKERS)


def foreground_pkg(adb) -> str:
    # MuMu 的 Android 版本不一,mCurrentFocus/mResumedActivity 行格式先在真机确认。
    out = adb.shell("dumpsys", "window")
    for line in out.splitlines():
        if "mCurrentFocus" in line or "mResumedActivity" in line:
            match = re.search(r"([A-Za-z0-9_.]+)/[A-Za-z0-9_.]+", line)
            if match:
                return match.group(1)
    return ""
