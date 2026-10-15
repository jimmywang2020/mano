"""Pluggable text-input backends.

The action protocol is unchanged (``{"action": "type", "text": "..."}``); the
*transport* used to put that text on the device is a backend chosen by whoever
constructs the executor -- the model and the Pack never pick one.

Two orthogonal validation layers:
  * generic/structural + Unicode safety lives in ``action_codec.validate_action``
    (non-empty, length cap, no control/format/surrogate characters);
  * charset/transport capability lives in ``backend.validate`` (pure, no device).

A backend's ``preflight`` may run *read-only* ADB to report availability; it must
never modify device state. ``type_text`` is the only method that changes the
device, and it must re-validate (defense in depth) before injecting.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .action_codec import TYPE_TEXT_RE
from .errors import ManoError


@dataclass(frozen=True)
class TextInputCapability:
    """Read-only preflight result for a backend on a specific device."""

    name: str
    available: bool
    unicode: bool
    reason: str = ""


class TextInputError(ManoError):
    """A text-input failure carrying a structured code and two review flags.

    ``action_may_have_executed`` is the important one: a restore failure after a
    successful injection leaves an *ambiguous* execution state, so the run must
    stop without replaying the input.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        action_may_have_executed: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(detail or code)
        self.code = code
        self.retryable = retryable
        self.action_may_have_executed = action_may_have_executed


@runtime_checkable
class TextInputBackend(Protocol):
    """A text transport. ``validate`` is a pure function (no device); ``preflight``
    is a read-only device check; only ``type_text`` modifies the device."""

    name: str

    def validate(self, text: str) -> str | None:
        ...

    def preflight(self, adb) -> TextInputCapability:
        ...

    def type_text(self, adb, text: str) -> None:
        ...


class AsciiAdbTextBackend:
    """Default backend: ``adb shell input text`` with the shell-safe ASCII subset.

    Behaviour is byte-for-byte the pre-existing one, so every current ASCII flow
    keeps working with no configuration.
    """

    name = "ascii-adb"

    def validate(self, text: str) -> str | None:
        if not isinstance(text, str) or TYPE_TEXT_RE.fullmatch(text) is None:
            return "text_unsupported_by_backend"
        return None

    def preflight(self, adb) -> TextInputCapability:
        # `input text` is always present; ASCII transport needs no assistive IME.
        return TextInputCapability(self.name, available=True, unicode=False)

    def type_text(self, adb, text: str) -> None:
        code = self.validate(text)
        if code is not None:
            raise TextInputError(code)
        try:
            adb.shell("input", "text", text.replace(" ", "%s"))
        except Exception as exc:
            raise TextInputError("text_input_failed", detail=str(exc)[:200]) from exc


class AdbKeyboardTextBackend:
    """Optional Unicode backend that relies on the external *ADB Keyboard* IME.

    Named for the concrete dependency on purpose: it needs that specific assistive
    IME, broadcast action and parameter protocol, and is NOT a generic "any
    Android Unicode" transport. It is never auto-installed; ``preflight`` only
    reports whether the IME is present, and ``type_text`` temporarily switches to
    it and restores the original IME in ``finally``.
    """

    name = "adb-keyboard"
    IME_ID = "com.android.adbkeyboard/.AdbIME"
    BROADCAST_ACTION = "ADB_INPUT_B64"

    def validate(self, text: str) -> str | None:
        # Generic/Unicode safety is enforced upstream in validate_action; the
        # base64 broadcast can carry any such string, so this stays pure + permissive.
        if not isinstance(text, str) or not text:
            return "text_unsupported_by_backend"
        return None

    def preflight(self, adb) -> TextInputCapability:
        try:
            installed = adb.shell("ime", "list", "-a")
        except Exception as exc:
            return TextInputCapability(
                self.name, available=False, unicode=True,
                reason=f"ime list failed: {str(exc)[:200]}",
            )
        if self.IME_ID in installed:
            return TextInputCapability(self.name, available=True, unicode=True)
        return TextInputCapability(
            self.name, available=False, unicode=True,
            reason=f"assistive IME not installed: {self.IME_ID}",
        )

    def type_text(self, adb, text: str) -> None:
        code = self.validate(text)
        if code is not None:
            raise TextInputError(code)
        cap = self.preflight(adb)
        if not cap.available:
            raise TextInputError("text_backend_unavailable", detail=cap.reason)

        try:
            original = adb.shell("settings", "get", "secure", "default_input_method").strip()
            enabled = adb.shell("ime", "list", "-s")
        except Exception as exc:
            raise TextInputError(
                "text_backend_preflight_failed", retryable=True, detail=str(exc)[:200]
            ) from exc

        was_enabled = self.IME_ID in enabled
        injected = False
        try:
            if not was_enabled:
                adb.shell("ime", "enable", self.IME_ID)
            adb.shell("ime", "set", self.IME_ID)
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            adb.shell("am", "broadcast", "-a", self.BROADCAST_ACTION, "--es", "msg", payload)
            injected = True
        except TextInputError:
            raise
        except Exception as exc:
            raise TextInputError("text_input_failed", detail=str(exc)[:200]) from exc
        finally:
            # Restore the original IME. If the text was already injected but this
            # fails, the execution state is ambiguous -> flag it, do not replay.
            try:
                if original and original != "null":
                    adb.shell("ime", "set", original)
                if not was_enabled:
                    adb.shell("ime", "disable", self.IME_ID)
            except Exception as exc:
                raise TextInputError(
                    "text_backend_restore_failed",
                    action_may_have_executed=injected,
                    detail=str(exc)[:200],
                ) from exc
