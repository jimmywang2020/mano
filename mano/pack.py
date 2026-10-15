"""App-pack protocol for the thin runner.

A Pack reports *what it sees*, *what to tell the VLM*, and *how to resolve /
re-verify a semantic action*; it never touches ADB, coordinates it cannot
justify, or evidence. The runner owns the device, the loop, and the reliability
guards. This is a 0.x experimental interface, not a frozen ABI.

Responsibility split (see docs/adding-a-pack.md §7):
  * Core  -- owns observation, the loop order, approval gating, re-observation,
            the foreground safety boundary, the anti-swap check, and single
            execution. It never interprets a control.
  * Pack  -- interprets the app: ``resolve`` turns a raw model action into a
            stable ``semantic_target`` + a concrete physical action; ``verify``
            re-checks that the *same* semantic target still holds on the latest
            page and may re-resolve coordinates (never the target or side-effects).
  * Consumer -- authorizes the semantic action (business rule); approve/veto only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Verdicts shared by resolve() and verify(). REPLAN is intentionally not used.
ALLOW = "ALLOW"
REOBSERVE = "REOBSERVE"
BLOCK = "BLOCK"


@dataclass(frozen=True)
class ResolvedAction:
    """A model action resolved to a stable semantic identity + a physical action.

    ``semantic_target`` is an opaque, comparable identity (e.g. ``home.search_field``).
    The Core never interprets it; it only freezes it, together with the action's
    non-coordinate side-effect fields, and refuses any later change to them.
    ``resolved_action`` is the concrete action for the *current* page; only its
    coordinates may be re-resolved later by ``verify``. ``metadata`` is auditable
    provenance (matched text, rule id, ...).
    """

    semantic_target: str
    resolved_action: dict
    metadata: dict = field(default_factory=dict)


@dataclass
class ResolveDecision:
    """Result of ``Pack.resolve``: a verdict plus the resolved semantic action."""

    verdict: str  # ALLOW | REOBSERVE | BLOCK
    semantic_target: str
    resolved_action: dict
    reason: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class VerificationResult:
    """Result of ``Pack.verify``: does the approved semantic target still hold?

    On ``ALLOW`` the pack returns a fresh ``resolved_action`` (coordinates may be
    updated for the latest page). The Core rejects it if any non-coordinate field
    differs from the approved action. ``REOBSERVE`` means the page is not ready
    (zero ADB, re-observe); ``BLOCK`` means a safety boundary was hit.
    """

    verdict: str  # ALLOW | REOBSERVE | BLOCK
    resolved_action: dict | None = None  # present only on ALLOW
    reason: str = ""
    metadata: dict = field(default_factory=dict)


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

    def resolve(
        self, *, proposed_action: dict, observation, model_text: str
    ) -> ResolveDecision:
        """Resolve a raw model action into a stable semantic target + physical
        action. Required: the runner classifies and gates on the *resolved*
        action, not the raw model action (a model ``wait`` may resolve to a
        device-modifying ``click``)."""
        ...

    # verify(*, approved_intent: str, fresh_observation) -> VerificationResult
    # is an OPTIONAL capability. A pack without it runs the legacy raw-XML
    # fallback (foreground/raw-XML change -> REOBSERVE; otherwise execute the
    # initial resolved action). Real packs must implement verify.

    # on_action_executed(*, executed: ResolvedAction) -> None is an OPTIONAL
    # post-execute notification. The runner calls it once per *successfully
    # executed device action*, after ADB confirms the change and before the next
    # observation; never on wait/terminate, BLOCK/REOBSERVE, approve veto,
    # foreground change, verify rejection, anti-swap, or ADB failure. ``executed``
    # carries the semantic target, the final action handed to ADB (post-verify
    # coordinates), and the final-stage metadata (verify's when verify ran -- even
    # {} -- else resolve's), passed as deep copies; the Core never reads it. A
    # pack that raises here stops the run (``post_execute_hook_failed``); the
    # action already happened and is never replayed. Exactly-once holds only
    # within one process's normal control flow, not across a crash.
