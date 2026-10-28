# Changelog

All notable, **verifiable** changes to `mano`, by release tag. Each entry is
grounded in a Git tag, commit, test count, or recorded acceptance; anything not
confirmable from the repository is marked **not recorded** rather than guessed.

`mano` is **0.x**: interfaces may change between minor versions. Pin to an
immovable tag. Tags are never moved or re-pointed.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

---

## v0.1.2 — 2026-10-28 — packaging metadata only

- **Packaging fix (no runtime change).** Bumped the package version in
  `pyproject.toml` and `mano/__init__.py` from `0.1.0` to `0.1.2` so the built
  wheel is named `mano-0.1.2-py3-none-any.whl` and
  `importlib.metadata.version("mano") == "0.1.2"`. Runtime code is **identical to
  v0.1.1**.
- **Why 0.1.2 and not 0.1.1 as a version:** the `v0.1.1` tag already exists with
  its `pyproject` version still reading `0.1.0`; bumping straight to `0.1.2` fixes
  the wheel metadata without moving the immovable `v0.1.1` tag.
- Commit `02a19738f2847266b3e87c3fe9a24c1da0207667`, tag `v0.1.2`. Full test suite
  green (metadata-only).
- **Migration:** metadata-only → reinstall the dependency and run your full test
  suite; **no device re-acceptance required**.

## v0.1.1 — 2026-10-28 — out-of-range coordinate → bounded re-plan

- **Fix (behavior change).** When `resolve` returns `ALLOW` but the resolved
  coordinate is outside the Core's `0..1000` space (`bad_coordinate` /
  `bad_coordinate2`), the Core now routes that into the existing **bounded,
  zero-side-effect re-plan** (carrying a `coordinate_space` hint to the next model
  call) instead of ending the run as `resolve_invalid_action`. All other validation
  codes (e.g. `text_unsupported_by_backend`) stay **fail-closed**. The Core still
  never clamps, scales, guesses, or executes an illegal coordinate.
- Zero added device/journal side effects: the re-plan branch runs before any
  journal prepare / approve / verify / ADB.
- Commit `9acb4b57947aca1da4c6049029b687aabf8e8449`, tag `v0.1.1`. 221 tests pass.
- **Note:** the wheel built at this tag is still labeled `mano-0.1.0` (the
  `pyproject` version was not bumped); corrected in v0.1.2.
- **Production acceptance (recorded):** Consumer non-editable Git install pinned to
  `v0.1.1`, `direct_url.json` `commit_id=9acb4b5`; real-device re-run of the former
  out-of-range-coordinate task completed via bounded auto-correction. See the
  Round-4 record in [docs/review-response-1020.md](docs/review-response-1020.md).

## v0.1.0 — 2026-10-23 — first tagged release

- First immovable release tag. Commit
  `47aaa09841ba04e99f40fd25ee814c485016cb31`, tag `v0.1.0`.
- Contents (implemented and unit-tested; see
  [docs/review-response-1020.md](docs/review-response-1020.md) for the per-defect
  test mapping):
  - the thin runner (`mano.runner.run`) with one-action-per-step and re-observe;
  - the `Pack` protocol (`resolve` / `verify` / optional `reconcile_action` /
    optional `on_action_executed`);
  - the durable per-action journal + crash recovery (`mano.action_journal`,
    opt-in via `journal=`), with the "never auto-replay the blurry window"
    invariant;
  - pluggable text backends (`AsciiAdbTextBackend` default; `OpenAtxAdbKeyboardBackend`
    Unicode, `DEVICE_ACCEPTED` on the recorded MuMu combination — see
    [docs/device-acceptance.md](docs/device-acceptance.md));
  - bounded, one-shot re-plan feedback after a zero-side-effect `REOBSERVE`.
- **Known packaging issue:** the wheel is labeled `mano-0.1.0` (correct here, but
  the same stale version also shipped at v0.1.1; fixed in v0.1.2).

## Before v0.1.0

Development history before the first tag is **not recorded** here as formal
releases. The pre-tag review rounds folded into v0.1.0 are documented in
[docs/review-response-1020.md](docs/review-response-1020.md) (Round 1 `2833eb9`,
Round 2 `2789339`, Round 3 `47aaa09`).
