"""HQ proposes specialists as Library cards (#1938): create, edit, archive and unarchive.

A specialist card is a Library card like a playbook's (`ask_library`, `ask_operations`): HQ stages
it, the Commander taps it, and the SAME route the Library's own form calls runs
(``/api/specialists*``, in-process, as the operator). Every rule is the store's own; HQ adds no
limit of its own (no narrower capacity, no cap on how many cards one turn stages).

**Indexes, resolved before validation.** `specialists.check_draft` accepts any registered agent,
offered model and Library skill. That is not the same as one HQ was SHOWN, so the agent, model,
effort and skills are resolved here first, against the listings this turn's read tools returned
(`list_agents`, `list_agent_models`, `list_skills`): a valid id this turn did not list is refused,
and the model never writes an engine id, a model id or a skill id. An index means the same row for
the whole turn (`stable`): a list fetched twice, or in another order, keeps every index it gave.

**Content writes go through `check_draft`, twice.** A create or an edit is checked at staging
(off the loop: it may ask a CLI) and again at the tap, by the route. Anything that changed in
between reads as **stale with the validator's reason**, never as invalid: the specialist's
revision (a concurrent edit), a pinned skill's revision, the agent's model list no longer
offering the model.

**Archive and unarchive do not.** They use `set_archived(..., expect_revision)` only, so they work
for a specialist whose old model or skill pins no longer validate. A stale revision is stale; a
refusal by the store that changed nothing (#1935's built-in staff) is ``refused``, in the store's
own words.

**A playbook names roles, never specialists.** `relies` derives the state of each specialist a
playbook card says it relies on at READ time, from the specialists store and this conversation's
cards, so it stays truthful whichever companion card is confirmed, declined or fails, in any
order. Saving a playbook creates no specialist.
"""

from __future__ import annotations

import asyncio

from . import asgi_call, ask_store

#: What a list tool returns at most (`ask_library.LIST_MAX`).
LIST_MAX = 60
#: Specialists a playbook card may say it relies on.
RELIES_MAX = 24
ACTIONS = ("create", "edit", "archive", "unarchive")
#: Draft PATHS the SERVER resolved from this turn's listings or read from the store, never model
#: text: an engine id, a model id, an effort level and the pinned skills (a model id and a skill
#: revision read as credential-shaped to the boundary's heuristic). Title and summary are guarded.
TRUSTED_DRAFT_PATHS: frozenset[tuple[str, ...]] = frozenset(
    (side, key) for side in ("body", "before") for key in ("engine", "model", "effort", "skills")
)
#: What did not happen, by action: the last sentence of a stale or refused card.
NOTHING = {
    "create": "Nothing was created.",
    "edit": "Nothing was saved.",
    "archive": "Nothing changed.",
    "unarchive": "Nothing changed.",
}
EDITED = "It was edited since this card was made."


def _refused(message: str, code: str = "bad_arguments"):
    from .ask_library import Refused as LibraryRefused

    return LibraryRefused(message, code)


# ---- this turn's listings ------------------------------------------------------------------------


def stable(prior: list[dict] | None, rows: list[dict]) -> list[dict]:
    """This turn's listing after one more read: every row keeps the index it was first given, a
    new row takes the next one, and a row the new read no longer returns stays in place marked
    ``gone`` (its index is never reused, and it can no longer be chosen). So an index names the
    same thing whichever of the turn's listings it came from."""
    out = [{**r, "gone": True} for r in (prior or [])]
    at = {r["id"]: i for i, r in enumerate(out)}
    for r in rows:
        i = at.get(r["id"])
        if i is None:
            at[r["id"]] = i = len(out)
            out.append({})
        out[i] = {**r, "index": i, "gone": False}
    return out


