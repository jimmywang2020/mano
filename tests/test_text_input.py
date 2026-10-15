"""Unit tests for the pluggable text-input backends and the combined validation
entry. No real device: a recording fake ADB drives the shell surface.
"""

from __future__ import annotations

import base64
import subprocess

import pytest

from mano import adb_executor
from mano.text_input import (
    AdbKeyboardTextBackend,
    AsciiAdbTextBackend,
    TextInputError,
)


class RecordingAdb:
    """Records shell() calls; can be told to raise on given arg prefixes."""

    def __init__(
        self,
        *,
        imes_available: str = "",
        imes_enabled: str = "",
        default_ime: str = "com.sogou/.ime",
        raise_prefixes: list[tuple[str, ...]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._imes_available = imes_available
        self._imes_enabled = imes_enabled
        self._default_ime = default_ime
        self._raise_prefixes = raise_prefixes or []
        self.text_backend = AsciiAdbTextBackend()

    def shell(self, *args: str) -> str:
        self.calls.append(args)
        for prefix in self._raise_prefixes:
            if args[: len(prefix)] == prefix:
                raise subprocess.CalledProcessError(1, list(args))
        if args[:4] == ("settings", "get", "secure", "default_input_method"):
            return self._default_ime
        if args[:3] == ("ime", "list", "-s"):
            return self._imes_enabled
        if args[:3] == ("ime", "list", "-a"):
            return self._imes_available
        return ""

    def broadcast_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[:2] == ("am", "broadcast")]


_IME = AdbKeyboardTextBackend.IME_ID


# --- AsciiAdbTextBackend ------------------------------------------------------

def test_ascii_backend_accepts_ascii_rejects_unicode_and_shell():
    backend = AsciiAdbTextBackend()
    assert backend.validate("hello world") is None
    assert backend.validate("iPad Pro (2022)") == "text_unsupported_by_backend"
    assert backend.validate("rm -rf /;") == "text_unsupported_by_backend"
    assert backend.preflight(RecordingAdb()).available is True


def test_ascii_backend_type_text_uses_input_text():
    adb = RecordingAdb()
    AsciiAdbTextBackend().type_text(adb, "hello world")
    assert ("input", "text", "hello%sworld") in adb.calls


def test_ascii_backend_type_text_rejects_unicode_zero_shell():
    adb = RecordingAdb()
    with pytest.raises(TextInputError) as exc:
        AsciiAdbTextBackend().type_text(adb, "iPad Pro (2022) 11.0英寸")
    assert exc.value.code == "text_unsupported_by_backend"
    assert exc.value.action_may_have_executed is False
    assert adb.calls == []


# --- AdbKeyboardTextBackend: validate / preflight -----------------------------

def test_adbkeyboard_validate_accepts_unicode_rejects_empty():
    backend = AdbKeyboardTextBackend()
    assert backend.validate("iPad Pro (2022) 11.0英寸") is None
    assert backend.validate("") == "text_unsupported_by_backend"


def test_adbkeyboard_preflight_unavailable_when_ime_missing():
    cap = AdbKeyboardTextBackend().preflight(RecordingAdb(imes_available=""))
    assert cap.available is False and cap.unicode is True
    assert _IME in cap.reason


def test_adbkeyboard_preflight_available_when_ime_present():
    cap = AdbKeyboardTextBackend().preflight(RecordingAdb(imes_available=_IME))
    assert cap.available is True and cap.unicode is True


# --- AdbKeyboardTextBackend: type_text fail-closed + restore ------------------

def test_adbkeyboard_type_text_unavailable_fails_closed():
    adb = RecordingAdb(imes_available="")  # assistive IME not installed
    with pytest.raises(TextInputError) as exc:
        AdbKeyboardTextBackend().type_text(adb, "中文")
    assert exc.value.code == "text_backend_unavailable"
    assert exc.value.action_may_have_executed is False
    assert adb.broadcast_calls() == []  # never injected


def test_adbkeyboard_type_text_injects_and_restores_default_ime():
    adb = RecordingAdb(imes_available=_IME, imes_enabled="", default_ime="com.sogou/.ime")
    AdbKeyboardTextBackend().type_text(adb, "iPad Pro (2022) 11.0英寸")
    payload = base64.b64encode("iPad Pro (2022) 11.0英寸".encode()).decode()
    assert ("am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", payload) in adb.calls
    assert ("ime", "set", "com.sogou/.ime") in adb.calls  # original restored
    assert ("ime", "disable", _IME) in adb.calls  # was not enabled -> disabled back


def test_adbkeyboard_type_text_keeps_originally_enabled_ime():
    adb = RecordingAdb(imes_available=_IME, imes_enabled=_IME, default_ime="com.sogou/.ime")
    AdbKeyboardTextBackend().type_text(adb, "中文")
    assert ("ime", "set", "com.sogou/.ime") in adb.calls
    assert ("ime", "disable", _IME) not in adb.calls  # leave it as it was


def test_adbkeyboard_preflight_query_failure_is_retryable():
    adb = RecordingAdb(imes_available=_IME, raise_prefixes=[("settings", "get")])
    with pytest.raises(TextInputError) as exc:
        AdbKeyboardTextBackend().type_text(adb, "中文")
    assert exc.value.code == "text_backend_preflight_failed"
    assert exc.value.retryable is True
    assert exc.value.action_may_have_executed is False
    assert adb.broadcast_calls() == []


def test_adbkeyboard_empty_default_ime_fails_closed_before_switching():
    # No restorable default IME -> refuse before touching the IME, not retryable.
    adb = RecordingAdb(imes_available=_IME, default_ime="null")
    with pytest.raises(TextInputError) as exc:
        AdbKeyboardTextBackend().type_text(adb, "中文")
    assert exc.value.code == "text_backend_unavailable"
    assert exc.value.retryable is False
    assert adb.broadcast_calls() == []
    assert ("ime", "set", _IME) not in adb.calls  # never switched away


def test_adbkeyboard_broadcast_failure_flags_ambiguous():
    # The broadcast may have reached the device even if ADB's return path fails:
    # conservatively flag a possible injection so the queue never replays.
    adb = RecordingAdb(imes_available=_IME, raise_prefixes=[("am", "broadcast")])
    with pytest.raises(TextInputError) as exc:
        AdbKeyboardTextBackend().type_text(adb, "中文")
    assert exc.value.code == "text_input_failed"
    assert exc.value.action_may_have_executed is True


def test_adbkeyboard_restore_failure_flags_ambiguous_execution():
    # Injection succeeds, but restoring the original IME fails -> the text may
    # already be on screen: ambiguous, must not replay.
    adb = RecordingAdb(
        imes_available=_IME,
        imes_enabled="",
        default_ime="com.sogou/.ime",
        raise_prefixes=[("ime", "set", "com.sogou/.ime")],  # only the restore
    )
    with pytest.raises(TextInputError) as exc:
        AdbKeyboardTextBackend().type_text(adb, "中文")
    assert exc.value.code == "text_backend_restore_failed"
    assert exc.value.action_may_have_executed is True
    assert exc.value.device_state_may_have_changed is True
    assert exc.value.retryable is False
    assert adb.broadcast_calls()  # injection did happen


def test_adbkeyboard_restore_default_failure_still_disables_assistive():
    # The two restore steps are independent: a failed default-IME restore must not
    # skip re-disabling the assistive IME that was not originally enabled.
    adb = RecordingAdb(
        imes_available=_IME,
        imes_enabled="",
        default_ime="com.sogou/.ime",
        raise_prefixes=[("ime", "set", "com.sogou/.ime")],
    )
    with pytest.raises(TextInputError) as exc:
        AdbKeyboardTextBackend().type_text(adb, "中文")
    assert exc.value.code == "text_backend_restore_failed"
    assert ("ime", "disable", _IME) in adb.calls  # attempted despite the earlier failure


# --- combined validation entry + executor defense in depth --------------------

class _StubAdb:
    def __init__(self, backend) -> None:
        self.text_backend = backend
        self.typed: list[str] = []

    def type_text(self, text: str) -> None:
        self.typed.append(text)


def test_validate_executable_action_combines_generic_and_backend():
    ascii_adb = _StubAdb(AsciiAdbTextBackend())
    uni_adb = _StubAdb(AdbKeyboardTextBackend())
    chinese = {"action": "type", "text": "中文"}
    # generic layer passes (Unicode-safe); ASCII backend rejects, Unicode accepts.
    assert adb_executor.validate_executable_action(ascii_adb, chinese) == "text_unsupported_by_backend"
    assert adb_executor.validate_executable_action(uni_adb, chinese) is None
    # generic layer still fails closed for everyone (control char).
    bad = {"action": "type", "text": "a\nb"}
    assert adb_executor.validate_executable_action(uni_adb, bad) == "text_control_character"


def test_validate_executable_action_defaults_to_ascii_without_backend():
    class NoBackend:
        pass

    adb = NoBackend()
    assert adb_executor.validate_executable_action(adb, {"action": "type", "text": "中文"}) == "text_unsupported_by_backend"
    assert adb_executor.validate_executable_action(adb, {"action": "type", "text": "ok"}) is None


def test_execute_action_rejects_unsupported_text_without_injecting():
    adb = _StubAdb(AsciiAdbTextBackend())
    with pytest.raises(adb_executor.ManoError):
        adb_executor.execute_action(adb, {"action": "type", "text": "中文"}, 1000, 1000)
    assert adb.typed == []  # executor rejected before reaching the device


def test_adb_type_text_enforces_generic_safety(monkeypatch):
    # The public Adb.type_text must not bypass the generic/Unicode safety layer,
    # even behind a permissive backend that only checks non-emptiness.
    monkeypatch.setattr(adb_executor, "find_adb", lambda: "/usr/bin/adb")
    adb = adb_executor.Adb("serial", text_backend=AdbKeyboardTextBackend())
    with pytest.raises(TextInputError) as exc:
        adb.type_text("a\nb")
    assert exc.value.code == "text_control_character"
