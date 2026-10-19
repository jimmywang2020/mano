# P0 Action Journal — Design (Round 1: design only)

> **Status: DESIGN / REVISED (round 1b). No runner behavior changes this round.**
> This document is the P0 deliverable. Implementation (SQLite reference +
> failure matrix) follows only after this design is approved.
> Source:《mano调用反馈_1016_1704》+《mano开发设计评审_1016_1724》, revised
> per《mano开发设计评审_1019_1448》(full transition table, cross-Runner
> uniqueness, recovery-before-new-action entry, dispatch_certainty rename,
> reconciliation_context / schema_version, RECONCILIATION_UNAVAILABLE).

## 1. Problem statement

The thin runner persists an *evidence* journal (per-step JSONL) **after** each
step. It does **not** protect the blurry window around the single external
side-effect:

```
process gets SIGKILL after ADB click but before the next record is durable
  -> on restart, cannot tell whether the click happened
  -> risk: replay a non-idempotent click / type / submit
```

The one external side-effect point today is
`execute_action(adb, final, obs.width, obs.height)` in
[`mano/runner.py`](../mano/runner.py) (device-modifying branch), with
`on_action_executed` (optional Pack post-processing) running right after it.

**Goal of P0:** a minimal, pluggable, durable *action* journal + a recovery
protocol whose only hard guarantee is **"no ambiguous window is ever
auto-replayed on the device."** It is explicitly **not** exactly-once, **not**
auto-resume-everything, and **not** LangGraph-bound.

## 2. Non-goals (restated from the review)

- No distributed journal; single-host, single-device reference implementation.
- No exactly-once transaction spanning "write journal + tap phone + write result".
- No LangGraph / LangChain dependency in Mano Core. (LangGraph handles the
  *workflow* layer in the consumer; the journal answers "did this one ADB action
  happen?" — a question checkpoints cannot answer.)
- No reimplementation of M7 queue / lease / retry / quoting / M10 review.
- No raw-evidence duplication into the journal (refs/hashes only; §7).

## 3. State machine (six states, no bare `FAILED`)

A `FAILED` state cannot distinguish "definitely did not touch the device" from
"might have." The journal uses six states; any terminal error carries an
explicit `dispatch_certainty`.

```
PREPARED        action intent durably persisted; ADB NOT yet permitted
DISPATCHING     durably recorded "about to enter the external side-effect window"
EXECUTED        ADB dispatch returned success; Pack post-processing (hook) may be pending
FINALIZED       Core execution record AND Pack post-processing both complete
NOT_EXECUTED    proven no device dispatch occurred
OUTCOME_UNKNOWN ADB timeout / disconnect / ambiguous; dispatch MAY have happened
```

`dispatch_certainty ∈ { NONE, POSSIBLE, CONFIRMED }` is **mandatory** on every
terminal transition. It describes only whether the ADB command was *dispatched*
— **not** whether the target app's semantic effect occurred (a `tap` dispatch can
succeed without the filter actually toggling). The *semantic* question is
answered separately by the Pack's `ALREADY_APPLIED | NOT_APPLIED | UNKNOWN` (§6).

| state | dispatch_certainty |
|---|---|
| `PREPARED` | `NONE` |
| `DISPATCHING` | `POSSIBLE` |
| `EXECUTED` | `CONFIRMED` |
| `FINALIZED` | `CONFIRMED` |
| `NOT_EXECUTED` | `NONE` |
| `OUTCOME_UNKNOWN` | `POSSIBLE` |

### Legal transitions (machine-checkable)

```
PREPARED        -> DISPATCHING | NOT_EXECUTED
DISPATCHING     -> EXECUTED | NOT_EXECUTED | OUTCOME_UNKNOWN
OUTCOME_UNKNOWN -> EXECUTED | NOT_EXECUTED | OUTCOME_UNKNOWN   (re-reconcile allowed)
EXECUTED        -> FINALIZED
```

Terminal states: `FINALIZED`, `NOT_EXECUTED`. `transition()` rejects any pair not
in this table (invalid chain -> `JOURNAL_CORRUPT`). `OUTCOME_UNKNOWN` may be
re-entered because a first reconciliation can itself be inconclusive; each
re-entry is a fresh CAS transition (new revision), never an auto-replay.

`load_open_actions(run_id)` returns every **non-terminal** record, i.e. any in:

```
PREPARED | DISPATCHING | EXECUTED | OUTCOME_UNKNOWN
```