def pick(args: dict, name: str, rows: list[dict] | None, listed_by: str) -> dict:
    """The row an index names in this turn's listing, or `Refused`."""
    if rows is None:
        raise _refused(f"refused: call {listed_by} in this turn first", "not_listed")
    v = args.get(name)
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < len(rows):
        raise _refused(
            f"refused: {name} must be an index {listed_by} returned in this turn", "bad_index"
        )
    if rows[v].get("gone"):
        raise _refused(
            f"refused: {name} names a row {listed_by} no longer returns; use its latest result",
            "bad_index",
        )
    return rows[v]


def agent_models(engine_id: str) -> list[dict]:
    """The models one agent offers, as a specialist may name them: the agent's own default first,
    then its list (an API client's, asked of its CLI; a terminal agent's manifest and the
    operator's added ids). Each with the effort levels it offers. Blocking."""
    from . import engines, model_choice, model_grades

    rows: list[dict] = [{"id": model_choice.DEFAULT, "label": "the agent's default", "efforts": []}]
    prov = engines.get(engine_id)
    manifest = getattr(prov, "manifest", None)
    if prov is None or manifest is None:
        return rows
    grades = model_grades.get()
    if manifest.runtime == "api":
        from . import native_models

        listing = native_models.models(prov)
        found = listing.models if listing.status == "ok" else []
        listed = [(m["id"], str(m.get("label") or ""), list(m.get("efforts") or [])) for m in found]
    else:
        listed = [(m["id"], "", []) for m in model_choice.offered(manifest)]
    for mid, label, efforts in listed[:LIST_MAX]:
        rows.append(
            {
                "id": mid,
                "label": label,
                "efforts": efforts,
                "grade": model_grades.grade_of(engine_id, mid, grades),
            }
        )
    return rows


def merge_models(prior: list[dict] | None, rows: list[dict]) -> list[dict]:
    """`stable` for a model list; a model's effort levels keep their positions too."""
    before = {r["id"]: r for r in (prior or [])}
    merged = []
    for r in rows:
        old = list((before.get(r["id"]) or {}).get("efforts_listed") or [])
        listed = old + [e for e in r["efforts"] if e not in old]
        merged.append({**r, "efforts_listed": listed})
    return stable(prior, merged)


# ---- reads ---------------------------------------------------------------------------------------


def _ident(value: object, secrets: list[str]) -> str:
    """A server-validated identifier for the model: checked against the REGISTERED secrets only
    (the boundary's shape heuristic, meant for free text, flags real model ids)."""
    from . import template_secrets

    return template_secrets.redact_text(str(value or ""), secrets)


def _secrets() -> list[str]:
    from . import template_secrets

    try:
        return template_secrets.redaction_values()
    except template_secrets.RedactionUnavailable as e:
        raise _refused(f"refused: {e}", "not_available") from None


def models_for_model(rows: list[dict]) -> list[dict]:
    """A model list as the MODEL sees it: indexes, names and effort levels by index."""
    from .mission_team import GRADE_LABEL

    secrets = _secrets()
    out = []
    for r in rows:
        if r.get("gone"):
            continue
        row = {"index": r["index"], "model": _ident(r["id"], secrets)}
        if r.get("label"):
            row["name"] = ask_store.guard_text(r["label"], 120, secrets=secrets)
        if r.get("grade"):
            row["grade"] = GRADE_LABEL.get(r["grade"], r["grade"])
        efforts = [
            {"index": i, "level": e}
            for i, e in enumerate(r.get("efforts_listed") or [])
            if e in r["efforts"]
        ]
        if efforts:
            row["efforts"] = efforts
        out.append(row)
    return out


