"""Notes between the two operators of a shared session (#1246 §6, #2059).

A grant has one thread of plain-text notes. The peer reads and writes it through five verbs
(``verbs.py`` holds their validators and the table); this instance's operator through the
operator routes, which call the ``owner_*`` functions here.

* ``note.list``: what changed in the thread after a change number, or the whole thread again;
* ``note.add``: add a note (with the caller's retry key);
* ``note.edit`` / ``note.delete``: the caller's OWN notes only; a delete leaves a tombstone;
* ``note.read``: the caller has read the other side's notes up to a change number.

**A note is a message to the other OPERATOR, never to an agent.** Its text lives in the
federation store and is read by ``federation/`` and ``routes/federation.py`` and by nothing
else: not the session journal, a projection, a transcript, a summary, retrieval, a judge, an
HQ tool, a prompt, the ledger, the bell or a push. Nothing in this module calls a runtime, a
model or a notification with a note's text. ``docs/invariants/federation-notes.md`` is the
contract; two source scans and a recording test pin it.

**Any live API grant is enough** (``view``): a note changes nothing about the session. A
terminal grant (#2056) has no thread: every verb here refuses it as ``no_grant``, and the store
refuses a note write under it from either side.

**A change cursor, not a position.** Every change to a thread (add, edit, hide, delete, and the
eviction of the oldest note at the bound) takes the next number of a per-grant counter.
``note.list`` answers the changes after a number, each with the note as it is NOW or its
removal. A cursor older than the retained changes gets ``reset: true`` and the current thread,
page by page. So a reader that only asks "what changed since" learns of an edit or a hide of
an old note, a tombstone and an eviction.

**The owner is the only store.** The receiving instance keeps no note: it passes these answers
to its operator's pane and forgets them. When the grant ends, no verb serves the thread (every
call here passes ``grants.authorize``, and the store re-reads the grant inside the same
transaction as the read or the write). This instance's operator can still read the thread
through the operator routes for seven days (``store.NOTES_GRACE_S``); then the sweep deletes it.

**Hide is the owner's.** A hidden note is a removal for the peer from then on, its own notes
included; the owner's own view keeps it, marked.
"""

from __future__ import annotations

import asyncio
import logging

from . import channel as ch
from . import grants, inputs, notices, sharing, store

log = logging.getLogger(__name__)

TEXT_MAX = store.NOTE_TEXT_MAX
#: Entries one ``note.list`` answer holds at most; the byte budget may cut it shorter.
PAGE_MAX = 100
NOT_FOUND = "not_found"
#: Every C0 control but tab and newline, DEL, and the C1 block: a note is plain text.
_CONTROLS = frozenset(chr(c) for c in (*range(0x00, 0x09), *range(0x0B, 0x20), *range(0x7F, 0xA0)))


def valid_text(text: object) -> bool:
    """A note's text: 1 to 2000 characters, not blank, no control characters. Shape only:
    whatever it says, it is shown as plain text and never parsed, rendered or executed."""
    return (
        isinstance(text, str)
        and 0 < len(text) <= TEXT_MAX
        and bool(text.strip())
        and not _CONTROLS.intersection(text)
    )


# ── what a reader is told ─────────────────────────────────────────────
def peer_note(note: store.Note, viewer: str) -> dict:
    """A note as the PEER reads it. The other side is ``owner``; a tombstone has no text."""
    return {
        "note_id": note.note_id,
        "author": "owner" if note.author == store.AUTHOR_OWNER else "peer",
        "mine": note.author == viewer,
        "actor": note.actor,
        "text": "" if note.deleted else note.text,
        "created_at": note.created_at,
        "edited_at": note.edited_at,
        "deleted": note.deleted,
    }


def owner_note(note: store.Note, peer_name: str = "") -> dict:
    """A note as THIS instance's operator reads it, hidden notes included."""
    mine = note.author == store.AUTHOR_OWNER
    return {
        "note_id": note.note_id,
        "author": "owner" if mine else "peer",
        "mine": mine,
        "peer_name": "" if mine else peer_name,
        "actor": note.actor,
        "text": "" if note.deleted else note.text,
        "created_at": note.created_at,
        "edited_at": note.edited_at,
        "deleted": note.deleted,
        "hidden": note.hidden,
        "seen": note.seen,
        "seq": note.position,
    }


