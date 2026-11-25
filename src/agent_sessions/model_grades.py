"""The operator's model → grade map (#1693, #1613 D18) — the `model_grades` prefs block.

``{engine: {model_id: "fast" | "strong" | "frontier"}}``, edited per agent in Settings → Agents.
It ships EMPTY: no catalog carries tiers, and a guessed tier would mislabel a model. A
specialist's grade is derived from this map at READ (``grade_of``), never stored on a version, so
a remap re-grades every specialist on that model at once.

* **Writes are patches**, merged under the one prefs lock (``prefs.mutate_block``): a save names
  only the rows it changes, with ``null`` clearing one, so two tabs grading different models never
  erase each other's work.
* **Strict on write, lenient on read.** A write names a registered agent and a well-formed model id
  and one of the three grades, or it is refused whole. A read drops anything malformed.
* ``default`` is never graded: it names whatever the agent picks, which is not one model.
* **Every commit bumps a revision.** The block is stored as ``{"rev": N, "map": {...}}``; each
  merge writes ``rev + 1`` under the same lock. `/api/config` and every save answer carry it, so a
  client showing whichever map has the HIGHEST revision is right for any order in which requests
  commit or answers arrive. Anything else stored reads as ``rev 0`` and an empty map.
"""

from __future__ import annotations

import re

from . import prefs

KEY = "model_grades"
#: Intern / Specialist / Senior in the SPA. Order matters: P5 compares `min_grade` by rank.
GRADES: tuple[str, ...] = ("fast", "strong", "frontier")
DEFAULT_MODEL = "default"
#: Bounds on what a hand-edited file or a hostile client can make every reader walk.
MAX_ENGINES = 32
MAX_MODELS_PER_ENGINE = 128

_ENGINE_RE = re.compile(r"\A[a-z][a-z0-9-]{1,23}\Z")
_MODEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}\Z")


class GradesRefused(ValueError):
    pass


def _coerce(raw: object) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for engine, models in list(raw.items())[:MAX_ENGINES]:
        if not isinstance(engine, str) or not _ENGINE_RE.fullmatch(engine):
            continue
        if not isinstance(models, dict):
            continue
        row = {
            m: g
            for m, g in list(models.items())[:MAX_MODELS_PER_ENGINE]
            if isinstance(m, str) and _MODEL_RE.fullmatch(m) and m != DEFAULT_MODEL and g in GRADES
        }
        if row:
            out[engine] = row
    return out


def _split(raw: object) -> tuple[int, dict[str, dict[str, str]]]:
    """``(rev, map)`` from the stored block; anything not of the stored shape is ``(0, {})``."""
    if not isinstance(raw, dict) or set(raw) != {"rev", "map"}:
        return 0, {}
    rev = raw["rev"]
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        return 0, {}
    return rev, _coerce(raw["map"])


def get() -> dict[str, dict[str, str]]:
    """The map every grade derivation reads (malformed rows dropped)."""
    return _split(prefs.read_block(KEY))[1]


def get_versioned() -> dict:
    """``{"rev", "map"}`` from ONE read, for a client that orders maps by commit."""
    rev, table = _split(prefs.read_block(KEY))
    return {"rev": rev, "map": table}


def agent_ids() -> set[str]:
    """The registered agents a grade may be written for (not terminals: they run no model)."""
    from . import engines

    return set(engines.ids_where(lambda m: m.identity.kind == "agent"))


def validate_patch(patch: object, known_engines: set[str] | None = None) -> str | None:
    """`None` when ``patch`` is a well-formed change, else the reason it is refused."""
    if known_engines is None:
        known_engines = agent_ids()
    if not isinstance(patch, dict) or not patch:
        return "model_grades must be an object naming at least one agent"
    if len(patch) > MAX_ENGINES:
        return "too many agents in one model_grades change"
    for engine, models in patch.items():
        if not isinstance(engine, str) or engine not in known_engines:
            return f"model_grades: unknown agent {engine!r}"
        if not isinstance(models, dict) or not models:
            return f"model_grades.{engine} must be an object naming at least one model"
        if len(models) > MAX_MODELS_PER_ENGINE:
            return f"model_grades.{engine}: too many models"
        for model, grade in models.items():
            if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
                return f"model_grades.{engine}: {model!r} is not a model id"
            if model == DEFAULT_MODEL:
                return "the agent's default model cannot be graded: it is not one model"
            if grade is not None and grade not in GRADES:
                return f"model_grades.{engine}.{model}: grade must be one of {', '.join(GRADES)}"
    return None


def apply_patch(patch: dict) -> dict:
    """Merge a validated patch under the prefs lock; ``null`` clears a row. Returns the committed
    ``{"rev", "map"}``: the revision is the stored one plus one, written in the same lock."""

    def merge(stored: object) -> dict:
        rev, cur = _split(stored)
        for engine, models in patch.items():
            row = dict(cur.get(engine, {}))
            for model, grade in models.items():
                if grade is None:
                    row.pop(model, None)
                else:
                    row[model] = grade
            if len(row) > MAX_MODELS_PER_ENGINE:
                raise GradesRefused(f"model_grades.{engine}: too many models")
            if row:
                cur[engine] = row
            else:
                cur.pop(engine, None)
        if len(cur) > MAX_ENGINES:
            raise GradesRefused("too many agents in model_grades")
        return {"rev": rev + 1, "map": cur}

    rev, table = _split(prefs.mutate_block(KEY, merge))
    return {"rev": rev, "map": table}


def grade_of(
    engine: str, model: str | None, grades: dict[str, dict[str, str]] | None = None
) -> str | None:
    """The grade of ``model`` on ``engine``, or ``None`` (ungraded). ``default`` is never graded.

    The caller passes the CANONICAL model id (a specialist stores one: the store resolves aliases
    through ``model_choice`` at write)."""
    if not model or model == DEFAULT_MODEL:
        return None
    table = grades if grades is not None else get()
    return table.get(engine, {}).get(model)


def rank(grade: str | None) -> int:
    """0 for ungraded, then 1…3 in `GRADES` order (P5 compares a role's `min_grade` by this)."""
    return GRADES.index(grade) + 1 if grade in GRADES else 0