async def detail(app, operator, row: dict, agents: list[dict] | None) -> tuple[dict, dict]:
    """One specialist in full, for the model: every field and its current version. Returns
    ``(shown, seen)``; ``seen`` is the revision HQ has now read (an edit is bound to it)."""
    from .ask_library import _get
    from .mission_team import GRADE_LABEL

    body = await _get(app, operator, f"/api/specialists/{row['id']}", "that specialist")
    secrets = await asyncio.to_thread(_secrets)
    cur = body.get("current") if isinstance(body.get("current"), dict) else {}
    names = {a["id"]: a["name"] for a in agents or []}
    missing = set(body.get("skills_missing") or [])
    outdated = set(body.get("skills_outdated") or [])

    def text(v: object, cap: int) -> str:
        return ask_store.guard_text(str(v or ""), cap, secrets=secrets)

    skills = []
    for k in cur.get("skills") or []:
        if not isinstance(k, dict):
            continue
        sid = str(k.get("id") or "")
        state = "missing" if sid in missing else "changed" if sid in outdated else "current"
        skills.append({"skill": _ident(sid, secrets), "pinned": state})
    grade = body.get("grade")
    shown = {
        "index": row["index"],
        "title": text(cur.get("title"), 120),
        "summary": text(cur.get("summary"), 400),
        "agent": names.get(cur.get("engine"), "an agent list_agents did not return"),
        "model": _ident(cur.get("model"), secrets),
        "effort": cur.get("effort"),
        "grade": GRADE_LABEL.get(grade, "ungraded") if grade else "ungraded",
        "skills": skills,
        "capacity": cur.get("capacity"),
        "version": cur.get("version"),
        "versions": len(body.get("versions") or []),
        "archived": bool(body.get("archived")),
        "running": body.get("live") or 0,
        "note": "The grade follows the model (the operator's model grades); it is never set on a "
        "specialist. An edit saves a new version; missions that pinned an older one keep it.",
    }
    return shown, {"revision": body.get("revision")}


# ---- the catalog entries -------------------------------------------------------------------------


def _sentence(detail: object, fallback: str) -> str:
    """The store's or the validator's own words as one sentence."""
    said = " ".join(str(detail or "").split()) or fallback
    said = said[0].upper() + said[1:]
    return said if said.endswith((".", "!", "?")) else said + "."


def _conflict() -> str:
    from . import specialists_store

    return str(specialists_store.Conflict())


def _outcome(action: str, done):
    """A specialist route's answer as the card's state. A content write the validator refuses at
    the tap, and a revision that moved, are STALE (the card was valid when it was staged, so
    something changed since); any other refusal by the store changed nothing: REFUSED."""

    def outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
        from .ask_operations import DONE, FAILED, REFUSED, STALE

        if 200 <= status < 300:
            return DONE, done(facts)
        detail = body.get("detail") if isinstance(body, dict) else None
        if status == 404:
            return STALE, f"It no longer exists. {NOTHING[action]}"
        if status == 409 and detail == _conflict():
            return STALE, f"{EDITED} {NOTHING[action]}"
        if status == 422 and action in ("create", "edit") and isinstance(detail, str):
            return STALE, f"{_sentence(detail, 'It is no longer valid')} {NOTHING[action]}"
        if 400 <= status < 500 and isinstance(detail, str) and detail.strip():
            return REFUSED, f"{_sentence(detail, 'Refused')} {NOTHING[action]}"
        return FAILED, ""

    return outcome


def _failure(action: str):
    def failure(status: int, detail: str, facts: dict) -> str:
        return f"{_sentence(detail, 'It did not go through')} {NOTHING[action]}"

    return failure


def _stale(action: str):
    """Why a card whose specialist moved before the tap is out of date, as a sentence."""

    def said(why: str, row: dict) -> str:
        name = str((row.get("fields") or {}).get("name") or "It")
        if why == "it no longer exists":
            what = f"{name} no longer exists."
        elif why == "its archive state changed":
            what = f"{name} was archived or unarchived since this card was made."
        else:
            what = f"{name} was edited since this card was made."
        return f"{what} {NOTHING[action]}"

    return said


def _body(fields: dict) -> dict:
    c = fields.get("_content")
    if not isinstance(c, dict) or not isinstance(c.get("body"), dict):
        raise ValueError("a specialist save runs only with its bound draft")
    return c["body"]


