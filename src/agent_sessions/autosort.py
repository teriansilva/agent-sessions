"""AI auto-sorter (#424 Phase 6).

Assigns UNASSIGNED sessions to one of the user's EXISTING project entities, reusing the
#356 AI-review gateway (``review.complete_json``) and the assignment seam (``metadata.patch``).

Safety contract:

* **Only acts on genuinely unassigned sessions** — no explicit ``project_id`` in the sidecar
  AND the session resolves to a *folder* fallback (not already adopted by a project). This
  NEVER overrides a manual assignment (drag #436 / menu #438 / a prior auto-sort), which is
  the issue's hard rule.
* **Confidence-gated** — a session is assigned only when the model returns a *known* project
  id with confidence ≥ the operator-set floor (``auto_sort.confidence_min``, #459); ambiguous
  sessions are left unassigned and surfaced as ``near_misses`` so the floor can be tuned.
* **Bounded + fail-soft** — at most ``max_per_pass`` endpoint calls per run (``auto_sort``
  prefs, #459), one at a time with a small spacing. A per-session endpoint/parse failure is
  skipped (logged), not fatal; an unconfigured endpoint is a no-op. The model output is
  treated strictly as data.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import engines, metadata, prefs, projects, review

log = logging.getLogger("agent_sessions.autosort")

# Spacing between consecutive endpoint calls so even a capped run can't burst the gateway.
CALL_SPACING_S = 1.0
# Cap on how many near-misses (a known pick below the confidence floor) the report returns,
# so a large unconfident batch can't bloat the response — the highest-confidence ones win.
NEAR_MISS_CAP = 8

# The confidence floor, per-run cap, and classifier prompt now live in the `auto_sort` prefs
# block and are read per run (`prefs.DEFAULT_AUTO_SORT_PROMPT` is the default prompt, #459).


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


async def _classify(
    cand: dict, projects_payload: list[dict], prompt: str
) -> tuple[str | None, float]:
    """Ask the gateway which project the session belongs to, using the operator-set classifier
    ``prompt`` (#459). Returns ``(project_id|None, confidence)``; a malformed reply degrades to
    ``(None, 0.0)``."""
    user = {
        "session": {"cwd": cand["cwd"], "title": cand["title"], "summary": cand["summary"]},
        "projects": projects_payload,
    }
    obj = await review.complete_json(
        [
            {"role": "system", "content": prompt},
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


async def run_sort(*, cap: int | None = None) -> dict:
    """One auto-sort pass. Returns a report dict ``{candidates, scanned, assigned[],
    near_misses[], …}``.

    The confidence floor, classifier prompt, and per-run cap are read from the ``auto_sort``
    prefs each call (#459), so a Settings change applies to the next run; ``cap`` overrides the
    pref (used by tests). The caller owns the *enabled* gate; this is a no-op when there are no
    projects or the reused AI-review endpoint isn't configured (``skipped`` says which). Only
    ever ASSIGNS; it never clears or changes an existing ``project_id``."""
    cfg = prefs.get_auto_sort()
    conf_min = float(cfg["confidence_min"])
    prompt = str(cfg["prompt"])
    limit = int(cfg["max_per_pass"]) if cap is None else cap

    project_index = projects.load()
    active = {pid: p for pid, p in project_index.items() if not p.archived}
    if not active:
        return _report([], 0, 0, 0, candidates=0, near_misses=[], skipped="no projects")

    cands = await asyncio.to_thread(_candidate_payload, project_index)
    window = cands[:limit]
    projects_payload = _projects_for_prompt(project_index)
    assigned: list[dict] = []
    near_misses: list[dict] = []
    low_conf = errors = scanned = 0
    for i, cand in enumerate(window):
        scanned += 1
        try:
            pid, conf = await _classify(cand, projects_payload, prompt)
        except review.NotConfiguredError:
            return _report(
                assigned,
                low_conf,
                errors,
                scanned - 1,
                candidates=len(cands),
                near_misses=near_misses,
                skipped="not configured",
            )
        except review.ReviewError:
            errors += 1
            log.debug("autosort: classify failed for %s — skipping", cand["key"], exc_info=True)
            continue
        if pid and pid in active and conf >= conf_min:
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
            # A KNOWN pick below the floor is an actionable near-miss (lower the threshold to
            # assign it); a null / unknown-id pick is counted but isn't a near-miss.
            if pid and pid in active:
                near_misses.append({"id": cand["key"], "project_id": pid, "confidence": conf})
        if i + 1 < len(window):
            await asyncio.sleep(CALL_SPACING_S)
    if assigned:
        log.info("autosort: assigned %d session(s) to projects", len(assigned))
    near_misses.sort(key=lambda n: n["confidence"], reverse=True)
    return _report(
        assigned,
        low_conf,
        errors,
        scanned,
        candidates=len(cands),
        near_misses=near_misses[:NEAR_MISS_CAP],
    )


def _report(assigned, low_conf, errors, scanned, *, candidates, near_misses=None, skipped=None):
    out = {
        "candidates": candidates,
        "scanned": scanned,
        "assigned": assigned,
        "low_confidence": low_conf,
        "errors": errors,
        "near_misses": near_misses or [],
    }
    if skipped:
        out["skipped"] = skipped
    return out
