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

    def __post_init__(self) -> None:
        if not isinstance(self.binding, str) or not _IDENTIFIER.fullmatch(self.binding):
            raise ValueError("execution authority needs a bounded opaque binding")
        if not callable(self.acquire):
            raise ValueError("execution authority needs a server admission factory")


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
