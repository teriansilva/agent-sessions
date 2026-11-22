"""The closed executor table (#2248 P1): the reviewed code a manifest's ``executor`` may name.

A manifest can only CHOOSE one of these; it can never add one. In P1 the table holds exactly the
five actions automations had before the registry existed, and every executor is an ADAPTER: its
five functions delegate to the code that already did the work, unchanged.

* ``validate`` → `automations.validate_action` (its branch for this kind);
* ``scope``    → `automations.scope_of`;
* ``describe`` → `automations.describe`;
* ``widened``  → `automations.widened`;
* ``run``      → `automation_runner._ACTIONS[kind]`.

So a stored automation's config, its consent scope, its digest and its consent lines are exactly
what they were: no receipt changes because the registry exists (`tests/test_actions.py` pins the
digests). Each function refuses an action of another kind rather than silently doing that kind's
work: an executor is one action, never a dispatcher.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .. import automations as model


@dataclass(frozen=True)
class Executor:
    id: str
    #: A raw action → the stored action shape, or `automations.AutomationError`.
    validate: Callable[[object], dict]
    #: ``(config, pins)`` → the consent scope.
    scope: Callable[[dict, dict], dict]
    #: ``(scope, preview)`` → the consent lines.
    describe: Callable[..., list[str]]
    #: ``(receipt, scope)`` → every way the scope exceeds the receipt.
    widened: Callable[[dict | None, dict], list[str]]
    #: ``(run, config, pins, *, registry)`` → ``(outcome, reason)``.
    run: Callable[..., Awaitable[tuple[str, str]]]


def _adapter(kind: str) -> Executor:
    def _mine(action: object, what: str) -> None:
        got = action.get("kind") if isinstance(action, dict) else None
        if got != kind:
            raise model.AutomationError(f"{what} is a {got!r} action, not {kind!r}")

    def validate(raw: object) -> dict:
        _mine(raw, "this action")
        return model.validate_action(raw)

    def scope(config: dict, pins: dict) -> dict:
        _mine(config.get("action"), "this automation")
        return model.scope_of(config, pins)

    def describe(scope_: dict, preview: str | None = None) -> list[str]:
        if scope_.get("action") != kind:
            raise model.AutomationError(f"this scope is not a {kind!r} action's")
        return model.describe(scope_, preview)

    def widened(old: dict | None, new: dict) -> list[str]:
        if new.get("action") != kind:
            raise model.AutomationError(f"this scope is not a {kind!r} action's")
        return model.widened(old, new)

    async def run(run_: dict, config: dict, pins: dict, *, registry) -> tuple[str, str]:
        _mine(config.get("action"), "this automation")
        # At call time: the runner's import graph reaches the native runtime and the routes.
        from .. import automation_runner

        return await automation_runner._ACTIONS[kind](run_, config, pins, registry=registry)

    return Executor(
        id=kind, validate=validate, scope=scope, describe=describe, widened=widened, run=run
    )


#: The table, in the order the gallery and the wizard list it (today's `automations.ACTION_KINDS`).
EXECUTORS: dict[str, Executor] = {
    k: _adapter(k)
    for k in ("start_mission", "start_session", "send_to_session", "forge_review_loop", "tell_hq")
}
