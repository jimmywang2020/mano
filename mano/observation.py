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
     configurable ceiling. On MuMu the real idle span is ~4.3-4.8s, so the
     default is 6000ms; 4000ms deterministically false-kills. Tune per platform.

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

# Measured idle screenshot+dump span on MuMu is ~4.3-4.8s; 6000ms clears it, 4000ms does not.
DEFAULT_MAX_SKEW_MS = 6000


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
    foreground: str  # authoritative value (== foreground_after)
    foreground_before: str
    foreground_after: str
    xml: str  # raw after-tree, kept in memory for Pack guards; never written to logs
    xml_fingerprint: str  # == xml_fingerprint_after
    xml_fingerprint_before: str
    xml_fingerprint_after: str
    capture_started_at: int
    capture_finished_at: int
    foreground_before_at: int
    xml_before_started_at: int
    xml_before_finished_at: int
    screenshot_started_at: int
    screenshot_finished_at: int
    xml_after_started_at: int
    xml_after_finished_at: int
    foreground_after_at: int
    skew_ms: int
    capture_attempt: int
    stability: str  # "stable" | "unstable"

    def to_log(self) -> dict:
        """Loggable view: references + timing + the inputs to the stable verdict,
        so a reviewer can audit why an observation was accepted. No raw XML."""
        return {
            "observation_id": self.observation_id,
            "screenshot": self.screenshot,
            "xml": self.xml_path,
            "foreground": self.foreground,
            "foreground_before": self.foreground_before,
            "foreground_after": self.foreground_after,
            "xml_fingerprint": self.xml_fingerprint,
            "xml_fingerprint_before": self.xml_fingerprint_before,
            "xml_fingerprint_after": self.xml_fingerprint_after,
            "capture_started_at": self.capture_started_at,
            "capture_finished_at": self.capture_finished_at,
            "foreground_before_at": self.foreground_before_at,
            "xml_before_started_at": self.xml_before_started_at,
            "xml_before_finished_at": self.xml_before_finished_at,
            "screenshot_started_at": self.screenshot_started_at,
            "screenshot_finished_at": self.screenshot_finished_at,
            "xml_after_started_at": self.xml_after_started_at,
            "xml_after_finished_at": self.xml_after_finished_at,
            "foreground_after_at": self.foreground_after_at,
            "skew_ms": self.skew_ms,
            "capture_attempt": self.capture_attempt,
            "stability": self.stability,
        }


@dataclass
class FreshnessCheck:
    """A pre-execution re-read of foreground + XML only (no screenshot).

    It is deliberately *not* an Observation: it never captures a frame and never
    fabricates a ``screenshot_*`` timestamp. It only answers "did the page move
    since the proposal?" so the runner can veto a stale action. Foreground is read
    on *both* sides of the slow ui_dump (``before -> ui_dump -> after``) so a
    foreground switch during the dump is caught even when the XML looks unchanged.
    """

    check_id: str
    step: int
    foreground_before: str  # fresh read before the ui_dump
    foreground_after: str  # fresh read after the ui_dump
    xml_fingerprint: str
    started_at: int
    foreground_before_at: int
    xml_at: int
    foreground_after_at: int
    finished_at: int
    proposal_foreground: str  # the proposal's foreground
    proposal_fingerprint: str  # the proposal's XML fingerprint
    foreground_changed: bool
    xml_changed: bool
    changed: bool
    xml: str = ""  # raw tree kept in memory; never written to logs

    def to_log(self) -> dict:
        return {
            "check_id": self.check_id,
            "foreground_before": self.foreground_before,
            "foreground_after": self.foreground_after,
            "xml_fingerprint": self.xml_fingerprint,
            "started_at": self.started_at,
            "foreground_before_at": self.foreground_before_at,
            "xml_at": self.xml_at,
            "foreground_after_at": self.foreground_after_at,
            "finished_at": self.finished_at,
            "proposal_foreground": self.proposal_foreground,
            "proposal_fingerprint": self.proposal_fingerprint,
            "foreground_changed": self.foreground_changed,
            "xml_changed": self.xml_changed,
            "changed": self.changed,
        }


