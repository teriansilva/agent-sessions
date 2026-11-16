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

from .. import chat_store, engines
from ..plugins import kinds
from . import channel as ch
from . import grants, identity, sharing, store

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
#: Bounds on a projection passed through to the viewer.
DEPTH_MAX = 12
TURNS_MAX = 1000
REQUESTS_MAX = 1000
KIND_MAX = 64
LINK_MAX = 64
ENTRY_KEYS = frozenset({"grant_id", "session", "title", "level", "expires_at", "revision"})
PAGE_KEYS = frozenset({"generation", "grants", "next"})
#: An entry may carry keys from a newer build; this bounds how many.
ENTRY_KEYS_MAX = 16
LEVEL_SHAPE_RE = re.compile(r"\A[a-z][a-z_]{0,31}\Z")


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
    does not know is SKIPPED (it is not listed here, and nothing is guessed about it). Everything
    this build does know is still checked strictly: a known field that is malformed fails the
    whole refresh, and the mirror stays as it was."""
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
    level = raw["level"]
    if not isinstance(level, str) or not LEVEL_SHAPE_RE.fullmatch(level):
        raise Invalid("level")
    if not grants.valid_revision(raw["revision"]):
        raise Invalid("revision")
    expires_at = _number(raw["expires_at"])
    if expires_at <= 0:
        raise Invalid("expires_at")
    title = clean_title(raw["title"])
    if level not in grants.LEVELS:
        # A level from a newer build: well formed, not understood, not listed. Its id is kept so
        # the page's order and progress checks still see it.
        return {"grant_id": raw["grant_id"], "skipped": True}
    return {
        "grant_id": raw["grant_id"],
        "engine": engine,
        "native_id": native_id,
        "title": title,
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
                return await asyncio.to_thread(fed.store.replace_remote, peer_id, entries)
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


def check_snapshot(doc: dict) -> dict:
    """A ``session.snapshot`` answer fit to hand to the viewer, or :class:`Invalid`."""
    _bounded(doc)
    _link(doc)
    _turns(doc.get("turns"))
    _dicts(doc.get("pending_requests"), REQUESTS_MAX, "pending_requests")
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


def events_timeout(wait: float) -> float:
    """How long the CALLER waits for ``session.events``: the wait it asked for, plus a margin for
    the owner's checks and the way back. The channel's default call timeout (20 s) would fire
    before a 20 s wait answers, so it is never used for this verb."""
    return float(wait) + sharing.WAIT_CALL_MARGIN_S


async def read(
    fed,
    peer_id: str,
    grant_id: str,
    verb: str,
    extra: dict,
    *,
    check,
    timeout: float = ch.CALL_TIMEOUT_S,
) -> dict:
    """Call ``verb`` for a mirrored grant on its owner's live channel and return the validated
    answer. Raises :class:`ReadError`: ``no_grant`` (not shared, or the owner refused),
    ``peer_offline`` (no channel), ``bad_answer`` (the answer was refused here), or the call's own
    code."""
    row = await asyncio.to_thread(fed.store.get_remote, peer_id, grant_id)
    peer = await asyncio.to_thread(fed.store.get, peer_id)
    if row is None or peer is None or not peer.trusted:
        raise ReadError(sharing.NO_GRANT)
    channel = fed.channel_for(peer_id)
    if channel is None:
        raise ReadError("peer_offline")
    args = {"grant_id": grant_id, "revision": row.revision, **extra}
    try:
        answer = await channel.call(verb, args, timeout=timeout)
    except ch.CallError as exc:
        if exc.code == sharing.NO_GRANT:
            # The grant ended or its revision moved: the mirror is stale, ask for the list again.
            fed.refresh_remote(peer_id)
        raise ReadError("peer_offline" if exc.code == "closed" else exc.code) from None
    try:
        return check(answer)
    except Invalid as exc:
        log.warning("federation: a peer's %s answer was refused (%s)", verb, exc)
        raise ReadError("bad_answer") from None
