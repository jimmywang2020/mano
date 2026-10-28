# Contributing

`mano` is the **safety-critical Core + example Pack** for autonomous Android
operation. Contributions must preserve the fail-closed contract. Read this before
sending changes.

## Scope: Core / Pack / Consumer

Know which layer you are changing:

- **Core** (`mano/`) — device I/O, the thin runner, observation, the action
  journal, text backends, the `Pack` protocol. Generic and safety-critical.
- **Pack** (`packs/`) — app-specific `resolve` / `verify` / `guard` / solvers.
  The Duolingo pack is the example.
- **Consumer** — the application that calls `runner.run` (e.g. the 采购助手). It
  lives in its **own repository**, not here.

The Core must stay app-agnostic: no SKU/queue/quote/human-review logic, no Pack- or
model-specific text-backend selection, in `mano/`.

## Development environment

- **Python 3.10+** (matches `pyproject.toml` `requires-python = ">=3.10"`).
- Runtime has **no third-party dependencies** (standard library only). Do not add a
  runtime dependency without an explicit design decision.
- Dev extra: `pytest>=8`.

### Option A — `uv`

```bash
uv run --extra dev pytest -q
```

### Option B — standard `venv`

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

Both commands run the same full suite. The current baseline is **225 passed**:
221 Core tests plus 4 offline documentation-contract tests (`tests/test_docs.py`).
Adding tests raises this count — update the expected number in the same change and
in the handoff, do not leave it stale.

## Before you open a change

Run all of these locally:

1. **Tests** — `uv run --extra dev pytest -q` (or the venv form). All green.
2. **Whitespace** — `git diff --check` (no trailing whitespace / conflict markers).
3. **Doc links** — the offline `tests/test_docs.py` validates every in-repo
   relative markdown link and the minimal integration example; keep it green.

## Core behavior changes require failure-window tests

Any change to Core runtime behavior — the runner, the journal, observation, a text
backend, or the `Pack` ABI — **must** add or update tests that cover the **failure
window**, not just the happy path. In particular:

- crash / blurry-execution-window reconciliation (journal);
- fail-closed paths (invalid verdict, out-of-charset text, out-of-range coordinate,
  approve veto, verify semantic swap);
- "action may have executed" ambiguity (never-auto-replay).

A Core change without a matching failure-window test will not be accepted.

## Don't touch runtime code in a docs change

If your change is documentation, examples, or tests, the diff must contain **only**
`docs/` / `README` / `CHANGELOG` / `CONTRIBUTING` / examples / `tests/` — **no**
`mano/` runtime edits. If you discover that a doc can only be made accurate by
changing Core behavior, **stop and open a separate development plan** instead of
editing the Core inside the docs change.

Likewise, do not "drive-by" refactor, re-export top-level API, change the text
default from ASCII, or expand `RunResult` as part of an unrelated change.

## Releases & immovable tags

- Release tags (`v0.1.0`, `v0.1.1`, `v0.1.2`, …) are **immovable**: never move,
  delete, or re-point an existing tag. A mistake ships as a **new** tag.
- `main` is the release line. Cut a release by bumping the version in
  `pyproject.toml` **and** `mano/__init__.py` (they must agree), landing on `main`,
  then tagging that commit.
- Record every release in [CHANGELOG.md](CHANGELOG.md), grounded in real
  commits/tests/acceptance. Distinguish **behavior changes** from **metadata-only**
  releases. Mark anything unverifiable as "not recorded" — never fabricate a SHA.
- A **metadata-only** release (e.g. a wheel-name fix) needs reinstall + full tests
  from Consumers, but **no** device re-acceptance.

## Device acceptance

Unicode text input is accepted only on the **recorded** device combination (see
[docs/device-acceptance.md](docs/device-acceptance.md)). A new device type, Android
version, or emulator build must re-run the Unicode acceptance matrix before relying
on OpenATX there.