def fit(page: dict, viewer: str) -> dict:
    """A ``store.note_page`` as the peer's answer, within the channel's byte budget
    (``sharing.BUDGET`` of ``channel.encode_answer`` output, the whole envelope). Entries that
    do not fit stay for the next call: ``seq`` (or, in a reset listing, ``next``) is then the
    last one delivered, and ``more`` is true. A note the owner hid, and one whose row is gone,
    is a removal."""
    reset = bool(page["reset"])
    # Measured with the longest values the cursor fields can take.
    out: dict = {
        "reset": reset,
        "seq": grants.REVISION_MAX,
        "changes": [],
        "more": True,
        "next": "x" * 64,
        "unread": int(page["unread"]),
    }
    used = sharing.wire_size(out)
    kept: list[dict] = []
    last_seq, last_note, cut = None, None, False
    for seq, note_id, note in page["entries"]:
        if note is None or note.hidden:
            if reset:  # a reset listing holds the notes that exist for this reader
                last_seq, last_note = seq, note_id
                continue
            entry = {"seq": seq, "note_id": note_id, "removed": True}
        else:
            entry = {"seq": seq, "note_id": note_id, "note": peer_note(note, viewer)}
        cost = sharing._size(entry) + (1 if kept else 0)
        if used + cost > sharing.BUDGET:
            cut = True
            break
        used += cost
        kept.append(entry)
        last_seq, last_note = seq, note_id
    more = bool(page["more"]) or cut
    out["changes"] = kept
    out["more"] = more
    if reset:
        out["seq"] = int(page["seq"])
        out["next"] = last_note if more else None
    else:
        out["seq"] = int(last_seq) if cut and last_seq is not None else int(page["seq"])
        out["next"] = None
    if cut and last_seq is None:
        raise ch.VerbError(sharing.UNAVAILABLE)  # not even one entry fits: nothing is sent
    return out


# ── the verbs ─────────────────────────────────────────────────────────
async def _stored(call, **kwargs):
    """A store call off the event loop, with its refusals as the verbs' codes."""
    try:
        return await asyncio.to_thread(lambda: call(**kwargs))
    except store.NotGranted:
        raise ch.VerbError(sharing.NO_GRANT) from None
    except store.RateLimited:
        raise ch.VerbError(inputs.BUSY) from None
    except store.NotFound:
        raise ch.VerbError(NOT_FOUND) from None
    except store.Conflict:
        raise ch.VerbError("conflict") from None
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None


