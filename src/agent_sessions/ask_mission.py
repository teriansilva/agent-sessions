"""HQ proposes a DRAFT mission (#1950): the card that makes "plan me a mission" work in one chat.

HQ picks the project (by the index `list_projects` returned this turn) and writes the instruction;
the card is ``mission.create``. The operator's tap runs ``POST /api/missions`` AS the operator
(`asgi_call`), the very route the Missions rail's "New mission" form posts to. The server creates a
**draft** and writes its objectives and plan from the project's playbook in the background, as it
always does.

* **A draft starts nothing.** Approve, Start and launch stay the operator's, in the console
  (**Begin**). This operation is the only mission write HQ has, and its route is pinned to
  ``POST /api/missions`` with no ``cwd``: the server resolves the path from the project.
* **Checked when staged.** The project must exist and not be archived, else the proposal is refused
  right there (the route would refuse it too) and HQ gets the refusal as its tool result.
* **Bound.** The project's name and archive state are the card's facts; a project renamed or
  archived before the tap makes the card ``stale``.
"""

from __future__ import annotations

GROUP = "mission"
#: What the card can show whole: the summary sentence is capped (`ask_operations.SUMMARY_MAX`) and
#: a card that would be cut is refused, so the instruction is bounded well inside that.
INSTRUCTION_MAX = 240


def instruction(v: object) -> str:
    from .ask_operations import Refused

    if not isinstance(v, str):
        raise Refused("instruction must be a string")
    text = " ".join(v.split())
    if not text:
        raise Refused("instruction must not be empty")
    if len(text) > INSTRUCTION_MAX:
        raise Refused(
            f"instruction is longer than {INSTRUCTION_MAX} characters: shorten it; the operator "
            "adds detail in the mission itself"
        )
    return text


def _body(target: str, f: dict, facts: dict) -> tuple[str, str, dict]:
    # `project_id` is the server's own id for a project HQ named by index; `cwd` is never sent.
    return "POST", "/api/missions", {"instruction": f["instruction"], "project_id": target}


def _describe(f: dict, facts: dict) -> str:
    return (
        f"Creates a draft mission: \"{f['instruction']}\". Its objectives and plan are written "
        "from the project's playbook. Nothing runs until you press Begin."
    )


def _outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    from .ask_operations import DONE, FAILED

    if not 200 <= status < 300 or not isinstance(body, dict):
        return FAILED, ""
    title = str(body.get("title") or "")[:120]
    return DONE, (
        f"Draft mission created ({title}). Its plan is being written; nothing runs until you "
        "press Begin."
    )


def _failure(status: int, detail: str, facts: dict) -> str:
    return f"Not created: {detail or 'refused'}. Nothing was written."


def mission_ops() -> tuple:
    from .ask_operations import Operation

    return (
        Operation(
            "mission.create",
            GROUP,
            "create",
            "Create a draft mission",
            "Create draft",
            "reversible",
            "project",
            ("instruction",),
            _body,
            _describe,
            _outcome,
            _failure,
        ),
    )


async def _row(target: str, app, operator) -> dict | None:
    from . import asgi_call

    r = await asgi_call.call(
        app, operator, "GET", "/api/projects", None, query="include_archived=1"
    )
    rows = (r.body or {}).get("projects") if isinstance(r.body, dict) else None
    return next((p for p in rows or [] if isinstance(p, dict) and p.get("id") == target), None)


async def refusal(target: str, app, operator) -> str:
    """Why the route would refuse a draft in this project, or "" (the dry run at staging): the
    project is gone, archived, or has no folder for the draft's path (`_resolve_cwd`)."""
    p = await _row(target, app, operator)
    if p is None:
        return "that project is no longer available"
    if p.get("archived"):
        return "that project is archived"
    if not [f for f in p.get("folders") or [] if isinstance(f, str) and f]:
        return "that project has no folder to work in: the operator adds one in the project"
    return ""


async def facts(target: str, app, operator) -> dict | None:
    """The project the draft goes into, as the operator sees it: its name and archive state.
    ``None`` (nothing staged) for a project that is gone or archived."""
    from . import ask_operations

    p = await _row(target, app, operator)
    if p is None or p.get("archived"):
        return None
    return {"name": str(p.get("name") or "")[: ask_operations.TITLE_MAX], "archived": False}
