"""The owner side of a shared API session: what a peer may list and read (#1246 §5, #2054).

Four verbs answer here (``verbs.py`` holds their validators and the table):

* ``share.list``: the caller's own live grants (of both kinds), one page, in grant-id order;
* ``session.snapshot``: ``share_api.project`` of the session, the projection a public ``view``
  link gets, through the same redactor;
* ``session.events``: cursors and kinds only, optionally held up to 20 s;
* ``session.history``: one older page of projected turns.

**A peer never sends a session key.** It names a grant id and the revision it was told; the
handler asks :func:`grants.authorize` and nothing else. Every refusal is the one code
``no_grant``.

**Every call is authorized twice.** Once when it arrives, and again **directly before the answer
is returned to the channel**: after any wait and after the answer was built and measured. The
second check is the last ``await`` of a handler; what follows it is a comparison and ``return``.
Neither check holds a transaction or a lock, so a revoke (by this process or another one over the
same store), an expiry, a revision change or an archive in between turns the answer into
``no_grant`` and nothing built before it is sent.

**Every answer is measured as it will be sent.** The channel writes ASCII-escaped JSON
(:func:`channel.encode_answer`), where one non-BMP character costs 12 bytes, so a character count
proves nothing. Each answer is built against :data:`BUDGET` bytes of that serializer's output
**for the whole envelope** (``{"ok":true,"result":…}``: turns, pending requests and every other
field together), under the channel's 256 KiB cap. One oversized turn or request is clipped by
:func:`clip`, from the ALREADY REDACTED projection, so a cut lands in ordinary text or inside a
``[secret]`` placeholder and never inside a secret; clipping one item is not the aggregate
guarantee, :func:`_enforce` is.

Images are not served to a peer in this phase: an attachment carries its name and type only.

A **terminal** session is read by one more verb, ``terminal.frame``, in :mod:`terminal`. The
session verbs here refuse a terminal grant as ``no_grant``, and that verb refuses an API grant.
"""

from __future__ import annotations

import asyncio
import logging

from .. import chat_store, engines, metadata, prefs, share_api, shares, template_secrets
from .. import structured_runtime as runtime
from . import channel as ch
from . import grants, store

log = logging.getLogger(__name__)

#: The whole answer, envelope included, as the channel serializes it.
BUDGET = 200 * 1024
#: One projected turn. Over it, the turn is clipped.
TURN_BUDGET = 150 * 1024
#: One pending request. Over it, the request is clipped by the same rule.
REQUEST_BUDGET = 8 * 1024
#: Everything that is not a turn (the envelope's own fields and the pending requests). With one
#: turn at its cap this still fits the budget, so every answer holds at least one turn.
HEAD_BUDGET = 40 * 1024
#: The envelope's own scalar fields, before requests are added.
ENVELOPE_BUDGET = 16 * 1024
CLIP_START = 32768
CLIP_FLOOR = 256
CUT_SUFFIX = "… [cut: {n} more characters]"

LIST_LIMIT_MAX = 50
EVENTS_LIMIT_MAX = 100
WAIT_MAX_S = 20.0
#: Parked ``session.events`` waits one peer may hold at once; the next is ``busy``.
WAITS_PER_PEER = 8
#: A parked wait re-reads its grant this often, so a revoke ends the wait and not only its answer.
WAIT_SLICE_S = 1.0
#: The runtime holds a few event waiters per session (`structured_runtime`), and the operator's
#: own pane needs them. Peers park in at most this many; past it a wait polls once per slice.
PEER_WAITERS_PER_SESSION = runtime.EVENTS_WAITERS_PER_SESSION // 2
TITLE_MAX = 200
#: The caller's allowance on top of ``wait`` (``remote.py`` uses it for its call timeout).
WAIT_CALL_MARGIN_S = 10.0

NO_GRANT = "no_grant"
UNAVAILABLE = "unavailable"

