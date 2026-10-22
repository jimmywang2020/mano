# Adding a new app pack

A pack teaches mano how to complete a task in one specific app. The engine
(`mano/`) stays untouched; everything app-specific lives under
`packs/<your-app>/`.

## 1. Scaffold

```
packs/<your-app>/
  __init__.py
  prompts.py   # system prompt + per-step instruction
  guards.py    # deterministic stop/boundary detection
  run.py       # driver loop (copy packs/duolingo/run.py and adapt)
```

Register the package in `pyproject.toml` under `[tool.setuptools].packages`.

## 2. Prompts (`prompts.py`)

Provide two functions:

- `system_prompt(width, height) -> str` — the `mobile_use` tool schema plus
  global rules (coordinate space is normalized to 1000×1000; never purchase;
  when to terminate success/failure).
- `instruction(history_json) -> str` — task description + per-screen operating
  knowledge, and the recent action history so the model avoids repeats.

Keep the model's job to **semantic judgment**. Anything mechanical and
repetitive should move to a solver (step 4).

## 3. Guards (`guards.py`)

Guards run **before** each action and stop the loop deterministically:

- `foreground_pkg(adb)` — bail out if the app is no longer in the foreground.
- `paywall_detected(xml)` — hand control back to the human on payment screens.
- A **task-boundary** detector — the single most important guard. A VLM does
  not know when "one task" ends; app funnels (new challenges, upsells, ads)
  will lure it onward. Detect the boundary from the XML and terminate.

Guards match on **real values** from a captured XML, not on field names. Run
`run.py dump`-style inspection first and copy actual on-screen strings.

## 4. Deterministic solvers (optional but recommended)

When a screen exposes its structure in the uiautomator XML and the subtask is a
mechanical multi-tap (matching, ordering, grid selection), a solver is far more
reliable than the VLM's cross-frame spatial tracking.

Pattern (see `packs/duolingo/matching_solver.py`):

1. `detect(xml)` — is this screen an *in-progress* instance of the subtask?
   Require both a title marker **and** real parsed cells, so a completed screen
   routes back to the VLM instead of spinning.
2. Parse cells from XML (`mano.ui_xml.node_texts`, `parse_bounds`); coordinates
   are **real pixels** — no normalization needed for taps.
3. If the subtask needs semantic content (e.g. the correct order/pairing), make
   **one text-only VLM call** to decide it, then let code execute the taps and
   re-dump to converge. This "VLM decides content, code executes taps with XML
   state tracking" hybrid generalizes across apps.

Wire the solver into the loop's routing branch: if `detect(xml)` is true, call
`solve(...)` and `continue`; otherwise fall through to the VLM.

## 5. Keep a VLM fallback

Some apps expose empty or non-semantic XML (canvas/game surfaces). Always keep
the VLM path so the loop degrades gracefully when no solver applies.

## 6. Evidence & evaluation

The driver writes `runs/<ts>/steps.jsonl` + screenshots + XML per step. Use it
to ground-truth behavior (read the real log before concluding anything). For
measuring generalization, keep a **held-out set** of apps/tasks where no
app-specific code is allowed, and report zero-prep completion rate, step
efficiency, repeat/wait-stall rates, safety-guard triggers, and the human
effort needed to onboard a new app.

## 7. Thin runner (`mano/runner.py`) and the resolve/verify loop

The thin runner is the app-agnostic alternative to copying `run.py`: it takes a
`Pack` (see `mano/pack.py`) and drives one observation → one action per step.
The loop separates three responsibilities on purpose:

- **Core = mechanism, no app knowledge.** It captures evidence, runs the generic
  anti-loop guards, enforces the action lifecycle, validates every resolved /
  verified action against a single action whitelist, and freezes the action's
  *execution payload* (action type + non-coordinate side-effects). It never
  contains app strings or page semantics.
- **Pack = app judgment.** It owns `guard` (deterministic stop), `resolve` (turn
  the raw model action into a stable semantic target + a physical action), and
  the optional `verify` (re-confirm the *same* semantic target on the latest page
  and re-resolve only the coordinates).
- **Consumer = business authorization.** The `approve(ResolvedAction, Observation)`
  veto decides whether a resolved semantic action is allowed to touch the device.

### Observation is bounded, not atomic

