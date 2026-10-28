# Integration guide (Consumer)

How a third party installs `mano`, constructs the device/backend/Pack/client, and
drives one run through the **thin runner** (`mano.runner.run`). This is the
Consumer's view; writing a Pack is covered in
[adding-a-pack.md](adding-a-pack.md).

> **0.x compatibility.** The import paths and call shapes below are the
> **v0.1.2-verified integration surface**, not a frozen ABI. `mano` is still 0.x;
> later changes ship through a new tag + a [CHANGELOG.md](../CHANGELOG.md) entry +
> migration notes. Pin to an immovable tag.

## 1. Install and verify

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'mano @ git+https://github.com/jimmywang2020/mano.git@v0.1.2'

python -c "import importlib.metadata as m; print(m.version('mano'))"   # 0.1.2
```

`mano` has **no third-party runtime dependencies** (standard library only). `uv`
is optional. For a Git (non-editable) install, `direct_url.json` records
`requested_revision == v0.1.2` and a pinned `commit_id`; confirm those match the
tag you intended.

## 2. Verified import paths (v0.1.2)

```python
from mano.adb_executor import Adb
from mano.runner import run, RunResult, propose_action
from mano.pack import (
    Pack, ResolveDecision, ResolvedAction, VerificationResult,
    ALLOW, REOBSERVE, BLOCK,
)
from mano.text_input import (
    AsciiAdbTextBackend, OpenAtxAdbKeyboardBackend,
    TextInputError, TextInputCapability,
)
from mano.observation import Observation, capture_observation, DEFAULT_MAX_SKEW_MS
from mano.gui_plus_client import GuiVLM
# Optional, only for durable/production runs:
from mano.action_journal import SQLiteActionJournal, RecoveryReport, ActionJournalError
```

The top-level package (`import mano`) re-exports only `ManoError` /
`ManoNetworkError`; everything else is imported from its module as above. (A
convenience re-export is a possible later change, not part of v0.1.2.)

## 3. Construct the device (`Adb`) and choose a text backend

The **text backend is chosen by the Consumer when it builds `Adb`** — the model
and the Pack never pick one. The default is ASCII.

```python
# ASCII (default): `adb shell input text`, shell-safe ASCII only.
adb = Adb("127.0.0.1:16384")                       # or set MANO_ADB_PATH / serial

# Unicode (e.g. Chinese queries): explicit OpenATX AdbKeyboard backend.
adb = Adb("127.0.0.1:16384", text_backend=OpenAtxAdbKeyboardBackend())
```

Preflight the backend **before** a run that needs Unicode (read-only ADB); start
only once the capability is satisfied — "fail before the run", not mid-ADB:

```python
cap = adb.text_backend.preflight(adb)   # TextInputCapability(name, available, unicode, reason)
if not cap.available:
    raise SystemExit(f"text backend not ready: {cap.reason}")
```

The Core never silently downgrades OpenATX → ASCII: out-of-charset text fails
closed with zero device change. The APK install/version is your deployment
concern (see [device-acceptance.md](device-acceptance.md) for the accepted
device combination).

## 4. Build the VLM client, the Pack, and the `run_dir`

```python
client = GuiVLM()            # reads MANO_API_KEY (or DASHSCOPE_API_KEY), MANO_MODEL, MANO_BASE_URL
pack = YourPack()            # implements the Pack protocol (see adding-a-pack.md)
run_dir = Path("runs/my-run-0001")
```

`GuiVLM` only proposes actions; it never touches ADB. It accumulates usage
metrics (`call_count`, `retries`, `prompt_tokens`, `completion_tokens`) that the
runner surfaces on `RunResult`.

## 5. Minimal `runner.run()` call

```python
from pathlib import Path
from mano.runner import run

