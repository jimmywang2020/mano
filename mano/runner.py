"""The thin, app-agnostic run loop.

One observation -> one proposed action -> one execution -> re-observe. The loop
reuses the existing mano modules (Adb / GuiVLM / execute_action / action_codec /
media) and keeps only the generic parts: device health, evidence journaling,
single-step VLM assembly with retry, and the runtime anti-loop guards.

Scope note (0.x): the guards below are *runtime anti-loop* only. Crash recovery
and blurry-window non-replay are NOT implemented here. Cost/price estimation is
NOT computed here either; the runner records raw usage and leaves pricing to the
caller.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .action_codec import action_signature, mapped_action, parse_tool_call
from .adb_executor import Adb, execute_action
from .errors import ManoError, ManoNetworkError
from .media import image_data_url
from .observation import Observation, capture_observation, recheck_before_execute


_HISTORY_KEYS = ("action", "coordinate", "coordinate2", "text", "status", "button")

# Actions that do not change device state: no fresh re-observation, no consumer
# approval, and never counted in actions_executed. Everything else is treated as
# device-modifying. (terminate is handled before this point.)
_NON_DEVICE_ACTIONS = frozenset({"wait"})


@dataclass
class RunResult:
    """Structured, explainable outcome of one run (raw usage, no pricing)."""

    result: str
    total_steps: int
    actions_executed: int
    vlm_calls: int
    retries: int
    prompt_tokens: int
    completion_tokens: int
    wall_seconds: float
    run_dir: str
    reobserves: int = 0


def _compact(action: dict) -> dict:
    return {key: action[key] for key in _HISTORY_KEYS if key in action}


def _loop_sig(action: dict) -> tuple | None:
    """Coarse grid signature over the normalized 0-1000 coordinate, used only to
    catch A-B-A-B oscillation the exact-coordinate repeat guard misses (a tapped
    target drifts a few pixels, so exact signatures never match)."""
    if action.get("action") != "click":
        return None
    coord = action.get("coordinate")
    if not isinstance(coord, list) or len(coord) != 2:
        return None
    try:
        return ("click", round(float(coord[0]) / 40), round(float(coord[1]) / 40))
    except (TypeError, ValueError):
        return None


def _is_local_tcp_serial(serial: str) -> bool:
    if not serial.startswith("127.0.0.1:"):
        return False
    try:
        int(serial.split(":", 1)[1])
        return True
    except (IndexError, ValueError):
        return False


def _try_reconnect(adb: Adb) -> None:
    if not _is_local_tcp_serial(adb.serial):
        return
    subprocess.run([adb.binary, "start-server"], check=False, capture_output=True)
    subprocess.run([adb.binary, "connect", adb.serial], check=False, capture_output=True)
    time.sleep(0.6)


def ensure_adb_ready(adb: Adb) -> None:
    for attempt in range(2):
        try:
            state = adb.run("get-state").stdout.decode("utf-8", errors="replace").strip()
            if state == "device":
                return
        except subprocess.CalledProcessError:
            pass
        if attempt == 0:
            _try_reconnect(adb)
    raise ManoError(
        f"ADB device not ready. Check the emulator/device, then: "
        f"{adb.binary} connect {adb.serial}"
    )


def _capture_frame(adb: Adb, shot: Path) -> tuple[int, int]:
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(2):
        try:
            return adb.screenshot(shot)
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            if attempt == 0:
                _try_reconnect(adb)
                continue
            break
    assert last_exc is not None
    detail = last_exc.stderr.decode("utf-8", errors="replace").strip() or repr(last_exc)
    raise ManoError(f"screenshot failed; ADB connection may be down: {detail}")


def propose_action(client, image: Path, system_prompt: str, instruction: str):
    """Single VLM step: assemble messages, keep 3 parse self-corrections and a
    network-error exponential backoff. Returns ``(raw_text, action)``.

    Public proposal entry point so external consumers can reuse the exact VLM
    assembly instead of copying it; ``_call_for_action`` stays as a private
    backward-compatible alias.
    """
    messages = [
        {"role": "system", "content": system_prompt},
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
                client.retries += 1
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
                            "The previous mobile_use tool_call was not valid JSON. "
                            f"Parse error: {str(exc)[:500]}. "
                            "Keep the intended UI action, return exactly one corrected "
                            '<tool_call> JSON. click arguments must look like '
                            '{"action":"click","coordinate":[x,y]}.'
                        ),
                    },
                ]
            )
            client.retries += 1
            continue
        return text, action
    raise ManoError(f"VLM returned an unparseable action 3 times in a row: {last_error}")


# Backward-compatible private alias (external adapters should migrate to propose_action).
_call_for_action = propose_action


def _safe_approve(approve, action: dict, observation: Observation) -> bool:
    """Call the consumer veto hook, failing closed on any exception."""
    try:
        return bool(approve(action, observation))
    except Exception:
        return False


def run(
    adb: Adb,
    client,
    pack,
    run_dir: Path,
    *,
    max_steps: int = 40,
    step_timeout_s: int = 900,
    settle_s: float = 1.0,
    capture_settle_s: float = 0.4,
    max_skew_ms: int = 4000,
    max_unstable: int = 3,
    max_reobserve: int = 3,
    approve: Optional[Callable[[dict, Observation], bool]] = None,
) -> RunResult:
    """Drive one run: one action per step, re-observe after every execution.

    Each step takes a bounded, coherent Observation (screenshot + XML + foreground
    captured close together and checked for stability); the model and the Pack
    guard share that single bundle. Before any device-modifying action the loop
    re-observes and, if the foreground or XML changed, discards the stale action
    and re-observes instead of executing it. ``wait`` is non-device-modifying: it
    is never gated by ``approve`` and never counted in ``actions_executed``.
    """
    ensure_adb_ready(adb)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    wall_start = time.time()
    jsonl = (run_dir / "steps.jsonl").open("w", encoding="utf-8")

    def log(record: dict) -> None:
        jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        jsonl.flush()

    history: list[dict] = []
    last_sig: tuple | None = None
    repeat = 0
    recent_loop: deque = deque(maxlen=6)
    wait_streak = 0
    unstable_streak = 0
    reobserve_streak = 0
    result = "max_steps"
    step = 0
    actions_executed = 0
    reobserves = 0
    deadline = time.monotonic() + step_timeout_s

    for step in range(1, max_steps + 1):
        if time.monotonic() > deadline:
            result = "timeout"
            break

        obs = capture_observation(
            adb,
            run_dir,
            step,
            capture_frame=_capture_frame,
            settle_s=capture_settle_s,
            max_skew_ms=max_skew_ms,
        )
        if obs.stability != "stable":
            unstable_streak += 1
            log({"step": step, "observation": obs.to_log(), "note": "observation_unstable"})
            if unstable_streak >= max_unstable:
                result = "observation_unstable"
                break
            continue
        unstable_streak = 0

        # Pack decides whether to stop; the returned reason is the run result.
        stop = pack.guard(obs.xml, obs.foreground)
        if stop is not None:
            log({"step": step, "observation": obs.to_log(), "guard": stop, "foreground": obs.foreground})
            result = stop
            break

        system_prompt = pack.system_prompt(obs.width, obs.height)
        instruction = pack.instruction(history)
        try:
            text, action = propose_action(client, run_dir / obs.screenshot, system_prompt, instruction)
        except ManoError as exc:
            log({"step": step, "observation": obs.to_log(), "vlm_error": str(exc)[:500]})
            result = "vlm_error" if isinstance(exc, ManoNetworkError) else "vlm_unparseable"
            break

        sig = action_signature(action, obs.width, obs.height)
        blocked_repeat = sig is not None and sig == last_sig and (repeat + 1) >= 2
        loop_sig = _loop_sig(action)
        recent_loop.append(loop_sig)
        oscillating = (
            loop_sig is not None
            and len(recent_loop) >= 4
            and recent_loop[-1] == recent_loop[-3]
            and recent_loop[-2] == recent_loop[-4]
            and recent_loop[-1] != recent_loop[-2]
        )

        record = {
            "step": step,
            "screenshot": obs.screenshot,
            "xml": obs.xml_path,
            "proposal_observation_id": obs.observation_id,
            "observation": obs.to_log(),
            "vlm_raw": text,
            "proposed_action": action,
            "mapped_action": mapped_action(action, obs.width, obs.height),
            "executed": False,
            "exec_error": None,
            "blocked_repeat": blocked_repeat,
            "oscillating": oscillating,
            "execution_observation_id": None,
            "approve_result": None,
            "note": "",
        }

        act = action.get("action")

        if act == "terminate":
            record["executed"] = True
            log(record)
            result = f"terminate_{action.get('status')}"
            break
        if blocked_repeat or oscillating:
            if oscillating and not blocked_repeat:
                record["note"] = "oscillation_loop"
            log(record)
            result = "oscillation_blocked" if (oscillating and not blocked_repeat) else "repeat_click_blocked"
            break

        wait_streak = wait_streak + 1 if act == "wait" else 0
        if wait_streak >= 4:
            record["note"] = "too_many_waits"
            log(record)
            result = "wait_stuck"
            break

        if act in _NON_DEVICE_ACTIONS:
            # Non-device-modifying: no fresh re-observe, no approval, not counted.
            try:
                execute_action(adb, action, obs.width, obs.height)
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
            continue

        # Device-modifying: re-observe just before executing and veto a stale action.
        exec_obs = recheck_before_execute(adb, obs, step)
        record["execution_observation_id"] = exec_obs.observation_id
        changed = (
            exec_obs.foreground != obs.foreground
            or exec_obs.xml_fingerprint != obs.xml_fingerprint
        )
        if changed:
            reobserve_streak += 1
            reobserves += 1
            record["note"] = "reobserve_required"
            log(record)
            if reobserve_streak >= max_reobserve:
                result = "reobserve_required"
                break
            continue
        reobserve_streak = 0

        if approve is not None:
            ok = _safe_approve(approve, action, exec_obs)
            record["approve_result"] = ok
            if not ok:
                record["note"] = "approve_rejected"
                log(record)
                result = "approve_rejected"
                break

        try:
            done = execute_action(adb, action, obs.width, obs.height)
            record["executed"] = True
            actions_executed += 1
        except ManoError as exc:
            record["exec_error"] = str(exc)[:500]
            log(record)
            result = "exec_refused"
            break

        log(record)
        history.append(_compact(action))
        repeat = repeat + 1 if (sig is not None and sig == last_sig) else 0
        last_sig = sig
        time.sleep(settle_s)
        if done:
            result = "terminate_from_exec"
            break

    jsonl.close()
    return RunResult(
        result=result,
        total_steps=step,
        actions_executed=actions_executed,
        vlm_calls=client.call_count,
        retries=client.retries,
        prompt_tokens=client.prompt_tokens,
        completion_tokens=client.completion_tokens,
        wall_seconds=round(time.time() - wall_start, 1),
        run_dir=str(run_dir),
        reobserves=reobserves,
    )
