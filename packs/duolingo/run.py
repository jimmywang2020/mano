"""mano · Duolingo pack driver: thin VLM loop + matching solver + safety cage.

Subcommands:
  run    drive one basic lesson end-to-end (a human watches the screen)
  smoke  sanity-check the VLM on a single screenshot (parse only, no taps)
  dump   ground-truth the matching-solver XML parse on the current screen
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Make the repo root importable so `mano` and `packs` resolve when this file is
# run directly, e.g. `python3 packs/duolingo/run.py run`.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mano.action_codec import action_signature, mapped_action, parse_tool_call
from mano.adb_executor import Adb, execute_action
from mano.errors import ManoError, ManoNetworkError
from mano.gui_plus_client import GuiPlus
from mano.media import image_data_url, png_size
from packs.duolingo import guards, matching_solver, prompts


PACKAGE = "com.duolingo"
MAX_STEPS = 40
STEP_TIMEOUT_S = 900
SETTLE_S = 1.0
_HISTORY_KEYS = ("action", "coordinate", "coordinate2", "text", "status", "button")


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _compact(action: dict) -> dict:
    return {key: action[key] for key in _HISTORY_KEYS if key in action}


def _is_local_tcp_serial(serial: str) -> bool:
    if not serial.startswith("127.0.0.1:"):
        return False
    try:
        int(serial.split(":", 1)[1])
        return True
    except (IndexError, ValueError):
        return False


def _try_reconnect(adb: Adb, serial: str) -> None:
    if not _is_local_tcp_serial(serial):
        return
    subprocess.run([adb.binary, "start-server"], check=False, capture_output=True)
    subprocess.run([adb.binary, "connect", serial], check=False, capture_output=True)
    time.sleep(0.6)


def _ensure_adb_ready(adb: Adb, serial: str) -> None:
    for attempt in range(2):
        try:
            state = adb.run("get-state").stdout.decode("utf-8", errors="replace").strip()
            if state == "device":
                return
        except subprocess.CalledProcessError:
            pass
        if attempt == 0:
            _try_reconnect(adb, serial)
    raise SystemExit(
        "ADB 设备未就绪。请先确认模拟器/设备在运行，然后执行:\n"
        f"{adb.binary} connect {serial}\n"
        f"{adb.binary} -s {serial} get-state"
    )


def _capture_frame(adb: Adb, serial: str, shot: Path) -> tuple[int, int]:
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(2):
        try:
            return adb.screenshot(shot)
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            if attempt == 0:
                _try_reconnect(adb, serial)
                continue
            break
    assert last_exc is not None
    err = last_exc.stderr.decode("utf-8", errors="replace").strip()
    out = last_exc.stdout.decode("utf-8", errors="replace").strip()
    detail = err or out or repr(last_exc)
    raise SystemExit(
        "截图失败，ADB 连接可能已失效。\n"
        f"serial={serial}\n"
        f"adb={adb.binary}\n"
        f"detail={detail}\n"
        "请先执行:\n"
        f"{adb.binary} connect {serial}\n"
        f"{adb.binary} -s {serial} exec-out screencap -p > /tmp/mano.png"
    )


def call_for_action_duo(client: GuiPlus, image: Path, instruction: str):
    """薄循环单步:装配 system prompt + 截图 + 指令，保留 3 次解析自纠与网络重试。"""
    width, height = png_size(image)
    messages = [
        {"role": "system", "content": prompts.duolingo_system_prompt(width, height)},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": image_data_url(image)}},
            ],
        },
    ]
    last_error: ManoError | None = None
    for attempt in range(3):
        try:
            response = client.complete(messages)
        except ManoError as exc:
            last_error = exc
            if attempt < 2 and isinstance(exc, ManoNetworkError):
                time.sleep(2**attempt)
                continue
            raise
        text = client.content(response)
        try:
            action = parse_tool_call(text)
        except ManoError as exc:
            last_error = exc
            if attempt == 2:
                break
            messages.extend(
                [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            "上一条 mobile_use tool_call 不是有效 JSON。"
                            f"解析错误：{str(exc)[:500]}。"
                            "请保持原定界面动作不变，只返回一个修正后的 <tool_call> JSON。"
                            'click 的 arguments 必须形如 {"action":"click","coordinate":[x,y]}。'
                        ),
                    },
                ]
            )
            continue
        return text, action, response
    raise ManoError(f"VLM 连续 3 次返回无法解析的动作: {last_error}")


def cmd_run(args: argparse.Namespace) -> int:
    serial = args.device or ""
    if not serial:
        raise SystemExit("需要设备 serial:设 MANO_DEVICE_SERIAL 或传 --device(如 127.0.0.1:16384)")
    adb = Adb(serial)
    _ensure_adb_ready(adb, serial)
    client = GuiPlus()
    run_dir = Path(args.output).resolve() / f"duo-{_now_id()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "environment.json").write_text(
        json.dumps(
            {
                "device": serial,
                "package": PACKAGE,
                "model": client.model,
                "endpoint": client.endpoint,
                "max_steps": MAX_STEPS,
                "cold_start": bool(args.cold_start),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.cold_start:
        adb.cold_start(PACKAGE)
        time.sleep(2)

    jsonl = (run_dir / "steps.jsonl").open("w", encoding="utf-8")

    def log(record: dict) -> None:
        jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        jsonl.flush()

    history: list[dict] = []
    last_sig: tuple | None = None
    repeat = 0
    wait_streak = 0
    result = "max_steps"
    step = 0
    deadline = time.monotonic() + STEP_TIMEOUT_S

    for step in range(1, MAX_STEPS + 1):
        if time.monotonic() > deadline:
            result = "timeout"
            break

        shot = run_dir / f"step-{step:02d}.png"
        width, height = _capture_frame(adb, serial, shot)
        xml = adb.ui_dump().decode("utf-8", errors="replace")
        (run_dir / f"step-{step:02d}.xml").write_text(xml, encoding="utf-8")

        # 笼子①:执行之前先拦越界/付费墙
        foreground = guards.foreground_pkg(adb)
        if foreground and foreground != PACKAGE:
            log({"step": step, "screenshot": shot.name, "guard": "left_app", "foreground": foreground})
            result = "left_app"
            break
        if guards.paywall_detected(xml):
            log({"step": step, "screenshot": shot.name, "guard": "paywall"})
            result = "paywall_handed_to_human"
            break
        # 笼②:本节课已完成——课后推新挑战/跳级/加油宝箱即边界,不再往后游荡
        if guards.bonus_offer_detected(xml):
            log({"step": step, "screenshot": shot.name, "guard": "lesson_done"})
            result = "lesson_done"
            break

        # 能力路由:配对题交确定性 solver(XML+ADB),不走 VLM 视觉循环
        if matching_solver.detect(xml):
            solver_status = matching_solver.solve(adb, client, log)
            log({"step": step, "screenshot": shot.name, "router": "matching_solver", "status": solver_status})
            history.append({"action": "matching_solver", "status": solver_status})
            last_sig = None
            repeat = 0
            if solver_status in ("matching_stuck", "matching_no_cells"):
                # solver 解析与真实界面不符,交回 VLM 尝试下一步
                pass
            time.sleep(SETTLE_S)
            continue

        instruction = prompts.duolingo_instruction(json.dumps(history[-8:], ensure_ascii=False))
        try:
            text, action, _ = call_for_action_duo(client, shot, instruction)
        except ManoError as exc:
            log({"step": step, "screenshot": shot.name, "vlm_error": str(exc)[:500]})
            result = "vlm_error" if isinstance(exc, ManoNetworkError) else "vlm_unparseable"
            break

        sig = action_signature(action, width, height)
        blocked_repeat = sig is not None and sig == last_sig and (repeat + 1) >= 2

        record = {
            "step": step,
            "screenshot": shot.name,
            "xml": f"step-{step:02d}.xml",
            "vlm_raw": text,
            "proposed_action": action,
            "mapped_action": mapped_action(action, width, height),
            "executed": False,
            "exec_error": None,
            "blocked_repeat": blocked_repeat,
            "human_verdict": None,
            "note": "",
        }

        if action.get("action") == "terminate":
            record["executed"] = True
            log(record)
            result = f"terminate_{action.get('status')}"
            break
        if blocked_repeat:
            log(record)
            result = "repeat_click_blocked"
            break

        # 笼②半:连续空等守卫——奖励/过场屏上 VLM 反复 wait 不推进时拦下
        wait_streak = wait_streak + 1 if action.get("action") == "wait" else 0
        if wait_streak >= 4:
            record["note"] = "too_many_waits"
            log(record)
            result = "wait_stuck"
            break

        # 笼子②:execute_action 内部再拒未知动作/越界坐标
        try:
            done = execute_action(adb, action, width, height)
            record["executed"] = True
        except ManoError as exc:
            record["exec_error"] = str(exc)[:500]
            log(record)
            result = "exec_refused"
            break

        log(record)
        history.append(_compact(action))
        repeat = repeat + 1 if (sig is not None and sig == last_sig) else 0
        last_sig = sig
        time.sleep(SETTLE_S)
        if done:
            result = "terminate_from_exec"
            break

    jsonl.close()
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "result": result,
                "total_steps": step,
                "model": client.model,
                "endpoint": client.endpoint,
                "run_dir": str(run_dir),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"RESULT={result} steps={step} run_dir={run_dir}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    import xml.etree.ElementTree as ET

    serial = args.device or ""
    if not serial:
        raise SystemExit("需要设备 serial:设 MANO_DEVICE_SERIAL 或传 --device")
    adb = Adb(serial)
    _ensure_adb_ready(adb, serial)
    xml = adb.ui_dump().decode("utf-8", errors="replace")
    root = ET.fromstring(xml)
    width, height = matching_solver._screen_size(root)
    left, right = matching_solver._split_columns(matching_solver._word_cells(root, width, height))
    print(f"detect_matching={matching_solver.detect(xml)} screen={width}x{height}")
    print(f"left({len(left)}): {[(c['text'], c['cx'], c['cy']) for c in left]}")
    print(f"right({len(right)}): {[(c['text'], c['cx'], c['cy']) for c in right]}")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    image = Path(args.image).expanduser().resolve()
    if not image.is_file():
        raise SystemExit(f"截图不存在: {image}")
    client = GuiPlus()
    text, action, _ = call_for_action_duo(client, image, prompts.duolingo_instruction("[]"))
    width, height = png_size(image)
    print(f"PASS VLM: model={client.model} endpoint={client.endpoint}")
    print(f"model_output={text[:400]}")
    print(f"parsed_action={json.dumps(action, ensure_ascii=False)}")
    print(f"mapped_action={json.dumps(mapped_action(action, width, height), ensure_ascii=False)}")
    print("(smoke 只解析,不执行动作)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mano · Duolingo pack — 薄 VLM 循环 spike")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="跑一节课的薄循环(人盯屏)")
    run.add_argument("--device", default=os.getenv("MANO_DEVICE_SERIAL", ""))
    run.add_argument("--output", default=str(_HERE / "runs"))
    run.add_argument("--cold-start", action="store_true", help="冷启动多邻国(默认否:由人类先进到课起点)")
    run.set_defaults(func=cmd_run)

    smoke = sub.add_parser("smoke", help="对单张截图验证 VLM 能读多邻国")
    smoke.add_argument("--image", required=True)
    smoke.set_defaults(func=cmd_smoke)

    dump = sub.add_parser("dump", help="在当前屏幕上验证配对题 XML 解析(ground-truth)")
    dump.add_argument("--device", default=os.getenv("MANO_DEVICE_SERIAL", ""))
    dump.set_defaults(func=cmd_dump)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
