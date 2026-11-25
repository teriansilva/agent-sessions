"""The action registry (#2248 P1): the bundled manifests, each bound to its reviewed executor.

P1 is an ADAPTER over today's automations, and adds no capability. The automation model keeps its
own lists (`automations.ACTION_KINDS`, `WEBHOOK_ACTIONS`) and the runner its table
(`automation_runner._ACTIONS`); they are the authority at save and at run, exactly as before. The
registry must AGREE with them, and `tests/test_actions.py` fails on any difference:

* `builtin_kinds()` + ``("plugin",)`` == `automations.ACTION_KINDS` ==
  `automation_runner._ACTIONS`, in order;
* `plugin_ids()` == `plugins.PLUGINS`: the plugin actions (#2248 P2) are ONE stored kind,
  ``plugin``, and the plugin table is the authority for which exist;
* the built-in actions whose manifest lists the ``webhook`` trigger, plus ``plugin``, ==
  `automations.WEBHOOK_ACTIONS`; a plugin's manifest lists ``webhook`` exactly when its module
  says a delivery may start it.

Choosing a parity test over making those lists views of this one keeps the save and run paths
byte-for-byte what they were: a manifest that failed to load could never take automations down.

`gallery()` is Library → Actions' read (`GET /api/actions`): the manifest facts, what each action
needs and whether that holds now, and how many automations use it. It changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache

from . import manifest as mf
from .executors import EXECUTORS, Executor

log = logging.getLogger(__name__)


class RegistryError(ValueError):
    """The bundled actions don't fit the executor table: a release defect."""


@dataclass(frozen=True)
class Action:
    manifest: mf.ActionManifest
    executor: Executor


def build(manifests: list[mf.ActionManifest]) -> tuple[Action, ...]:
    """Bind each manifest to its executor. An executor outside the closed table, two manifests for
    one executor, or an executor no manifest describes is refused."""
    out: dict[str, Action] = {}
    for m in manifests:
        ex = EXECUTORS.get(m.executor)
        if ex is None:
            raise RegistryError(f"action {m.id!r}: unknown executor {m.executor!r}")
        if m.id != m.executor:
            # P1: one manifest per executor, under the executor's own name (the stored action's
            # `kind`), so nothing that reads a stored action needs to translate.
            raise RegistryError(f"action {m.id!r} must be named for its executor {m.executor!r}")
        if m.executor in out:
            raise RegistryError(f"two manifests describe executor {m.executor!r}")
        out[m.executor] = Action(m, ex)
    missing = [k for k in EXECUTORS if k not in out]
    if missing:
        raise RegistryError(f"no manifest describes executor {missing[0]!r}")
    return tuple(out[k] for k in EXECUTORS)


@cache
def actions() -> tuple[Action, ...]:
    """Every action, in the executor table's order."""
    return build(mf.bundled())


def get(action_id: str) -> Action | None:
    return next((a for a in actions() if a.manifest.id == action_id), None)


def kinds() -> tuple[str, ...]:
    """Every action id: the built-in kinds, then the plugin ids."""
    return tuple(a.manifest.id for a in actions())


def builtin_kinds() -> tuple[str, ...]:
    from .executors import BUILTIN

    return tuple(k for k in kinds() if k in BUILTIN)


def plugin_ids() -> tuple[str, ...]:
    from .executors import BUILTIN

    return tuple(k for k in kinds() if k not in BUILTIN)


def action_id(action: object) -> str | None:
    """The gallery id of a stored action: its kind, or its plugin id for a plugin action."""
    if not isinstance(action, dict):
        return None
    kind = action.get("kind")
    if kind == "plugin":
        pid = action.get("plugin")
        return pid if isinstance(pid, str) else None
    return kind if isinstance(kind, str) else None


def webhook_kinds() -> tuple[str, ...]:
    return tuple(a.manifest.id for a in actions() if "webhook" in a.manifest.triggers)


# ---- the gallery's read -----------------------------------------------------------------------


