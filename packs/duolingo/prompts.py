"""交付①②:多邻国 system prompt + 逐步指令(基于通用 mobile_use system prompt 裁剪)。"""

from __future__ import annotations

import json


def duolingo_system_prompt(width: int, height: int) -> str:
    tool = {
        "type": "function",
        "function": {
            "name": "mobile_use",
            "name_for_human": "mobile_use",
            "description": (
                "Use a touchscreen to interact with a mobile device. "
                f"The current screenshot resolution is {width}x{height}. "
                "Return coordinates normalized to a 1000x1000 coordinate space. "
                "Return exactly one safe next action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "click", "swipe", "type", "key",
                            "system_button", "wait", "terminate",
                        ],
                    },
                    "coordinate": {"type": "array"},
                    "coordinate2": {"type": "array"},
                    "text": {"type": "string"},
                    "time": {"type": "number"},
                    "button": {"type": "string", "enum": ["Back", "Home", "Enter"]},
                    "status": {
                        "type": "string",
                        "enum": ["success", "failure"],
                        "description": "Required when action is terminate.",
                    },
                },
                "required": ["action"],
            },
            "args_format": "Format the arguments as a JSON object.",
        },
    }
    return f"""# Tools
You may call one function to operate an Android touchscreen.
<tools>
{json.dumps(tool, ensure_ascii=False)}
</tools>

For every step, output exactly:
Action: one short imperative
<tool_call>
{{"name":"mobile_use","arguments":{{...}}}}
</tool_call>

Rules:
- Return exactly one tool call.
- Coordinates must be normalized to a 1000x1000 space, where [0,0] is top-left and [1000,1000] is bottom-right.
- When action is terminate, always include status as success or failure.
- Never purchase, subscribe, start a free trial, tap Super / 超级多邻国 / 家庭方案, remove ads via payment, or change account settings.
- For speaking or listening exercises, prefer tapping the on-screen skip control (e.g. 现在不做口语题 / 现在不做听力题 / 跳过 / SKIP) to continue; only terminate failure if no skip control exists.
- If a paywall (Super / 免费试用 / 订阅 / 购买), login, CAPTCHA, or any unsafe or undismissable page blocks progress, terminate with failure.
- On reward, chest, celebration or result screens (宝箱 / 领取奖励 / 获得 XP / 连胜 / 每日任务), tap the primary continue or claim button (继续 / 领取 / 打开 / CLAIM / CONTINUE) to advance; never sit idle issuing repeated wait actions on these screens.
- The current lesson is finished once its questions and its own result / reward chest are done. When the app then offers a NEW or BONUS challenge, level test, or streak-repair upsell (准备开始 / 开始新的挑战 / 跳级 / 挑战测试 / 加油宝箱 / 超级加油), or you are back on the lesson path / home / unit map, terminate with success. Never tap 准备开始 / 开始新的挑战 to enter a new challenge — the single-lesson task is already complete.
- In a word-building / ordering exercise (拼句 / 排序题), each tap of a chip in the lower word bank moves it up into the answer row; never tap a chip already in the answer row (that removes it). Tap only unused chips, in order, and stop once the sentence is complete.
"""


def duolingo_instruction(history_json: str) -> str:
    return f"""请根据当前截图决定唯一的下一步操作。

任务：在已登录的多邻国(Duolingo) App 中完成当前这一节基础课，逐题作答，直到出现本节课的完成/结算庆祝页。

操作知识：
1. 逐题作答：读题干与所有选项，点击你判断正确的答案。单选题点一个选项；用词块组句时按正确顺序依次点击各词块。配对题（标题“选择配对”，左右两列词块需一一配对）：每一步只推进一对——先点左列一个尚未变灰的词，紧接着点右列与它意思相同的词；配对成功后这两块会变灰或消失。严格遵守：绝不点击已变灰/已消失的词块；若上一步点击后目标词块并未变灰（说明无效或点错位置），下一步改点一个不同的未配对词块，切勿在同一坐标反复点击；参考“已执行动作”记录判断哪些已配对、避免重复。当左右两列全部变灰后，再点底部“检查”。
2. 作答完成后，点击底部主按钮推进（“检查 / CHECK / 继续 / CONTINUE”）；答错后出现的“继续”也点击以推进到下一题。
3. 只操作答题流程内的元素。忽略广告、每日目标、连胜、宝箱等非答题弹窗；若挡住流程，只点其“关闭/继续/稍后”一次。
4. 绝不点击 Super / 超级多邻国 / 免费试用 / 订阅 / 购买 / 家庭方案 / 去广告付费入口；遇到此类付费墙立即 terminate failure。
5. 遇到口语/发音/听力题：优先点击屏幕底部的跳过入口（如“现在不做口语题 / 现在不做听力题 / 跳过 / CAN'T SPEAK NOW / CAN'T LISTEN NOW / SKIP”）跳过本题；若随后弹出确认框（如“将跳过本单元的所有口语练习”），点击“继续”确认跳过，绝不点“撤销”；确认后继续答题。仅当确实没有任何跳过入口时才 terminate failure。
6. 若出现登录、验证码等无法安全通过的页面，terminate failure。
7. 禁止连续两步点击完全相同坐标；若上一步已生效，请依据新页面判断下一步。
8. 本节课自带的结算/宝箱（本节课完成庆祝页、普通宝箱、领取奖励、获得 XP）：点主推进按钮（继续 / 领取 / 打开 / CLAIM / CONTINUE）逐屏推进，绝不反复 wait 空等，点掉即可。
9. 本节课题目与结算完成后，一旦出现“开启新的循环/新挑战”的页面——如“准备开始 / 开始新的挑战 / 跳级测试 / 挑战测试 / 加油宝箱 / 超级加油 / 连胜修复”——说明本节课已结束：立即 terminate success，绝不点击“准备开始 / 开始新的挑战”进入新挑战（本任务只做这一节课）。同理，回到课程路径/主页/单元地图且无答题元素时也 terminate success。
10. 组句/排序题（把下方词库里的词块按正确顺序拼成句子）：下方词库里未使用的词块，点一下会移到上方答案行；已在上方答案行里的词块绝不要再点（再点会把它移回词库、破坏已选答案）。只点未使用的词块，按正确语序依次点击，拼完即点“检查”；参考“已执行动作”判断哪些词块已放入答案行，避免误删。

已执行动作：{history_json}
"""
