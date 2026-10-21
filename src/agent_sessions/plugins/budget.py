"""One cooperative wall-clock budget shared by install download/extraction/validation."""

import contextlib
import contextvars
import time

_DEADLINE = contextvars.ContextVar("plugin_install_deadline", default=None)


class BudgetError(ValueError):
    pass


def remaining(cap: float) -> float:
    deadline = _DEADLINE.get()
    left = cap if deadline is None else min(cap, deadline - time.monotonic())
    if left <= 0:
        raise BudgetError("installation exceeded its time limit")
    return left


def check() -> None:
    remaining(float("inf"))


@contextlib.contextmanager
def limited(seconds: float):
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)
