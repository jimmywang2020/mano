"""Whitelisted ADB operations used by the mano engine.

The action surface is intentionally small: tap / swipe / text / a few keyevents /
screenshot / ui dump. Inputs are validated before they reach the device.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from shutil import which

from .errors import ManoError
from .action_codec import int_coord
from .media import png_size


# MuMu Player ships its own adb; used only as a fallback when there is no system
# adb on PATH and no MANO_ADB_PATH override.
_MUMU_ADB = Path(
    "/Applications/MuMuPlayer.app/Contents/MacOS/"
    "MuMuEmulator.app/Contents/MacOS/tools/adb"
)


def find_adb() -> str:
    override = os.getenv("MANO_ADB_PATH", "")
    if override:
        if Path(override).is_file() and os.access(override, os.X_OK):
            return override
        raise ManoError(f"MANO_ADB_PATH is not an executable adb: {override}")
    found = which("adb")
    if found:
        return found
    if _MUMU_ADB.is_file() and os.access(_MUMU_ADB, os.X_OK):
        return str(_MUMU_ADB)
    raise ManoError("adb not found; install Android platform-tools or set MANO_ADB_PATH")


class Adb:
    """ADB executor with a deliberately small input/action surface."""

    def __init__(self, serial: str):
        self.binary = find_adb()
        self.serial = serial

    def run(self, *args: str, capture: bool = True) -> subprocess.CompletedProcess[bytes]:
        cmd = [self.binary, "-s", self.serial, *args]
        return subprocess.run(cmd, check=True, capture_output=capture)

    def shell(self, *args: str) -> str:
        result = self.run("shell", *args)
        return result.stdout.decode("utf-8", errors="replace").strip()

    def screenshot(self, path: Path) -> tuple[int, int]:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self.run("exec-out", "screencap", "-p")
        path.write_bytes(result.stdout)
        return png_size(path)

    def cold_start(self, package: str) -> None:
        self.shell("am", "force-stop", package)
        time.sleep(1)
        self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(3)

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 700) -> None:
        self.shell(
            "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)
        )

    def type_text(self, text: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9 +._-]{1,80}", text):
            raise ManoError(f"refusing non-whitelisted text input: {text!r}")
        self.shell("input", "text", text.replace(" ", "%s"))

    def keyevent(self, value: str) -> None:
        allowed = {"BACK": "4", "HOME": "3", "ENTER": "66", "CLEAR": "28"}
        key = allowed.get(value.upper())
        if key is None:
            raise ManoError(f"refusing non-whitelisted keyevent: {value}")
        self.shell("input", "keyevent", key)

    def ui_dump(self) -> bytes:
        remote = "/sdcard/mano-window.xml"
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(2):
            try:
                self.shell("uiautomator", "dump", remote)
                return self.run("exec-out", "cat", remote).stdout
            except subprocess.CalledProcessError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)
        assert last_error is not None
        raise last_error


def execute_action(
    adb: Adb, action: dict[str, object], width: int, height: int
) -> bool:
    """Execute one already-resolved, policy-approved action. Returns True on terminate."""
    kind = str(action.get("action", ""))
    if kind == "click":
        adb.tap(*int_coord(action.get("coordinate"), width, height))
    elif kind == "swipe":
        x1, y1 = int_coord(action.get("coordinate"), width, height)
        x2, y2 = int_coord(action.get("coordinate2"), width, height)
        adb.swipe(x1, y1, x2, y2)
    elif kind == "type":
        adb.type_text(str(action.get("text", "")))
    elif kind == "key":
        adb.keyevent(str(action.get("text", "")))
    elif kind == "system_button":
        adb.keyevent(str(action.get("button", "")))
    elif kind == "wait":
        time.sleep(min(max(float(action.get("time", 2)), 0.2), 5.0))
    elif kind == "terminate":
        return True
    else:
        raise ManoError(f"refusing unknown or high-risk action: {kind}")
    return False
