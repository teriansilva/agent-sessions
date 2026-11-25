"""Small shared values for structured clients (#1275).

Correlation is data, never authority. Execution guards are server-created capabilities: their
opaque binding is persisted, while their callable is never accepted from an HTTP request or log.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_CONTEXT_FIELDS = frozenset({"mission_id", "flow_revision", "step_id", "episode", "reservation_id"})


@dataclass(frozen=True)
class ExecutionGuard:
    """Original authority, re-acquired at each effect rather than at submission alone.

    ``acquire`` holds the caller's authorization fence and verifies the original grant. It
    yields an idempotent release callback, so an HTTP adapter can release after request-body
    delivery instead of keeping policy edits blocked through model inference. File decisions
    retain it through the mutation. A future continuation must supply the same binding.
    """

    binding: str
    acquire: Callable[[], AbstractAsyncContextManager[Callable[[], None]]]
    #: For a client whose turn is claimed in ANOTHER process (native workers, #1897): a blocking,
    #: bounded call run off the loop that performs the caller's domain check inside its write
    #: fence and returns the durable authority tokens the worker re-checks at its claim
    #: (`session_input.admit_native`). It raises when authority is withdrawn. A guard without it
    #: cannot fence a native turn, and a native client refuses it.
    admit: Callable[[], dict] | None = None
    #: The operator is behind THIS turn (#2008): their own tap approved it, or HQ sends it in a
    #: turn the Commander started and asked for. Set by server code at the two call sites that
    #: know it (`actuator.deliver`), never read from a request or a stored record. It changes one
    #: thing: such a turn may go into a session that skips permission prompts, as the operator's
    #: typed message may. Unattended work — a mission, an automation, the supervisor — never
    #: carries it, and is refused there as before (#1897, #1613 D8).
    attended: bool = False

    def __post_init__(self) -> None:
        if self.attended is not True and self.attended is not False:
            raise ValueError("execution authority's attended flag must be a bool")
        if not isinstance(self.binding, str) or not _IDENTIFIER.fullmatch(self.binding):
            raise ValueError("execution authority needs a bounded opaque binding")
        if not callable(self.acquire):
            raise ValueError("execution authority needs a server admission factory")
        if self.admit is not None and not callable(self.admit):
            raise ValueError("execution authority's cross-process admission must be callable")


def normalize_context(value: object = None) -> dict:
    """Validate and detach bounded flow correlation; domain owners validate referenced rows.

    Revisions are opaque strings; episodes are nonnegative integers. In particular, bools are
    not episode numbers and nested objects cannot smuggle a mutable authority claim into a turn.
    """
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _CONTEXT_FIELDS:
        raise ValueError("invalid structured turn correlation fields")
    out = {}
    for key, item in value.items():
        if key == "episode":
            if type(item) is not int or not 0 <= item <= 2**53 - 1:
                raise ValueError("episode must be a nonnegative safe integer")
        elif not isinstance(item, str) or not _IDENTIFIER.fullmatch(item):
            raise ValueError(f"{key} must be a bounded opaque identifier")
        out[key] = item
    return out
