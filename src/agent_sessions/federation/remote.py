"""The receiving side of a shared session: the remote key, the mirror and the reads
(#1246 §5, #2054).

**The remote key** is ``peer:<peer_id>/<engine>:<native_id>``. It is built and parsed HERE and
nowhere else. It is never a local session key: ``engines.parse_key`` refuses the ``peer`` prefix
(``plugins/kinds.NAMESPACE_RESERVED_IDS``), this module never calls it on one, and no session
scan, lock, archive, cleanup, mission or notification code reads the mirror.

**The mirror** (``remote_shares`` in ``federation.db``) is a cache for LISTING what a peer shares
with this instance. :func:`refresh` reads every page of the peer's ``share.list`` and replaces
that peer's rows in one transaction, and only when all pages arrived, validated, and carried the
same ``generation``. Anything else (a failed or timed-out call, a malformed entry, more than 200
entries, a page that does not advance, a generation that changed between pages after 3 retries)
changes nothing: the previous rows stay. Every READ goes to the owner, which checks its grant for
that call.

**Everything a peer returns is untrusted.** List entries are validated field by field before they
are stored. A projection (snapshot, events, history) is checked for shape and depth and then
passed through as data for the viewer, which renders it as text; this server never uses any part
of it as HTML, a path or a key.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import unicodedata

from .. import chat_store, engines, scrollback
from ..plugins import kinds
from . import channel as ch
from . import frames, grants, identity, offers, sharing, store

log = logging.getLogger(__name__)

PREFIX = "peer:"
#: An engine id as a manifest may declare one (``plugins/manifest._ID_RE``). The owner's engine
#: need not exist on this instance, so the shape is all that can be asked of it.
ENGINE_RE = re.compile(r"\A[a-z][a-z0-9-]{1,23}\Z")
#: A native id of an engine this instance does not know: bounded, and free of every character
#: the key's own syntax uses (``:`` and ``/``).
NATIVE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ENTRIES_MAX = store.GRANTS_PER_PEER_MAX
TITLE_MAX = sharing.TITLE_MAX
#: What a title may measure BEFORE cleaning; anything longer is not a title.
TITLE_RAW_MAX = 4 * TITLE_MAX
REFRESH_RETRIES = 3
REFRESH_EVERY_S = 60.0
#: Between two refreshes of one peer that hints kept asking for.
REFRESH_MIN_GAP_S = 1.0
REFRESH_TIMEOUT_S = 60.0
LIST_CALL_TIMEOUT_S = ch.CALL_TIMEOUT_S
#: How long a READ waits for the refresh a refusal asks for, before it answers 503.
RE_RESOLVE_TIMEOUT_S = 20.0
#: Bounds on a projection passed through to the viewer.
DEPTH_MAX = 12
TURNS_MAX = 1000
REQUESTS_MAX = 1000
KIND_MAX = 64
LINK_MAX = 64
#: The keys every entry carries. ``kind`` (#2056) is optional: an owner on the build before it
#: has API grants only and does not send it.
ENTRY_KEYS = frozenset({"grant_id", "session", "title", "level", "expires_at", "revision"})
#: The three answers of ``terminal.frame``, by their exact keys.
FRAME_KEYS = frozenset({"fingerprint", "cols", "rows", "lines", "settled"})
FRAME_UNCHANGED_KEYS = frozenset({"fingerprint", "unchanged"})
FRAME_SETTLING_KEYS = frozenset({"settling"})
#: The owner never renders a larger grid.
FRAME_CELLS_MAX = scrollback.SCREEN_EXPORT_MAX_CELLS
PAGE_KEYS = frozenset({"generation", "grants", "next"})
#: An entry may carry keys from a newer build; this bounds how many.
ENTRY_KEYS_MAX = 16
LEVEL_SHAPE_RE = re.compile(r"\A[a-z][a-z_]{0,31}\Z")
KIND_SHAPE_RE = LEVEL_SHAPE_RE
#: How far past the longest grant an owner can make (``grants.MAX_EXPIRY_S`` from ITS clock) a
#: listed expiry may lie on THIS clock: room for two instances' clocks to disagree. Anything later
#: is not an expiry the owner could have produced (``1e300`` is a finite number, and a date no
#: browser can build), so the entry is malformed like any other bad known field.
EXPIRY_CLOCK_SLACK_S = 365 * 86400
EXPIRES_AHEAD_MAX_S = grants.MAX_EXPIRY_S + EXPIRY_CLOCK_SLACK_S


class Invalid(Exception):
    """A peer sent something this instance will not store or serve. The reason stays local."""


class ReadError(Exception):
    """A read of a remote session did not produce an answer. ``code`` is a short stable word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Torn(Exception):
    """The owner's list changed between two pages."""


