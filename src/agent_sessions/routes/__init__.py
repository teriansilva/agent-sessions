"""Route registrars extracted from ``main.create_app`` (agent-sessions#265).

Each submodule exposes a ``register(app, *, <deps>)`` that attaches one group of
routes to the app, threading the in-scope closures/locals from ``create_app`` as
keyword args. Behavior is identical to the inline definitions — same paths, deps,
status codes, and bodies.
"""

from . import scrollback, sessions, system, upload

__all__ = ["scrollback", "sessions", "system", "upload"]
