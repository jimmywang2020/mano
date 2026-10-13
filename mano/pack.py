"""App-pack protocol for the thin runner.

A Pack only reports *what it sees* and *what to tell the VLM*; it never touches
ADB, coordinates, or evidence. The runner owns the device, the loop, and the
reliability guards. This is a 0.x experimental interface, not a frozen ABI.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Pack(Protocol):
    """The minimal surface the thin runner needs from an app pack.

    Deliberately excludes deterministic solvers: those stay inside their pack
    and, when wired in later, must return *at most one* action per observation
    rather than driving the device themselves.
    """

    package: str

    def system_prompt(self, width: int, height: int) -> str:
        """Return the system prompt for a screen of ``width`` x ``height`` px."""
        ...

    def instruction(self, history: list[dict]) -> str:
        """Return the per-step user instruction given the action history."""
        ...

    def guard(self, xml: str, foreground: str | None) -> str | None:
        """Return a stop reason (used verbatim as the run result) or ``None``.

        ``foreground`` is the current foreground package read by the runner;
        the pack compares it against ``self.package`` and inspects ``xml`` to
        decide whether to stop (e.g. left app, paywall) or keep going.
        """
        ...
