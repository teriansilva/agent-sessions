"""The Forgejo review loop at run time (#2202): a verified delivery → its decision → the forge's
current revision → the dispatch admission → the reviewer specialist's API session.

Read `docs/invariants/forge-review-loop.md` before changing anything here.

**Order of checks, each before any effect:**

1. The route has verified the signature (`forge_hook.signature_ok`) and that the automation is an
   enabled webhook automation; nothing here runs for an unsigned delivery.
2. `forge_hook.parse` types the body; a body it refuses is a ``refused`` run (claimed by the
   body's fingerprint, so a replay records nothing twice).
3. `review_loop.classify` says what the delivery is to this automation. Chatter is not a run.
4. **The forge says what is current** (`current_revision`): the delivery's revision must BE the
   item's current one, read through the operator's configured read-only forge client, or nothing
   launches (a late A after B is ``ignored``). A forge that cannot be read, is not configured, or is
   another origin than the delivery's repository fails CLOSED (``refused``).
5. `automations_store.begin_review` claims the revision, re-reads consent, reserves one of the
   item's lifetime reviews and writes the review record — ONE transaction. Only then:
6. the reviewer launches through the specialist API launch seam (`mission_structured.
   automation_start` with the pinned version), under the automation's effect lock and authority,
   never bypassed. A crash in the launch is ``interrupted``: never relaunched, still counted.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from urllib.parse import urlsplit

from . import automation_effect_lock as effect_lock
from . import automation_runner as runner
from . import automations as model
from . import automations_store as store
from . import engines, forge, forge_hook, prefs, review_loop

log = logging.getLogger("agent_sessions.review_loop_runner")

#: Why a delivery could not be checked against the forge (prefix; the cause follows).
UNCONFIRMED = "could not confirm the item's current revision"


def _origin(url: str) -> str:
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}".lower()


def forge_client(repo_origin: str = "") -> tuple[forge.ForgeClient | None, str]:
    """The operator's configured forge client, or why there is none. When ``repo_origin`` is given
    (the delivery's ``repository.html_url``), the configured forge must be that origin: a token
    minted for one host is never sent to another, and a delivery from another forge is not ours."""
    try:
        cfg = prefs.get_forge()
    except Exception as e:  # noqa: BLE001 — an unreadable prefs file: no forge
        return None, f"the forge settings could not be read ({type(e).__name__})"
    if not cfg.get("enabled") or not str(cfg.get("base_url") or "").strip():
        return None, "no forge is configured (Settings → Forge)"
    if cfg.get("kind") not in ("forgejo", "gitea"):
        return None, "the configured forge is not a Forgejo instance"
    if repo_origin and _origin(str(cfg["base_url"])) != repo_origin:
        return None, "the delivery comes from another forge than the one configured"
    try:
        return (
            forge.ForgeClient(
                kind=str(cfg["kind"]),
                base_url=str(cfg["base_url"]),
                token=str(cfg.get("token") or ""),
            ),
            "",
        )
    except forge.ForgeError as e:
        return None, str(e)


def current_revision(repo: str, item: str, repo_origin: str) -> tuple[str | None, bool, str]:
    """``(revision, open, why)`` of ``item`` NOW, from the forge. ``revision`` is None when it could
    not be established — the caller fails closed. BLOCKING."""
    client, why = forge_client(repo_origin)
    if client is None:
        return None, False, why
    kind, _, number = item.partition("#")
    fact = (
        client.review_pull(repo, int(number))
        if kind == "pr"
        else client.issue_by_number(repo, int(number))
    )
    if not fact.observed:
        return None, False, fact.detail or "the forge did not answer"
    extra = fact.extra or {}
    rev = extra.get("head_sha") if kind == "pr" else extra.get("revision")
    if not isinstance(rev, str) or not rev:
        return None, False, "the forge did not report a revision"
    return rev, bool(fact.value), ""


def _fingerprint(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:32]


def _facts(ev: forge_hook.Event | None, headers: dict) -> dict:
    """What a run may show about its delivery: typed values, and the unsigned headers only in a
    closed alphabet, marked as recorded-not-trusted."""
    out = {
        "event": forge_hook.display(headers.get(forge_hook.EVENT_HEADER)),
        "delivery": forge_hook.display(headers.get(forge_hook.DELIVERY_HEADER)),
        "headers": "recorded, not trusted",
    }
    if ev is not None and ev.kind != "other":
        out.update({"kind": ev.kind, "action": ev.action, "item": ev.item})
        if ev.revision:
            out["revision"] = ev.revision
    return out


async def handle(aid: str, body: bytes, headers: dict, *, registry) -> None:
    """Act on one VERIFIED delivery. Never raises; every outcome is recorded or logged."""
    try:
        await _handle(aid, body, headers, registry=registry)
    except Exception:  # noqa: BLE001 — a delivery must never take the app down
        log.warning("review loop %s: delivery handling failed", aid, exc_info=True)


async def _handle(aid: str, body: bytes, headers: dict, *, registry) -> None:
    # Classified against one automation revision, admitted only under that same revision: if the
    # operator changed (and re-approved) the automation during the forge read, the delivery is
    # classified ONCE more against the current configuration — never admitted under a config it
    # was not checked against (Hermes on #2206).
    for _attempt in range(2):
        if not await _handle_once(aid, body, headers, registry=registry):
            return
    log.warning("review loop %s: the automation kept changing; delivery dropped", aid)


async def _handle_once(aid: str, body: bytes, headers: dict, *, registry) -> bool:
    """One classification and admission. Returns True when the automation changed under it and
    the delivery should be classified again."""
    fp = _fingerprint(body)
    try:
        ev = forge_hook.parse(body)
    except forge_hook.Rejected as e:
        await asyncio.to_thread(
            store.note_delivery, aid, _facts(None, headers)["event"], f"rejected: {e.reason}"
        )
        await asyncio.to_thread(
            store.record_delivery,
            aid,
            slot=f"rejected:{fp}",
            outcome="refused",
            reason=f"rejected: {e.reason}; nothing started",
            inputs=_facts(None, headers),
        )
        return False
    row = await asyncio.to_thread(store.get, aid)
    config = row["config"]
    if config is None or config["action"]["kind"] != "forge_review_loop":
        return False
    action = config["action"]
    dec = review_loop.classify(action, ev)
    facts = _facts(ev, headers)
    await asyncio.to_thread(
        store.note_delivery, aid, facts["event"] or ev.kind, dec.reason or dec.kind
    )
    if dec.kind == "ignore":
        return False
    if dec.kind == "rejected":
        await asyncio.to_thread(
            store.record_delivery,
            aid,
            slot=f"rejected:{fp}",
            outcome="refused",
            reason=f"rejected: {dec.reason}",
            inputs=facts,
        )
        return False
    origin = _origin(_repo_url(body))
    if dec.kind == "verdict":
        return await _verdict(aid, row, action, ev, facts, fp, origin, registry=registry)
    # THE FORGE SAYS WHAT IS CURRENT — never the order deliveries arrived in.
    rev, is_open, why = await asyncio.to_thread(current_revision, action["repo"], dec.item, origin)
    if rev is None:
        await asyncio.to_thread(
            store.record_delivery,
            aid,
            slot=f"unconfirmed:{fp}",
            outcome="refused",
            reason=f"{UNCONFIRMED} ({why}); nothing started",
            inputs=facts,
        )
        return False
    if rev != dec.revision or not is_open:
        why = (
            f"stale delivery: it is for {dec.item} at {dec.revision}, but the forge says the "
            f"current revision is {rev}"
            if rev != dec.revision
            else f"{dec.item} is closed on the forge"
        )
        await asyncio.to_thread(
            store.record_delivery,
            aid,
            slot=f"stale:{dec.slot}",
            outcome="ignored",
            reason=why,
            inputs=facts,
        )
        return False
    res = await asyncio.to_thread(
        store.begin_review,
        aid,
        slot=dec.slot,
        repo=action["repo"],
        item=dec.item,
        revision=dec.revision,
        expect_revision=row["revision"],
        inputs=facts,
    )
    if res.get("stale_config"):
        return True
    run = res.get("run")
    if res.get("capped"):
        _announce_once(
            aid,
            f"capped:{dec.item}",
            f"Review loop reached its limit: {row['name']}",
            res.get("reason") or f"{dec.item} reached its review limit",
        )
    if run is not None and run["state"] == "dispatching":
        runner.spawn(run, registry=registry)
    return False


def _announce_once(aid: str, what: str, title: str, reason: str) -> None:
    """One bell row per ``(automation, what)``: server-derived key, no forge text in it."""
    key = f"automation:{aid}:loop:{what}"
    try:
        if not runner._in_bell(key):
            runner._announce(title, reason, key)
    except Exception:  # noqa: BLE001 — the run is recorded; the bell must not undo that
        log.warning("review loop %s: notification %s failed", aid, key, exc_info=True)


async def _verdict(
    aid: str,
    row: dict,
    action: dict,
    ev: forge_hook.Event,
    facts: dict,
    fp: str,
    origin: str,
    *,
    registry,
) -> bool:
    """The return leg's acceptance (#2202 PR 2): the marker, the formal type, the forge's current
    revision, then ONE transaction (`accept_verdict`) before anything is sent."""
    if ev.marker is None:
        if ev.marker_problem == "no marker":
            # The reviewer's other comments are chatter, like anyone's.
            await asyncio.to_thread(
                store.note_delivery, aid, facts["event"] or ev.kind, "reviewer post, no verdict"
            )
            return False
        await asyncio.to_thread(
            store.record_delivery,
            aid,
            slot=f"verdict-bad:{fp}",
            outcome="ignored",
            reason=f"ignored: {ev.marker_problem}",
            inputs=facts,
        )
        return False
    record, kind, number, rev, verdict = ev.marker
    item = f"{kind}#{number}"
    why = ""
    if item != ev.item:
        why = f"ignored: the marker names {item}, but the verdict was posted on {ev.item}"
    elif ev.kind == "pr_review":
        expected = forge_hook.REVIEW_TYPES.get(ev.review_type)
        if expected is None:
            why = "ignored: a comment-only review is not a verdict"
        elif expected != verdict:
            why = (
                f"ignored: the marker says {verdict}, but the review was submitted as "
                f"{ev.review_type.removeprefix('pull_request_review_')}"
            )
    if why:
        await asyncio.to_thread(
            store.record_delivery,
            aid,
            slot=f"verdict-bad:{fp}",
            outcome="ignored",
            reason=why,
            inputs=facts,
        )
        return False
    current, is_open, cwhy = await asyncio.to_thread(current_revision, action["repo"], item, origin)
    if current is None:
        await asyncio.to_thread(
            store.record_delivery,
            aid,
            slot=f"unconfirmed:{fp}",
            outcome="refused",
            reason=f"{UNCONFIRMED} ({cwhy}); nothing sent",
            inputs=facts,
        )
        return False
    if not is_open:
        # A late verdict on a merged or closed item never asks anyone to fix and push.
        await asyncio.to_thread(
            store.record_delivery,
            aid,
            slot=f"closed-verdict:{fp}",
            outcome="ignored",
            reason=f"ignored: {item} is closed on the forge; the verdict is not returned",
            inputs=facts,
        )
        return False
    res = await asyncio.to_thread(
        store.accept_verdict,
        aid,
        slot=f"verdict:{action['repo'].lower()}:{kind}:{number}:{record}",
        repo=action["repo"],
        expect_revision=row["revision"],
        record=record,
        item=item,
        revision=rev,
        verdict=verdict,
        current=current,
        inputs={
            **facts,
            "item": item,
            "revision": rev,
            "verdict": verdict,
            "session_marker": ev.session_marker,
            "head_ref": ev.head_ref,
            "head_repo": ev.head_repo,
        },
    )
    if res.get("stale_config"):
        return True
    run = res.get("run")
    if run is not None and run["state"] == "dispatching":
        runner.spawn(run, registry=registry)
    return False
    return False


def _repo_url(body: bytes) -> str:
    """``repository.html_url`` from a body `forge_hook.parse` already accepted, or ""."""
    import json

    try:
        repo = json.loads(body).get("repository") or {}
    except (ValueError, AttributeError):
        return ""
    url = repo.get("html_url") if isinstance(repo, dict) else None
    return url if isinstance(url, str) and len(url) <= 2000 else ""


# ---- the launch (an automation action) ----------------------------------------------------------


def check_save(config: dict) -> None:
    """Save-time checks for a review loop. Raises ``AutomationError``; BLOCKING."""
    from .routes import missions as mroutes

    action = config["action"]
    try:
        _pid, cwd = mroutes._resolve_cwd(action["project_id"])
    except Exception as e:  # noqa: BLE001 — the mission store's own refusal, reworded
        raise model.AutomationError(str(e), status=getattr(e, "status", 422)) from None
    if not cwd or not runner.scope_ok(cwd):
        raise model.AutomationError("that project's folder is outside your project folders")
    pin = model.specialist_pin(action["reviewer"])
    if pin.get("missing"):
        raise model.AutomationError(f"the reviewer specialist can't be used: {pin['missing']}")
    prov = engines.get(str(pin.get("engine") or ""))
    if prov is None or not runner._structured().is_api(prov):
        raise model.AutomationError(
            "the reviewer specialist must run on an API client: nothing unattended drives a "
            "terminal"
        )
    ok, why = runner.target_state(prov.engine_id)
    if not ok:
        raise model.AutomationError(why)
    client, why = forge_client()
    if client is None:
        raise model.AutomationError(
            f"a review loop reads each item's current revision from your forge: {why}"
        )


async def launch_review(run: dict, config: dict, pins: dict, *, registry) -> tuple[str, str]:
    """The reviewer's API session for a review record already admitted (`begin_review`) — or, for
    an accepted verdict (``leg: return``), its delivery to the originating session."""
    if (run.get("inputs") or {}).get("leg") == "return":
        return await return_verdict(run, config, pins, registry=registry)
    rid = run["id"]
    action = config["action"]
    inputs = run.get("inputs") or {}
    record, item, rev = inputs.get("record"), inputs.get("item"), inputs.get("revision")
    round_no, cap = inputs.get("round"), inputs.get("cap")
    if not (record and item and rev and round_no and cap):
        return "refused", "this run has no admitted review record"
    spec = pins.get("specialist") or {}
    pinned = pins.get("cwd") or {}
    if spec.get("missing"):
        return "refused", f"the reviewer specialist can't be used: {spec['missing']}"
    async with effect_lock.runner(run["automation_id"]) as held:
        if not held:
            return "skipped", runner.LOCK_BUSY
        stop = await asyncio.to_thread(runner._authority_outcome, run)
        if stop:
            return stop
        engine = str(spec.get("engine") or "")
        prov = engines.get(engine)
        if prov is None or not runner._structured().is_api(prov):
            return "refused", "the reviewer specialist is not on an API client"
        ok, why = await asyncio.to_thread(runner.target_state, engine)
        if not ok:
            return "refused", why
        real = pinned.get("real")
        if not real:
            return "refused", "the project has no folder to work in"
        ok, why = await asyncio.to_thread(runner.folder_ok, real)
        if not ok:
            return "refused", why
        brief = review_loop.review_brief(
            repo=action["repo"],
            item=item,
            revision=rev,
            round_no=int(round_no),
            rounds=int(cap),
            record=record,
        )

        def on_key(key: str) -> None:
            store.link(rid, session_key=key)
            store.set_review_session(record, key)

        def fenced() -> tuple[bool, str]:
            ok, why = runner._still_authorized(run)
            if not ok:
                return False, why
            moved = runner._real_still_pinned(pinned)
            if moved:
                return False, moved
            return runner.folder_ok(real)

        model_id = spec.get("model")
        try:
            out = await runner._structured().automation_start(
                binding=runner.binding(run),
                run_id=rid,
                engine=engine,
                cwd=real,
                brief=brief,
                on_key=on_key,
                check=fenced,
                model=None if model_id in (None, "default") else str(model_id),
                effort=spec.get("effort"),
                specialist=(str(spec["id"]), int(spec["version"])),
            )
        except runner.headless_dispatch.DispatchError as e:
            return "refused", str(e)
    for fact in ("launched", "started", "briefed"):
        if getattr(out, fact):
            await asyncio.to_thread(store.add_step, rid, fact, out.key)
    if out.teardown:
        await asyncio.to_thread(
            store.add_step,
            rid,
            "worker_stopped" if out.teardown == "stopped" else "stop_failed",
            out.key,
        )
    if out.ok:
        words = f"{spec.get('name') or spec.get('id')} started on {item} at {rev}"
        runner._delivered(rid, "ok", words)
        return "ok", words
    reason = out.reason or f"the session reached {out.state}"
    if out.seed_outcome == "zero_write" and out.teardown == "stopped":
        return "refused", f"{reason}; its worker was stopped"
    return ("failed" if out.launched else "refused"), reason


# ---- the return leg (#2202 PR 2) ----------------------------------------------------------------

#: Sessions scanned for the PR-branch fallback must have been active this recently.
FALLBACK_ACTIVE_S = 7 * 86400
_GIT_TIMEOUT_S = 10.0


def _git(root: str, cwd: str, *args: str) -> str | None:
    """One read-only git command in the checkout ``cwd`` through `gitpanel`'s hardened runner (a
    literal argv, an allowlisted environment, ``core.fsmonitor=false``, bounded output, ceiling at
    ``root``). The checkout belongs to an agent, so its config is not trusted. Returns stripped
    stdout, or None when git cannot answer. BLOCKING."""
    from . import gitpanel

    try:
        rc, out, _err = gitpanel._run_git_rc(root, list(args), cwd=cwd)
    except Exception:  # noqa: BLE001 — no git, a refusal, a timeout: git cannot answer
        return None
    if rc != 0:
        return None
    return out.decode("utf-8", "replace").strip()


def forge_authority() -> str:
    """The configured forge's host (lower-case, no port), or "" — what a checkout's remote must
    point at. BLOCKING."""
    try:
        base = str(prefs.get_forge().get("base_url") or "")
        return (urlsplit(base.strip()).hostname or "").lower()
    except Exception:  # noqa: BLE001 — unreadable settings: no authority, nothing fits
        return ""


def remote_names_repo(url: str, repo: str, host: str) -> bool:
    """Whether a git remote URL is a clone of ``repo`` (``owner/name``) ON the configured forge
    ``host``: ``https://host[:port]/owner/name[.git]``, ``ssh://user@host[:port]/owner/name[.git]``
    or ``user@host:owner/name[.git]``. A local path, another host or another path never fits.
    Credentials in the URL are ignored, never compared or kept."""
    if not host:
        return False
    u = (url or "").strip()
    if "://" in u:
        try:
            p = urlsplit(u)
        except ValueError:
            return False
        if p.scheme not in ("https", "http", "ssh") or (p.hostname or "").lower() != host:
            return False
        path = p.path
    else:
        m = re.match(r"^[A-Za-z0-9._-]+@([A-Za-z0-9.-]+):(?!/)(.+)$", u)
        if not m or m.group(1).lower() != host:
            return False  # a local path or a bare `host:path` is not a forge clone
        path = m.group(2)
    path = path.strip("/").removesuffix(".git")
    return path.lower() == repo.lower()


def _inside(path: str, root: str) -> bool:
    try:
        real = os.path.realpath(path)
        return os.path.commonpath([real, root]) == root
    except (ValueError, OSError):
        return False


def checkout_fits(
    cwd: str, repo: str, head_ref: str, root: str, head_repo: str = "", host: str | None = None
) -> str:
    """Why the checkout at ``cwd`` is NOT the item's (``""`` when it is): its ``origin`` must name
    the repo — or, for a contributor's PR, the fork it comes from (``head_repo``) — and, for a PR,
    its current branch must be the PR's head branch. BLOCKING."""
    url = _git(root, cwd, "remote", "get-url", "origin")
    names = [r for r in (repo, head_repo) if r]
    host = forge_authority() if host is None else host
    if url is None or not any(remote_names_repo(url, r, host) for r in names):
        return f"its checkout is not a clone of {' or '.join(names)}"
    if head_ref:
        # The branch HEAD points at — a detached HEAD has none, so it never fits.
        branch = _git(root, cwd, "symbolic-ref", "--short", "-q", "HEAD")
        if branch != head_ref:
            return f"its checkout is not on {head_ref}"
    return ""


