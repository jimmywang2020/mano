# Result & error reference

A run surfaces facts from **several different namespaces**. They are NOT one flat
"Mano error code" set: some are fixed Core constants, some are the journal's core
facts, some come from a text backend, and some are **Pack/guard-defined** strings
that Mano passes through verbatim. This doc records each namespace separately so a
Consumer handles them correctly.

> Verified against `v0.1.2`. `RunResult` carries only the fields listed in
> [integration-guide.md](integration-guide.md) §8 — do not depend on fields not
> listed there. **Never infer "the action did not execute" from a result string
> alone**; the ambiguous facts below exist because that inference is unsafe.

Columns: **Source** (who produces it), **Stability** (fixed Core constant vs Pack
extension), **Side-effect** (could a device change have happened), **Retryable**
(may the Consumer re-run the same logical task), **Review** (needs human review),
**Handling**.

---

## 1. `RunResult.result` — fixed Core stop codes

Fixed constants emitted by the Core (string values on `RunResult.result`).

| `result` | Side-effect | Retryable | Review | Meaning / handling |
|---|---|---|---|---|
| `terminate_<status>` | n/a | n/a | no | Model terminated; `<status>` is its `terminate` status (e.g. `success`/`failure`). Business outcome. |
| `terminate_from_exec` | n/a | n/a | no | A terminate surfaced from the execution path. |
| `max_steps` | no new one | yes | no | Step budget hit. Raise `max_steps` or treat as not-finished. |
| `timeout` | no new one | yes | no | `step_timeout_s` hit. |
| `observation_unstable` | no | yes | no | Foreground kept changing / skew over budget past `max_unstable`. Let the page settle. |
| `vlm_error` | no | yes | no | Network error talking to the VLM. Retry/backoff. |
| `vlm_unparseable` | no | yes (bounded) | maybe | Model output could not be parsed after self-corrections. |
| `reobserve_required` | no | yes | no | `max_reobserve` zero-side-effect re-observations exhausted. Page never became ready. |
| `invalid_resolve_verdict` | no | no | yes | Pack `resolve` returned an illegal verdict — Pack bug. |
| `invalid_semantic_target` | no | no | yes | `resolve` ALLOW with an empty/invalid `semantic_target` — Pack bug. |
| `resolve_invalid_action` | no | no | maybe | Resolved action failed the whitelist for a non-coordinate reason (e.g. `text_unsupported_by_backend`). Fail-closed. |
| `invalid_verify_verdict` | no | no | yes | Pack `verify` returned an illegal verdict — Pack bug. |
| `verify_invalid_action` | no | no | maybe | Verified action failed validation. Fail-closed. |
| `verify_semantic_swap_blocked` | no | no | yes | `verify` tried to change the action type / side-effects (not just coordinates). Fail-closed. |
| `approve_rejected` | no | no | no | Consumer `approve` vetoed (or raised). Expected business stop. |
| `exec_refused` | **maybe** | maybe | maybe | ADB/execution failed or a non-ambiguous text error. The dispatch may or may not have landed depending on the error; do not assume success. |
| `text_ambiguous_execution` | **yes (ambiguous)** | **no (do not replay)** | **yes** | A text failure left an ambiguous state (e.g. IME restore failed after a possible injection). **Never auto-replay.** Human/verify the field value. |
| `post_execute_hook_failed` | **yes** | no | yes | The device change landed, then `on_action_executed` raised. Action is **never replayed**; reconcile Pack state. |
| `oscillation_blocked` | no new one | no | maybe | Anti-loop: A↔B oscillation detected. |
| `repeat_click_blocked` | no new one | no | maybe | Anti-loop: same semantic action repeated. |
| `wait_stuck` | no | no | maybe | Too many consecutive `wait`s. |

## 2. `RunResult.result` — journal / recovery codes

Emitted only in journal mode; the detail is on `RunResult.journal_error_code`.

| `result` | Side-effect | Retryable | Review | Meaning / handling |
|---|---|---|---|---|
| `journal_config_error` | **no** | fix config, then yes | no | Preflight failed (missing stable `run_id`, bad hook ABI, missing `pack_id`/`reconciliation_schema_version`). Zero device change. Fix wiring. |
| `journal_error` | maybe | yes (recovery runs next start) | maybe | CAS conflict / DB busy / corrupt journal. The run stops; it never degrades to no-journal. Restart with the same `run_id` to recover. |
| `recovery_required` | ambiguous | no (needs a decision) | yes | An open action but no `recovery_decide` callback. Supply one. |
| `recovery_manual_review` | ambiguous | no | **yes** | Consumer chose `MANUAL_REVIEW` for the recovered action. |
| `recovery_terminated` | ambiguous | no | yes | Consumer chose `TERMINATE` for the recovered action. |
| `invalid_recovery_decision` | ambiguous | no | yes | Illegal/absent recovery decision (or `CONTINUE`/`REPLAN` without a concrete reconciliation verdict). |

