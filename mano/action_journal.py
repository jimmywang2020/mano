"""Durable per-action journal (P0) — SQLite reference + storage-layer protocol.

Commit A scope (see docs/action-journal-design.md): schema / transactions / CAS /
stable error codes / ``RecoveryReport`` + ``RecoveryDecision`` vocabulary /
cross-process durability. The Runner is **not** wired here — the journal stays an
injectable optional dependency, so ``runner.run(journal=None)`` is unchanged. The
full ADB/hook crash-window matrix lands in Commit B when the Runner is wired.

Guarantees provided by this layer:

- ``prepare()`` is a single ``BEGIN IMMEDIATE`` transaction that atomically
  verifies the run has no open action, assigns ``action_sequence`` and inserts a
  durable ``PREPARED`` row. The DB additionally enforces *at most one open action
  per run* via a partial unique index, so two Runners cannot both proceed.
- ``transition()`` is a compare-and-set on ``(expected_state, expected_revision)``
  and only permits the legal state graph; ``dispatch_certainty`` is derived by
  Core transitions alone and is never patchable by a caller.
- ``WAL`` + ``synchronous=FULL`` promise process-crash / SIGKILL recovery (not an
  absolute guarantee against arbitrary hardware power-loss).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .errors import ManoError

# --- State vocabulary (mirrors the CHECK constraints in the schema) ----------

PREPARED = "PREPARED"
DISPATCHING = "DISPATCHING"
EXECUTED = "EXECUTED"
FINALIZED = "FINALIZED"
NOT_EXECUTED = "NOT_EXECUTED"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

ALL_STATES = frozenset(
    {PREPARED, DISPATCHING, EXECUTED, FINALIZED, NOT_EXECUTED, OUTCOME_UNKNOWN}
)
OPEN_STATES = frozenset({PREPARED, DISPATCHING, EXECUTED, OUTCOME_UNKNOWN})
TERMINAL_STATES = frozenset({FINALIZED, NOT_EXECUTED})

LEGAL_TRANSITIONS = frozenset(
    {
        (PREPARED, DISPATCHING),
        (PREPARED, NOT_EXECUTED),
        (DISPATCHING, EXECUTED),
        (DISPATCHING, NOT_EXECUTED),
        (DISPATCHING, OUTCOME_UNKNOWN),
        (EXECUTED, FINALIZED),
        (OUTCOME_UNKNOWN, FINALIZED),
        (OUTCOME_UNKNOWN, OUTCOME_UNKNOWN),
    }
)

# dispatch_certainty — Core-owned; never patchable by a caller.
CERTAINTY_NONE = "NONE"
CERTAINTY_POSSIBLE = "POSSIBLE"
CERTAINTY_CONFIRMED = "CONFIRMED"
ALL_CERTAINTY = frozenset({CERTAINTY_NONE, CERTAINTY_POSSIBLE, CERTAINTY_CONFIRMED})

# reconciliation_verdict — a separate semantic fact (Pack-produced).
ALREADY_APPLIED = "ALREADY_APPLIED"
NOT_APPLIED = "NOT_APPLIED"
UNKNOWN = "UNKNOWN"
ALL_VERDICTS = frozenset({ALREADY_APPLIED, NOT_APPLIED, UNKNOWN})

# hook_recovery — Pack-declared capability.
HOOK_IDEMPOTENT = "IDEMPOTENT"
HOOK_NOT_REPLAYABLE = "NOT_REPLAYABLE"
HOOK_NOT_APPLICABLE = "NOT_APPLICABLE"
ALL_HOOK_RECOVERY = frozenset(
    {HOOK_IDEMPOTENT, HOOK_NOT_REPLAYABLE, HOOK_NOT_APPLICABLE}
)

# core_fact — the journal-derivable subset. Pack-dependent facts
# (RECONCILIATION_UNAVAILABLE / PACK_INCOMPATIBLE) are filled by the Runner in
# Commit B; they are named here only so the vocabulary lives in one place.
NO_PENDING_ACTION = "NO_PENDING_ACTION"
SAFE_TO_REOBSERVE = "SAFE_TO_REOBSERVE"
EXECUTION_CONFIRMED = "EXECUTION_CONFIRMED"
AMBIGUOUS_EFFECT = "AMBIGUOUS_EFFECT"
RECONCILIATION_UNAVAILABLE = "RECONCILIATION_UNAVAILABLE"
PACK_INCOMPATIBLE = "PACK_INCOMPATIBLE"
JOURNAL_CORRUPT = "JOURNAL_CORRUPT"

# RecoveryDecision — Consumer policy; the close-policy rules are enforced by the
# Runner in Commit B. Named here so Core/Pack/Consumer share one spelling.
CONTINUE = "CONTINUE"
REPLAN = "REPLAN"
MANUAL_REVIEW = "MANUAL_REVIEW"
TERMINATE = "TERMINATE"
ALL_DECISIONS = frozenset({CONTINUE, REPLAN, MANUAL_REVIEW, TERMINATE})

# Fields a transition() patch may touch. Everything else (identity + payload hash
# + dispatch_certainty + revision + state) is immutable to a caller: state and
# revision are managed by the CAS itself, dispatch_certainty is derived by Core.
_PATCHABLE = frozenset(
    {"reconciliation_verdict", "hook_applied", "recovery_observation_refs"}
)

_CONTEXT_MAX_BYTES = 8192
_OPEN_SQL = ",".join(f"'{s}'" for s in sorted(OPEN_STATES))
_RECORD_COLS = (
    "run_id, action_sequence, action_id, revision, state, dispatch_certainty, "
    "reconciliation_verdict, hook_applied, hook_recovery, pack_id, pack_digest, "
    "reconciliation_schema_version, semantic_target, action_type, "
    "execution_payload_hash, reconciliation_context, "
    "pre_execution_observation_refs, recovery_observation_refs, foreground, "
    "created_at, updated_at"
)


# --- Errors — one stable ``.code`` per distinguishable failure ---------------


class ActionJournalError(ManoError):
    """Base for journal failures; ``code`` is a stable machine-readable token."""

    code = "JOURNAL_ERROR"


class RunIdInvalidError(ActionJournalError):
    code = "RUN_ID_INVALID"


class DuplicateActionError(ActionJournalError):
    code = "DUPLICATE_ACTION_ID"


class ActiveActionError(ActionJournalError):
    code = "ACTIVE_ACTION_CONFLICT"


class RevisionConflictError(ActionJournalError):
    code = "CAS_CONFLICT"


class IllegalTransitionError(ActionJournalError):
    code = "ILLEGAL_TRANSITION"


class ImmutableFieldError(ActionJournalError):
    code = "IMMUTABLE_FIELD"


class JournalBusyError(ActionJournalError):
    code = "DB_BUSY"


class JournalCorruptError(ActionJournalError):
    code = "JOURNAL_CORRUPT"


# --- Value objects -----------------------------------------------------------


@dataclass(frozen=True)
class ActionIntent:
    """Prepare-time input. ``action_sequence`` / ``revision`` / ``state`` are
    assigned by the journal, never by the caller."""

    run_id: str
    action_id: str
    semantic_target: str
    action_type: str
    execution_payload_hash: str
    pack_id: str | None = None
    pack_digest: str | None = None
    reconciliation_schema_version: str | None = None
    reconciliation_context: dict[str, Any] | None = None
    pre_execution_observation_refs: list[Any] = field(default_factory=list)
    foreground: str | None = None
    hook_recovery: str = HOOK_NOT_APPLICABLE


@dataclass(frozen=True)
class PreparedAction:
    run_id: str
    action_id: str
    action_sequence: int
    revision: int
    state: str


@dataclass(frozen=True)
class ActionRecord:
    run_id: str
    action_sequence: int
    action_id: str
    revision: int
    state: str
    dispatch_certainty: str
    reconciliation_verdict: str | None
    hook_applied: bool | None
    hook_recovery: str
    pack_id: str | None
    pack_digest: str | None
    reconciliation_schema_version: str | None
    semantic_target: str
    action_type: str
    execution_payload_hash: str
    reconciliation_context: dict[str, Any] | None
    pre_execution_observation_refs: list[Any]
    recovery_observation_refs: list[Any]
    foreground: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RecoveryReport:
    """Facts-only report the Runner hands to the Consumer's ``recovery_decide``.

    ``revision`` is included so the Consumer's decision is applied via CAS and two
    recovery processes cannot both act on the same stale report. Pre-execution and
    recovery evidence stay separate so their provenance is never conflated.
    """

    run_id: str
    action_sequence: int
    action_id: str
    revision: int
    core_fact: str
    dispatch_certainty: str
    reconciliation_verdict: str | None
    hook_applied: bool | None
    pre_execution_observation_refs: list[Any]
    recovery_observation_refs: list[Any]


def core_fact_from_state(state: str) -> str:
    """The journal-only core fact for a persisted state (no Pack input)."""
    if state in (PREPARED, NOT_EXECUTED):
        return SAFE_TO_REOBSERVE
    if state == EXECUTED:
        return EXECUTION_CONFIRMED
    if state in (DISPATCHING, OUTCOME_UNKNOWN):
        return AMBIGUOUS_EFFECT
    if state == FINALIZED:
        return NO_PENDING_ACTION
    raise JournalCorruptError(f"unknown state: {state!r}")


def _certainty_for(new_state: str, current: str) -> str:
    """Core-owned dispatch_certainty derivation along the legal lattice."""
    if new_state == DISPATCHING or new_state == OUTCOME_UNKNOWN:
        return CERTAINTY_POSSIBLE
    if new_state == EXECUTED:
        return CERTAINTY_CONFIRMED
    if new_state == NOT_EXECUTED:
        return CERTAINTY_NONE
    if new_state == FINALIZED:
        return current  # safely-closed: inherit whatever certainty was proven
    return current


@runtime_checkable
class ActionJournal(Protocol):
    def prepare(self, intent: ActionIntent) -> PreparedAction: ...

    def transition(
        self,
        action_id: str,
        *,
        expected_state: str,
        expected_revision: int,
        new_state: str,
        patch: dict[str, Any] | None = None,
    ) -> int: ...

    def load_open_actions(self, run_id: str) -> list[ActionRecord]: ...


# --- Helpers -----------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_context(ctx: dict[str, Any] | None) -> str | None:
    if ctx is None:
        return None
    try:
        text = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ActionJournalError(
            f"reconciliation_context not JSON-serializable: {exc}"
        ) from exc
    if len(text.encode("utf-8")) > _CONTEXT_MAX_BYTES:
        raise ActionJournalError("reconciliation_context exceeds size cap")
    return text


def _as_busy(exc: sqlite3.OperationalError) -> Exception:
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        return JournalBusyError(str(exc))
    return exc


# --- SQLite reference implementation -----------------------------------------


class SQLiteActionJournal:
    """Consumer-injected SQLite journal. The path is explicit — Core never
    creates a hidden default database."""

    def __init__(self, path: str, *, busy_timeout_ms: int = 5000) -> None:
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._create_schema()

    def close(self) -> None:
        self._conn.close()

    # -- transaction helpers --
    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                action_sequence INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN
                    ('PREPARED','DISPATCHING','EXECUTED','FINALIZED',
                     'NOT_EXECUTED','OUTCOME_UNKNOWN')),
                dispatch_certainty TEXT NOT NULL CHECK (dispatch_certainty IN
                    ('NONE','POSSIBLE','CONFIRMED')),
                reconciliation_verdict TEXT CHECK (reconciliation_verdict IN
                    ('ALREADY_APPLIED','NOT_APPLIED','UNKNOWN')),
                hook_applied INTEGER CHECK (hook_applied IN (0,1)),
                hook_recovery TEXT NOT NULL CHECK (hook_recovery IN
                    ('IDEMPOTENT','NOT_REPLAYABLE','NOT_APPLICABLE')),
                pack_id TEXT,
                pack_digest TEXT,
                reconciliation_schema_version TEXT,
                semantic_target TEXT NOT NULL,
                action_type TEXT NOT NULL,
                execution_payload_hash TEXT NOT NULL,
                reconciliation_context TEXT,
                pre_execution_observation_refs TEXT NOT NULL DEFAULT '[]',
                recovery_observation_refs TEXT NOT NULL DEFAULT '[]',
                foreground TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (run_id, action_sequence)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_per_run
                ON actions(run_id)
                WHERE state IN
                    ('PREPARED','DISPATCHING','EXECUTED','OUTCOME_UNKNOWN');

            CREATE TABLE IF NOT EXISTS action_transitions (
                action_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                at TEXT NOT NULL,
                PRIMARY KEY (action_id, revision)
            );
            """
        )

    # -- API --
    def prepare(self, intent: ActionIntent) -> PreparedAction:
        run_id = intent.run_id
        if not isinstance(run_id, str) or not run_id.strip():
            raise RunIdInvalidError(
                "run_id must be a non-empty, non-whitespace string"
            )
        if not isinstance(intent.action_id, str) or not intent.action_id.strip():
            raise ActionJournalError("action_id must be a non-empty string")
        if intent.hook_recovery not in ALL_HOOK_RECOVERY:
            raise ActionJournalError(f"bad hook_recovery: {intent.hook_recovery!r}")
        ctx = _dump_context(intent.reconciliation_context)
        pre_refs = json.dumps(list(intent.pre_execution_observation_refs or []))
        now = _now()
        try:
            self._begin()
            dup = self._conn.execute(
                "SELECT 1 FROM actions WHERE action_id=?", (intent.action_id,)
            ).fetchone()
            if dup is not None:
                self._rollback()
                raise DuplicateActionError(
                    f"action_id already exists: {intent.action_id!r}"
                )
            open_row = self._conn.execute(
                f"SELECT action_id FROM actions "
                f"WHERE run_id=? AND state IN ({_OPEN_SQL})",
                (run_id,),
            ).fetchone()
            if open_row is not None:
                self._rollback()
                raise ActiveActionError(
                    f"run {run_id!r} already has an open action: {open_row[0]!r}"
                )
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(action_sequence),0)+1 "
                "FROM actions WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            try:
                self._conn.execute(
                    "INSERT INTO actions ("
                    "action_id, run_id, action_sequence, revision, state, "
                    "dispatch_certainty, hook_recovery, pack_id, pack_digest, "
                    "reconciliation_schema_version, semantic_target, action_type, "
                    "execution_payload_hash, reconciliation_context, "
                    "pre_execution_observation_refs, recovery_observation_refs, "
                    "foreground, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent.action_id,
                        run_id,
                        seq,
                        0,
                        PREPARED,
                        CERTAINTY_NONE,
                        intent.hook_recovery,
                        intent.pack_id,
                        intent.pack_digest,
                        intent.reconciliation_schema_version,
                        intent.semantic_target,
                        intent.action_type,
                        intent.execution_payload_hash,
                        ctx,
                        pre_refs,
                        "[]",
                        intent.foreground,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                self._rollback()
                if "action_id" in str(exc).lower():
                    raise DuplicateActionError(
                        f"action_id already exists: {intent.action_id!r}"
                    ) from exc
                raise ActiveActionError(
                    f"concurrent open action for run {run_id!r}"
                ) from exc
            self._conn.execute(
                "INSERT INTO action_transitions "
                "(action_id, revision, from_state, to_state, at) "
                "VALUES (?,?,?,?,?)",
                (intent.action_id, 0, None, PREPARED, now),
            )
            self._commit()
        except sqlite3.OperationalError as exc:
            self._rollback()
            raise _as_busy(exc)
        return PreparedAction(
            run_id=run_id,
            action_id=intent.action_id,
            action_sequence=seq,
            revision=0,
            state=PREPARED,
        )

    def transition(
        self,
        action_id: str,
        *,
        expected_state: str,
        expected_revision: int,
        new_state: str,
        patch: dict[str, Any] | None = None,
    ) -> int:
        patch = dict(patch or {})
        if (expected_state, new_state) not in LEGAL_TRANSITIONS:
            raise IllegalTransitionError(
                f"illegal transition {expected_state!r} -> {new_state!r}"
            )
        bad = set(patch) - _PATCHABLE
        if bad:
            raise ImmutableFieldError(
                f"fields not patchable by transition: {sorted(bad)}"
            )
        if (
            "reconciliation_verdict" in patch
            and patch["reconciliation_verdict"] not in ALL_VERDICTS
        ):
            raise ActionJournalError(
                f"bad reconciliation_verdict: {patch['reconciliation_verdict']!r}"
            )
        try:
            self._begin()
            row = self._conn.execute(
                "SELECT state, revision, dispatch_certainty, "
                "recovery_observation_refs FROM actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                self._rollback()
                raise RevisionConflictError(f"no such action_id: {action_id!r}")
            cur_state, cur_rev, cur_cert, refs_json = row
            new_cert = _certainty_for(new_state, cur_cert)
            sets = [
                "state=?",
                "dispatch_certainty=?",
                "revision=revision+1",
                "updated_at=?",
            ]
            params: list[Any] = [new_state, new_cert, _now()]
            if "reconciliation_verdict" in patch:
                sets.append("reconciliation_verdict=?")
                params.append(patch["reconciliation_verdict"])
            if "hook_applied" in patch:
                sets.append("hook_applied=?")
                params.append(1 if patch["hook_applied"] else 0)
            if "recovery_observation_refs" in patch:
                existing = json.loads(refs_json or "[]")
                existing.extend(patch["recovery_observation_refs"])
                sets.append("recovery_observation_refs=?")
                params.append(json.dumps(existing))
            params.extend([action_id, expected_state, expected_revision])
            cur = self._conn.execute(
                f"UPDATE actions SET {', '.join(sets)} "
                "WHERE action_id=? AND state=? AND revision=?",
                params,
            )
            if cur.rowcount != 1:
                self._rollback()
                raise RevisionConflictError(
                    f"CAS failed for {action_id!r}: expected "
                    f"{expected_state}@{expected_revision}, "
                    f"found {cur_state}@{cur_rev}"
                )
            new_rev = expected_revision + 1
            self._conn.execute(
                "INSERT INTO action_transitions "
                "(action_id, revision, from_state, to_state, at) "
                "VALUES (?,?,?,?,?)",
                (action_id, new_rev, expected_state, new_state, _now()),
            )
            self._commit()
        except sqlite3.OperationalError as exc:
            self._rollback()
            raise _as_busy(exc)
        return new_rev

    def load_open_actions(self, run_id: str) -> list[ActionRecord]:
        rows = self._conn.execute(
            f"SELECT {_RECORD_COLS} FROM actions "
            f"WHERE run_id=? AND state IN ({_OPEN_SQL}) "
            "ORDER BY action_sequence",
            (run_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_record(self, action_id: str) -> ActionRecord | None:
        row = self._conn.execute(
            f"SELECT {_RECORD_COLS} FROM actions WHERE action_id=?",
            (action_id,),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def build_recovery_report(
        self, action_id: str, *, core_fact: str | None = None
    ) -> RecoveryReport:
        rec = self.get_record(action_id)
        if rec is None:
            raise JournalCorruptError(f"no such action_id: {action_id!r}")
        fact = core_fact if core_fact is not None else core_fact_from_state(rec.state)
        return RecoveryReport(
            run_id=rec.run_id,
            action_sequence=rec.action_sequence,
            action_id=rec.action_id,
            revision=rec.revision,
            core_fact=fact,
            dispatch_certainty=rec.dispatch_certainty,
            reconciliation_verdict=rec.reconciliation_verdict,
            hook_applied=rec.hook_applied,
            pre_execution_observation_refs=rec.pre_execution_observation_refs,
            recovery_observation_refs=rec.recovery_observation_refs,
        )

    def verify_integrity(self, action_id: str) -> None:
        """Walk the persisted transition chain and raise ``JournalCorruptError``
        if any recorded adjacency is not a legal transition."""
        rows = self._conn.execute(
            "SELECT from_state, to_state FROM action_transitions "
            "WHERE action_id=? ORDER BY revision",
            (action_id,),
        ).fetchall()
        if not rows:
            raise JournalCorruptError(f"no transition history: {action_id!r}")
        for from_state, to_state in rows:
            if from_state is None:
                if to_state != PREPARED:
                    raise JournalCorruptError(
                        f"chain must open at PREPARED, got {to_state!r}"
                    )
                continue
            if (from_state, to_state) not in LEGAL_TRANSITIONS:
                raise JournalCorruptError(
                    f"illegal persisted transition {from_state!r} -> {to_state!r}"
                )

    def _row_to_record(self, row: tuple[Any, ...]) -> ActionRecord:
        (
            run_id,
            action_sequence,
            action_id,
            revision,
            state,
            dispatch_certainty,
            reconciliation_verdict,
            hook_applied,
            hook_recovery,
            pack_id,
            pack_digest,
            reconciliation_schema_version,
            semantic_target,
            action_type,
            execution_payload_hash,
            reconciliation_context,
            pre_refs,
            recovery_refs,
            foreground,
            created_at,
            updated_at,
        ) = row
        if state not in ALL_STATES:
            raise JournalCorruptError(f"unknown state {state!r} for {action_id!r}")
        return ActionRecord(
            run_id=run_id,
            action_sequence=action_sequence,
            action_id=action_id,
            revision=revision,
            state=state,
            dispatch_certainty=dispatch_certainty,
            reconciliation_verdict=reconciliation_verdict,
            hook_applied=None if hook_applied is None else bool(hook_applied),
            hook_recovery=hook_recovery,
            pack_id=pack_id,
            pack_digest=pack_digest,
            reconciliation_schema_version=reconciliation_schema_version,
            semantic_target=semantic_target,
            action_type=action_type,
            execution_payload_hash=execution_payload_hash,
            reconciliation_context=(
                json.loads(reconciliation_context)
                if reconciliation_context is not None
                else None
            ),
            pre_execution_observation_refs=json.loads(pre_refs or "[]"),
            recovery_observation_refs=json.loads(recovery_refs or "[]"),
            foreground=foreground,
            created_at=created_at,
            updated_at=updated_at,
        )