def specialist_ops() -> tuple:
    from .ask_operations import Operation

    sp = "specialist"
    return (
        Operation(
            "specialist.create",
            sp,
            "create",
            "New specialist",
            "Create specialist",
            "reversible",
            "none",
            ("name",),
            lambda t, f, x: ("POST", "/api/specialists", _body(f)),
            lambda f, x: f"Creates the specialist {f['name']} in your Library.",
            _outcome("create", lambda x: "Created. Open it in the Library."),
            _failure("create"),
            draft=True,
        ),
        Operation(
            "specialist.edit",
            sp,
            "edit",
            "Edit specialist",
            "Save new version",
            "reversible",
            sp,
            ("name", "version"),
            lambda t, f, x: (
                "PATCH",
                f"/api/specialists/{t}",
                {"revision": x["revision"], "draft": _body(f)},
            ),
            lambda f, x: f"Saves version {f['version']} of {f['name']}. Missions that pinned "
            f"version {f['version'] - 1} keep it.",
            _outcome("edit", lambda x: f"Saved as version {int(x.get('version') or 0) + 1}."),
            _failure("edit"),
            draft=True,
            verb_of=lambda f, x: f"Save as version {f['version']}",
            stale=_stale("edit"),
        ),
        Operation(
            "specialist.archive",
            sp,
            "archive",
            "Archive specialist",
            "Archive",
            "reversible",
            sp,
            ("name",),
            lambda t, f, x: (
                "POST",
                f"/api/specialists/{t}/archive",
                {"revision": x["revision"]},
            ),
            lambda f, x: "No new launches. Versions that missions pinned stay readable.",
            _outcome("archive", lambda x: "Archived. Unarchive brings it back as it is."),
            _failure("archive"),
            stale=_stale("archive"),
        ),
        Operation(
            "specialist.unarchive",
            sp,
            "unarchive",
            "Unarchive specialist",
            "Unarchive",
            "reversible",
            sp,
            ("name", "version"),
            lambda t, f, x: (
                "POST",
                f"/api/specialists/{t}/unarchive",
                {"revision": x["revision"]},
            ),
            lambda f, x: "It can be launched again. Nothing about it changes: it comes back as "
            f"version {f['version']}.",
            _outcome("unarchive", lambda x: "Unarchived. It can be launched again."),
            _failure("unarchive"),
            stale=_stale("unarchive"),
        ),
    )


async def facts(op, target: str, app, operator) -> dict | None:
    """What a specialist card binds: the store's revision, the current version and whether it is
    archived. None when it is gone. A create binds nothing (there is no target yet)."""
    if op.target == "none":
        return {}
    r = await asgi_call.call(app, operator, "GET", f"/api/specialists/{target}", None)
    if r.status != 200 or not isinstance(r.body, dict):
        return None
    cur = r.body.get("current") if isinstance(r.body.get("current"), dict) else {}
    return {
        "revision": int(r.body.get("revision") or 0),
        "version": int(cur.get("version") or 0),
        "archived": bool(r.body.get("archived")),
    }


# ---- staging -------------------------------------------------------------------------------------


def _capacity(raw: object) -> object:
    # The store's whole range is `check_draft`'s to judge; only the type is settled here.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _refused("capacity must be a whole number")
    return raw


def _skills(args: dict, lists: dict, library: dict[str, str]) -> list[dict]:
    raw = args.get("skill_indexes")
    if not isinstance(raw, list):
        raise _refused("skill_indexes must be a list of indexes list_skills returned (or [])")
    out = []
    for v in raw:
        row = pick({"skill_index": v}, "skill_index", lists.get("skills"), "list_skills")
        if row["id"] not in library:
            raise _refused(
                "refused: a skill you picked is no longer in the Library; call list_skills again",
                "not_available",
            )
        # Pinned at the revision the Library holds NOW: `check_draft` accepts no other.
        out.append({"id": row["id"], "revision": library[row["id"]]})
    return out


def _agent(args: dict, lists: dict) -> dict:
    return pick(args, "engine_index", lists.get("agents"), "list_agents")


