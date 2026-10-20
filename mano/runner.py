"""The thin, app-agnostic run loop.

One observation -> one proposed action -> one execution -> re-observe. The loop
reuses the existing mano modules (Adb / GuiVLM / execute_action / action_codec /
media) and keeps only the generic parts: device health, evidence journaling,
single-step VLM assembly with retry, and the runtime anti-loop guards.

Scope note (0.x): the runtime anti-loop guards below are generic. When a durable
``journal`` is supplied the loop also brackets every device-modifying action and
replays nothing from the blurry window on restart; with ``journal=None`` neither
is active. Cost/price estimation is NOT computed here; the runner records raw
usage and leaves pricing to the caller.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .action_codec import (
    action_signature,
    execution_payload,
    mapped_action,
    parse_tool_call,
    semantic_signature,
)
from .action_journal import (
    ALL_DECISIONS,
    ALL_VERDICTS,
    ALREADY_APPLIED,
    AMBIGUOUS_EFFECT,
    CONTINUE,
    DISPATCHING,
    EXECUTED,
    EXECUTION_CONFIRMED,
    FINALIZED,
    HOOK_IDEMPOTENT,
    HOOK_NOT_APPLICABLE,
    HOOK_NOT_REPLAYABLE,
    MANUAL_REVIEW,
    NOT_APPLIED,
    NOT_EXECUTED,
    OUTCOME_UNKNOWN,
    PACK_INCOMPATIBLE,
    PREPARED,
    RECONCILIATION_UNAVAILABLE,
    REPLAN,
    SAFE_TO_REOBSERVE,
    TERMINATE,
    UNKNOWN,
    ActionIntent,
    ActionJournal,
    ActionJournalError,
    JournalCorruptError,
    RecoveryReport,
    core_fact_from_state,
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
from .pack import (
    ALLOW,
    BLOCK,
    REOBSERVE,
    ReconciliationResult,
    ResolveDecision,
    ResolvedAction,
    VerificationResult,
)
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

# Stable journal/recovery result codes (Core facts, never the Consumer's business
# states like NEEDS_HUMAN_REVIEW). journal_config_error = run_id / hook-ABI
# preflight failed; journal_error = a CAS conflict / DB busy / corrupt journal
# (the run stops, never degrades to no-journal); recovery_required = an open
# action but no recovery_decide callback; recovery_manual_review / _terminated =
# the Consumer chose to stop; invalid_recovery_decision = an illegal/absent
# Consumer decision for the recovered fact.
_RESULT_JOURNAL_CONFIG_ERROR = "journal_config_error"
_RESULT_JOURNAL_ERROR = "journal_error"
_RESULT_RECOVERY_REQUIRED = "recovery_required"
_RESULT_RECOVERY_MANUAL_REVIEW = "recovery_manual_review"
_RESULT_RECOVERY_TERMINATED = "recovery_terminated"
_RESULT_INVALID_RECOVERY_DECISION = "invalid_recovery_decision"



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
    recovery_reports: list = field(default_factory=list)
    journal_error_code: Optional[str] = None


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


def _hook_accepts_journal_abi(hook) -> bool:
    """True iff ``hook`` can be *bound* with the journal-mode ABI keywords. This is
    a pure signature check (never calls the hook body), so a genuine ``TypeError``
    raised inside a correct hook is not mistaken for a signature mismatch."""
    try:
        inspect.signature(hook).bind(
            executed=None, journaled_action=None, action_id="", recovery=False
        )
        return True
    except (TypeError, ValueError):
        return False


def _resolve_hook_recovery(pack) -> tuple[Optional[str], Optional[str]]:
    """Return ``(hook_recovery, config_error_code)`` for journal mode. No hook ->
    NOT_APPLICABLE. A hook present MUST declare IDEMPOTENT / NOT_REPLAYABLE and use
    the new ABI, else it is a fail-closed config error (checked before any action).
    """
    hook = getattr(pack, "on_action_executed", None)
    if not callable(hook):
        return HOOK_NOT_APPLICABLE, None
    # ABI shape first: a legacy ``(*, executed)`` hook is fundamentally the wrong
    # contract; only a journal-shaped hook is then asked to declare its recovery.
    if not _hook_accepts_journal_abi(hook):
        return None, "hook_abi_incompatible"
    declared = getattr(pack, "hook_recovery", None)
    if declared not in (HOOK_IDEMPOTENT, HOOK_NOT_REPLAYABLE):
        return None, "hook_recovery_not_declared"
    return declared, None


def _nonempty(value) -> bool:
    """A non-empty string identity (``None`` / blank are both rejected)."""
    return isinstance(value, str) and bool(value.strip())


def _reconcile_config_error(pack) -> Optional[str]:
    """A Pack that can reconcile MUST advertise a stable identity: non-empty
    ``pack_id`` + ``reconciliation_schema_version``, so a later process can prove
    the recovering Pack is the same one that journaled the action."""
    if not callable(getattr(pack, "reconcile_action", None)):
        return None
    if not _nonempty(getattr(pack, "pack_id", None)):
        return "reconcile_pack_id_required"
    if not _nonempty(getattr(pack, "reconciliation_schema_version", None)):
        return "reconcile_schema_required"
    return None


def _require_record(journal: ActionJournal, action_id: str):
    """Fetch a record through the Protocol, mapping a vanished row to a corrupt
    journal (a conforming Journal must never lose an in-flight action)."""
    rec = journal.get_record(action_id)
    if rec is None:
        raise JournalCorruptError(f"journal lost record {action_id!r}")
    return rec



def _payload_hash(action: dict) -> str:
    """Stable hash of the non-coordinate execution payload (reuses action_codec's
    canonical ``execution_payload``; no third normalization path)."""
    canonical = json.dumps(
        execution_payload(action), sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _obs_ref(obs: Observation, work_dir: Path, run_dir: Path) -> dict:
    """A journal-safe reference (no raw evidence) that locates an observation's
    files relative to ``run_dir``, so a fresh process can find them after a crash.
    """
    base = work_dir.relative_to(run_dir).as_posix()
    prefix = "" if base == "." else base + "/"
    return {
        "observation_id": obs.observation_id,
        "screenshot": prefix + obs.screenshot,
        "xml": prefix + obs.xml_path,
        "foreground": obs.foreground,
    }


def _pack_reconcile_fact(pack, rec) -> str:
    """Core fact for an ambiguous (OUTCOME_UNKNOWN) record given the Pack's
    reconciliation capability: no interface -> RECONCILIATION_UNAVAILABLE; a
    missing / mismatched ``pack_id`` or ``reconciliation_schema_version`` (either
    side) -> PACK_INCOMPATIBLE; otherwise AMBIGUOUS_EFFECT (judgeable). Matching on
    both identity + schema stops a different Pack that happens to reuse a schema
    string from silently closing another Pack's action."""
    if not callable(getattr(pack, "reconcile_action", None)):
        return RECONCILIATION_UNAVAILABLE
    cur_pid = getattr(pack, "pack_id", None)
    cur_sv = getattr(pack, "reconciliation_schema_version", None)
    if not (_nonempty(cur_pid) and _nonempty(cur_sv)):
        return PACK_INCOMPATIBLE
    if not (_nonempty(rec.pack_id) and _nonempty(rec.reconciliation_schema_version)):
        return PACK_INCOMPATIBLE
    if cur_pid != rec.pack_id or cur_sv != rec.reconciliation_schema_version:
        return PACK_INCOMPATIBLE
    return AMBIGUOUS_EFFECT