res = run(
    adb,
    client,
    pack,
    run_dir,
    max_steps=40,
    approve=my_approve,     # optional; see §6
)
print(res.result, res.total_steps, res.actions_executed)
```

Full signature (keyword-only after `run_dir`):

```python
run(adb, client, pack, run_dir, *,
    max_steps=40, step_timeout_s=900, settle_s=1.0, capture_settle_s=0.4,
    max_skew_ms=6000, max_unstable=3, max_reobserve=3,
    approve=None, run_id=None, journal=None, recovery_decide=None) -> RunResult
```

> This minimal call shape (import paths, arguments, result handling) is locked by
> an **offline** test (`tests/test_docs.py`) using fake ADB / VLM / Pack — no real
> device, no real VLM, no taps. The snippet above is the real-device form; the
> test exercises the same shape with fakes.

## 6. `approve` — the Consumer veto

```python
def my_approve(resolved: ResolvedAction, observation: Observation) -> bool:
    # Business authorization only: allow/deny a device-modifying semantic action.
    return is_allowed(resolved.semantic_target)
```

- Called once per **device-modifying** resolved action, after `resolve` and
  before re-validation / `verify` / execute.
- It is **business authorization**, not a safety gate for malformed actions (the
  Core already validates those). Return `False` to veto; an exception or `False`
  **fails closed** (`approve_rejected`). `wait` is never sent to `approve`.
- `approve` is **not** a queue, lease, or retry budget — those are the Consumer's
  own systems; the Core does not provide them.

## 7. `journal=None` vs a durable journal

`journal=None` (default) keeps today's behavior and the legacy
`on_action_executed(*, executed)` hook ABI; crash recovery is **not** active.

- **Experiment / read-only / supervised development** may run without a journal.
- **This project's rule:** an **unattended** production run that may modify device
  state MUST enable a durable journal, a stable `run_id`, and `recovery_decide`.
  This is a Consumer safety requirement, not a Core-enforced check on every device
  action.

```python
journal = SQLiteActionJournal("state/mano-journal.sqlite3")

