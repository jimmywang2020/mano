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
import re
import time
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
        device_state_may_have_changed: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(detail or code)
        self.code = code
        self.retryable = retryable
        self.action_may_have_executed = action_may_have_executed
        # Distinct from action_may_have_executed: the text may NOT have been typed
        # yet the default/enabled IME state could have been left changed.
        self.device_state_may_have_changed = device_state_may_have_changed


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
    """LEGACY / DEPRECATED Unicode backend over the external *ADB Keyboard* IME.

    Do NOT adopt for new (esp. Chinese) Consumers: real-device MuMu testing proved
    a cold-start false success -- ``_await_active`` only confirms the
    ``default_input_method`` settings value, which flips (~71ms) *before* the
    broadcast receiver is ready, so the first ``ADB_INPUT_B64`` broadcast can be
    silently dropped while the call still returns success. Prefer
    ``OpenAtxAdbKeyboardBackend`` (broadcast-receipt based). Kept only until the
    OpenATX backend passes device acceptance and Consumers migrate off it.

    Named for the concrete dependency on purpose: it needs that specific assistive
    IME, broadcast action and parameter protocol, and is NOT a generic "any
    Android Unicode" transport. It is never auto-installed; ``preflight`` only
    reports whether the IME is present, and ``type_text`` temporarily switches to
    it and restores the original IME in ``finally``.
    """

    name = "adb-keyboard"
    IME_ID = "com.android.adbkeyboard/.AdbIME"
    BROADCAST_ACTION = "ADB_INPUT_B64"
    # A cold-started IME can accept `ime set` yet drop an immediately following
    # broadcast. Poll (bounded, short interval) until it is the active default
    # before injecting; never a blind fixed sleep.
    READY_TIMEOUT_S = 2.0
    READY_POLL_S = 0.1

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

    def _await_active(self, adb) -> bool:
        """Bounded poll until the assistive IME is the active default. Returns
        False on timeout. ``settings get`` is read-only; a probe error counts as
        not-yet-ready, not a hard failure.
        """
        deadline = time.monotonic() + self.READY_TIMEOUT_S
        while True:
            try:
                current = adb.shell(
                    "settings", "get", "secure", "default_input_method"
                ).strip()
            except Exception:
                current = ""
            if current == self.IME_ID:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.READY_POLL_S)

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
        if not original or original == "null":
            # No known IME to switch back to -> refuse before changing anything.
            # Not transient (re-querying won't conjure a default), so not retryable.
            raise TextInputError(
                "text_backend_unavailable", detail="no restorable default IME"
            )

        was_enabled = self.IME_ID in enabled
        injection_attempted = False
        try:
            if not was_enabled:
                adb.shell("ime", "enable", self.IME_ID)
            adb.shell("ime", "set", self.IME_ID)
            if not self._await_active(adb):
                # Switch accepted but IME not active yet: broadcasting now may be
                # silently dropped. Fail before injecting (finally still restores);
                # transient, so retryable once the IME has warmed up.
                raise TextInputError(
                    "text_backend_not_ready",
                    retryable=True,
                    detail=f"assistive IME not active within {self.READY_TIMEOUT_S}s",
                )
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            # From here the device may already hold the text even if ADB's return
            # path then fails: treat any later error as a possible injection.
            injection_attempted = True
            adb.shell("am", "broadcast", "-a", self.BROADCAST_ACTION, "--es", "msg", payload)
        except TextInputError:
            raise
        except Exception as exc:
            raise TextInputError(
                "text_input_failed",
                action_may_have_executed=injection_attempted,
                detail=str(exc)[:200],
            ) from exc
        finally:
            # Restore each piece independently so a failed default-IME restore does
            # not skip re-disabling the assistive IME; aggregate the errors.
            restore_errors: list[str] = []
            try:
                adb.shell("ime", "set", original)
            except Exception as exc:
                restore_errors.append(f"restore default IME: {str(exc)[:120]}")
            if not was_enabled:
                try:
                    adb.shell("ime", "disable", self.IME_ID)
                except Exception as exc:
                    restore_errors.append(f"disable assistive IME: {str(exc)[:120]}")
            if restore_errors:
                raise TextInputError(
                    "text_backend_restore_failed",
                    action_may_have_executed=injection_attempted,
                    device_state_may_have_changed=True,
                    detail="; ".join(restore_errors),
                )


