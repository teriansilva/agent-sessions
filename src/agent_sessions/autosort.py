"""AI auto-sorter (#424 Phase 6).

Assigns UNASSIGNED sessions to one of the user's EXISTING project entities, reusing the
#356 AI-review gateway (``review.complete_json``) and the assignment seam (``metadata.patch``).

Safety contract:

* **Only acts on genuinely unassigned sessions** — no explicit ``project_id`` in the sidecar
  AND the session resolves to a *folder* fallback (not already adopted by a project). This
  NEVER overrides a manual assignment (drag #436 / menu #438 / a prior auto-sort), which is
  the issue's hard rule.
* **Confidence-gated** — a session is assigned only when the model returns a *known* project
  id with confidence ≥ ``CONFIDENCE_MIN``; ambiguous sessions are left unassigned.
* **Bounded + fail-soft** — at most ``cap`` endpoint calls per run, one at a time with a small
  spacing. A per-session endpoint/parse failure is skipped (logged), not fatal; an
  unconfigured endpoint is a no-op. The model output is treated strictly as data.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import engines, metadata, projects, review

log = logging.getLogger("agent_sessions.autosort")

# Below this the model's pick is treated as "not sure" → the session stays unassigned.
CONFIDENCE_MIN = 0.7
# Default per-run cap on endpoint calls (the background loop uses a smaller one).
DEFAULT_CAP = 8
# Spacing between consecutive endpoint calls so even a capped run can't burst the gateway.
CALL_SPACING_S = 1.0

_SYSTEM_PROMPT = (
    "You assign a coding session to ONE of the user's existing projects, or to none.\n"
    "You are given the session's working directory, title, and summary, plus a list of "
    "projects (id, name, and the folders each project has adopted).\n"
    "Choose the single best-matching project, weighing the working directory's relationship "
    "to the projects' adopted folders first, then the title/summary. If none clearly fits, "
    "return null — do NOT invent an id.\n"
    'Reply with ONLY a JSON object: {"project_id": "<one of the given ids, or null>", '
    '"confidence": <number 0..1>}. Be conservative: prefer null over a wrong guess.'
)


def _candidate_payload(project_index: dict[str, projects.Project]) -> list[dict]:
    """Enumerate unassigned, non-archived sessions (synchronous FS scan — run under
    ``asyncio.to_thread``). A candidate has NO explicit ``project_id`` and resolves to a
    folder fallback (so it doesn't already belong to a project via folder adoption)."""
    meta_index = metadata.load()
    aliases = metadata.load_aliases()
    out: list[dict] = []
    for s in engines.scan_all():
        key = engines.session_key(s)
        phys = engines.physical_key(key, aliases)
        m = meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()
        if m.archived:
            continue
        if m.project_id:  # explicit assignment — never override (the issue's hard rule)
            continue
        ref = projects.resolve(s.cwd, m.project_id, project_index, alias=m.project_alias)
        if ref.kind != "folder":  # already belongs to a project via folder adoption
            continue
        out.append(
            {
                "key": key,
                "cwd": s.cwd,
                "title": metadata.display_title(m, s.first_user_message),
                "summary": m.ai_summary or "",
            }
        )
    return out


def _projects_for_prompt(project_index: dict[str, projects.Project]) -> list[dict]:
    return [
        {"id": p.id, "name": p.name, "folders": list(p.folders)}
        for p in project_index.values()
        if not p.archived
    ]


async def _classify(cand: dict, projects_payload: list[dict]) -> tuple[str | None, float]:
    """Ask the gateway which project the session belongs to. Returns ``(project_id|None,
    confidence)``; a malformed reply degrades to ``(None, 0.0)``."""
    user = {
        "session": {"cwd": cand["cwd"], "title": cand["title"], "summary": cand["summary"]},
        "projects": projects_payload,
    }
    obj = await review.complete_json(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user)},
        ]
    )
    pid = obj.get("project_id")
    if not isinstance(pid, str) or not pid.strip():
        pid = None
    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    return pid, conf


async def run_sort(*, cap: int = DEFAULT_CAP) -> dict:
    """One auto-sort pass. Returns a report dict ``{candidates, scanned, assigned[], …}``.

    The caller owns the *enabled* gate; this is a no-op when there are no projects or the
    reused AI-review endpoint isn't configured (``skipped`` says which). Only ever ASSIGNS
    (sets an empty ``project_id``); it never clears or changes an existing one."""
    project_index = projects.load()
    active = {pid: p for pid, p in project_index.items() if not p.archived}
    if not active:
        return _report([], 0, 0, 0, candidates=0, skipped="no projects")

    cands = await asyncio.to_thread(_candidate_payload, project_index)
    window = cands[:cap]
    projects_payload = _projects_for_prompt(project_index)
    assigned: list[dict] = []
    low_conf = errors = scanned = 0
    for i, cand in enumerate(window):
        scanned += 1
        try:
            pid, conf = await _classify(cand, projects_payload)
        except review.NotConfiguredError:
            return _report(
                assigned,
                low_conf,
                errors,
                scanned - 1,
                candidates=len(cands),
                skipped="not configured",
            )
        except review.ReviewError:
            errors += 1
            log.debug("autosort: classify failed for %s — skipping", cand["key"], exc_info=True)
            continue
        if pid and pid in active and conf >= CONFIDENCE_MIN:
            try:
                await asyncio.to_thread(
                    metadata.patch, metadata.resolve_key(cand["key"]), project_id=pid
                )
                assigned.append({"id": cand["key"], "project_id": pid, "confidence": conf})
            except Exception:
                errors += 1
                log.warning("autosort: failed to assign %s → %s", cand["key"], pid, exc_info=True)
        else:
            low_conf += 1
        if i + 1 < len(window):
            await asyncio.sleep(CALL_SPACING_S)
    if assigned:
        log.info("autosort: assigned %d session(s) to projects", len(assigned))
    return _report(assigned, low_conf, errors, scanned, candidates=len(cands))


def _report(assigned, low_conf, errors, scanned, *, candidates, skipped=None):
    out = {
        "candidates": candidates,
        "scanned": scanned,
        "assigned": assigned,
        "low_confidence": low_conf,
        "errors": errors,
    }
    if skipped:
        out["skipped"] = skipped
    return out
