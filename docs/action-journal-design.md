# P0 Action Journal — Design (Round 1: design only)

> **Status: DESIGN / UNREVIEWED. No runner behavior changes this round.**
> This document is the P0 deliverable. Implementation (SQLite reference +
> failure matrix) follows only after this design is reviewed and approved.
> Source of requirements:《mano调用反馈_1016_1704》+《mano开发设计评审_1016_1724》.

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
explicit `effect_certainty`.

```
PREPARED        action intent durably persisted; ADB NOT yet permitted
DISPATCHING     durably recorded "about to enter the external side-effect window"
EXECUTED        ADB returned success; Pack post-processing (hook) may be pending
FINALIZED       Core execution record AND Pack post-processing both complete
NOT_EXECUTED    proven no device side-effect occurred
OUTCOME_UNKNOWN ADB timeout / disconnect / ambiguous; side-effect MAY have happened
```

`effect_certainty ∈ { NONE, POSSIBLE, CONFIRMED }` is **mandatory** on every
terminal transition (it is the machine-checkable field recovery reads):

| state | effect_certainty |
|---|---|
| `PREPARED` | `NONE` |
| `DISPATCHING` | `POSSIBLE` |
| `EXECUTED` | `CONFIRMED` |
| `FINALIZED` | `CONFIRMED` |
| `NOT_EXECUTED` | `NONE` |
| `OUTCOME_UNKNOWN` | `POSSIBLE` |

### Recovery interpretation (per persisted state)

| persisted state | recovery meaning |
|---|---|
| `PREPARED` | ADB never entered -> re-observe + re-plan; never replay old coordinates |
| `DISPATCHING` | blurry window -> **must assume it may have executed** |
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
NO_PENDING_ACTION     no open action for this run
SAFE_TO_REOBSERVE     only PREPARED/NOT_EXECUTED open -> re-observe is safe
EXECUTION_CONFIRMED   EXECUTED (post-processing recovery may remain)
AMBIGUOUS_EFFECT      DISPATCHING / OUTCOME_UNKNOWN -> effect POSSIBLE
JOURNAL_CORRUPT       record unreadable / invalid transition chain
PACK_INCOMPATIBLE     pack_id / pack_version(digest) mismatch -> cannot reconcile
```

**Pack reconcile (semantic, §6):** `ALREADY_APPLIED | NOT_APPLIED | UNKNOWN`.

**Consumer maps to policy:** `继续 | 重新规划 | 人工审核 | 失败终止`
(`NEEDS_REVIEW` is an M7/采购助手 workflow state, not a Mano Core fact.)

## 6. New recovery interface — do NOT overload `verify`

`verify` answers "does the approved target still hold *before* execution?" It
cannot answer "after a crash, did this action already succeed?" Add a **separate
optional** Pack capability:

```python
def reconcile_action(
    self, *, journaled_action, fresh_observation
) -> ReconciliationResult   # ALREADY_APPLIED | NOT_APPLIED | UNKNOWN
```

- Called only during recovery, against a **fresh** post-restart observation.
- Must work from a **new Pack instance**: the current 转转 Pack keeps
  `_verification_contexts` as an in-process dict that is gone after restart, so
  recovery **cannot** depend on it. All information a new instance needs must
  come from the journal record (§7) + the fresh observation.
- A Pack without `reconcile_action` -> Core reports `AMBIGUOUS_EFFECT` /
  `PACK_INCOMPATIBLE` and the Consumer decides; **never** an auto-replay.

### `on_action_executed` recovery rule (phase 1)

Covers: `ADB success -> EXECUTED durable -> SIGKILL before the hook runs`.

- **The hook MUST be idempotent.** Core records `hook_applied` on `FINALIZED`.
- On recovery, `EXECUTED` but not `FINALIZED` -> **replay the hook only, never
  the ADB action**, then transition to `FINALIZED`.
- If a Pack cannot promise hook idempotency, recovery result is `UNKNOWN` and the
  Consumer decides. (Documented requirement on the Pack contract.)

## 7. Journal record — refs/hashes, not raw evidence

Minimum fields the record MUST carry so a fresh Pack instance can reconcile:

```
run_id
action_id            unique; a second prepare() for the same id is rejected
revision             monotonic; CAS token for transitions (§8)
pack_id
pack_version | digest used to detect PACK_INCOMPATIBLE on recovery
semantic_target
action_type
execution_payload hash   reuse action_codec.execution_payload (§9); hash, not plaintext
fresh_observation refs   evidence IDs/refs + capture timestamps (NOT the bytes)
foreground
state, effect_certainty, hook_applied
timestamps per transition
```

- **No raw screenshots / XML / plaintext are copied into the journal** — only
  evidence IDs/refs + hashes + timestamps.
- For `type` actions: store `text hash` + `length` + `semantic_target` +
  encrypted evidence ref — **never** the plaintext. An ambiguous action is never
  auto-replayed, so the journal never needs directly-replayable plaintext.

## 8. `ActionJournal` Protocol — CAS / revision required

`prepare/mark_completed/mark_failed` alone allow concurrent / illegal
overwrites. The Protocol uses optimistic concurrency:

```python
class ActionJournal(Protocol):
    def prepare(self, record) -> int:        # returns revision; DURABLE before return
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
    def load_open_actions(self, run_id: str) -> list:   # non-terminal records
        ...
```

Requirements:

- `prepare()` MUST be durable before it returns; a second `prepare()` for an
  existing `action_id` is rejected.
- `transition()` MUST atomically check `expected_state` **and**
  `expected_revision` before applying; mismatch raises (no silent overwrite).
- SQLite reference impl: real transactions + explicit durability
  (`PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL`, fsync on commit).
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
9. Pack digest incompatible (`PACK_INCOMPATIBLE`).
10. two Runners race the same action/run (CAS rejects the loser).

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

## 12. Open questions for the reviewer

1. **Journal scope key:** `action_id` per device-modifying step only (wait /
   terminate excluded), keyed by `(run_id, step)` — confirm `wait` and
   `terminate` are never journaled (no external-replay risk).
2. **`NOT_EXECUTED` precision:** today a pre-ADB clean refusal (`TextInputError`
   `ame=False`) happens *inside* `execute_action`, i.e. after `DISPATCHING`. Do
   we (a) accept it resolves `DISPATCHING -> NOT_EXECUTED` via the backend's
   `action_may_have_executed=False` contract, or (b) push the DISPATCHING mark
   deeper so pure-validation refusals never reach `POSSIBLE`? (a) is simpler and
   relies on the already-audited backend flag; (b) narrows the ambiguous window
   but couples the journal to backend internals.
3. **Hook idempotency enforcement:** phase 1 *documents* the requirement. Do we
   also want a Core-side guard (e.g. refuse to replay a hook on a Pack that does
   not declare idempotency, forcing `UNKNOWN`)?
4. **Reference storage path:** SQLite file location — Core-chosen default under
   the run output dir, or Consumer-injected path? (Leaning Consumer-injected,
   Core default fallback.)