class OpenAtxAdbKeyboardBackend:
    """Optional Unicode backend over the OpenATX uiautomator2 *AdbKeyboard* IME.

    Readiness is proven by the broadcast *receipt*, not a settings value: the
    OpenATX receiver returns ``result=-1`` once it has actually accepted the text
    and ``result=0`` while a cold start is still cancelling, so only an explicit
    ``result=0`` is retried (bounded, same-content ``SET_TEXT`` -> no appended
    duplicates). An unparsable/unexpected receipt is treated as a possible
    injection, never blindly re-sent. Depends on the specific OpenATX component,
    action and parameter protocol; never auto-installed.
    """

    name = "openatx-adb-keyboard"
    PACKAGE = "com.github.uiautomator"
    IME_ID = "com.github.uiautomator/.AdbKeyboard"
    SET_TEXT_ACTION = "ADB_KEYBOARD_SET_TEXT"
    SUCCESS_RESULT = -1
    CANCELLED_RESULT = 0
    MAX_ATTEMPTS = 3
    RETRY_SLEEP_S = 1.0
    _RESULT_RE = re.compile(r"\bresult=(-?\d+)\b")

    def validate(self, text: str) -> str | None:
        # Generic/Unicode safety is enforced upstream in validate_action; the
        # base64 payload carries any such string, so this stays pure + permissive.
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

    def _parse_result(self, output: str) -> int | None:
        match = self._RESULT_RE.search(output or "")
        return int(match.group(1)) if match else None

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
        if not original or original == "null":
            # No known IME to switch back to -> refuse before changing anything.
            raise TextInputError(
                "text_backend_unavailable", detail="no restorable default IME"
            )

        was_enabled = self.IME_ID in enabled
        injection_attempted = False
        try:
            if not was_enabled:
                adb.shell("ime", "enable", self.IME_ID)
            adb.shell("ime", "set", self.IME_ID)  # one switch covers all retries
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            for attempt in range(self.MAX_ATTEMPTS):
                # SET_TEXT replaces, so a retry re-sends identical content (never
                # appends). From the broadcast on, the device may already hold it.
                injection_attempted = True
                output = adb.shell(
                    "am", "broadcast", "-a", self.SET_TEXT_ACTION, "--es", "text", payload
                )
                result = self._parse_result(output)
                if result == self.SUCCESS_RESULT:
                    break
                if result == self.CANCELLED_RESULT:
                    # Explicit cancel: receiver not ready yet, retry same content.
                    if attempt < self.MAX_ATTEMPTS - 1:
                        time.sleep(self.RETRY_SLEEP_S)
                    continue
                # Unexpected number or no parsable receipt -> outcome unknown; do
                # not blindly re-send; conservatively flag a possible injection.
                raise TextInputError(
                    "text_input_failed",
                    action_may_have_executed=True,
                    detail=f"unexpected broadcast receipt: {output[:160]!r}",
                )
            else:
                # Every attempt was an explicit result=0: not received, so the text
                # was not applied -> retryable, and not executed.
                raise TextInputError(
                    "text_backend_not_ready",
                    retryable=True,
                    action_may_have_executed=False,
                    detail=f"OpenATX cancelled (result=0) after {self.MAX_ATTEMPTS} attempts",
                )
        except TextInputError:
            raise
        except Exception as exc:
            raise TextInputError(
                "text_input_failed",
                action_may_have_executed=injection_attempted,
                detail=str(exc)[:200],
            ) from exc
        finally:
            # Restore each piece independently; aggregate errors (same policy as
            # the legacy backend).
            restore_errors: list[str] = []
            try:
                adb.shell("ime", "set", original)
            except Exception as exc:
                restore_errors.append(f"restore default IME: {str(exc)[:120]}")
            if not was_enabled:
                try:
                    adb.shell("ime", "disable", self.IME_ID)
                except Exception as exc:
                    restore_errors.append(f"disable assistive IME: {str(exc)[:120]}")
            if restore_errors:
                raise TextInputError(
                    "text_backend_restore_failed",
                    action_may_have_executed=injection_attempted,
                    device_state_may_have_changed=True,
                    detail="; ".join(restore_errors),
                )
