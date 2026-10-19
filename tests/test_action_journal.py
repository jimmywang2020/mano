"""Storage-layer tests for the P0 action journal (Commit A).

No Runner, no ADB, no hook here: these exercise the SQLite schema, the atomic
``prepare()`` / CAS ``transition()`` contract, the "at most one open action per
run" guard, append-only recovery refs, corruption detection, and durability of a
committed transition across a real subprocess + ``SIGKILL``.

The full ADB/hook crash-window matrix (ADB-call SIGKILL, EXECUTED-before-hook,
hook-before-FINALIZED, Consumer decisions, no-double-dispatch counter) belongs to
Commit B once the Runner is wired; it is intentionally absent here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mano.action_journal import (
    ALREADY_APPLIED,
    AMBIGUOUS_EFFECT,
    CERTAINTY_CONFIRMED,
    CERTAINTY_NONE,
    CERTAINTY_POSSIBLE,
    DISPATCHING,
    EXECUTED,
    EXECUTION_CONFIRMED,
    FINALIZED,
    HOOK_IDEMPOTENT,
    NOT_EXECUTED,
    OUTCOME_UNKNOWN,
    PREPARED,
    ActionIntent,
    ActiveActionError,
    DuplicateActionError,
    IllegalTransitionError,
    ImmutableFieldError,
    JournalCorruptError,
    RevisionConflictError,
    RunIdInvalidError,
    SQLiteActionJournal,
)


def _intent(run_id: str = "run-1", action_id: str = "act-1", **kw) -> ActionIntent:
    base = dict(
        run_id=run_id,
        action_id=action_id,
        semantic_target="filter:price_low_to_high",
        action_type="click",
        execution_payload_hash="payload-hash",
    )
    base.update(kw)
    return ActionIntent(**base)


@pytest.fixture
def journal(tmp_path: Path) -> SQLiteActionJournal:
    j = SQLiteActionJournal(str(tmp_path / "journal.db"))
    yield j
    j.close()


# --- schema / pragmas --------------------------------------------------------


def test_pragmas_wal_and_full(journal: SQLiteActionJournal) -> None:
    mode = journal._conn.execute("PRAGMA journal_mode").fetchone()[0]
    sync = journal._conn.execute("PRAGMA synchronous").fetchone()[0]
    assert mode.lower() == "wal"
    assert sync == 2  # FULL


# --- prepare() ---------------------------------------------------------------


def test_prepare_creates_durable_prepared(journal: SQLiteActionJournal) -> None:
    prepared = journal.prepare(_intent())
    assert prepared.state == PREPARED
    assert prepared.action_sequence == 1
    assert prepared.revision == 0
    rec = journal.get_record("act-1")
    assert rec.state == PREPARED
    assert rec.dispatch_certainty == CERTAINTY_NONE
    assert rec.recovery_observation_refs == []


def test_prepare_rejects_blank_run_id(journal: SQLiteActionJournal) -> None:
    for bad in ("", "   ", "\t"):
        with pytest.raises(RunIdInvalidError):
            journal.prepare(_intent(run_id=bad))


def test_prepare_rejects_duplicate_action_id(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="dup"))
    journal.transition(
        "dup", expected_state=PREPARED, expected_revision=0, new_state=NOT_EXECUTED
    )
    with pytest.raises(DuplicateActionError):
        journal.prepare(_intent(action_id="dup", run_id="run-other"))


def test_at_most_one_open_action_per_run(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    with pytest.raises(ActiveActionError):
        journal.prepare(_intent(action_id="b"))


def test_sequence_increments_after_terminal_close(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=NOT_EXECUTED
    )
    prepared_b = journal.prepare(_intent(action_id="b"))
    assert prepared_b.action_sequence == 2


def test_second_open_run_isolated(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(run_id="run-A", action_id="a"))
    # a different run may open its own action concurrently
    prepared = journal.prepare(_intent(run_id="run-B", action_id="b"))
    assert prepared.action_sequence == 1
    assert len(journal.load_open_actions("run-A")) == 1
    assert len(journal.load_open_actions("run-B")) == 1


# --- transition() CAS + certainty --------------------------------------------


def test_happy_path_certainty_lattice(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    r1 = journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=DISPATCHING
    )
    assert r1 == 1
    assert journal.get_record("a").dispatch_certainty == CERTAINTY_POSSIBLE
    r2 = journal.transition(
        "a", expected_state=DISPATCHING, expected_revision=1, new_state=EXECUTED
    )
    assert r2 == 2
    assert journal.get_record("a").dispatch_certainty == CERTAINTY_CONFIRMED
    r3 = journal.transition(
        "a", expected_state=EXECUTED, expected_revision=2, new_state=FINALIZED
    )
    assert r3 == 3
    rec = journal.get_record("a")
    assert rec.state == FINALIZED
    assert rec.dispatch_certainty == CERTAINTY_CONFIRMED  # inherited


def test_cas_conflict_leaves_record_unchanged(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    with pytest.raises(RevisionConflictError):
        journal.transition(
            "a", expected_state=PREPARED, expected_revision=99, new_state=DISPATCHING
        )
    rec = journal.get_record("a")
    assert rec.state == PREPARED
    assert rec.revision == 0


def test_illegal_transition_rejected(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    with pytest.raises(IllegalTransitionError):
        journal.transition(
            "a", expected_state=PREPARED, expected_revision=0, new_state=EXECUTED
        )


def test_immutable_fields_not_patchable(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    for bad in ("run_id", "semantic_target", "dispatch_certainty", "action_sequence"):
        with pytest.raises(ImmutableFieldError):
            journal.transition(
                "a",
                expected_state=PREPARED,
                expected_revision=0,
                new_state=DISPATCHING,
                patch={bad: "x"},
            )
    assert journal.get_record("a").state == PREPARED


def test_not_executed_only_resets_certainty_pre_dispatch(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=NOT_EXECUTED
    )
    rec = journal.get_record("a")
    assert rec.state == NOT_EXECUTED
    assert rec.dispatch_certainty == CERTAINTY_NONE


# --- reconciliation never rewrites dispatch_certainty ------------------------


def test_reconciliation_keeps_possible_on_close(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=DISPATCHING
    )
    journal.transition(
        "a", expected_state=DISPATCHING, expected_revision=1, new_state=OUTCOME_UNKNOWN
    )
    assert journal.get_record("a").dispatch_certainty == CERTAINTY_POSSIBLE
    journal.transition(
        "a",
        expected_state=OUTCOME_UNKNOWN,
        expected_revision=2,
        new_state=FINALIZED,
        patch={"reconciliation_verdict": ALREADY_APPLIED},
    )
    rec = journal.get_record("a")
    assert rec.state == FINALIZED
    assert rec.reconciliation_verdict == ALREADY_APPLIED
    assert rec.dispatch_certainty == CERTAINTY_POSSIBLE  # never raised to CONFIRMED


# --- append-only recovery refs ----------------------------------------------


def test_recovery_refs_append_never_overwrite(journal: SQLiteActionJournal) -> None:
    journal.prepare(
        _intent(action_id="a", pre_execution_observation_refs=["pre-1"])
    )
    journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=DISPATCHING
    )
    journal.transition(
        "a",
        expected_state=DISPATCHING,
        expected_revision=1,
        new_state=OUTCOME_UNKNOWN,
        patch={"recovery_observation_refs": ["rec-1"]},
    )
    journal.transition(
        "a",
        expected_state=OUTCOME_UNKNOWN,
        expected_revision=2,
        new_state=OUTCOME_UNKNOWN,
        patch={"recovery_observation_refs": ["rec-2"]},
    )
    rec = journal.get_record("a")
    assert rec.pre_execution_observation_refs == ["pre-1"]  # untouched
    assert rec.recovery_observation_refs == ["rec-1", "rec-2"]  # appended


# --- load_open_actions -------------------------------------------------------


def test_load_open_actions_excludes_terminal(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=NOT_EXECUTED
    )
    assert journal.load_open_actions("run-1") == []
    journal.prepare(_intent(action_id="b"))
    opens = journal.load_open_actions("run-1")
    assert [r.action_id for r in opens] == ["b"]
    assert opens[0].state == PREPARED


# --- recovery report ---------------------------------------------------------


def test_recovery_report_carries_cas_identity(journal: SQLiteActionJournal) -> None:
    journal.prepare(
        _intent(action_id="a", pre_execution_observation_refs=["pre-1"])
    )
    journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=DISPATCHING
    )
    report = journal.build_recovery_report("a")
    assert report.action_id == "a"
    assert report.action_sequence == 1
    assert report.revision == 1  # for Consumer CAS
    assert report.core_fact == AMBIGUOUS_EFFECT
    assert report.dispatch_certainty == CERTAINTY_POSSIBLE
    assert report.pre_execution_observation_refs == ["pre-1"]
    assert report.recovery_observation_refs == []


def test_recovery_report_executed_is_confirmed(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a", hook_recovery=HOOK_IDEMPOTENT))
    journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=DISPATCHING
    )
    journal.transition(
        "a", expected_state=DISPATCHING, expected_revision=1, new_state=EXECUTED
    )
    report = journal.build_recovery_report("a")
    assert report.core_fact == EXECUTION_CONFIRMED
    assert report.dispatch_certainty == CERTAINTY_CONFIRMED


# --- corruption detection ----------------------------------------------------


def test_verify_integrity_flags_illegal_chain(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    journal.verify_integrity("a")  # legal so far
    # inject an illegal persisted adjacency directly
    journal._conn.execute(
        "INSERT INTO action_transitions "
        "(action_id, revision, from_state, to_state, at) VALUES (?,?,?,?,?)",
        ("a", 1, PREPARED, EXECUTED, "2026-01-01T00:00:00+00:00"),
    )
    with pytest.raises(JournalCorruptError):
        journal.verify_integrity("a")


def test_row_with_unknown_state_is_corrupt(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    # bypass the CHECK by disabling it is not possible; simulate a corrupt read
    # by asserting get_record tolerates only known states.
    with pytest.raises(JournalCorruptError):
        journal._row_to_record(
            (
                "run-1",
                1,
                "a",
                0,
                "WAT",  # unknown state
                CERTAINTY_NONE,
                None,
                None,
                "NOT_APPLICABLE",
                None,
                None,
                None,
                "t",
                "click",
                "h",
                None,
                "[]",
                "[]",
                None,
                "t0",
                "t0",
            )
        )


# --- cross-process durability across SIGKILL ---------------------------------


_CHILD = """
import sys, time
from mano.action_journal import (
    SQLiteActionJournal, ActionIntent, PREPARED, DISPATCHING,
)
db, run_id, aid = sys.argv[1], sys.argv[2], sys.argv[3]
j = SQLiteActionJournal(db)
j.prepare(ActionIntent(
    run_id=run_id, action_id=aid,
    semantic_target="t", action_type="click", execution_payload_hash="h",
))
j.transition(aid, expected_state=PREPARED, expected_revision=0,
             new_state=DISPATCHING)