def capture_observation(
    adb,
    run_dir: Path,
    step: int,
    *,
    capture_frame,
    max_attempts: int = 3,
    settle_s: float = 0.4,
    max_skew_ms: int = DEFAULT_MAX_SKEW_MS,
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
    fp_before = fp_after = ""
    fg_before = fg_after = ""
    capture_started_at = capture_finished_at = 0
    fg_before_at = fg_after_at = 0
    xml_before_started_at = xml_before_finished_at = 0
    screenshot_started_at = screenshot_finished_at = 0
    xml_after_started_at = xml_after_finished_at = 0
    skew_ms = 0
    stability = "unstable"
    attempt = 0

    for attempt in range(1, max_attempts + 1):
        capture_started_at = _now_ms()
        fg_before = adb.foreground_package()
        fg_before_at = _now_ms()

        xml_before_started_at = _now_ms()
        xml_before = adb.ui_dump().decode("utf-8", errors="replace")
        xml_before_finished_at = _now_ms()

        screenshot_started_at = _now_ms()
        width, height = capture_frame(adb, shot)
        screenshot_finished_at = _now_ms()

        xml_after_started_at = _now_ms()
        xml_after = adb.ui_dump().decode("utf-8", errors="replace")
        xml_after_finished_at = _now_ms()

        fg_after = adb.foreground_package()
        fg_after_at = _now_ms()
        capture_finished_at = fg_after_at

        xml = xml_after
        fp_before = xml_fingerprint(xml_before)
        fp_after = xml_fingerprint(xml_after)
        skew_ms = capture_finished_at - capture_started_at
        coherent = (
            fg_before == fg_after and fp_before == fp_after and skew_ms <= max_skew_ms
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
        foreground=fg_after,
        foreground_before=fg_before,
        foreground_after=fg_after,
        xml=xml,
        xml_fingerprint=fp_after,
        xml_fingerprint_before=fp_before,
        xml_fingerprint_after=fp_after,
        capture_started_at=capture_started_at,
        capture_finished_at=capture_finished_at,
        foreground_before_at=fg_before_at,
        xml_before_started_at=xml_before_started_at,
        xml_before_finished_at=xml_before_finished_at,
        screenshot_started_at=screenshot_started_at,
        screenshot_finished_at=screenshot_finished_at,
        xml_after_started_at=xml_after_started_at,
        xml_after_finished_at=xml_after_finished_at,
        foreground_after_at=fg_after_at,
        skew_ms=skew_ms,
        capture_attempt=attempt,
        stability=stability,
    )


def check_freshness(adb, proposal: Observation, step: int) -> FreshnessCheck:
    """Re-read foreground + XML just before executing and report whether the page
    moved since ``proposal``. Takes no screenshot and fabricates no frame time.

    Foreground is sampled on both sides of the slow ui_dump; a switch to another
    app during the dump makes ``foreground_changed`` true even if the XML is
    byte-identical, so we never tap into the wrong foreground.
    """
    started = _now_ms()
    foreground_before = adb.foreground_package()
    foreground_before_at = _now_ms()
    xml = adb.ui_dump().decode("utf-8", errors="replace")
    xml_at = _now_ms()
    foreground_after = adb.foreground_package()
    foreground_after_at = _now_ms()
    fp = xml_fingerprint(xml)
    foreground_changed = (
        foreground_before != proposal.foreground or foreground_after != proposal.foreground
    )
    xml_changed = fp != proposal.xml_fingerprint
    return FreshnessCheck(
        check_id=_new_id(),
        step=step,
        foreground_before=foreground_before,
        foreground_after=foreground_after,
        xml_fingerprint=fp,
        started_at=started,
        foreground_before_at=foreground_before_at,
        xml_at=xml_at,
        foreground_after_at=foreground_after_at,
        finished_at=foreground_after_at,
        proposal_foreground=proposal.foreground,
        proposal_fingerprint=proposal.xml_fingerprint,
        foreground_changed=foreground_changed,
        xml_changed=xml_changed,
        changed=foreground_changed or xml_changed,
        xml=xml,
    )
