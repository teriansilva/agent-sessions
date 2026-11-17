"""HQ's clustering sweep (#1997): group each project's sessions by topic.

An interval loop on the ``autosort_loop`` pattern. Every pass, behind the gate
(``clusters.enabled`` and a configured AI-review endpoint, re-read each time):

1. :func:`collect` reads the listed, non-archived sessions once (the list's own scope), which
   project each is in, and whether a mission holds it.
2. :func:`session_clusters.reconcile` drops memberships that no longer describe anything.
3. Each project whose inputs changed (a fingerprint over its candidates and its stored clusters)
   gets ONE model call, at most :data:`SWEEP_CAP` projects per pass.

**Aliases, never keys.** The model sees ``s1…sN`` and ``c1…cN``; the reply is mapped back here,
so it cannot name a session outside the project's candidate set.

**The reply is data.** :func:`plan_from_reply` drops unknown aliases, duplicate assignments and
anything aimed at a placed session; a reply of the wrong shape writes nothing.

**The commit is fenced.** The model answers with no lock held. ``commit_sweep`` then re-reads the
store under its lock and refuses a plan made from an older state of the project, and members are
checked against a fresh :func:`collect`, so a session archived, moved to another project or taken
by a mission while the model was answering is not placed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import (
    ai_providers,
    aitasks,
    engines,
    metadata,
    missions,
    prefs,
    projects,
    prompts,
    review,
)
from . import session_clusters as sc

log = logging.getLogger("agent_sessions.clusters_loop")

#: Projects given a model call per scheduled pass; the rest wait for the next one.
SWEEP_CAP = 4
#: Projects one "re-cluster all" request sweeps inline; the rest are left to the schedule.
RECLUSTER_ALL_CAP = 12
#: Sessions of one project sent to the model, most recently active first.
MAX_CANDIDATES = 80
SUMMARY_CHARS = 400
TITLE_CHARS = 120
#: Entries of a reply that are read; more than this is not a grouping.
MAX_REPLY_CLUSTERS = 50
CALL_SPACING_S = 1.0
_BACKOFF_MAX_MULT = 8

#: One sweep per project at a time in this process (the scheduled pass and "re-cluster now").
#: Across processes the commit's basis check does the same job.
_LOCKS: dict[str, asyncio.Lock] = {}


class InvalidReply(Exception):
    """The model's reply is not the contract's shape. Nothing is written for it."""


@dataclass(frozen=True)
class Candidate:
    key: str
    title: str
    summary: str
    last_mtime: float


@dataclass
class Fleet:
    """One read of the listed sessions, as the sweep and the cluster routes need it."""

    live: dict[str, sc.Live] = field(default_factory=dict)
    #: Sessions the operator archived: positive evidence a membership is over.
    archived: set[str] = field(default_factory=set)
    #: project_id -> its candidates, most recently active first.
    candidates: dict[str, list[Candidate]] = field(default_factory=dict)


def loop_enabled() -> bool:
    """Env kill-switch: ``AGENT_SESSIONS_CLUSTERS_LOOP=0`` keeps the task from ever sweeping."""
    return (os.environ.get("AGENT_SESSIONS_CLUSTERS_LOOP", "1") or "1") != "0"


def ready() -> bool:
    return bool(prefs.get_clusters()["enabled"]) and bool(
        prefs.public_ai_review().get("configured")
    )


def collect() -> Fleet:
    """Read the fleet (blocking: run under ``asyncio.to_thread``).

    Scope is the session list's (`_scope_filter`: roots, exclusions, folder visibility), so a
    session the list hides is neither grouped nor sent to the model. Raises when the missions
    store cannot be read: without it a mission-held session cannot be told apart, and grouping it
    would be acting on a guess."""
    from .routes.sessions import _scope_filter

    held = missions.active_membership_rows()
    meta_index = metadata.load()
    aliases = metadata.load_aliases()
    project_index = projects.load()
    use_ai_title = prefs.renames_sessions()
    keep = _scope_filter()
    out = Fleet()
    for s in engines.scan_all_cached():
        key = engines.session_key(s)
        phys = engines.physical_key(key, aliases)
        m = meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()
        ref = projects.resolve(s.cwd, m.project_id, project_index, alias=m.project_alias).as_dict()
        archived = m.archived if m.archived is not None else s.archived
        if archived:
            out.archived.update((key, phys))
            continue
        if not keep(s.cwd, ref, archived=False):
            continue
        pid = sc.project_id_of(ref)
        mission = key in held or phys in held
        candidate = not mission and not m.review_excluded and bool(m.ai_title or m.ai_summary)
        out.live[key] = sc.Live(project_id=pid, mission=mission, candidate=candidate)
        if candidate:
            out.candidates.setdefault(pid, []).append(
                Candidate(
                    key=key,
                    title=sc.clean_text(
                        metadata.display_title(m, s.first_user_message, use_ai_title=use_ai_title),
                        TITLE_CHARS,
                    ),
                    summary=sc.clean_text(m.ai_summary, SUMMARY_CHARS),
                    last_mtime=float(s.last_mtime or 0),
                )
            )
    for rows in out.candidates.values():
        rows.sort(key=lambda c: (-c.last_mtime, c.key))
    return out


def _inputs(fleet: Fleet, project_id: str) -> list[Candidate]:
    return fleet.candidates.get(project_id, [])[:MAX_CANDIDATES]


def fingerprint(doc: dict, project_id: str, inputs: list[Candidate]) -> str:
    """What one project's sweep depends on: the sessions the model would be asked about (key,
    title, summary) and the project's stored clusters and memberships."""
    payload = [
        sc.basis(doc, project_id),
        sorted([c.key, c.title, c.summary] for c in inputs),
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass
class Prompted:
    """One project's model input and the alias maps needed to read the reply."""

    payload: dict
    sessions: dict[str, str]  # alias -> session key
    clusters: dict[str, str]  # alias -> tc_id
    placed: set[str]  # aliases of placed sessions


def build_input(doc: dict, project_id: str, inputs: list[Candidate]) -> Prompted:
    """The model's view of one project. Session keys and cluster ids stay here."""
    own = {
        cid: c
        for cid, c in sorted(
            doc["clusters"].items(), key=lambda kv: (kv[1].get("created_at") or 0, kv[0])
        )
        if c["project_id"] == project_id
    }
    cluster_alias = {cid: f"c{i}" for i, cid in enumerate(own, 1)}
    session_alias = {c.key: f"s{i}" for i, c in enumerate(inputs, 1)}
    placed: set[str] = set()
    current: dict[str, str] = {}
    fixed: dict[str, list[str]] = {cid: [] for cid in own}
    for key, alias in session_alias.items():
        m = doc["membership"].get(key)
        if m is None or m["cluster"] not in own:
            continue
        current[alias] = cluster_alias[m["cluster"]]
        if m["placed"]:
            placed.add(alias)
            fixed[m["cluster"]].append(alias)
    payload = {
        "clusters": [
            {
                "ref": cluster_alias[cid],
                "title": sc.shown_title(c),
                "summary": c["summary"],
                "title_fixed": sc.kept(c),
                "fixed_members": fixed[cid],
            }
            for cid, c in own.items()
        ],
        "sessions": [
            {
                "alias": session_alias[c.key],
                "title": c.title,
                "summary": c.summary,
                "current": current.get(session_alias[c.key]),
            }
            for c in inputs
        ],
    }
    return Prompted(
        payload=payload,
        sessions={alias: key for key, alias in session_alias.items()},
        clusters={alias: cid for cid, alias in cluster_alias.items()},
        placed=placed,
    )


def plan_from_reply(reply: object, prompted: Prompted) -> dict:
    """Turn the model's reply into a plan over session KEYS, or raise :class:`InvalidReply`.

    Shape errors refuse the whole reply. Everything else is dropped piece by piece: an unknown or
    repeated ``ref``, an unknown alias, a second assignment of one session (the first wins), any
    assignment of a placed session (it stays where the operator put it). Titles and summaries are
    cut and cleaned again when they are stored."""
    if not isinstance(reply, dict) or not isinstance(reply.get("clusters"), list):
        raise InvalidReply("the reply has no `clusters` list")
    entries = reply["clusters"]
    if len(entries) > MAX_REPLY_CLUSTERS:
        raise InvalidReply("the reply lists too many clusters")
    plan: dict = {"existing": {}, "new": []}
    taken: set[str] = set(prompted.placed)
    for entry in entries:
        if not isinstance(entry, dict):
            raise InvalidReply("a cluster in the reply is not an object")
        ref = entry.get("ref")
        members_raw = entry.get("members")
        if not isinstance(members_raw, list):
            raise InvalidReply("a cluster in the reply has no `members` list")
        members: list[str] = []
        for alias in members_raw:
            if not isinstance(alias, str) or alias not in prompted.sessions or alias in taken:
                continue
            taken.add(alias)
            members.append(prompted.sessions[alias])
        item = {
            "title": sc.clean_text(entry.get("title"), sc.TITLE_MAX),
            "summary": sc.clean_text(entry.get("summary"), sc.SUMMARY_MAX),
            "members": members,
        }
        if ref == "new":
            if item["title"] and len(plan["new"]) < sc.MAX_CLUSTERS_PER_PROJECT:
                plan["new"].append(item)
        elif isinstance(ref, str) and ref in prompted.clusters:
            cid = prompted.clusters[ref]
            if cid not in plan["existing"]:
                plan["existing"][cid] = item
        # Any other ref names nothing this project has: dropped, its members stay ungrouped.
    return plan


def _lock(project_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(project_id)
    if lock is None:
        lock = _LOCKS[project_id] = asyncio.Lock()
    return lock


async def sweep_project(project_id: str, fleet: Fleet, *, force: bool = False) -> dict:
    """One project's sweep. Returns ``{"project_id", "status", …}`` where status is one of
    ``unchanged`` / ``empty`` / ``committed`` / ``stale`` / ``error`` / ``unreadable``."""
    async with _lock(project_id):
        return await _sweep_project(project_id, fleet, force=force)


async def _sweep_project(project_id: str, fleet: Fleet, *, force: bool) -> dict:
    out: dict = {"project_id": project_id}
    try:
        doc = await asyncio.to_thread(sc.read_checked)
    except sc.ClustersUnreadable:
        return out | {"status": "unreadable"}
    inputs = _inputs(fleet, project_id)
    fp = fingerprint(doc, project_id, inputs)
    stored = doc["sweeps"].get(project_id) or {}
    if not force and stored.get("fingerprint") == fp:
        return out | {"status": "unchanged"}
    has_clusters = any(c["project_id"] == project_id for c in doc["clusters"].values())
    if len(inputs) < sc.MIN_MEMBERS and not has_clusters:
        # Nothing to group and nothing to keep in shape: no model call.
        return out | {"status": "empty"}
    prompted = build_input(doc, project_id, inputs)
    planned_basis = sc.basis(doc, project_id)
    try:
        reply = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("session_clusters")},
                {"role": "user", "content": json.dumps(prompted.payload)},
            ]
        )
        plan = plan_from_reply(reply, prompted)
    except review.NotConfiguredError:
        raise
    except InvalidReply as e:
        # Deterministic for these inputs: keep the fingerprint so the same reply is not asked for
        # again until something changes.
        await asyncio.to_thread(sc.record_sweep, project_id, fingerprint=fp, error=str(e))
        return out | {"status": "error", "error": str(e)}
    except review.ReviewError as e:
        # Transport or endpoint trouble: no fingerprint, so the next pass tries again.
        await asyncio.to_thread(sc.record_sweep, project_id, fingerprint="", error=str(e))
        return out | {"status": "error", "error": str(e)}
    # Read the fleet AGAIN: the model call took seconds, and a session may have been archived,
    # moved to another project or taken by a mission meanwhile.
    fresh = await asyncio.to_thread(collect)
    status = await asyncio.to_thread(
        sc.commit_sweep,
        project_id,
        planned_basis=planned_basis,
        plan=plan,
        input_keys=[c.key for c in inputs],
        live=fresh.live,
        archived=fresh.archived,
        # Over the inputs the model was ACTUALLY given, never the fresh ones: a session that
        # appeared, or a title or summary that changed, while the model was answering was not
        # judged, so it must leave the project dirty for the next pass.
        fingerprint=lambda d: fingerprint(d, project_id, inputs),
    )
    return out | {"status": status}


