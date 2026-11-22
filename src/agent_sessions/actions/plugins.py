"""Plugin actions (#2248 P2): the first NEW actions, stored as ``{kind: "plugin", ...}``.

A stored plugin action is::

    {"kind": "plugin", "plugin": "<id>", "version": 1, "inputs": {...}, "from_delivery": {...}?}

The five actions automations had before the registry keep their own kinds and exact shapes (their
receipts are pinned by `tests/test_actions.py`); ``plugin`` is ONE more kind in
`automations.ACTION_KINDS`, and this module is the authority for which plugins exist (`PLUGINS`,
the closed table of reviewed modules). `automations` and `automation_runner` call into here at
exactly the seams the five use: validate, message, pins and drift, scope, describe, widened, the
save check, the delivery fill and the run. Nothing else in them names a plugin.

**A plugin module** (``actions/<id>.py``) provides:

* ``ID``, ``VERSION``, ``WEBHOOK`` (a webhook delivery may start it), ``MESSAGE`` (it carries a
  P2a message: ``inputs.message`` is ``{text}`` or ``{template_id, values}``, and ``from_delivery``
  maps that template's own typed fields, as for the five), ``SECRET_REFUSAL`` (why a template with
  a secret is refused, for a message plugin) and ``TARGET_WORDS`` (what a changed target field is
  called in the consent dialog);
* ``validate_inputs(inputs, mapped)`` → the stored inputs, STRICT; ``mappable(inputs)`` → the
  input names a delivery may fill (a non-message plugin);
* ``pins(action)`` → what the run is bound to beyond the config (``None`` for nothing), and
  ``drift(stored, current)`` → why that changed, or ``""``;
* ``target(action, pins)`` (equality fields: the destination, the recipient, the folder) and
  ``data(action)`` (the other disclosed inputs), and ``lines(scope)`` → the consent lines for both;
* ``check_save(config)`` (raises `AutomationError`), ``fill(action, delivered)`` (a non-message
  plugin), and ``effect(run, config, pins)`` → ``(outcome, reason)``, run BLOCKING on one worker
  thread under the effect lock (`run`).

**Authority at the effect boundary.** `run` takes `automation_effect_lock.runner_sync` on ONE
worker thread, reads the automation's authority (`automation_runner._authority_outcome`), and
only then calls the plugin's ``effect``, which reads the saved configuration, the connector's or
the project's current state, and the automation's authority AGAIN immediately before the effect
(`final`). A cancelled run JOINS the worker through repeated cancellation (the `_tell_hq` pattern),
so the lock is held until the effect is over: a disable acknowledged without ``in_flight`` is never
followed by an effect, and an effect that happened is recorded as delivered, never as interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from types import ModuleType

from .. import automations as model
from .. import webhook_events

#: The stored contract version every plugin action carries. A higher one is refused at save and
#: read as unreadable (runs nothing), never half-read.
VERSION = 1
_ACTION_KEYS = {"kind", "plugin", "version", "inputs", "from_delivery"}
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _modules() -> dict[str, ModuleType]:
    # At call time: each module imports the stores and transports it needs.
    from . import call_webhook, publish_artifact, send_message, write_memory

    return {m.ID: m for m in (send_message, call_webhook, publish_artifact, write_memory)}


#: The closed table of plugin ids, in the gallery's order. A manifest whose executor is not one of
#: these is refused by the registry; a stored action naming another id is refused at save and runs
#: nothing.
PLUGINS = ("send_message", "call_webhook", "publish_artifact", "write_memory")


def module(plugin_id: object) -> ModuleType:
    mods = _modules()
    if not isinstance(plugin_id, str) or plugin_id not in mods:
        raise model.AutomationError(f"plugin must be one of {list(PLUGINS)}")
    return mods[plugin_id]


def is_plugin(action: dict) -> bool:
    return isinstance(action, dict) and action.get("kind") == "plugin"


def mod_of(action: dict) -> ModuleType:
    return module(action.get("plugin"))


# ---- validation ---------------------------------------------------------------------------------


def validate(raw: dict) -> dict:
    """A raw plugin action → its stored shape, STRICT (unknown keys anywhere refused)."""
    a = model._obj(raw, "a plugin action", _ACTION_KEYS, {"kind", "plugin", "version", "inputs"})
    pid = a["plugin"]
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        raise model.AutomationError("plugin must be an action id")
    mod = module(pid)
    version = a["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != VERSION:
        raise model.AutomationError(f"version must be {VERSION}")
    inputs = a["inputs"]
    if not isinstance(inputs, dict):
        raise model.AutomationError("inputs must be an object")
    raw_map = a.get("from_delivery")
    if mod.MESSAGE:
        if "message" not in inputs:
            raise model.AutomationError("inputs needs message")
        if not isinstance(inputs["message"], dict):
            raise model.AutomationError("message must be an object")
        # The P2a message rules, unchanged: text, or a template with fixed and delivered values
        # (never both for one field). `from_delivery` lives on the action, as the contract says.
        msg = model.validate_message(
            {**inputs["message"], **({"from_delivery": raw_map} if raw_map else {})}, "message"
        )
        mapped = msg.pop("from_delivery", {})
        clean = mod.validate_inputs({**inputs, "message": msg}, mapped)
    else:
        try:
            mapped = webhook_events.validate_mapping(raw_map)
        except webhook_events.EventError as e:
            raise model.AutomationError(f"from_delivery: {e}") from None
        clean = mod.validate_inputs(inputs, mapped)
        # `None`: any name the plugin's own validation accepted (it checked the mapping itself).
        allowed = mod.mappable(clean)
        bad = [] if allowed is None else sorted(set(mapped) - allowed)
        if bad:
            raise model.AutomationError(
                f"from_delivery: {bad} can't be filled from a delivery (only {sorted(allowed)})"
            )
    out = {"kind": "plugin", "plugin": pid, "version": VERSION, "inputs": clean}
    return {**out, "from_delivery": mapped} if mapped else out


def webhook_ok(action: dict) -> bool:
    return bool(mod_of(action).WEBHOOK)


def mapping_of(action: dict) -> dict:
    return action.get("from_delivery") or {}


def message_of(action: dict) -> dict:
    """A message plugin's message in the P2a shape (``from_delivery`` inside it, as the five keep
    it), so pins, the template checks, the content digest and the consent lines read it exactly as
    they read the five's. ``{}`` for a plugin without a message."""
    if not mod_of(action).MESSAGE:
        return {}
    msg = dict(action["inputs"]["message"])
    mapped = mapping_of(action)
    return {**msg, "from_delivery": mapped} if mapped else msg