def _api_agent() -> tuple[bool, str]:
    """Whether some API agent may start unattended now: the same `target_state` that refuses an
    automation's save and run, asked of every engine (never naming one)."""
    from .. import automation_runner, engines, mission_structured

    for eid in engines.engine_ids():
        prov = engines.get(eid)
        try:
            if prov is None or not mission_structured.is_api(prov):
                continue
            if automation_runner.target_state(eid)[0]:
                return True, "an API agent is ready"
        except Exception as e:  # noqa: BLE001 — an engine that cannot answer is not ready
            log.debug("actions gallery: %s could not answer (%s)", eid, type(e).__name__)
    return False, "needs an API agent that can start unattended (Settings → Agents)"


def _messenger() -> tuple[bool, str]:
    """Whether some messenger connector can send now: switched on, tested, with recipients bound
    to its bot (`connectors.store.sendable`, the same test the send's token fetch applies)."""
    from ..connectors import store

    try:
        doc = store.read()
    except Exception:  # noqa: BLE001 — unreadable settings: not set up
        return False, "the connector settings could not be read"
    if any(store.MESSENGER_KEYS <= set(i) and store.sendable(i) for i in doc["instances"]):
        return True, "a messenger can send"
    return False, "needs Telegram, Matrix or mail set up to send"


def _artifacts() -> tuple[bool, str]:
    from .. import artifact_store

    try:
        on = artifact_store.enabled()
    except Exception:  # noqa: BLE001
        return False, "the artifact settings could not be read"
    return (True, "artifacts are on") if on else (False, "needs artifacts switched on")


def _containment() -> tuple[bool, str]:
    from . import run_local

    why = run_local.containment_problem()
    return (False, why) if why else (True, "commands run in their own process scope")


#: The requirements this read can check now. The rest depend on a choice made in the wizard (which
#: project, which session) and are checked there and at save.
_CHECKS = {
    "api_agent": _api_agent,
    "messenger": _messenger,
    "artifacts": _artifacts,
    "containment": _containment,
}

#: Where a requirement that does not hold is set up: a Settings section id (`settingsTabs.ts`).
SETUP = {
    "api_agent": "agents",
    "messenger": "ai-hq",
    "artifacts": "artifacts",
}


def requirement(rid: str) -> dict:
    words = mf.REQUIREMENTS[rid]
    check = _CHECKS.get(rid)
    setup = SETUP.get(rid)
    extra = {"setup": setup} if setup else {}
    if check is None:
        return {"id": rid, "words": words, "ok": None, "detail": "", **extra}
    try:
        ok, detail = check()
    except Exception:  # noqa: BLE001 — a check that fails says so, never "ready"
        ok, detail = False, "could not be checked right now"
    return {"id": rid, "words": words, "ok": ok, "detail": detail, **extra}


def _used_by() -> dict[str, int] | None:
    """How many automations use each action, or ``None`` when the store can't be read."""
    from .. import automations_store as store

    try:
        rows = store.list_all()
    except Exception as e:  # noqa: BLE001 — the gallery still shows the actions
        log.info("actions gallery: automations unreadable (%s)", type(e).__name__)
        return None
    out: dict[str, int] = {}
    for r in rows:
        kind = action_id((r.get("config") or {}).get("action"))
        if kind is not None:
            out[kind] = out.get(kind, 0) + 1
    return out


def gallery() -> dict:
    """Library → Actions: every action's manifest facts, its requirements now and its use."""
    used = _used_by()
    checked: dict[str, dict] = {}
    rows = []
    for a in actions():
        m = a.manifest
        reqs = []
        for rid in m.requires:
            if rid not in checked:
                checked[rid] = requirement(rid)
            reqs.append(checked[rid])
        rows.append(
            {
                **m.public(),
                "requires": reqs,
                "available": all(r["ok"] is not False for r in reqs),
                "used_by": None if used is None else used.get(m.id, 0),
            }
        )
    return {
        "actions": rows,
        "categories": [{"id": k, "label": v} for k, v in mf.CATEGORIES.items()],
        "effects": [
            {"id": k, "level": lv, "label": words} for k, (lv, words) in mf.EFFECTS.items()
        ],
    }
