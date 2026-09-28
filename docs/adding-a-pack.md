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
