"""mano: a thin VLM-driven Android GUI automation engine.

The engine proposes one safe action at a time with a hosted GUI VLM and executes
it through a small, whitelisted ADB surface. App-specific knowledge (prompts,
guards, deterministic solvers) lives in ``packs/``, never in the engine.
"""

from .errors import ManoError, ManoNetworkError

__all__ = ["ManoError", "ManoNetworkError"]
__version__ = "0.1.0"
