"""The synthetic application mission (#1950 acceptance scenario), with NO personal content.

Same flow, roles and pool as the operator's own application project: intake → posting → cv ‖
cover_letter → documents → review → pull_request → approve → submit → record. Roles:
`career-archive` (fast), `cv-writing` (strong), `application-review` (frontier); `review` is
`distinct_from` `cv` and `cover_letter` by specialist, with `rework → cv` (3 rounds). Pool, all on
claude-api: career-archivist (sonnet, capacity 2), cv-writer (sonnet, 2), application-reviewer
(opus, 1). Grades: opus frontier, sonnet strong, haiku fast.
"""

from __future__ import annotations

import copy
import json
import tomllib

from agent_sessions import mission_plan_document as plan_doc
from agent_sessions import mission_team as team
from agent_sessions import missions
from agent_sessions.playbooks import validate

FLOW = """format = 5
title = "Application"

[[steps]]
id = "intake"
title = "Intake"
actor = { kind = "operator" }
[[steps.checklist]]
key = "intake_done"
title = "The posting is chosen"
probe = "none"

[[steps]]
id = "posting"
title = "Read the posting"
actor = { kind = "agent" }
role = { skills = ["career-archive"], min_grade = "fast" }
after = ["intake"]
[[steps.checklist]]
key = "posting_read"
title = "The posting is summarised"
probe = "supervisor_judged"

[[steps]]
id = "cv"
title = "Tailor the CV"
actor = { kind = "agent" }
role = { skills = ["cv-writing"], min_grade = "strong" }
after = ["posting"]
[[steps.checklist]]
key = "cv_ready"
title = "The CV is tailored"
probe = "supervisor_judged"

[[steps]]
id = "cover_letter"
title = "Cover letter"
actor = { kind = "agent" }
role = { skills = ["cv-writing"], min_grade = "strong" }
after = ["posting"]
[[steps.checklist]]
key = "letter_ready"
title = "The letter is written"
probe = "supervisor_judged"

[[steps]]
id = "documents"
title = "Assemble the documents"
actor = { kind = "agent" }
role = { skills = ["career-archive"], min_grade = "fast" }
after = ["cv", "cover_letter"]
[[steps.checklist]]
key = "docs_ready"
title = "The documents are together"
probe = "supervisor_judged"

[[steps]]
id = "review"
title = "Review the application"
actor = { kind = "agent" }
role = { skills = ["application-review"], min_grade = "frontier" }
after = ["documents"]
distinct_from = [
  { step = "cv", constraint = "specialist" },
  { step = "cover_letter", constraint = "specialist" },
]
rework = { to = "cv", when = "review_ok", max_rounds = 3 }
[[steps.checklist]]
key = "review_ok"
title = "The review passes"
probe = "supervisor_judged"

[[steps]]
id = "pull_request"
title = "Open the pull request"
actor = { kind = "agent" }
role = { skills = ["career-archive"], min_grade = "fast" }
after = ["review"]
[[steps.checklist]]
key = "pr_ready"
title = "The pull request is open"
probe = "supervisor_judged"

[[steps]]
id = "approve"
title = "Approve"
actor = { kind = "operator" }
after = ["pull_request"]
[[steps.checklist]]
key = "approved"
title = "The operator approves"
probe = "none"

[[steps]]
id = "submit"
title = "Submit"
actor = { kind = "operator" }
after = ["approve"]
[[steps.checklist]]
key = "submitted"
title = "Submitted"
probe = "none"

[[steps]]
id = "record"
title = "Record the application"
actor = { kind = "agent" }
role = { skills = ["career-archive"], min_grade = "fast" }
after = ["submit"]
[[steps.checklist]]
key = "recorded"
title = "The application is recorded"
probe = "supervisor_judged"
"""

ENGINE = "claude-api"
GRADES = {ENGINE: {"opus": "frontier", "sonnet": "strong", "haiku": "fast"}}
MODELS = {"opus": ("low", "high"), "sonnet": ("low", "high"), "haiku": ()}
LIBRARY = {"career-archive": "r1", "cv-writing": "r1", "application-review": "r1"}


def procedure(flow_text: str = FLOW, fid: str = "application") -> dict:
    doc = tomllib.loads(flow_text)
    flow = validate.flow_snapshot(doc, fid, [], roles_only=True, manifest_format=None)
    return {
        "source": "mission_local",
        "flow_id": fid,
        "title": flow["title"],
        "flow": flow,
        "digest": plan_doc.flow_digest(flow),
        "document": copy.deepcopy(doc),
        "variables": [],
    }


def document(flow_text: str = FLOW, *, objectives: int = 1) -> dict:
    proc = procedure(flow_text)
    objs = [
        {"id": f"o{i + 1}", "project_id": "p-synthetic01", "procedure": proc, "inputs": []}
        for i in range(objectives)
    ]
    return plan_doc.build(objs, {o["id"]: "apply the application" for o in objs})


def spec(sid, model, skills, *, capacity=1, version=1, archived=False, engine=ENGINE, effort=None):
    return {
        "id": sid,
        "revision": 1,
        "archived": archived,
        "current": {
            "version": version,
            "title": sid.replace("-", " ").title(),
            "summary": "",
            "skills": [{"id": s, "revision": LIBRARY.get(s, "r1")} for s in skills],
            "engine": engine,
            "model": model,
            "effort": effort,
            "capacity": capacity,
            "created_at": 1.0,
        },
    }


def pool(**over) -> list[dict]:
    p = {
        "career-archivist": spec("career-archivist", "sonnet", ["career-archive"], capacity=2),
        "cv-writer": spec("cv-writer", "sonnet", ["cv-writing"], capacity=2),
        "application-reviewer": spec(
            "application-reviewer", "opus", ["application-review"], capacity=1
        ),
    }
    for k, v in over.items():
        p[k.replace("_", "-")] = v
    return [v for v in p.values() if v is not None]


def client(engine=ENGINE, *, ready=True, skills=True, models=MODELS, usage="ok", why=None):
    return team.Client(
        engine,
        "Claude — API",
        skills,
        ready,
        why,
        dict(models) if models is not None else None,
        usage,
    )


def context(*, specs=None, grades=None, clients=None, library=LIBRARY, pool_max=6) -> team.Context:
    return team.Context(
        grades=copy.deepcopy(GRADES if grades is None else grades),
        clients=clients if clients is not None else {ENGINE: client()},
        library=library,
        pool=specs if specs is not None else pool(),
        pool_max=pool_max,
    )


def store_plan(mid: str, doc: dict, *, author: str = "operator") -> int:
    """Append `doc` as the next revision, the way every writer does (validated in its tx)."""
    con = missions._ready(None)
    try:
        con.execute("BEGIN IMMEDIATE")
        n = missions._append_plan_revision_tx(
            con, mid, json.loads(json.dumps(doc)), author_kind=author, reason="test", ts=1.0
        )
        con.execute("COMMIT")
        return n
    finally:
        con.close()