### Recovery interpretation (per persisted state)

| persisted state | recovery meaning |
|---|---|
| `PREPARED` | ADB never entered -> re-observe + re-plan; never replay old coordinates. Close the stale intent (-> `NOT_EXECUTED`) before planning a new action |
| `DISPATCHING` | blurry window -> **must assume it may have dispatched**; hand to Pack reconcile / Consumer |
| `EXECUTED` | **never replay ADB**; Pack post-processing may need recovery (§6) |
| `FINALIZED` | action complete; nothing to do |
| `NOT_EXECUTED` | safe to enter re-observation |
| `OUTCOME_UNKNOWN` | needs Pack semantic reconciliation or hand to Consumer |

## 4. Transition map onto the current runner flow

Anchored to the device-modifying branch of
[`mano/runner.py`](../mano/runner.py) (after anti-swap, around the
`execute_action(...)` call). **No code is changed this round** — this is where
the Round-2 implementation will hook:

```
... resolve / validate / anti-loop / approve / revalidation / verify|fallback ...
final action decided (post anti-swap)
  journal.prepare(record)                 -> PREPARED      (durable before return)
  journal.transition(... PREPARED->DISPATCHING ...)        (durable, POSSIBLE)
  execute_action(adb, final, w, h)        <-- external side-effect window
    success:
      journal.transition(... DISPATCHING->EXECUTED ...)    (CONFIRMED)
      on_action_executed(...) hook (if any)
      journal.transition(... EXECUTED->FINALIZED ...)
    TextInputError (ame=True) / ADB timeout / disconnect:
      journal.transition(... DISPATCHING->OUTCOME_UNKNOWN ...)
    clean refusal proven pre-ADB (e.g. TextInputError ame=False):
      journal.transition(... DISPATCHING->NOT_EXECUTED ...)
```

Design constraint: `journal.prepare()` and the `->DISPATCHING` transition **must
be durable before** `execute_action` is called; the `->EXECUTED` transition is
attempted immediately after ADB returns. The window between ADB-success and
`EXECUTED`-durable is covered by recovery rule `DISPATCHING = assume POSSIBLE`.

## 5. Recovery result — responsibilities are split 3 ways

Core does **not** emit business states like `NEEDS_REVIEW`. It reports *facts*;
the Pack reconciles *semantics*; the Consumer decides *policy*.

**Core fact (from journal state alone):**

```
NO_PENDING_ACTION          no open action for this run
SAFE_TO_REOBSERVE          only PREPARED/NOT_EXECUTED open -> re-observe is safe
EXECUTION_CONFIRMED        EXECUTED (post-processing recovery may remain)
AMBIGUOUS_EFFECT           DISPATCHING / OUTCOME_UNKNOWN -> dispatch POSSIBLE
RECONCILIATION_UNAVAILABLE Pack has no reconcile_action -> cannot judge semantics
PACK_INCOMPATIBLE          reconciliation_schema_version mismatch -> cannot reconcile
JOURNAL_CORRUPT            record unreadable / invalid transition chain
```

`RECONCILIATION_UNAVAILABLE` (no interface) and `PACK_INCOMPATIBLE`
(`reconciliation_schema_version` mismatch) are distinct: the former means the
Pack never offered recovery semantics; the latter means it did, but against an
incompatible schema. The pack code digest is **audit-only** and never by itself
forces `PACK_INCOMPATIBLE`.

**Pack reconcile (semantic, §6):** `ALREADY_APPLIED | NOT_APPLIED | UNKNOWN`.

**Consumer maps to policy:** `继续 | 重新规划 | 人工审核 | 失败终止`
(`NEEDS_REVIEW` is an M7/采购助手 workflow state, not a Mano Core fact.)

### Recovery entry ordering (recovery MUST precede any new action)

On Runner start, before any model call or new `prepare()`:

```
open journal
-> load_open_actions(run_id)
-> for each open action: fresh observation
   -> Pack.reconcile_action (if declared) / Consumer decision
   -> close it (-> NOT_EXECUTED / FINALIZED) or stop safely
-> only once NO open action remains may the Runner call the model / execute
```

If the Pack has no `reconcile_action`, is schema-incompatible, or the Consumer
returns no policy, the Runner MUST return a structured recovery result and keep
**zero ADB** — it must not fall through into the normal loop.

## 6. New recovery interface — do NOT overload `verify`

`verify` answers "does the approved target still hold *before* execution?" It
cannot answer "after a crash, did this action already succeed?" Add a **separate
optional** Pack capability:

