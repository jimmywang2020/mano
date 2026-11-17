"""Domain errors shared by the mano engine and its app packs."""


class ManoError(RuntimeError):
    """A user-facing, safely reportable mano failure.

    Optional ``code`` is set only when the caller passes ``code=`` so subclass
    class attributes (e.g. journal ``DB_BUSY``) are not clobbered by ``""``.
    """

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class ManoNetworkError(ManoError):
    """A transient network failure while talking to the VLM provider.

    Raised separately so callers can retry on network blips without matching on
    error-message substrings.
    """


class ManoDeviceError(ManoError):
    """ADB / device-side failure with a stable ``code`` for adapters.

    Codes used by Core today:
    - ``UI_DUMP_FAILED`` — uiautomator dump exhausted retries
    - ``UI_DUMP_EMPTY`` — dump succeeded but returned empty XML
    - ``UI_DUMP_KILLED`` — dump process likely OOM/killed (e.g. exit 137)
    """