def _project_ids(fleet: Fleet, doc: dict) -> list[str]:
    """Every project with something to sweep: candidates, or stored clusters to keep in shape.
    Least recently swept first, so a capped pass cannot starve a project."""
    ids = set(fleet.candidates) | {c["project_id"] for c in doc["clusters"].values()}

    def swept_at(pid: str) -> float:
        at = (doc["sweeps"].get(pid) or {}).get("at")
        return float(at) if isinstance(at, int | float) else 0.0

    return sorted(ids, key=lambda pid: (swept_at(pid), pid))


@ai_providers.independent_model_work
async def run_sweep(*, project_id: str | None = None, force: bool = False) -> dict:
    """One pass over one project or all of them. The caller owns the gate (:func:`ready`).

    ``force`` ignores the fingerprints (re-cluster now) and retries a project once when an
    operator write made its first plan stale. Returns ``{"projects": [report…], "deferred": n}``;
    ``deferred`` counts projects left for a later pass by the cap."""
    fleet = await asyncio.to_thread(collect)
    try:
        await asyncio.to_thread(sc.reconcile, fleet.live, fleet.archived)
        doc = await asyncio.to_thread(sc.read_checked)
    except sc.ClustersUnreadable:
        return {"projects": [], "deferred": 0, "unreadable": True}
    ids = [project_id] if project_id is not None else _project_ids(fleet, doc)
    cap = RECLUSTER_ALL_CAP if force else SWEEP_CAP
    reports: list[dict] = []
    calls = 0
    deferred: list[str] = []
    for pid in ids:
        if calls >= cap:
            deferred.append(pid)
            continue
        if calls:
            await asyncio.sleep(CALL_SPACING_S)
        try:
            report = await sweep_project(pid, fleet, force=force)
            if force and report["status"] == "stale":
                fleet = await asyncio.to_thread(collect)
                report = await sweep_project(pid, fleet, force=True)
        except review.NotConfiguredError:
            reports.append({"project_id": pid, "status": "error", "error": "not configured"})
            break
        if report["status"] not in ("unchanged", "empty"):
            calls += 1
        reports.append(report)
    if force and deferred:
        # Left to the schedule, which must not skip them as unchanged.
        await asyncio.to_thread(sc.forget_sweeps, deferred)
    return {"projects": reports, "deferred": len(deferred)}