# A turn at its cap, the head at its cap and the wrapper must fit together.
assert TURN_BUDGET + HEAD_BUDGET + 1024 <= BUDGET < ch.ANSWER_MAX  # noqa: S101 — a constant relation


# ── size ──────────────────────────────────────────────────────────────
def _size(value: object) -> int:
    """Bytes of ``value`` as the channel serializes it."""
    return len(ch.encode_answer(value))


def wire_size(result: dict) -> int:
    """Bytes of the complete answer the channel will write for ``result``."""
    return len(ch.encode_answer({"ok": True, "result": result}))


# ── clipping one item ─────────────────────────────────────────────────
def _cut(value, limit: int):
    """Every string longer than ``limit`` keeps its first ``limit`` characters and says how many
    were cut. Deterministic, and blind to what the text contains."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + CUT_SUFFIX.format(n=len(value) - limit)
    if isinstance(value, dict):
        return {k: _cut(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, limit) for v in value]
    return value


def _drop_from_end(item: dict, key: str, budget: int) -> None:
    """Drop entries of ``item[key]`` from the end until ``item`` fits, counting them."""
    entries = item.get(key)
    if not isinstance(entries, list) or not entries:
        return
    counter = f"{key}_omitted"
    already = item.get(counter) if type(item.get(counter)) is int else 0
    # Measured with the largest count the field can take, so the count itself always fits.
    used = _size({**item, key: [], counter: already + len(entries)})
    keep = 0
    for entry in entries:
        cost = _size(entry) + (1 if keep else 0)
        if used + cost > budget:
            break
        used += cost
        keep += 1
    if keep < len(entries):
        item[counter] = already + len(entries) - keep
        item[key] = entries[:keep]


def _stub(item: dict) -> dict:
    """The last resort: the item's own scalar fields, each cut to the floor, and counts of what
    was left out. Bounded by the allowlist's field count, whatever the item held."""
    out: dict = {}
    for key, value in item.items():
        if isinstance(value, dict | list):
            if key in ("tools", "attachments"):
                counter = f"{key}_omitted"
                already = item.get(counter) if type(item.get(counter)) is int else 0
                out[counter] = already + len(value)
                out[key] = []
            continue
        out[key] = _cut(value, CLIP_FLOOR)
    out["truncated"] = True
    return out


