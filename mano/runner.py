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
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .action_codec import (
    action_signature,
    execution_payload,
    mapped_action,
    parse_tool_call,
    semantic_signature,
)
from .adb_executor import Adb, execute_action, validate_executable_action
from .errors import ManoError, ManoNetworkError
from .media import image_data_url
from .observation import (
    DEFAULT_MAX_SKEW_MS,
    Observation,
    capture_observation,
    capture_revalidation,
)
from .pack import ALLOW, BLOCK, REOBSERVE, ResolveDecision, ResolvedAction, VerificationResult
from .text_input import TextInputError


_HISTORY_KEYS = ("action", "coordinate", "coordinate2", "text", "status", "button")

# The only verdicts resolve()/verify() may return; anything else is a Pack bug
# and must fail closed (never silently fall through to ALLOW / REOBSERVE).
_VERDICTS = frozenset({ALLOW, REOBSERVE, BLOCK})

# Actions that do not change device state: no fresh re-observation, no consumer
# approval, and never counted in actions_executed. Everything else is treated as
# device-modifying. (terminate is handled after resolve, before this classification.)
_NON_DEVICE_ACTIONS = frozenset({"wait"})

# Validation codes that denote a text problem (generic layer + text backend). The
# legacy `bad_text` (non-string payload) counts too; identity is not the `text_`
# prefix. Used to mirror the code into the structured text-error log fields.
_TEXT_ERROR_CODES = frozenset(
    {
        "bad_text",
        "text_empty",
        "text_too_long",
        "text_control_character",
        "text_unsupported_by_backend",
    }
)



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


def _record_text_error(
    record: dict,
    code: str,
    *,
    retryable: bool,
    may_have_executed: bool,
    state_changed: bool = False,
) -> None:
    """Mirror a text failure into one uniform set of fields across resolve / verify
    / execute, so an Adapter reads the same keys regardless of which stage failed.
    """
    record["text_error_code"] = code
    record["text_retryable"] = retryable
    record["action_may_have_executed"] = may_have_executed
    record["device_state_may_have_changed"] = state_changed


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


def _safe_approve(approve, resolved: ResolvedAction, observation: Observation) -> bool:
    """Call the consumer veto hook with the resolved semantic action, failing
    closed on any exception."""
    try:
        return bool(approve(resolved, observation))
    except Exception:
        return False


def _safe_resolve(pack, action: dict, observation: Observation, model_text: str) -> ResolveDecision:
    """Call Pack.resolve, failing closed (BLOCK) on any exception."""
    try:
        decision = pack.resolve(
            proposed_action=action, observation=observation, model_text=model_text
        )
    except Exception as exc:  # a pack bug must not drive the device
        return ResolveDecision(
            verdict=BLOCK,
            semantic_target="",
            resolved_action=dict(action),
            reason="resolve_error",
            metadata={"error": str(exc)[:500]},
        )
    if not isinstance(decision, ResolveDecision):
        return ResolveDecision(
            verdict=BLOCK,
            semantic_target="",
            resolved_action=dict(action),
            reason="resolve_error",
            metadata={"error": "resolve did not return a ResolveDecision"},
        )
    return decision


