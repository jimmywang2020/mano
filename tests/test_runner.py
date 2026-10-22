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
from mano.pack import ALLOW, BLOCK, REOBSERVE, ResolveDecision, ResolvedAction, VerificationResult
from mano.text_input import TextInputError


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
        self.type_calls = 0
        self.keyevent_calls = 0

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

    def type_text(self, text: str) -> None:
        self.type_calls += 1

    def keyevent(self, name: str) -> None:
        self.keyevent_calls += 1


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


class HookPack(FakePack):
    """FakePack plus an optional on_action_executed that records every call."""

    def __init__(self, guard_result: str | None = None, *, raises: bool = False) -> None:
        super().__init__(guard_result)
        self.hook_calls: list[ResolvedAction] = []
        self._raises = raises

    def on_action_executed(self, *, executed: ResolvedAction) -> None:
        self.hook_calls.append(executed)
        if self._raises:
            raise RuntimeError("pack state update failed")


class VerifyHookPack(HookPack):
    """resolve tags its own metadata; verify re-resolves the coordinate and
    returns its own metadata. Used to prove the hook gets the post-verify action
    and the verify-stage metadata (even when empty)."""

    def __init__(self, *, verify_coordinate, verify_metadata, resolve_metadata) -> None:
        super().__init__()
        self._verify_coordinate = verify_coordinate
        self._verify_metadata = verify_metadata
        self._resolve_metadata = resolve_metadata

    def resolve(self, *, proposed_action, observation, model_text) -> ResolveDecision:
        d = super().resolve(
            proposed_action=proposed_action, observation=observation, model_text=model_text
        )
        return ResolveDecision(
            verdict=ALLOW,
            semantic_target=d.semantic_target,
            resolved_action=d.resolved_action,
            metadata=self._resolve_metadata,
        )

    def verify(self, *, approved_intent, fresh_observation) -> VerificationResult:
        return VerificationResult(
            verdict=ALLOW,
            resolved_action={"action": "click", "coordinate": self._verify_coordinate},
            metadata=self._verify_metadata,
        )


class VerdictHookPack(HookPack):
    """resolve returns a fixed verdict (to prove the hook never fires on
    BLOCK / REOBSERVE)."""

    def __init__(self, verdict: str) -> None:
        super().__init__()
        self._verdict = verdict

    def resolve(self, *, proposed_action, observation, model_text) -> ResolveDecision:
        return ResolveDecision(
            verdict=self._verdict,
            semantic_target="t",
            resolved_action={"action": "click", "coordinate": [100, 100]},
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


# --- on_action_executed post-execute hook ----------------------------------


def test_pack_without_hook_runs_unchanged(tmp_path, monkeypatch):
    # FakePack has no on_action_executed; its absence must not break the loop.
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    assert not hasattr(FakePack(), "on_action_executed")
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=10, settle_s=0)
    assert res.result == "terminate_success"
    assert res.actions_executed == 1


def test_hook_called_once_per_successful_device_action(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    pack = HookPack()
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "click", "coordinate": [200, 200]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)
    assert res.result == "terminate_success"
    assert res.actions_executed == 2
    # two device clicks -> two calls; terminate is excluded
    assert len(pack.hook_calls) == 2
    assert all(isinstance(c, ResolvedAction) for c in pack.hook_calls)


def test_hook_receives_post_verify_coordinate_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    pack = VerifyHookPack(
        verify_coordinate=[222, 333],
        verify_metadata={"clicked_texts": ["搜索"]},
        resolve_metadata={"clicked_texts": ["stale"]},
    )
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)
    assert res.result == "terminate_success"
    assert len(pack.hook_calls) == 1
    # the hook sees the action actually handed to ADB (verify's coordinate) ...
    assert pack.hook_calls[0].resolved_action["coordinate"] == [222, 333]
    # ... and the verify-stage metadata, not the stale resolve metadata
    assert pack.hook_calls[0].metadata == {"clicked_texts": ["搜索"]}


