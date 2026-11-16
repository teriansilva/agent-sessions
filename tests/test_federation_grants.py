"""Grants (#2054): the store's schema 2, the owner rules, the per-call check and the archive fence.

The store is a real file under the test's tmp directory. Sessions are named by key only: the
`live` fixture answers `shares.session_state` for them, so a test decides what "exists and is not
archived" says at each moment.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import stat
import threading
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_sessions import shares
from agent_sessions.federation import grants, noise, store

TERMINAL = "claude:11111111-1111-1111-1111-111111111111"


def api_key() -> str:
    return f"codex-api:{uuid.uuid4()}"


@pytest.fixture
def live(monkeypatch):
    """``{session key: state}``; a key that is not named is ``live``."""
    states: dict[str, str] = {}
    monkeypatch.setattr(
        shares,
        "session_state",
        lambda prov, native: states.get(f"{prov.engine_id}:{native}", "live"),
    )
    return states


@pytest.fixture
def st(tmp_path):
    return store.Store(tmp_path / "fed")


def trusted(st: store.Store, name: str = "box") -> store.Peer:
    return st.trust(noise.public_bytes(X25519PrivateKey.generate()), name=name, endpoints=[])


def grant(st: store.Store, peer: store.Peer, key: str | None = None, **kw) -> store.Grant:
    return grants.create(
        st,
        session_key=key or api_key(),
        peer_id=peer.peer_id,
        level=kw.pop("level", "view"),
        expires_in=kw.pop("expires_in", None),
        **kw,
    )


def refusal(fn, *args, **kwargs) -> grants.GrantError:
    with pytest.raises(grants.GrantError) as exc:
        fn(*args, **kwargs)
    return exc.value


# ── the store: schema 2 ───────────────────────────────────────────────


def _schema_1_file(directory) -> bytes:
    """A store exactly as the previous build (#2049) left it, with one trusted peer."""
    directory.mkdir(parents=True)
    path = directory / store.DB_FILE
    con = sqlite3.connect(path)
    for stmt in store._SCHEMA_V1:
        con.execute(stmt)
    key = noise.public_bytes(X25519PrivateKey.generate())
    con.execute(
        "INSERT INTO peers (peer_id, public_key, name, state, endpoints, revision, created_at,"
        " updated_at) VALUES (?, ?, 'old box', 'trusted', '[]', 1, 1.0, 1.0)",
        ("a" * 32, key),
    )
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()
    return key


def test_schema_1_migrates_in_place_and_a_backup_is_taken_first(tmp_path):
    directory = tmp_path / "fed"
    key = _schema_1_file(directory)
    st = store.Store(directory)
    assert st.by_key(key).name == "old box"  # the first use migrates, and the peer is still there

    con = sqlite3.connect(st.db_path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 2
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"peers", "grants", "grant_events", "remote_shares", "meta"} <= tables
    con.close()
    assert st.generation() == 0

    (backup,) = directory.glob(f"{store.DB_FILE}.bak-v1-*")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    old = sqlite3.connect(backup)
    # Taken BEFORE the migration: it is still schema 1, with the peer and without a grants table.
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1
    assert old.execute("SELECT name FROM peers").fetchone()[0] == "old box"
    assert not old.execute("SELECT 1 FROM sqlite_master WHERE name='grants'").fetchone()
    old.close()

    # A second open migrates nothing and copies nothing; a new store has nothing to back up.
    store.Store(directory).list_peers()
    assert len(list(directory.glob("*.bak-*"))) == 1
    fresh = store.Store(tmp_path / "new")
    trusted(fresh)
    assert list((tmp_path / "new").glob("*.bak-*")) == []


def test_a_store_from_a_newer_build_is_refused_untouched(st, live):
    peer = trusted(st)
    made = grant(st, peer)
    con = sqlite3.connect(st.db_path)
    con.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    con.close()
    before = st.db_path.read_bytes()
    other = store.Store(st.db_path.parent)
    for call in (
        other.list_peers,
        other.generation,
        lambda: other.get_grant(made.grant_id),
        lambda: other.revoke_grant(made.grant_id),
        lambda: other.end_grants_for_session(made.session_key),  # so an archive fails closed
        lambda: other.expire_due(),
        lambda: other.list_remote(),
        # ...and the object that opened it before the newer build wrote it
        lambda: st.revoke_grant(made.grant_id),
    ):
        with pytest.raises(store.StoreUnsupported):
            call()
    assert st.db_path.read_bytes() == before
    assert list(st.db_path.parent.glob("*.bak-*")) == []
    # the per-call check reads it as "no": nothing is served from a store it cannot read
    assert grants.authorize(other, made.grant_id, peer.peer_id, 1) is None


def test_reading_never_creates_the_store(tmp_path):
    st = store.Store(tmp_path / "never")
    assert st.generation() == 0
    assert st.get_grant("x" * 22) is None
    assert st.grant_and_peer_state("x" * 22, "a" * 32) == (None, None)
    assert st.list_grants() == [] and st.live_grants_of("a" * 32) == (0, None, [])
    assert st.grant_events("x" * 22) == [] and st.grant_marks() == {}
    assert st.expire_due() == [] and st.end_grants_for_session("codex-api:x") == []
    assert st.list_remote() == [] and st.get_remote("a" * 32, "x" * 22) is None
    assert grants.authorize(st, "x" * 22, "a" * 32, 1) is None
    assert not (tmp_path / "never").exists()


def test_grant_events_are_append_only(st, live):
    made = grant(st, trusted(st))
    con = sqlite3.connect(st.db_path)
    for stmt in ("UPDATE grant_events SET actor = 'system'", "DELETE FROM grant_events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(stmt)
    con.close()
    assert [e["event"] for e in st.grant_events(made.grant_id)] == ["granted"]


# ── the owner rules ───────────────────────────────────────────────────


def test_a_grant_needs_a_local_api_session_a_trusted_peer_and_a_bounded_expiry(st, live):
    peer = trusted(st)
    before = time.time()
    made = grant(st, peer)
    assert made.state == "live" and made.level == "view" and made.revision == 1
    assert made.principal == f"peer:{peer.peer_id}" and made.peer_id == peer.peer_id
    assert grants.GRANT_ID_RE.fullmatch(made.grant_id)
    # the default: seven days for `view`
    assert 7 * 86400 - 5 < made.expires_at - before < 7 * 86400 + 5
    assert st.grant_events(made.grant_id) == [
        {"at": made.created_at, "event": "granted", "actor": "operator"}
    ]
    assert st.generation() == 1

    # a terminal session is refused here (P4b), as is anything that is not a local session key
    assert refusal(grant, st, peer, TERMINAL).code == "not_shareable"
    for key in ("nope:1", f"peer:{'a' * 32}/codex-api:{uuid.uuid4()}", "", None, 7):
        assert refusal(
            grants.create, st, session_key=key, peer_id=peer.peer_id, level="view", expires_in=None
        ).status in (404, 422)

    # the session must exist and not be archived, as the inside-the-lock re-check sees it
    for state, status in (("unknown", 404), ("archived", 409), ("unreadable", 503)):
        key = api_key()
        live[key] = state
        assert refusal(grant, st, peer, key).status == status

    # the peer must be a known, trusted one
    unknown = refusal(
        grants.create, st, session_key=api_key(), peer_id="b" * 32, level="view", expires_in=None
    )
    assert unknown.status == 404
    for bad in ("../x", "", None, "A" * 32):
        assert (
            refusal(
                grants.create, st, session_key=api_key(), peer_id=bad, level="view", expires_in=None
            ).status
            == 404
        )
    gone = trusted(st, "gone")
    st.revoke(gone.peer_id)
    assert refusal(grant, st, gone).status == 409

    # the expiry is the operator's pick, at most 90 days
    longest = grant(st, peer, expires_in=90 * 86400)
    assert longest.expires_at - time.time() > 90 * 86400 - 5
    for bad in (90 * 86400 + 1, 0, -5, 1, True, "3600", float("nan"), float("inf"), 10**400, []):
        assert refusal(grant, st, peer, expires_in=bad).code == "invalid", bad
    # only `view` in this phase
    for level in ("send", "suggest", "", None, 1):
        assert refusal(grant, st, peer, level=level).code == "invalid"

    # one live grant per session and peer; another peer may hold its own
    assert refusal(grant, st, peer, made.session_key).status == 409
    assert grant(st, trusted(st, "second"), made.session_key).state == "live"


def test_the_201st_live_grant_for_one_peer_is_refused(st, live, monkeypatch):
    peer, other = trusted(st), trusted(st, "other")
    made = [grant(st, peer) for _ in range(store.GRANTS_PER_PEER_MAX)]
    assert len(st.live_grants_of(peer.peer_id)[2]) == 200
    over = refusal(grant, st, peer)
    assert over.status == 409 and "200" in over.detail
    assert grant(st, other).state == "live"  # the cap is per peer
    grants.revoke(st, made[0].grant_id)
    assert grant(st, peer).state == "live"  # an ended grant frees its place
    assert refusal(grant, st, peer).status == 409
    # ...and so does one that ran out, without waiting for the sweep
    soon = made[1].expires_at + 1
    monkeypatch.setattr(grants, "_clock", lambda: soon)
    assert grant(st, peer).state == "live"
    assert st.get_grant(made[1].grant_id).state == "expired"


def test_changing_a_grant_bumps_its_revision_and_the_generation(st, live):
    peer = trusted(st)
    made = grant(st, peer)
    changed = grants.change(st, made.grant_id, expires_in=3600, revision=1)
    assert changed.revision == 2 and changed.expires_at < made.expires_at
    assert st.generation() == 2
    assert [e["event"] for e in st.grant_events(made.grant_id)] == ["granted", "changed"]
    # the operator's own stale write is refused, and so is a malformed one
    assert refusal(grants.change, st, made.grant_id, expires_in=3600, revision=1).status == 409
    for revision in (None, True, 0, "2", 2.0):
        bad = refusal(grants.change, st, made.grant_id, expires_in=3600, revision=revision)
        assert bad.status == 422
    assert refusal(grants.change, st, made.grant_id, expires_in=None, revision=2).status == 422
    assert refusal(grants.change, st, made.grant_id, expires_in=10**9, revision=2).status == 422
    assert refusal(grants.change, st, "x" * 22, expires_in=3600, revision=1).status == 404
    assert refusal(grants.change, st, "../etc", expires_in=3600, revision=1).status == 404
    assert st.generation() == 2  # a refused change moved nothing

    ended = grants.revoke(st, made.grant_id)
    assert ended.state == "revoked" and ended.end_reason == "revoked" and ended.ended_at
    assert st.generation() == 3
    assert grants.revoke(st, made.grant_id).state == "revoked"  # idempotent
    assert st.generation() == 3 and len(st.grant_events(made.grant_id)) == 3
    assert refusal(grants.change, st, made.grant_id, expires_in=3600, revision=2).status == 409
    assert refusal(grants.revoke, st, "y" * 22).status == 404
    assert grants.listing(st) == [] and grants.listing(st, made.session_key) == []


def test_expiry_is_checked_at_use_and_swept(st, live, monkeypatch):
    peer = trusted(st)
    made = grant(st, peer, expires_in=60)
    assert grants.authorize(st, made.grant_id, peer.peer_id, 1) == made
    later = made.expires_at + 0.001
    monkeypatch.setattr(grants, "_clock", lambda: later)
    # at use: refused at once, though no sweep has run and the row still says live
    assert grants.authorize(st, made.grant_id, peer.peer_id, 1) is None
    assert st.get_grant(made.grant_id).state == "live"
    assert grants.listing(st) == [] and st.live_grants_of(peer.peer_id, now=later)[2] == []
    generation = st.generation()
    (swept,) = grants.sweep(st)
    assert swept.grant_id == made.grant_id
    after = st.get_grant(made.grant_id)
    assert after.state == "expired" and after.end_reason == "expired"
    assert st.grant_events(made.grant_id)[-1] == {
        "at": later,
        "event": "expired",
        "actor": "system",
    }
    assert st.generation() == generation + 1
    assert grants.sweep(st) == [] and st.generation() == generation + 1


# ── the per-call check ────────────────────────────────────────────────


def test_authorize_is_one_answer_for_everything_that_is_not_this_peers_live_grant(
    st, live, monkeypatch, caplog
):
    peer, other = trusted(st), trusted(st, "other")
    mine, theirs = grant(st, peer), grant(st, other)
    caplog.set_level(logging.INFO, logger=grants.__name__)

    def refused(grant_id, peer_id, revision, why: str) -> None:
        caplog.clear()
        assert grants.authorize(st, grant_id, peer_id, revision) is None
        # the reason stays here, in the owner's log
        assert why in caplog.text, (why, caplog.text)

    assert grants.authorize(st, mine.grant_id, peer.peer_id, 1) == mine
    refused("z" * 22, peer.peer_id, 1, "unknown grant")
    refused(theirs.grant_id, peer.peer_id, 1, "another principal")
    refused(mine.grant_id, other.peer_id, 1, "another principal")
    refused(mine.grant_id, peer.peer_id, 2, "stale revision")
    refused(mine.grant_id, "c" * 32, 1, "peer not trusted")
    for malformed in ("", None, "../../x", "x" * 200, 7, ["a"]):
        refused(malformed, peer.peer_id, 1, "malformed")
    for revision in (None, True, 0, -1, 1.0, "1", 2**60):
        refused(mine.grant_id, peer.peer_id, revision, "malformed")

    # a revision change: the old one is refused, the new one served
    grants.change(st, mine.grant_id, expires_in=3600, revision=1)
    refused(mine.grant_id, peer.peer_id, 1, "stale revision")
    assert grants.authorize(st, mine.grant_id, peer.peer_id, 2) is not None

    # the session: archived, gone, unreadable, or no longer an API session of a known agent
    for state in ("archived", "unknown", "unreadable"):
        live[mine.session_key] = state
        refused(mine.grant_id, peer.peer_id, 2, f"session {state}")
    live.clear()
    con = sqlite3.connect(st.db_path)
    con.execute("UPDATE grants SET session_key = ? WHERE grant_id = ?", (TERMINAL, mine.grant_id))
    con.commit()
    refused(mine.grant_id, peer.peer_id, 2, "no longer a local API session")
    con.execute(
        "UPDATE grants SET session_key = ?, level = 'send' WHERE grant_id = ?",
        (mine.session_key, mine.grant_id),
    )
    con.commit()
    refused(mine.grant_id, peer.peer_id, 2, "level not served")  # a level from a later build
    con.execute("UPDATE grants SET level = 'view' WHERE grant_id = ?", (mine.grant_id,))
    con.commit()
    con.close()
    assert grants.authorize(st, mine.grant_id, peer.peer_id, 2) is not None

    # expiry, on this instance's clock
    with monkeypatch.context() as m:
        m.setattr(grants, "_clock", lambda: time.time() + 3601)
        refused(mine.grant_id, peer.peer_id, 2, "grant expired")

    # a revoke of the peer written by ANOTHER process over the same store
    store.Store(st.db_path.parent).revoke(other.peer_id)
    refused(theirs.grant_id, other.peer_id, 1, "peer not trusted")
    # ...and an operator revoke of the grant
    grants.revoke(st, mine.grant_id)
    refused(mine.grant_id, peer.peer_id, 2, "grant ended (revoked)")


# ── revoking a peer ───────────────────────────────────────────────────


def test_revoking_a_peer_ends_its_grants_in_the_same_transaction(st, live, monkeypatch):
    peer, other = trusted(st), trusted(st, "other")
    first, second, kept = grant(st, peer), grant(st, peer), grant(st, other)
    st.replace_remote(peer.peer_id, [_remote_row()])
    st.replace_remote(other.peer_id, [_remote_row()])
    generation = st.generation()

    # A failure AFTER the peer row was updated, before the transaction commits: nothing changed.
    real = store.Store._end_grants

    def fails(self, con, *args, **kwargs):
        assert (
            con.execute("SELECT state FROM peers WHERE peer_id = ?", (peer.peer_id,)).fetchone()[0]
            == "revoked"
        )  # the peer row has changed inside this transaction
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as m:
        m.setattr(store.Store, "_end_grants", fails)
        with pytest.raises(sqlite3.OperationalError):
            st.revoke(peer.peer_id)
    assert st.get(peer.peer_id).state == "trusted" and st.get(peer.peer_id).revision == 1
    assert st.get_grant(first.grant_id).state == st.get_grant(second.grant_id).state == "live"
    assert st.generation() == generation and len(st.list_remote(peer.peer_id)) == 1
    assert grants.authorize(st, first.grant_id, peer.peer_id, 1) is not None
    assert store.Store._end_grants is real

    assert st.revoke(peer.peer_id).state == "revoked"
    for made in (first, second):
        ended = st.get_grant(made.grant_id)
        assert ended.state == "revoked" and ended.end_reason == "peer_revoked" and ended.ended_at
        assert st.grant_events(made.grant_id)[-1]["event"] == "peer_revoked"
        assert st.grant_events(made.grant_id)[-1]["actor"] == "system"
        assert grants.authorize(st, made.grant_id, peer.peer_id, 1) is None
    assert st.generation() == generation + 1  # one change, one transaction
    assert st.list_remote(peer.peer_id) == []  # what it shared with us is gone from the mirror
    # the other peer is untouched
    assert st.get_grant(kept.grant_id).state == "live" and len(st.list_remote(other.peer_id)) == 1
    # pairing again does not bring a grant back
    st.trust(st.get(peer.peer_id).public_key, name="box", endpoints=[])
    assert grants.authorize(st, first.grant_id, peer.peer_id, 1) is None
    assert st.live_grants_of(peer.peer_id)[2] == []


def _remote_row() -> dict:
    return {
        "grant_id": store.new_grant_id(),
        "engine": "codex-api",
        "native_id": str(uuid.uuid4()),
        "title": "t",
        "level": "view",
        "expires_at": time.time() + 60,
        "revision": 1,
    }


def test_a_grant_created_while_its_peer_is_being_revoked_is_refused_or_ended(st, live):
    # both orders, decided
    peer = trusted(st)
    made = grant(st, peer)
    st.revoke(peer.peer_id)
    assert st.get_grant(made.grant_id).end_reason == "peer_revoked"  # created first: ended
    assert refusal(grant, st, peer).status == 409  # revoked first: refused

    # and raced: two threads, each over its own store object as two processes would be
    for _ in range(25):
        racer = trusted(st, "racer")
        ready = threading.Barrier(2)
        outcome: dict = {}

        def create(racer=racer, ready=ready, outcome=outcome) -> None:
            ready.wait(5)
            try:
                outcome["grant"] = grant(store.Store(st.db_path.parent), racer)
            except grants.GrantError as exc:
                outcome["refused"] = exc.status

        def revoke(racer=racer, ready=ready) -> None:
            ready.wait(5)
            store.Store(st.db_path.parent).revoke(racer.peer_id)

        threads = [threading.Thread(target=create), threading.Thread(target=revoke)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert st.live_grants_of(racer.peer_id)[2] == []  # whatever the order: nothing is live
        if "grant" in outcome:
            assert st.get_grant(outcome["grant"].grant_id).end_reason == "peer_revoked"
        else:
            assert outcome["refused"] == 409


# ── archive ───────────────────────────────────────────────────────────


def test_archive_ends_the_sessions_grants_before_the_move(live):
    st = store.Store()  # the process's own store: the one `shares.archiving` ends grants in
    peer, other = trusted(st), trusted(st, "other")
    key = api_key()
    first, second, elsewhere = grant(st, peer, key), grant(st, other, key), grant(st, peer)
    generation = st.generation()
    seen: dict = {}

    async def archive() -> None:
        async with shares.archiving(key):
            # The move happens here: by now nothing of this session is granted to anyone.
            seen["during"] = [st.get_grant(g.grant_id).state for g in (first, second)]
            seen["check"] = grants.authorize(st, first.grant_id, peer.peer_id, 1)

    asyncio.run(archive())
    assert seen == {"during": ["revoked", "revoked"], "check": None}
    for made in (first, second):
        ended = st.get_grant(made.grant_id)
        assert ended.end_reason == "archived"
        assert st.grant_events(made.grant_id)[-1]["event"] == "archived"
        assert st.grant_events(made.grant_id)[-1]["actor"] == "system"
    assert st.get_grant(elsewhere.grant_id).state == "live"  # another session's grant stays
    assert st.generation() == generation + 1


def test_an_archive_whose_grant_ending_fails_does_not_archive(live, monkeypatch):
    st = store.Store()
    peer = trusted(st)
    made = grant(st, peer)
    ran: list[str] = []

    def broken(self, session_key, **_kw):
        raise sqlite3.OperationalError("database is locked")

    async def archive() -> None:
        async with shares.archiving(made.session_key):
            ran.append("moved")

    with monkeypatch.context() as m:
        m.setattr(store.Store, "end_grants_for_session", broken)
        with pytest.raises(shares.SharesUnavailable, match="shares with peers"):
            asyncio.run(archive())
    assert ran == []  # the body (the move) never ran
    assert st.get_grant(made.grant_id).state == "live"
    # the session lock was released: the next archive goes through
    asyncio.run(archive())
    assert ran == ["moved"] and st.get_grant(made.grant_id).end_reason == "archived"

    # a store from a newer build cannot be written: the archive refuses, it does not skip
    con = sqlite3.connect(st.db_path)
    con.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    con.close()
    with pytest.raises(shares.SharesUnavailable):
        asyncio.run(archive())
    assert ran == ["moved"]


def test_an_archive_on_an_install_that_never_federated_creates_no_store(tmp_path):
    ran: list[str] = []

    async def archive() -> None:
        async with shares.archiving(api_key()):
            ran.append("moved")

    asyncio.run(archive())
    assert ran == ["moved"]
    assert not store.Store().db_path.exists() and not store.Store().db_path.parent.exists()


def test_every_archive_ends_federation_grants_inside_the_archiving_wrapper():
    """The federation analogue of `test_every_archive_path_ends_links_first`: that ratchet pins
    every archive path to `shares.archiving`; this pins the wrapper to ending the grants before
    it yields for the move, and to failing closed."""
    source = inspect.getsource(shares.archiving.__wrapped__)
    ends, yields = source.index("_end_federation_grants"), source.index("\n        yield\n")
    assert ends < yields and source.count("yield\n") == 1
    assert "raise SharesUnavailable" in source[ends:yields]
    assert "end_grants_for_session" in inspect.getsource(shares._end_federation_grants)


def test_a_grant_created_while_an_archive_is_in_flight_never_survives_the_move(live):
    """Both orders. The create takes the session lock `archiving` holds across the revoke and the
    move, and re-reads the session's state inside it."""
    st = store.Store()
    peer = trusted(st)

    # 1. The archive is first. A create that arrives during it waits, then is refused.
    key = api_key()
    outcome: dict = {}
    entered = threading.Event()

    def create() -> None:
        entered.set()
        try:
            outcome["result"] = grant(st, peer, key)
        except grants.GrantError as exc:
            outcome["result"] = exc.status

    async def archive_first() -> None:
        async with shares.archiving(key):
            t = threading.Thread(target=create)
            t.start()
            entered.wait(5)
            time.sleep(0.1)  # the create is now blocked on the session lock
            assert "result" not in outcome and st.list_grants(session_key=key) == []
            live[key] = "archived"  # the move, still under the lock
        await asyncio.to_thread(t.join, 5)

    asyncio.run(archive_first())
    assert outcome["result"] == 409
    assert st.list_grants(session_key=key) == []

    # 2. The create is first. The archive waits for it, then ends what it made.
    key2 = api_key()
    inside = threading.Event()
    release = threading.Event()
    made: dict = {}
    real = shares.session_state

    def slow_state(prov, native):
        inside.set()  # the create holds the session lock and is about to insert
        release.wait(5)
        return real(prov, native)

    def create_first() -> None:
        shares.session_state = slow_state
        try:
            made["grant"] = grant(st, peer, key2)
        finally:
            shares.session_state = real

    async def archive_second() -> None:
        t = threading.Thread(target=create_first)
        t.start()
        await asyncio.to_thread(inside.wait, 5)
        archive = asyncio.ensure_future(_archive(key2, live))
        await asyncio.sleep(0.1)
        assert not archive.done()  # blocked on the lock the create holds
        release.set()
        await archive
        await asyncio.to_thread(t.join, 5)

    asyncio.run(archive_second())
    ended = st.get_grant(made["grant"].grant_id)
    assert ended.state == "revoked" and ended.end_reason == "archived"
    assert st.list_grants(session_key=key2) == []


async def _archive(key: str, live: dict) -> None:
    async with shares.archiving(key):
        live[key] = "archived"