### `journal_error_code` (companion string on `RunResult`)

Set alongside a journal result. Examples seen in code: `run_id_required` (preflight),
and the journal's own CAS/busy/corrupt codes (DB-level). Treat any value as "the
journal could not be advanced safely"; recover on restart with the same `run_id`.

## 3. `TextInputError.code` (per-step log / journal, carries flags)

Raised by the text layer; carries `retryable`, `action_may_have_executed`,
`device_state_may_have_changed`. These land in the **step record** (and journal),
and map into `RunResult.result` as `text_ambiguous_execution` (when
`action_may_have_executed`) or `exec_refused` otherwise.

| `code` | Source | `action_may_have_executed` | Retryable | Handling |
|---|---|---|---|---|
| `text_unsupported_by_backend` | generic/backend `validate` | no | no | Charset not supported (e.g. Chinese on ASCII backend). Configure a Unicode backend; fail-closed. |
| `bad_text` / `text_empty` / `text_too_long` / `text_control_character` | `action_codec.validate_action` | no | no | Generic/Unicode safety rejection (≤80 code points, no control/format/surrogate). |
| `text_backend_unavailable` | backend preflight | no | no | Assistive IME not installed / no restorable default IME. Install/configure, then retry. |
| `text_backend_preflight_failed` | backend | no | **yes** | Transient preflight ADB error. |
| `text_backend_not_ready` | backend | no | **yes** | IME accepted the switch but was not active / OpenATX cancelled (`result=0`). Retry once warm. |
| `text_input_failed` | backend | **sometimes** | no | Injection error; may have landed (OpenATX unparsable receipt flags a possible injection). |
| `text_backend_restore_failed` | backend `finally` | **yes** | no | IME restore failed **after** a possible injection → `device_state_may_have_changed=True`. **Never replay**; restore IME and verify the field. |

## 4. Journal / recovery **core facts** (not `RunResult`)

Internal vocabulary on journal records and `RecoveryReport` — facts, not Consumer
decisions. The Core never reads them as a business state.

- **Persisted states:** `PREPARED`, `DISPATCHING`, `EXECUTED`, `FINALIZED`,
  `NOT_EXECUTED`, `OUTCOME_UNKNOWN`.
- **Core facts (`core_fact`):** `SAFE_TO_REOBSERVE` (PREPARED/NOT_EXECUTED),
  `EXECUTION_CONFIRMED` (EXECUTED), `AMBIGUOUS_EFFECT` (DISPATCHING/OUTCOME_UNKNOWN),
  `NO_PENDING_ACTION` (FINALIZED), plus `PACK_INCOMPATIBLE` and
  `RECONCILIATION_UNAVAILABLE` on recovery.
- **`dispatch_certainty`:** `CERTAINTY_NONE` / `CERTAINTY_POSSIBLE` /
  `CERTAINTY_CONFIRMED`.
- **Reconciliation verdict (`reconcile_action`):** `ALREADY_APPLIED` /
  `NOT_APPLIED` / `UNKNOWN`.
- **Consumer recovery decision (`recovery_decide` returns):** `CONTINUE` /
  `REPLAN` / `MANUAL_REVIEW` / `TERMINATE`. `CONTINUE`/`REPLAN` require a concrete
  `ALREADY_APPLIED`/`NOT_APPLIED` verdict from a compatible Pack.

See [action-journal-design.md](action-journal-design.md) for the full lattice.

## 5. Pack verdicts (`resolve` / `verify`)

A **fixed three-value** vocabulary the Pack returns (not a result string):

| Verdict | Core behavior |
|---|---|
| `ALLOW` | Proceed (validated, then approve/verify/execute). |
| `REOBSERVE` | Zero ADB, re-observe (bounded by `max_reobserve`). For temporarily-unusable targets, **not** safety violations. |
| `BLOCK` | Fail-closed stop; the Core never auto-retries a `BLOCK`. |

## 6. Pack `reason` & guard / App stop reasons — **extension space**

These are **not** fixed Core constants; Mano passes the Pack's string through:

- **`resolve`/`verify` `BLOCK` reason** becomes `RunResult.result` (defaulting to
  `resolve_blocked` / `verify_blocked` when empty).
- **`pack.guard(xml, foreground)` return** becomes `RunResult.result` **verbatim**.
  Example values from the Duolingo pack: `left_app`, `paywall_handed_to_human`,
  `energy_depleted`, `lesson_done`. Each Pack defines its own; document them in
  the Pack, not here.

Because these are Pack-defined, a Consumer must treat an **unrecognized**
`RunResult.result` as "a Pack/guard stop reason" (map it from the Pack's own
contract), not as a Core error.
