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


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)


def _tool_call(action: dict) -> str:
    return "<tool_call>" + json.dumps({"name": "mobile_use", "arguments": action}) + "</tool_call>"


class ScriptedAdb:
    """Fake ADB whose ui_dump / foreground returns are scripted (hold-last)."""

    def __init__(self, *, dumps=None, foregrounds=None, dump_counter: bool = False) -> None:
        self.serial = "emulator-fake"
        self.binary = "/usr/bin/adb"
        self.screenshot_calls = 0
        self.ui_dump_calls = 0
        self.tap_calls = 0
        self.swipe_calls = 0
        self._dumps = list(dumps) if dumps is not None else None
        self._fg = list(foregrounds) if foregrounds is not None else None
        self._dump_counter = dump_counter
        self._n = 0

    def run(self, *args, capture: bool = True):
        return SimpleNamespace(stdout=b"device")

    def screenshot(self, path: Path) -> tuple[int, int]:
        self.screenshot_calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png_header(1000, 1000))
        return 1000, 1000

    def ui_dump(self) -> bytes:
        self.ui_dump_calls += 1
        if self._dump_counter:
            self._n += 1
            return f"<x{self._n}/>".encode()
        if self._dumps is not None:
            value = self._dumps[0] if len(self._dumps) == 1 else self._dumps.pop(0)
            return value.encode()
        return b"<p/>"

    def foreground_package(self) -> str:
        if self._fg is not None:
            return self._fg[0] if len(self._fg) == 1 else self._fg.pop(0)
        return "com.test"

    def tap(self, x: int, y: int) -> None:
        self.tap_calls += 1

    def swipe(self, x1, y1, x2, y2, duration_ms: int = 700) -> None:
        self.swipe_calls += 1

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

    def system_prompt(self, width: int, height: int) -> str:
        return "sys"

    def instruction(self, history) -> str:
        return "inst"

    def guard(self, xml: str, foreground: str | None) -> str | None:
        return None


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
    adb = ScriptedAdb(dump_counter=True)  # tree never settles
    client = FakeClient([])  # must never be consulted
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "observation_unstable"
    assert res.vlm_calls == 0
    assert adb.tap_calls == 0
    assert all(r.get("note") == "observation_unstable" for r in _records(tmp_path))


def test_bounded_recapture_reaches_stable_then_proceeds(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # attempt 1 straddles a transition (m1 != m2); attempt 2 is settled (p == p).
    adb = ScriptedAdb(dumps=["<m1/>", "<m2/>", "<p/>", "<p/>"])
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
    # step1 sandwich stable on <p/>; recheck sees <q/> (changed); step2 stable <q/>.
    adb = ScriptedAdb(dumps=["<p/>", "<p/>", "<q/>", "<q/>", "<q/>"])
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=5, settle_s=0, capture_settle_s=0)

    assert res.result == "terminate_success"
    assert adb.tap_calls == 0  # stale click never executed
    assert res.actions_executed == 0
    assert res.reobserves == 1
    first = _records(tmp_path)[0]
    assert first["note"] == "reobserve_required"
    assert first["executed"] is False
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
        "xml_before_started_at",
        "xml_before_finished_at",
        "screenshot_started_at",
        "screenshot_finished_at",
        "xml_after_started_at",
        "xml_after_finished_at",
        "foreground_after_at",
        "foreground_before",
        "foreground_after",
        "xml_fingerprint_before",
        "xml_fingerprint_after",
        "skew_ms",
    ):
        assert key in obs
    assert obs["xml"].endswith(".xml")  # reference, not raw tree


# G ----------------------------------------------------------------------------
def test_slow_capture_over_skew_bound_is_unstable(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    ticks = iter(range(0, 10_000_000, 1000))  # every clock read advances 1000 ms
    monkeypatch.setattr(observation, "_now_ms", lambda: next(ticks))
    adb = ScriptedAdb()  # fingerprints identical; only the span is abnormal
    client = FakeClient([])
    res = runner.run(
        adb, client, FakePack(), tmp_path,
        max_steps=5, settle_s=0, capture_settle_s=0, max_skew_ms=100,
    )

    assert res.result == "observation_unstable"
    assert res.vlm_calls == 0
    assert adb.tap_calls == 0


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


# M: foreground switches *during* the freshness ui_dump -----------------------
class ForegroundFlipAdb(ScriptedAdb):
    """ScriptedAdb that switches foreground while a specific ui_dump is running,
    while the XML stays byte-identical (the race codex's injection reproduces)."""

    def __init__(self, *, flip_on_dump: int) -> None:
        super().__init__()
        self._flip_on_dump = flip_on_dump
        self._dump_seen = 0

    def ui_dump(self) -> bytes:
        self._dump_seen += 1
        if self._dump_seen == self._flip_on_dump:
            self._fg = ["com.other"]  # app switches mid-dump; XML unchanged
        return super().ui_dump()


def test_foreground_flip_during_freshness_dump_zero_action(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    # proposal sandwich does dumps #1-#2; the freshness re-read is dump #3.
    adb = ForegroundFlipAdb(flip_on_dump=3)
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