def _safe_verify(verify, approved_intent: str, fresh_view) -> VerificationResult:
    """Call Pack.verify, failing closed (BLOCK) on any exception."""
    try:
        verification = verify(approved_intent=approved_intent, fresh_observation=fresh_view)
    except Exception as exc:
        return VerificationResult(verdict=BLOCK, reason="verify_error", metadata={"error": str(exc)[:500]})
    if not isinstance(verification, VerificationResult):
        return VerificationResult(verdict=BLOCK, reason="verify_error")
    return verification



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
    max_skew_ms: int = DEFAULT_MAX_SKEW_MS,
    max_unstable: int = 3,
    max_reobserve: int = 3,
    approve: Optional[Callable[[ResolvedAction, Observation], bool]] = None,
) -> RunResult:
    """Drive one run: one action per step, re-observe after every execution.

    Each step takes a bounded Observation (screenshot + XML + foreground captured
    close together, foreground-stable and within a time bound -- but NOT an atomic
    snapshot and NOT gated on raw-XML equality). The model and the Pack guard share
    that single bundle. The raw model action is then resolved by the Pack into a
    stable ``semantic_target`` + a physical action; classification and all guards
    run on the *resolved* action (a model ``wait`` may resolve to a device click).

    For a device-modifying resolved action the order is
    ``resolve -> classify -> approve(ResolvedAction) -> RevalidationView ->
    Core foreground check -> Pack.verify (or legacy raw-XML fallback) -> execute``:
    the consumer approves the semantic action, a revalidation re-reads foreground
    (sandwiched around the ui_dump) + XML, a foreground change fails closed with
    zero ADB, and the Pack re-verifies the same target on the latest page (it may
    re-resolve coordinates but never the target or side-effect fields -- the Core
    enforces that). ``wait`` resolved actions are non-device-modifying: never gated
    by ``approve`` and never counted in ``actions_executed``.
    """
    ensure_adb_ready(adb)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    wall_start = time.monotonic()
    jsonl = (run_dir / "steps.jsonl").open("w", encoding="utf-8")

    def log(record: dict) -> None:
        jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        jsonl.flush()

    history: list[dict] = []
    last_sem_sig: tuple | None = None
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

        # Pack resolves the raw model action into a stable semantic target + a
        # physical action. Classification and ALL guards run on the resolved action.
        decision = _safe_resolve(pack, action, obs, text)
        resolved = decision.resolved_action if isinstance(decision.resolved_action, dict) else {}

        record = {
            "step": step,
            "screenshot": obs.screenshot,
            "xml": obs.xml_path,
            "proposal_observation_id": obs.observation_id,
            "observation": obs.to_log(),
            "vlm_raw": text,
            "proposed_action": action,
            "resolve_verdict": decision.verdict,
            "semantic_target": decision.semantic_target,
            "resolved_action": resolved,
            "resolve_reason": decision.reason,
            "mapped_action": None,
            "physical_signature": None,
            "semantic_signature": None,
            "approve_result": None,
            "execution_check": None,
            "verification_mode": None,
            "verify_verdict": None,
            "verify_reason": "",
            "final_resolved_action": None,
            "executed": False,
            "exec_error": None,
            "post_execute_error": None,
            "text_error_code": None,
            "text_retryable": None,
            "action_may_have_executed": None,
            "device_state_may_have_changed": None,
            "blocked_repeat": False,
            "oscillating": False,
            "note": "",
        }

        # Resolve verdict gate (before validation / classification): the Pack may
        # refuse or ask to re-observe before the Core commits to any action. An
        # unrecognized verdict is a Pack bug and fails closed (never ALLOW).
        if decision.verdict not in _VERDICTS:
            record["note"] = "resolve_blocked"
            record["resolve_reason"] = "invalid_resolve_verdict"
            log(record)
            result = "invalid_resolve_verdict"
            break
        if decision.verdict == BLOCK:
            record["note"] = "resolve_blocked"
            log(record)
            result = decision.reason or "resolve_blocked"
            break
        if decision.verdict == REOBSERVE:
            reobserve_streak += 1
            reobserves += 1
            record["note"] = "reobserve_required"
            log(record)
            if reobserve_streak >= max_reobserve:
                result = "reobserve_required"
                break
            continue

        # ALLOW requires a usable semantic identity; BLOCK/REOBSERVE may omit it.
        if not isinstance(decision.semantic_target, str) or not decision.semantic_target:
            record["note"] = "resolve_blocked"
            record["resolve_reason"] = "invalid_semantic_target"
            log(record)
            result = "invalid_semantic_target"
            break

        # ALLOW: Pack output is untrusted input -> validate against the single
        # whitelist (generic + current text backend) before it can reach
        # signatures / approve / verify / the device. Fail closed (zero device
        # change) with a stable error code; never crash the run.
        invalid = validate_executable_action(adb, resolved)
        if invalid is not None:
            record["note"] = "resolve_invalid_action"
            record["resolve_reason"] = invalid
            if invalid in _TEXT_ERROR_CODES:
                _record_text_error(record, invalid, retryable=False, may_have_executed=False)
            log(record)
            result = "resolve_invalid_action"
            break

        act = resolved["action"]
        record["mapped_action"] = mapped_action(resolved, obs.width, obs.height)
        phys_sig = action_signature(resolved, obs.width, obs.height)
        record["physical_signature"] = list(phys_sig) if phys_sig is not None else None
        sem_sig = semantic_signature(decision.semantic_target, resolved)
        record["semantic_signature"] = list(sem_sig) if sem_sig is not None else None

        # Anti-loop keys on the SEMANTIC signature (target + side-effects, no
        # coordinates), so a drifting tap on the same control is still "the same
        # action". recent_loop holds only successfully executed device actions.
        blocked_repeat = sem_sig is not None and sem_sig == last_sem_sig and (repeat + 1) >= 2
        oscillating = (
            sem_sig is not None
            and len(recent_loop) >= 3
            and sem_sig == recent_loop[-2]
            and recent_loop[-1] == recent_loop[-3]
            and sem_sig != recent_loop[-1]
        )
        record["blocked_repeat"] = blocked_repeat
        record["oscillating"] = oscillating

        if act == "terminate":
            record["executed"] = True
            log(record)
            result = f"terminate_{resolved.get('status')}"
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
            # Non-device-modifying: no approval, no revalidation, not counted, and
            # it never enters the device-action history (recent_loop / repeat). A
            # successful wait does clear the re-observe streak.
            try:
                execute_action(adb, resolved, obs.width, obs.height)
                record["executed"] = True
            except (ManoError, subprocess.CalledProcessError, OSError) as exc:
                record["exec_error"] = str(exc)[:500]
                log(record)
                result = "exec_refused"
                break
            reobserve_streak = 0
            log(record)
            history.append(_compact(resolved))
            continue

        # Device-modifying: approve(ResolvedAction) -> revalidation -> verify/fallback.
        approved = ResolvedAction(
            semantic_target=decision.semantic_target,
            resolved_action=resolved,
            metadata=decision.metadata,
        )
        if approve is not None:
            ok = _safe_approve(approve, approved, obs)
            record["approve_result"] = ok
            if not ok:
                record["note"] = "approve_rejected"
                log(record)
                result = "approve_rejected"
                break

        reval = capture_revalidation(adb, obs, step)
        record["execution_check"] = reval.to_log()

        # Core foreground safety boundary (both modes): a foreground switch during
        # confirmation / ui_dump -> zero ADB, re-observe.
        if reval.foreground_changed:
            reobserve_streak += 1
            reobserves += 1
            record["note"] = "reobserve_required"
            log(record)
            if reobserve_streak >= max_reobserve:
                result = "reobserve_required"
                break
            continue

        final = resolved
        final_metadata = decision.metadata
        verify = getattr(pack, "verify", None)
        if callable(verify):
            record["verification_mode"] = "pack_verify"
            verification = _safe_verify(verify, decision.semantic_target, reval)
            record["verify_verdict"] = verification.verdict
            record["verify_reason"] = verification.reason
            if verification.verdict not in _VERDICTS:
                record["note"] = "verify_blocked"
                record["verify_reason"] = "invalid_verify_verdict"
                log(record)
                result = "invalid_verify_verdict"
                break
            if verification.verdict == BLOCK:
                record["note"] = "verify_blocked"
                log(record)
                result = verification.reason or "verify_blocked"
                break
            if verification.verdict == REOBSERVE:
                reobserve_streak += 1
                reobserves += 1
                record["note"] = "reobserve_required"
                log(record)
                if reobserve_streak >= max_reobserve:
                    result = "reobserve_required"
                    break
                continue
            candidate = verification.resolved_action
            # verify output is untrusted too: validate before anti-swap / execute.
            invalid_verify = validate_executable_action(adb, candidate)
            if invalid_verify is not None:
                record["note"] = "verify_invalid_action"
                record["verify_reason"] = invalid_verify
                if invalid_verify in _TEXT_ERROR_CODES:
                    _record_text_error(record, invalid_verify, retryable=False, may_have_executed=False)
                log(record)
                result = "verify_invalid_action"
                break
            # Anti-swap: verify may only re-resolve coordinates. The execution
            # payload (action type + non-coordinate side-effects) must be identical;
            # coordinates and metadata are excluded from the comparison.
            if execution_payload(candidate) != execution_payload(resolved):
                record["note"] = "verify_semantic_swap_blocked"
                log(record)
                result = "verify_semantic_swap_blocked"
                break
            final = candidate
            # Strict: once verify ran, its metadata is the execution-time truth
            # (even {}); never fall back to stale resolve metadata.
            final_metadata = verification.metadata
        else:
            # Legacy fallback (no Pack.verify): foreground already safe; a raw-XML
            # change means re-observe, otherwise execute the initial resolved action.
            record["verification_mode"] = "legacy_raw_xml_fallback"
            if reval.xml_changed:
                reobserve_streak += 1
                reobserves += 1
                record["note"] = "reobserve_required"
                log(record)
                if reobserve_streak >= max_reobserve:
                    result = "reobserve_required"
                    break
                continue

        reobserve_streak = 0
        record["final_resolved_action"] = final
        try:
            done = execute_action(adb, final, obs.width, obs.height)
            record["executed"] = True
            actions_executed += 1
        except TextInputError as exc:
            # A text backend may leave an ambiguous state (restore failed after a
            # possible injection): stop, never replay, and surface it distinctly.
            _record_text_error(
                record,
                exc.code,
                retryable=exc.retryable,
                may_have_executed=exc.action_may_have_executed,
                state_changed=exc.device_state_may_have_changed,
            )
            record["exec_error"] = str(exc)[:500]
            log(record)
            result = "text_ambiguous_execution" if exc.action_may_have_executed else "exec_refused"
            break
        except (ManoError, subprocess.CalledProcessError, OSError) as exc:
            record["exec_error"] = str(exc)[:500]
            log(record)
            result = "exec_refused"
            break

        # Optional post-execute notification: the device change already happened,
        # so a hook failure stops the run (never replays) and is NOT an ADB
        # failure. Deep copies keep this untrusted extension point from mutating
        # the audited final action / metadata.
        hook = getattr(pack, "on_action_executed", None)
        if callable(hook):
            try:
                hook(
                    executed=ResolvedAction(
                        semantic_target=decision.semantic_target,
                        resolved_action=deepcopy(final),
                        metadata=deepcopy(final_metadata),
                    )
                )
            except Exception as exc:
                record["post_execute_error"] = str(exc)[:500]
                log(record)
                result = "post_execute_hook_failed"
                break

        log(record)
        history.append(_compact(final))
        # Only a successfully executed device action enters the semantic loop
        # history; REOBSERVE / blocked / failed steps never pollute it.
        recent_loop.append(sem_sig)
        repeat = repeat + 1 if (sem_sig is not None and sem_sig == last_sem_sig) else 0
        last_sem_sig = sem_sig
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
        wall_seconds=round(time.monotonic() - wall_start, 1),
        run_dir=str(run_dir),
        reobserves=reobserves,
    )