res = run(
    adb, client, pack, run_dir,
    run_id="order-1d9803c5",          # STABLE: reused byte-for-byte across restarts
    journal=journal,
    recovery_decide=my_recovery_decide,
    approve=my_approve,
)
```

- **`run_id` must be stable across restarts** (same logical task → same string).
  With a journal, a missing/empty `run_id` fails closed with
  `journal_config_error` before any device touch.
- The journal is **not** a task queue and does **not** replace your lease /
  fencing / retry budget. Its single invariant: a device action in the blurry
  execution window is **never auto-replayed** after a crash.
- **`recovery_decide(report: RecoveryReport) -> str`** is called before the normal
  loop when an open action is recovered for `run_id`. It returns one of the Core
  recovery decisions (`CONTINUE` / `REPLAN` / `MANUAL_REVIEW` / `TERMINATE`); an
  illegal/absent decision stops the run (`invalid_recovery_decision`). `CONTINUE` /
  `REPLAN` are legal only with a concrete `ALREADY_APPLIED` / `NOT_APPLIED`
  reconciliation verdict. See [action-journal-design.md](action-journal-design.md)
  for the recovery contract and [result-and-error-reference.md](result-and-error-reference.md)
  for the facts on `RecoveryReport`.

## 8. `RunResult` — current real fields

`RunResult` (from `mano.runner`) currently carries **exactly** these fields — do
not depend on fields that are not listed here:

| Field | Type | Meaning |
|---|---|---|
| `result` | `str` | Terminal result code (see below and the reference doc). |
| `total_steps` | `int` | Steps attempted. |
| `actions_executed` | `int` | Device-modifying actions that dispatched (excludes `wait`). |
| `vlm_calls` | `int` | Model calls. |
| `retries` | `int` | Model-call retries. |
| `prompt_tokens` | `int` | Raw usage (no pricing — pricing is the caller's job). |
| `completion_tokens` | `int` | Raw usage. |
| `wall_seconds` | `float` | Wall-clock duration. |
| `run_dir` | `str` | The run directory. |
| `reobserves` | `int` | Zero-side-effect re-observations taken. |
| `recovery_reports` | `list` | Recovery facts (journal mode). |
| `journal_error_code` | `Optional[str]` | Set when `result` is a journal error. |

`result` is a **string**, and it is **not** all one namespace: some values are
fixed Core codes, some come from the Pack/guard. Treat it as follows:

- `terminate_<status>` — the model terminated (`<status>` is the model's
  `terminate` status, e.g. `terminate_success` / `terminate_failure`);
  `terminate_from_exec` is a terminate surfaced from the execution path.
- Fixed Core stop codes — `max_steps`, `timeout`, `observation_unstable`,
  `vlm_error`, `vlm_unparseable`, `reobserve_required`, `resolve_invalid_action`,
  `verify_invalid_action`, `verify_semantic_swap_blocked`, `approve_rejected`,
  `exec_refused`, `text_ambiguous_execution`, `post_execute_hook_failed`,
  `oscillation_blocked`, `repeat_click_blocked`, `wait_stuck`,
  `invalid_semantic_target`, `invalid_resolve_verdict`, `invalid_verify_verdict`.
- Journal / recovery codes — `journal_config_error`, `journal_error`,
  `recovery_required`, `recovery_manual_review`, `recovery_terminated`,
  `invalid_recovery_decision` (with `journal_error_code` carrying the detail).
- **Pack/guard-provided** — a `resolve`/`verify` `BLOCK` returns the Pack's own
  `reason` (defaulting to `resolve_blocked` / `verify_blocked`). These are **not**
  a fixed Core constant set.

See [result-and-error-reference.md](result-and-error-reference.md) for each code's
side-effect risk, retryability, review need, and recommended handling. **Do not
infer "the action definitely did not execute" from a result string alone** —
`text_ambiguous_execution` and the journal `OUTCOME_UNKNOWN` facts exist precisely
because that inference is unsafe.

## 9. Structured text-error fields (per-step log, not `RunResult`)

A text-backend failure is classified with `retryable` and the two review flags
(`action_may_have_executed`, `device_state_may_have_changed`); these are written
into the **per-step `steps.jsonl` record** (and the journal), not yet projected as
structured fields on `RunResult`. If your handling needs the flags, read them from
the step log / journal. (A structured `RunResult` projection is a possible later,
tagged change — not available in v0.1.2.)

## 10. Experiment vs. production wiring

| | Experiment / supervised | Production (unattended, device-modifying) |
|---|---|---|
| `approve` | optional | required (business authorization) |
| `journal` | may be `None` | required (`SQLiteActionJournal`) |
| `run_id` | optional | required, **stable across restarts** |
| `recovery_decide` | optional | required |
| text backend | ASCII default fine | the backend your text needs, preflighted |

The Core does **not** provide: a task queue, device leases / fencing, a business
retry budget, pricing/quotes, or a human-review system. Those are Consumer
responsibilities built around `runner.run`.

## 11. Evidence, privacy, retention

Each run writes screenshots, uiautomator XML, and model output under `run_dir`
(journal mode: `run_dir/attempts/<id>/`). These can contain **account screens and
personal data**. The journal stores evidence **refs** plus a text hash/length, not
raw text. Treat the whole `run_dir` as sensitive: keep it out of version control
(it is git-ignored in this repo) and apply your own retention / deletion policy.

## 12. Current limitations (v0.1.2)

- The `Pack` protocol is a **0.x experimental interface**, not a frozen ABI.
- `RunResult` does **not** fully project the text-backend structured termination
  facts (see §9); read them from the step log / journal for now.
- Deterministic solvers are **not** fully migrated into the thin runner; the
  Duolingo example runs solver subtasks only in the baseline `run.py`.
- OpenATX Unicode input is `DEVICE_ACCEPTED` only on the **recorded** device
  combination; a new device/Android/emulator must re-run its acceptance matrix.
- `journal=None` provides **no** crash blurry-window protection.
