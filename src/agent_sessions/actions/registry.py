"""The action registry (#2248 P1): the bundled manifests, each bound to its reviewed executor.

P1 is an ADAPTER over today's automations, and adds no capability. The automation model keeps its
own lists (`automations.ACTION_KINDS`, `WEBHOOK_ACTIONS`) and the runner its table
(`automation_runner._ACTIONS`); they are the authority at save and at run, exactly as before. The
registry must AGREE with them, and `tests/test_actions.py` fails on any difference:

* `kinds()` == `automations.ACTION_KINDS` == `automation_runner._ACTIONS`, in order;
* `webhook_kinds()` (the actions whose manifest lists the ``webhook`` trigger) ==
  `automations.WEBHOOK_ACTIONS`.

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
    return tuple(a.manifest.id for a in actions())


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


def _forge() -> tuple[bool, str]:
    from .. import prefs

    try:
        cfg = prefs.public_forge()
    except Exception:  # noqa: BLE001 — unreadable settings: not set up
        return False, "the forge settings could not be read"
    if not cfg.get("configured"):
        return False, "needs a Forgejo set up in Settings → Forge"
    if cfg.get("kind") not in ("forgejo", "gitea"):
        return False, "the forge in Settings → Forge is not a Forgejo"
    return True, "Forgejo is set up"


#: The requirements this read can check now. The rest depend on a choice made in the wizard (which
#: project, which session, which reviewer) and are checked there and at save.
_CHECKS = {"api_agent": _api_agent, "forge": _forge}


def requirement(rid: str) -> dict:
    words = mf.REQUIREMENTS[rid]
    check = _CHECKS.get(rid)
    if check is None:
        return {"id": rid, "words": words, "ok": None, "detail": ""}
    try:
        ok, detail = check()
    except Exception:  # noqa: BLE001 — a check that fails says so, never "ready"
        ok, detail = False, "could not be checked right now"
    return {"id": rid, "words": words, "ok": ok, "detail": detail}


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
        kind = ((r.get("config") or {}).get("action") or {}).get("kind")
        if isinstance(kind, str):
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
