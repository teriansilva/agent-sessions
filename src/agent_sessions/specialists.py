"""Specialists (#1693, #1613 P3) — what a specialist IS, and the checks every write passes.

A specialist is a job in the Commander's pool: a title, a summary, the skills it brings (pinned
``{id, revision}``), the agent and model it runs on (plus an effort level for an API client), and
how many may run at once. ``specialists_store`` keeps its immutable versions; this module decides
what a version may contain. It names no engine: what an agent can do is asked of its manifest.

* **Strict on write.** A draft names exactly the fields below. Unknown fields, a skill missing
  from the library or changed since it was loaded, an agent that cannot start, a model the agent
  does not offer, and skills on an agent with no tool channel are each refused, and nothing is
  written.
* **Canonical at write.** An alias is stored as the model id it names, so a grade (derived at
  read through ``model_grades``) and a later launch both see one id.
* ``check_draft`` may ask an API client's CLI for its model list: call it OFF the event loop.
"""

from __future__ import annotations

import re
import unicodedata

from . import engines, model_choice, model_grades
from .playbooks.schema import SKILL_REF_RE

FIELDS = ("title", "summary", "skills", "engine", "model", "effort", "capacity")
TITLE_MAX = 60
SUMMARY_MAX = 280
SKILLS_MAX = 12
CAPACITY_MIN, CAPACITY_MAX = 1, 8
DEFAULT_MODEL = model_grades.DEFAULT_MODEL
_REVISION_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

#: The starters "New specialist" offers (#1693). A template prefills the JOB — title, summary,
#: suggested skills, the grade it is meant for — never the agent or model: those are the
#: operator's. The pool ships empty; nothing is created from these.
TEMPLATES: tuple[dict, ...] = (
    {
        "id": "intern",
        "title": "Intern",
        "summary": "Small, well-specified chores: renames, copy, dependency bumps.",
        "grade": "fast",
        "skills": [],
    },
    {
        "id": "implementer",
        "title": "Implementer",
        "summary": "Builds a phase to its acceptance criteria, red test first.",
        "grade": "strong",
        "skills": [],
    },
    {
        "id": "reviewer",
        "title": "Reviewer",
        "summary": "Independent second review of a change before it merges.",
        "grade": "frontier",
        "skills": [],
    },
    {
        "id": "bookkeeper",
        "title": "Bookkeeper",
        "summary": "Keeps ledgers, notes and release notes tidy.",
        "grade": "strong",
        "skills": [],
    },
)


class DraftRefused(ValueError):
    def __init__(self, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _text(value: object, field: str, limit: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise DraftRefused(f"{field} must be text")
    out = value.strip()
    if required and not out:
        raise DraftRefused(f"{field} is required")
    if len(out) > limit:
        raise DraftRefused(f"{field} is longer than {limit} characters")
    # Cs: a lone surrogate decodes from JSON (`"\\ud800"`) but can never be encoded back to
    # UTF-8, so a stored one would break every later read of the pool.
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in out):
        raise DraftRefused(f"{field} contains control or invalid characters")
    return out


def library_revisions() -> dict[str, str]:
    """``{skill_id: current_revision}`` of every readable skill in the local library."""
    from .gallery import skills

    return {
        s["id"]: s["revision"]
        for s in skills.listing().get("skills", [])
        if not s.get("error") and isinstance(s.get("revision"), str)
    }


def _skills(value: object, library: dict[str, str]) -> list[dict]:
    if not isinstance(value, list):
        raise DraftRefused("skills must be a list")
    if len(value) > SKILLS_MAX:
        raise DraftRefused(f"a specialist carries at most {SKILLS_MAX} skills")
    out: list[dict] = []
    seen: set[str] = set()
    for ref in value:
        if not isinstance(ref, dict) or set(ref) != {"id", "revision"}:
            raise DraftRefused("each skill is {id, revision}")
        sid, rev = ref["id"], ref["revision"]
        if not isinstance(sid, str) or not SKILL_REF_RE.fullmatch(sid):
            raise DraftRefused("a skill id is malformed")
        if not isinstance(rev, str) or not _REVISION_RE.fullmatch(rev):
            raise DraftRefused(f"skill {sid}: the revision is malformed")
        if sid in seen:
            raise DraftRefused(f"skill {sid} is listed twice")
        seen.add(sid)
        if sid not in library:
            raise DraftRefused(f"skill {sid} is not in your skills library")
        if library[sid] != rev:
            # Pinned revisions stay honest: the library keeps only its current tree, so a version
            # may only pin what is current right now.
            raise DraftRefused(f"skill {sid} changed since you loaded it: reload and save again")
        out.append({"id": sid, "revision": rev})
    return out


def _runs_on(engine: object, model: object, effort: object, has_skills: bool) -> tuple:
    if not isinstance(engine, str) or not engine:
        raise DraftRefused("choose the agent it runs on")
    prov = engines.get(engine)
    manifest = getattr(prov, "manifest", None)
    if prov is None or manifest is None or manifest.identity.kind != "agent":
        raise DraftRefused(f"unknown agent {engine!r}")
    if not engines.registry.can_start(prov):
        raise DraftRefused(f"{engine} cannot start a session on this host")
    if has_skills and manifest.tools_mcp is None:
        raise DraftRefused(f"{engine} cannot load skills: it has no tool channel")
    try:
        if manifest.runtime == "api":
            chosen, level = model_choice.select_api_choice(prov, model, effort)
        else:
            if effort is not None:
                raise DraftRefused(f"{engine} takes no effort level")
            chosen, level = model_choice.select(prov, model).model, None
    except model_choice.ModelRefused as e:
        raise DraftRefused(e.detail) from None
    return engine, chosen or DEFAULT_MODEL, level


def check_draft(draft: object, *, library: dict[str, str] | None = None) -> dict:
    """The version body a draft becomes, or `DraftRefused`. Off the loop (it may ask a CLI)."""
    if not isinstance(draft, dict):
        raise DraftRefused("expected a specialist object")
    unknown = set(draft) - set(FIELDS)
    if unknown:
        raise DraftRefused(f"unknown field(s): {', '.join(sorted(unknown))}")
    missing = [f for f in FIELDS if f not in draft]
    if missing:
        raise DraftRefused(f"missing field(s): {', '.join(missing)}")
    title = _text(draft["title"], "title", TITLE_MAX, required=True)
    summary = _text(draft["summary"], "summary", SUMMARY_MAX, required=False)
    cap = draft["capacity"]
    if isinstance(cap, bool) or not isinstance(cap, int) or not CAPACITY_MIN <= cap <= CAPACITY_MAX:
        raise DraftRefused(f"capacity must be {CAPACITY_MIN}…{CAPACITY_MAX}")
    skills = _skills(draft["skills"], library if library is not None else library_revisions())
    engine, model, effort = _runs_on(draft["engine"], draft["model"], draft["effort"], bool(skills))
    return {
        "title": title,
        "summary": summary,
        "skills": skills,
        "engine": engine,
        "model": model,
        "effort": effort,
        "capacity": cap,
    }