def secret_refusal(action: dict) -> str:
    return mod_of(action).SECRET_REFUSAL


# ---- pins, scope, consent -----------------------------------------------------------------------


def pins(action: dict) -> dict | None:
    mod = mod_of(action)
    p = mod.pins(action)
    return None if p is None else {"of": mod.ID, **p}


def drift(stored: dict, current: dict) -> str:
    """Why a plugin's own pins differ, or ``""``. Only compared when either side has them."""
    s, c = stored.get("plugin"), current.get("plugin")
    if s == c:
        return ""
    if isinstance(c, dict) and c.get("missing"):
        return str(c["missing"])
    pid = (c or s or {}).get("of") if isinstance(c or s, dict) else None
    try:
        words = module(pid).drift(s or {}, c or {})
    except model.AutomationError:
        words = ""
    return words or "what this action is bound to changed since you approved it"


def target(action: dict, pins_: dict) -> dict:
    mod = mod_of(action)
    return {"plugin": mod.ID, "version": action["version"], **mod.target(action, pins_)}


def data(action: dict) -> dict:
    return mod_of(action).data(action)


def lines(scope: dict) -> list[str]:
    t = scope.get("target") or {}
    pid = t.get("plugin")
    try:
        mod = module(pid)
    except model.AutomationError:
        return [f"Runs the action {pid!r} · {json.dumps(t, sort_keys=True)}"]
    out = [f"Action: {mod.LABEL} (plugin {pid}, version {t.get('version')})"]
    out.extend(mod.lines(scope))
    return out


def target_changes(old: dict, new: dict) -> list[str]:
    """The consent dialog's words for a plugin target that changed: the field by name ("a
    different recipient") rather than "a new target". A different plugin is a different action."""
    if old.get("plugin") != new.get("plugin") or old.get("version") != new.get("version"):
        return ["a different action"]
    try:
        words = module(new.get("plugin")).TARGET_WORDS
    except model.AutomationError:
        return ["a new target"]
    out: list[str] = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key) or (key in old) != (key in new):
            w = words.get(key, "a new target")
            if w not in out:
                out.append(w)
    return out or ["a new target"]


def check_save(config: dict) -> None:
    mod_of(config["action"]).check_save(config)


def fill(action: dict, delivered: dict) -> dict:
    """The action with this run's delivered values filled in (exactly the mapped fields)."""
    mod = mod_of(action)
    if mod.MESSAGE:
        msg = dict(action["inputs"]["message"])
        msg["values"] = dict(sorted({**msg.get("values", {}), **delivered}.items()))
        out = {k: v for k, v in action.items() if k != "from_delivery"}
        return {**out, "inputs": {**action["inputs"], "message": msg}}
    filled = mod.fill(action, delivered)
    return {k: v for k, v in filled.items() if k != "from_delivery"}


# ---- the run ------------------------------------------------------------------------------------


def final(run: dict) -> tuple[str, str] | None:
    """The automation's authority, read again immediately before an effect: ``None`` to go on,
    else the outcome to record (`automation_runner._authority_outcome`)."""
    from .. import automation_runner as runner

    return runner._authority_outcome(run)


def delivered(run: dict, outcome: str, reason: str) -> tuple[str, str]:
    """The effect HAPPENED: anything failing afterwards is bookkeeping and never turns this run
    into a failure or an interruption (`automation_runner._DELIVERED`)."""
    from .. import automation_runner as runner

    runner._delivered(run["id"], outcome, reason)
    return outcome, reason


def _locked(run: dict, config: dict, pins_: dict) -> tuple[str, str]:
    from .. import automation_effect_lock as effect_lock
    from .. import automation_runner as runner

    with effect_lock.runner_sync(run["automation_id"]) as held:
        if not held:
            return "skipped", runner.LOCK_BUSY
        stop = runner._authority_outcome(run)
        if stop:
            return stop
        return mod_of(config["action"]).effect(run, config, pins_)


async def run(run_: dict, config: dict, pins_: dict, *, registry=None) -> tuple[str, str]:
    """Run a plugin action: the whole effect on ONE worker thread under the effect lock. On
    cancellation the worker is JOINED, however often we are cancelled meanwhile, before the
    cancellation goes on (`automation_runner._tell_hq`'s contract)."""
    task = asyncio.ensure_future(asyncio.to_thread(_locked, run_, config, pins_))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        raise


def run_async(coro) -> object:
    """Run one coroutine to completion on the CURRENT worker thread (its own event loop): an
    async transport's request stays inside the effect lock's critical section."""
    return asyncio.run(coro)
