"""Domain errors shared by the mano engine and its app packs."""


class ManoError(RuntimeError):
    """A user-facing, safely reportable mano failure."""


class ManoNetworkError(ManoError):
    """A transient network failure while talking to the VLM provider.

    Raised separately so callers can retry on network blips without matching on
    error-message substrings.
    """