async def sweep() -> dict:
    """One gated scheduled pass (``skipped="disabled"`` when the gate is closed, so the loop never
    reaches the endpoint). Re-reads prefs, so a Settings change applies to the next pass."""
    if not ready():
        return {"projects": [], "deferred": 0, "skipped": "disabled"}
    async with aitasks.track("clusters", "sweep"):
        return await run_sweep()


async def run() -> None:
    """The background loop (started from the app lifespan). Sleeps ``clusters.interval_minutes``
    (re-read every iteration) times the failure backoff between passes."""
    if not loop_enabled():
        log.info("clusters loop disabled (AGENT_SESSIONS_CLUSTERS_LOOP=0)")
        return
    log.info("clusters loop armed (gated on the clusters prefs per sweep)")
    consecutive_failures = 0
    while True:
        interval_s = int(prefs.get_clusters()["interval_minutes"]) * 60
        await asyncio.sleep(interval_s * min(2**consecutive_failures, _BACKOFF_MAX_MULT))
        try:
            report = await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            log.exception("clusters sweep crashed")
            continue
        consecutive_failures = 0
        done = [p for p in report.get("projects", []) if p.get("status") == "committed"]
        if done:
            log.info("clusters sweep: regrouped %d project(s)", len(done))


def counts(doc: dict, live: Mapping[str, sc.Live]) -> dict[str, int]:
    """Listed sessions per cluster, by the same rule the session list stamps rows with."""
    out: dict[str, int] = {}
    for key, fact in live.items():
        cid = sc.effective_cluster(doc, (key,), fact.project_id, mission=fact.mission)
        if cid is not None:
            out[cid] = out.get(cid, 0) + 1
    return out