`capture_observation()` (`mano/observation.py`) takes one screenshot + **one** XML
dump + two foreground reads, in the order
`foreground_before → screenshot → XML → foreground_after`, and marks the bundle
*stable* iff the screenshot and XML both succeeded, `foreground_before ==
foreground_after`, and `skew_ms <= max_skew_ms`. The `skew_ms` *duration* is
measured with the **monotonic** clock (`time.monotonic_ns`), not wall time, so a
system-clock step/rollback (NTP, DST) cannot forge a negative or huge span; the
epoch timestamps are still recorded as evidence only. It is deliberately **NOT** an
atomic snapshot: the screenshot and the XML can still describe slightly different
instants, and the Core does **not** try to detect cross-modal contradictions
(e.g. XML says "result" while the PNG still shows a spinner). The raw XML
fingerprint is recorded as **evidence only** — it does **not** gate stability, so
a page with a legitimately animating/churning XML (carousels, timers) is not
falsely reported unstable. Deciding whether a page is "ready enough" is the
model's and the Pack's job, not the Core's. External consumers should call
`capture_observation()` + `propose_action()` instead of rolling their own
screenshot/ui_dump/VLM code, so these boundaries apply uniformly.

### The per-step order

```
capture Observation              # single bounded bundle (model + guard share it)
 -> Pack.guard                   # deterministic stop
 -> model proposes raw action
 -> Pack.resolve                 # -> ResolveDecision(verdict, semantic_target, resolved_action)
 -> classify the RESOLVED action # all anti-loop guards key on the resolved action
 -> Consumer approve(ResolvedAction)
 -> capture RevalidationView     # foreground-sandwiched re-read of the latest page
 -> Core foreground check        # foreground changed -> zero ADB, re-observe
 -> Pack.verify  OR  legacy fallback
 -> execute the final physical action
```

Key points when integrating:

- **Guards run on the resolved action.** A model `wait` may `resolve` to a device
  `click`; repeat/oscillation/wait-stall guards and classification all use the
  resolved action, so the Pack's rewrite is what actually governs the loop.
- **Resolved / verified output is validated (fail-closed, zero ADB).** Every
  `resolve` ALLOW result and every `verify` ALLOW result is checked against the
  single action whitelist in `mano/action_codec.py` (`validate_action`): the
  action must be in the allowed set, carry no unknown fields, have coordinates
  inside `0–1000`, and pass per-action field checks (text charset, keyevent name,
  `terminate` status ∈ {success, failure}). A bad resolve stops the run with
  `resolve_invalid_action`; a bad verify stops with `verify_invalid_action`. The
  same whitelist is re-applied inside `execute_action` as defense in depth, so no
  caller can drive the device with an action the whitelist would reject. (One
  consequence: a `terminate` whose status can't be recovered is now rejected as an
  invalid action rather than silently executed.)
- **Anti-loop keys on the SEMANTIC signature, not pixels.** The repeat and
  oscillation guards compare `semantic_signature = (semantic_target, action type,
  non-coordinate side-effects)` and deliberately **exclude coordinates**, so a tap
  on the same control whose pixel position drifts a few points is still recognized
  as the same action. Only *successfully executed device actions* enter the loop
  history — `REOBSERVE` / blocked / failed steps never pollute it — and a
  `wait` additionally clears the re-observe streak. The raw physical signature is
  still journaled (`physical_signature`) as evidence alongside `semantic_signature`.
- **`resolve` verdicts.** `ResolveDecision.verdict` is `ALLOW` / `REOBSERVE` /
  `BLOCK`. `BLOCK` stops the run (fail-closed; a `resolve` exception or a wrong
  return type is treated as `BLOCK`/`resolve_error`). `REOBSERVE` discards the
  step with zero ADB and re-observes.
- **Temporarily-unusable targets should `REOBSERVE`, not `BLOCK`.** If a target is
  only momentarily invisible / not-ready / mid-animation (not a safety violation),
  `resolve` (or `verify`) should return `REOBSERVE` with a short `reason`; the Core
  re-observes with zero device side effect. A genuine safety boundary (wrong page,
  irreversible mismatch, ambiguity that must not be guessed) MUST still return
  `BLOCK` — the Core never auto-retries a `BLOCK`.