print("READY", flush=True)
time.sleep(60)
"""


def test_committed_transition_survives_sigkill(tmp_path: Path) -> None:
    db = str(tmp_path / "journal.db")
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, db, "run-k", "act-k"],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        line = proc.stdout.readline()
        assert line.strip() == "READY", f"child did not reach READY: {line!r}"
        proc.kill()  # SIGKILL
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    journal = SQLiteActionJournal(db)
    try:
        opens = journal.load_open_actions("run-k")
        assert len(opens) == 1
        rec = opens[0]
        # the committed DISPATCHING survived; the never-reached EXECUTED is absent
        assert rec.state == DISPATCHING
        assert rec.dispatch_certainty == CERTAINTY_POSSIBLE
        assert rec.action_id == "act-k"
    finally:
        journal.close()


def test_two_journals_same_path_one_open_wins(tmp_path: Path) -> None:
    db = str(tmp_path / "journal.db")
    j1 = SQLiteActionJournal(db)
    j2 = SQLiteActionJournal(db)
    try:
        j1.prepare(_intent(run_id="run-x", action_id="a"))
        # a second journal (separate connection) racing the same run with a
        # different action_id must be rejected by the DB-level guard
        with pytest.raises(ActiveActionError):
            j2.prepare(_intent(run_id="run-x", action_id="b"))
        assert len(j2.load_open_actions("run-x")) == 1
    finally:
        j1.close()
        j2.close()
