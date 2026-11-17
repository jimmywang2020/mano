"""Unit tests for Adb.ui_dump compressed-first path and warm_start."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from mano.adb_executor import Adb
from mano.errors import ManoDeviceError


class _RecordingAdb(Adb):
    """Adb subclass that never touches a real device."""

    def __init__(self) -> None:
        # Skip Adb.__init__ (find_adb).
        self.binary = "adb"
        self.serial = "fake"
        self.shell_calls: list[tuple[str, ...]] = []
        self._shell_plan: list[Any] = []
        self._cat_payload = b"<hierarchy/>"
        self.text_backend = None  # type: ignore[assignment]

    def plan_shell(self, *outcomes: Any) -> None:
        self._shell_plan = list(outcomes)

    def shell(self, *args: str) -> str:
        self.shell_calls.append(args)
        if not self._shell_plan:
            return "OK"
        outcome = self._shell_plan.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    def run(self, *args: str, capture: bool = True):  # type: ignore[override]
        # Only used for exec-out cat of the dump file.
        class _R:
            stdout = self._cat_payload
            returncode = 0

        return _R()


def _cpe(code: int = 1) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(code, ["adb", "shell"])


def test_ui_dump_prefers_compressed() -> None:
    adb = _RecordingAdb()
    adb.plan_shell("UI hierchary dumped to: /sdcard/mano-window.xml")
    raw = adb.ui_dump()
    assert raw.startswith(b"<hierarchy")
    assert adb.shell_calls[0] == ("uiautomator", "dump", "--compressed", "/sdcard/mano-window.xml")


def test_ui_dump_falls_back_to_plain() -> None:
    adb = _RecordingAdb()
    adb.plan_shell(_cpe(1), _cpe(1), "dumped")
    raw = adb.ui_dump()
    assert b"hierarchy" in raw
    assert ("uiautomator", "dump", "--compressed", "/sdcard/mano-window.xml") in adb.shell_calls
    assert ("uiautomator", "dump", "/sdcard/mano-window.xml") in adb.shell_calls


def test_ui_dump_empty_raises() -> None:
    adb = _RecordingAdb()
    adb._cat_payload = b"   "
    adb.plan_shell("ok")
    with pytest.raises(ManoDeviceError) as ei:
        adb.ui_dump()
    assert ei.value.code == "UI_DUMP_EMPTY"


def test_ui_dump_killed_code() -> None:
    adb = _RecordingAdb()
    # 2 compressed + 2 plain all killed
    adb.plan_shell(_cpe(137), _cpe(137), _cpe(137), _cpe(137))
    with pytest.raises(ManoDeviceError) as ei:
        adb.ui_dump()
    assert ei.value.code == "UI_DUMP_KILLED"


def test_warm_start_no_force_stop() -> None:
    adb = _RecordingAdb()
    sleeps: list[float] = []

    import mano.adb_executor as mod

    real_sleep = mod.time.sleep
    mod.time.sleep = lambda s: sleeps.append(s)  # type: ignore[assignment]
    try:
        adb.warm_start("com.example.app")
    finally:
        mod.time.sleep = real_sleep
    assert adb.shell_calls == [
        ("monkey", "-p", "com.example.app", "-c", "android.intent.category.LAUNCHER", "1")
    ]
    assert sleeps == [1.5]
    assert not any("force-stop" in c for c in adb.shell_calls)
