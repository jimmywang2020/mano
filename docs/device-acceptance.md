# Device Acceptance Record

Tracked, shareable record of real-device acceptance runs. (The local
`HANDOFF_NEXT_SESSION.md` is gitignored and device-local; it is **not** a
cross-device deliverable. Anything that must survive a clone/pull lives here.)

Status vocabulary: `PLANNED` -> `IMPLEMENTED` -> `UNIT_TESTED` ->
`DEVICE_UNVERIFIED` -> `DEVICE_ACCEPTED`. A capability is only
`DEVICE_ACCEPTED` after a human runs the stated real-device matrix.

---

## OpenAtxAdbKeyboardBackend (Unicode text input) — DEVICE_ACCEPTED

- **Commit:** `97f9a4d0731b0fc36f0eae5641dacf27e8d4fa91` (branch `feat/thin-runner`).
- **Unit tests:** 111 passed (`uv run --extra dev pytest -q`).
- **Backend:** `OpenAtxAdbKeyboardBackend` — IME `com.github.uiautomator/.AdbKeyboard`,
  broadcast action `ADB_KEYBOARD_SET_TEXT`, success judged by broadcast receipt
  `result=-1` (not by a settings/process readiness proxy).
- **Real-device evidence (human-run, source《mano调用反馈_1016_1704》):**
  - MuMu cold start: **10/10** success.
  - Hot start + exact UI verification: **60/60** success.
  - Query `iPad Pro (2022) 11.0英寸` injected in full.
  - Original Sogou IME restored after every run.
  - Fail-closed preflight when the component is not installed.
  - No silent install / silent IME switch / silent downgrade.
- **Run metadata (to fill from the human's records — never fabricated):**
  - Acceptance date: `<待人类回填>`
  - MuMu version / Android version: `<待人类回填>`
  - OpenATX AdbKeyboard APK version or digest: `<待人类回填>`
  - Run IDs / command transcript ref: `<待人类回填>`
  - The source feedback《mano调用反馈_1016_1704》is **not** committed in this
    repo; this tracked file is the in-repo summary of record.
- **Scope note:** This certifies the Mano backend. APK install and backend
  selection remain Consumer/deployment concerns. The consumer default sampling
  backend stays `v01`; Mano runs behind a feature switch.

### Why the previous readiness signal was rejected

Commit `93c23ba` used `settings get secure default_input_method == IME_ID` as a
readiness proxy. Real-device MuMu proved it insufficient: `default_input_method`,
`dumpsys mCurMethodId`, and the IME PID all appear ~71 ms before the broadcast
receiver is ready, so the first broadcast still dropped (backend returned
success in 277 ms, UI XML empty). The broadcast-receipt design replaces that
proxy. Source:《mano调用反馈_1016.1326》.

### Deprecated / removed

- Legacy `AdbKeyboardTextBackend` (F-Droid `com.android.adbkeyboard/.AdbIME`)
  exhibited "broadcast returns success but text never enters the field" on cold
  start. It is scheduled for removal (direct delete — no git tag, still `0.1.0`,
  never merged to `main`, no in-repo production caller). See the action-journal
  rollout plan for sequencing.
