"""Unit tests for the thin runner: fake Pack + fake Adb/Client, call-counted.

Each assertion hardcodes the expected value first, then checks:
  - one observation drives exactly one executed action,
  - the loop re-observes after every execution,
  - the repeat / oscillation / wait guards each block,
  - a structured RunResult is returned.
No real device, no real VLM, no network.
"""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

from mano import adb_executor, runner
from mano.pack import ALLOW, ResolveDecision


def _png_header(width: int, height: int) -> bytes:
    """A 24-byte buffer png_size() accepts (sig + IHDR length/type + W/H)."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)


def _tool_call(action: dict) -> str:
    return "<tool_call>" + json.dumps({"name": "mobile_use", "arguments": action}) + "</tool_call>"


class FakeAdb:
    def __init__(self) -> None:
        self.serial = "emulator-fake"
        self.binary = "/usr/bin/adb"
        self.screenshot_calls = 0
        self.ui_dump_calls = 0
        self.tap_calls = 0
        self.swipe_calls = 0

    def run(self, *args, capture: bool = True):
        return SimpleNamespace(stdout=b"device")

    def screenshot(self, path: Path) -> tuple[int, int]:
        self.screenshot_calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png_header(1000, 1000))
        return 1000, 1000

    def ui_dump(self) -> bytes:
        self.ui_dump_calls += 1
        return b"<hierarchy/>"

    def foreground_package(self) -> str:
        return "com.test"

    def tap(self, x: int, y: int) -> None:
        self.tap_calls += 1

    def swipe(self, x1, y1, x2, y2, duration_ms: int = 700) -> None:
        self.swipe_calls += 1


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
        self.prompt_tokens += 10
        self.completion_tokens += 5
        return {"_idx": idx}

    def content(self, response) -> str:
        return self._scripts[response["_idx"]]


class FakePack:
    package = "com.test"

    def __init__(self, guard_result: str | None = None) -> None:
        self._guard_result = guard_result

    def system_prompt(self, width: int, height: int) -> str:
        return "sys"

    def instruction(self, history) -> str:
        return "inst"

    def guard(self, xml: str, foreground: str | None) -> str | None:
        return self._guard_result

    def resolve(self, *, proposed_action, observation, model_text) -> ResolveDecision:
        # Passthrough: the thin runner unit tests exercise the generic loop, not
        # app semantics; the resolved action equals the model action. The semantic
        # target embeds the proposed coordinate so two different taps stay distinct
        # even though the semantic signature itself drops coordinates.
        target = "target:" + str(proposed_action.get("action"))
        coord = proposed_action.get("coordinate")
        if isinstance(coord, list) and len(coord) == 2:
            target += f":{coord[0]},{coord[1]}"
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target=target,
            resolved_action=dict(proposed_action),
        )


def test_one_action_one_execute_and_reobserve(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "click", "coordinate": [200, 200]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=10, settle_s=0)

    assert res.result == "terminate_success"
    assert res.total_steps == 3
    assert res.actions_executed == 2
    assert res.vlm_calls == 3
    assert res.prompt_tokens == 30
    assert res.completion_tokens == 15
    # one bounded observation per step (single ui_dump) + a revalidation
    # re-read (1 ui_dump) before each device action; exactly one tap per step
    assert adb.screenshot_calls == 3
    assert adb.ui_dump_calls == 5
    assert adb.tap_calls == 2


def test_repeat_guard_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    same = _tool_call({"action": "click", "coordinate": [300, 300]})
    client = FakeClient([same, same, same])
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=10, settle_s=0)

    assert res.result == "repeat_click_blocked"
    assert res.total_steps == 3
    assert res.actions_executed == 2
    assert adb.tap_calls == 2


def test_oscillation_guard_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    a = _tool_call({"action": "click", "coordinate": [100, 100]})
    b = _tool_call({"action": "click", "coordinate": [500, 500]})
    client = FakeClient([a, b, a, b])
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=10, settle_s=0)

    assert res.result == "oscillation_blocked"
    assert res.total_steps == 4
    assert res.actions_executed == 3
    assert adb.tap_calls == 3


def test_wait_guard_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(adb_executor.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    wait = _tool_call({"action": "wait", "time": 0.2})
    client = FakeClient([wait, wait, wait, wait])
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=10, settle_s=0)

    assert res.result == "wait_stuck"
    assert res.total_steps == 4
    # wait is non-device-modifying: executed but never counted in actions_executed
    assert res.actions_executed == 0
    assert adb.tap_calls == 0


def _run_with_exploding_tap(exc: Exception, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)

    class ExplodingAdb(FakeAdb):
        def tap(self, x: int, y: int) -> None:
            self.tap_calls += 1
            raise exc

    adb = ExplodingAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            # a second identical click would be reached only if the first polluted
            # history / counted as executed; the run must break before this.
            _tool_call({"action": "click", "coordinate": [100, 100]}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=10, settle_s=0)
    records = [
        json.loads(line)
        for line in (tmp_path / "steps.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return res, adb, records


def test_adb_called_process_error_refuses_without_crashing(tmp_path, monkeypatch):
    res, adb, records = _run_with_exploding_tap(
        subprocess.CalledProcessError(1, ["adb", "shell", "input", "tap"]),
        tmp_path,
        monkeypatch,
    )
    assert res.result == "exec_refused"
    assert res.actions_executed == 0  # failed action is never counted
    assert res.total_steps == 1  # broke on the first step; history not polluted
    assert adb.tap_calls == 1
    step_records = [r for r in records if r.get("step") == 1]
    assert step_records, "journal must retain the failed step"
    failed = step_records[-1]
    assert failed["executed"] is False
    assert failed["exec_error"]


def test_adb_oserror_refuses_without_crashing(tmp_path, monkeypatch):
    res, adb, records = _run_with_exploding_tap(
        OSError("adb: device offline"), tmp_path, monkeypatch
    )
    assert res.result == "exec_refused"
    assert res.actions_executed == 0
    assert res.total_steps == 1
    assert adb.tap_calls == 1
    failed = [r for r in records if r.get("step") == 1][-1]
    assert failed["executed"] is False
    assert failed["exec_error"]


def test_pack_guard_stops_before_vlm(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = FakeClient([])
    res = runner.run(adb, client, FakePack(guard_result="left_app"), tmp_path, settle_s=0)

    assert res.result == "left_app"
    assert res.total_steps == 1
    assert res.actions_executed == 0
    assert res.vlm_calls == 0
    assert adb.tap_calls == 0


def test_structured_result_and_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = FakeClient([_tool_call({"action": "terminate", "status": "failure"})])
    res = runner.run(adb, client, FakePack(), tmp_path, settle_s=0)

    assert isinstance(res, runner.RunResult)
    assert res.result == "terminate_failure"
    assert res.run_dir == str(tmp_path)
    lines = (tmp_path / "steps.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["proposed_action"]["action"] == "terminate"