def _branch_candidates(
    project_real: str, repo: str, head_ref: str, head_repo: str = ""
) -> list[str]:
    """API sessions in the project whose checkout is on ``head_ref`` (the PR fallback). BLOCKING."""
    ms = runner._structured()
    out: list[str] = []
    cutoff = time.time() - FALLBACK_ACTIVE_S
    for prov in engines.all_providers():
        try:
            if not ms.is_api(prov):
                continue
            rows = prov.scan()
        except Exception:  # noqa: BLE001, S112 — a provider that cannot list contributes nothing
            continue
        for s in rows:
            if getattr(s, "archived", False) or float(getattr(s, "last_mtime", 0) or 0) < cutoff:
                continue
            cwd = str(getattr(s, "cwd", "") or "")
            if not cwd or not _inside(cwd, project_real):
                continue
            if checkout_fits(cwd, repo, head_ref, project_real, head_repo):
                continue
            out.append(f"{prov.engine_id}:{s.uuid}")
    return sorted(set(out))


async def resolve_origin(inputs: dict, action: dict, pins: dict) -> tuple[str | None, str, bool]:
    """``(session_key, why_not, terminal)`` for the session that opened the item.

    The payload never names it: a PR's ``agent-session-pr`` record (from the signed body) or an
    issue's newest session record (read through the forge), else, for a PR only, the ONE API
    session in the project whose checkout is on the PR's branch. The pick is then validated: an
    API session this host knows, inside the consented project folder, on a clone of the repo and
    (for a PR) on its branch."""
    item = str(inputs.get("item") or "")
    kind, _, number = item.partition("#")
    project_real = str((pins.get("cwd") or {}).get("real") or "")
    if not project_real:
        return None, "the project has no folder", False
    head_ref = str(inputs.get("head_ref") or "") if kind == "pr" else ""
    head_repo = str(inputs.get("head_repo") or "") if kind == "pr" else ""
    key = str(inputs.get("session_marker") or "") if kind == "pr" else ""
    if kind == "issue":
        client, why = await asyncio.to_thread(forge_client)
        if client is None:
            return None, f"the issue's session record could not be read ({why})", False
        fact = await asyncio.to_thread(client.issue_session_marker, action["repo"], int(number))
        if not fact.observed:
            return None, f"the issue's session record could not be read ({fact.detail})", False
        key = str((fact.extra or {}).get("session") or "")
    if not key and kind == "pr" and head_ref:
        found = await asyncio.to_thread(
            _branch_candidates, project_real, action["repo"], head_ref, head_repo
        )
        if len(found) > 1:
            return None, f"{len(found)} API sessions in the project are on {head_ref}", False
        key = found[0] if found else ""
    if not key:
        return None, f"no session record names the session that opened {item}", False
    try:
        prov, _native = engines.parse_key(key)
    except Exception:  # noqa: BLE001
        return None, f"the session record names no session this app knows ({key})", False
    ms = runner._structured()
    if not ms.is_api(prov):
        return key, "it is a terminal session: nothing unattended is typed into one", True
    target = await ms.automation_target(key)
    if target.get("gone"):
        return key, "that session no longer exists", False
    if "unreadable" in target:
        return key, f"that session could not be read ({target['unreadable']})", False
    cwd = str(target.get("cwd") or "")
    if not cwd or not _inside(cwd, project_real):
        return key, "that session is outside the review loop's project", False
    misfit = await asyncio.to_thread(
        checkout_fits, cwd, action["repo"], head_ref, project_real, head_repo
    )
    if misfit:
        return key, misfit, False
    return key, "", False