def clip(item: dict, budget: int) -> dict:
    """``item`` (a projected turn or request, already redacted) within ``budget`` bytes.

    1. every string field longer than 32 768 characters keeps its first 32 768 and gets the suffix
       ``… [cut: N more characters]``;
    2. while the item is still over budget, that limit is halved, down to 256;
    3. if it is still over, tool entries and then attachment entries are dropped from the end,
       with ``tools_omitted`` and ``attachments_omitted`` counts.

    The result carries ``truncated: true``. An item within budget is returned unchanged."""
    if _size(item) <= budget:
        return item
    limit = CLIP_START
    while True:
        out = _cut(item, limit)
        out["truncated"] = True
        if _size(out) <= budget or limit <= CLIP_FLOOR:
            break
        limit = max(CLIP_FLOOR, limit // 2)
    for key in ("tools", "attachments"):
        if _size(out) <= budget:
            return out
        _drop_from_end(out, key, budget)
    if _size(out) <= budget:
        return out
    return _stub(out)


# ── the aggregate budget ──────────────────────────────────────────────
def _fit_turns(out: dict, turns: list[dict]) -> tuple[list[dict], int]:
    """The newest turns that fit beside what ``out`` already holds (its ``turns`` empty and its
    counters at their largest), chronological, and how many older ones were left out."""
    used = wire_size(out)
    kept: list[dict] = []
    for turn in reversed(turns):
        item = clip(turn, TURN_BUDGET)
        cost = _size(item) + (1 if kept else 0)
        if used + cost > BUDGET:
            break
        used += cost
        kept.insert(0, item)
    return kept, len(turns) - len(kept)


def _enforce(out: dict, *, on_drop) -> dict:
    """THE aggregate guarantee: the complete answer, as the channel will serialize it, is within
    :data:`BUDGET`. While it is not, the oldest turn goes (``on_drop`` keeps the cursor fields
    true), then the last turn is clipped harder, then pending requests go from the end. An answer
    that still cannot fit is not sent."""
    while True:
        over = wire_size(out) - BUDGET
        if over <= 0:
            return out
        turns = out.get("turns") or []
        requests = out.get("pending_requests") or []
        if len(turns) > 1:
            on_drop(out, turns.pop(0))
            continue
        if turns:
            now = _size(turns[0])
            smaller = clip(turns[0], max(0, now - over))
            if _size(smaller) < now:
                turns[0] = smaller
                continue
        if requests:
            requests.pop()
            out["pending_requests_omitted"] = (out.get("pending_requests_omitted") or 0) + 1
            continue
        log.error("federation: an answer could not be brought within its byte budget")
        raise ch.VerbError(UNAVAILABLE)


def _strip_images(view: dict) -> None:
    """A peer gets no image handle (there is no verb that serves the bytes): name and type only."""
    for turn in view.get("turns") or []:
        for attachment in turn.get("attachments") or []:
            attachment.pop("image", None)


def fit_snapshot(view: dict) -> dict:
    """A projected snapshot within the byte budget: its head, as many pending requests as the head
    share holds, then turns newest first. ``omitted_turns`` counts what did not fit."""
    turns = list(view.get("turns") or [])
    requests = list(view.get("pending_requests") or [])
    omitted = view.get("omitted_turns") or 0
    out = {**view, "turns": [], "pending_requests": []}
    if wire_size(out) > ENVELOPE_BUDGET:
        # Its own fields are a few short strings and numbers. If one of them is not short, it is
        # cut like any other string; nothing is dropped.
        out = {k: _cut(v, CLIP_FLOOR) if isinstance(v, str) else v for k, v in out.items()}
        out["truncated"] = True
        if wire_size(out) > ENVELOPE_BUDGET:
            raise ch.VerbError(UNAVAILABLE)
    kept: list[dict] = []
    for request in requests:
        item = clip(request, REQUEST_BUDGET)
        out["pending_requests"] = [*kept, item]
        if wire_size({**out, "pending_requests_omitted": len(requests)}) > HEAD_BUDGET:
            break
        kept.append(item)
    out["pending_requests"] = kept
    if len(kept) < len(requests):
        out["pending_requests_omitted"] = len(requests) - len(kept)
    # Measured with the largest count it can take, then set to the true one (never longer).
    out["omitted_turns"] = omitted + len(turns)
    out["turns"], dropped = _fit_turns(out, turns)
    out["omitted_turns"] = omitted + dropped

    def on_drop(doc: dict, _turn: dict) -> None:
        doc["omitted_turns"] += 1

    return _enforce(out, on_drop=on_drop)


def fit_history(link: str, turns: list[dict], before: object, has_more: bool) -> dict:
    """One history page within the byte budget. When older turns of the page did not fit,
    ``before`` is the oldest turn that was delivered, so the next call continues exactly there."""
    # Measured with the longest values the cursor fields can take.
    out = {"link": link, "turns": [], "before": "0" * 36, "has_more": False}
    kept, dropped = _fit_turns(out, turns)
    out["turns"] = kept
    out["before"] = before
    out["has_more"] = bool(has_more)

    def on_drop(doc: dict, _turn: dict) -> None:
        doc["has_more"] = True
        doc["before"] = doc["turns"][0].get("turn_id")

    if dropped:
        on_drop(out, {})
    out = _enforce(out, on_drop=on_drop)
    if out["has_more"] and not chat_store.valid_turn_id(out["before"]):
        raise ch.VerbError(UNAVAILABLE)  # a page that could not be continued is not sent
    return out


def fit_events(view: dict) -> dict:
    """An events page within the byte budget. Events that did not fit stay for the next call:
    ``next_cursor`` is then the last event that was delivered."""
    events = list(view.get("events") or [])
    out = {**view, "events": []}
    used = wire_size(out) + 24  # room for a cursor rewritten below
    kept: list[dict] = []
    for event in events:
        item = clip(event, REQUEST_BUDGET)
        cost = _size(item) + (1 if kept else 0)
        if used + cost > BUDGET:
            break
        used += cost
        kept.append(item)
    out["events"] = kept
    if len(kept) < len(events):
        if not kept or type(kept[-1].get("cursor")) is not int:
            raise ch.VerbError(UNAVAILABLE)
        out["next_cursor"] = kept[-1]["cursor"]
    if wire_size(out) > BUDGET:
        raise ch.VerbError(UNAVAILABLE)
    return out


# ── the per-call check ────────────────────────────────────────────────
async def _authorize(fed, peer_id: str, args: dict, *, kind: str = grants.KIND_API) -> store.Grant:
    """:func:`grants.authorize`, off the event loop. One refusal code, whatever failed. The verbs
    of this module read API sessions: a terminal grant is refused here like an unknown one
    (``terminal.py`` asks for its own kind)."""
    grant = await asyncio.to_thread(
        grants.authorize, fed.store, args["grant_id"], peer_id, args["revision"], kind=kind
    )
    if grant is None:
        raise ch.VerbError(NO_GRANT)
    return grant


def _runtime_refusal(exc: runtime.StructuredError) -> ch.VerbError:
    """The facade's refusal as a code. Its text is written for the operator and never sent."""
    if exc.status == 404:
        return ch.VerbError(NO_GRANT)  # the session is gone: the same answer as no grant
    if exc.status == 409:
        return ch.VerbError("conflict")
    if exc.status == 422:
        return ch.VerbError("invalid")
    if exc.status == 429:
        return ch.VerbError("busy")
    return ch.VerbError(UNAVAILABLE)


async def _projected(build):
    """Run ``build`` off the event loop (reading the secret store and redacting are real work).
    Fail closed: while redaction or the keyed digests are unavailable, nothing is sent."""
    try:
        return await asyncio.to_thread(build)
    except (template_secrets.RedactionUnavailable, template_secrets.SecretKeyUnavailable):
        raise ch.VerbError(UNAVAILABLE) from None


async def _authors(session_key: str) -> dict:
    try:
        return await asyncio.to_thread(shares.store().authors, session_key)
    except Exception:  # noqa: BLE001 — attribution that cannot be read is not guessed
        log.warning("federation: turn attribution could not be read; nothing was sent")
        raise ch.VerbError(UNAVAILABLE) from None


# ── share.list ────────────────────────────────────────────────────────
def _list_page(st: store.Store, peer_id: str, after: str | None, limit: int) -> dict:
    """One page of ``peer_id``'s live grants. Blocking."""
    generation, peer_state, live = st.live_grants_of(peer_id, after=after, now=grants._clock())
    if peer_state != "trusted":
        raise ch.VerbError(NO_GRANT)
    out: dict = {"generation": generation, "grants": [], "next": None}
    if not live:
        return out
    redact = share_api.redactor()
    use_ai_title = prefs.renames_sessions()
    meta = metadata.load()
    scans: dict[str, dict] = {}
    # Measured with the longest `next` there can be, so setting it never passes the budget.
    used = wire_size({**out, "next": "x" * 64})
    entries: list[dict] = []
    examined = 0
    for grant in live:
        if len(entries) >= limit:
            break
        entry = _list_entry(grant, redact, meta, scans, use_ai_title)
        if entry is not None:
            cost = _size(entry) + (1 if entries else 0)
            if entries and used + cost > BUDGET:
                break  # the next page starts with it
            used += cost
            entries.append(entry)
        examined += 1
    out["grants"] = entries
    if examined < len(live) and entries:
        out["next"] = entries[-1]["grant_id"]
    return out


def _session_rows(engine: str) -> dict:
    """``{native id: row}`` of an engine's sessions, as its provider scans them. An engine that is
    gone, or a store that cannot be read, lists nothing."""
    prov = engines.get_any(engine)
    if prov is None:
        return {}
    try:
        return {s.uuid: s for s in prov.scan()}
    except Exception:  # noqa: BLE001 — an unreadable engine store lists nothing
        return {}


def _list_entry(grant: store.Grant, redact, meta, scans: dict, use_ai_title: bool) -> dict | None:
    """What a peer learns of one grant: the session's id, its redacted title, the kind, the
    level, the expiry and the revision. No cwd, no project, no model. ``None`` for a session that
    is not there to be read any more, or is not of the grant's kind any more (the per-call check
    would refuse it)."""
    engine, _, native = grant.session_key.partition(":")
    if not grants.serves(grant):
        return None
    if engine not in scans:
        scans[engine] = _session_rows(engine)
    row = scans[engine].get(native)
    if row is None or getattr(row, "archived", False):
        return None
    m = meta.get(grant.session_key) or metadata.SessionMeta()
    if m.archived is True:
        return None
    raw = metadata.display_title(
        m, getattr(row, "first_user_message", "") or "", use_ai_title=use_ai_title
    )
    # Redacted WHOLE, then made one line, then cut: cutting first could split a secret.
    title = " ".join(redact(raw).split())[:TITLE_MAX]
    return {
        "grant_id": grant.grant_id,
        "session": {"engine": engine, "id": native},
        "title": title,
        "kind": grant.kind,
        "level": grant.level,
        "expires_at": grant.expires_at,
        "revision": grant.revision,
    }


#: How often a page is rebuilt when the grants changed under it, before answering `unavailable`.
LIST_REBUILDS = 3


def _live_now(st: store.Store, peer_id: str, after: str | None):
    return st.live_grants_of(peer_id, after=after, now=grants._clock())


async def share_list(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    after = args.get("after")
    for _attempt in range(LIST_REBUILDS):
        page = await _projected(lambda: _list_page(fed.store, peer_id, after, args["limit"]))
        if wire_size(page) > BUDGET:
            raise ch.VerbError(UNAVAILABLE)
        # The second check, directly before the answer is returned. Building the page scans
        # the engines and redacts titles, which takes time: a grant revoked, changed, archived
        # or expired meanwhile must not be published. So the store is read again, in one
        # transaction: the peer is still trusted, the generation is the one the page was built
        # from (else the page is rebuilt), and every entry is still a live, unexpired grant at
        # the revision listed (an expiry moves no generation).
        generation, peer_state, live = await asyncio.to_thread(_live_now, fed.store, peer_id, after)
        if peer_state != "trusted":
            raise ch.VerbError(NO_GRANT)
        if generation != page["generation"]:
            continue
        current = {grant.grant_id: grant.revision for grant in live}
        page["grants"] = [
            entry for entry in page["grants"] if current.get(entry["grant_id"]) == entry["revision"]
        ]
        return page
    raise ch.VerbError(UNAVAILABLE)  # the grants kept changing; the caller asks again


# ── session.snapshot ──────────────────────────────────────────────────
async def session_snapshot(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await _authorize(fed, peer_id, args)
    try:
        snap = await runtime.snapshot(grant.session_key)
    except runtime.StructuredError as exc:
        raise _runtime_refusal(exc) from None
    authors = await _authors(grant.session_key)

    def build() -> dict:
        # The SAME projection and redactor a public `view` link gets. The grant id stands where
        # that path uses the share id.
        view = share_api.project(snap, authors, share_api.redactor(), grant.grant_id)
        view["link"] = share_api.link_id(grant.grant_id)
        view["can_send"] = False
        _strip_images(view)
        return fit_snapshot(view)

    answer = await _projected(build)
    # The second check, directly before the answer is returned: nothing may wait after it.
    await _authorize(fed, peer_id, args)
    return answer


# ── session.history ───────────────────────────────────────────────────
async def session_history(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await _authorize(fed, peer_id, args)
    try:
        page = await runtime.history(grant.session_key, before=args["before"])
    except runtime.StructuredError as exc:
        raise _runtime_refusal(exc) from None
    authors = await _authors(grant.session_key)

    def build() -> dict:
        view = share_api.project(
            {"turns": page["turns"]}, authors, share_api.redactor(), grant.grant_id
        )
        _strip_images(view)
        link = share_api.link_id(grant.grant_id)
        return fit_history(link, view["turns"], page["before"], bool(page["has_more"]))

    answer = await _projected(build)
    # The second check, directly before the answer is returned: nothing may wait after it.
    await _authorize(fed, peer_id, args)
    return answer


# ── session.events ────────────────────────────────────────────────────
def _moved(page: dict, after: int) -> bool:
    return bool(page.get("events")) or page.get("next_cursor") != after


async def _held_events(fed, peer_id: str, grant: store.Grant, args: dict) -> dict:
    """The events page, held up to ``wait`` seconds while nothing lies past ``after``. The grant
    is re-read between slices, so a wait ends when its grant does."""
    loop = asyncio.get_running_loop()
    key, after, limit = grant.session_key, args["after"], args["limit"]
    deadline = loop.time() + args["wait"]
    while True:
        started = loop.time()
        step = max(0.0, min(WAIT_SLICE_S, deadline - started))
        try:
            if runtime.events_waiters(key) < PEER_WAITERS_PER_SESSION:
                page = await runtime.wait_events(key, after=after, limit=limit, wait=step)
            else:
                page = await runtime.events(key, after=after, limit=limit)
        except runtime.StructuredError as exc:
            raise _runtime_refusal(exc) from None
        if _moved(page, after) or deadline - loop.time() <= 0:
            return page
        # Not held by the runtime (its waiter slots are taken, or the peers' share of them is):
        # the slice is waited out here, so this loop never spins.
        rest = step - (loop.time() - started)
        if rest > 0:
            await asyncio.sleep(rest)
        # Ends the wait early when the grant ended. It never lets an answer out: the caller
        # runs the full check again directly before it returns one.
        granted = await asyncio.to_thread(
            grants.still_granted, fed.store, args["grant_id"], peer_id, args["revision"]
        )
        if not granted:
            raise ch.VerbError(NO_GRANT)


async def session_events(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await _authorize(fed, peer_id, args)
    try:
        link = await asyncio.to_thread(share_api.link_id, grant.grant_id)
    except template_secrets.SecretKeyUnavailable:
        raise ch.VerbError(UNAVAILABLE) from None
    if args["wait"] > 0:
        if fed.waits.get(peer_id, 0) >= WAITS_PER_PEER:
            raise ch.VerbError("busy")
        fed.waits[peer_id] = fed.waits.get(peer_id, 0) + 1
        try:
            page = await _held_events(fed, peer_id, grant, args)
        finally:
            # Answered, refused or cancelled (the channel closed): the slot is free either way.
            fed.waits[peer_id] -= 1
            if not fed.waits[peer_id]:
                del fed.waits[peer_id]
    else:
        try:
            page = await runtime.events(grant.session_key, after=args["after"], limit=args["limit"])
        except runtime.StructuredError as exc:
            raise _runtime_refusal(exc) from None
    # Cursors and kinds only, never `data`: the public viewer's own projection.
    answer = fit_events(share_api.project_events(page, link))
    # The second check, directly before the answer is returned: nothing may wait after it. An
    # event that woke this wait is not sent when the grant ended while it was parked.
    await _authorize(fed, peer_id, args)
    return answer


# ── share.changed (this instance is the RECEIVER) ─────────────────────
async def share_changed(channel: ch.Channel, args: dict) -> dict:
    """A hint from the owner that its list changed. It carries no data: this side asks again."""
    fed, peer_id = channel.context
    fed.refresh_remote(peer_id)
    return {}