- **Bounded replan feedback (one-shot, generic).** After any *zero-side-effect*
  `REOBSERVE` (a `resolve` REOBSERVE, a `verify` REOBSERVE, the Core foreground
  switch, or the legacy raw-XML-change re-observe), the Core carries a small,
  bounded fact into the **immediately next** model planning call only: `{source,
  reason, rejected_action, semantic_target?}` appended as a stable
  `PREVIOUS_ACTION_REJECTED <json>` line on that one instruction (also logged as
  `replan_feedback` on the producing step and `replan_feedback_consumed` on the
  consuming step). It is **one-shot** (replaced if the next step also REOBSERVEs,
  gone after any allowed action), it is **never** added to the planning `history`,
  and it **never** creates an Action Journal record or touches ADB. The Core
  carries runtime facts only — it does not interpret App semantics. The `reason` is
  length-capped (1024 chars, UTF-8 safe) and defaults to `reobserve_required` when
  empty. Correction stays **bounded by the existing `max_reobserve`**; at the limit
  the run still ends with `reobserve_required` (and remains subject to `max_steps` /
  timeout). A successful allowed action clears the consecutive re-observe streak.
  Fail-closed paths — `BLOCK`, a `resolve`/`verify` exception or unknown verdict,
  an invalid `semantic_target`, a whitelist / anti-swap / text-backend rejection,
  an approve veto, an ADB execution failure, an ambiguous/crash execution window,
  or a Pack-declared non-retryable error — never emit replan feedback.
- **A page change is NOT an invalid action.** When the revalidation shows the XML
  moved but the foreground is unchanged, that is expected churn. If the Pack
  implements `verify`, the Core asks it to re-confirm the same `semantic_target`
  on the latest page and (optionally) hand back fresh coordinates; only a
  foreground switch is treated by the Core itself as "re-observe".
- **Anti-swap.** `verify` may re-resolve `coordinate`/`coordinate2` only. The Core
  compares `execution_payload()` (action type + non-coordinate side-effects;
  coordinates and diagnostic metadata excluded) of the verified action against the
  resolved action; any difference blocks with `verify_semantic_swap_blocked`.
  `verify` is also fail-closed.
- **`verification_mode`.** Packs that expose a callable `verify` run in
  `pack_verify` mode. Packs **without** `verify` run in `legacy_raw_xml_fallback`
  mode: the Core keeps the foreground safety check, and a raw-XML change simply
  triggers a re-observe. This fallback exists **only** to migrate pre-resolve
  packs; it is not the long-term contract, and it must not re-introduce XML
  normalization. New packs should implement `resolve` (mandatory) and, when the
  app has moving targets, `verify`. **Known limitation (legacy only):** because a
  legacy pack's `semantic_target` is just a deterministic signature of the action
  (the Duolingo adapter embeds the coordinate as `xy=x,y`), its semantic dedup
  *degrades to physical dedup* — a tap on the same control whose pixels drift is
  NOT recognized as a repeat, so the loop relies on the `max_steps` / wait-stall
  bounds to stop rather than on semantic de-duplication. This is an honest
  degradation of the migration path, **not** a Core feature: real resolve/verify
  (Batch 2) removes it by producing a stable, coordinate-free target.
- **Duolingo is still a legacy passthrough.** `packs/duolingo/pack.py` does not
  interpret any control and has no `verify`; it runs the `legacy_raw_xml_fallback`
  path with coordinate-embedded targets as described above. A legacy pack MAY
  temporarily use a coordinate-bearing identity, but that does **not** meet the
  "Stable semantic identity" contract below, is **not** a model for new packs, and
  MUST switch to a coordinate-free stable identity when it wires in real
  `resolve`/`verify`. Real resolve/verify from the answer-option XML (text-node
  hit-test, since the option nodes carry no `resource-id`/`clickable`) is deferred
  to Batch 2.
- **`wait`** is non-device-modifying: never approved, never revalidated, never
  counted in `actions_executed`. `approve` fails closed on `False`/exception.
