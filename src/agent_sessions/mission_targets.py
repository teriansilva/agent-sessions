"""Where a mission may run: its projects and its agents (#2152, D5).

The one-agent planner (`mission_plan`, #893/#967) is retired: HQ plans every mission. These helpers
outlived it because other callers still need the same answers:

* :func:`project_options` — the projects HQ is shown, capped (`ask_orchestrator`'s
  `list_projects`); :func:`resolve_chosen_project` — one project by id, uncapped
  (`ask_proposals`); :func:`picker_project_options` — the operator's list, read with one snapshot
  (the legacy plan card's names).
* :func:`engine_options` / :func:`engine_refusal` / :func:`engine_unavailable` /
  :func:`picker_lists` — the one mission admission (`mission_structured.admits`, #1902): which
  agents a mission may dispatch into, adopt or spawn, and why the others are left out
  (`routes/missions.py`).

They moved here unchanged. Nothing in this module calls a model or writes a plan.
"""

from __future__ import annotations

import logging

from . import engines, projects

log = logging.getLogger(__name__)

#: How many of each the model is shown. A menu nobody reads is a menu that gets a random index.
MAX_PROJECTS = 40
MAX_ENGINES = 12


def project_options() -> list[dict]:
    """The projects a mission may be planned into, in a stable order.

    Archived projects are excluded and a project with no folder is excluded: both would produce a
    plan that cannot be dispatched, and offering a choice that always fails is worse than offering
    fewer choices.
    """
    try:
        loaded = projects.load()
    except Exception:  # noqa: BLE001 — an unreadable projects file is "no options", not a 500
        log.debug("project options unavailable")
        return []
    out: list[dict] = []
    for entity in sorted(loaded.values(), key=lambda e: (e.name.lower(), e.id)):
        if entity.archived:
            continue
        cwd = entity.default_folder or (entity.folders[0] if entity.folders else "")
        if not cwd:
            continue
        out.append({"id": entity.id, "name": entity.name, "cwd": cwd})
    return out[:MAX_PROJECTS]


def resolve_chosen_project(
    project_id: str, *, index: dict | None = None
) -> tuple[dict | None, str]:
    """`(project, "")` for the mission's chosen project, or `(None, reason)` (#967).

    Resolved from the PROJECT STORE by id — not from :func:`project_options`, whose list is capped
    at :data:`MAX_PROJECTS`, so a chosen project that sorts past the cap is still the one used.
    The rules match the option list's: archived and folderless projects cannot be planned into.

    `index` is a store already read by the caller — the write boundary passes the one yielded by
    `projects.locked_index()`, so the check and the plan commit happen under the same lock.
    """
    try:
        entity = (projects.load() if index is None else index).get(project_id)
    except Exception:  # noqa: BLE001 — unreadable is a reason, not a crash
        return None, "the chosen project could not be read"
    if entity is None:
        return None, "the chosen project no longer exists"
    if entity.archived:
        return None, "the chosen project is archived"
    cwd = entity.default_folder or (entity.folders[0] if entity.folders else "")
    if not cwd:
        return None, "the chosen project has no folder to work in"
    return {"id": entity.id, "name": entity.name, "cwd": cwd}, ""


def picker_project_options() -> list[dict]:
    """Every project the OPERATOR may pick, uncapped and read with ONE snapshot.

    Not :func:`project_options`, which is what the MODEL is shown and is capped at
    :data:`MAX_PROJECTS`. A project is listed exactly when :func:`resolve_chosen_project` resolves
    it against that single read: it exists, is not archived, and has a folder to work in. Same
    order as the model's list. Today it names the project of a legacy one-agent plan on its
    read-only card (#2152, D2).
    """
    try:
        loaded = projects.load()
    except Exception:  # noqa: BLE001 — an unreadable projects file is "no options", not a 500
        log.debug("picker project options unavailable")
        return []
    out: list[dict] = []
    for entity in sorted(loaded.values(), key=lambda e: (e.name.lower(), e.id)):
        project, _why = resolve_chosen_project(entity.id, index=loaded)
        if project is not None:
            out.append(project)
    return out


def engine_options() -> list[dict]:
    """The engines that can actually be dispatched into: only mission-ready API clients (#1902).

    Built through `mission_structured.admits`, the one mission admission. Applying it HERE, when
    the list is built, is what makes "a terminal client is never a mission target" a property of
    the offer rather than a refusal after the fact. An empty list is an install with no
    mission-ready API client.
    """
    from . import mission_structured

    out: list[dict] = []
    for prov in engines.all_providers():
        # ONE admission (#1898, #1902): an API client needs its kind's mission capability AND
        # readiness; a console client is never admitted. `mission_structured.admits` is that rule.
        admitted, _why = mission_structured.admits(prov)
        if not admitted:
            continue
        out.append({"id": prov.engine_id, "label": prov.engine_id})
    return out[:MAX_ENGINES]


def engine_refusal(engine_id: str) -> str:
    """Why ``engine_id`` is not in :func:`engine_options`, in the operator's words (#1902): a
    terminal client's words name its family's API client to use, or say none exists yet. For the
    refusal a route shows; may probe readiness, so call it off the loop."""
    from . import mission_structured

    removed = engines.removed_reason(engine_id) if engine_id else None
    prov = None if removed else (engines.get(engine_id) if engine_id else None)
    if prov is None:
        return removed or "it is not an available agent"
    if not mission_structured.is_api(prov):
        return mission_structured.console_refusal(prov)
    try:
        _ok, why = mission_structured.admits(prov)
    except Exception:  # noqa: BLE001 — a client that can't answer is not admitted
        why = None
    return why or mission_structured.NOT_MISSION_READY


def engine_unavailable() -> list[dict]:
    """The agents :func:`engine_options` leaves out, each with the server's reason (#1577).

    DISPLAY ONLY. The picker showed two agents out of nine and said nothing about the rest, so the
    operator could not tell "not supported for missions yet" from "missing". Nothing here is a
    choice: every dispatch, adopt and spawn path keeps validating against `engine_options()`, and
    the model is never shown this list. Non-agent engines (a shell) are not listed at all.
    """
    out: list[dict] = []
    for prov in engines.all_providers():
        if not engines.is_agent(prov):
            continue
        from . import mission_structured

        ok, why = mission_structured.admits(prov)
        if ok:
            continue
        # A terminal client's words name the API client to use instead, or say none exists yet.
        words = why or ""
        if not mission_structured.is_api(prov):
            words = mission_structured.console_refusal(prov)
        out.append(
            {"id": prov.engine_id, "label": prov.engine_id, "reason": words, "detail": why or ""}
        )
    return out[:MAX_ENGINES]


def picker_lists() -> dict:
    """The lists a plan card offers: projects, agents, and the agents left out with their reasons.
    Blocking (a client's readiness may be probed, and #1902's refusal words ask the family's
    default client): call it OFF the loop."""
    return {
        "project_options": picker_project_options(),
        "engine_options": engine_options(),
        "engine_unavailable": engine_unavailable(),
    }