async def return_verdict(run: dict, config: dict, pins: dict, *, registry) -> tuple[str, str]:
    """Deliver an ACCEPTED verdict to the originating API session: one guarded structured turn
    through the automation send path (`automation_runner._send_structured`), under the effect lock
    and the automation's authority. Never typed into a terminal; never resent."""
    rid = run["id"]
    action = config["action"]
    inputs = run.get("inputs") or {}
    item = str(inputs.get("item") or "")
    kind = item.partition("#")[0]
    key, why, terminal = await resolve_origin(inputs, action, pins)
    if key:
        await asyncio.to_thread(store.link, rid, session_key=key, origin=False)
    if why:
        return "undelivered", (
            f"the verdict on {item} was not delivered: {why}"
            + ("; open that session and tell it yourself" if terminal else "")
        )
    text = review_loop.return_message(
        repo=action["repo"],
        item=item,
        revision=str(inputs.get("revision") or ""),
        verdict=str(inputs.get("verdict") or ""),
        reviewer=action["reviewer_login"],
        round_no=int(inputs.get("round") or 0),
        rounds=int(action["max_rounds"].get(kind) or 0),
    )
    send_config = {
        **config,
        "action": {"kind": "send_to_session", "session_key": key, "message": {"text": text}},
    }
    outcome, reason = await runner._send_structured(run, send_config, {}, keep_inputs=inputs)
    if outcome in ("refused", "skipped"):
        # Not sent (busy, waiting on a question, not live): visible and announced, never resent.
        return "undelivered", f"the verdict on {item} was not delivered: {reason}"
    return outcome, reason