- **`on_action_executed` (optional post-execute hook).** A Pack may expose
  `on_action_executed(*, executed: ResolvedAction) -> None` to advance its own
  app state after a change lands (e.g. "search query typed" so the next step
  resolves "click search" as a click, not a second type). The Core calls it once
  per **successfully executed device action**, after ADB confirms the change and
  before the next observation; it is **never** called on `wait`/`terminate`,
  `BLOCK`/`REOBSERVE`, approve veto, foreground change, verify rejection,
  anti-swap, or ADB failure. `executed` carries the `semantic_target`, the final
  action handed to ADB (post-`verify` coordinates), and the final-stage metadata
  — `verify`'s when `verify` ran (**even `{}`**, which means "no fresh structured
  evidence"; the Core never falls back to the possibly-stale resolve metadata),
  else resolve's. Both are **deep copies**, so the hook cannot mutate the audited
  action/metadata. If the hook raises, the device change already happened, so the
  run stops with `post_execute_hook_failed` (recorded as `post_execute_error`, not
  `exec_error`) and the action is **never replayed**. Exactly-once holds only
  within one process's normal control flow — it is **not** promised across a
  crash (ADB may succeed then the process die before the hook) in the default
  (`journal=None`) mode. With a durable `journal=` wired, the new hook ABI plus
  crash recovery close that gap; see "Durable action journal + crash recovery"
  below. Core passes only the execution facts and never interprets the metadata.

### Text input is backend-dependent

The action protocol is unchanged: a Pack still emits `{"action": "type",
"text": "..."}` and never picks a transport (no `backend`/`ime` field). *How* the
text reaches the device is a **text backend** chosen when the executor (`Adb`) is
constructed:

- `AsciiAdbTextBackend` (default) — `adb shell input text`, shell-safe ASCII only.
- `OpenAtxAdbKeyboardBackend` (optional, explicit) — Unicode via the OpenATX
  uiautomator2 *AdbKeyboard* IME; success is proven by the broadcast receipt
  (`result=-1`), an explicit `result=0` is retried (bounded), never
  auto-installed, temporarily switches IME and restores it.
- `AdbKeyboardTextBackend` (**legacy / deprecated**) — the original ADB-Keyboard
  (`ADB_INPUT_B64`) backend; real-device testing proved a cold-start false
  success (its `_await_active` confirms only the `default_input_method` value,
  which flips before the receiver is ready). Do not adopt for new Consumers; kept
  only until Consumers migrate to the OpenATX backend.

Validation is two orthogonal layers, both reached through one combined entry
`validate_executable_action(adb, action)` (used by the runner on `resolve` and
`verify` output **and** by `execute_action`, so runner-allows can never diverge
from executor-rejects):

- `action_codec.validate_action` — generic/Unicode safety for every backend:
  non-empty, ≤ 80 code points, no NUL/CR/LF, no control/format/surrogate
  characters (`Cc`/`Cf`/`Cs`); never normalizes, truncates or rewrites.
- `backend.validate(text)` — a **pure** charset/transport check (no device). The
  ASCII backend rejects parentheses/Chinese as `text_unsupported_by_backend`; the
  OpenATX and legacy ADB-Keyboard backends accept them.

Capability is read-only and declared by the Consumer, not the Pack: Core exposes
`backend.preflight(adb)` (read-only ADB) and the pure `backend.validate`; the
Consumer reads its own canonical query, `validate` + `preflight`, and only starts
once the capability is satisfied — "fail before the run", not mid-ADB. A Pack that
needs Unicode text therefore relies on the Consumer having configured a Unicode
backend; with the default ASCII backend such text fails closed (zero device
change), it is **never** silently downgraded, truncated or garbled.

Backend failures carry a structured code plus `retryable` and
`action_may_have_executed`. The important one is `text_backend_restore_failed`
(IME restore failed *after* a possible injection): the execution state is
ambiguous, so the run stops as `text_ambiguous_execution` and the input is
**never replayed**. No text failure is auto-retried by the Core.

> Device status: the **OpenATX** backend passed a real-device PoC (5/5 MuMu
> cold starts, incl. `iPad Pro (2022) 11.0英寸`) but is **not yet certified in
> this repo**; its `type_text` must still pass the full MuMu cold/hot-start
> acceptance before any Consumer enables Chinese queries (`FakeAdb` tests alone
> do not certify it). The **legacy** ADB-Keyboard backend is deprecated: it
> reports available yet can silently drop a cold-start first injection. The APK
> install/version is a Consumer/deployment concern, not the Pack's.

### Stable semantic identity (Pack contract)

`semantic_target` is the backbone of every anti-loop guard, the Consumer
`approve`, `verify` re-location, and `on_action_executed` state attribution. It
MUST represent the **stable App-level role and intent** of a logical control, not
how that control happens to look right now.