```python
def reconcile_action(
    self, *, journaled_action, fresh_observation, recovery: bool = True
) -> ReconciliationResult   # ALREADY_APPLIED | NOT_APPLIED | UNKNOWN
```

- Called only during recovery, against a **fresh** post-restart observation, with
  the stable `action_id` (inside `journaled_action`) and `recovery=True` so the
  Pack can de-duplicate.
- Must work from a **new Pack instance**: the current 转转 Pack keeps
  `_verification_contexts` as an in-process dict that is gone after restart
  (`RevalidationView` is in-memory and persists only an XML *fingerprint*, not
  the XML — [observation.py](../mano/observation.py)). Recovery therefore
  **cannot** depend on process memory. Everything a new instance needs must come
  from the journal record (§7) + the fresh observation.
- An `execution_payload` hash alone is **not** enough to reconcile. A Pack MAY
  attach a constrained **`reconciliation_context`** at `prepare()` time:
  non-sensitive semantic info sufficient to recognise its own effect later (e.g.
  the stable target id + matched rule). Sensitive data is stored as a hash or a
  Consumer-provided encrypted evidence ref only.
- A Pack **without** `reconcile_action` -> Core reports
  `RECONCILIATION_UNAVAILABLE` (not `PACK_INCOMPATIBLE`); the Consumer decides;
  **never** an auto-replay.

### `on_action_executed` recovery rule — Core-enforced declaration

Covers: `ADB success -> EXECUTED durable -> SIGKILL before the hook runs`.

- A Pack MUST declare `hook_recovery = IDEMPOTENT | NOT_REPLAYABLE` (Core reads
  this; it is **not** merely documented).
- `IDEMPOTENT`: on recovery with `EXECUTED` but not `FINALIZED`, Core **replays
  the hook only, never the ADB action** (passing the stable `action_id` and
  `recovery=True`), then transitions to `FINALIZED`.
- `NOT_REPLAYABLE` (or undeclared): recovery result is `UNKNOWN`, the hook is
  **not** replayed, the ADB action is **never** replayed, and the Consumer
  decides.
- Core records `hook_applied` on `FINALIZED`.

## 7. Journal record — refs/hashes, not raw evidence

Minimum fields the record MUST carry so a fresh Pack instance can reconcile:

```
run_id
action_sequence      journal-assigned in the prepare() transaction; UNIQUE(run_id, action_sequence)
action_id            unique; a second prepare() for the same id is rejected
revision             monotonic; CAS token for transitions (§8)
pack_id
pack_digest          audit-only; never alone forces PACK_INCOMPATIBLE
reconciliation_schema_version   mismatch -> PACK_INCOMPATIBLE
semantic_target
action_type
execution_payload hash   reuse action_codec.execution_payload (§9); hash, not plaintext
reconciliation_context   Pack-provided, constrained, non-sensitive (§6)
fresh_observation refs   evidence IDs/refs + capture timestamps (NOT the bytes)
foreground
state, dispatch_certainty, hook_applied, hook_recovery
timestamps per transition
```

- **No raw screenshots / XML / plaintext are copied into the journal** — only
  evidence IDs/refs + hashes + timestamps. The pre-execution `RevalidationView`
  is in-memory and keeps only an XML fingerprint; if recovery needs any
  execution-time context, the necessary refs/context MUST be captured into the
  record **before** `PREPARED` is durable.
- For `type` actions: store `text hash` + `length` + `semantic_target` +
  encrypted evidence ref — **never** the plaintext. An ambiguous action is never
  auto-replayed, so the journal never needs directly-replayable plaintext.

## 8. `ActionJournal` Protocol — CAS / revision required

`prepare/mark_completed/mark_failed` alone allow concurrent / illegal
overwrites. The Protocol uses optimistic concurrency:

```python
class ActionJournal(Protocol):
    def prepare(self, record) -> PreparedAction:   # assigns action_sequence + revision; DURABLE before return
        ...
    def transition(
        self,
        action_id: str,
        *,
        expected_state: str,
        expected_revision: int,
        new_state: str,
        patch: dict,
    ) -> int:                                 # atomic CAS; returns new revision
        ...
    def load_open_actions(self, run_id: str) -> list:   # non-terminal (see §3)
        ...
```

Requirements:

- `prepare()` MUST be durable before it returns; it **assigns `action_sequence`
  inside the DB transaction** (not from the Runner loop `step`) and rejects a
  second `prepare()` for an existing `action_id`.
