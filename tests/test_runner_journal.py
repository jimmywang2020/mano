"""Commit B: Runner <-> durable Action Journal wiring + crash-window recovery.

The invariant under test: *no device action that sits in the blurry execution
window may ever be auto-replayed by Mano*. Two layers of evidence:

  * In-process matrix (A-E): compatibility, the fixed execution order
    (PREPARED -> DISPATCHING -> execute -> EXECUTED -> hook -> FINALIZED),
    exception classification, recovery decisions, and concurrency fail-closed.

  * Real crash matrix (F): a real child process is SIGKILLed at six seams; a
    durable, out-of-process dispatch counter proves the recovered run NEVER
    re-dispatches (counter stays put across the crash+recovery boundary).

No real device, no real VLM, no network. The journal is always a real on-disk
SQLite file (``:memory:`` is explicitly rejected).

This file is ALSO the crash-child entrypoint: ``python test_runner_journal.py
<window> <db> <run_dir> <run_id> <counter> <marker> <schema>`` runs one real
Runner step and self-SIGKILLs at ``<window>``.
"""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# When executed as a plain script (the crash child), make ``mano`` importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mano import runner  # noqa: E402

# --- real imports -------------------------------------------------------------
from mano.action_journal import (  # noqa: E402
    ALREADY_APPLIED,
    CONTINUE,
    DISPATCHING,
    EXECUTED,
    FINALIZED,
    HOOK_IDEMPOTENT,
    HOOK_NOT_APPLICABLE,
    HOOK_NOT_REPLAYABLE,
    MANUAL_REVIEW,
    NOT_APPLIED,
    NOT_EXECUTED,
    OUTCOME_UNKNOWN,
    PREPARED,
    TERMINATE,
    ActionIntent,
    ActionJournalError,
    ActiveActionError,
    SQLiteActionJournal,
)
from mano.pack import ALLOW, ReconciliationResult, ResolveDecision  # noqa: E402

_SCHEMA = "recon-v1"


# --------------------------------------------------------------------------- #
# Self-contained fakes (no cross-test-module imports).
# --------------------------------------------------------------------------- #
def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)


def _tool_call(action: dict) -> str:
    return "<tool_call>" + json.dumps({"name": "mobile_use", "arguments": action}) + "</tool_call>"


class FakeAdb:
    def __init__(self) -> None:
        self.serial = "emulator-fake"
        self.binary = "/usr/bin/adb"
        self.tap_calls = 0
        self.swipe_calls = 0
        self.type_calls = 0
        self.keyevent_calls = 0

    def run(self, *args, capture: bool = True):
        return SimpleNamespace(stdout=b"device")

    def screenshot(self, path: Path) -> tuple[int, int]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png_header(1000, 1000))
        return 1000, 1000

    def ui_dump(self) -> bytes:
        return b"<hierarchy/>"

    def foreground_package(self) -> str:
        return "com.test"

    def tap(self, x: int, y: int) -> None:
        self.tap_calls += 1

    def swipe(self, x1, y1, x2, y2, duration_ms: int = 700) -> None:
        self.swipe_calls += 1

    def type_text(self, text: str) -> None:
        self.type_calls += 1

    def keyevent(self, name: str) -> None:
        self.keyevent_calls += 1


class FakeClient:
    def __init__(self, scripts: list[str]) -> None:
        self._scripts = list(scripts)
        self.call_count = 0
        self.retries = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, messages, max_tokens: int = 700):
        idx = self.call_count
        self.call_count += 1
        return {"_idx": idx}

    def content(self, response) -> str:
        return self._scripts[response["_idx"]]


class CountingClient(FakeClient):
    """A client whose device-action count is externally observable."""

    def __init__(self, scripts: list[str]) -> None:
        super().__init__(scripts)


class FakePack:
    package = "com.test"

    def system_prompt(self, width: int, height: int) -> str:
        return "sys"

    def instruction(self, history) -> str:
        return "inst"

    def guard(self, xml: str, foreground: str | None) -> str | None:
        return None

    def resolve(self, *, proposed_action, observation, model_text) -> ResolveDecision:
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target="target:" + str(proposed_action.get("action")),
            resolved_action=dict(proposed_action),
        )