- **Stability rule.** *While the same logical target still exists and is uniquely
  identifiable*, changes to coordinates, bounds, position, dynamic presentation
  text, badges, timers, carousel content, or unrelated XML MUST NOT change the
  `semantic_target`. (The precondition matters: if the control genuinely became a
  *different* control, the Pack must not keep the old identity — it should
  re-resolve or, when ambiguous, `REOBSERVE`/`BLOCK`.)
- **May participate in identity:** a page/feature domain (`home`, `search`,
  `filter`), a stable control role (`search_entry`, `confirm_button`), a value
  that is itself a stable business choice (`capacity:256G`), an App-provided
  stable object id, or a Pack-defined stable state. The naming below is
  **illustrative only** — any stable, unique, coordinate-free string is valid and
  the Core enforces **no** naming format (e.g. both `home.search_entry` and
  `home_search_entry_visible` are fine; packs need not rename an existing one):

  ```text
  home.search_entry
  filter.capacity:256G
  filter.confirm
  product.open:<stable_product_id>
  ```

- **MUST NOT be the source of identity** (unless the thing itself is the target's
  stable business value): `coordinate`/`coordinate2`, pixel bounds, XML node order
  or list position, observation/screenshot id, XML fingerprint, time/countdown,
  count badges, carousel/recommendation/ad text, dynamic placeholder, the model's
  natural-language description, or any transient session id / random value. These
  go in `metadata`.

  ```text
  # wrong (volatile / collides / coordinate-bound)
  click:431,53
  labels:["iphone 15"]
  node[3]
  xml_sha256:...

  # right (structural role, coordinate-free)
  home.search_entry
  ```

  Note: this is not "forbid all visible text". A stable filter value like `256G`
  may be part of the identity; dynamic placeholders, prices and countdowns must
  not be.

- **Uniqueness.** Stable also means collision-free: the same logical target maps
  to one identity across observations, and *different* logical targets map to
  *different* identities. When a control cannot be uniquely determined, return
  `REOBSERVE`/`BLOCK` — never keep an identity by treating a nearby or
  similar-looking control as the original target.
- **`verify` must re-find the same target.** `approved_intent` is the frozen
  stable identity. Same target still uniquely present → `ALLOW` (coordinates may
  be updated); page not ready / target temporarily gone / ambiguous → `REOBSERVE`;
  wrong app / dangerous page / the control clearly became a different one →
  `BLOCK`. `verify` must not rename the target, substitute an adjacent or
  similar-text control, or change the identity because coordinates drifted. The
  matched text and coordinates are re-location *evidence* and go in verify
  `metadata`.
- **`metadata` is auditable, JSON-serializable, and never part of identity.**
  Dynamic placeholder, coordinates and match evidence live here.
- **Core boundary.** The Core does not parse or infer identity content: it only
  requires a non-empty string on `ALLOW`, freezes the approved `semantic_target`,
  derives the semantic signature from it, hands the *same* identity to `verify`,
  and forbids `verify` from swapping the action type / side-effect fields. Whether
  the string is genuinely stable and names the right App control is the **Pack's**
  responsibility; there is deliberately **no** Core regex check on target content
  (stable identity is expressed differently per app).

**Pack conformance tests (every production pack).**

1. Same control, coordinates change → identity unchanged.
2. Dynamic placeholder / recommendation text changes → identity unchanged.
3. An unrelated XML node is added → identity unchanged.
4. Target disappears → `verify` returns `REOBSERVE`, zero device action.
5. The same coordinate becomes a different control → must NOT `ALLOW`.
6. Multiple candidate targets present at once → `REOBSERVE` or `BLOCK`.
7. Two different controls never produce the same identity.
8. The same input resolved repeatedly serializes to a deterministic identity.

**Core contract / integration test (Core + a fake pack).**

9. When the same semantic target drifts in coordinates, the Core's repeat guard
   still recognizes it as the same action (covered in this repo by
   `tests/test_runner_observation.py::test_semantic_dedup_blocks_despite_coordinate_drift`
   and `tests/test_runner.py::test_oscillation_guard_blocks`).

**Case study (转转 home search box).**

```text
WRONG:
  the home search box placeholder changes from "iphone 15" to "ipad"
  and the semantic_target changes with it (labels:["iphone 15"] -> labels:["ipad"]).

RIGHT:
  the Pack recognizes the role from App structure (the unique wide search input
  at the top of home + its adjacent search button) and always emits a stable
  identity; the placeholder and coordinates go only into metadata.
```

