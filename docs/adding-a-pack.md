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
  path with coordinate-embedded targets as described above. Real resolve/verify
  from the answer-option XML (text-node hit-test, since the option nodes carry no
  `resource-id`/`clickable`) is deferred to Batch 2.
- **`wait`** is non-device-modifying: never approved, never revalidated, never
  counted in `actions_executed`. `approve` fails closed on `False`/exception.

### Non-goals (0.x)

Not implemented in the Core yet, and not claimed by shipping this loop: crash
recovery / durable journaling, lease/fencing for concurrent runners, XML
normalization or a stability fingerprint, visual↔XML consistency checking, and
any app logic (Duolingo/转转 semantics live only in their Packs, never in the
Core).


