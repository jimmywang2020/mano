"""Deterministic tests for bounded observation + execution freshness + approve.

The observation race is non-deterministic on a real device ("it didn't happen
this time" proves nothing), so these fake-ADB tests script the exact skews and
page transitions the Core must survive:

  A. a page still in motion (ui_dump keeps changing) -> observation_unstable,
     the model is never called and no device action runs;
  A2. bounded re-capture reaches a stable view and then proceeds;
  B. the XML changes between proposal and execution -> stale action discarded,
     zero taps;
  C. the foreground changes between proposal and execution -> fail closed;
  D. a stable page -> the proposed action executes exactly once;
  E. wait is non-device-modifying -> approve not called, not counted;
  F. every phase is journaled with observation ids + capture timestamps;
  G. an abnormally slow capture (skew over the bound) -> unstable;
  H/I. approve veto / approve raising -> fail closed, zero taps.

No real device, no real VLM, no network.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace

from mano import adb_executor, observation, runner
from mano.pack import ALLOW, BLOCK, REOBSERVE, ResolveDecision, VerificationResult

def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)


def _tool_call(action: dict) -> str:
    return "<tool_call>" + json.dumps({"name": "mobile_use", "arguments": action}) + "</tool_call>"


class ScriptedAdb:
    """Fake ADB with phase-aware scripting (capture vs revalidation).

    ``dumps`` / ``foregrounds`` are consumed per call and hold their last value.
    ``dump_counter`` / ``fg_counter`` make the XML / foreground change on *every*
    read. ``flip_fg_during_capture`` / ``flip_fg_during_revalidation`` switch the
    foreground to the given package while the ui_dump of that phase runs (the phase
    is set by the ``_track_phase`` helper), reproducing a mid-dump app switch
    without relying on fragile call counts. ``tap`` / ``swipe`` record the last
    coordinate so tests can assert *where* an action landed.
    """

    def __init__(
        self,
        *,
        dumps=None,
        foregrounds=None,
        dump_counter: bool = False,
        fg_counter: bool = False,
        flip_fg_during_capture=None,
        flip_fg_during_revalidation=None,
    ) -> None:
        self.serial = "emulator-fake"
        self.binary = "/usr/bin/adb"
        self.screenshot_calls = 0
        self.ui_dump_calls = 0
        self.tap_calls = 0
        self.swipe_calls = 0
        self.last_tap = None
        self.last_swipe = None
        self.phase = "capture"
        self._dumps = list(dumps) if dumps is not None else None
        self._fg = list(foregrounds) if foregrounds is not None else None
        self._dump_counter = dump_counter
        self._fg_counter = fg_counter
        self._flip_cap = flip_fg_during_capture
        self._flip_reval = flip_fg_during_revalidation
        self._n = 0
        self._nfg = 0

    def run(self, *args, capture: bool = True):
        return SimpleNamespace(stdout=b"device")

    def screenshot(self, path: Path) -> tuple[int, int]:
        self.screenshot_calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png_header(1000, 1000))
        return 1000, 1000

    def ui_dump(self) -> bytes:
        self.ui_dump_calls += 1
        if self.phase == "capture" and self._flip_cap is not None:
            self._fg = [self._flip_cap]
        if self.phase == "revalidation" and self._flip_reval is not None:
            self._fg = [self._flip_reval]
        if self._dump_counter:
            self._n += 1
            return f"<x{self._n}/>".encode()
        if self._dumps is not None:
            value = self._dumps[0] if len(self._dumps) == 1 else self._dumps.pop(0)
            return value.encode()
        return b"<p/>"

    def foreground_package(self) -> str:
        if self._fg_counter:
            self._nfg += 1
            return f"com.app{self._nfg}"
        if self._fg is not None:
            return self._fg[0] if len(self._fg) == 1 else self._fg.pop(0)
        return "com.test"

    def tap(self, x: int, y: int) -> None:
        self.tap_calls += 1
        self.last_tap = (x, y)

    def swipe(self, x1, y1, x2, y2, duration_ms: int = 700) -> None:
        self.swipe_calls += 1
        self.last_swipe = (x1, y1, x2, y2)

    def type_text(self, *a, **k) -> None:
        pass

    def keyevent(self, *a, **k) -> None:
        pass



class FakeClient:
    def __init__(self, scripts: list[str]) -> None:
        self._scripts = list(scripts)
        self.call_count = 0
        self.retries = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, messages, max_tokens: int = 700):
        idx = self.call_count
        self.call_count += 1
        return {"_idx": idx}

    def content(self, response) -> str:
        return self._scripts[response["_idx"]]


class FakePack:
    package = "com.test"

    def __init__(self, *, guard_result=None, resolve_fn=None, verify_fn=None) -> None:
        self._guard_result = guard_result
        self._resolve_fn = resolve_fn
        # verify is an OPTIONAL capability: only expose it when a hook is given,
        # so a plain FakePack() runs the legacy raw-XML fallback.
        if verify_fn is not None:
            self._verify_fn = verify_fn
            self.verify = self._verify

    def system_prompt(self, width: int, height: int) -> str:
        return "sys"

    def instruction(self, history) -> str:
        return "inst"

    def guard(self, xml: str, foreground: str | None) -> str | None:
        return self._guard_result

    def resolve(self, *, proposed_action, observation, model_text) -> ResolveDecision:
        if self._resolve_fn is not None:
            return self._resolve_fn(proposed_action, observation, model_text)
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target="target:" + str(proposed_action.get("action")),
            resolved_action=dict(proposed_action),
        )

    def _verify(self, *, approved_intent, fresh_observation) -> VerificationResult:
        return self._verify_fn(approved_intent, fresh_observation)


def _track_phase(monkeypatch) -> None:
    """Mark the adb phase 'revalidation' around capture_revalidation so a
    ScriptedAdb can flip its foreground during *that* ui_dump, not by call count."""
    orig = runner.capture_revalidation

    def traced(adb, proposal, step):
        prev = getattr(adb, "phase", "capture")
        adb.phase = "revalidation"
        try:
            return orig(adb, proposal, step)
        finally:
            adb.phase = prev

    monkeypatch.setattr(runner, "capture_revalidation", traced)



def _silence_sleep(monkeypatch) -> None:
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(observation.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(adb_executor.time, "sleep", lambda *_a, **_k: None)


def _records(tmp_path: Path) -> list[dict]:
    text = (tmp_path / "steps.jsonl").read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines()] if text else []


# A ----------------------------------------------------------------------------
def test_unstable_page_blocks_model_and_execute(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb(fg_counter=True)  # foreground changes mid-capture every attempt
    client = FakeClient([])  # must never be consulted
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "observation_unstable"
    assert res.vlm_calls == 0
    assert adb.tap_calls == 0
    assert all(r.get("note") == "observation_unstable" for r in _records(tmp_path))


def test_bounded_recapture_reaches_stable_then_proceeds(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # attempt 1 straddles a foreground switch (before=A, after=B); attempt 2 settles.
    adb = ScriptedAdb(foregrounds=["comA", "comB", "comB"])
    client = FakeClient([_tool_call({"action": "terminate", "status": "success"})])
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 0
    obs = _records(tmp_path)[0]["observation"]
    assert obs["stability"] == "stable"
    assert obs["capture_attempt"] == 2


# B ----------------------------------------------------------------------------
def test_xml_change_before_execute_discards_action(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # step1 capture <p/>; revalidation sees <q/> (changed); step2 capture <q/>.
    adb = ScriptedAdb(dumps=["<p/>", "<q/>", "<q/>"])
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 0  # stale click never executed (legacy raw-XML fallback)
    assert res.actions_executed == 0
    assert res.reobserves == 1
    first = _records(tmp_path)[0]
    assert first["note"] == "reobserve_required"
    assert first["executed"] is False
    assert first["verification_mode"] == "legacy_raw_xml_fallback"
    check = first["execution_check"]
    assert check["check_id"]
    assert check["xml_changed"] is True
    assert check["changed"] is True


# C ----------------------------------------------------------------------------
def test_foreground_change_before_execute_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # com.test during step1 sandwich, then recheck + step2 see com.other.
    adb = ScriptedAdb(foregrounds=["com.test", "com.test", "com.other", "com.other", "com.other"])
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 0
    assert res.reobserves == 1
    assert _records(tmp_path)[0]["note"] == "reobserve_required"


# D ----------------------------------------------------------------------------
def test_stable_page_executes_once(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()  # constant <p/>, com.test
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 1
    assert res.actions_executed == 1
    assert res.reobserves == 0


# E ----------------------------------------------------------------------------
def test_wait_skips_approve_and_is_not_counted(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()
    client = FakeClient(
        [
            _tool_call({"action": "wait", "time": 0.1}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    approved: list = []
    res = runner.run(
        adb, client, FakePack(), tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0,
        approve=lambda action, obs: approved.append(action) or True,
    )

    assert res.result == "terminate_success"
    assert approved == []  # wait must not reach the veto hook
    assert res.actions_executed == 0  # wait executed but not counted
    assert _records(tmp_path)[0]["executed"] is True


# F ----------------------------------------------------------------------------
def test_journal_records_observation_ids_and_phases(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    rec = _records(tmp_path)[0]
    assert rec["proposal_observation_id"]
    assert rec["execution_check"]["check_id"]
    obs = rec["observation"]
    for key in (
        "capture_started_at",
        "capture_finished_at",
        "foreground_before_at",
        "screenshot_started_at",
        "screenshot_finished_at",
        "xml_started_at",
        "xml_finished_at",
        "foreground_after_at",
        "foreground_before",
        "foreground_after",
        "xml_fingerprint",
        "skew_ms",
    ):
        assert key in obs
    # single-dump capture no longer fabricates before/after fingerprints
    assert "xml_fingerprint_before" not in obs
    assert "xml_fingerprint_after" not in obs
    assert obs["xml"].endswith(".xml")  # reference, not raw tree


# G ----------------------------------------------------------------------------
def test_slow_capture_over_skew_bound_is_unstable(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    ticks = iter(range(0, 10_000_000, 1000))  # every monotonic read advances 1000 ms
    monkeypatch.setattr(observation, "_mono_ms", lambda: next(ticks))
    adb = ScriptedAdb()  # fingerprints identical; only the span is abnormal
    client = FakeClient([])
    res = runner.run(
        adb, client, FakePack(), tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0, max_skew_ms=100,
    )

    assert res.result == "observation_unstable"
    assert res.vlm_calls == 0
    assert adb.tap_calls == 0


# G2: skew uses the MONOTONIC clock, so a wall-clock rollback can't forge it ----
def test_skew_is_monotonic_not_wallclock(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # Wall clock jumps far backwards mid-capture (NTP step / DST); the monotonic
    # clock keeps advancing a tiny, bounded amount. Skew must stay small & >= 0,
    # so the capture is still "stable" (not forged negative/huge by wall time).
    wall = iter([1_000_000, 500_000, 500_001, 500_002, 500_003, 500_004, 500_005, 500_006])
    mono = iter(range(0, 1_000_000, 5))  # +5 ms per read, strictly increasing
    monkeypatch.setattr(observation, "_now_ms", lambda: next(wall))
    monkeypatch.setattr(observation, "_mono_ms", lambda: next(mono))
    adb = ScriptedAdb()
    client = FakeClient(
        [
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(
        adb, client, FakePack(), tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0, max_skew_ms=100,
    )

    assert res.result == "terminate_success"
    obs = _records(tmp_path)[0]["observation"]
    assert obs["stability"] == "stable"
    assert 0 <= obs["skew_ms"] <= 100  # bounded, non-negative despite the rollback


# H ----------------------------------------------------------------------------
def test_approve_veto_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    seen: list = []

    def veto(action, obs):
        seen.append(obs)
        return False

    res = runner.run(
        adb, client, FakePack(), tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0, approve=veto,
    )

    assert res.result == "approve_rejected"
    assert adb.tap_calls == 0
    rec = _records(tmp_path)[0]
    assert rec["approve_result"] is False
    # approve runs on the stable proposal observation, before the freshness check
    assert seen[0].observation_id == rec["proposal_observation_id"]
    assert rec["execution_check"] is None  # vetoed before any freshness re-read


# I ----------------------------------------------------------------------------
def test_approve_exception_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])

    def boom(action, obs):
        raise RuntimeError("hook blew up")

    res = runner.run(
        adb, client, FakePack(), tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0, approve=boom,
    )

    assert res.result == "approve_rejected"
    assert adb.tap_calls == 0


# J: drift during human confirmation ------------------------------------------
def test_approve_changes_xml_then_zero_action(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()  # stable <p/> for the proposal sandwich
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )

    def approve_then_page_moves(action, obs):
        adb._dumps = ["<q/>"]  # UI changes during confirmation
        return True

    res = runner.run(
        adb, client, FakePack(), tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0, approve=approve_then_page_moves,
    )

    assert res.result == "terminate_success"
    assert adb.tap_calls == 0  # approved action discarded, page moved
    assert res.reobserves == 1
    first = _records(tmp_path)[0]
    assert first["approve_result"] is True
    assert first["note"] == "reobserve_required"
    assert first["execution_check"]["xml_changed"] is True


def test_approve_changes_foreground_then_zero_action(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()  # com.test for the proposal sandwich
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )

    def approve_then_foreground_moves(action, obs):
        adb._fg = ["com.other"]  # foreground changes during confirmation
        return True

    res = runner.run(
        adb, client, FakePack(), tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0, approve=approve_then_foreground_moves,
    )

    assert res.result == "terminate_success"
    assert adb.tap_calls == 0
    assert res.reobserves == 1
    first = _records(tmp_path)[0]
    assert first["approve_result"] is True
    assert first["execution_check"]["foreground_changed"] is True


# K: FreshnessCheck must not fabricate a screenshot time -----------------------
def test_freshness_check_has_no_screenshot_time(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    check = _records(tmp_path)[0]["execution_check"]
    assert "started_at" in check and "finished_at" in check
    assert not any("screenshot" in key for key in check)


# L: proposal and final freshness check are linkable in the journal -----------
def test_journal_links_proposal_and_freshness(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    rec = _records(tmp_path)[0]
    check = rec["execution_check"]
    # the final check records the proposal's foreground + fingerprint, so a reader
    # can tie the executed action back to the exact observation it was proposed on
    assert rec["observation"]["foreground"] == check["proposal_foreground"]
    assert rec["observation"]["xml_fingerprint"] == check["proposal_fingerprint"]
    assert rec["proposal_observation_id"] != check["check_id"]


# M: foreground switches *during* the revalidation ui_dump --------------------
def test_foreground_flip_during_freshness_dump_zero_action(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    _track_phase(monkeypatch)
    # foreground is com.test until the revalidation ui_dump runs, then flips.
    adb = ScriptedAdb(flip_fg_during_revalidation="com.other")
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert adb.tap_calls == 0  # never tap into the switched foreground
    assert res.reobserves == 1
    first = _records(tmp_path)[0]
    assert first["note"] == "reobserve_required"
    check = first["execution_check"]
    assert check["foreground_changed"] is True
    assert check["xml_changed"] is False  # XML looked identical; foreground caught it
    assert check["foreground_before"] == "com.test"
    assert check["foreground_after"] == "com.other"


# ---------------------------------------------------------------------------
# Semantic resolve/verify closed loop (codex acceptance tests 1-11).
# ---------------------------------------------------------------------------


def _resolve_click(target, x, y):
    """A resolve_fn that pins a stable semantic target and resolves to a click;
    terminate/wait pass through so the run can still end."""

    def _fn(proposed_action, observation, model_text):
        if proposed_action.get("action") in ("terminate", "wait"):
            return ResolveDecision(
                verdict=ALLOW,
                semantic_target="passthrough:" + str(proposed_action.get("action")),
                resolved_action=dict(proposed_action),
            )
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target=target,
            resolved_action={"action": "click", "coordinate": [x, y]},
        )

    return _fn


# 1: unrelated dynamic text changed but the target control still exists --------
def test_verify_allows_with_latest_coordinates(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # raw XML changes every read (carousel), foreground stable -> capture stable.
    adb = ScriptedAdb(dump_counter=True)

    def verify(approved_intent, fresh):
        # target still present; re-resolve to the latest coordinate
        return VerificationResult(verdict=ALLOW, resolved_action={"action": "click", "coordinate": [250, 250]})

    pack = FakePack(resolve_fn=_resolve_click("home.search_field", 100, 100), verify_fn=verify)
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 1
    assert adb.last_tap == (250, 250)  # executed the verify-resolved coordinate
    first = _records(tmp_path)[0]
    assert first["verification_mode"] == "pack_verify"
    assert first["verify_verdict"] == "ALLOW"
    assert first["final_resolved_action"]["coordinate"] == [250, 250]


# 2: the target control disappeared -> REOBSERVE, zero ADB --------------------
def test_verify_reobserve_when_target_gone(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def verify(approved_intent, fresh):
        return VerificationResult(verdict=REOBSERVE, reason="target_gone")

    pack = FakePack(resolve_fn=_resolve_click("home.search_field", 100, 100), verify_fn=verify)
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 0
    assert res.reobserves == 1
    first = _records(tmp_path)[0]
    assert first["verify_verdict"] == "REOBSERVE"
    assert first["note"] == "reobserve_required"


# 3 & 4: same target moved to a new coordinate -> tap the NEW coordinate -------
def test_verify_uses_new_coordinate_for_same_target(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def verify(approved_intent, fresh):
        assert approved_intent == "search.submit_button"
        return VerificationResult(verdict=ALLOW, resolved_action={"action": "click", "coordinate": [900, 50]})

    pack = FakePack(resolve_fn=_resolve_click("search.submit_button", 100, 100), verify_fn=verify)
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 1
    assert adb.last_tap == (900, 50)  # new coordinate, not the original (100,100) px
    first = _records(tmp_path)[0]
    assert first["semantic_target"] == "search.submit_button"
    assert first["resolved_action"]["coordinate"] == [100, 100]  # initial
    assert first["final_resolved_action"]["coordinate"] == [900, 50]  # after verify


# 5: foreground switches during verify -> Core blocks, zero ADB ---------------
def test_core_foreground_check_precedes_verify(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    _track_phase(monkeypatch)
    adb = ScriptedAdb(flip_fg_during_revalidation="com.other")
    verify_called = []

    def verify(approved_intent, fresh):
        verify_called.append(True)
        return VerificationResult(verdict=ALLOW, resolved_action={"action": "click", "coordinate": [1, 1]})

    pack = FakePack(resolve_fn=_resolve_click("x", 100, 100), verify_fn=verify)
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert adb.tap_calls == 0
    assert verify_called == []  # Core foreground boundary fires before verify
    assert res.reobserves == 1


# 7: model wait, pack resolves to click -> full approve + verify path ----------
def test_wait_resolved_to_click_goes_through_approve_and_verify(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()
    approvals = []

    def resolve_wait_to_click(proposed_action, observation, model_text):
        if proposed_action.get("action") == "terminate":
            return ResolveDecision(
                verdict=ALLOW, semantic_target="terminate", resolved_action=dict(proposed_action)
            )
        assert proposed_action["action"] == "wait"
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target="home.cta",
            resolved_action={"action": "click", "coordinate": [100, 100]},
        )

    def verify(approved_intent, fresh):
        return VerificationResult(verdict=ALLOW, resolved_action={"action": "click", "coordinate": [100, 100]})

    pack = FakePack(resolve_fn=resolve_wait_to_click, verify_fn=verify)
    client = FakeClient(
        [
            _tool_call({"action": "wait", "time": 0.1}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(
        adb, client, pack, tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0,
        approve=lambda resolved, obs: approvals.append(resolved) or True,
    )

    assert res.result == "terminate_success"
    assert adb.tap_calls == 1  # resolved click executed (no wait bypass)
    assert res.actions_executed == 1
    assert len(approvals) == 1
    assert approvals[0].semantic_target == "home.cta"
    first = _records(tmp_path)[0]
    assert first["proposed_action"]["action"] == "wait"
    assert first["resolved_action"]["action"] == "click"
    assert first["execution_check"] is not None  # revalidation ran


# 8: verify tries to swap the semantic target / side-effect -> Core rejects ----
def test_verify_cannot_swap_action_type(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def verify(approved_intent, fresh):
        # illegal: change click -> type (a non-coordinate side-effect field)
        return VerificationResult(verdict=ALLOW, resolved_action={"action": "type", "text": "pwned"})

    pack = FakePack(resolve_fn=_resolve_click("x", 100, 100), verify_fn=verify)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "verify_semantic_swap_blocked"
    assert adb.tap_calls == 0
    first = _records(tmp_path)[0]
    assert first["note"] == "verify_semantic_swap_blocked"
    assert first["executed"] is False


# 9: constant unrelated XML churn is NOT observation_unstable ------------------
def test_dynamic_xml_churn_is_not_unstable(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb(dump_counter=True)  # XML differs every read, foreground stable

    def verify(approved_intent, fresh):
        return VerificationResult(verdict=ALLOW, resolved_action={"action": "click", "coordinate": [100, 100]})

    pack = FakePack(resolve_fn=_resolve_click("x", 100, 100), verify_fn=verify)
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 1  # Pack verify owns the decision, not raw XML equality
    assert all(r.get("note") != "observation_unstable" for r in _records(tmp_path))
    assert _records(tmp_path)[0]["observation"]["stability"] == "stable"


# 10: no verify -> legacy fallback, mode is journaled -------------------------
def test_legacy_fallback_executes_and_logs_mode(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()  # stable, XML unchanged -> legacy executes
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 1
    first = _records(tmp_path)[0]
    assert first["verification_mode"] == "legacy_raw_xml_fallback"
    assert first["verify_verdict"] is None


# 11: screenshot loading / XML result -> no atomic claim, model may wait -------
def test_model_may_wait_on_non_atomic_capture(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # foreground stable + skew ok -> capture is "stable" even though the PNG and
    # XML are not guaranteed to be the same instant; the model safely waits.
    adb = ScriptedAdb()
    client = FakeClient(
        [
            _tool_call({"action": "wait", "time": 0.1}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 0  # no wrong tap on a non-atomic observation
    assert res.actions_executed == 0  # wait is non-device-modifying
    assert _records(tmp_path)[0]["observation"]["stability"] == "stable"


# ---------------------------------------------------------------------------
# Batch 1 hardening: semantic dedup, REOBSERVE non-pollution, wait resets the
# re-observe streak, and fail-closed validation of resolve / verify output.
# ---------------------------------------------------------------------------


# B2: a fixed semantic target with drifting coordinates is still a repeat ------
def test_semantic_dedup_blocks_despite_coordinate_drift(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()  # stable page, legacy fallback executes
    coords = iter([[100, 100], [101, 102], [103, 104]])

    def resolve_fn(proposed_action, observation, model_text):
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target="home.cta",  # same control every step
            resolved_action={"action": "click", "coordinate": next(coords)},
        )

    pack = FakePack(resolve_fn=resolve_fn)  # no verify -> legacy fallback
    click = _tool_call({"action": "click", "coordinate": [100, 100]})
    client = FakeClient([click, click, click])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "repeat_click_blocked"  # dedup fires though pixels drift
    assert adb.tap_calls == 2
    recs = _records(tmp_path)
    # physical signatures differ (drift) but the semantic signature is identical
    assert recs[0]["physical_signature"] != recs[1]["physical_signature"]
    assert recs[0]["semantic_signature"] == recs[1]["semantic_signature"]


# REOBSERVE must not pollute the device-action history ------------------------
def test_reobserve_does_not_pollute_action_history(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()
    state = {"n": 0}

    def verify(approved_intent, fresh):
        state["n"] += 1
        if state["n"] <= 2:
            return VerificationResult(verdict=REOBSERVE, reason="not_ready")
        return VerificationResult(
            verdict=ALLOW, resolved_action={"action": "click", "coordinate": [100, 100]}
        )

    pack = FakePack(resolve_fn=_resolve_click("home.cta", 100, 100), verify_fn=verify)
    click = _tool_call({"action": "click", "coordinate": [100, 100]})
    client = FakeClient([click, click, click, _tool_call({"action": "terminate", "status": "success"})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=6, settle_s=0, capture_settle_s=0)

    # two REOBSERVE steps added nothing to the loop history, so the single real
    # click is NOT mistaken for a repeat and executes exactly once.
    assert res.result == "terminate_success"
    assert adb.tap_calls == 1
    assert res.reobserves == 2
    recs = _records(tmp_path)
    assert recs[0]["executed"] is False and recs[0]["note"] == "reobserve_required"
    assert recs[1]["executed"] is False and recs[1]["note"] == "reobserve_required"
    assert recs[2]["executed"] is True


# A3: a successful wait clears the re-observe streak --------------------------
def test_wait_resets_reobserve_streak(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def resolve_fn(proposed_action, observation, model_text):
        act = proposed_action.get("action")
        if act in ("terminate", "wait"):
            return ResolveDecision(
                verdict=ALLOW, semantic_target="pt:" + act, resolved_action=dict(proposed_action)
            )
        return ResolveDecision(
            verdict=REOBSERVE, semantic_target="home.cta", resolved_action={}, reason="not_ready"
        )

    pack = FakePack(resolve_fn=resolve_fn)
    click = _tool_call({"action": "click", "coordinate": [100, 100]})
    wait = _tool_call({"action": "wait", "time": 0.1})
    # REOBSERVE, REOBSERVE, wait (resets), REOBSERVE, terminate.
    # Without the reset the third REOBSERVE would hit max_reobserve=3 and stop.
    client = FakeClient([click, click, wait, click, _tool_call({"action": "terminate", "status": "success"})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=8, settle_s=0, capture_settle_s=0, max_reobserve=3)

    assert res.result == "terminate_success"
    assert res.reobserves == 3  # 2 + 1, but never 3 in a row thanks to the wait reset
    assert adb.tap_calls == 0


# A1: resolve returns an out-of-range coordinate -> fail closed, zero ADB ------
def test_resolve_out_of_range_coordinate_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def resolve_fn(proposed_action, observation, model_text):
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target="home.cta",
            resolved_action={"action": "click", "coordinate": [2000, 100]},  # > 1000
        )

    pack = FakePack(resolve_fn=resolve_fn)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "resolve_invalid_action"
    assert adb.tap_calls == 0
    first = _records(tmp_path)[0]
    assert first["note"] == "resolve_invalid_action"
    assert first["resolve_reason"] == "bad_coordinate"


# A1: resolve returns an unknown action -> fail closed ------------------------
def test_resolve_unknown_action_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def resolve_fn(proposed_action, observation, model_text):
        return ResolveDecision(
            verdict=ALLOW, semantic_target="home.cta", resolved_action={"action": "frobnicate"}
        )

    pack = FakePack(resolve_fn=resolve_fn)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "resolve_invalid_action"
    assert adb.tap_calls == 0
    assert _records(tmp_path)[0]["resolve_reason"] == "unknown_action"


# A1: verify returns an out-of-range coordinate -> fail closed ----------------
def test_verify_out_of_range_coordinate_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def verify(approved_intent, fresh):
        return VerificationResult(
            verdict=ALLOW, resolved_action={"action": "click", "coordinate": [5000, 5000]}
        )

    pack = FakePack(resolve_fn=_resolve_click("home.cta", 100, 100), verify_fn=verify)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "verify_invalid_action"
    assert adb.tap_calls == 0
    first = _records(tmp_path)[0]
    assert first["note"] == "verify_invalid_action"
    assert first["verify_reason"] == "bad_coordinate"


# A1: verify returns a non-dict action -> fail closed -------------------------
def test_verify_non_dict_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def verify(approved_intent, fresh):
        return VerificationResult(verdict=ALLOW, resolved_action=["action", "click"])  # not a dict

    pack = FakePack(resolve_fn=_resolve_click("home.cta", 100, 100), verify_fn=verify)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "verify_invalid_action"
    assert adb.tap_calls == 0
    assert _records(tmp_path)[0]["verify_reason"] == "not_a_dict"


# Unrecognized verdicts are Pack bugs and must fail closed (never ALLOW/REOBSERVE)
def test_resolve_unknown_verdict_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def resolve_fn(proposed_action, observation, model_text):
        return ResolveDecision(
            verdict="MAYBE",  # not ALLOW / REOBSERVE / BLOCK
            semantic_target="home.cta",
            resolved_action={"action": "click", "coordinate": [100, 100]},
        )

    pack = FakePack(resolve_fn=resolve_fn)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "invalid_resolve_verdict"
    assert adb.tap_calls == 0
    assert _records(tmp_path)[0]["resolve_reason"] == "invalid_resolve_verdict"


def test_resolve_allow_empty_semantic_target_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def resolve_fn(proposed_action, observation, model_text):
        return ResolveDecision(
            verdict=ALLOW, semantic_target="", resolved_action={"action": "click", "coordinate": [100, 100]}
        )

    pack = FakePack(resolve_fn=resolve_fn)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "invalid_semantic_target"
    assert adb.tap_calls == 0
    assert _records(tmp_path)[0]["resolve_reason"] == "invalid_semantic_target"


def test_resolve_allow_non_str_semantic_target_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def resolve_fn(proposed_action, observation, model_text):
        return ResolveDecision(
            verdict=ALLOW, semantic_target=123, resolved_action={"action": "click", "coordinate": [100, 100]}
        )

    pack = FakePack(resolve_fn=resolve_fn)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "invalid_semantic_target"
    assert adb.tap_calls == 0
    assert _records(tmp_path)[0]["resolve_reason"] == "invalid_semantic_target"


def test_verify_unknown_verdict_fails_closed(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    adb = ScriptedAdb()

    def verify(approved_intent, fresh):
        # a misspelled verdict must NOT be silently read as REOBSERVE
        return VerificationResult(verdict="PERHAPS", resolved_action={"action": "click", "coordinate": [100, 100]})

    pack = FakePack(resolve_fn=_resolve_click("home.cta", 100, 100), verify_fn=verify)
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "invalid_verify_verdict"
    assert adb.tap_calls == 0
    assert _records(tmp_path)[0]["verify_reason"] == "invalid_verify_verdict"


# wall_seconds uses the monotonic clock, so a wall-clock rollback can't forge it
def test_wall_seconds_uses_monotonic_not_wallclock(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # wall clock jumps far backwards mid-run; wall_seconds must stay non-negative
    vals = iter([1_000_000.0])
    monkeypatch.setattr(runner.time, "time", lambda: next(vals, 0.0))
    adb = ScriptedAdb()
    client = FakeClient([_tool_call({"action": "terminate", "status": "success"})])
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert res.wall_seconds >= 0.0  # monotonic duration, unaffected by the wall jump


# ---------------------------------------------------------------------------
# action_codec unit tests: single source of truth for the action whitelist.
# ---------------------------------------------------------------------------
from mano import action_codec


def test_validate_action_accepts_whitelisted_actions():
    assert action_codec.validate_action({"action": "click", "coordinate": [10, 20]}) is None
    assert action_codec.validate_action(
        {"action": "swipe", "coordinate": [10, 20], "coordinate2": [30, 40]}
    ) is None
    assert action_codec.validate_action({"action": "type", "text": "hello world"}) is None
    assert action_codec.validate_action({"action": "key", "text": "BACK"}) is None
    assert action_codec.validate_action({"action": "system_button", "button": "HOME"}) is None
    assert action_codec.validate_action({"action": "wait", "time": 0.5}) is None
    assert action_codec.validate_action({"action": "terminate", "status": "success"}) is None


def test_validate_action_rejects_bad_input():
    assert action_codec.validate_action("nope") == "not_a_dict"
    assert action_codec.validate_action({"action": "frobnicate"}) == "unknown_action"
    assert action_codec.validate_action({"action": "click", "coordinate": [10, 20], "x": 1}) == "unknown_field"
    assert action_codec.validate_action({"action": "click", "coordinate": [2000, 1]}) == "bad_coordinate"
    assert action_codec.validate_action({"action": "click", "coordinate": [1]}) == "bad_coordinate"
    assert action_codec.validate_action({"action": "type", "text": "rm -rf /;"}) == "bad_text"
    assert action_codec.validate_action({"action": "key", "text": "ZOOM"}) == "bad_key"
    assert action_codec.validate_action({"action": "terminate", "status": "maybe"}) == "bad_status"


def test_validate_action_wait_time_must_be_finite():
    # wait.time reaches time.sleep(); NaN raises ValueError there, Inf never wakes,
    # bool is a degenerate int. All must fail closed as bad_time before execution.
    assert action_codec.validate_action({"action": "wait", "time": float("nan")}) == "bad_time"
    assert action_codec.validate_action({"action": "wait", "time": float("inf")}) == "bad_time"
    assert action_codec.validate_action({"action": "wait", "time": float("-inf")}) == "bad_time"
    assert action_codec.validate_action({"action": "wait", "time": True}) == "bad_time"
    assert action_codec.validate_action({"action": "wait", "time": False}) == "bad_time"
    assert action_codec.validate_action({"action": "wait", "time": 1}) is None
    assert action_codec.validate_action({"action": "wait", "time": 0.5}) is None


def test_terminate_meta_field_is_allowed_and_excluded_from_identity():
    action = {"action": "terminate", "status": "success", "status_inferred_from_text": True}
    assert action_codec.validate_action(action) is None
    # the diagnostic meta field never enters the execution payload / identity
    assert action_codec.execution_payload(action) == {"action": "terminate", "status": "success"}


def test_execution_payload_excludes_coordinate():
    payload = action_codec.execution_payload({"action": "click", "coordinate": [10, 20]})
    assert payload == {"action": "click"}  # coordinate is re-resolvable, not frozen
    assert action_codec.execution_payload({"action": "type", "text": "hi"}) == {
        "action": "type",
        "text": "hi",
    }


def test_semantic_signature_ignores_coordinate_drift():
    a = action_codec.semantic_signature("home.cta", {"action": "click", "coordinate": [100, 100]})
    b = action_codec.semantic_signature("home.cta", {"action": "click", "coordinate": [103, 98]})
    assert a == b  # same target + same side-effects -> same identity despite drift
    c = action_codec.semantic_signature("other.cta", {"action": "click", "coordinate": [100, 100]})
    assert a != c
    # non-device actions have no cross-step identity
    assert action_codec.semantic_signature("x", {"action": "wait", "time": 1}) is None
    assert action_codec.semantic_signature("x", {"action": "terminate", "status": "success"}) is None


def test_semantic_signature_distinguishes_side_effect_change():
    click = action_codec.semantic_signature("f", {"action": "click", "coordinate": [10, 10]})
    typed = action_codec.semantic_signature("f", {"action": "type", "text": "hi"})
    assert click != typed  # same target, different side-effect -> different identity