class LegacyHookPack(FakePack):
    """A Pack with ONLY the old hook ABI -> rejected in journal mode."""

    def on_action_executed(self, *, executed) -> None:  # legacy signature
        pass


class UndeclaredHookPack(FakePack):
    """New-ABI hook but no ``hook_recovery`` declaration -> config error."""

    def on_action_executed(self, *, executed, journaled_action, action_id, recovery) -> None:
        pass


class ReconcilePack(FakePack):
    """Pack that reconciles by reading the real device (here: a durable counter
    file). ``>0`` dispatches observed -> ALREADY_APPLIED, else NOT_APPLIED. No
    hook -> HOOK_NOT_APPLICABLE in journal mode."""

    reconciliation_schema_version = _SCHEMA

    def __init__(self, *, counter_path: Path | None = None) -> None:
        self._counter_path = counter_path

    def reconcile_action(self, *, journaled_action, fresh_observation, recovery=True) -> ReconciliationResult:
        applied = _read_counter(self._counter_path) > 0 if self._counter_path else False
        return ReconciliationResult(verdict=ALREADY_APPLIED if applied else NOT_APPLIED, reason="counter")


class HookReconcilePack(ReconcilePack):
    """ReconcilePack + an idempotent new-ABI hook that records replays (never ADB)."""

    hook_recovery = HOOK_IDEMPOTENT

    def __init__(self, *, counter_path: Path | None = None) -> None:
        super().__init__(counter_path=counter_path)
        self.hook_replays: list[str] = []

    def on_action_executed(self, *, executed, journaled_action, action_id, recovery):
        self.hook_replays.append(action_id)


# --------------------------------------------------------------------------- #
# Durable out-of-process dispatch counter (the crash proof).
# --------------------------------------------------------------------------- #
def _read_counter(path: Path | None) -> int:
    if path is None or not Path(path).exists():
        return 0
    return int(Path(path).read_text() or "0")


def _bump_counter(path: Path) -> None:
    n = _read_counter(path) + 1
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, str(n).encode())
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# A. Compatibility / preflight (journal=None stays today's behavior).
# --------------------------------------------------------------------------- #
def _click_then_terminate():
    return FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )


def test_journal_none_keeps_today_behaviour(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    res = runner.run(FakeAdb(), _click_then_terminate(), FakePack(), tmp_path, settle_s=0)
    assert res.result == "terminate_success"
    assert res.actions_executed == 1
    assert res.recovery_reports == []
    assert res.journal_error_code is None


def test_memory_and_empty_journal_path_rejected(tmp_path):
    with pytest.raises(ActionJournalError):
        SQLiteActionJournal(":memory:")
    with pytest.raises(ActionJournalError):
        SQLiteActionJournal("")
    with pytest.raises(ActionJournalError):
        SQLiteActionJournal("   ")
    # a real path still works
    SQLiteActionJournal(str(tmp_path / "ok.db")).close()


def test_blank_run_id_fails_before_any_adb(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    j = SQLiteActionJournal(str(tmp_path / "j.db"))
    adb = FakeAdb()
    res = runner.run(adb, _click_then_terminate(), FakePack(), tmp_path, settle_s=0, journal=j, run_id="  ")
    assert res.result == "journal_config_error"
    assert res.journal_error_code == "run_id_required"
    assert adb.tap_calls == 0  # fail-closed BEFORE the device
    j.close()


def test_legacy_only_hook_rejected_in_journal_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    j = SQLiteActionJournal(str(tmp_path / "j.db"))
    adb = FakeAdb()
    res = runner.run(adb, _click_then_terminate(), LegacyHookPack(), tmp_path, settle_s=0, journal=j, run_id="r1")
    assert res.result == "journal_config_error"
    assert res.journal_error_code == "hook_abi_incompatible"
    assert adb.tap_calls == 0
    j.close()


def test_undeclared_hook_recovery_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    j = SQLiteActionJournal(str(tmp_path / "j.db"))
    adb = FakeAdb()
    res = runner.run(adb, _click_then_terminate(), UndeclaredHookPack(), tmp_path, settle_s=0, journal=j, run_id="r1")
    assert res.result == "journal_config_error"
    assert res.journal_error_code == "hook_recovery_not_declared"
    assert adb.tap_calls == 0
    j.close()


# --------------------------------------------------------------------------- #
# B. Happy path: the fixed execution order is durable, and the device dispatch
#    happens only after DISPATCHING is persisted / the hook only after EXECUTED.
# --------------------------------------------------------------------------- #
class OrderAdb(FakeAdb):
    """Records the journal state read fresh from disk at tap time."""

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self.state_at_tap: str | None = None

    def tap(self, x: int, y: int) -> None:
        self.state_at_tap = _only_record_state(self._db_path)
        super().tap(x, y)


def _only_record_state(db_path: Path) -> str | None:
    import sqlite3

    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT state FROM actions").fetchall()
    finally:
        con.close()
    return rows[-1][0] if rows else None


def test_happy_path_order_prepared_dispatching_executed_finalized(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    db = tmp_path / "j.db"
    j = SQLiteActionJournal(str(db))
    adb = OrderAdb(db)
    res = runner.run(adb, _click_then_terminate(), FakePack(), tmp_path, settle_s=0, journal=j, run_id="r1")
    assert res.result == "terminate_success"
    assert res.actions_executed == 1
    # DISPATCHING was durable BEFORE the ADB dispatch.
    assert adb.state_at_tap == DISPATCHING
    # The single action finished clean; a NOT_APPLICABLE pack never set the hook.
    recs = _all_records(db)
    assert len(recs) == 1
    assert recs[0]["state"] == FINALIZED
    assert recs[0]["hook_applied"] == 0
    j.close()


def _all_records(db_path: Path) -> list[dict]:
    import sqlite3

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM actions ORDER BY action_sequence").fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# C. Exception classification at the execute seam.
# --------------------------------------------------------------------------- #
class TextAmbiguousAdb(FakeAdb):
    def tap(self, x: int, y: int) -> None:
        from mano.text_input import TextInputError

        raise TextInputError("restore_failed", retryable=False, action_may_have_executed=True)


class TextCleanAdb(FakeAdb):
    def tap(self, x: int, y: int) -> None:
        from mano.text_input import TextInputError

        raise TextInputError("not_sent", retryable=False, action_may_have_executed=False)


class AdbFailureAdb(FakeAdb):
    def tap(self, x: int, y: int) -> None:
        from mano.errors import ManoError

        raise ManoError("adb exploded")


def _run_one(adb, pack, tmp_path, monkeypatch, run_id="r1"):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    db = tmp_path / "j.db"
    j = SQLiteActionJournal(str(db))
    res = runner.run(adb, _click_then_terminate(), pack, tmp_path, settle_s=0, journal=j, run_id=run_id)
    recs = _all_records(db)
    j.close()
    return res, recs


def test_ambiguous_text_error_marks_outcome_unknown(tmp_path, monkeypatch):
    res, recs = _run_one(TextAmbiguousAdb(), FakePack(), tmp_path, monkeypatch)
    assert res.result == "text_ambiguous_execution"
    assert recs[-1]["state"] == OUTCOME_UNKNOWN


def test_clean_text_error_marks_not_executed(tmp_path, monkeypatch):
    res, recs = _run_one(TextCleanAdb(), FakePack(), tmp_path, monkeypatch)
    assert res.result == "exec_refused"
    assert recs[-1]["state"] == NOT_EXECUTED


def test_generic_adb_failure_marks_outcome_unknown(tmp_path, monkeypatch):
    res, recs = _run_one(AdbFailureAdb(), FakePack(), tmp_path, monkeypatch)
    assert res.result == "exec_refused"
    assert recs[-1]["state"] == OUTCOME_UNKNOWN


# --------------------------------------------------------------------------- #
# D. Recovery decisions on a pre-existing open action (no model, no ADB replay).
# --------------------------------------------------------------------------- #
def _seed_open_action(db_path: Path, run_id: str, *, to_state: str, hook_recovery=HOOK_NOT_APPLICABLE) -> str:
    """Drive a real journal into an open ``to_state`` as the runner would."""
    j = SQLiteActionJournal(str(db_path))
    action_id = f"{run_id}:obs:1"
    intent = ActionIntent(
        run_id=run_id,
        action_id=action_id,
        semantic_target="t",
        action_type="click",
        execution_payload_hash="h",
        reconciliation_schema_version=_SCHEMA,
        hook_recovery=hook_recovery,
    )
    prepared = j.prepare(intent)
    rev = prepared.revision
    if to_state == PREPARED:
        j.close()
        return action_id
    rev = j.transition(action_id, expected_state=PREPARED, expected_revision=rev, new_state=DISPATCHING)
    if to_state == DISPATCHING:
        j.close()
        return action_id
    if to_state == EXECUTED:
        j.transition(action_id, expected_state=DISPATCHING, expected_revision=rev, new_state=EXECUTED)
        j.close()
        return action_id
    if to_state == OUTCOME_UNKNOWN:
        j.transition(action_id, expected_state=DISPATCHING, expected_revision=rev, new_state=OUTCOME_UNKNOWN)
        j.close()
        return action_id
    raise AssertionError(to_state)


def test_recovery_required_when_no_callback(tmp_path):
    db = tmp_path / "j.db"
    _seed_open_action(db, "r1", to_state=DISPATCHING)
    j = SQLiteActionJournal(str(db))
    adb = FakeAdb()
    res = runner.run(adb, FakeClient([]), ReconcilePack(), tmp_path, settle_s=0, journal=j, run_id="r1")
    assert res.result == "recovery_required"
    assert adb.tap_calls == 0  # ambiguous window NEVER re-dispatched
    assert len(res.recovery_reports) == 1
    j.close()


def test_recovery_manual_review_and_terminate(tmp_path):
    for decide, expected in ((MANUAL_REVIEW, "recovery_manual_review"), (TERMINATE, "recovery_terminated")):
        db = tmp_path / f"j-{expected}.db"
        _seed_open_action(db, "r1", to_state=DISPATCHING)
        j = SQLiteActionJournal(str(db))
        adb = FakeAdb()
        res = runner.run(
            adb, FakeClient([]), ReconcilePack(), tmp_path, settle_s=0,
            journal=j, run_id="r1", recovery_decide=lambda _r, d=decide: d,
        )
        assert res.result == expected
        assert adb.tap_calls == 0
        j.close()


def test_recovery_prepared_continue_closes_not_executed_then_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    db = tmp_path / "j.db"
    _seed_open_action(db, "r1", to_state=PREPARED)
    j = SQLiteActionJournal(str(db))
    adb = FakeAdb()
    # After recovery the loop runs; the model terminates immediately -> no dispatch.
    client = FakeClient([_tool_call({"action": "terminate", "status": "success"})])
    res = runner.run(adb, client, ReconcilePack(), tmp_path, settle_s=0,
                     journal=j, run_id="r1", recovery_decide=lambda _r: CONTINUE)
    assert res.result == "terminate_success"
    assert adb.tap_calls == 0
    assert _all_records(db)[0]["state"] == NOT_EXECUTED
    j.close()


def test_recovery_executed_idempotent_replays_hook_never_adb(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    db = tmp_path / "j.db"
    _seed_open_action(db, "r1", to_state=EXECUTED, hook_recovery=HOOK_IDEMPOTENT)
    j = SQLiteActionJournal(str(db))
    adb = FakeAdb()
    pack = HookReconcilePack()
    client = FakeClient([_tool_call({"action": "terminate", "status": "success"})])
    res = runner.run(adb, client, pack, tmp_path, settle_s=0,
                     journal=j, run_id="r1", recovery_decide=lambda _r: CONTINUE)
    assert res.result == "terminate_success"
    assert adb.tap_calls == 0  # ADB dispatch NEVER replayed
    assert pack.hook_replays == ["r1:obs:1"]  # the hook WAS replayed (idempotent)
    assert _all_records(db)[0]["state"] == FINALIZED
    j.close()


def test_recovery_outcome_unknown_reconcile_continue_finalizes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    db = tmp_path / "j.db"
    counter = tmp_path / "counter"
    counter.write_text("1")  # the device shows the effect WAS applied
    _seed_open_action(db, "r1", to_state=OUTCOME_UNKNOWN)
    j = SQLiteActionJournal(str(db))
    adb = FakeAdb()
    pack = ReconcilePack(counter_path=counter)
    client = FakeClient([_tool_call({"action": "terminate", "status": "success"})])
    res = runner.run(adb, client, pack, tmp_path, settle_s=0,
                     journal=j, run_id="r1", recovery_decide=lambda _r: CONTINUE)
    assert res.result == "terminate_success"
    assert adb.tap_calls == 0
    rec = _all_records(db)[0]
    assert rec["state"] == FINALIZED
    assert rec["reconciliation_verdict"] == ALREADY_APPLIED
    # append-only: a recovery observation ref was added, pre-exec refs intact.
    assert json.loads(rec["recovery_observation_refs"])
    j.close()


def test_recovery_continue_illegal_without_verdict(tmp_path):
    # No reconcile capability -> RECONCILIATION_UNAVAILABLE -> CONTINUE is illegal.
    db = tmp_path / "j.db"
    _seed_open_action(db, "r1", to_state=OUTCOME_UNKNOWN)
    j = SQLiteActionJournal(str(db))
    adb = FakeAdb()
    res = runner.run(adb, FakeClient([]), FakePack(), tmp_path, settle_s=0,
                     journal=j, run_id="r1", recovery_decide=lambda _r: CONTINUE)
    assert res.result == "invalid_recovery_decision"
    assert adb.tap_calls == 0
    j.close()


# --------------------------------------------------------------------------- #
# E. Concurrency: two Runners, same run_id, different action_id -> one prepare
#    wins; the loser fails closed with a stable conflict (never a second open).
# --------------------------------------------------------------------------- #
def test_one_open_action_per_run_enforced(tmp_path):
    db = tmp_path / "j.db"
    SQLiteActionJournal(str(db)).close()  # schema
    j1 = SQLiteActionJournal(str(db))
    j2 = SQLiteActionJournal(str(db))
    base = dict(run_id="r1", semantic_target="t", action_type="click", execution_payload_hash="h")
    j1.prepare(ActionIntent(action_id="r1:a", **base))
    with pytest.raises(ActiveActionError):
        j2.prepare(ActionIntent(action_id="r1:b", **base))
    # the first (winner) record is intact and still the only open one.
    recs = _all_records(db)
    assert len(recs) == 1
    assert recs[0]["action_id"] == "r1:a"
    j1.close()
    j2.close()


# --------------------------------------------------------------------------- #
# F. Real crash matrix: a real child process SIGKILLed at six seams; a durable
#    out-of-process dispatch counter proves recovery NEVER re-dispatches.
# --------------------------------------------------------------------------- #
# window -> (expected journal state after crash, expected dispatch count, hook?)
_WINDOWS = {
    "after_prepared": (PREPARED, 0, False),
    "after_dispatching": (DISPATCHING, 0, False),
    "during_adb": (DISPATCHING, 1, False),
    "before_executed": (DISPATCHING, 1, False),
    "after_executed": (EXECUTED, 1, False),
    "after_hook": (EXECUTED, 1, True),
}


@pytest.mark.parametrize("window", list(_WINDOWS))
def test_sigkill_window_is_never_auto_replayed(tmp_path, window, monkeypatch):
    expected_state, expected_count, with_hook = _WINDOWS[window]
    db = tmp_path / "crash.db"
    run_dir = tmp_path / "run"
    counter = tmp_path / "dispatch_count"
    marker = tmp_path / "marker"
    run_id = "crash-run"

    # 1) spawn a real child in its own session and let it self-SIGKILL at <window>.
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), window, str(db), str(run_dir),
         run_id, str(counter), str(marker), _SCHEMA],
        start_new_session=True,
    )
    proc.wait(timeout=60)
    assert proc.returncode != 0, "child must die by SIGKILL, not exit cleanly"
    assert marker.exists(), "child must have reached the fault window"

    # 2) the durable journal reflects exactly the crash seam.
    recs = _all_records(db)
    assert len(recs) == 1
    assert recs[0]["state"] == expected_state
    crash_count = _read_counter(counter)
    assert crash_count == expected_count

    # 3) a FRESH process' Runner recovers the same run_id. CONTINUE is chosen;
    #    the dispatch counter MUST NOT move (no auto-replay of the device action).
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    j = SQLiteActionJournal(str(db))
    adb = _CountingAdb(counter)  # would bump the counter if it ever tapped
    pack = HookReconcilePack(counter_path=counter) if with_hook else ReconcilePack(counter_path=counter)
    client = FakeClient([_tool_call({"action": "terminate", "status": "success"})])
    res = runner.run(adb, client, pack, run_dir, settle_s=0,
                     journal=j, run_id=run_id, recovery_decide=lambda _r: CONTINUE)
    j.close()

    assert _read_counter(counter) == crash_count, "recovery must NOT re-dispatch"
    assert adb.tap_calls == 0
    # PREPARED never dispatched -> the stale intent is discarded (NOT_EXECUTED);
    # every other seam already dispatched -> the action is closed FINALIZED.
    expected_final = NOT_EXECUTED if window == "after_prepared" else FINALIZED
    assert _all_records(db)[0]["state"] == expected_final
    assert res.result in ("terminate_success", "max_steps")


class _CountingAdb(FakeAdb):
    """Every tap bumps a durable, out-of-process counter (the crash proof)."""

    def __init__(self, counter_path: Path) -> None:
        super().__init__()
        self._counter_path = Path(counter_path)

    def tap(self, x: int, y: int) -> None:
        _bump_counter(self._counter_path)
        super().tap(x, y)


# --------------------------------------------------------------------------- #
# Crash-child entrypoint: run ONE real Runner step and self-SIGKILL at <window>.
# --------------------------------------------------------------------------- #
class _CrashJournal:
    """Wraps a real SQLiteActionJournal and self-SIGKILLs at a chosen seam
    (always AFTER the durable commit it is simulating a crash past, except
    ``before_executed`` which kills BEFORE the EXECUTED commit)."""

    def __init__(self, real: SQLiteActionJournal, window: str, marker: Path) -> None:
        self._real = real
        self._window = window
        self._marker = marker

    def _die(self) -> None:
        self._marker.write_text(self._window)
        os.fsync(os.open(str(self._marker), os.O_RDONLY))
        os.kill(os.getpid(), signal.SIGKILL)

    def prepare(self, intent):
        p = self._real.prepare(intent)
        if self._window == "after_prepared":
            self._die()
        return p

    def transition(self, action_id, *, expected_state, expected_revision, new_state, patch=None):
        if self._window == "before_executed" and new_state == EXECUTED:
            self._die()
        rev = self._real.transition(
            action_id, expected_state=expected_state, expected_revision=expected_revision,
            new_state=new_state, patch=patch,
        )
        if self._window == "after_dispatching" and new_state == DISPATCHING:
            self._die()
        if self._window == "after_executed" and new_state == EXECUTED:
            self._die()
        return rev

    def load_open_actions(self, run_id):
        return self._real.load_open_actions(run_id)

    def get_record(self, action_id):
        return self._real.get_record(action_id)

    def close(self):
        self._real.close()


class _CrashHookPack(HookReconcilePack):
    """HookReconcilePack whose hook self-SIGKILLs after running (window=after_hook)."""

    def __init__(self, window: str, marker: Path, counter_path: Path) -> None:
        super().__init__(counter_path=counter_path)
        self._window = window
        self._marker = marker

    def on_action_executed(self, *, executed, journaled_action, action_id, recovery):
        self.hook_replays.append(action_id)
        if self._window == "after_hook" and not recovery:
            self._marker.write_text(self._window)
            os.kill(os.getpid(), signal.SIGKILL)


def _crash_child_main(argv: list[str]) -> None:
    window, db, run_dir, run_id, counter, marker, schema = argv
    counter_path = Path(counter)
    marker_path = Path(marker)

    real = SQLiteActionJournal(db)
    journal = _CrashJournal(real, window, marker_path)
    adb = _CountingAdb(counter_path)
    if window in ("during_adb",):
        adb = _DieOnTapAdb(counter_path, marker_path, window)

    if window == "after_hook":
        pack: ReconcilePack = _CrashHookPack(window, marker_path, counter_path)
    else:
        pack = ReconcilePack(counter_path=counter_path)

    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    runner.run(adb, client, pack, Path(run_dir), settle_s=0, journal=journal, run_id=run_id)
    # Reaching here means the window never fired; signal that as a hard failure.
    sys.exit(0)


class _DieOnTapAdb(_CountingAdb):
    def __init__(self, counter_path: Path, marker: Path, window: str) -> None:
        super().__init__(counter_path)
        self._marker = Path(marker)
        self._window = window

    def tap(self, x: int, y: int) -> None:
        _bump_counter(self._counter_path)  # dispatch is durable BEFORE the crash
        self._marker.write_text(self._window)
        os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    _crash_child_main(sys.argv[1:])
