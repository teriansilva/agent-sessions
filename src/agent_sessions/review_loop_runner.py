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
    if dec.kind == "verdict":
        # The return leg is #2202 PR 2: until it lands a verdict is noted, never acted on.
        await asyncio.to_thread(
            store.note_delivery, aid, facts["event"] or ev.kind, "verdict (return leg not active)"
        )
        return False
    # THE FORGE SAYS WHAT IS CURRENT — never the order deliveries arrived in.
    origin = _origin(_repo_url(body))
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
    if run is not None and run["state"] == "dispatching":
        runner.spawn(run, registry=registry)
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
    """The reviewer's API session for a review record already admitted (`begin_review`)."""
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
