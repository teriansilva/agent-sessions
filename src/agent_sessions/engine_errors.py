"""`EngineError` in a leaf module (#853 P2).

It lives here, importing nothing from the app, so `agent_sessions.plugins` can raise it without
importing the `engines` package — whose registry is built FROM the plugins package, which made
"import plugins first" a circular import. `engines.base` re-exports it, so every existing
`engines.EngineError` / `base.EngineError` reference is the same class.
"""

from __future__ import annotations


class EngineError(RuntimeError):
    """Unknown engine, malformed native id, or an operation the engine refuses."""
