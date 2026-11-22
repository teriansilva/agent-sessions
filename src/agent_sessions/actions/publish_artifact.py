"""Publish an artifact (#2248 P2): a file or folder from a project, as a private artifact.

``inputs = {project_id, path, title, why}``: a project, a path RELATIVE to its folder (fixed at
save: never absolute, never ``..``, never a hidden segment), and the artifact's title and one
line saying why — fixed, or (on a webhook) typed values from the delivery. A delivery never
chooses the project or the path.

**The artifact store's own publish path** (`artifact_store.publish`), with every rule it applies
to an agent's publish: the session boundary (here the project's folders, inside the roots and
outside `folder_exclusions`), no symlink, no hidden or key file, and **a file with a secret in it
refuses the whole publish**. Its origin is the automation (``engine="automation"``, session
``automation:<id>``), never an agent session. The project's folder is pinned at consent (a
repointed project is a drift); before the effect it is resolved again, checked against the
boundary (fail closed), and the item must still be inside it. Artifacts must be on for the project
(`artifact_store.effective_mode`).

**Private.** Nothing here shares. A later run publishes a new revision behind the same link only
while that artifact is still private; once the operator has shared it publicly, the next run
starts a new, private artifact instead, so an automation never puts new content behind a public
link. That decision is the STORE's, made inside the transaction that appends the revision
(`artifact_store.publish(private_only=True)`): a share landing after this module's snapshot and
before the commit can't slip a revision behind the public link. The run records the artifact's
link (its output for a later step).
"""

from __future__ import annotations

import os

from .. import automations as model
from . import plugins

ID = "publish_artifact"
LABEL = "Publish an artifact"
WEBHOOK = True
MESSAGE = False
SECRET_REFUSAL = ""
TARGET_WORDS = {
    "project_id": "a different project",
    "folder": "the project's folder changed",
    "path": "a different file or folder",
}
_INPUTS = {"project_id", "path", "title", "why"}
_MAPPABLE = {"title", "why"}
PATH_MAX = 1024


def _limits() -> tuple[int, int]:
    from .. import mission_shelf

    return mission_shelf.TITLE_MAX, mission_shelf.WHY_MAX


def _line(raw: object, what: str, cap: int) -> str:
    text = model._text(raw, what, cap)
    return " ".join(text.split())


def normalize_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > PATH_MAX or "\x00" in raw:
        raise model.AutomationError("path must name a file or folder in the project")
    p = raw.strip().replace("\\", "/")
    if p.startswith("/") or p.startswith("~"):
        raise model.AutomationError("path is relative to the project's folder, never absolute")
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if not parts:
        raise model.AutomationError("path must name a file or folder in the project, not all of it")
    if any(s == ".." for s in parts):
        raise model.AutomationError("path must stay inside the project's folder (no ..)")
    if any(s.startswith(".") for s in parts):
        raise model.AutomationError("path can't be a hidden file or folder")
    model._text("/".join(parts), "path", PATH_MAX)
    return "/".join(parts)


# ---- validation and save ------------------------------------------------------------------------


def validate_inputs(inputs: dict, mapped: dict) -> dict:
    model._obj(inputs, "a publish_artifact action's inputs", _INPUTS, {"project_id", "path"})
    pid = inputs["project_id"]
    if not isinstance(pid, str) or not pid or len(pid) > 200:
        raise model.AutomationError("project_id must be a project id")
    bad = sorted(set(mapped) - _MAPPABLE)
    if bad:
        raise model.AutomationError(
            f"from_delivery: only title and why can come from a delivery, not {bad}"
        )
    title_max, why_max = _limits()
    out: dict = {"project_id": pid, "path": normalize_path(inputs["path"])}
    for key, cap in (("title", title_max), ("why", why_max)):
        # An empty fixed value beside a mapped one is "not fixed" (the wizard sends it so).
        if key in inputs and not (key in mapped and inputs[key] == ""):
            if key in mapped:
                raise model.AutomationError(
                    f"{key} has a fixed value and a value from the delivery"
                )
            out[key] = _line(inputs[key], key, cap)
    if not out.get("title") and "title" not in mapped:
        raise model.AutomationError("an artifact needs a title (fixed, or from the delivery)")
    out.setdefault("title", "")
    out.setdefault("why", "")
    return out


def mappable(_inputs: dict) -> None:
    return None  # checked by `validate_inputs`: title and why only


def _item(real: str, path: str) -> str:
    return os.path.normpath(os.path.join(real, path))


def _item_problem(real: str, path: str) -> str | None:
    """Why the item can't be published from ``real`` now, or None. Fails closed."""
    import stat as _stat

    from .. import automation_runner as runner
    from .. import project_dirs

    if not runner.scope_ok(real):
        return "the project's folder is outside your project folders"
    item = _item(real, path)
    try:
        st = os.lstat(item)
    except FileNotFoundError:
        return f"{path} does not exist in the project's folder"
    except OSError:
        return f"{path} can't be read"
    if _stat.S_ISLNK(st.st_mode):
        return f"{path} is a symbolic link, which is never published"
    if not project_dirs.path_within(os.path.realpath(item), real):
        return f"{path} is outside the project's folder"
    if not runner.scope_ok(os.path.realpath(item)):
        return f"{path} is outside your project folders"
    return None


def check_save(config: dict) -> None:
    from .. import artifact_store

    inp = config["action"]["inputs"]
    try:
        cwd = model.resolve_mission_cwd(inp["project_id"])
    except model.PinsUnavailable as e:
        raise model.AutomationError(str(e), status=503) from None
    if cwd.get("missing"):
        raise model.AutomationError(str(cwd["missing"]))
    why = _item_problem(cwd["real"], inp["path"])
    if why:
        raise model.AutomationError(why)
    if not artifact_store.enabled():
        raise model.AutomationError("artifacts are switched off in Settings → Artifacts")


