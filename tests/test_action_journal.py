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
import sqlite3
import subprocess
import sys
import threading
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
    NOT_APPLIED,
    NOT_EXECUTED,
    OUTCOME_UNKNOWN,
    PREPARED,
    UNKNOWN,
    ActionIntent,
    ActionJournalError,
    ActiveActionError,
    DuplicateActionError,
    IllegalTransitionError,
    ImmutableFieldError,
    JournalBusyError,
    JournalCorruptError,
    RevisionConflictError,
    RunIdInvalidError,
    SchemaVersionError,
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
        "a", expected_state=EXECUTED, expected_revision=2, new_state=FINALIZED,
        patch={"hook_applied": False},
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

# --- state-specific patch constraints (review 1625 blocker 1) ----------------


def _to_outcome_unknown(journal: SQLiteActionJournal, aid: str) -> None:
    journal.prepare(_intent(action_id=aid))
    journal.transition(
        aid, expected_state=PREPARED, expected_revision=0, new_state=DISPATCHING
    )
    journal.transition(
        aid, expected_state=DISPATCHING, expected_revision=1,
        new_state=OUTCOME_UNKNOWN,
    )


def test_outcome_unknown_needs_concrete_verdict_to_finalize(
    journal: SQLiteActionJournal,
) -> None:
    _to_outcome_unknown(journal, "a")
    with pytest.raises(ActionJournalError):  # no verdict
        journal.transition(
            "a", expected_state=OUTCOME_UNKNOWN, expected_revision=2,
            new_state=FINALIZED,
        )
    with pytest.raises(ActionJournalError):  # UNKNOWN cannot close
        journal.transition(
            "a", expected_state=OUTCOME_UNKNOWN, expected_revision=2,
            new_state=FINALIZED, patch={"reconciliation_verdict": UNKNOWN},
        )
    assert journal.get_record("a").state == OUTCOME_UNKNOWN
    # UNKNOWN may only keep it open (self-loop)
    journal.transition(
        "a", expected_state=OUTCOME_UNKNOWN, expected_revision=2,
        new_state=OUTCOME_UNKNOWN, patch={"reconciliation_verdict": UNKNOWN},
    )
    # a concrete verdict closes it
    journal.transition(
        "a", expected_state=OUTCOME_UNKNOWN, expected_revision=3,
        new_state=FINALIZED, patch={"reconciliation_verdict": NOT_APPLIED},
    )
    assert journal.get_record("a").state == FINALIZED