def test_hook_uses_verify_metadata_even_when_empty(tmp_path, monkeypatch):
    # Empty verify metadata means "no fresh structured evidence"; it must NOT
    # silently fall back to resolve metadata (which may be stale).
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    pack = VerifyHookPack(
        verify_coordinate=[200, 300],
        verify_metadata={},
        resolve_metadata={"clicked_texts": ["stale"]},
    )
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)
    assert len(pack.hook_calls) == 1
    assert pack.hook_calls[0].metadata == {}


def test_hook_not_called_on_adb_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)

    class ExplodingAdb(FakeAdb):
        def tap(self, x: int, y: int) -> None:
            self.tap_calls += 1
            raise OSError("adb offline")

    pack = HookPack()
    adb = ExplodingAdb()
    client = FakeClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)
    assert res.result == "exec_refused"
    assert pack.hook_calls == []


def test_hook_not_called_on_block_reobserve_or_approve_veto(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    click = _tool_call({"action": "click", "coordinate": [100, 100]})

    block = VerdictHookPack(BLOCK)
    runner.run(FakeAdb(), FakeClient([click]), block, tmp_path / "block", max_steps=10, settle_s=0)
    assert block.hook_calls == []

    reobserve = VerdictHookPack(REOBSERVE)
    runner.run(
        FakeAdb(), FakeClient([click, click, click, click]), reobserve,
        tmp_path / "reobs", max_steps=10, settle_s=0, max_reobserve=2,
    )
    assert reobserve.hook_calls == []

    vetoed = HookPack()
    res = runner.run(
        FakeAdb(), FakeClient([click]), vetoed, tmp_path / "veto",
        max_steps=10, settle_s=0, approve=lambda _a, _o: False,
    )
    assert res.result == "approve_rejected"
    assert vetoed.hook_calls == []


def test_hook_exception_stops_without_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    pack = HookPack(raises=True)
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            # a second identical click would run only if the hook failure replayed
            # or failed to stop the loop.
            _tool_call({"action": "click", "coordinate": [100, 100]}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)
    assert res.result == "post_execute_hook_failed"
    assert res.actions_executed == 1  # the executed action is still counted
    assert adb.tap_calls == 1  # executed exactly once, never replayed
    assert len(pack.hook_calls) == 1

    records = [
        json.loads(line)
        for line in (tmp_path / "steps.jsonl").read_text().splitlines()
        if line.strip()
    ]
    step1 = [r for r in records if r.get("step") == 1][-1]
    assert step1["executed"] is True  # the device action really happened
    assert step1["post_execute_error"]  # recorded as a hook failure ...
    assert step1["exec_error"] is None  # ... NOT confused with an ADB failure


def test_hook_not_called_for_wait_or_terminate(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(adb_executor.time, "sleep", lambda *_a, **_k: None)
    pack = HookPack()
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "wait", "time": 0.2}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)
    assert res.result == "terminate_success"
    assert pack.hook_calls == []


def test_hook_cannot_mutate_audited_action_or_history(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)

    class MutatingPack(HookPack):
        def on_action_executed(self, *, executed: ResolvedAction) -> None:
            super().on_action_executed(executed=executed)
            executed.resolved_action["coordinate"] = [999, 999]
            executed.resolved_action["action"] = "swipe"
            executed.metadata["injected"] = True

    pack = MutatingPack()
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)

    records = [
        json.loads(line)
        for line in (tmp_path / "steps.jsonl").read_text().splitlines()
        if line.strip()
    ]
    step1 = [r for r in records if r.get("step") == 1][-1]
    # the deep copy handed to the pack absorbed the mutation; the audited action
    # in the journal is untouched
    assert step1["final_resolved_action"] == {"action": "click", "coordinate": [100, 100]}