def _model(args: dict, lists: dict, engine: str) -> dict:
    rows = (lists.get("models") or {}).get(engine)
    if rows is None:
        raise _refused(
            "refused: call list_agent_models for that agent in this turn first", "not_listed"
        )
    return pick(args, "model_index", rows, "list_agent_models")


def _effort(args: dict, model: dict) -> str | None:
    v = args.get("effort_index")
    if v is None or v == -1:  # -1: no effort level (an edit clears one with it)
        return None
    listed = model.get("efforts_listed") or []
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < len(listed):
        raise _refused(
            "refused: effort_index must be an index from that model's efforts in "
            "list_agent_models (leave it out when the model lists none)",
            "bad_index",
        )
    if listed[v] not in model["efforts"]:
        raise _refused("refused: that model no longer offers that effort level", "bad_index")
    return listed[v]


def _check(draft: dict, library: dict[str, str]) -> dict:
    from . import specialists

    try:
        return specialists.check_draft(draft, library=library)
    except specialists.DraftRefused as e:
        raise _refused(f"the specialist is not valid: {e.detail}", "invalid") from None


def _library() -> dict[str, str]:
    from . import specialists

    return specialists.library_revisions()


def _target(args: dict, lists: dict) -> dict:
    return pick(args, "specialist_index", lists.get("specialists"), "list_specialists")


async def _current(app, operator, sid: str) -> dict:
    from .ask_library import _get

    return await _get(app, operator, f"/api/specialists/{sid}", "that specialist")


def _staged(
    op, target: str, title: str, fields: dict, draft: dict | None, expected: dict | None = None
) -> dict:
    out = {"op": op, "target": target, "title": title, "fields": fields, "draft": draft}
    if expected is not None:
        out["expected"] = expected
    return out


def _snapshot(stored: dict) -> dict:
    """The facts of the ONE read a card was built from: what `facts` must read again at staging
    (`ask_operations.stage(expected=…)`), so the revision a card binds is the revision its draft,
    its version and its title came from. A save in between refuses the card (#1938 review)."""
    cur = stored.get("current") if isinstance(stored.get("current"), dict) else {}
    return {
        "revision": int(stored.get("revision") or 0),
        "version": int(cur.get("version") or 0),
        "archived": bool(stored.get("archived")),
    }