### Non-goals (0.x)

Not implemented in the Core yet, and not claimed by shipping this loop:
lease/fencing for concurrent runners (beyond the journal's at-most-one-open-action
rule), XML normalization or a stability fingerprint, visual↔XML consistency
checking, and any app logic (Duolingo/转转 semantics live only in their Packs,
never in the Core). Durable journaling + crash recovery ARE now available as an
opt-in (`journal=`); see the next section.

### Durable action journal + crash recovery (optional)

`run(..., journal=ActionJournal, run_id="stable-id", recovery_decide=cb)` wires a
durable, per-action SQLite journal (`mano/action_journal.py`). With `journal=None`
(default) none of this is active and the legacy hook ABI is unchanged. The single
invariant: **a device action that sits in the blurry execution window is never
auto-replayed after a crash.**

- **Fixed execution order (every device-modifying action):** `prepare → PREPARED
  → DISPATCHING → execute → EXECUTED → hook → FINALIZED`. `PREPARED` and
  `DISPATCHING` are durable *before* ADB is touched, so a crash mid-dispatch is
  recoverable, not replayable.
- **Preflight (before any observation / model / ADB):** a non-empty `run_id` is
  required; a hook present must use the new ABI and declare `hook_recovery`; a
  Pack with `reconcile_action` **or** an `IDEMPOTENT` hook must advertise non-empty
  `pack_id` + `reconciliation_schema_version`. Any failure returns
  `journal_config_error` with zero device change.
- **Exception classification at the execute seam:** text error with
  `action_may_have_executed=False → NOT_EXECUTED`; any ambiguous text error or
  other ADB failure → `OUTCOME_UNKNOWN`; ADB success but the `EXECUTED` write
  fails → the record stays `DISPATCHING`, the run stops, no hook. **Any** journal
  CAS conflict / DB-busy / corruption (including a failed state write after an ADB
  error) stops the run with `journal_error`; the Core never silently degrades to
  running without the journal, and never swallows a journal write failure.
- **Recovery runs before the normal loop** for every open action of that
  `run_id`, and only reads the device:
  - `PREPARED` → ADB never entered → close `NOT_EXECUTED` (the stale intent is
    discarded; old coordinates are never replayed).
  - `EXECUTED` → ADB already happened, **never** replayed. Closed per the
    **persisted** `hook_recovery` (not the current Pack's): `NOT_APPLICABLE` closes
    straight to `FINALIZED`; `IDEMPOTENT` replays only the hook, and only if the
    current Pack is still `IDEMPOTENT` **and proves the same identity** (non-empty
    `pack_id` + `reconciliation_schema_version`, equal on both sides) — any
    mismatch or missing identity → `PACK_INCOMPATIBLE`, no replay, record stays
    `EXECUTED`; `NOT_REPLAYABLE` is never replayed and is handed to the Consumer.
  - `DISPATCHING`/`OUTCOME_UNKNOWN` → take one **fresh** recovery observation
    (append-only evidence refs), then ask `reconcile_action`. An **unstable**
    recovery observation is appended as evidence but never drives a verdict
    (forced `UNKNOWN`); `reconcile_action` is not called and the action is not
    closed. `CONTINUE`/`REPLAN` is legal only with a concrete `ALREADY_APPLIED` /
    `NOT_APPLIED` verdict from a compatible Pack; otherwise the run stops.
- **Pack compatibility on recovery** compares BOTH the persisted and current
  `pack_id` and `reconciliation_schema_version` (a different `pack_id` reusing the
  same schema string → `PACK_INCOMPATIBLE`); this single identity check gates both
  `reconcile_action` and `IDEMPOTENT`-hook replay. `pack_digest` is audit-only.
- **Evidence isolation:** journal mode writes each process's screenshots / XML /
  `steps.jsonl` under `run_dir/attempts/<uuid>/`, so a restart never overwrites
  the crashed run's evidence. Every observation ref (pre-execution and recovery)
  carries capture timing (`capture_started_at` / `capture_finished_at` /
  `skew_ms`) and `stability`; recovery refs are append-only.
- **Pluggability:** the Core uses only the `ActionJournal` Protocol
  (`prepare` / `transition` / `load_open_actions` / `get_record`); a `get_record`
  returning `None` for an in-flight action is treated as `JOURNAL_CORRUPT`.


