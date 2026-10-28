# Troubleshooting

Symptom-first diagnosis. Verified against `v0.1.2`. The result/error codes
referenced here are defined in
[result-and-error-reference.md](result-and-error-reference.md).

> Never "fix" a run by: silently switching the text backend, guessing/clamping a
> coordinate, auto-replaying an ambiguous or crash-window action, or bypassing
> `verify`. Those defeat the safety contract. The Core deliberately fails closed —
> resolve the root cause instead.

---

## ADB unavailable / device offline

**Symptom:** run raises an ADB error early, or `exec_refused` mid-run; screenshots
fail.

- Confirm the device: `adb -s <serial> get-state` → `device`.
- For a local TCP emulator the runner auto-tries `adb connect <serial>` once; if it
  still fails, reconnect manually: `adb connect 127.0.0.1:16384`.
- Ensure `adb` is on `PATH` or `MANO_ADB_PATH` points at an executable adb.
- MuMu: the bundled adb is used only as a fallback when no system adb is found.

## Text backend / IME not ready

**Symptom:** `text_backend_unavailable`, `text_backend_not_ready`,
`text_backend_preflight_failed`, or Unicode text silently refused.

- **Preflight before the run** (read-only): `adb.text_backend.preflight(adb)` →
  `TextInputCapability.available`. Start only when `available` is true. Do not
  start a Unicode task on the ASCII default backend.
- `text_unsupported_by_backend` on Chinese/parentheses = you are on the ASCII
  backend. Construct `Adb(serial, text_backend=OpenAtxAdbKeyboardBackend())`. The
  Core will **not** auto-downgrade or auto-switch — that is intentional.
- `text_backend_not_ready` (OpenATX `result=0` / IME not active yet): the assistive
  IME is installed but cold. Let it warm up and retry; this is retryable and the
  text was **not** applied.
- `text_backend_unavailable` ("no restorable default IME" / IME not installed):
  install/configure the assistive IME (deployment concern), confirm a sane default
  IME exists, then retry.

## Transport receipt succeeded but the App value is wrong/empty

**Symptom:** the backend reported success (e.g. OpenATX `result=-1`) yet the field
shows nothing or stale text.

- A **transport receipt is not** proof the field holds the value (see the three
  success facts in [adding-a-pack.md](adding-a-pack.md)). The Pack must re-read the
  **latest** page and verify the actual value equals the canonical text.
- If `verify` is implemented, this should surface as a `REOBSERVE` or `BLOCK` from
  the Pack, not as a silent "typed OK". If your Pack concludes success from the
  receipt alone, fix the Pack to add post-input page verification.
- Do **not** retype blindly — that can double-enter. Verify first.

## `text_ambiguous_execution`

**Symptom:** run stops with `text_ambiguous_execution`.

- A text failure (often `text_backend_restore_failed`) left an **ambiguous** state:
  the text may already be on the device. The Core **never replays** it.
- Restore the IME if needed, read the actual field value, and let a human / the
  Pack reconcile. Do not auto-retry the type.

## `vlm_unparseable` / `vlm_error`

- `vlm_error` = network error to the VLM → retry with backoff (the client already
  retries bounded times); check `MANO_API_KEY`, `MANO_BASE_URL`, connectivity.
- `vlm_unparseable` = the model's output could not be parsed after self-corrections.
  Check the `steps.jsonl` raw text; usually a prompt/schema mismatch in the Pack's
  `system_prompt`. Do not loosen Core parsing to force it through.

## `reobserve_required` (hit the re-observe cap)

**Symptom:** run ends `reobserve_required`.

- The page never became ready within `max_reobserve` zero-side-effect
  re-observations. Inspect the `replan_feedback` facts in `steps.jsonl` to see what
  the Pack kept rejecting.
- Legitimate causes: a genuinely slow page, a target that stays hidden, repeated
  foreground switches. Raise `max_reobserve` only if the page truly just needs more
  time; otherwise fix the Pack's readiness check. The Core will not guess a
  coordinate to "get past" it.

## Pack `BLOCK` (`resolve_blocked` / `verify_blocked` / a Pack reason)

**Symptom:** run stops with the Pack's BLOCK reason.

- `BLOCK` is a **safety boundary** the Pack raised (wrong page, irreversible
  mismatch, ambiguity that must not be guessed). The Core never auto-retries a
  BLOCK — that is correct. Do not wrap the run in a retry loop that ignores BLOCK.
- If the BLOCK is wrong, fix the Pack's `resolve`/`verify` logic; if it is right,
  route to human handling.

## `approve_rejected`

- Your `approve(resolved, observation)` returned `False` or raised (fails closed).
  Expected when business authorization denies the action. Check your approve logic
  and the `semantic_target` it was asked about.

## Journal: `journal_config_error`

**Symptom:** run returns immediately with `journal_config_error`, zero device
change; see `RunResult.journal_error_code`.

- Missing/empty `run_id` (`run_id_required`) → pass a stable, non-empty `run_id`.
- Hook present but on the legacy ABI, or a Pack with `reconcile_action`/`IDEMPOTENT`
  hook missing `pack_id` / `reconciliation_schema_version` → declare them.
- This is a wiring problem; fix config and re-run.

## Journal: `journal_error`

**Symptom:** run stops with `journal_error` mid-run.

- A CAS conflict (another runner won), DB busy, or a corrupt journal. The Core
  **never** degrades to running without the journal.
- Ensure only one runner acts per `run_id` (leases/fencing are the Consumer's job).
  Restart with the **same** `run_id`: recovery runs before the normal loop and
  closes/escalates the open action safely.

## Recovery: `recovery_required` / `recovery_manual_review` / `recovery_terminated` / `invalid_recovery_decision`

- `recovery_required` = an open action but no `recovery_decide` callback. Supply
  one returning `CONTINUE` / `REPLAN` / `MANUAL_REVIEW` / `TERMINATE`.
- `CONTINUE`/`REPLAN` are legal **only** with a concrete `ALREADY_APPLIED` /
  `NOT_APPLIED` reconciliation verdict from a compatible Pack; otherwise you get
  `invalid_recovery_decision`.
- `recovery_manual_review` / `recovery_terminated` = your decision stopped the run
  for human handling. Inspect the `RecoveryReport` facts (core fact, dispatch
  certainty, reconciliation verdict, evidence refs) before acting.
- A `PACK_INCOMPATIBLE` recovery fact means the current Pack identity differs from
  the persisted one (different `pack_id` or `reconciliation_schema_version`); the
  action is not replayed. Deploy the matching Pack to recover, or escalate.

## Evidence & privacy

- Each run writes screenshots / XML / model output under `run_dir` (journal mode:
  `run_dir/attempts/<id>/`). These may contain account data.
- Keep `run_dir` out of version control (git-ignored here) and apply your own
  retention/deletion. The journal stores evidence **refs** plus a text hash/length,
  not raw typed text — but screenshots/XML can still be sensitive.