async def prepare(action: str, args: dict, *, lists: dict, app, operator) -> dict:
    """What `propose_library` stages for group ``specialist``. ``lists`` holds this turn's
    ``agents``, ``models`` (engine id → its listing), ``skills``, ``specialists`` and ``seen``
    (specialist id → the revision `specialist_detail` read). Raises `ask_library.Refused`."""
    from .ask_operations import CATALOG

    op = CATALOG.get(f"specialist.{action}")
    if op is None:
        raise _refused(f"action must be one of {', '.join(ACTIONS)}")

    if op.id == "specialist.create":
        agent = _agent(args, lists)
        model = _model(args, lists, agent["id"])
        library = await asyncio.to_thread(_library)
        draft = {
            "title": args.get("title"),
            "summary": args.get("summary", ""),
            "skills": _skills(args, lists, library),
            "engine": agent["id"],
            "model": model["id"],
            "effort": _effort(args, model),
            "capacity": _capacity(args.get("capacity", 1)),
        }
        body = await asyncio.to_thread(_check, draft, library)
        return _staged(op, "", body["title"], {"name": body["title"]}, {"body": body})

    row = _target(args, lists)
    sid = row["id"]
    stored = await _current(app, operator, sid)
    cur = stored.get("current") if isinstance(stored.get("current"), dict) else {}
    name, version = str(cur.get("title") or sid), int(cur.get("version") or 0)
    # The card binds THIS read's revision, never one a later read finds (#1938 review): staging
    # re-reads the facts and refuses the card when they moved since.
    snap = _snapshot(stored)

    if op.id == "specialist.archive":
        if stored.get("archived"):
            raise _refused("it is already archived")
        return _staged(op, sid, f"{name} · version {version}", {"name": name}, None, snap)
    if op.id == "specialist.unarchive":
        if not stored.get("archived"):
            raise _refused("it is not archived")
        return _staged(
            op,
            sid,
            f"{name} · version {version} · archived",
            {"name": name, "version": version},
            None,
            snap,
        )

    # specialist.edit — HQ's change to what IT read: bound to the revision `specialist_detail`
    # returned this turn, never to one saved in between.
    seen = (lists.get("seen") or {}).get(sid)
    if seen is None:
        raise _refused(
            "refused: read it with specialist_detail in this turn first, then propose the edit",
            "not_read",
        )
    if seen != stored.get("revision"):
        raise _refused(
            "refused: it was saved elsewhere after you read it; read it again and redo the edit "
            "on the new version",
            "stale_read",
        )
    if stored.get("archived"):
        raise _refused("refused: it is archived; propose unarchive first, then the edit")
    before = {k: cur.get(k) for k in _fields()}
    library = await asyncio.to_thread(_library)
    draft = dict(before)
    for k in ("title", "summary"):
        if args.get(k) is not None:
            draft[k] = args[k]
    if args.get("capacity") is not None:
        draft["capacity"] = _capacity(args["capacity"])
    if args.get("skill_indexes") is not None:
        draft["skills"] = _skills(args, lists, library)
    else:
        # Kept skills are pinned at the Library's revision NOW, as saving in the Library's own
        # form does: a version may only pin what is current.
        draft["skills"] = [
            {"id": k["id"], "revision": library.get(k["id"], k["revision"])}
            for k in before["skills"] or []
        ]
    engine = before["engine"]
    if args.get("engine_index") is not None:
        engine = _agent(args, lists)["id"]
        if engine != before["engine"] and args.get("model_index") is None:
            raise _refused(
                "refused: another agent needs its own model: give model_index from "
                "list_agent_models for that agent"
            )
    draft["engine"] = engine
    if args.get("model_index") is not None:
        model = _model(args, lists, engine)
        draft["model"] = model["id"]
        draft["effort"] = _effort(args, model)
    elif "effort_index" in args:
        listed = next(
            (m for m in (lists.get("models") or {}).get(engine) or [] if m["id"] == draft["model"]),
            None,
        )
        if args["effort_index"] in (None, -1):
            draft["effort"] = None
        elif listed is None or listed.get("gone"):
            raise _refused(
                "refused: call list_agent_models for its agent in this turn first", "not_listed"
            )
        else:
            draft["effort"] = _effort(args, listed)
    body = await asyncio.to_thread(_check, draft, library)
    if body == before:
        raise _refused("that edit changes nothing")
    return _staged(
        op,
        sid,
        f"{name} · version {version} → {version + 1}",
        {"name": name, "version": version + 1},
        {"body": body, "before": before, "version": version + 1},
        snap,
    )


def _fields() -> tuple[str, ...]:
    from . import specialists

    return specialists.FIELDS


# ---- the card's content --------------------------------------------------------------------------


def _shown(body: dict, labels: dict, grades: dict) -> dict:
    """One version as the card shows it: the stored fields, the agent's name, and the grade the
    operator's model map gives its model NOW (derived, never stored and never chosen here)."""
    from . import model_grades

    engine, model = str(body.get("engine") or ""), body.get("model")
    return {
        "title": body.get("title") or "",
        "summary": body.get("summary") or "",
        "engine": engine,
        "agent": labels.get(engine, ""),
        "model": model or "",
        "effort": body.get("effort"),
        "grade": model_grades.grade_of(engine, model, grades),
        "skills": [
            {"id": k.get("id"), "revision": k.get("revision")}
            for k in body.get("skills") or []
            if isinstance(k, dict)
        ],
        "capacity": body.get("capacity"),
    }