# ── the remote key ────────────────────────────────────────────────────
def valid_engine(engine: object) -> bool:
    return (
        isinstance(engine, str)
        and ENGINE_RE.fullmatch(engine) is not None
        and engine not in kinds.NAMESPACE_RESERVED_IDS
    )


def valid_native(engine: str, native_id: object) -> bool:
    """A native id's shape. Where this instance knows the engine, its registry's own id pattern
    decides as well. ``parse_key`` is never involved: this is another instance's session."""
    if not isinstance(native_id, str) or NATIVE_RE.fullmatch(native_id) is None:
        return False
    prov = engines.get_any(engine)
    return prov is None or prov.id_pattern.fullmatch(native_id) is not None


def key(peer_id: str, engine: str, native_id: str) -> str:
    """The address of a session ``peer_id`` shares. Raises ``ValueError`` on any part that is not
    exactly what it claims to be."""
    if not isinstance(peer_id, str) or identity.PEER_ID_RE.fullmatch(peer_id) is None:
        raise ValueError("bad peer id")
    if not valid_engine(engine) or not valid_native(engine, native_id):
        raise ValueError("bad session id")
    return f"{PREFIX}{peer_id}/{engine}:{native_id}"


def parse(remote_key: object) -> tuple[str, str, str]:
    """``(peer_id, engine, native_id)`` of a remote key, or ``ValueError``."""
    if not isinstance(remote_key, str) or not remote_key.startswith(PREFIX):
        raise ValueError("not a remote key")
    peer_id, slash, rest = remote_key[len(PREFIX) :].partition("/")
    engine, colon, native_id = rest.partition(":")
    if not slash or not colon:
        raise ValueError("not a remote key")
    if key(peer_id, engine, native_id) != remote_key:
        raise ValueError("not a remote key")
    return peer_id, engine, native_id


