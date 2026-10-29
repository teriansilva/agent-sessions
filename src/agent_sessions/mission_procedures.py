"""Apply a procedure to a mission (#1777 S1b, #1613 P4 slice 1: one project).

An **objective** is one project, one procedure from that project's playbook, and that procedure's
inputs (#1613 §1). This module builds the two things the operator applies from:

* **the listing** — the mission project's DEPLOYED playbook and its procedures, by index, with a
  digest of exactly that list (`listing`). The client names a procedure only by its index plus
  that digest; a list that changed in between is a 409, never a different procedure.
* **a mission-local procedure** — a format-5 flow the operator wrote for this mission, validated
  by the bundle loader's own flow validator (`playbooks.validate.flow_document`). Never a bypass.

**Where every probe target comes from** (#1777 D-P4-2, #1076's principle):

* a deployed procedure: ONLY the deployment's recorded, reviewed targets (`review_facts`, written
  at apply from the review the operator confirmed, including any individually confirmed format-2
  default). Current bindings are not re-read, so nothing the operator did not review can become a
  target. The playbook's current revision must BE the deployed revision; otherwise the listing
  says "update the deployment first" — nothing is silently remapped;
* a mission-local procedure: the project's current bindings (`binding.resolve`), and a target
  that would come from a bundle DEFAULT is refused.

Either way each concrete argument passes `missions.validate_probe_args` at the store. An argument
that waits for an upstream step's output (`{{steps.<id>.<slot>}}`) is not a target yet: the row is
written as a required `none` gate carrying its real probe in `pending` (D-P4-3), which no runner
probes and `gate_tally` counts as unmet.

Every function here is BLOCKING (store reads, then one missions transaction); routes run them
through `missions.run_admitted`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib

from . import mission_plan_document as plan_doc
from . import missions, template_vars
from .playbooks import PlaybookFormatError, binding, schema, store, validate
from .playbooks import deployment_state as state

#: The ONE reason that means "there is no deployment" — every other `_deployment` reason is a
#: deployment that exists but can't be used or read, and must never pass for an absent one.
NO_DEPLOYMENT = "this project has no deployed playbook"


class ProcedureError(missions.MissionError):
    """A refusal the route answers with its status (409: stale or not applicable; 422: invalid)."""


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()[:32]


def _current_bundle(playbook_id: str) -> tuple[dict, str]:
    """The validated bundle at its current revision, or `StoreError`. The bundle also carries
    each flow AS AUTHORED (`_documents`), which is what a plan snapshot stores and the store
    re-validates (`mission_plan_document`); parsed from the same tree snapshot it was validated
    from, so the two cannot be different bytes."""
    store.playbook_id(playbook_id)
    with store.root_lock(exclusive=False) as root_fd:
        entry = store._find(store._all_entries(root_fd, strict=True), playbook_id)
    if entry.error or entry.pb is None or entry.revision is None or entry.tree is None:
        raise store.StoreError("the project's playbook is invalid", status=409)
    documents = {}
    for fid in entry.pb["flows"]:
        data = entry.tree.files.get(f"{schema.FLOWS_DIR}/{fid}.toml")
        if data is None:
            raise store.StoreError("the project's playbook is invalid", status=409)
        documents[fid] = tomllib.loads(data.decode("utf-8"))
    return {**entry.pb, "_documents": documents}, entry.revision


def _deployment(project_id: str) -> tuple[dict | None, dict | None, str | None]:
    """`(record, bundle, reason)`: the project's applied deployment and the bundle it deployed, or
    the reason there is none to apply from. Fail closed: a damaged, unsettled or out-of-date
    deployment is a reason, never an empty list that looks like "no playbook"."""
    try:
        record = state.active(project_id)
    except store.StoreError as e:
        return None, None, f"the project's deployment record cannot be read ({e})"
    if record is None:
        return None, None, NO_DEPLOYMENT
    if record["state"] != "applied" or state.pending(record):
        return record, None, "the project's deployment is not settled; finish or recover it first"
    facts = record.get("review_facts")
    deployed = (record.get("applied_paths") or {}).get("revision")
    if (
        not isinstance(facts, dict)
        or not isinstance(facts.get("targets"), list)
        or not isinstance(deployed, str)
        or (record.get("inputs") or {}).get("revision") != deployed
    ):
        return record, None, "the deployment has no recorded review; review and apply it again"
    try:
        bundle, current = _current_bundle(record["playbook_id"])
    except store.StoreError as e:
        return record, None, f"the project's playbook cannot be read ({e.detail})"
    if current != deployed:
        return (
            record,
            None,
            "the playbook changed since this project deployed it; update the deployment first",
        )
    return record, bundle, None


def _reviewed_items(flow_id: str, flow: dict, facts: dict) -> list[dict]:
    """The procedure's evidence with each target taken from the deployment's review, or the
    `ProcedureError` saying why this procedure cannot be applied from it."""
    targets = {t.get("id"): t for t in facts["targets"] if isinstance(t, dict)}
    items: list[dict] = []
    for step in flow["steps"]:
        for item in step["checklist"]:
            t = targets.get(f"flow:{flow_id}:{step['id']}:{item['key']}")
            if t is None or t.get("kind") != "probe" or t.get("probe") != item["probe"]:
                raise ProcedureError(
                    "the deployment's review does not cover this procedure; review and apply "
                    "the deployment again",
                    status=409,
                )
            args, pending = t.get("args"), t.get("pending")
            defaults = t.get("default_variables")
            if (
                not isinstance(args, dict)
                or not isinstance(pending, dict)
                or not isinstance(defaults, list)
            ):
                raise ProcedureError("the deployment's review is damaged", status=409)
            # FAIL CLOSED ON A BUNDLE DEFAULT (the independent review on #1790). The review
            # record says which targets a bundle default would choose, but NOT which of them the
            # operator individually confirmed: a deployment applies with such targets left
            # unarmed. So a target derived from a default is never a mission's — concrete or
            # waiting — until the deployment records the confirmation itself (a follow-up).
            if defaults:
                raise ProcedureError(
                    "a probe target in this procedure comes from a playbook default ("
                    + ", ".join(sorted(str(d) for d in defaults))
                    + "); bind a project value for it and apply the deployment again",
                    status=409,
                )
            items.append(_item(step, item, args, pending))
    return items


def _item(step: dict, item: dict, args: dict, pending: dict) -> dict:
    title = item["title"]
    if pending:
        # WAITING for an upstream step's output: a `none` gate until the bridge (S1c) turns it
        # into its real probe. It GATES even when the item is optional (Hermes on #1790): a row no
        # runner can probe must never let a mission settle around it. Its own `required` travels
        # in `pending.gate`, which the bridge restores. Unmet in `gate_tally`; probed by nothing.
        return {
            "step": step["id"],
            "key": item["key"],
            "title": title,
            "probe": "none",
            "probe_args": None,
            "pending": {
                "probe": item["probe"],
                "args": copy.deepcopy(args),
                "refs": copy.deepcopy(pending),
                "gate": bool(item["required"]),
            },
            "gate": True,
        }
    missions.validate_probe_args(item["probe"], args)
    return {
        "step": step["id"],
        "key": item["key"],
        "title": title,
        "probe": item["probe"],
        "probe_args": copy.deepcopy(args) or None,
        "pending": None,
        "gate": item["required"],
    }


def _ready_reason(flow_id: str, flow: dict, facts: dict) -> str | None:
    try:
        items = _reviewed_items(flow_id, flow, facts)
        for it in items:
            plan_doc.evidence_key("o99", it["key"])
    except missions.MissionError as e:
        return str(e).removeprefix("invalid plan: ")
    if len(items) > missions.OBJECTIVES_MAX:
        return f"this procedure has more evidence than a mission holds ({missions.OBJECTIVES_MAX})"
    return None


def _variables_used(flow: dict) -> list[str]:
    names: set[str] = set()
    for step in flow["steps"]:
        for item in step["checklist"]:
            for value in item["probe_args"].values():
                ref = validate._whole_var_ref(value)
                if ref is not None:
                    names.add(ref)
    return sorted(names)


def listing(mission_id: str) -> dict:
    """The procedures this mission can apply, by index, with the digest of exactly this list."""
    return _listing(mission_id)[0]


def _flow_targets(facts: dict, fid: str) -> list:
    """The reviewed targets of one flow, in a stable order: part of what a listing digest binds."""
    prefix = f"flow:{fid}:"
    rows = [t for t in facts.get("targets", []) if isinstance(t, dict)]
    return sorted(
        (t for t in rows if str(t.get("id", "")).startswith(prefix)),
        key=lambda t: str(t.get("id")),
    )


def _listing(mission_id: str) -> tuple[dict, dict | None, dict | None]:
    """`(listing, record, bundle)`: the listing AND the one deployment snapshot it was built from.

    The digest binds that snapshot — the deployment's identity and generation, the deployed
    revision, each flow's digest AND each flow's reviewed targets (Hermes on #1790) — and an apply
    builds its rows from this same snapshot, never from a second read. So the targets written are
    exactly the ones the operator's `{index, listing}` was chosen against, and a re-applied
    deployment with other targets is a different listing."""
    scope = missions.procedure_scope(mission_id)
    pid = scope["project_id"]
    out: dict = {
        "mission_id": mission_id,
        "project_id": pid,
        "playbook": None,
        "reason": None,
        "procedures": [],
    }
    basis: dict = {"project": pid, "playbook": None, "flows": []}
    record = bundle = None
    if not pid:
        out["reason"] = "the mission has no project; every objective names one (choose it first)"
    else:
        record, bundle, reason = _deployment(pid)
        out["reason"] = reason
        if bundle is not None and record is not None:
            revision = record["applied_paths"]["revision"]
            out["playbook"] = {
                "id": record["playbook_id"],
                "name": bundle["identity"]["name"],
                "revision": revision,
            }
            basis["playbook"] = [
                record["playbook_id"],
                revision,
                record.get("id"),
                record.get("generation"),
            ]
            for index, (fid, flow) in enumerate(sorted(bundle["flows"].items())):
                why = _ready_reason(fid, flow, record["review_facts"])
                out["procedures"].append(
                    {
                        "index": index,
                        "flow_id": fid,
                        "title": flow["title"],
                        "description": flow["description"],
                        "steps": len(flow["steps"]),
                        "default": fid == bundle["default_flow"],
                        "ready": why is None,
                        "reason": why,
                    }
                )
                basis["flows"].append(
                    [fid, plan_doc.flow_digest(flow), _flow_targets(record["review_facts"], fid)]
                )
    out["listing"] = _digest(basis)
    return out, record, bundle


# ---- procedure inputs picked by index (S1d, #1076) ----------------------------------------------

#: How many choices one input offers: the deployed value plus open pull requests.
MAX_CHOICES = 50
#: The arguments whose variable may be chosen per objective. `branch` only: it NARROWS a target
#: within the reviewed repository, never chooses one (`repo`/`url` stay deployment-reviewed).
CHOOSABLE_ARGS = frozenset({"branch"})


def _forge_client():
    """The configured forge's client, or None. Its credential stays server-side."""
    from . import forge, prefs

    try:
        cfg = prefs.get_forge()
        if not cfg.get("enabled") or not str(cfg.get("base_url") or "").strip():
            return None
        return forge.ForgeClient(
            kind=str(cfg.get("kind") or ""),
            base_url=str(cfg.get("base_url") or ""),
            token=str(cfg.get("token") or ""),
            owner=str(cfg.get("owner") or ""),
        )
    except Exception:  # noqa: BLE001 — fail-soft: no forge means the deployed value only
        return None


def _choosable(fid: str, flow: dict, facts: dict) -> dict[str, dict]:
    """`{variable: {deployed, repos}}` for each variable a `branch` argument of this procedure
    takes whole: its REVIEWED value and the reviewed repositories it narrows within."""
    targets = {t.get("id"): t for t in facts["targets"] if isinstance(t, dict)}
    out: dict[str, dict] = {}
    for step in flow["steps"]:
        for item in step["checklist"]:
            t = targets.get(f"flow:{fid}:{step['id']}:{item['key']}") or {}
            for arg, symbolic in item["probe_args"].items():
                var = validate._whole_var_ref(symbolic)
                if var is None or arg not in CHOOSABLE_ARGS:
                    continue
                deployed = (t.get("args") or {}).get(arg)
                if not isinstance(deployed, str) or not deployed:
                    continue
                row = out.setdefault(var, {"deployed": deployed, "repos": set()})
                if row["deployed"] != deployed:
                    row["conflict"] = True
                repo = (t.get("args") or {}).get("repo")
                if isinstance(repo, str) and repo and item["probe"].startswith("forge_"):
                    row["repos"].add(repo)
    return {k: v for k, v in out.items() if not v.get("conflict")}


def _choices_for(row: dict) -> tuple[list[dict], str]:
    """`(choices, listed)`: the deployed value first, then the open, non-fork PRs' head branches
    of the ONE reviewed repository (#1076) in PR-number order, each shaped like a git ref name.
    `listed` says how far the PR list went — `complete`, `partial` (the page budget ran out) or
    `unavailable` (no forge, an unreachable one, or not exactly one reviewed repository), which
    leaves the deployed value alone."""
    choices = [{"index": 0, "value": row["deployed"], "source": "deployment"}]
    if len(row["repos"]) != 1:
        return choices, "unavailable"
    client = _forge_client()
    if client is None:
        return choices, "unavailable"
    try:
        pulls, complete = client.open_pull_heads(next(iter(row["repos"])))
    except Exception:  # noqa: BLE001 — an unreachable forge degrades to the deployed value
        return choices, "unavailable"
    numbers: set[int] = set()
    for pr in sorted(pulls, key=lambda p: p["number"]):
        branch = pr["branch"]
        # One choice per PULL REQUEST (its number is its identity), not per branch: two open PRs
        # on one head (different bases) are two choices, and the deployed branch's own PR is a
        # numbered choice beside choice 0 (the branch, unbound).
        if pr["number"] in numbers:
            continue
        if not missions._SLOT_SHAPES["branch"].fullmatch(branch):
            continue
        if ".." in branch or len(choices) >= MAX_CHOICES:
            continue
        numbers.add(pr["number"])
        choices.append(
            {
                "index": len(choices),
                "value": branch,
                "source": "pull_request",
                "number": pr["number"],
                "title": pr["title"],
                "repo": next(iter(row["repos"])),
            }
        )
    return choices, "complete" if complete else "partial"


def _input_choices(now: dict, record: dict, bundle: dict, index: int) -> dict:
    row = now["procedures"][index]
    fid = row["flow_id"]
    choosable = _choosable(fid, bundle["flows"][fid], record["review_facts"])
    inputs: dict[str, list[dict]] = {}
    listed: dict[str, str] = {}
    for var in sorted(choosable):
        inputs[var], listed[var] = _choices_for(choosable[var])
    # The digest binds what a pick MEANS — position, value, source and PR number — and not a
    # PR's title, so retitling a PR is not a stale list (the independent review on #1793).
    basis = {
        "listing": now["listing"],
        "index": index,
        "inputs": {
            var: [[c["index"], c["value"], c["source"], c.get("number")] for c in rows]
            for var, rows in inputs.items()
        },
    }
    return {
        "index": index,
        "listing": now["listing"],
        "inputs": inputs,
        "listed": listed,
        "deployed": {var: row["deployed"] for var, row in choosable.items()},
        "choices": _digest(basis),
    }


def input_choices(mission_id: str, index: object, listing_digest: object) -> dict:
    """The server-built choices for each choosable input of the procedure at `index` (#1076).

    BLOCKING, and it may make the bounded forge read — run it off the event loop, never inside
    the missions write lock (it takes none)."""
    now, record, bundle = _checked_listing(mission_id, index, listing_digest)
    return _input_choices(now, record, bundle, index)  # type: ignore[arg-type]


def _checked_listing(mission_id: str, index: object, listing_digest: object):
    if type(index) is not int or index < 0:
        raise ProcedureError("index must be a non-negative integer", status=422)
    if not isinstance(listing_digest, str) or not listing_digest:
        raise ProcedureError(
            "listing is required: the digest of the list you chose from", status=422
        )
    now, record, bundle = _listing(mission_id)
    if now["listing"] != listing_digest:
        raise ProcedureError("the procedure list changed; read it again", status=409)
    if now["reason"] is not None:
        raise ProcedureError(now["reason"], status=409)
    if index >= len(now["procedures"]):
        raise ProcedureError("no procedure has that index", status=422)
    if not now["procedures"][index]["ready"]:
        raise ProcedureError(now["procedures"][index]["reason"], status=409)
    return now, record, bundle


def _chosen(picks: object, choices_digest: object, offered: dict) -> dict[str, dict]:
    """`{variable: choice}` from the client's `{variable: {"choice": n}}` against the offered set.

    The shape carries a POSITION and nothing else: a value, a URL or a branch name in it is a
    422, so no caller — operator UI, or HQ in P5 — can put a target into this request."""
    if not isinstance(picks, dict) or not picks:
        raise ProcedureError("inputs is {variable: {choice: n}}", status=422)
    if offered["choices"] != choices_digest:
        raise ProcedureError(
            "the input choices changed (a pull request opened or closed, or the forge could not "
            "be read); read them again",
            status=409,
        )
    out: dict[str, dict] = {}
    for var, pick in picks.items():
        if var not in offered["inputs"]:
            raise ProcedureError(f"{var} is not a choosable input of this procedure", status=422)
        if not isinstance(pick, dict) or set(pick) != {"choice"}:
            raise ProcedureError("each input is {choice: n}: a position, never a value", status=422)
        n = pick["choice"]
        if type(n) is not int or not 0 <= n < len(offered["inputs"][var]):
            raise ProcedureError(f"{var}: no choice has that index", status=422)
        out[var] = offered["inputs"][var][n]
    return out


def resolve_picks(
    mission_id: str, index: object, listing_digest: object, inputs: object, choices: object
) -> dict[str, dict]:
    """`{variable: choice}` for the client's positions, checked against a FRESH read of the
    offered choices (the bounded forge read). BLOCKING and possibly slow: run it off the event
    loop and outside the missions pool; it takes no missions lock."""
    now, record, bundle = _checked_listing(mission_id, index, listing_digest)
    offered = _input_choices(now, record, bundle, index)  # type: ignore[arg-type]
    return _chosen(inputs, choices, offered)


def apply_listed(
    mission_id: str,
    index: object,
    listing_digest: object,
    *,
    inputs: object = None,
    choices: object = None,
    picked: dict[str, dict] | None = None,
) -> dict:
    """Apply the procedure at `index` of the listing whose digest the client echoes; optionally
    with per-objective inputs picked by index (`inputs` + `choices`, checked here, or `picked`
    already checked by `resolve_picks` — the route's split, so the forge read runs off the
    missions pool and the store write through it). The listing is re-checked either way."""
    if picked is None and inputs is not None:
        picked = resolve_picks(mission_id, index, listing_digest, inputs, choices)
    now, record, bundle = _checked_listing(mission_id, index, listing_digest)
    assert record is not None and bundle is not None  # a listing with procedures has both
    return _apply_from(mission_id, now, record, bundle, index, picked or {})  # type: ignore[arg-type]


def _apply_from(
    mission_id: str, now: dict, record: dict, bundle: dict, index: int, picked: dict
) -> dict:
    pid = now["project_id"]
    fid = now["procedures"][index]["flow_id"]
    flow = bundle["flows"][fid]
    revision = record["applied_paths"]["revision"]
    items = _reviewed_items(fid, flow, record["review_facts"])
    if picked:
        items = _with_picks(flow, items, picked)
    procedure = {
        "source": "playbook",
        "playbook_id": record["playbook_id"],
        "revision": revision,
        "flow_id": fid,
        "title": flow["title"],
        "flow": copy.deepcopy(flow),
        "digest": plan_doc.flow_digest(flow),
        "document": copy.deepcopy(bundle["_documents"][fid]),
        "variables": copy.deepcopy(bundle["variables"]),
        "manifest_format": bundle["format"],
    }
    # EVERY choosable variable records the value it took, picked or not, so applying with no
    # pick and picking choice 0 (the deployed value) are the same objective — a duplicate — and
    # never two (the independent review on #1793). Other variables record their source only.
    choosable = _choosable(fid, flow, record["review_facts"])
    inputs = []
    for n in _variables_used(flow):
        if n in picked:
            row = {"name": n, "source": picked[n]["source"], "value": picked[n]["value"]}
            if picked[n].get("number") is not None:
                # THE PR'S IDENTITY, not just its branch (Hermes on #1793): a branch reused by a
                # later PR, or two open PRs on one head, are different objectives.
                row["number"] = picked[n]["number"]
            inputs.append(row)
        elif n in choosable:
            inputs.append({"name": n, "source": "deployment", "value": choosable[n]["deployed"]})
        else:
            inputs.append({"name": n, "source": "deployment"})
    return missions.apply_procedure(
        mission_id,
        project_id=pid,
        procedure=procedure,
        inputs=inputs,
        items=items,
        reason=f"applied {record['playbook_id']}@{revision[:12]} · {fid}",
    )


def _bind_number(it: dict, picked: dict, branch_ref: object, var_steps: dict[str, set]) -> None:
    """Bind a PR-scoped forge item to ITS picked pull request's NUMBER (#1777 S1d).

    An item is about a picked variable when its symbolic `branch` is exactly that variable, or a
    step reference into a step whose branch-bearing items take it (the waiting `forge_review` on
    `{{steps.implement.head_branch}}`: the number rides in `pending.args`, and the bridge fills
    only the reference). Each item is bound to the ONE numbered pick it is about, so two picks in
    one apply bind their own evidence (Hermes on #1793). An item about none stays unbound — on
    another variable, or with no `branch` (the checkout's); binding it would make its gate unknown
    for ever. A step reference into a step that takes SEVERAL branch variables is left unbound
    too: whichever producer settles first names its output, so no single PR is meant."""
    probe = it["pending"]["probe"] if it["pending"] else it["probe"]
    if probe not in missions.PR_PROBES or not isinstance(branch_ref, str):
        return
    m = schema.STEP_TOKEN_RE.fullmatch(branch_ref)
    if m is not None and sum(m.group(1) in steps for steps in var_steps.values()) > 1:
        # The referenced step takes SEVERAL branch variables, picked or not: which PR its output
        # names is decided by whichever producer settles first, so no number is bound and the
        # item keeps branch resolution (the independent review on #1793).
        return
    about = []
    for var, pick in picked.items():
        if pick.get("number") is None:
            continue
        if branch_ref == "{{" + var + "}}" or (
            m is not None and m.group(1) in var_steps.get(var, set())
        ):
            about.append(pick)
    if len(about) != 1:  # a direct reference names one variable; a step token, one step's
        return
    pick = about[0]
    if isinstance(pick["number"], bool) or not isinstance(pick["number"], int):
        raise ProcedureError("a picked pull request has a number", status=422)
    if it["pending"]:
        args = it["pending"]["args"]
    else:
        if it["probe_args"] is None:
            it["probe_args"] = {}
        args = it["probe_args"]
    # A PR number belongs to ONE repository. An item that names another is left unbound; one that
    # names none (it would fall back to the checkout's origin, Hermes on #1793) is bound to the
    # pick's REVIEWED repository with the number — the authority the number was chosen within.
    if args.get("repo") not in (None, pick.get("repo")) or not pick.get("repo"):
        return
    args["repo"] = pick["repo"]
    args["number"] = pick["number"]
    if not it["pending"]:
        missions.validate_probe_args(it["probe"], args)


def _with_picks(flow: dict, items: list[dict], picked: dict) -> list[dict]:
    """The reviewed items with each chosen variable's `branch` arguments set to its choice."""
    symbolic = {
        (step["id"], item["key"]): item["probe_args"]
        for step in flow["steps"]
        for item in step["checklist"]
    }
    # Which steps take each variable as a branch: a step reference into one of them is about the
    # same pull request as the variable (`_bind_number`).
    var_steps: dict[str, set] = {}
    for step in flow["steps"]:
        for item in step["checklist"]:
            var = validate._whole_var_ref(item["probe_args"].get("branch"))
            if var is not None:
                var_steps.setdefault(var, set()).add(step["id"])
    out = []
    for it in items:
        it = copy.deepcopy(it)
        for arg, value in symbolic[(it["step"], it["key"])].items():
            var = validate._whole_var_ref(value)
            if var not in picked or arg not in CHOOSABLE_ARGS:
                continue
            value = picked[var].get("value")
            # RE-CHECKED HERE, not trusted from the caller (the independent review on #1793):
            # `picked` comes from `resolve_picks` today, and a later caller (HQ, P5) must not be
            # able to hand this a value the offered list could never have held.
            if not isinstance(value, str) or not missions._SLOT_SHAPES["branch"].fullmatch(value):
                raise ProcedureError(f"{var}: a picked value must be a branch name", status=422)
            if ".." in value:
                raise ProcedureError(f"{var}: a picked value must be a branch name", status=422)
            target = it["pending"]["args"] if it["pending"] else it["probe_args"]
            target[arg] = value
        _bind_number(it, picked, symbolic[(it["step"], it["key"])].get("branch"), var_steps)
        if not it["pending"]:
            missions.validate_probe_args(it["probe"], it["probe_args"])
        out.append(it)
    return out


def apply_local(mission_id: str, flow_id: object, doc: object) -> dict:
    """Apply a mission-local procedure the operator wrote: the bundle's own flow validator, and
    targets from the project's current bindings with bundle defaults refused."""
    scope = missions.procedure_scope(mission_id)
    pid = scope["project_id"]
    if not pid:
        raise ProcedureError(
            "the mission has no project; every objective names one (choose it first)", status=409
        )
    record, bundle, reason = _deployment(pid)
    if bundle is None and reason != NO_DEPLOYMENT:
        # The project HAS a playbook, and its declarations are what a mission-local procedure may
        # reference — but not while the deployment is unsettled or out of date.
        raise ProcedureError(reason or "the project's deployment is not usable", status=409)
    variables = list(bundle["variables"]) if bundle is not None else []
    if not isinstance(flow_id, str):
        raise ProcedureError("flow_id is required", status=422)
    try:
        flow = validate.flow_document(doc, flow_id, variables)
    except PlaybookFormatError as e:
        raise ProcedureError(f"the procedure is invalid: {e}", status=422) from None
    try:
        resolved = binding.resolve({"variables": variables}, pid, [])
        targets = binding.targets({"flows": {flow_id: flow}, "connections": []}, resolved)
    except (
        store.StoreError,
        missions.MissionError,
        template_vars.BindingUnusable,
        template_vars.ResolutionUnavailable,
        template_vars.VariableStoreUnsupported,
        template_vars.VariableError,
    ) as e:
        raise ProcedureError(f"the procedure's inputs cannot be bound: {e}", status=409) from None
    defaulted = sorted({v for t in targets for v in t["default_variables"]})
    if defaulted:
        raise ProcedureError(
            "a probe target would come from a playbook default; bind a project value for "
            + ", ".join(defaulted)
            + " first",
            status=409,
        )
    by_id = {t["id"]: t for t in targets}
    items = []
    for step in flow["steps"]:
        for item in step["checklist"]:
            t = by_id[f"flow:{flow_id}:{step['id']}:{item['key']}"]
            items.append(_item(step, item, t["args"], t["pending"]))
    if len(items) > missions.OBJECTIVES_MAX:
        raise ProcedureError("this procedure has more evidence than a mission holds", status=422)
    sources = {row["name"]: row.get("source") for row in resolved.public}
    procedure = {
        "source": "mission_local",
        "flow_id": flow_id,
        "title": flow["title"],
        "flow": copy.deepcopy(flow),
        "digest": plan_doc.flow_digest(flow),
        "document": copy.deepcopy(doc),
        "variables": copy.deepcopy(variables),
    }
    inputs = [
        {"name": n, "source": str(sources.get(n) or "missing")} for n in _variables_used(flow)
    ]
    return missions.apply_procedure(
        mission_id,
        project_id=pid,
        procedure=procedure,
        inputs=inputs,
        items=items,
        reason=f"mission-local procedure {flow_id} added by the operator",
    )


def plan(mission_id: str, revision: int | None = None) -> dict:
    """A plan revision plus each step's derived state (never stored), or `{"plan": None}`."""
    rev = missions.plan_revision(mission_id, revision)
    if rev is None:
        if revision is not None:
            raise ProcedureError("no such plan revision", status=404)
        return {"mission_id": mission_id, "plan": None}
    latest = rev if revision is None else missions.plan_revision(mission_id)
    rows = missions.objectives(mission_id)
    return {
        "mission_id": mission_id,
        "plan": rev,
        # Derived from TODAY's evidence rows, on whichever revision was asked for.
        "step_states": plan_doc.step_states(rev["document"], rows),
        "is_latest": latest is not None and latest["revision"] == rev["revision"],
    }


__all__ = ["ProcedureError", "apply_listed", "apply_local", "listing", "plan"]
