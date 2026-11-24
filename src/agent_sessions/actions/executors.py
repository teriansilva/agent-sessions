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

**Plugin executors (#2248 P2).** The plugin actions (`plugins.PLUGINS`) are stored as ONE kind,
``plugin``, and each has its own executor here, named by its plugin id. Its five functions go
through the same `automations` / `automation_runner` entry points as the five (whose ``plugin``
branch calls the plugin's reviewed module), and refuse an action of another plugin.
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


def _plugin_executor(pid: str) -> Executor:
    """A plugin action's executor (#2248 P2): the ``plugin`` kind, this plugin only."""

    def _mine(action: object, what: str) -> None:
        a = action if isinstance(action, dict) else {}
        if a.get("kind") != "plugin" or a.get("plugin") != pid:
            raise model.AutomationError(f"{what} is not a {pid!r} action")

    def _scope_mine(scope_: dict) -> None:
        if scope_.get("action") != "plugin" or (scope_.get("target") or {}).get("plugin") != pid:
            raise model.AutomationError(f"this scope is not a {pid!r} action's")

    def validate(raw: object) -> dict:
        _mine(raw, "this action")
        return model.validate_action(raw)

    def scope(config: dict, pins: dict) -> dict:
        _mine(config.get("action"), "this automation")
        return model.scope_of(config, pins)

    def describe(scope_: dict, preview: str | None = None) -> list[str]:
        _scope_mine(scope_)
        return model.describe(scope_, preview)

    def widened(old: dict | None, new: dict) -> list[str]:
        _scope_mine(new)
        return model.widened(old, new)

    async def run(run_: dict, config: dict, pins: dict, *, registry) -> tuple[str, str]:
        _mine(config.get("action"), "this automation")
        from .. import automation_runner

        return await automation_runner._ACTIONS["plugin"](run_, config, pins, registry=registry)

    return Executor(
        id=pid, validate=validate, scope=scope, describe=describe, widened=widened, run=run
    )


#: The actions automations had before the registry (their own stored kinds). The review loop
#: (`forge_review_loop`, #2202) was one of them until it was retired (#2222).
BUILTIN = ("start_mission", "start_session", "send_to_session", "tell_hq")


def _table() -> dict[str, Executor]:
    from .plugins import PLUGINS

    return {
        **{k: _adapter(k) for k in BUILTIN},
        **{p: _plugin_executor(p) for p in PLUGINS},
    }


#: The table, in the order the gallery and the wizard list it: the built-in ones, then the plugins.
EXECUTORS: dict[str, Executor] = _table()