def test_verdict_forbidden_on_non_reconcile_transition(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    with pytest.raises(ActionJournalError):
        journal.transition(
            "a", expected_state=PREPARED, expected_revision=0,
            new_state=DISPATCHING,
            patch={"reconciliation_verdict": ALREADY_APPLIED},
        )
    assert journal.get_record("a").state == PREPARED


def test_hook_applied_must_be_strict_bool(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a", hook_recovery=HOOK_IDEMPOTENT))
    journal.transition(
        "a", expected_state=PREPARED, expected_revision=0, new_state=DISPATCHING
    )
    journal.transition(
        "a", expected_state=DISPATCHING, expected_revision=1, new_state=EXECUTED
    )
    for bad in ("false", 1, 0, None):
        with pytest.raises(ActionJournalError):
            journal.transition(
                "a", expected_state=EXECUTED, expected_revision=2,
                new_state=FINALIZED, patch={"hook_applied": bad},
            )
    assert journal.get_record("a").state == EXECUTED
    journal.transition(
        "a", expected_state=EXECUTED, expected_revision=2,
        new_state=FINALIZED, patch={"hook_applied": True},
    )
    assert journal.get_record("a").hook_applied is True


def test_hook_applied_only_on_executed_finalize(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    with pytest.raises(ActionJournalError):
        journal.transition(
            "a", expected_state=PREPARED, expected_revision=0,
            new_state=DISPATCHING, patch={"hook_applied": True},
        )


def test_recovery_refs_must_be_list(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    with pytest.raises(ActionJournalError):
        journal.transition(
            "a", expected_state=PREPARED, expected_revision=0,
            new_state=DISPATCHING, patch={"recovery_observation_refs": "x"},
        )


# --- unified transaction: no leaked write txn (review 1625 blocker 2) --------


def test_corrupt_refs_convert_to_journal_corrupt_without_leaking_txn(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    # corrupt the persisted recovery refs to invalid JSON
    journal._conn.execute(
        "UPDATE actions SET recovery_observation_refs='{bad' WHERE action_id=?",
        ("a",),
    )
    with pytest.raises(JournalCorruptError):
        journal.transition(
            "a", expected_state=PREPARED, expected_revision=0,
            new_state=DISPATCHING,
            patch={"recovery_observation_refs": ["y"]},
        )
    assert journal._conn.in_transaction is False  # rolled back, not locked


# --- integrity (review 1625 blocker 3) ---------------------------------------


def test_integrity_detects_revision_gap(journal: SQLiteActionJournal) -> None:
    journal.prepare(_intent(action_id="a"))
    journal._conn.execute(
        "INSERT INTO action_transitions "
        "(action_id, revision, from_state, to_state, at) VALUES (?,?,?,?,?)",
        ("a", 5, PREPARED, DISPATCHING, "t"),
    )
    with pytest.raises(JournalCorruptError):
        journal.verify_integrity("a")


def test_integrity_detects_main_record_state_mismatch(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    journal._conn.execute(
        "UPDATE actions SET state='DISPATCHING' WHERE action_id=?", ("a",)
    )
    with pytest.raises(JournalCorruptError):
        journal.verify_integrity("a")


def test_load_open_actions_rejects_corrupt_record(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    journal._conn.execute(
        "UPDATE actions SET revision=9 WHERE action_id=?", ("a",)
    )
    with pytest.raises(JournalCorruptError):
        journal.load_open_actions("run-1")


# --- schema version ----------------------------------------------------------


def test_schema_version_mismatch_refused(tmp_path: Path) -> None:
    db = str(tmp_path / "journal.db")
    SQLiteActionJournal(db).close()
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version=999")
    raw.close()
    with pytest.raises(SchemaVersionError):
        SQLiteActionJournal(db)


# --- intent input validation -------------------------------------------------


def test_intent_rejects_blank_required_fields(
    journal: SQLiteActionJournal,
) -> None:
    for kw in (
        {"semantic_target": ""},
        {"action_type": "  "},
        {"execution_payload_hash": ""},
    ):
        with pytest.raises(ActionJournalError):
            journal.prepare(_intent(**kw))


def test_intent_rejects_non_list_refs(journal: SQLiteActionJournal) -> None:
    with pytest.raises(ActionJournalError):
        journal.prepare(
            _intent(pre_execution_observation_refs="not-a-list")
        )


def test_intent_rejects_non_dict_context(journal: SQLiteActionJournal) -> None:
    with pytest.raises(ActionJournalError):
        journal.prepare(_intent(reconciliation_context=[1, 2]))


# --- persisted JSON corruption surfaces JOURNAL_CORRUPT (review 1646 #1) -----


def test_corrupt_context_reads_as_journal_corrupt(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a", reconciliation_context={"k": "v"}))
    journal._conn.execute(
        "UPDATE actions SET reconciliation_context='{bad' WHERE action_id=?",
        ("a",),
    )
    with pytest.raises(JournalCorruptError):
        journal.get_record("a")
    with pytest.raises(JournalCorruptError):
        journal.load_open_actions("run-1")


def test_corrupt_pre_refs_reads_as_journal_corrupt(
    journal: SQLiteActionJournal,
) -> None:
    journal.prepare(_intent(action_id="a"))
    journal._conn.execute(
        "UPDATE actions SET pre_execution_observation_refs='{bad' "
        "WHERE action_id=?",
        ("a",),
    )
    with pytest.raises(JournalCorruptError):
        journal.get_record("a")
    with pytest.raises(JournalCorruptError):
        journal.build_recovery_report("a")


# --- hook_recovery / hook_applied close consistency (review 1646 #3) ---------


def _to_executed(journal: SQLiteActionJournal, aid: str, hook: str) -> None:
    journal.prepare(_intent(action_id=aid, hook_recovery=hook))
    journal.transition(
        aid, expected_state=PREPARED, expected_revision=0, new_state=DISPATCHING
    )
    journal.transition(
        aid, expected_state=DISPATCHING, expected_revision=1, new_state=EXECUTED
    )


def test_not_applicable_close_requires_false(
    journal: SQLiteActionJournal,
) -> None:
    from mano.action_journal import HOOK_NOT_APPLICABLE

    _to_executed(journal, "a", HOOK_NOT_APPLICABLE)
    with pytest.raises(ActionJournalError):  # no hook fact
        journal.transition(
            "a", expected_state=EXECUTED, expected_revision=2, new_state=FINALIZED
        )
    with pytest.raises(ActionJournalError):  # cannot claim a hook ran
        journal.transition(
            "a", expected_state=EXECUTED, expected_revision=2,
            new_state=FINALIZED, patch={"hook_applied": True},
        )
    assert journal.get_record("a").state == EXECUTED
    journal.transition(
        "a", expected_state=EXECUTED, expected_revision=2,
        new_state=FINALIZED, patch={"hook_applied": False},
    )
    assert journal.get_record("a").state == FINALIZED


def test_declared_hook_close_requires_true(
    journal: SQLiteActionJournal,
) -> None:
    _to_executed(journal, "a", HOOK_IDEMPOTENT)
    with pytest.raises(ActionJournalError):  # missing hook fact
        journal.transition(
            "a", expected_state=EXECUTED, expected_revision=2, new_state=FINALIZED
        )
    with pytest.raises(ActionJournalError):  # a declared hook must have run
        journal.transition(
            "a", expected_state=EXECUTED, expected_revision=2,
            new_state=FINALIZED, patch={"hook_applied": False},
        )
    journal.transition(
        "a", expected_state=EXECUTED, expected_revision=2,
        new_state=FINALIZED, patch={"hook_applied": True},
    )
    assert journal.get_record("a").hook_applied is True


# --- schema version checked before any DDL (review 1646 #2) ------------------


def test_unmarked_nonempty_db_refused_without_ddl(tmp_path: Path) -> None:
    db = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE legacy(x)")
    raw.execute("PRAGMA user_version=0")
    before = raw.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    raw.close()
    with pytest.raises(SchemaVersionError):
        SQLiteActionJournal(db)
    raw = sqlite3.connect(db)
    after = raw.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    raw.close()
    assert before == after  # refusal must not run any DDL


def test_v1_missing_required_table_is_corrupt(tmp_path: Path) -> None:
    db = str(tmp_path / "j.db")
    SQLiteActionJournal(db).close()
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE action_transitions")
    raw.close()
    with pytest.raises(JournalCorruptError):
        SQLiteActionJournal(db)


def test_v1_missing_column_is_corrupt(tmp_path: Path) -> None:
    db = str(tmp_path / "j.db")
    SQLiteActionJournal(db).close()
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE actions DROP COLUMN foreground")  # same name, weaker
    raw.close()
    with pytest.raises(JournalCorruptError):
        SQLiteActionJournal(db)


def test_v1_index_without_unique_partial_is_corrupt(tmp_path: Path) -> None:
    db = str(tmp_path / "j.db")
    SQLiteActionJournal(db).close()
    raw = sqlite3.connect(db)
    raw.execute("DROP INDEX idx_one_open_per_run")
    raw.execute("CREATE INDEX idx_one_open_per_run ON actions(run_id)")
    raw.close()
    with pytest.raises(JournalCorruptError):
        SQLiteActionJournal(db)


def test_schema_init_failure_leaves_no_half_built_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "j.db")
    real_create = SQLiteActionJournal._create_schema

    def boom(self: SQLiteActionJournal) -> None:
        real_create(self)  # tables exist inside the txn...
        raise RuntimeError("crash before the version stamp commits")

    monkeypatch.setattr(SQLiteActionJournal, "_create_schema", boom)
    with pytest.raises(RuntimeError):
        SQLiteActionJournal(db)
    monkeypatch.undo()
    # the aborted txn must roll back fully: no "tables exist but version=0"
    probe = sqlite3.connect(db)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 0
        assert probe.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] == 0
    finally:
        probe.close()
    # and a fresh open now succeeds as a brand-new database
    j = SQLiteActionJournal(db)
    try:
        assert j._conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        j.close()


# --- real lock contention maps to DB_BUSY (review 1711 #1) -------------------


def test_write_under_lock_maps_to_db_busy(tmp_path: Path) -> None:
    db = str(tmp_path / "j.db")
    seed = SQLiteActionJournal(db)
    seed.prepare(_intent(action_id="x"))
    seed.close()
    holder = SQLiteActionJournal(db)
    contender = SQLiteActionJournal(db, busy_timeout_ms=0)
    try:
        holder._conn.execute("BEGIN IMMEDIATE")  # hold the write lock
        with pytest.raises(JournalBusyError) as prep:
            contender.prepare(_intent(run_id="run-2", action_id="y"))
        assert prep.value.code == "DB_BUSY"
        with pytest.raises(JournalBusyError) as trans:
            contender.transition(
                "x", expected_state=PREPARED, expected_revision=0,
                new_state=DISPATCHING,
            )
        assert trans.value.code == "DB_BUSY"
        assert contender._conn.in_transaction is False  # no leaked txn
    finally:
        holder._conn.execute("ROLLBACK")
        holder.close()
        contender.close()

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


def test_concurrent_prepare_exactly_one_wins(tmp_path: Path) -> None:
    db = str(tmp_path / "journal.db")
    SQLiteActionJournal(db).close()  # create schema once, before the race
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def worker(aid: str) -> None:
        j = SQLiteActionJournal(db)
        try:
            barrier.wait()
            j.prepare(_intent(run_id="run-c", action_id=aid))
            results[aid] = "ok"
        except ActiveActionError:
            results[aid] = "rejected"
        except Exception as exc:  # pragma: no cover - surfaces unexpected races
            results[aid] = f"err:{exc!r}"
        finally:
            j.close()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert sorted(results.values()) == ["ok", "rejected"], results
    winner = SQLiteActionJournal(db)
    try:
        assert len(winner.load_open_actions("run-c")) == 1
    finally:
        winner.close()
