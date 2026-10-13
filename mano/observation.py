"""Bounded, coherent device observation shared by the runner and external consumers.

ADB cannot capture a screenshot and the UI tree atomically: ``uiautomator dump``
is slow, so a naive ``screenshot then ui_dump`` can straddle a page transition and
hand the model one reality while the Pack guard sees another. This module captures
a *bounded, coherent* observation instead of claiming an atomic one.

Stability is generic and carries no app semantics (it does not know what a
"loading spinner" or a "result page" looks like). Two complementary checks:

  a) primary consistency (sandwich): ``XML-before -> screenshot -> XML-after``;
     the observation is stable only when the foreground package is unchanged
     across the window AND both XML fingerprints are identical, i.e. the
     screenshot was bracketed by a still page.
  b) time safety bound: the whole capture span (``skew_ms``) must stay under a
     configurable ceiling; an abnormally slow ADB is treated as unstable.

A mismatch only costs one extra re-capture, so false "unstable" fails safe. A
constant cross-modal contradiction (XML forever "result", PNG forever "loading")
is explicitly NOT a 0.x guarantee: detecting it would require visual semantics in
the Core, which is out of scope.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


def _now_ms() -> int:
    """Wall-clock milliseconds; module-level so tests can inject a fake clock."""
    return int(time.time() * 1000)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def xml_fingerprint(xml: str) -> str:
    return hashlib.sha256(xml.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class Observation:
    """One bounded, coherent view of the device at a point in the run."""

    observation_id: str
    step: int
    screenshot: str  # file name of the PNG inside run_dir
    xml_path: str  # file name of the XML inside run_dir
    width: int
    height: int
    foreground: str
    xml: str  # raw tree, kept in memory for Pack guards; never written to logs
    xml_fingerprint: str
    capture_started_at: int
    capture_finished_at: int
    xml_before_at: int
    screenshot_at: int
    xml_after_at: int
    foreground_at: int
    skew_ms: int
    capture_attempt: int
    stability: str  # "stable" | "unstable"

    def to_log(self) -> dict:
        """Loggable view: references + timing + verdict, without the raw XML."""
        return {
            "observation_id": self.observation_id,
            "screenshot": self.screenshot,
            "xml": self.xml_path,
            "foreground": self.foreground,
            "xml_fingerprint": self.xml_fingerprint,
            "capture_started_at": self.capture_started_at,
            "capture_finished_at": self.capture_finished_at,
            "xml_before_at": self.xml_before_at,
            "screenshot_at": self.screenshot_at,
            "xml_after_at": self.xml_after_at,
            "foreground_at": self.foreground_at,
            "skew_ms": self.skew_ms,
            "capture_attempt": self.capture_attempt,
            "stability": self.stability,
        }


def capture_observation(
    adb,
    run_dir: Path,
    step: int,
    *,
    capture_frame,
    max_attempts: int = 3,
    settle_s: float = 0.4,
    max_skew_ms: int = 4000,
) -> Observation:
    """Capture a bounded, coherent observation with a sandwich stability check.

    ``capture_frame(adb, path) -> (width, height)`` is injected (the runner passes
    its screenshot-with-reconnect helper) so this module stays free of ADB retry
    policy. On each attempt the screenshot is bracketed by two ui_dumps; the
    attempt is stable when the foreground is unchanged, both XML fingerprints
    match, and the span stays within ``max_skew_ms``. After ``max_attempts`` the
    latest (possibly unstable) view is returned with ``stability == "unstable"``;
    the caller must not drive the model or execute a device action on it.
    """
    run_dir = Path(run_dir)
    shot = run_dir / f"step-{step:02d}.png"
    xml_path = run_dir / f"step-{step:02d}.xml"

    width = height = 0
    xml = ""
    foreground = ""
    attempt_start = xml_before_at = screenshot_at = xml_after_at = foreground_at = _now_ms()
    skew_ms = 0
    stability = "unstable"
    attempt = 0

    for attempt in range(1, max_attempts + 1):
        attempt_start = _now_ms()
        fg_before = adb.foreground_package()
        xml_before_at = _now_ms()
        xml_before = adb.ui_dump().decode("utf-8", errors="replace")
        width, height = capture_frame(adb, shot)
        screenshot_at = _now_ms()
        xml_after_at = _now_ms()
        xml_after = adb.ui_dump().decode("utf-8", errors="replace")
        fg_after = adb.foreground_package()
        foreground_at = _now_ms()

        xml = xml_after
        foreground = fg_after
        skew_ms = foreground_at - attempt_start
        coherent = (
            fg_before == fg_after
            and xml_fingerprint(xml_before) == xml_fingerprint(xml_after)
            and skew_ms <= max_skew_ms
        )
        if coherent:
            stability = "stable"
            break
        stability = "unstable"
        if attempt < max_attempts:
            time.sleep(settle_s)

    xml_path.write_text(xml, encoding="utf-8")
    return Observation(
        observation_id=_new_id(),
        step=step,
        screenshot=shot.name,
        xml_path=xml_path.name,
        width=width,
        height=height,
        foreground=foreground,
        xml=xml,
        xml_fingerprint=xml_fingerprint(xml),
        capture_started_at=attempt_start,
        capture_finished_at=foreground_at,
        xml_before_at=xml_before_at,
        screenshot_at=screenshot_at,
        xml_after_at=xml_after_at,
        foreground_at=foreground_at,
        skew_ms=skew_ms,
        capture_attempt=attempt,
        stability=stability,
    )


def recheck_before_execute(adb, proposal: Observation, step: int) -> Observation:
    """Lightweight freshness re-observation taken just before a device action.

    Grabs one fresh ``foreground + XML`` (no new screenshot: when the page is
    unchanged the proposal's screenshot is still valid, and when it changed the
    action is discarded anyway). The returned Observation reuses the proposal's
    screenshot/dimension refs but carries a fresh ``observation_id``, foreground,
    XML and timestamps. The runner compares its foreground/fingerprint against the
    proposal to decide whether the old action is still safe to execute.
    """
    started = _now_ms()
    foreground = adb.foreground_package()
    foreground_at = _now_ms()
    xml = adb.ui_dump().decode("utf-8", errors="replace")
    xml_at = _now_ms()
    return Observation(
        observation_id=_new_id(),
        step=step,
        screenshot=proposal.screenshot,
        xml_path=proposal.xml_path,
        width=proposal.width,
        height=proposal.height,
        foreground=foreground,
        xml=xml,
        xml_fingerprint=xml_fingerprint(xml),
        capture_started_at=started,
        capture_finished_at=xml_at,
        xml_before_at=started,
        screenshot_at=started,
        xml_after_at=xml_at,
        foreground_at=foreground_at,
        skew_ms=xml_at - started,
        capture_attempt=1,
        stability="stable",
    )