async def note_list(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    viewer = store.principal_of(peer_id)
    page = await _stored(
        fed.store.note_page,
        grant_id=grant.grant_id,
        after_seq=args["after_seq"],
        limit=args["limit"],
        reader=viewer,
        cursor=args["cursor"],
        peer=(peer_id, args["revision"]),
    )
    answer = fit(page, viewer)
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


async def note_add(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    viewer = store.principal_of(peer_id)
    note, fresh = await _stored(
        fed.store.add_note,
        grant_id=grant.grant_id,
        author=viewer,
        actor=args["actor"],
        client_key=args["client_key"],
        text=args["text"],
        peer=(peer_id, args["revision"]),
    )
    if fresh:
        # In the background: the bell and the push never delay or decide the answer. They
        # carry the peer's name and the session's title, never this text.
        fed.background(asyncio.to_thread(notices.announce_note, fed.store, grant))
    answer = {"note": peer_note(note, viewer), "seq": note.position}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


async def note_edit(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    viewer = store.principal_of(peer_id)
    note = await _stored(
        fed.store.edit_note,
        grant_id=grant.grant_id,
        note_id=args["note_id"],
        author=viewer,
        text=args["text"],
        peer=(peer_id, args["revision"]),
    )
    answer = {"note": peer_note(note, viewer)}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


async def note_delete(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    viewer = store.principal_of(peer_id)
    note = await _stored(
        fed.store.tombstone_note,
        grant_id=grant.grant_id,
        note_id=args["note_id"],
        author=viewer,
        peer=(peer_id, args["revision"]),
    )
    # A deleted note is no longer unread: the owner's bell row goes when nothing else is.
    fed.background(asyncio.to_thread(notices.retract, fed.store))
    answer = {"note": peer_note(note, viewer)}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


async def note_mark_read(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    viewer = store.principal_of(peer_id)
    await _stored(
        fed.store.mark_notes_seen,
        grant_id=grant.grant_id,
        reader=viewer,
        upto=args["seq"],
        peer=(peer_id, args["revision"]),
    )
    unread = await _stored(fed.store.unread_notes, grant_id=grant.grant_id, reader=viewer)
    answer = {"unread": unread}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


# ── this instance's operator (the operator routes; no verb reaches these) ──
def _refused(exc: store.StoreError) -> inputs.InputError:
    if isinstance(exc, store.NotGranted):
        return inputs.InputError("ended", "this share has ended", status=409)
    if isinstance(exc, store.RateLimited):
        return inputs.InputError(inputs.BUSY, str(exc), status=429)
    if isinstance(exc, store.NotFound):
        return inputs.InputError(NOT_FOUND, str(exc), status=404)
    if isinstance(exc, store.Conflict):
        return inputs.InputError("conflict", str(exc), status=409)
    return inputs.InputError(inputs.store_code(exc), str(exc), exc.status)


def _ids(grant_id: object, note_id: object = "x" * 22) -> None:
    if not grants.valid_grant_id(grant_id):
        raise inputs.InputError(NOT_FOUND, "unknown grant", status=404)
    if not grants.valid_grant_id(note_id):  # the same shape: `store.new_grant_id`
        raise inputs.InputError(NOT_FOUND, "unknown note", status=404)


def _text(text: object) -> str:
    if not valid_text(text):
        raise inputs.InputError(
            "invalid", f"a note is 1 to {TEXT_MAX} characters of plain text", status=422
        )
    return text  # type: ignore[return-value]


def owner_thread(st: store.Store, grant_id: object) -> dict:
    """The whole thread for the operator: while the grant is live, and for seven days after it
    ended (``ended`` is then true and nothing can be written). Blocking."""
    _ids(grant_id)
    try:
        # The owner's clock decides the grace, as it decides every expiry.
        grant, rows, newest = st.owner_notes(grant_id, now=grants._clock())  # type: ignore[arg-type]
    except store.StoreError as exc:
        raise _refused(exc) from None
    peer = st.get(grant.peer_id)
    name = peer.name if peer is not None else ""
    return {
        "grant": grants.public(grant),
        "ended": not grant.live(grants._clock()),
        "notes": [owner_note(note, name) for note in rows],
        "seq": newest,
        "unread": st.unread_notes(grant.grant_id, store.AUTHOR_OWNER),
    }


def owner_add(st: store.Store, grant_id: object, *, client_key: str, text: object, actor: str):
    _ids(grant_id)
    try:
        note, _fresh = st.add_note(
            grant_id=grant_id,  # type: ignore[arg-type]
            author=store.AUTHOR_OWNER,
            actor=actor,
            client_key=client_key,
            text=_text(text),
        )
    except store.StoreError as exc:
        raise _refused(exc) from None
    return note


def owner_edit(st: store.Store, grant_id: object, note_id: object, *, text: object) -> store.Note:
    _ids(grant_id, note_id)
    try:
        return st.edit_note(
            grant_id=grant_id,  # type: ignore[arg-type]
            note_id=note_id,  # type: ignore[arg-type]
            author=store.AUTHOR_OWNER,
            text=_text(text),
        )
    except store.StoreError as exc:
        raise _refused(exc) from None


def owner_delete(st: store.Store, grant_id: object, note_id: object) -> store.Note:
    _ids(grant_id, note_id)
    try:
        return st.tombstone_note(
            grant_id=grant_id,  # type: ignore[arg-type]
            note_id=note_id,  # type: ignore[arg-type]
            author=store.AUTHOR_OWNER,
        )
    except store.StoreError as exc:
        raise _refused(exc) from None


def owner_hide(st: store.Store, grant_id: object, note_id: object) -> store.Note:
    """Hide any note of the thread from the peer. It narrows."""
    _ids(grant_id, note_id)
    try:
        hidden = st.hide_note(grant_id=grant_id, note_id=note_id)  # type: ignore[arg-type]
    except store.StoreError as exc:
        raise _refused(exc) from None
    notices.retract(st)  # a hidden note is no longer unread
    return hidden


def owner_read(st: store.Store, grant_id: object, seq: object) -> int:
    """The operator has read the peer's notes up to ``seq``. Returns what is still unread."""
    _ids(grant_id)
    if type(seq) is not int or not 0 <= seq <= grants.REVISION_MAX:
        raise inputs.InputError("invalid", "seq must be a change number", status=422)
    try:
        st.mark_notes_seen(
            grant_id=grant_id,  # type: ignore[arg-type]
            reader=store.AUTHOR_OWNER,
            upto=seq,
        )
    except store.StoreError as exc:
        raise _refused(exc) from None
    notices.retract(st)
    return st.unread_notes(grant_id, store.AUTHOR_OWNER)  # type: ignore[arg-type]
