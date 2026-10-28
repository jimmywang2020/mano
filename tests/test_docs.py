"""Offline doc guards.

1. Every in-repo relative Markdown link resolves to a real file.
2. The integration-guide minimal example (import paths + RunResult fields +
   runner.run call shape) stays accurate, driven entirely by local fakes.

No real device, no real VLM, no network. Mirrors the fakes in test_runner.py so
this file stays self-contained.
"""

from __future__ import annotations

import dataclasses
import json
import re
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that never contain source-of-truth docs.
_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}
# Device-local / git-ignored handoff: not a committed deliverable.
_EXCLUDED_MD = {"HANDOFF_NEXT_SESSION.md"}

_LINK_RE = re.compile(r"\]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.md"):
        if any(part in _EXCLUDED_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path.name in _EXCLUDED_MD:
            continue
        files.append(path)
    return files


def test_repo_markdown_links_resolve() -> None:
    broken: list[str] = []
    for md in _markdown_files():
        text = md.read_text(encoding="utf-8")
        for raw in _LINK_RE.findall(text):
            target = raw.strip()
            # Skip external links and in-page anchors.
            if (
                not target
                or target.startswith("#")
                or target.startswith(("http://", "https://", "mailto:"))
                or "://" in target
            ):
                continue
            # Strip a trailing #anchor and any surrounding whitespace.
            target = target.split("#", 1)[0].strip()
            if not target:
                continue
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{md.relative_to(REPO_ROOT)} -> {raw}")
    assert not broken, "unresolved relative markdown links:\n" + "\n".join(broken)


# --- Minimal integration example, offline (mirrors test_runner.py fakes) ---


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height)


def _tool_call(action: dict) -> str:
    return "<tool_call>" + json.dumps({"name": "mobile_use", "arguments": action}) + "</tool_call>"


class _FakeAdb:
    def __init__(self) -> None:
        self.serial = "emulator-fake"
        self.binary = "/usr/bin/adb"

    def run(self, *args, capture: bool = True):
        return SimpleNamespace(stdout=b"device")

    def screenshot(self, path: Path) -> tuple[int, int]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png_header(1000, 1000))
        return 1000, 1000

    def ui_dump(self) -> bytes:
        return b"<hierarchy/>"

    def foreground_package(self) -> str:
        return "com.test"

    def tap(self, x: int, y: int) -> None:
        pass

    def swipe(self, x1, y1, x2, y2, duration_ms: int = 700) -> None:
        pass

    def type_text(self, text: str) -> None:
        pass

    def keyevent(self, name: str) -> None:
        pass


class _FakeClient:
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


def test_integration_guide_import_paths_resolve() -> None:
    # Exactly the import paths the integration guide tells a Consumer to use.
    from mano.adb_executor import Adb  # noqa: F401
    from mano.runner import run, RunResult, propose_action  # noqa: F401
    from mano.pack import (  # noqa: F401
        Pack,
        ResolveDecision,
        ResolvedAction,
        VerificationResult,
        ALLOW,
        REOBSERVE,
        BLOCK,
    )
    from mano.text_input import (  # noqa: F401
        AsciiAdbTextBackend,
        OpenAtxAdbKeyboardBackend,
        TextInputError,
        TextInputCapability,
    )
    from mano.observation import (  # noqa: F401
        Observation,
        capture_observation,
        DEFAULT_MAX_SKEW_MS,
    )
    from mano.gui_plus_client import GuiVLM  # noqa: F401
    from mano.action_journal import (  # noqa: F401
        SQLiteActionJournal,
        RecoveryReport,
        ActionJournalError,
    )

    assert ALLOW == "ALLOW"
    assert REOBSERVE == "REOBSERVE"
    assert BLOCK == "BLOCK"


def test_run_result_fields_match_docs() -> None:
    """Lock the RunResult shape documented in integration-guide.md §8. If Core adds
    or removes a field, update the docs in the same change."""
    from mano.runner import RunResult

    documented = {
        "result",
        "total_steps",
        "actions_executed",
        "vlm_calls",
        "retries",
        "prompt_tokens",
        "completion_tokens",
        "wall_seconds",
        "run_dir",
        "reobserves",
        "recovery_reports",
        "journal_error_code",
    }
    actual = {f.name for f in dataclasses.fields(RunResult)}
    assert actual == documented


def test_integration_guide_minimal_example_runs_offline(tmp_path, monkeypatch) -> None:
    from mano import runner
    from mano.pack import ALLOW, ResolveDecision

    monkeypatch.setattr(runner.time, "sleep", lambda *_a, **_k: None)

    class _FakePack:
        package = "com.test"

        def system_prompt(self, width: int, height: int) -> str:
            return "sys"

        def instruction(self, history) -> str:
            return "inst"

        def guard(self, xml: str, foreground):
            return None

        def resolve(self, *, proposed_action, observation, model_text) -> ResolveDecision:
            target = "target:" + str(proposed_action.get("action"))
            coord = proposed_action.get("coordinate")
            if isinstance(coord, list) and len(coord) == 2:
                target += f":{coord[0]},{coord[1]}"
            return ResolveDecision(
                verdict=ALLOW,
                semantic_target=target,
                resolved_action=dict(proposed_action),
            )

    client = _FakeClient(
        [
            _tool_call({"action": "click", "coordinate": [100, 100]}),
            _tool_call({"action": "terminate", "status": "success"}),
        ]
    )

    res = runner.run(
        _FakeAdb(),
        client,
        _FakePack(),
        tmp_path,
        max_steps=10,
        settle_s=0,
        capture_settle_s=0,
    )

    assert isinstance(res, runner.RunResult)
    assert res.result == "terminate_success"
    assert res.actions_executed == 1
    assert res.run_dir == str(tmp_path)