def test_hook_state_update_prevents_reresolving_search_as_type(tmp_path, monkeypatch):
    # Regression for the migration defect: once the query is typed, the Pack's
    # post-execute state must make the next "click search" resolve to a click,
    # not re-resolve back into another type.
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)

    class StatefulSearchPack(FakePack):
        def __init__(self) -> None:
            super().__init__()
            self.query_typed = False
            self.hook_calls: list[ResolvedAction] = []

        def resolve(self, *, proposed_action, observation, model_text) -> ResolveDecision:
            act = proposed_action.get("action")
            if act == "type":
                return ResolveDecision(
                    verdict=ALLOW,
                    semantic_target="search.query",
                    resolved_action={"action": "type", "text": "iPad"},
                )
            if act == "click":
                if not self.query_typed:
                    # the legacy bug: query state never updated -> re-type
                    return ResolveDecision(
                        verdict=ALLOW,
                        semantic_target="search.query",
                        resolved_action={"action": "type", "text": "iPad"},
                    )
                return ResolveDecision(
                    verdict=ALLOW,
                    semantic_target="search.submit",
                    resolved_action={"action": "click", "coordinate": [900, 50]},
                )
            return super().resolve(
                proposed_action=proposed_action, observation=observation, model_text=model_text
            )

        def on_action_executed(self, *, executed: ResolvedAction) -> None:
            self.hook_calls.append(executed)
            if executed.resolved_action.get("action") == "type":
                self.query_typed = True

    pack = StatefulSearchPack()
    adb = FakeAdb()
    client = FakeClient(
        [
            _tool_call({"action": "type", "text": "iPad"}),
            _tool_call({"action": "click", "coordinate": [900, 50]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)
    assert res.result == "terminate_success"
    assert res.actions_executed == 2
    assert pack.query_typed is True

    records = [
        json.loads(line)
        for line in (tmp_path / "steps.jsonl").read_text().splitlines()
        if line.strip()
    ]
    step2 = [r for r in records if r.get("step") == 2][-1]
    # step 2 executed a click (search submit), NOT a second type
    assert step2["final_resolved_action"] == {"action": "click", "coordinate": [900, 50]}


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


# --- pluggable text backend: runner-level fail-closed + structured fields -----


class VerifyTextPack(FakePack):
    """resolve passes a type action through; verify returns a type whose text the
    backend cannot transmit (to prove verify-stage text rejection is zero-input)."""

    def __init__(self, verify_text: str) -> None:
        super().__init__()
        self._verify_text = verify_text

    def verify(self, *, approved_intent, fresh_observation) -> VerificationResult:
        return VerificationResult(
            verdict=ALLOW, resolved_action={"action": "type", "text": self._verify_text}
        )


class TextRaisingAdb(FakeAdb):
    """FakeAdb whose type_text raises a given TextInputError at execute time."""

    def __init__(self, error: TextInputError) -> None:
        super().__init__()
        self._error = error

    def type_text(self, text: str) -> None:
        self.type_calls += 1
        raise self._error


def _records(tmp_path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "steps.jsonl").read_text().splitlines()
        if line.strip()
    ]


def test_resolve_unsupported_text_blocks_zero_input(tmp_path, monkeypatch):
    # Default ASCII backend + a Chinese resolve -> reject before any device call.
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = FakeClient([_tool_call({"action": "type", "text": "中文"})])
    res = runner.run(adb, client, FakePack(), tmp_path, settle_s=0)

    assert res.result == "resolve_invalid_action"
    assert adb.type_calls == 0 and adb.tap_calls == 0
    rec = _records(tmp_path)[-1]
    assert rec["resolve_reason"] == "text_unsupported_by_backend"
    assert rec["text_error_code"] == "text_unsupported_by_backend"
    assert rec["text_retryable"] is False
    assert rec["action_may_have_executed"] is False


def test_verify_unsupported_text_blocks_zero_input(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = FakeClient([_tool_call({"action": "type", "text": "iPad"})])
    res = runner.run(adb, client, VerifyTextPack("中文"), tmp_path, settle_s=0)

    assert res.result == "verify_invalid_action"
    assert adb.type_calls == 0
    rec = _records(tmp_path)[-1]
    assert rec["verify_reason"] == "text_unsupported_by_backend"
    assert rec["text_error_code"] == "text_unsupported_by_backend"
    assert rec["text_retryable"] is False


def test_text_broadcast_failure_is_ambiguous_execution(tmp_path, monkeypatch):
    # A possible-injection failure at execute -> stop as text_ambiguous_execution,
    # flag action_may_have_executed, never replay.
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = TextRaisingAdb(
        TextInputError("text_input_failed", action_may_have_executed=True)
    )
    client = FakeClient(
        [
            _tool_call({"action": "type", "text": "iPad"}),
            _tool_call({"action": "type", "text": "iPad"}),  # must NOT be reached
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, settle_s=0)

    assert res.result == "text_ambiguous_execution"
    assert res.total_steps == 1  # broke on step 1, no replay
    assert adb.type_calls == 1
    rec = _records(tmp_path)[-1]
    assert rec["executed"] is False
    assert rec["text_error_code"] == "text_input_failed"
    assert rec["action_may_have_executed"] is True


def test_text_error_retryable_preserved_in_log(tmp_path, monkeypatch):
    # A transient backend failure stays exec_refused but preserves retryable=True
    # and the detailed code so an Adapter can decide to retry.
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = TextRaisingAdb(
        TextInputError("text_backend_preflight_failed", retryable=True)
    )
    client = FakeClient([_tool_call({"action": "type", "text": "iPad"})])
    res = runner.run(adb, client, FakePack(), tmp_path, settle_s=0)

    assert res.result == "exec_refused"
    rec = _records(tmp_path)[-1]
    assert rec["text_error_code"] == "text_backend_preflight_failed"
    assert rec["text_retryable"] is True
    assert rec["action_may_have_executed"] is False


# --- Round 3: bounded replan feedback after a zero-side-effect REOBSERVE -------


class RecordingClient(FakeClient):
    """Captures the user instruction text handed to each model call so a test can
    prove exactly which call the one-shot replan feedback reached."""

    def __init__(self, scripts: list[str]) -> None:
        super().__init__(scripts)
        self.instructions: list[str] = []

    def complete(self, messages, max_tokens: int = 700):
        self.instructions.append(messages[1]["content"][0]["text"])
        return super().complete(messages, max_tokens)


class ResolveReplanPack(FakePack):
    """resolve REOBSERVEs the first ``reobserve_times`` proposals, then passes the
    proposal through (ALLOW). It also records the history it is handed each step."""

    def __init__(self, *, reobserve_times: int = 1, reason: str = "need_more_context") -> None:
        super().__init__()
        self._reobserve_times = reobserve_times
        self._reason = reason
        self.resolve_calls = 0
        self.seen_history: list[list] = []

    def instruction(self, history) -> str:
        self.seen_history.append(list(history))
        return "inst"

    def resolve(self, *, proposed_action, observation, model_text) -> ResolveDecision:
        self.resolve_calls += 1
        if self.resolve_calls <= self._reobserve_times:
            return ResolveDecision(
                verdict=REOBSERVE, semantic_target="", resolved_action={}, reason=self._reason
            )
        return super().resolve(
            proposed_action=proposed_action, observation=observation, model_text=model_text
        )


class VerifyReplanPack(FakePack):
    """resolve passes through; verify REOBSERVEs the first ``reobserve_times``
    device steps, then ALLOWs the same payload (never an anti-swap)."""

    def __init__(
        self, *, reobserve_times: int = 1, reason: str = "ui_not_settled", allow_action=None
    ) -> None:
        super().__init__()
        self._reobserve_times = reobserve_times
        self._reason = reason
        # verify's ``approved_intent`` is the semantic_target STRING, not the
        # resolved action, so the ALLOW payload is supplied up front; only its
        # coordinate may differ (anti-swap excludes coordinates).
        self._allow_action = allow_action or {"action": "click", "coordinate": [150, 150]}
        self.verify_calls = 0

    def verify(self, *, approved_intent, fresh_observation) -> VerificationResult:
        self.verify_calls += 1
        if self.verify_calls <= self._reobserve_times:
            return VerificationResult(verdict=REOBSERVE, resolved_action={}, reason=self._reason)
        return VerificationResult(verdict=ALLOW, resolved_action=dict(self._allow_action))


def _feedback_payload(instruction: str) -> dict:
    marker = "PREVIOUS_ACTION_REJECTED"
    assert marker in instruction
    return json.loads(instruction.split(marker, 1)[1])


# A. resolve REOBSERVE -> the rejected proposal + reason reach the NEXT call only.
def test_resolve_reobserve_carries_rejected_action_into_next_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    rejected = {"action": "click", "coordinate": [100, 100]}
    client = RecordingClient(
        [
            _tool_call(rejected),
            _tool_call({"action": "click", "coordinate": [200, 200]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(
        adb, client, ResolveReplanPack(reason="need_more_context"), tmp_path, max_steps=10, settle_s=0
    )
    assert res.result == "terminate_success"
    # the REOBSERVE touched no device; only the second (corrected) click ran.
    assert adb.tap_calls == 1
    assert "PREVIOUS_ACTION_REJECTED" not in client.instructions[0]
    payload = _feedback_payload(client.instructions[1])
    assert payload["source"] == "resolve"
    assert payload["reason"] == "need_more_context"
    assert payload["rejected_action"] == rejected
    # audit: step 1 produced the feedback (0 ADB), step 2 consumed the same fact.
    recs = _records(tmp_path)
    step1 = [r for r in recs if r["step"] == 1][-1]
    step2 = [r for r in recs if r["step"] == 2][-1]
    assert step1["replan_feedback"]["source"] == "resolve"
    assert step1["executed"] is False
    assert step2["replan_feedback_consumed"] == step1["replan_feedback"]


# B. verify REOBSERVE -> the reverted resolved action + verify reason reach next.
def test_verify_reobserve_carries_rejected_action_into_next_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    rejected = {"action": "click", "coordinate": [150, 150]}
    client = RecordingClient(
        [
            _tool_call(rejected),
            _tool_call({"action": "click", "coordinate": [150, 150]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(
        adb, client, VerifyReplanPack(reason="ui_not_settled"), tmp_path, max_steps=10, settle_s=0
    )
    assert res.result == "terminate_success"
    assert adb.tap_calls == 1  # verify REOBSERVE executed nothing; the retry did.
    assert "PREVIOUS_ACTION_REJECTED" not in client.instructions[0]
    payload = _feedback_payload(client.instructions[1])
    assert payload["source"] == "verify"
    assert payload["reason"] == "ui_not_settled"
    assert payload["rejected_action"] == rejected
    assert payload["semantic_target"] == "target:click:150,150"


# C. Bounded: resolve REOBSERVEs forever, but max_reobserve still terminates with
#    the existing result and zero device actions on every round.
def test_replan_feedback_is_bounded_by_max_reobserve(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = RecordingClient(
        [_tool_call({"action": "click", "coordinate": [i * 10, i * 10]}) for i in range(1, 6)]
    )
    res = runner.run(
        adb, client, ResolveReplanPack(reobserve_times=99, reason="still_bad"),
        tmp_path, max_steps=10, settle_s=0, max_reobserve=3,
    )
    assert res.result == "reobserve_required"
    assert res.reobserves == 3
    assert adb.tap_calls == 0  # every step was a zero-side-effect REOBSERVE.
    # the feedback is replaced each round (consumed by the immediately next call).
    assert "PREVIOUS_ACTION_REJECTED" not in client.instructions[0]
    assert "PREVIOUS_ACTION_REJECTED" in client.instructions[1]
    assert "PREVIOUS_ACTION_REJECTED" in client.instructions[2]


# D. BLOCK is fail-closed: never produces replan feedback.
def test_block_verdict_sets_no_replan_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = RecordingClient([_tool_call({"action": "click", "coordinate": [100, 100]})])
    res = runner.run(adb, client, VerdictHookPack(BLOCK), tmp_path, max_steps=10, settle_s=0)
    assert res.result == "resolve_blocked"
    assert adb.tap_calls == 0
    assert _records(tmp_path)[-1]["replan_feedback"] is None


# E. An unknown verdict and a resolve exception are both fail-closed: no feedback.
def test_illegal_verdict_and_resolve_exception_set_no_replan_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)

    adb1 = FakeAdb()
    res1 = runner.run(
        adb1, RecordingClient([_tool_call({"action": "click", "coordinate": [1, 1]})]),
        VerdictHookPack("weird"), tmp_path / "bad", max_steps=10, settle_s=0,
    )
    assert res1.result == "invalid_resolve_verdict"
    assert adb1.tap_calls == 0
    assert _records(tmp_path / "bad")[-1]["replan_feedback"] is None

    class BoomPack(FakePack):
        def resolve(self, *, proposed_action, observation, model_text):
            raise RuntimeError("boom")

    adb2 = FakeAdb()
    res2 = runner.run(
        adb2, RecordingClient([_tool_call({"action": "click", "coordinate": [1, 1]})]),
        BoomPack(), tmp_path / "boom", max_steps=10, settle_s=0,
    )
    assert res2.result == "resolve_error"
    assert adb2.tap_calls == 0
    assert _records(tmp_path / "boom")[-1]["replan_feedback"] is None


# F. One-shot: the feedback reaches ONLY the immediately next call, then is gone.
def test_replan_feedback_is_one_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = RecordingClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),  # rejected
            _tool_call({"action": "click", "coordinate": [200, 200]}),  # executes
            _tool_call({"action": "click", "coordinate": [300, 300]}),  # executes
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, ResolveReplanPack(reobserve_times=1), tmp_path, max_steps=10, settle_s=0)
    assert "PREVIOUS_ACTION_REJECTED" not in client.instructions[0]
    assert "PREVIOUS_ACTION_REJECTED" in client.instructions[1]  # consumed here ...
    assert "PREVIOUS_ACTION_REJECTED" not in client.instructions[2]  # ... not repeated
    assert "PREVIOUS_ACTION_REJECTED" not in client.instructions[3]


# G. The rejected action never enters the history the Pack plans against.
def test_replan_feedback_does_not_pollute_pack_history(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    pack = ResolveReplanPack(reobserve_times=1)
    client = RecordingClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),  # rejected
            _tool_call({"action": "click", "coordinate": [200, 200]}),  # executes
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, pack, tmp_path, max_steps=10, settle_s=0)
    # history handed to the Pack on the retry step is still empty (the rejected
    # action was never committed), even though the model saw the feedback.
    assert pack.seen_history[0] == []
    assert pack.seen_history[1] == []
    assert pack.seen_history[2] and pack.seen_history[2][-1]["action"] == "click"
    assert pack.seen_history[2][-1]["coordinate"] == [200, 200]


# H. The reason is length-capped, unicode-safe, and defaults when empty.
def test_replan_reason_is_bounded_truncated_and_unicode_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    long_reason = "需" * 2000  # unicode + well over the 1024 cap
    adb = FakeAdb()
    client = RecordingClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "click", "coordinate": [200, 200]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, ResolveReplanPack(reason=long_reason), tmp_path, max_steps=10, settle_s=0)
    payload = _feedback_payload(client.instructions[1])
    assert len(payload["reason"]) == 1024
    assert payload["reason"] == long_reason[:1024]  # unicode intact, not mojibake


def test_replan_reason_defaults_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = RecordingClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "click", "coordinate": [200, 200]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    runner.run(adb, client, ResolveReplanPack(reason=""), tmp_path, max_steps=10, settle_s=0)
    payload = _feedback_payload(client.instructions[1])
    assert payload["reason"] == "reobserve_required"


# J. Existing behavior regression: a run with no REOBSERVE never emits feedback.
def test_no_reobserve_run_never_emits_replan_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)
    adb = FakeAdb()
    client = RecordingClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )
    res = runner.run(adb, client, FakePack(), tmp_path, max_steps=10, settle_s=0)
    assert res.result == "terminate_success"
    assert all("PREVIOUS_ACTION_REJECTED" not in inst for inst in client.instructions)
    for rec in _records(tmp_path):
        assert rec["replan_feedback"] is None
        assert rec["replan_feedback_consumed"] is None