- A DB-level `UNIQUE(run_id, action_sequence)` constraint is the real cross-Runner
  guard: single-record CAS alone does **not** stop two Runners that mint
  *different* `action_id`s for the same logical step. The Runner MUST resolve all
  open actions (§5 recovery entry) before `prepare()`-ing the next one.
- `transition()` MUST atomically check `expected_state` **and**
  `expected_revision` before applying; mismatch raises (no silent overwrite).
- SQLite reference impl: real transactions + `PRAGMA journal_mode=WAL`,
  `PRAGMA synchronous=FULL`. This promises **process-crash / SIGKILL** recovery;
  it does **not** claim an absolute fsync guarantee against arbitrary hardware
  power-loss.
- **No** claim of an exactly-once transaction with ADB.
- Auto-recovery goal is **"never mis-replay"**, not "always auto-continue."
- The journal is a Core-owned pluggable interface; it MUST NOT bind to the
  采购助手 database or to LangGraph `Store` (a `LangGraphStoreActionJournal`
  adapter MAY exist later, but Core never depends on it).

## 9. `action_codec` reuse (minimal-diff policy)

`action_codec.py` is **not** an absolute no-touch zone, but default is no change.
The journal reuses the existing public helpers to avoid a third action
normalization path:

- `execution_payload(action)` — canonical non-coordinate side-effect fields ->
  hash for the record (§7).
- `semantic_signature(semantic_target, action)` — cross-step identity, already
  computed in the runner.

Expected diff to `action_codec.py` this effort: **none** (import and call only).
If a stable hash helper is genuinely needed, it will be proposed as a minimal
addition with rationale **before** editing — never by copying the logic.

## 10. Failure matrix (Round 2 acceptance) — real process death required

Unit-level exception injection is insufficient. The matrix MUST include:

1. terminate after `PREPARED`, before `DISPATCHING`.
2. SIGKILL after `DISPATCHING` durable, before the ADB call.
3. SIGKILL during the ADB call.
4. SIGKILL after ADB success, before `EXECUTED` durable.
5. SIGKILL after `EXECUTED`, before `on_action_executed`.
6. SIGKILL after hook success, before `FINALIZED`.
7. hook raises.
8. journal state corruption (`JOURNAL_CORRUPT`).
9. Pack reconciliation unavailable vs schema-incompatible
   (`RECONCILIATION_UNAVAILABLE` vs `PACK_INCOMPATIBLE`).
10. two Runners race the same `(run_id, action_sequence)` (DB `UNIQUE` +
    per-record CAS reject the loser).

At least one case MUST use a **real subprocess + SIGKILL**, not a mock
exception. The single machine-checkable acceptance assertion:

```
no ambiguous window ever auto-replays a device action
```

## 11. Rollout constraints (until P0 is accepted)

- Mano stays behind the consumer feature switch; default sampling backend `v01`.
- The journal ships as an **injectable optional dependency** (default `None` =
  today's behavior, zero runner change) until Round 2 wires it; even then it
  does not flip the consumer default.
- Do **not** claim crash-safe auto-resume before the failure matrix passes.
- No LangGraph dependency added to Mano Core.
- No duplication of M7 queue/lease/retry/quote/review.

## 12. Reviewer decisions (resolved; source《mano开发设计评审_1019_1448》)

1. **Journal scope:** `wait` / `terminate` are **not** journaled (no replayable
   side-effect). Only device-modifying actions enter the journal, keyed by the
   journal-assigned `(run_id, action_sequence)` with a DB `UNIQUE` constraint —
   **not** the Runner loop `step`. Open actions must be resolved before a new one
   is assigned.
2. **`NOT_EXECUTED` precision:** option (a). Trust the already-audited
   `TextInputError.action_may_have_executed is False` -> `DISPATCHING ->
   NOT_EXECUTED`. All other ADB exceptions default to `OUTCOME_UNKNOWN`. Do not
   couple the journal to text-backend internals.
3. **Hook idempotency:** Core-enforced via `hook_recovery = IDEMPOTENT |
   NOT_REPLAYABLE`. Only `IDEMPOTENT` may replay the hook on recovery (with
   stable `action_id` + `recovery=True`); otherwise `UNKNOWN`, and the ADB action
   is never replayed.
4. **SQLite path:** Consumer-injected. Core ships `SQLiteActionJournal(path)` but
   the Runner never silently creates a default journal; `journal=None` = disabled.