def _safe_recovery_decision(recovery_decide, report) -> Optional[str]:
    """Call the Consumer's recovery callback, failing closed: any exception or any
    value outside the decision vocabulary becomes ``None`` (treated as illegal)."""
    try:
        decision = recovery_decide(report)
    except Exception:
        return None
    return decision if decision in ALL_DECISIONS else None


def _run_recovery(
    *,
    journal: ActionJournal,
    rec,
    adb: Adb,
    work_dir: Path,
    run_dir: Path,
    pack,
    recovery_decide,
    hook_recovery: str,
    capture_settle_s: float,
    max_skew_ms: int,
    recovery_reports: list,
    log,
) -> tuple[bool, Optional[str]]:
    """Resolve one open action BEFORE the normal loop may run. Returns
    ``(stop, result_code)``: ``stop=True`` means the run must return with
    ``result_code`` and the record stays open (or was safely closed and stopped);
    ``stop=False`` means the record was closed and the loop may proceed. Reads are
    the only ADB allowed here; the ambiguous window is NEVER re-dispatched.
    ``ActionJournalError`` propagates so the caller stops without degrading."""
    action_id = rec.action_id
    state = rec.state

    def _emit(core_fact: str, verdict: Optional[str]):
        fresh = _require_record(journal, action_id)
        report = RecoveryReport(
            run_id=fresh.run_id,
            action_sequence=fresh.action_sequence,
            action_id=fresh.action_id,
            revision=fresh.revision,
            core_fact=core_fact,
            dispatch_certainty=fresh.dispatch_certainty,
            reconciliation_verdict=verdict,
            hook_applied=fresh.hook_applied,
            pre_execution_observation_refs=fresh.pre_execution_observation_refs,
            recovery_observation_refs=fresh.recovery_observation_refs,
        )
        recovery_reports.append(report)
        log(
            {
                "recovery": True,
                "action_id": action_id,
                "core_fact": core_fact,
                "reconciliation_verdict": verdict,
                "dispatch_certainty": fresh.dispatch_certainty,
            }
        )
        return report, fresh

    if state == PREPARED:
        report, fresh = _emit(SAFE_TO_REOBSERVE, None)
        if recovery_decide is None:
            return True, _RESULT_RECOVERY_REQUIRED
        decision = _safe_recovery_decision(recovery_decide, report)
        if decision == MANUAL_REVIEW:
            return True, _RESULT_RECOVERY_MANUAL_REVIEW
        if decision == TERMINATE:
            return True, _RESULT_RECOVERY_TERMINATED
        if decision not in (CONTINUE, REPLAN):
            return True, _RESULT_INVALID_RECOVERY_DECISION
        # ADB never entered: close the stale intent; never replay old coordinates.
        journal.transition(
            action_id,
            expected_state=PREPARED,
            expected_revision=fresh.revision,
            new_state=NOT_EXECUTED,
        )
        return False, None

    if state == EXECUTED:
        # Recovery MUST honor the hook contract that was PERSISTED with the action,
        # never the current Pack's declaration: a Pack upgraded to add an IDEMPOTENT
        # hook must not run it against an action journaled when it had no hook.
        persisted_hr = rec.hook_recovery
        current_hr, _cfg = _resolve_hook_recovery(pack)
        incompatible = persisted_hr == HOOK_IDEMPOTENT and current_hr != HOOK_IDEMPOTENT
        report, fresh = _emit(PACK_INCOMPATIBLE if incompatible else EXECUTION_CONFIRMED, None)
        if recovery_decide is None:
            return True, _RESULT_RECOVERY_REQUIRED
        decision = _safe_recovery_decision(recovery_decide, report)
        if decision == MANUAL_REVIEW:
            return True, _RESULT_RECOVERY_MANUAL_REVIEW
        if decision == TERMINATE:
            return True, _RESULT_RECOVERY_TERMINATED
        if decision not in (CONTINUE, REPLAN):
            return True, _RESULT_INVALID_RECOVERY_DECISION
        # ADB already confirmed: NEVER replay it. Close per the PERSISTED hook
        # declaration; an unsatisfiable hook contract can never be auto-continued.
        if incompatible:
            return True, _RESULT_INVALID_RECOVERY_DECISION
        if persisted_hr == HOOK_NOT_APPLICABLE:
            journal.transition(
                action_id,
                expected_state=EXECUTED,
                expected_revision=fresh.revision,
                new_state=FINALIZED,
                patch={"hook_applied": False},
            )
            return False, None
        if persisted_hr == HOOK_IDEMPOTENT:
            hook = getattr(pack, "on_action_executed", None)
            try:
                hook(executed=None, journaled_action=fresh, action_id=action_id, recovery=True)
            except Exception as exc:
                log({"recovery": True, "action_id": action_id, "post_execute_error": str(exc)[:500]})
                return True, "post_execute_hook_failed"
            journal.transition(
                action_id,
                expected_state=EXECUTED,
                expected_revision=fresh.revision,
                new_state=FINALIZED,
                patch={"hook_applied": True},
            )
            return False, None
        # NOT_REPLAYABLE: hook never replayed, ADB never replayed -> Consumer only.
        return True, _RESULT_INVALID_RECOVERY_DECISION

    # DISPATCHING / OUTCOME_UNKNOWN: the dispatch MAY have happened. Take a fresh
    # recovery observation (append-only refs), then ask the Pack to reconcile.
    obs = capture_observation(
        adb,
        work_dir,
        0,
        capture_frame=_capture_frame,
        settle_s=capture_settle_s,
        max_skew_ms=max_skew_ms,
    )
    ref = _obs_ref(obs, work_dir, run_dir)
    if state == DISPATCHING:
        journal.transition(
            action_id,
            expected_state=DISPATCHING,
            expected_revision=rec.revision,
            new_state=OUTCOME_UNKNOWN,
            patch={"recovery_observation_refs": [ref]},
        )
    else:  # already OUTCOME_UNKNOWN: a repeated recovery pass appends, never overwrites.
        journal.transition(
            action_id,
            expected_state=OUTCOME_UNKNOWN,
            expected_revision=rec.revision,
            new_state=OUTCOME_UNKNOWN,
            patch={"recovery_observation_refs": [ref]},
        )
    fresh = _require_record(journal, action_id)
    stable = obs.stability == "stable"
    log({"recovery": True, "action_id": action_id, "recovery_stability": obs.stability})
    core_fact = _pack_reconcile_fact(pack, fresh)
    verdict: Optional[str] = None
    if core_fact == AMBIGUOUS_EFFECT and not stable:
        # An unstable recovery observation (foreground switch / capture timeout) is
        # appended as failure evidence but MUST NOT drive a verdict: treat as
        # UNKNOWN so CONTINUE/REPLAN is refused and OUTCOME_UNKNOWN stays open.
        verdict = UNKNOWN
    elif core_fact == AMBIGUOUS_EFFECT:
        reconcile = getattr(pack, "reconcile_action", None)
        try:
            rr = reconcile(journaled_action=fresh, fresh_observation=obs, recovery=True)
            verdict = rr.verdict if isinstance(rr, ReconciliationResult) and rr.verdict in ALL_VERDICTS else UNKNOWN
        except Exception:
            verdict = UNKNOWN
    report, fresh = _emit(core_fact, verdict)
    if recovery_decide is None:
        return True, _RESULT_RECOVERY_REQUIRED
    decision = _safe_recovery_decision(recovery_decide, report)
    if decision == MANUAL_REVIEW:
        return True, _RESULT_RECOVERY_MANUAL_REVIEW
    if decision == TERMINATE:
        return True, _RESULT_RECOVERY_TERMINATED
    if decision not in (CONTINUE, REPLAN):
        return True, _RESULT_INVALID_RECOVERY_DECISION
    # CONTINUE / REPLAN is legal only with a concrete reconciliation verdict; an
    # UNKNOWN / unavailable / incompatible fact can never be auto-continued.
    if core_fact != AMBIGUOUS_EFFECT or verdict not in (ALREADY_APPLIED, NOT_APPLIED):
        return True, _RESULT_INVALID_RECOVERY_DECISION
    journal.transition(
        action_id,
        expected_state=OUTCOME_UNKNOWN,
        expected_revision=fresh.revision,
        new_state=FINALIZED,
        patch={"reconciliation_verdict": verdict},
    )
    return False, None



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
    run_id: Optional[str] = None,
    journal: Optional[ActionJournal] = None,
    recovery_decide: Optional[Callable[[RecoveryReport], str]] = None,
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

    Durable action journal (optional; ``journal=None`` keeps today's behavior and
    the legacy ``on_action_executed(*, executed)`` ABI unchanged). When a
    ``journal`` is supplied the Consumer MUST pass a stable, non-empty ``run_id``
    (reused across restarts); the runner validates ``run_id`` + the hook ABI BEFORE
    any observation / model / ADB, recovers every open action for that ``run_id``
    BEFORE the normal loop may run, and brackets each device-modifying action with
    ``prepare -> DISPATCHING -> execute -> EXECUTED -> hook -> FINALIZED`` so a
    crash in the blurry window is never auto-replayed. Any journal CAS conflict /
    DB-busy / corruption stops the run (``journal_error``); the runner never
    degrades to running without the journal.
    """
    wall_start = time.monotonic()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    recovery_reports: list = []
    journal_error_code: Optional[str] = None
    hook_recovery = HOOK_NOT_APPLICABLE

    def _result(result: str, *, total_steps: int = 0, actions_executed: int = 0, reobserves: int = 0) -> RunResult:
        return RunResult(
            result=result,
            total_steps=total_steps,
            actions_executed=actions_executed,
            vlm_calls=client.call_count,
            retries=client.retries,
            prompt_tokens=client.prompt_tokens,
            completion_tokens=client.completion_tokens,
            wall_seconds=round(time.monotonic() - wall_start, 1),
            run_dir=str(run_dir),
            reobserves=reobserves,
            recovery_reports=recovery_reports,
            journal_error_code=journal_error_code,
        )

    # Journal preflight: fail closed BEFORE any observation / model / ADB call.
    if journal is not None:
        if not isinstance(run_id, str) or not run_id.strip():
            journal_error_code = "run_id_required"
            return _result(_RESULT_JOURNAL_CONFIG_ERROR)
        hook_recovery, hook_cfg_err = _resolve_hook_recovery(pack)
        if hook_cfg_err is not None:
            journal_error_code = hook_cfg_err
            return _result(_RESULT_JOURNAL_CONFIG_ERROR)
        reconcile_cfg_err = _reconcile_config_error(pack)
        if reconcile_cfg_err is not None:
            journal_error_code = reconcile_cfg_err
            return _result(_RESULT_JOURNAL_CONFIG_ERROR)

    ensure_adb_ready(adb)
    # Journal mode writes evidence into a per-process attempt dir so a restart
    # never overwrites the crashed run's screenshots / XML / steps.jsonl.
    if journal is not None:
        work_dir = run_dir / "attempts" / uuid.uuid4().hex[:12]
    else:
        work_dir = run_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    jsonl = (work_dir / "steps.jsonl").open("w", encoding="utf-8")

    def log(record: dict) -> None:
        jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        jsonl.flush()

    # Recovery MUST precede any model call or new prepare(): resolve every open
    # action for this run_id first; only once none remain may the loop run.
    if journal is not None:
        assert run_id is not None  # guaranteed by the preflight above
        try:
            open_actions = journal.load_open_actions(run_id)
        except ActionJournalError as exc:
            journal_error_code = exc.code
            jsonl.close()
            return _result(_RESULT_JOURNAL_ERROR)
        if open_actions:
            try:
                stop, code = _run_recovery(
                    journal=journal,
                    rec=open_actions[0],
                    adb=adb,
                    work_dir=work_dir,
                    run_dir=run_dir,
                    pack=pack,
                    recovery_decide=recovery_decide,
                    hook_recovery=hook_recovery,
                    capture_settle_s=capture_settle_s,
                    max_skew_ms=max_skew_ms,
                    recovery_reports=recovery_reports,
                    log=log,
                )
            except ActionJournalError as exc:
                journal_error_code = exc.code
                jsonl.close()
                return _result(_RESULT_JOURNAL_ERROR)
            if stop:
                jsonl.close()
                return _result(code)

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
            work_dir,
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
            text, action = propose_action(client, work_dir / obs.screenshot, system_prompt, instruction)
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

        if journal is None:
            # --- no journal: today's behavior + legacy hook ABI (unchanged) ---
            try:
                done = execute_action(adb, final, obs.width, obs.height)
                record["executed"] = True
                actions_executed += 1
            except TextInputError as exc:
                # A text backend may leave an ambiguous state (restore failed after
                # a possible injection): stop, never replay, surface it distinctly.
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

            # Optional post-execute notification: the device change already
            # happened, so a hook failure stops the run (never replays) and is NOT
            # an ADB failure. Deep copies keep this untrusted extension point from
            # mutating the audited final action / metadata.
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
        else:
            # --- journal: prepare -> DISPATCHING (durable) -> execute -> EXECUTED
            # -> hook -> FINALIZED. PREPARED + DISPATCHING MUST be durable before
            # the external side-effect window so a crash is never auto-replayed. ---
            action_id = f"{run_id}:{obs.observation_id}:{step}"
            record["journal_action_id"] = action_id
            intent = ActionIntent(
                run_id=run_id,
                action_id=action_id,
                semantic_target=decision.semantic_target,
                action_type=final["action"],
                execution_payload_hash=_payload_hash(final),
                pack_id=getattr(pack, "pack_id", None),
                pack_digest=getattr(pack, "pack_digest", None),
                reconciliation_schema_version=getattr(pack, "reconciliation_schema_version", None),
                reconciliation_context=getattr(decision, "reconciliation_context", None),
                pre_execution_observation_refs=[_obs_ref(obs, work_dir, run_dir)],
                foreground=obs.foreground,
                hook_recovery=hook_recovery,
            )
            try:
                prepared = journal.prepare(intent)
                dispatch_rev = journal.transition(
                    action_id,
                    expected_state=PREPARED,
                    expected_revision=prepared.revision,
                    new_state=DISPATCHING,
                )
            except ActionJournalError as exc:
                # Zero ADB: a CAS conflict (another Runner won) / busy / corrupt
                # journal stops this Runner; it never degrades to no-journal.
                journal_error_code = exc.code
                record["exec_error"] = exc.code
                log(record)
                jsonl.close()
                return _result(_RESULT_JOURNAL_ERROR, total_steps=step, actions_executed=actions_executed, reobserves=reobserves)

            try:
                done = execute_action(adb, final, obs.width, obs.height)
            except TextInputError as exc:
                # ame=False is the only proof of no dispatch -> NOT_EXECUTED; any
                # ambiguous text error -> OUTCOME_UNKNOWN (never replayed).
                new_state = OUTCOME_UNKNOWN if exc.action_may_have_executed else NOT_EXECUTED
                _record_text_error(
                    record,
                    exc.code,
                    retryable=exc.retryable,
                    may_have_executed=exc.action_may_have_executed,
                    state_changed=exc.device_state_may_have_changed,
                )
                record["exec_error"] = str(exc)[:500]
                try:
                    journal.transition(action_id, expected_state=DISPATCHING, expected_revision=dispatch_rev, new_state=new_state)
                except ActionJournalError as jexc:
                    # The classification could not be persisted -> surface the journal
                    # failure explicitly (never swallow it); the record stays open.
                    journal_error_code = jexc.code
                    record["journal_error"] = jexc.code
                    log(record)
                    jsonl.close()
                    return _result(_RESULT_JOURNAL_ERROR, total_steps=step, actions_executed=actions_executed, reobserves=reobserves)
                log(record)
                result = "text_ambiguous_execution" if exc.action_may_have_executed else "exec_refused"
                break
            except (ManoError, subprocess.CalledProcessError, OSError) as exc:
                # Cannot prove the dispatch did not happen -> OUTCOME_UNKNOWN.
                record["exec_error"] = str(exc)[:500]
                try:
                    journal.transition(action_id, expected_state=DISPATCHING, expected_revision=dispatch_rev, new_state=OUTCOME_UNKNOWN)
                except ActionJournalError as jexc:
                    journal_error_code = jexc.code
                    record["journal_error"] = jexc.code
                    log(record)
                    jsonl.close()
                    return _result(_RESULT_JOURNAL_ERROR, total_steps=step, actions_executed=actions_executed, reobserves=reobserves)
                log(record)
                result = "exec_refused"
                break

            # ADB dispatch returned success -> count it even if EXECUTED fails to
            # persist (the dispatch is a real device fact).
            record["executed"] = True
            actions_executed += 1
            try:
                exec_rev = journal.transition(action_id, expected_state=DISPATCHING, expected_revision=dispatch_rev, new_state=EXECUTED)
            except ActionJournalError as exc:
                # Keep DISPATCHING; stop; no hook; no second action; recover on restart.
                journal_error_code = exc.code
                log(record)
                jsonl.close()
                return _result(_RESULT_JOURNAL_ERROR, total_steps=step, actions_executed=actions_executed, reobserves=reobserves)

            if hook_recovery == HOOK_NOT_APPLICABLE:
                try:
                    journal.transition(action_id, expected_state=EXECUTED, expected_revision=exec_rev, new_state=FINALIZED, patch={"hook_applied": False})
                except ActionJournalError as exc:
                    journal_error_code = exc.code
                    log(record)
                    jsonl.close()
                    return _result(_RESULT_JOURNAL_ERROR, total_steps=step, actions_executed=actions_executed, reobserves=reobserves)
            else:
                hook = getattr(pack, "on_action_executed", None)
                try:
                    journaled = _require_record(journal, action_id)
                except ActionJournalError as exc:
                    # A journal read failure before the hook is a journal error, not
                    # a hook failure; the action stays EXECUTED and is never replayed.
                    journal_error_code = exc.code
                    record["journal_error"] = exc.code
                    log(record)
                    jsonl.close()
                    return _result(_RESULT_JOURNAL_ERROR, total_steps=step, actions_executed=actions_executed, reobserves=reobserves)
                try:
                    hook(
                        executed=ResolvedAction(
                            semantic_target=decision.semantic_target,
                            resolved_action=deepcopy(final),
                            metadata=deepcopy(final_metadata),
                        ),
                        journaled_action=journaled,
                        action_id=action_id,
                        recovery=False,
                    )
                except Exception as exc:
                    # EXECUTED stays durable; the ADB action is never replayed.
                    record["post_execute_error"] = str(exc)[:500]
                    log(record)
                    result = "post_execute_hook_failed"
                    break
                try:
                    journal.transition(action_id, expected_state=EXECUTED, expected_revision=exec_rev, new_state=FINALIZED, patch={"hook_applied": True})
                except ActionJournalError as exc:
                    journal_error_code = exc.code
                    log(record)
                    jsonl.close()
                    return _result(_RESULT_JOURNAL_ERROR, total_steps=step, actions_executed=actions_executed, reobserves=reobserves)

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
    return _result(
        result,
        total_steps=step,
        actions_executed=actions_executed,
        reobserves=reobserves,
    )