def is_remote(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


# ── validating what a peer lists ──────────────────────────────────────
def clean_title(raw: object) -> str:
    """A display label: printable, one line, at most :data:`TITLE_MAX` characters after cleaning.
    Empty is allowed (an untitled session)."""
    if not isinstance(raw, str) or len(raw) > TITLE_RAW_MAX:
        raise Invalid("title")
    text = unicodedata.normalize("NFC", raw)
    text = "".join(" " if c.isspace() else c for c in text)
    text = "".join(c for c in text if unicodedata.category(c)[0] != "C")
    text = " ".join(text.split())
    if len(text) > TITLE_MAX:
        raise Invalid("title")
    return text


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise Invalid("number")
    try:
        number = float(value)
    except OverflowError:
        raise Invalid("number") from None
    if not math.isfinite(number):
        raise Invalid("number")
    return number


def clean_entry(raw: object) -> dict:
    """One ``share.list`` entry as a mirror row, a ``{"grant_id", "skipped": True}`` marker for an
    entry this build does not understand, or :class:`Invalid`.

    Two instances are updated independently, so the list must survive a peer that is one version
    ahead: a key this build does not know is ignored, and an entry whose ``level`` this build
    or ``kind`` this build does not know is SKIPPED (it is not listed here, and nothing is guessed
    about it). ``kind`` itself is optional: absent means ``api``. Everything
    this build does know is still checked strictly: a known field that is malformed fails the
    whole refresh, and the mirror stays as it was. An ``expires_at`` later than
    :data:`EXPIRES_AHEAD_MAX_S` from now is such a field: no owner sets one."""
    if not isinstance(raw, dict) or not ENTRY_KEYS <= set(raw) or len(raw) > ENTRY_KEYS_MAX:
        raise Invalid("entry")
    session = raw["session"]
    if not isinstance(session, dict) or set(session) != {"engine", "id"}:
        raise Invalid("session")
    engine, native_id = session["engine"], session["id"]
    if not valid_engine(engine) or not valid_native(engine, native_id):
        raise Invalid("session id")
    if not grants.valid_grant_id(raw["grant_id"]):
        raise Invalid("grant id")
    # Absent: an owner on the build before kinds existed, whose grants are all API grants.
    kind = raw.get("kind", grants.KIND_API)
    if not isinstance(kind, str) or not KIND_SHAPE_RE.fullmatch(kind):
        raise Invalid("kind")
    level = raw["level"]
    if not isinstance(level, str) or not LEVEL_SHAPE_RE.fullmatch(level):
        raise Invalid("level")
    if not grants.valid_revision(raw["revision"]):
        raise Invalid("revision")
    expires_at = _number(raw["expires_at"])
    if not 0 < expires_at <= grants._clock() + EXPIRES_AHEAD_MAX_S:
        raise Invalid("expires_at")
    title = clean_title(raw["title"])
    if kind not in grants.KINDS or not grants._level_allowed(kind, level):
        # A kind or a level from a newer build: well formed, not understood, not listed, and
        # never stored as another kind. Its id is kept so the page's order and progress checks
        # still see it.
        return {"grant_id": raw["grant_id"], "skipped": True}
    return {
        "grant_id": raw["grant_id"],
        "engine": engine,
        "native_id": native_id,
        "title": title,
        "kind": kind,
        "level": level,
        "expires_at": expires_at,
        "revision": raw["revision"],
    }


def clean_page(raw: object) -> tuple[int, list[dict], str | None]:
    """``(generation, entries, next)`` of one ``share.list`` answer, or :class:`Invalid`."""
    if not isinstance(raw, dict) or set(raw) != PAGE_KEYS:
        raise Invalid("page")
    generation, items, nxt = raw["generation"], raw["grants"], raw["next"]
    if type(generation) is not int or not 0 <= generation <= grants.REVISION_MAX:
        raise Invalid("generation")
    if not isinstance(items, list) or len(items) > sharing.LIST_LIMIT_MAX:
        raise Invalid("grants")
    if nxt is not None and not grants.valid_grant_id(nxt):
        raise Invalid("next")
    return generation, [clean_entry(item) for item in items], nxt


async def _collect(channel: ch.Channel) -> list[dict]:
    """Every page of the peer's list, validated, under ONE generation."""
    entries: list[dict] = []
    sessions: set[tuple[str, str]] = set()
    after: str | None = None
    last: str | None = None
    seen = 0
    generation: int | None = None
    while True:
        args: dict = {"limit": sharing.LIST_LIMIT_MAX}
        if after is not None:
            args["after"] = after
        page = await channel.call("share.list", args, timeout=LIST_CALL_TIMEOUT_S)
        gen, items, nxt = clean_page(page)
        if generation is None:
            generation = gen
        elif gen != generation:
            raise _Torn()
        for item in items:
            if last is not None and item["grant_id"] <= last:
                raise Invalid("grant ids out of order")
            last = item["grant_id"]
            seen += 1
            if seen > ENTRIES_MAX:
                raise Invalid("more entries than a peer may hold")
            if item.get("skipped"):
                continue
            session = (item["engine"], item["native_id"])
            if session in sessions:
                raise Invalid("one session listed twice")
            sessions.add(session)
            entries.append(item)
        if nxt is None:
            return entries
        if not items or nxt != items[-1]["grant_id"]:
            raise Invalid("a page that does not advance")
        after = nxt


async def refresh(fed, peer_id: str) -> bool:
    """Bring the mirror of ``peer_id`` up to date. True when its rows were replaced; on any
    failure the previous rows stay exactly as they were."""
    try:
        async with asyncio.timeout(REFRESH_TIMEOUT_S):
            for _attempt in range(1 + REFRESH_RETRIES):
                channel = fed.channel_for(peer_id)
                if channel is None:
                    return False
                try:
                    entries = await _collect(channel)
                except _Torn:
                    continue  # the owner's list moved between pages: read it again
                before = await asyncio.to_thread(fed.store.list_remote, peer_id)
                replaced = await asyncio.to_thread(fed.store.replace_remote, peer_id, entries)
                if replaced:
                    # A COMPLETE refresh, and only one: what is new is announced, and the bell
                    # rows of a grant the list no longer has are retracted (#2059). Every
                    # failure path below returns without reaching this.
                    fed.refreshed_remote(peer_id, before, entries)
                return replaced
            log.info("federation: a peer's list kept changing; the mirror is unchanged")
            return False
    except Invalid as exc:
        log.warning("federation: a peer's list was refused (%s); the mirror is unchanged", exc)
    except ch.CallError as exc:
        log.info("federation: a peer's list failed (%s); the mirror is unchanged", exc.code)
    except TimeoutError:
        log.info("federation: a peer's list timed out; the mirror is unchanged")
    except (store.StoreError, OSError) as exc:
        log.warning("federation: the mirror could not be written (%s)", type(exc).__name__)
    return False


# ── listing ───────────────────────────────────────────────────────────
def listing(fed) -> list[dict]:
    """What trusted peers share with this instance, as the operator API shows it. Blocking."""
    peers = {p.peer_id: p for p in fed.store.list_peers() if p.trusted}
    out = []
    for row in fed.store.list_remote():
        peer = peers.get(row.peer_id)
        if peer is None:
            continue  # a revoke removes the rows; a row that outlived one is not shown
        try:
            remote_key = key(row.peer_id, row.engine, row.native_id)
        except ValueError:
            continue  # stored under an older rule this build no longer accepts
        out.append(
            {
                "key": remote_key,
                "peer_id": row.peer_id,
                "peer_name": peer.name,
                "connected": fed.channel_for(row.peer_id) is not None,
                "grant_id": row.grant_id,
                "engine": row.engine,
                "native_id": row.native_id,
                "title": row.title,
                "kind": row.kind,
                "level": row.level,
                "expires_at": row.expires_at,
                "revision": row.revision,
                "seen_at": row.seen_at,
            }
        )
    return out


# ── reads ─────────────────────────────────────────────────────────────
def _bounded(value: object, depth: int = 0) -> None:
    """A JSON value no deeper than :data:`DEPTH_MAX`. The channel already capped its bytes."""
    if depth > DEPTH_MAX:
        raise Invalid("too deep")
    if isinstance(value, dict):
        for item in value.values():
            _bounded(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1)


def _cursor(value: object, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    if type(value) is not int or not 0 <= value <= grants.REVISION_MAX:
        raise Invalid("cursor")


def _link(doc: dict) -> None:
    link = doc.get("link")
    if not isinstance(link, str) or not 1 <= len(link) <= LINK_MAX:
        raise Invalid("link")


def _dicts(value: object, limit: int, what: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > limit:
        raise Invalid(what)
    if not all(isinstance(item, dict) for item in value):
        raise Invalid(what)
    return value


def _turns(value: object) -> None:
    for turn in _dicts(value, TURNS_MAX, "turns"):
        turn_id = turn.get("turn_id")
        if turn_id is not None and not chat_store.valid_turn_id(turn_id):
            raise Invalid("turn id")


CHOICE_RE = re.compile(r"\A[a-z][a-z_]{0,31}\Z")
CHOICES_MAX = 8
DECISIONS_MAX = 64
AUTHOR_VIAS = frozenset({"owner", "peer"})
NAME_RAW_MAX = 4 * store.NAME_MAX


def _choices(request: dict) -> None:
    """What the owner says this instance may answer of a request: decision KINDS by shape. The
    owner decides again on the call; this only keeps the list a list of short words."""
    choices = request.get("choices")
    if choices is None:
        return
    if not isinstance(choices, list) or len(choices) > CHOICES_MAX:
        raise Invalid("choices")
    if not all(isinstance(c, str) and CHOICE_RE.fullmatch(c) for c in choices):
        raise Invalid("choices")


def _offer(request: dict) -> None:
    """The owner's name for the exact request it offers choices for (``offers.token``): sent
    back unchanged with ``decision.answer``. By shape only; it means nothing here."""
    offer = request.get("offer")
    if offer is not None and not offers.valid_token(offer):
        raise Invalid("offer")


def _author(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or value.get("via") not in AUTHOR_VIAS:
        raise Invalid("author")
    name = value.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > NAME_RAW_MAX):
        raise Invalid("author name")


def _decisions(value: object) -> None:
    if value is None:
        return
    for entry in _dicts(value, DECISIONS_MAX, "decisions"):
        _author(entry.get("author"))
        if not isinstance(entry.get("answer"), str) or not CHOICE_RE.fullmatch(entry["answer"]):
            raise Invalid("decision")


def check_snapshot(doc: dict) -> dict:
    """A ``session.snapshot`` answer fit to hand to the viewer, or :class:`Invalid`."""
    _bounded(doc)
    _link(doc)
    _turns(doc.get("turns"))
    for request in _dicts(doc.get("pending_requests"), REQUESTS_MAX, "pending_requests"):
        _choices(request)
        _offer(request)
    _decisions(doc.get("decisions"))
    if type(doc.get("owner_present", False)) is not bool:
        raise Invalid("owner_present")
    if doc.get("can_send") is not False:
        raise Invalid("a view grant never sends")  # whatever the peer claims
    if type(doc.get("omitted_turns")) is not int or doc["omitted_turns"] < 0:
        raise Invalid("omitted_turns")
    _cursor(doc.get("revision"))
    _cursor(doc.get("event_cursor"))
    return doc


def check_events(doc: dict) -> dict:
    """A ``session.events`` answer: cursors and kinds, and nothing else."""
    if set(doc) != {"link", "revision", "next_cursor", "events"}:
        raise Invalid("events page")
    _link(doc)
    _cursor(doc["revision"])
    _cursor(doc["next_cursor"])
    for event in _dicts(doc["events"], sharing.EVENTS_LIMIT_MAX, "events"):
        if set(event) != {"cursor", "kind"}:
            raise Invalid("event")
        _cursor(event["cursor"], optional=False)
        if not isinstance(event["kind"], str) or not 1 <= len(event["kind"]) <= KIND_MAX:
            raise Invalid("event kind")
    return doc


def check_history(doc: dict) -> dict:
    """A ``session.history`` answer fit to hand to the viewer, or :class:`Invalid`."""
    _bounded(doc)
    if set(doc) != {"link", "turns", "before", "has_more"}:
        raise Invalid("history page")
    _link(doc)
    _turns(doc["turns"])
    if type(doc["has_more"]) is not bool:
        raise Invalid("has_more")
    if doc["before"] is not None and not chat_store.valid_turn_id(doc["before"]):
        raise Invalid("before")
    if doc["has_more"] and doc["before"] is None:
        raise Invalid("a page that cannot be continued")
    return doc


#: How long the CALLER waits for ``input.send``: past the owner's own handler timeout, so the
#: owner's ``timeout`` answer arrives instead of this side giving up first.
INPUT_CALL_TIMEOUT_S = ch.HANDLER_TIMEOUT_S + 10.0
SUGGESTION_STATES = frozenset(store.SUGGESTION_STATES)
HANDOFFS = frozenset({"queued", "uncertain", "sent", "not_sent"})
SUGGESTIONS_MAX = 50


def _suggestion_ref(doc: dict) -> None:
    if not grants.valid_grant_id(doc.get("suggestion_id")):
        raise Invalid("suggestion id")
    if doc.get("state") not in SUGGESTION_STATES:
        raise Invalid("suggestion state")


def check_suggestion(doc: dict) -> dict:
    """An ``input.suggest`` or ``input.withdraw`` answer: an id and a state, nothing else."""
    if set(doc) != {"suggestion_id", "state"}:
        raise Invalid("suggestion")
    _suggestion_ref(doc)
    return doc


def check_suggestions(doc: dict) -> dict:
    """An ``input.suggestions`` answer: ids, retry keys, states and times. No text."""
    if set(doc) != {"suggestions"}:
        raise Invalid("suggestions")
    keys = {"suggestion_id", "client_key", "state", "handoff", "created_at", "updated_at"}
    for row in _dicts(doc["suggestions"], SUGGESTIONS_MAX, "suggestions"):
        if set(row) != keys:
            raise Invalid("suggestion")
        _suggestion_ref(row)
        if not isinstance(row["client_key"], str) or len(row["client_key"]) > 64:
            raise Invalid("client key")
        if row["handoff"] is not None and row["handoff"] not in HANDOFFS:
            raise Invalid("handoff")
        _number(row["created_at"])
        _number(row["updated_at"])
    return doc


def check_sent(doc: dict) -> dict:
    """An ``input.send`` answer: the owner knows the operation, and the runtime's receipt (or
    ``null`` when that runtime gives none). Nothing here is read as more than it says."""
    if set(doc) != {"operation_id_known", "handoff"} or doc["operation_id_known"] is not True:
        raise Invalid("send answer")
    if doc["handoff"] is not None and doc["handoff"] not in HANDOFFS:
        raise Invalid("handoff")
    return doc


def check_status(doc: dict) -> dict:
    """An ``input.status`` answer: unknown, a sent message's receipt, or a suggestion's state.
    Exactly these shapes; anything else from a peer is refused here."""
    if doc == {"known": False} and doc["known"] is False:
        return doc
    if doc.get("known") is not True:
        raise Invalid("status")
    if doc.get("kind") == "send":
        if set(doc) != {"known", "kind", "handoff"}:
            raise Invalid("status")
    elif doc.get("kind") == "suggest":
        if set(doc) != {"known", "kind", "suggestion_id", "state", "handoff"}:
            raise Invalid("status")
        _suggestion_ref(doc)
    else:
        raise Invalid("status kind")
    if doc["handoff"] is not None and doc["handoff"] not in HANDOFFS:
        raise Invalid("handoff")
    return doc


ACCESS_STATES = frozenset(store.ACCESS_STATES)
DECISION_OUTCOMES = frozenset({"accepted", "settled", "uncertain"})
NOTE_KEYS = frozenset(
    {"note_id", "author", "mine", "actor", "text", "created_at", "edited_at", "deleted"}
)
NOTES_PAGE_MAX = 100


def _level(value: object) -> None:
    if not isinstance(value, str) or not LEVEL_SHAPE_RE.fullmatch(value):
        raise Invalid("level")


def check_access(doc: dict) -> dict:
    """An ``access.request`` answer: an id, the level asked for and a state."""
    if set(doc) != {"request_id", "level", "state"}:
        raise Invalid("access request")
    if not grants.valid_grant_id(doc["request_id"]) or doc["state"] not in ACCESS_STATES:
        raise Invalid("access request")
    _level(doc["level"])
    return doc


def check_access_status(doc: dict) -> dict:
    """An ``access.status`` answer: the grant's level and this instance's newest request."""
    if set(doc) != {"level", "request"}:
        raise Invalid("access status")
    _level(doc["level"])
    request = doc["request"]
    if request is None:
        return doc
    keys = {"request_id", "level", "state", "created_at", "updated_at", "expires_at"}
    if not isinstance(request, dict) or set(request) != keys:
        raise Invalid("access status")
    if not grants.valid_grant_id(request["request_id"]) or request["state"] not in ACCESS_STATES:
        raise Invalid("access status")
    _level(request["level"])
    for key in ("created_at", "updated_at", "expires_at"):
        _number(request[key])
    return doc


def check_decision(doc: dict) -> dict:
    """A ``decision.answer`` answer: what became of it and who settled the request."""
    if set(doc) != {"outcome", "author"} or doc["outcome"] not in DECISION_OUTCOMES:
        raise Invalid("decision answer")
    _author(doc["author"])
    return doc


def _note(note: object) -> None:
    """One note as the owner tells it. Its text is plain text of bounded length; it is handed
    to the operator's pane as data and is never stored, parsed or passed anywhere else."""
    if not isinstance(note, dict) or set(note) != NOTE_KEYS:
        raise Invalid("note")
    if not grants.valid_grant_id(note["note_id"]) or note["author"] not in AUTHOR_VIAS:
        raise Invalid("note")
    if type(note["mine"]) is not bool or type(note["deleted"]) is not bool:
        raise Invalid("note")
    if not isinstance(note["text"], str) or len(note["text"]) > store.NOTE_TEXT_MAX:
        raise Invalid("note text")
    if not isinstance(note["actor"], str) or len(note["actor"]) > NAME_RAW_MAX:
        raise Invalid("note actor")
    _number(note["created_at"])
    if note["edited_at"] is not None:
        _number(note["edited_at"])


def check_notes(doc: dict) -> dict:
    """A ``note.list`` answer: the changes after a number, or the thread again."""
    if set(doc) != {"reset", "seq", "changes", "more", "next", "unread"}:
        raise Invalid("notes page")
    if type(doc["reset"]) is not bool or type(doc["more"]) is not bool:
        raise Invalid("notes page")
    _cursor(doc["seq"], optional=False)
    _cursor(doc["unread"], optional=False)
    if doc["next"] is not None and not grants.valid_grant_id(doc["next"]):
        raise Invalid("notes cursor")
    for entry in _dicts(doc["changes"], NOTES_PAGE_MAX, "changes"):
        _cursor(entry.get("seq"), optional=False)
        if not grants.valid_grant_id(entry.get("note_id")):
            raise Invalid("note id")
        if set(entry) == {"seq", "note_id", "removed"}:
            if entry["removed"] is not True:
                raise Invalid("note change")
        elif set(entry) == {"seq", "note_id", "note"}:
            _note(entry["note"])
            if entry["note"]["note_id"] != entry["note_id"]:
                raise Invalid("note change")
        else:
            raise Invalid("note change")
    return doc


def check_note(doc: dict) -> dict:
    """A ``note.add`` / ``note.edit`` / ``note.delete`` answer: the note, and for an add its
    change number."""
    if set(doc) - {"note", "seq"} or "note" not in doc:
        raise Invalid("note answer")
    _note(doc["note"])
    if "seq" in doc:
        _cursor(doc["seq"], optional=False)
    return doc


def check_unread(doc: dict) -> dict:
    """A ``note.read`` answer: how many of the other side's notes are still unread."""
    if set(doc) != {"unread"}:
        raise Invalid("unread")
    _cursor(doc["unread"], optional=False)
    return doc


def check_frame(doc: dict, have: str | None = None) -> dict:
    """A ``terminal.frame`` answer fit to hand to the viewer, or :class:`Invalid`. Exactly one of
    three shapes: ``{settling: true}``; ``{fingerprint, unchanged: true}``, and then only for the
    fingerprint this instance sent as ``have``; or a frame, whose lines are at most ``rows``
    strings of at most ``cols`` characters with no control character among them."""
    if not isinstance(doc, dict):
        raise Invalid("frame")
    keys = set(doc)
    if keys == FRAME_SETTLING_KEYS:
        if doc["settling"] is not True:
            raise Invalid("settling")
        return doc
    if not frames.valid_fingerprint(doc.get("fingerprint")):
        raise Invalid("fingerprint")
    if keys == FRAME_UNCHANGED_KEYS:
        if doc["unchanged"] is not True or have is None or doc["fingerprint"] != have:
            raise Invalid("unchanged")
        return doc
    if keys != FRAME_KEYS:
        raise Invalid("frame")
    rows, cols = doc["rows"], doc["cols"]
    if type(rows) is not int or not 1 <= rows <= frames.ROWS_MAX:
        raise Invalid("rows")
    if type(cols) is not int or not 1 <= cols <= frames.COLS_MAX:
        raise Invalid("cols")
    if rows * cols > FRAME_CELLS_MAX:
        raise Invalid("a larger grid than an owner renders")
    if type(doc["settled"]) is not bool:
        raise Invalid("settled")
    try:
        frames.check_lines(doc["lines"], rows, cols)
    except ValueError as exc:
        raise Invalid(str(exc)) from None
    return doc


def events_timeout(wait: float) -> float:
    """How long the CALLER waits for a held call (``session.events``, ``terminal.frame``): the
    wait it asked for, plus a margin for the owner's checks and the way back. The channel's
    default call timeout (20 s) would fire before a 20 s wait answers, so it is never used for
    these verbs."""
    return float(wait) + sharing.WAIT_CALL_MARGIN_S


#: A WRITE whose channel ended after the request was handed to it (#2061): the owner may have
#: admitted it, or not. Never ``peer_offline``, which says nothing was sent.
PEER_LOST = "peer_lost"


async def _call(channel, verb: str, args: dict, timeout: float, *, write: bool = False) -> dict:
    """One call. A refusal by the owner is returned as ``None``; every other failure is a
    :class:`ReadError`.

    A channel that closed is ``peer_offline`` for a read (it has no effect, and the peer is not
    connected now). For a ``write`` it is ``peer_offline`` only when the channel says the
    request never left (``CallError.dispatched`` is false: refused before anything was queued);
    otherwise it is ``peer_lost``: the request was queued and the answer never came, so the owner
    may have carried it out. Nothing here calls again."""
    try:
        return await channel.call(verb, args, timeout=timeout)
    except ch.CallError as exc:
        if exc.code == sharing.NO_GRANT:
            return None
        if exc.code == ch.ANSWER_LOST:
            # The owner's handler ran and its answer was withheld (its byte budget, a size cap).
            # A read had no effect and is simply asked again later: `busy`, as before. A write
            # may have been carried out: the same answer as a channel lost under it.
            raise ReadError(PEER_LOST if write else "busy") from None
        if exc.code != "closed":
            raise ReadError(exc.code) from None
        raise ReadError(PEER_LOST if write and exc.dispatched else "peer_offline") from None


async def _re_resolve(fed, peer_id: str, grant_id: str):
    """After the owner refused: refresh this peer's mirror ONCE, here and now, and return the
    grant's row as the owner lists it now. :class:`ReadError` ``no_grant`` when the refreshed
    list no longer has it (or the peer is not trusted any more); ``peer_offline`` or
    ``unavailable`` when the list could not be read, because then nothing is known and nothing
    is final."""
    try:
        async with asyncio.timeout(RE_RESOLVE_TIMEOUT_S):
            refreshed = await refresh(fed, peer_id)
    except TimeoutError:
        refreshed = False
    peer = await asyncio.to_thread(fed.store.get, peer_id)
    if peer is None or not peer.trusted:
        raise ReadError(sharing.NO_GRANT)
    if not refreshed:
        raise ReadError("peer_offline" if fed.channel_for(peer_id) is None else "unavailable")
    row = await asyncio.to_thread(fed.store.get_remote, peer_id, grant_id)
    if row is None:
        raise ReadError(sharing.NO_GRANT)
    return row


async def read(
    fed,
    peer_id: str,
    grant_id: str,
    verb: str,
    extra: dict,
    *,
    check,
    timeout: float = ch.CALL_TIMEOUT_S,
    kind: str = grants.KIND_API,
    retry: bool = True,
    revision: int | None = None,
) -> dict:
    """Call ``verb`` for a mirrored grant on its owner's live channel and return the validated
    answer. ``kind`` is what the verb reads: a mirrored grant of the other kind is ``no_grant``
    here, without a call (the owner would refuse it the same way). Raises :class:`ReadError`:
    ``no_grant`` (not shared, or the owner refused),
    ``peer_offline`` (no channel), ``bad_answer`` (the answer was refused here), or the call's own
    code.

    **A refusal is re-resolved before it is final** (#2055). The owner answers one refusal for
    every cause, so a grant whose revision moved (a renewed expiry) looks like an ended one until
    this side reads the list again. On a refusal the mirror of that peer is refreshed ONCE and the
    call is made ONCE more with the revision the refreshed list carries. Only a second refusal,
    or a grant the refreshed list no longer has, is ``no_grant``. A refresh that fails is
    ``peer_offline`` / ``unavailable`` (503): nothing is answered from the mirror or from an
    earlier answer. A FINAL refusal retracts this instance's bell rows about the grant (#2059);
    a refusal that was re-resolved retracts nothing.

    **A refused WRITE is never repeated** (``retry=False``: ``input.suggest``, ``input.send``,
    ``input.withdraw``; and of working together (#2059) ``access.request``, ``decision.answer``,
    ``note.add`` / ``note.edit`` / ``note.delete`` / ``note.read``, and Continue here's create).
    The refusal is returned as it is (``no_grant``); the mirror is refreshed in the background so
    the next call uses the current revision, and that refresh, not the refusal, decides the bell.
    Sending a message a second time is the operator's decision, never this function's: a
    repeated send could deliver it under a grant the owner's operator just changed.

    **A write's lost answer is not a refusal** (#2061). ``peer_offline`` for a write means the
    request provably never left: no channel, or the channel refused it before queuing anything.
    A channel that ended after the request was queued is ``peer_lost`` (504): the owner may have
    carried it out. So is an answer the owner withheld AFTER its handler ran (``answer_lost``:
    its byte budget or a size cap), which is never passed on as ``busy`` or ``too_large``.
    It is never retried here; the caller asks ``input.status`` for the same key.

    ``revision`` pins the call to a revision the caller was BOUND to earlier (#2059, Continue
    here) and not to whatever the mirror holds now: when the grant moved since, the owner
    refuses, which is the point. A pinned call is therefore never re-resolved (that would be a
    call under another revision): it must say ``retry=False``."""
    answer, _ = await read_bound(
        fed,
        peer_id,
        grant_id,
        verb,
        extra,
        check=check,
        timeout=timeout,
        kind=kind,
        retry=retry,
        revision=revision,
    )
    return answer


async def read_bound(
    fed,
    peer_id: str,
    grant_id: str,
    verb: str,
    extra: dict,
    *,
    check,
    timeout: float = ch.CALL_TIMEOUT_S,
    kind: str = grants.KIND_API,
    retry: bool = True,
    revision: int | None = None,
) -> tuple[dict, int]:
    """:func:`read`, and the revision the owner ANSWERED under: after a re-resolved refusal that
    is the refreshed one, not the one the first call carried. What a caller binds to (Continue
    here's handle) is this revision and no other."""
    if revision is not None and retry:
        raise ValueError("a call pinned to a revision is never re-resolved: pass retry=False")
    row = await asyncio.to_thread(fed.store.get_remote, peer_id, grant_id)
    peer = await asyncio.to_thread(fed.store.get, peer_id)
    if row is None or peer is None or not peer.trusted or row.kind != kind:
        raise ReadError(sharing.NO_GRANT)
    channel = fed.channel_for(peer_id)
    if channel is None:
        raise ReadError("peer_offline")
    used = row.revision if revision is None else revision
    # `retry=False` is a write (or the pinned read that gates one): a lost answer is not a refusal.
    answer = await _call(
        channel, verb, {"grant_id": grant_id, "revision": used, **extra}, timeout, write=not retry
    )
    if answer is None:
        if not retry:
            fed.refresh_remote(peer_id)
            raise ReadError(sharing.NO_GRANT)
        row = await _re_resolve(fed, peer_id, grant_id)
        if row.kind != kind:
            # The refreshed list has this grant as the other kind: the same answer as before the
            # first call, and no second one.
            raise ReadError(sharing.NO_GRANT)
        channel = fed.channel_for(peer_id)
        if channel is None:
            raise ReadError("peer_offline")
        used = row.revision
        answer = await _call(
            channel, verb, {"grant_id": grant_id, "revision": used, **extra}, timeout
        )
        if answer is None:
            # Refused under the revision the owner itself just listed: final. Whatever this
            # instance's bell says about that grant is no longer true (#2059).
            fed.refused_remote(peer_id, grant_id)
            raise ReadError(sharing.NO_GRANT)
    try:
        return check(answer), used
    except Invalid as exc:
        log.warning("federation: a peer's %s answer was refused (%s)", verb, exc)
        raise ReadError("bad_answer") from None