# ---- pins, scope, consent -----------------------------------------------------------------------


def pins(action: dict) -> dict:
    """The project's folder as approved (its identity): a repointed or replaced folder is a drift.
    The file itself is not pinned: publishing its latest content is the point."""
    cwd = model.resolve_mission_cwd(action["inputs"]["project_id"])
    if cwd.get("missing"):
        return {"missing": str(cwd["missing"])}
    return {"cwd": cwd}


def drift(_stored: dict, _current: dict) -> str:
    return "the project's folder changed since you approved this automation"


def target(action: dict, pins_: dict) -> dict:
    inp = action["inputs"]
    cwd = (pins_.get("plugin") or {}).get("cwd") or {}
    return {"project_id": inp["project_id"], "folder": cwd.get("real"), "path": inp["path"]}


def data(action: dict) -> dict:
    inp = action["inputs"]
    return {"title": inp["title"], "why": inp["why"]}


def lines(scope: dict) -> list[str]:
    t = scope.get("target") or {}
    d = scope.get("inputs") or {}
    out = [
        f"Publishes {t.get('path')} from project {t.get('project_id')}, in {t.get('folder')}",
        "As a PRIVATE artifact: only you can open it until you share it yourself",
        f"Title: {d.get('title')}" if d.get("title") else "Title: from the delivery",
        f"Why: {d.get('why')}" if d.get("why") else "Why: (none, unless the delivery gives one)",
        "A file with a secret in it, a hidden or key file, or a symbolic link refuses the publish",
        "Each run adds a revision behind the same link while it is private; once you share it, "
        "the next run starts a new private artifact",
    ]
    return out


def fill(action: dict, delivered: dict) -> dict:
    inp = {**action["inputs"]}
    title_max, why_max = _limits()
    for key, cap in (("title", title_max), ("why", why_max)):
        if key in delivered:
            inp[key] = " ".join(str(delivered[key]).split())[:cap]
    return {**action, "inputs": inp}


# ---- the run ------------------------------------------------------------------------------------


def effect(run: dict, config: dict, pins_: dict) -> tuple[str, str]:
    """BLOCKING, under the effect lock, after the authority read."""
    from .. import artifact_store, artifacts_mcp
    from .. import automations_store as store

    inp = config["action"]["inputs"]
    pinned = (pins_.get("plugin") or {}).get("cwd") or {}
    try:
        now = model.resolve_mission_cwd(inp["project_id"])
    except model.PinsUnavailable as e:
        return "refused", f"the project could not be resolved ({e}); nothing was published"
    if now != pinned or not now.get("real"):
        return (
            "refused",
            "the project's folder changed since you approved this; nothing was published",
        )
    real = now["real"]
    why = _item_problem(real, inp["path"])
    if why:
        return "refused", f"{why}; nothing was published"
    key = f"automation:{run['automation_id']}"
    mode = artifact_store.effective_mode([key], cwd=real)
    if mode.mode == "off":
        return "refused", "artifacts are off for this project; nothing was published"
    title = inp["title"].strip()
    if not title:
        return "refused", "the artifact has no title; nothing was published"
    # A new revision behind the same link only while that artifact is still PRIVATE.
    previous = None
    try:
        items = artifact_store.list_artifacts(session=key, limit=1)["items"]
    except Exception:  # noqa: BLE001 — unreadable list: start a new artifact, never guess
        items = []
    if items and items[0].get("visibility") == "private":
        previous = items[0]["id"]
    store.set_inputs(
        run["id"],
        {"project": inp["project_id"], "path": inp["path"], "title": title, "why": inp["why"]},
    )
    stop = plugins.final(run)
    if stop:
        return stop
    origin = artifact_store.Origin(engine="automation", session_key=key, phys_key=key, cwd=real)
    args = {"path": _item(real, inp["path"]), "title": title, "why": inp["why"] or None}
    try:
        try:
            # `private_only`: the store adds the revision behind `previous` only if it is still
            # private INSIDE its commit transaction; shared since this snapshot (a share takes the
            # same write lock), the content becomes a new private artifact there instead.
            got = artifact_store.publish(
                origin, {**args, "artifact_id": previous}, private_only=True
            )
        except artifact_store.ArtifactRefused as e:
            if previous is None or e.code not in ("kind_mismatch", "unknown_artifact", "not_yours"):
                raise
            got = artifact_store.publish(origin, {**args, "artifact_id": None}, private_only=True)
    except artifact_store.ArtifactRefused as e:
        meaning = artifacts_mcp.MEANING.get(e.code, e.code)
        where = f" ({e.path})" if e.path else ""
        return "refused", f"the artifact store refused it: {meaning}{where}; nothing was published"
    except artifact_store.ArtifactsUnavailable as e:
        return "failed", f"the artifact store is unavailable ({e}); nothing was published"
    if got.get("started_new_private"):
        words = (
            f"published privately as a new artifact, since the previous one was shared publicly "
            f"meanwhile: {got['url']}"
        )
    else:
        words = f"published privately: {got['url']} (revision {got['revision']})"
    out = plugins.delivered(run, "ok", words)
    store.set_inputs(
        run["id"],
        {
            "project": inp["project_id"],
            "path": inp["path"],
            "title": title,
            "why": inp["why"],
            "output": {"artifact": got["id"], "link": got["url"]},
        },
    )
    return out
