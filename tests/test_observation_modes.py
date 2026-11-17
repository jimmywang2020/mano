"""Unit tests for capture_observation modes (full / tree / shot)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mano.observation import Observation, capture_observation


class _FakeAdb:
    def __init__(self, *, xml: bytes = b"<hierarchy/>", fg: str = "com.test"):
        self.xml = xml
        self.fg = fg
        self.dump_calls = 0
        self.fg_calls = 0

    def foreground_package(self) -> str:
        self.fg_calls += 1
        return self.fg

    def ui_dump(self) -> bytes:
        self.dump_calls += 1
        return self.xml


def _capture_frame(adb, path: Path) -> tuple[int, int]:
    path.write_bytes(b"PNG")
    return 100, 200


def test_full_mode_default_writes_shot_and_xml(tmp_path: Path) -> None:
    adb = _FakeAdb()
    obs = capture_observation(adb, tmp_path, 1, capture_frame=_capture_frame)
    assert obs.capture_mode == "full"
    assert obs.screenshot == "step-01.png"
    assert obs.xml_path == "step-01.xml"
    assert (tmp_path / obs.screenshot).is_file()
    assert (tmp_path / obs.xml_path).is_file()
    assert obs.width == 100 and obs.height == 200
    assert adb.dump_calls == 1
    assert obs.stability == "stable"
    log = obs.to_log()
    assert log["capture_mode"] == "full"
    assert log["screenshot_skipped"] is False
    assert log["xml_skipped"] is False
    assert "screenshot_ms" in log and "xml_ms" in log


def test_tree_mode_skips_screenshot(tmp_path: Path) -> None:
    adb = _FakeAdb(xml=b"<hierarchy><node text='Hi'/></hierarchy>")
    frames: list[Path] = []

    def capture_frame(adb, path: Path) -> tuple[int, int]:
        frames.append(path)
        return _capture_frame(adb, path)

    obs = capture_observation(
        adb,
        tmp_path,
        2,
        capture_frame=capture_frame,
        mode="tree",
        fallback_size=(1080, 2400),
    )
    assert frames == []
    assert obs.capture_mode == "tree"
    assert obs.screenshot == ""
    assert obs.screenshot_skipped is True
    assert obs.xml_skipped is False
    assert obs.xml_path == "step-02.xml"
    assert (tmp_path / obs.xml_path).is_file()
    assert not (tmp_path / "step-02.png").exists()
    assert obs.width == 1080 and obs.height == 2400
    assert "Hi" in obs.xml
    assert obs.screenshot_ms == 0
    assert obs.xml_ms >= 0
    assert obs.stability == "stable"


def test_shot_mode_skips_xml_dump(tmp_path: Path) -> None:
    adb = _FakeAdb()
    obs = capture_observation(
        adb, tmp_path, 3, capture_frame=_capture_frame, mode="shot"
    )
    assert adb.dump_calls == 0
    assert obs.capture_mode == "shot"
    assert obs.xml_skipped is True
    assert obs.screenshot_skipped is False
    assert obs.screenshot == "step-03.png"
    assert obs.xml_path == ""
    assert obs.xml == ""
    assert obs.xml_fingerprint == ""
    assert (tmp_path / obs.screenshot).is_file()
    assert not (tmp_path / "step-03.xml").exists()
    assert obs.width == 100 and obs.height == 200
    assert obs.stability == "stable"


def test_unsupported_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported capture mode"):
        capture_observation(
            _FakeAdb(),
            tmp_path,
            1,
            capture_frame=_capture_frame,
            mode="both",  # type: ignore[arg-type]
        )


def test_observation_breakdown_properties() -> None:
    obs = Observation(
        observation_id="abc",
        step=1,
        screenshot="step-01.png",
        xml_path="step-01.xml",
        width=1,
        height=2,
        foreground="com.x",
        foreground_before="com.x",
        foreground_after="com.x",
        xml="<a/>",
        xml_fingerprint="f",
        capture_started_at=1000,
        capture_finished_at=1500,
        foreground_before_at=1001,
        screenshot_started_at=1100,
        screenshot_finished_at=1300,
        xml_started_at=1300,
        xml_finished_at=1450,
        foreground_after_at=1500,
        skew_ms=500,
        capture_attempt=1,
        stability="stable",
        capture_mode="full",
    )
    assert obs.screenshot_ms == 200
    assert obs.xml_ms == 150
