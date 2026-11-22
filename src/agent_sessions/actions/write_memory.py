"""Write a memory note (#2248 P2): a short note into a project's memory, reviewed as an agent's is.

``inputs = {project_id, title, message}``: the project whose notebook takes the note, the note's
title (fixed), and its text — the P2a message: text, or a template with fixed values and typed
values from a webhook delivery. A delivery never chooses the project.

**The memory service's own path** (`memory_service.submit`, the call HQ's notes and capture use):
a worker principal for exactly that project's notebook, provenance ``agent`` with the origin
``automation:<id>:<run>`` (a receipt, so a run never files a note twice), and the store's own
rules: the notebook must be on, and BattleLab REVIEWS the note before other sessions see it — a
credential shape or a stored secret value holds it back in the project's quarantine for the
operator, never in the notes; the size and count caps apply. Nothing here writes a note file or
reaches the notebook any other way.

A template with a secret is refused at save and at the run (a note is read by other sessions).
Notes are hints for agents, never instructions; the note says it came from an automation.
"""

from __future__ import annotations

from .. import automations as model
from . import plugins

ID = "write_memory"
LABEL = "Write a memory note"
WEBHOOK = True
MESSAGE = True
#: Why a template with a secret is refused (the refusal's words, not a secret).
SECRET_REFUSAL = (
    "a template with a secret can't be a memory note: "  # noqa: S105
    "other sessions read the project's memory"
)
TARGET_WORDS = {"project_id": "a different project"}
_INPUTS = {"project_id", "title", "message"}
TITLE_MAX = 120


def _service():
    from .. import memory_service

    return memory_service


def validate_inputs(inputs: dict, mapped: dict) -> dict:
    model._obj(inputs, "a write_memory action's inputs", _INPUTS, _INPUTS)
    pid = inputs["project_id"]
    if not isinstance(pid, str) or not pid or len(pid) > 200:
        raise model.AutomationError("project_id must be a project id")
    title = " ".join(model._text(inputs["title"], "title", TITLE_MAX).split())
    if not title:
        raise model.AutomationError("a memory note needs a title")
    return {"project_id": pid, "title": title, "message": inputs["message"]}


def mappable(_inputs: dict) -> set[str]:  # pragma: no cover — a message plugin maps its template
    return set()


def check_save(config: dict) -> None:
    from .. import projects

    pid = config["action"]["inputs"]["project_id"]
    try:
        entity = projects.load().get(pid)
    except Exception as e:  # noqa: BLE001 — unreadable projects: refuse, never guess
        raise model.AutomationError(
            f"the projects can't be read ({type(e).__name__})", 503
        ) from None
    if entity is None or getattr(entity, "archived", False):
        raise model.AutomationError("that project doesn't exist (or is archived)")
    service = _service()
    try:
        rows = service.notebooks(service.worker([pid]))
    except Exception as e:  # noqa: BLE001 — the memory service's own refusal or an unreadable store
        raise model.AutomationError(
            f"the project's memory can't be checked now ({e})", 503
        ) from None
    if not any(r.get("id") == pid and r.get("enabled") for r in rows):
        raise model.AutomationError(
            "memory is off for that project: switch it on in Library → Memory first"
        )


def pins(_action: dict) -> None:
    return None


def drift(_stored: dict, _current: dict) -> str:  # pragma: no cover — no pins
    return ""


def target(action: dict, _pins: dict) -> dict:
    return {"project_id": action["inputs"]["project_id"]}


def data(action: dict) -> dict:
    return {"title": action["inputs"]["title"]}


def lines(scope: dict) -> list[str]:
    t = scope.get("target") or {}
    d = scope.get("inputs") or {}
    return [
        f"Writes a note titled “{d.get('title')}” into project {t.get('project_id')}'s memory",
        "BattleLab reviews it before other sessions see it: a credential or a stored secret holds "
        "it back for you; the notebook must be on",
        "Other agents read notes as hints, never as instructions",
    ]


def effect(run: dict, config: dict, pins_: dict) -> tuple[str, str]:
    """BLOCKING, under the effect lock, after the authority read."""
    from .. import automation_runner as runner
    from .. import automations_store as store

    service = _service()
    inp = config["action"]["inputs"]
    pid, title = inp["project_id"], inp["title"]
    try:
        text, rendered = runner._render_plain(config, pins_, carried_refusal=SECRET_REFUSAL)
    except runner.RunRefused as e:
        return "refused", e.detail
    if not text.strip():
        return "refused", "the note is empty; nothing was filed"
    store.set_inputs(run["id"], {"project": pid, "title": title, "note": rendered["message"]})
    stop = plugins.final(run)
    if stop:
        return stop
    aid = run["automation_id"]
    try:
        res = service.submit(
            service.worker([pid]),
            pid,
            title=title,
            body=text,
            origin=f"automation:{aid}:{run['id']}",
            kind="finding",
            source=f"automation:{aid}",
            source_kind="agent",
        )
    except service.memory.MemoryStoreError as e:
        status = getattr(e, "status", 500)
        if status < 500:
            return "refused", f"project memory refused it ({e}); nothing was filed"
        return "failed", f"project memory is unavailable ({e}); nothing was filed"
    state = res.get("state")
    if state == "saved":
        return plugins.delivered(run, "ok", f"filed in project memory as {res.get('slug')}")
    if state == "quarantined":
        # The review held it back: it is in the quarantine for the operator, not in the notes.
        return plugins.delivered(
            run,
            "refused",
            "held back by BattleLab's review (it looks like a credential or a stored secret); "
            "it is in the project's memory quarantine for you, and no session sees it",
        )
    if state == "forgotten":
        return "refused", "a note from this run was forgotten earlier; it is not filed again"
    return "refused", "project memory refused it (too large, or the notebook is full)"