def content_view(draft: dict) -> dict:
    """What a specialist card shows of its draft, for the operator's browser only (never the
    model): the specialist in full and, for an edit, the version it changes. ``skills_now`` is
    the Library's revision of each pinned skill at THIS read (None: gone), so a pin that moved
    since staging is marked on the card. Blocking."""
    from . import ask_settings, model_grades, specialists

    labels, grades = ask_settings.agent_labels(), model_grades.get()
    body = draft.get("body") if isinstance(draft.get("body"), dict) else {}
    before = draft.get("before") if isinstance(draft.get("before"), dict) else None
    try:
        library: dict | None = specialists.library_revisions()
    except Exception:  # noqa: BLE001 — an unreadable library marks nothing; never a guess
        library = None
    after = _shown(body, labels, grades)
    out: dict = {
        "kind": "specialist",
        "action": "edit" if before is not None else "create",
        "after": after,
        "before": _shown(before, labels, grades) if before is not None else None,
        "skills_now": (
            {k["id"]: library.get(k["id"]) for k in after["skills"]} if library is not None else {}
        ),
    }
    version = draft.get("version")
    if before is not None and isinstance(version, int) and not isinstance(version, bool):
        out["version"] = {"from": version - 1, "to": version}
    return out


def relies(cid: str, pid: str, titles: object) -> list[dict]:
    """The specialists a playbook card says it relies on, each with its state NOW: ``exists`` (an
    unarchived specialist in the Library has that title), ``proposed`` (a card creating it waits
    in this conversation) or ``missing``. Derived at read from the store and the conversation's
    cards, never from what was staged. Blocking."""
    from . import ask_proposals, ask_settings, specialists_store

    names = [t for t in titles or [] if isinstance(t, str)][:RELIES_MAX]
    if not names:
        return []
    labels = ask_settings.agent_labels()
    have: dict[str, dict] = {}
    for s in specialists_store.list_all():
        if not s.get("archived"):
            have.setdefault(str(s["current"].get("title") or "").casefold(), s)
    waiting: dict[str, str] = {}
    rows = ask_store.read_proposals(cid)
    own = next((i for i, r in enumerate(rows) if r.get("id") == pid), -1)
    for i, r in enumerate(rows):
        if r.get("op") == "specialist.create" and r.get("state") in (
            ask_proposals.PENDING,
            ask_proposals.DECIDING,
        ):
            title = str((r.get("fields") or {}).get("name") or "").casefold()
            waiting.setdefault(title, "below" if i > own else "above")
    out = []
    for title in names:
        key = title.casefold()
        if key in have:
            cur = have[key]["current"]
            out.append(
                {
                    "title": title,
                    "state": "exists",
                    "version": cur.get("version"),
                    "engine": cur.get("engine"),
                    "agent": labels.get(cur.get("engine"), ""),
                    "model": cur.get("model"),
                }
            )
        elif key in waiting:
            out.append({"title": title, "state": "proposed", "where": waiting[key]})
        else:
            out.append({"title": title, "state": "missing"})
    return out


def relies_arg(raw: object) -> list[str]:
    """`propose_library`'s ``specialists`` for a playbook: the titles of the specialists its steps
    rely on. Titles only (a playbook names roles; nothing here binds or creates one)."""
    from . import specialists

    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > RELIES_MAX:
        raise _refused(f"specialists must be a list of at most {RELIES_MAX} specialist titles")
    out: list[str] = []
    for t in raw:
        try:
            title = specialists._text(t, "a specialist title", specialists.TITLE_MAX, required=True)
        except specialists.DraftRefused as e:
            raise _refused(f"specialists: {e.detail}") from None
        if title.casefold() not in {o.casefold() for o in out}:
            out.append(title)
    return out


__all__ = [
    "ACTIONS",
    "NOTHING",
    "TRUSTED_DRAFT_PATHS",
    "agent_models",
    "content_view",
    "detail",
    "facts",
    "merge_models",
    "models_for_model",
    "pick",
    "prepare",
    "relies",
    "relies_arg",
    "specialist_ops",
    "stable",
]
