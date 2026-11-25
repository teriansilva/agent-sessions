"""Working together on a shared session (#2059): schema 5, presence, access requests, a peer
answering a pending decision, the bell on both sides, and the routes.

`owner` is one real `service.Federation` over the test's own store (`test_federation_inputs`).
A verb is called the way the channel calls it: the table's validator, then its handler, with the
authenticated peer id as the context. `decisions` stands in for `structured_runtime.snapshot`,
`decide` and `decision_record` ONLY: it holds pending requests, settles each once, answers a
repeat of a decision id from its own journal and refuses a settled request, as the runtime does.
The tests near the end use the real native runtime and two real app instances.

Every race is forced by events (`decisions.gate`, `until`), never by sleeping.
Notes are in `test_federation_notes.py`; Continue here is in `test_federation_continue.py`.
"""

# ruff: noqa: F811 — fixtures imported from the other federation test modules
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sqlite3
import stat
import textwrap
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

import agent_sessions
from agent_sessions import native_runtime, notifications, orchestrator_ledger, presence, shares
from agent_sessions import structured_runtime as runtime
from agent_sessions.federation import channel as ch
from agent_sessions.federation import (
    continue_here,
    fence,
    grants,
    inbox,
    inputs,
    notes,
    notices,
    offers,
    remote,
    service,
    sharing,
    store,
    together,
    verbs,
)
from agent_sessions.federation import presence as fed_presence
from federation_helpers import until
from test_federation_fence import waiting  # noqa: F401 — a fixture
from test_federation_grants import api_key, live, trusted  # noqa: F401 — `live` is a fixture
from test_federation_inputs import (  # noqa: F401 — fixtures
    ENDINGS,
    TITLE,
    Owner,
    _mirrored,
    _names,
    _rel,
    _sources,
    bell,
    end,
    expire,
    held,
    ledger_rows,
    owner,
    ref,
    submits,
)
from test_federation_routes import Instance, _client, _login, _wait, two_instances  # noqa: F401
from test_federation_sharing import _pair_instances
from test_native_runtime import ENGINES, host, ident, pending, project, settle  # noqa: F401

SRC = Path(agent_sessions.__file__).parent
QUESTION = "PLANTED-QUESTION which database should the migration use?"
OPTION = "PLANTED-OPTION postgres"
PAYLOAD = "PLANTED-PAYLOAD rm -rf /srv/data"
NOTE = "PLANTED-ACCESS-NOTE please let me send"
GRANT_ID = "s" * 22


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Decisions:
    """`structured_runtime.snapshot` / `decide` / `decision_record`, scripted.

    `effects` is what an agent would have received: one entry per SETTLED request. `calls` is
    every `decide` call. `gate` holds a call inside the runtime; `crash_before` and
    `crash_after` raise once, before the runtime took the decision and after it accepted it."""

    def __init__(self) -> None:
        self.turn = str(uuid.uuid4())
        self.requests: dict[str, list[dict]] = {}
        self.journal: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.expected: list[str | None] = []  # the `expected_digest` of every `decide` call
        self.effects: list[dict] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.crash_before = self.crash_after = False
        self.forgets = False  # the journal no longer holds the record (a rollover)
        self.before_decide = None

    def install(self, monkeypatch) -> Decisions:
        monkeypatch.setattr(Decisions, "current", self, raising=False)
        for name in ("snapshot", "decide", "decision_record", "events"):
            monkeypatch.setattr(runtime, name, getattr(self, name))
        return self

    async def events(self, key: str, *, after: int, limit: int = 100) -> dict:
        return {"session_key": key, "revision": 1, "events": [], "next_cursor": after}

    def ask(self, key: str, request_id: str = "req-1", **extra) -> dict:
        """A pending tool permission on ``key``: allow once, reject, stop, and a standing grant."""
        request = {
            "request_id": request_id,
            "turn_id": extra.pop("turn_id", self.turn),
            "kind": extra.pop("kind", "Bash"),
            "item_id": "item-1",
            "summary": extra.pop("summary", f"run a command\n{PAYLOAD}"),
            "payload": extra.pop("payload", {"command": PAYLOAD}),
            "payload_digest": extra.pop("digest", "d" * 64),
            "choices": list(extra.pop("choices", ("approve", "reject", "cancel"))),
            "complete": True,
            "always": extra.pop("always", [{"id": "grant-1", "label": "every Bash command"}]),
            "risk": None,
            "operator_only": True,
            "context": {},
            **extra,
        }
        self.requests.setdefault(key, []).append(request)
        return request

    def question(self, key: str, request_id: str = "q-1", *, multi: bool = False) -> dict:
        asked = {
            "questions": [
                {
                    "question": QUESTION,
                    "header": "Database",
                    "multiSelect": multi,
                    "options": [{"label": "sqlite"}, {"label": OPTION}, {"label": "mysql"}],
                }
            ]
        }
        return self.ask(
            key,
            request_id,
            kind="AskUserQuestion",
            summary=json.dumps(asked),
            payload={"input": asked},
            choices=("answer", "reject", "cancel"),
            always=[],
        )

    async def snapshot(self, key: str, **_kw) -> dict:
        return {
            "session_key": key,
            "revision": len(self.journal),
            "event_cursor": len(self.journal),
            "cwd": "/home/op/secret-project",
            "state": "awaiting_approval" if self.requests.get(key) else "idle",
            "active_turn": self.turn,
            "model_effective": "model-x",
            "turns": [{"kind": "operator", "turn_id": self.turn, "operation_id": self.turn}],
            "omitted_turns": 0,
            "pending_requests": [dict(q) for q in self.requests.get(key, [])],
        }

    async def decide(
        self,
        key,
        *,
        turn_id,
        request_id,
        decision_id,
        decision,
        user,
        answers=None,
        expected_digest=None,
        **_kw,
    ) -> dict:
        call = {
            "key": key,
            "turn_id": turn_id,
            "request_id": request_id,
            "decision_id": decision_id,
            "decision": decision,
            "actor": user,
            "answers": answers,
        }
        self.calls.append(call)
        self.expected.append(expected_digest)
        self.started.set()
        if self.before_decide is not None:
            self.before_decide(call)
        if self.gate is not None:
            await self.gate.wait()
        if self.crash_before:
            self.crash_before = False
            raise RuntimeError("the process died before the runtime took the decision")
        prior = self.journal.get(decision_id)
        if prior is not None:
            if {k: prior[k] for k in call} != call:
                raise runtime.StructuredError(409, "decision id already used")
            return {**call, "session_key": key, "handoff": prior["handoff"]}
        request = next(
            (
                q
                for q in self.requests.get(key, [])
                if (q["request_id"], q["turn_id"]) == (request_id, turn_id)
            ),
            None,
        )
        if request is None or expected_digest not in (None, request["payload_digest"]):
            raise runtime.StructuredError(409, "that approval is no longer pending")
        self.requests[key].remove(request)  # settled ONCE
        record = {**call, "digest": request["payload_digest"], "handoff": "sent"}
        self.journal[decision_id] = record
        self.effects.append(record)
        if self.crash_after:
            self.crash_after = False
            raise RuntimeError("the process died after the runtime accepted the decision")
        return {**call, "session_key": key, "handoff": "sent"}

    async def decision_record(self, key, *, turn_id, request_id) -> list[dict]:
        if self.forgets:
            return []
        return [
            {
                "decision_id": r["decision_id"],
                "decision": r["decision"],
                "actor": r["actor"],
                "digest": r["digest"],
                "handoff": r["handoff"],
            }
            for r in self.journal.values()
            if (r["key"], r["turn_id"], r["request_id"]) == (key, turn_id, request_id)
        ]

    async def owner_decides(self, fed, key: str, request: dict, decision: str = "approve") -> dict:
        """As the operator's decide route does: the runtime, then the attribution hook."""
        out = await self.decide(
            key,
            turn_id=request["turn_id"],
            request_id=request["request_id"],
            decision_id=str(uuid.uuid4()),
            decision=decision,
            user="marcus",
        )
        await fed.owner_decided(key, request["turn_id"], request["request_id"])
        return out


@pytest.fixture
def decisions(monkeypatch):
    return Decisions().install(monkeypatch)


def shown_offer(grant: store.Grant, request_id: str) -> str:
    """The offer a peer was shown for ``request_id`` on the scripted runtime: the pending
    request's, else the one a request of the current turn with the default payload had. (On
    the real runtime a test passes the snapshot's own ``offer``.)"""
    script = getattr(Decisions, "current", None)
    key = getattr(grant, "session_key", "unknown:session")  # a made-up grant has none
    pending_now = script.requests.get(key, []) if script else []
    request = next(
        (q for q in pending_now if q["request_id"] == request_id),
        {
            "turn_id": script.turn if script else "no-turn",
            "request_id": request_id,
            "payload_digest": "d" * 64,
        },
    )
    return offers.token(grant.grant_id, key, request)


def answer(grant: store.Grant, request_id: str = "req-1", choice: str = "approve", **extra) -> dict:
    return {
        "grant_id": grant.grant_id,
        "revision": grant.revision,
        "request_id": request_id,
        "offer": extra.pop("offer", None) or shown_offer(grant, request_id),
        "choice": choice,
        "client_key": extra.pop("client_key", None) or str(uuid.uuid4()),
        **extra,
    }


def authors(st: store.Store, key: str) -> list[dict]:
    return st.decision_authors(key)


def peer_ledger() -> list[dict]:
    return [r for r in ledger_rows() if r.get("verb") == "peer_decision"]


# ── the store: schema 5 ───────────────────────────────────────────────


@pytest.mark.parametrize("version", [2, 3, 4])
def test_an_older_store_migrates_in_place_to_5_and_a_backup_is_taken_first(tmp_path, version):
    """Schema 2 is #2054's (grants), 3 #2056's (the grant kind), 4 #2057's (input); working
    together is schema 5. A store at any of them takes exactly the steps it lacks, in place,
    after a complete copy, and what it held is unchanged."""
    directory = tmp_path / "fed"
    directory.mkdir()
    path = directory / store.DB_FILE
    con = sqlite3.connect(path)
    steps = (store._SCHEMA_V1, store._SCHEMA_V2, store._SCHEMA_V3, store._SCHEMA_V4)[:version]
    for stmt in (s for step in steps for s in step):
        con.execute(stmt)
    con.execute(
        "INSERT INTO peers (peer_id, public_key, name, state, endpoints, revision, created_at,"
        " updated_at) VALUES (?, ?, 'old box', 'trusted', '[]', 1, 1.0, 1.0)",
        ("a" * 32, b"k" * 32),
    )
    con.execute(
        "INSERT INTO grants (grant_id, session_key, principal, level, expires_at, revision, state,"
        " created_at) VALUES (?, 'codex-api:x', ?, ?, ?, 4, 'live', 1.0)",
        ("g" * 22, "peer:" + "a" * 32, "view" if version < 4 else "suggest", time.time() + 3600),
    )
    if version >= 3:  # a terminal grant, as #2056 stores it
        con.execute(
            "INSERT INTO grants (grant_id, session_key, principal, level, expires_at, revision,"
            " state, created_at, kind) VALUES (?, 'claude:y', ?, 'view', ?, 2, 'live', 1.0,"
            " 'terminal')",
            ("t" * 22, "peer:" + "a" * 32, time.time() + 3600),
        )
    if version >= 4:  # a pending suggestion, as #2057 stores it
        con.execute(
            "INSERT INTO suggestions (suggestion_id, grant_id, session_key, principal, client_key,"
            " actor, text, operation_id, state, created_at, updated_at, expires_at)"
            " VALUES ('s1', ?, 'codex-api:x', ?, 'k', '', 'kept text', 'op', 'pending', 1, 1, ?)",
            ("g" * 22, "peer:" + "a" * 32, time.time() + 3600),
        )
    con.execute(f"PRAGMA user_version={version}")
    con.commit()
    con.close()

    st = store.Store(directory)
    kept = {g.grant_id[0]: g for g in st.list_grants()}  # the first use migrates, in place
    assert kept["g"].revision == 4 and kept["g"].kind == "api"
    assert kept["g"].level == ("view" if version < 4 else "suggest")
    if version >= 3:
        assert kept["t"].kind == "terminal" and kept["t"].level == "view"
    else:
        assert set(kept) == {"g"}
    if version >= 4:
        assert st.get_suggestion("s1").text == "kept text"
    assert st.by_key(b"k" * 32).name == "old box"
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 5
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    new = {"access_requests", "peer_decisions", "decision_authors", "notes", "note_changes"}
    assert new | {"suggestions", "peer_turns", "grants", "remote_shares"} <= tables
    assert "kind" in {r[1] for r in con.execute("PRAGMA table_info(grants)")}
    assert "kind" in {r[1] for r in con.execute("PRAGMA table_info(remote_shares)")}
    con.close()

    (backup,) = directory.glob(f"{store.DB_FILE}.bak-v{version}-*")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    old = sqlite3.connect(backup)
    # Taken BEFORE the migration: still the old schema, with its rows and without the new tables.
    assert old.execute("PRAGMA user_version").fetchone()[0] == version
    assert old.execute("SELECT revision FROM grants WHERE grant_id LIKE 'g%'").fetchone()[0] == 4
    if version >= 4:
        assert old.execute("SELECT text FROM suggestions").fetchone()[0] == "kept text"
    assert not old.execute("SELECT 1 FROM sqlite_master WHERE name='notes'").fetchone()
    assert not old.execute("SELECT 1 FROM sqlite_master WHERE name='access_requests'").fetchone()
    old.close()
    store.Store(directory).list_peers()  # a second open migrates and copies nothing
    assert len(list(directory.glob("*.bak-*"))) == 1


def test_a_store_from_a_newer_build_is_refused_untouched_by_the_new_reads(tmp_path):
    st = store.Store(tmp_path / "fed")
    trusted(st)
    con = sqlite3.connect(st.db_path)
    con.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    con.close()
    before = st.db_path.read_bytes()
    again = store.Store(tmp_path / "fed")
    for read in (
        lambda: again.list_access_requests(),
        lambda: again.decision_authors("codex-api:x"),
        lambda: again.note_grants_unread(),
        lambda: again.open_peer_decisions(),
    ):
        with pytest.raises(store.StoreUnsupported):
            read()
    assert st.db_path.read_bytes() == before and not list((tmp_path / "fed").glob("*.bak-*"))


def test_the_new_reads_never_create_the_store(tmp_path):
    st = store.Store(tmp_path / "never")
    assert st.list_access_requests() == [] and st.access_status(GRANT_ID) is None
    assert st.get_peer_decision(GRANT_ID, "k") is None and st.open_peer_decisions() == []
    assert st.decision_authors("codex-api:x") == [] and st.note_grants_unread() == {}
    assert st.unread_notes(GRANT_ID, "owner") == 0 and st.purge_notes() == 0
    assert st.session_shared_ever("codex-api:x") is False
    assert not (tmp_path / "never").exists()


# ── presence ──────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_watching_is_a_timestamp_per_grant_in_memory_and_gone_after_30_seconds():
    clock = Clock()
    seen = fed_presence.Watching(clock=clock)
    assert seen.watching(GRANT_ID) is False
    seen.touch(GRANT_ID)
    clock.now += 30.0
    assert seen.watching(GRANT_ID) is True  # 30 s after the last read: still watching
    clock.now += 0.01
    assert seen.watching(GRANT_ID) is False and len(seen) == 0  # and then gone, nothing kept
    seen.touch(GRANT_ID)
    seen.forget(GRANT_ID)  # the grant ended
    assert seen.watching(GRANT_ID) is False
    # What is held is a float under a grant id: no name, no count, no session, no device.
    seen.touch(GRANT_ID)
    assert seen._seen == {GRANT_ID: clock.now}
    # Bounded, whatever a peer does.
    for i in range(fed_presence.MAX_GRANTS + 50):
        seen.touch(f"g{i:021d}")
    assert len(seen) == fed_presence.MAX_GRANTS


@pytest.mark.anyio
async def test_a_read_marks_its_own_live_grant_watched_and_nothing_is_stored(
    owner, decisions, live, monkeypatch
):
    clock = Clock()
    owner.fed.watching = fed_presence.Watching(clock=clock)
    monkeypatch.setattr(presence, "_DEFAULT", presence.Presence(clock=clock))
    made, other = owner.grant("view"), owner.grant("view")
    before = owner.st.db_path.read_bytes()

    snap = await owner.call("session.snapshot", ref(made))
    assert owner.fed.watching.watching(made.grant_id) is True
    assert owner.fed.watching.watching(other.grant_id) is False  # nothing about another session
    assert snap["owner_present"] is False
    # In memory only: the store's bytes did not move, and no table could hold it.
    assert owner.st.db_path.read_bytes() == before
    con = sqlite3.connect(owner.st.db_path)
    names = " ".join(r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE sql NOT NULL"))
    con.close()
    assert "watch" not in names and "presence" not in names and "present" not in names

    # `owner_present`: the operator's pane read THAT session within 30 s. One bit.
    presence.default().touch(made.session_key, presence.OPERATOR)
    assert (await owner.call("session.snapshot", ref(made)))["owner_present"] is True
    assert (await owner.call("session.snapshot", ref(other)))["owner_present"] is False
    assert fed_presence.owner_present(other.session_key) is False
    # a visitor of a public link is not the operator
    presence.default().touch(other.session_key, "v:abc", name="Alex")
    assert (await owner.call("session.snapshot", ref(other)))["owner_present"] is False
    clock.now += 30.01
    again = await owner.call("session.snapshot", ref(made))
    assert again["owner_present"] is False and type(again["owner_present"]) is bool
    assert set(again) >= {"owner_present"} and "present" not in again  # no count is exposed

    # the events read marks it too; gone 30 s after the LAST read
    clock.now += 100
    assert owner.fed.watching.watching(made.grant_id) is False
    await owner.call("session.events", ref(made, after=0))
    assert owner.fed.watching.watching(made.grant_id) is True
    clock.now += 30.01
    assert owner.fed.watching.watching(made.grant_id) is False

    # only under a live grant: a refused read touches nothing
    grants.revoke(owner.st, other.grant_id)
    assert await owner.refused("session.snapshot", ref(other)) == "no_grant"
    assert await owner.refused("session.events", ref(other, after=0)) == "no_grant"
    assert owner.fed.watching.watching(other.grant_id) is False
    stale = ref(made, revision=made.revision + 1)
    assert await owner.refused("session.snapshot", stale) == "no_grant"
    assert owner.fed.watching.watching(made.grant_id) is False


def test_the_grants_listing_says_who_is_watching_and_forgets_an_ended_grant(auth_cfg, monkeypatch):
    monkeypatch.setattr(shares, "session_state", lambda prov, native: "live")
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    fed = c.app.state.federation
    clock = Clock()
    fed.watching = fed_presence.Watching(clock=clock)
    peer = trusted(fed.store, "GPU box")
    made = c.post(
        "/api/federation/grants", json={"session_key": api_key(), "peer_id": peer.peer_id},
        headers=hdr,
    ).json()["grant"]  # fmt: skip
    assert made["watching"] is False

    def listed() -> dict:
        (row,) = c.get("/api/federation/grants").json()["grants"]
        return row

    fed.watching.touch(made["grant_id"])
    row = listed()
    assert row["watching"] is True and row["peer_name"] == "GPU box"
    assert not {"watchers", "viewers", "present", "count"} & set(row)  # a name and a bit
    clock.now += 30.01
    assert listed()["watching"] is False
    fed.watching.touch(made["grant_id"])
    ended = c.post(f"/api/federation/grants/{made['grant_id']}/revoke", headers=hdr)
    assert ended.status_code == 200 and ended.json()["grant"]["watching"] is False
    assert fed.watching.watching(made["grant_id"]) is False  # forgotten with the grant


# ── access requests ───────────────────────────────────────────────────


def ask(grant: store.Grant, level: str = "send", **extra) -> dict:
    return {"grant_id": grant.grant_id, "revision": grant.revision, "level": level, **extra}


@pytest.mark.anyio
async def test_an_access_request_is_a_record_and_never_changes_a_grant(owner):
    made = owner.grant("view")
    before = (owner.st.get_grant(made.grant_id), owner.st.generation())
    asked = await owner.call("access.request", ask(made, "send", note=NOTE))
    await owner.quiet()
    assert asked["state"] == "pending" and asked["level"] == "send"
    assert remote.check_access(asked) is asked
    # The grant is exactly what it was: level, expiry, revision, generation, events.
    assert (owner.st.get_grant(made.grant_id), owner.st.generation()) == before
    assert [e["event"] for e in owner.st.grant_events(made.grant_id)] == ["granted"]
    row = owner.st.get_access_request(asked["request_id"])
    assert (row.level, row.note, row.state, row.peer_id) == (
        "send",
        NOTE,
        "pending",
        owner.peer.peer_id,
    )
    # the peer still cannot do what it asked for
    assert await owner.refused("decision.answer", answer(made)) == "no_grant"
    status = await owner.call("access.status", ref(made))
    assert remote.check_access_status(status) is status
    assert status["level"] == "view" and status["request"]["state"] == "pending"
    assert NOTE not in json.dumps(status)  # its own note is not read back to it
    # In the source: no module a verb runs in can name the store's grant writers.
    for module in ("together", "notes", "offers", "inbox", "presence", "continue_here"):
        names = _names(SRC / "federation" / f"{module}.py")
        assert not names & {"create_grant", "change_grant", "revoke_grant"}, module
    # …and the store's access methods write no grant row
    for method in (
        store.Store.add_access_request,
        store.Store.decline_access_request,
        store.Store.withdraw_access_request,
    ):
        text = textwrap.dedent(inspect.getsource(method))
        assert "UPDATE grants" not in text and "INSERT INTO grants" not in text


@pytest.mark.anyio
async def test_one_request_is_pending_per_grant_and_a_second_replaces_nothing(owner):
    made = owner.grant("view")
    first = await owner.call("access.request", ask(made, "suggest", note="first"))
    second = await owner.call("access.request", ask(made, "send", note="second"))
    assert second == first  # the pending one, as it is: its level and its id
    (row,) = owner.st.list_access_requests()
    assert (row.level, row.note) == ("suggest", "first")
    # twenty at once still leave one
    again = await asyncio.gather(
        *(owner.call("access.request", ask(made, "send")) for _ in range(20))
    )
    assert {a["request_id"] for a in again} == {first["request_id"]}
    assert len(owner.st.list_access_requests()) == 1
    await owner.quiet()
    assert [r["action_id"] for r in bell()] == [notices.access_key(first["request_id"])]


@pytest.mark.anyio
async def test_a_peer_withdraws_its_own_pending_request_and_nothing_else_changes(owner):
    """#2062: the waiting state has Withdraw. It narrows: the request's row changes state, no
    grant column does, the bell row goes, and asking again is a new request."""
    made = owner.grant("view")
    before = (owner.st.get_grant(made.grant_id), owner.st.generation())
    # nothing pending: not an error, and nothing is created
    none = await owner.call("access.withdraw", ref(made))
    assert none == {"level": "view", "request": None}
    assert owner.st.list_access_requests() == []
    asked = await owner.call("access.request", ask(made, "send", note=NOTE))
    await owner.quiet()
    assert [r["action_id"] for r in bell()] == [notices.access_key(asked["request_id"])]
    gone = await owner.call("access.withdraw", ref(made))
    assert remote.check_access_status(gone) is gone
    assert gone["level"] == "view" and gone["request"]["state"] == "withdrawn"
    assert gone["request"]["request_id"] == asked["request_id"]
    assert NOTE not in json.dumps(gone)
    row = owner.st.get_access_request(asked["request_id"])
    assert (row.state, row.end_reason) == ("withdrawn", "peer")
    assert (owner.st.get_grant(made.grant_id), owner.st.generation()) == before
    await owner.quiet()
    assert bell() == []
    # again: the same answer, still one row
    assert (await owner.call("access.withdraw", ref(made)))["request"]["state"] == "withdrawn"
    assert len(owner.st.list_access_requests()) == 1
    # the operator cannot decline what was taken back, and a new request is a new record
    with pytest.raises(store.StateConflict):
        owner.st.decline_access_request(asked["request_id"])
    fresh = await owner.call("access.request", ask(made, "suggest"))
    assert fresh["request_id"] != asked["request_id"] and fresh["state"] == "pending"
    # the verb takes a grant reference and nothing else
    spec = verbs.PEER["access.withdraw"]
    with pytest.raises(ch.BadArgs):
        spec.validate({**ref(made), "request_id": asked["request_id"]})


def test_the_grants_listing_counts_a_threads_notes_and_shows_none_of_them(auth_cfg, monkeypatch):
    """#2062: the owner's strip says how many notes each grant's thread has. A number."""
    monkeypatch.setattr(shares, "session_state", lambda prov, native: "live")
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    fed = c.app.state.federation
    peer = trusted(fed.store, "GPU box")
    made = c.post(
        "/api/federation/grants", json={"session_key": api_key(), "peer_id": peer.peer_id},
        headers=hdr,
    ).json()["grant"]  # fmt: skip
    gid = made["grant_id"]

    def listed() -> dict:
        (row,) = c.get("/api/federation/grants").json()["grants"]
        return row

    assert listed()["notes"] == 0
    kept = c.post(
        f"/api/federation/grants/{gid}/notes",
        json={"client_key": str(uuid.uuid4()), "text": "PLANTED one"},
        headers=hdr,
    ).json()["note"]
    dropped = c.post(
        f"/api/federation/grants/{gid}/notes",
        json={"client_key": str(uuid.uuid4()), "text": "PLANTED two"},
        headers=hdr,
    ).json()["note"]
    assert listed()["notes"] == 2
    assert (
        c.delete(
            f"/api/federation/grants/{gid}/notes/{dropped['note_id']}", headers=hdr
        ).status_code
        == 200
    )
    row = listed()
    assert row["notes"] == 1 and kept["note_id"] not in json.dumps(row)
    assert "PLANTED" not in c.get("/api/federation/grants").text
    assert fed.store.note_counts([]) == {} and fed.store.note_counts(["x" * 22]) == {}


@pytest.mark.anyio
async def test_only_a_level_above_the_grants_can_be_asked_for(owner):
    view, suggest, send = owner.grant("view"), owner.grant("suggest"), owner.grant("send")
    assert await owner.refused("access.request", ask(suggest, "suggest")) == "invalid"
    assert await owner.refused("access.request", ask(send, "send")) == "invalid"
    assert await owner.refused("access.request", ask(send, "suggest")) == "invalid"
    assert (await owner.call("access.request", ask(view, "suggest")))["state"] == "pending"
    assert (await owner.call("access.request", ask(suggest, "send")))["state"] == "pending"
    spec = verbs.PEER["access.request"]
    for bad in (
        ask(view, "view"),  # asking for nothing
        ask(view, "admin"),
        ask(view, None),
        ask(view, "send", note="x" * (store.ACCESS_NOTE_MAX + 1)),
        ask(view, "send", note="line\x1b[2Jone"),
        ask(view, "send", note=7),
        ask(view, "send", expires_in=86400),
        ask(view, "send", accept_risk=True),  # the operator's word is never a peer's argument
        {"level": "send"},
    ):
        with pytest.raises(ch.BadArgs):
            spec.validate(dict(bad))
    assert spec.validate(ask(view, "send"))["note"] == ""


@pytest.mark.anyio
async def test_approving_is_the_operators_own_grant_change(owner):
    made = owner.grant("view")
    asked = await owner.call("access.request", ask(made, "send", note=NOTE))
    await owner.quiet()
    assert [r["action_id"] for r in bell()] == [notices.access_key(asked["request_id"])]

    # `send` still needs the operator's accept_risk, in the operator's own request
    with pytest.raises(grants.GrantError) as risky:
        grants.change(owner.st, made.grant_id, expires_in=None, revision=1, level="send")
    assert risky.value.code == "risk"
    assert owner.st.get_access_request(asked["request_id"]).state == "pending"
    assert owner.st.get_grant(made.grant_id).level == "view"

    # a raise that does not reach what was asked for leaves the request pending
    raised = grants.change(owner.st, made.grant_id, expires_in=None, revision=1, level="suggest")
    assert owner.st.get_access_request(asked["request_id"]).state == "pending"

    granted = grants.change(
        owner.st, made.grant_id, expires_in=None, revision=raised.revision, level="send",
        accept_risk=True,
    )  # fmt: skip
    assert granted.level == "send"
    assert owner.st.get_access_request(asked["request_id"]).state == "granted"
    notices.retract(owner.st)
    assert bell() == []  # retracted: granted
    status = await owner.call("access.status", ref(granted))
    assert status["level"] == "send" and status["request"]["state"] == "granted"
    # there is no approve function anywhere (and no route: the decline test asks for one)
    assert not [n for n in dir(together) if "approve" in n or "grant_access" in n]
    assert not [n for n in dir(store.Store) if "approve_access" in n or "grant_access" in n]


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["revoke", "expiry", "peer_revoke", "archive"])
async def test_a_pending_request_ends_with_its_grant(owner, live, action):
    made = owner.grant("view")
    asked = await owner.call("access.request", ask(made, "send"))
    await owner.quiet()
    assert len(bell()) == 1
    end(action, owner, made, live)
    if action == "expiry":
        assert (await owner.refused("access.status", ref(made))) == "no_grant"
        grants.sweep(owner.st)
    row = owner.st.get_access_request(asked["request_id"])
    assert row.state == "expired" and row.end_reason.startswith("grant_")
    notices.retract(owner.st)
    assert bell() == []  # retracted: the grant ended
    assert await owner.refused("access.request", ask(made, "send")) == "no_grant"
    assert await owner.refused("access.status", ref(made)) == "no_grant"
    with pytest.raises(together.inputs.InputError) as late:
        together.decline_access(owner.st, asked["request_id"])
    assert (late.value.status, late.value.state) == (409, "expired")


@pytest.mark.anyio
async def test_a_request_expires_after_a_day_and_requests_are_bounded(owner, monkeypatch):
    made = owner.grant("view", expires_in=3 * 86400)
    asked = await owner.call("access.request", ask(made, "send"))
    await owner.quiet()
    row = owner.st.get_access_request(asked["request_id"])
    assert 86400 - 5 < row.expires_at - time.time() <= 86400
    con = sqlite3.connect(owner.st.db_path)
    con.execute("UPDATE access_requests SET expires_at = ?", (time.time() - 1,))
    con.commit()
    con.close()
    assert (await owner.call("access.status", ref(made)))["request"]["state"] == "expired"
    with pytest.raises(together.inputs.InputError) as late:
        together.decline_access(owner.st, asked["request_id"])
    assert late.value.state == "expired"
    grants.sweep(owner.st)
    assert owner.st.get_access_request(asked["request_id"]).state == "expired"
    notices.retract(owner.st)
    assert bell() == []  # retracted: expired
    # declined requests do not invite a flood: five in a day, then `busy`
    for _ in range(store.ACCESS_PER_DAY_MAX - 1):
        again = await owner.call("access.request", ask(made, "send"))
        together.decline_access(owner.st, again["request_id"])
    assert await owner.refused("access.request", ask(made, "send")) == "busy"
    assert len(owner.st.list_access_requests()) == store.ACCESS_PER_DAY_MAX
    await owner.quiet()
    assert bell() == []  # retracted: declined


def test_decline_is_compare_and_set_and_the_operator_reads_the_note(auth_cfg, monkeypatch):
    monkeypatch.setattr(shares, "session_state", lambda prov, native: "live")
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    fed = c.app.state.federation
    peer = trusted(fed.store, "GPU box")
    key = api_key()
    made = grants.create(
        fed.store, session_key=key, peer_id=peer.peer_id, level="view", expires_in=None
    )
    request, fresh = fed.store.add_access_request(
        grant_id=made.grant_id, peer_id=peer.peer_id, revision=1, level="send", note=NOTE
    )
    assert fresh
    notices.announce_access(fed.store, request)
    listed = c.get(f"/api/federation/access-requests?session={key}")
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    (row,) = listed.json()["access_requests"]
    assert (row["note"], row["level"], row["state"], row["peer_name"]) == (
        NOTE,
        "send",
        "pending",
        "GPU box",
    )
    assert c.get(f"/api/federation/access-requests?session={api_key()}").json() == {
        "access_requests": []
    }
    # approving through the dialog's route: the grant change, with the risk accepted
    url = f"/api/federation/grants/{made.grant_id}"
    risky = c.patch(url, json={"level": "send", "revision": 1}, headers=hdr)
    assert risky.status_code == 422 and risky.json()["code"] == "risk"
    assert fed.store.get_access_request(request.request_id).state == "pending"
    for path in ("approve", "grant", "accept"):
        r = c.post(f"/api/federation/access-requests/{request.request_id}/{path}", headers=hdr)
        assert r.status_code in (404, 405), path  # no such route
    declined = c.post(f"/api/federation/access-requests/{request.request_id}/decline", headers=hdr)
    assert declined.status_code == 200
    assert declined.json()["access_request"]["state"] == "declined"
    again = c.post(f"/api/federation/access-requests/{request.request_id}/decline", headers=hdr)
    assert again.status_code == 409 and again.json()["state"] == "declined"
    missing = c.post(f"/api/federation/access-requests/{'z' * 22}/decline", headers=hdr)
    assert missing.status_code == 404
    assert bell() == []  # retracted: declined
    assert fed.store.get_grant(made.grant_id) == made  # declining changed no grant either


# ── a terminal grant (#2056) ──────────────────────────────────────────


def _term(owner) -> store.Grant:
    """A REAL terminal grant: a pty agent's session, granted through the operator rules."""
    term = owner.grant("view", key=f"claude:{uuid.uuid4()}")
    assert (term.kind, term.level) == ("terminal", "view")
    return term


@pytest.mark.anyio
async def test_a_terminal_grant_is_refused_by_every_working_together_verb_and_nothing_is_stored(
    owner, decisions
):
    """Every verb of #2059 is for API grants. A live terminal grant is `no_grant` to each one,
    except `access.request`, which answers `not_possible` (the contract: a terminal grant is
    view-only for good). Nothing is stored for it, from either side, and it is never watched."""
    term = _term(owner)
    decisions.ask(term.session_key)
    # It IS a live grant the per-call check passes for its own kind, so every refusal below is
    # the verb's kind, not a dead grant.
    assert grants.authorize(
        owner.st, term.grant_id, owner.peer.peer_id, 1, kind=grants.KIND_TERMINAL
    ) == owner.st.get_grant(term.grant_id)
    assert set(_calls(term, "n" * 22)) == set(OWNER_VERBS)
    for name, args in _calls(term, "n" * 22).items():
        want = "not_possible" if name == "access.request" else "no_grant"
        assert await owner.refused(name, args) == want, name
    # the API reads refuse it too, so presence never marks it
    for name in ("session.snapshot", "session.events"):
        assert await owner.refused(name, ref(term)) == "no_grant", name
    assert owner.fed.watching.watching(term.grant_id) is False

    # A terminal row made to say `send` behind this build's back is still nothing: the level is
    # one this build does not serve for the kind, so even `not_possible` is now `no_grant`.
    con = sqlite3.connect(owner.st.db_path)
    con.execute("UPDATE grants SET level = 'send' WHERE grant_id = ?", (term.grant_id,))
    con.commit()
    con.close()
    for name, args in _calls(term, "n" * 22).items():
        assert await owner.refused(name, args) == "no_grant", name

    # The store refuses the same inside the transaction, for a caller that skipped the check.
    peer = owner.peer.peer_id
    with pytest.raises(store.NotGranted):
        owner.st.add_access_request(
            grant_id=term.grant_id, peer_id=peer, revision=1, level="send", note=""
        )
    with pytest.raises(store.NotGranted):
        owner.st.add_note(
            grant_id=term.grant_id,
            author=store.principal_of(peer),
            actor="",
            client_key=str(uuid.uuid4()),
            text="a note",
            peer=(peer, 1),
        )
    with pytest.raises(store.NotGranted):
        owner.st.record_peer_decision(
            grant_id=term.grant_id,
            session_key=term.session_key,
            peer_id=peer,
            client_key=str(uuid.uuid4()),
            actor="",
            turn_id=decisions.turn,
            request_id="req-1",
            digest="d" * 64,
            choice='{"choice":"approve"}',
            kind="once",
        )
    # …and the owner's own note routes have no thread under it either
    with pytest.raises(store.Invalid):
        owner.st.add_note(
            grant_id=term.grant_id,
            author=store.AUTHOR_OWNER,
            actor="",
            client_key=str(uuid.uuid4()),
            text="a note",
        )
    with pytest.raises(inputs.InputError):
        notes.owner_add(owner.st, term.grant_id, client_key=str(uuid.uuid4()), text="x", actor="")

    await owner.quiet()
    assert decisions.calls == [] and owner.st.open_peer_decisions() == []
    assert owner.st.list_access_requests() == [] and authors(owner.st, term.session_key) == []
    assert owner.st.owner_notes(term.grant_id, now=time.time())[1:] == ([], 0)
    assert peer_ledger() == [] and bell() == []
    assert owner.st.get_grant(term.grant_id).revision == 1  # nothing raised or changed it


@pytest.mark.anyio
async def test_a_send_peer_is_not_hinted_about_a_terminal_grant(owner, decisions, monkeypatch):
    term = _term(owner)
    decisions.ask(term.session_key)
    con = sqlite3.connect(owner.st.db_path)
    con.execute("UPDATE grants SET level = 'send' WHERE grant_id = ?", (term.grant_id,))
    con.commit()
    con.close()
    hinted: list = []
    monkeypatch.setattr(owner.fed, "channel_for", lambda peer_id: object())
    monkeypatch.setattr(owner.fed, "hint", lambda *a: hinted.append(a))
    await owner.fed._decision_hints()
    assert hinted == [] and owner.fed._decision_marks == {}


# ── decisions: who may answer, and what ───────────────────────────────


@pytest.mark.anyio
async def test_view_and_suggest_cannot_answer_a_decision(owner, decisions):
    for level in ("view", "suggest"):
        made = owner.grant(level)
        decisions.ask(made.session_key)
        assert await owner.refused("decision.answer", answer(made)) == "no_grant"
        snap = await owner.call("session.snapshot", ref(made))
        # …and such a peer is not even shown what could be answered
        assert all("choices" not in q and "questions" not in q for q in snap["pending_requests"])
    assert decisions.calls == [] and owner.st.open_peer_decisions() == []
    assert peer_ledger() == []


@pytest.mark.anyio
async def test_a_send_peer_answers_allow_once_with_the_owners_stored_identity(owner, decisions):
    made = owner.grant("send")
    request = decisions.ask(made.session_key)
    snap = await owner.call("session.snapshot", ref(made))
    (shown,) = snap["pending_requests"]
    # What the peer is offered: the one-time kinds. Never the standing grant, never the stop.
    assert shown["choices"] == ["approve", "reject"]
    assert "always" not in shown and "payload" not in shown and "grant-1" not in json.dumps(snap)
    assert remote.check_snapshot(snap) is snap
    # …and the owner's name for THIS exact request, which the answer sends back unchanged. It
    # carries the payload digest only inside a hash.
    assert shown["offer"] == offers.token(made.grant_id, made.session_key, request)
    assert offers.valid_token(shown["offer"]) and request["payload_digest"] not in json.dumps(snap)

    seen: list = []
    decisions.before_decide = lambda call: seen.append(owner.st.peer_decision(call["decision_id"]))
    sent = answer(made, shown["request_id"], actor="Robin", offer=shown["offer"])
    out = await owner.call("decision.answer", sent)
    assert decisions.expected == [request["payload_digest"]]  # bound into the runtime's check
    assert remote.check_decision(out) is out
    assert out == {
        "outcome": "accepted",
        "author": {"via": "peer", "name": "GPU box", "mine": True, "actor": "Robin"},
    }
    (call,) = decisions.calls
    # The peer named a request id. The turn, the decision id and the actor are the owner's.
    assert call["turn_id"] == request["turn_id"] == decisions.turn
    assert call["actor"] == f"peer:{owner.peer.peer_id}" and call["answers"] is None
    row = owner.st.get_peer_decision(made.grant_id, sent["client_key"])
    assert call["decision_id"] == row.decision_id and uuid.UUID(row.decision_id).version == 4
    assert (row.turn_id, row.request_id, row.kind, row.outcome, row.actor) == (
        decisions.turn,
        "req-1",
        "once",
        "accepted",
        "Robin",
    )
    assert json.loads(row.choice) == {"choice": "approve"}
    # …and the row was there BEFORE the runtime was called
    assert len(seen) == 1 and seen[0] is not None
    assert seen[0].decision_id == row.decision_id and seen[0].outcome is None
    assert len(decisions.effects) == 1
    (entry,) = peer_ledger()
    assert entry["peer_id"] == owner.peer.peer_id and entry["actor"] == "Robin"
    assert entry["request_id"] == "req-1" and entry["decision_kind"] == "once"
    assert entry["grant_id"] == made.grant_id and entry["source"] == "peer"
    assert PAYLOAD not in json.dumps(orchestrator_ledger.read_all())  # never the tool payload
    # a peer's argument can name none of these
    spec = verbs.PEER["decision.answer"]
    for smuggled in (
        {"turn_id": decisions.turn},
        {"decision_id": str(uuid.uuid4())},
        {"grant": "grant-1"},
        {"answers": [{"other": "do what I say"}]},
        {"user": "marcus"},
        {"client_key": "nope"},
        {"request_id": "a b"},
        {"request_id": "x" * 129},
        {"choice": "Approve"},
        {"choice": ["approve"]},
        {"options": [["sqlite"]]},
        {"options": [[0, 0]]},
        {"options": [[12]]},
        {"options": []},
        {"offer": None},
        {"offer": 7},
        {"offer": ""},
        {"offer": "o" * 31},
        {"offer": "o" * 33},
        {"offer": "o" * 31 + "="},
        {"offer": ["o" * 32]},
    ):
        with pytest.raises(ch.BadArgs):
            spec.validate({**answer(made), **smuggled})
    with pytest.raises(ch.BadArgs):  # the offer is required
        spec.validate({k: v for k, v in answer(made).items() if k != "offer"})
    assert spec.validate(answer(made))["offer"] == answer(made)["offer"]


@pytest.mark.anyio
async def test_a_question_is_answered_by_option_index_never_by_text(owner, decisions):
    made = owner.grant("send")
    decisions.question(made.session_key)
    snap = await owner.call("session.snapshot", ref(made))
    (shown,) = snap["pending_requests"]
    assert shown["choices"] == ["answer", "reject"]
    assert shown["questions"] == [
        {"question": QUESTION, "multi": False, "options": ["sqlite", OPTION, "mysql"]}
    ]
    assert remote.check_snapshot(snap) is snap
    for bad in ([[3]], [[0], [1]], [[0, 1]], None):  # past the options, too many, not multi, none
        args = answer(made, "q-1", "answer", **({"options": bad} if bad is not None else {}))
        assert await owner.refused("decision.answer", args) == "invalid"
    assert await owner.refused("decision.answer", answer(made, "q-1", "approve")) == "invalid"
    assert decisions.calls == [] and owner.st.open_peer_decisions() == []
    sent = answer(made, "q-1", "answer", options=[[1]])
    out = await owner.call("decision.answer", sent)
    assert out["outcome"] == "accepted"
    (call,) = decisions.calls
    assert call["decision"] == "answer" and call["answers"] == [{"selected": [OPTION]}]
    row = owner.st.get_peer_decision(made.grant_id, sent["client_key"])
    # Stored: the kind and the indexes. Never the option's label or the question.
    assert json.loads(row.choice) == {"choice": "answer", "options": [[1]]} and row.kind == "answer"
    con = sqlite3.connect(owner.st.db_path)
    dump = "\n".join(con.iterdump())
    con.close()
    assert "PLANTED" not in dump
    assert "PLANTED" not in json.dumps(orchestrator_ledger.read_all())


def _api_engines() -> list[str]:
    return sorted(ENGINES.values())


def test_the_runtime_classifies_every_decision_kind_and_this_package_holds_no_list():
    """THE classification is `structured_runtime.decision_effect`. Every kind the native
    runtime accepts is classified; `always` is the one that widens; a kind nobody classified
    is treated as widening; and no module of `federation/` names a decision kind."""
    accepted = None
    for node in ast.walk(ast.parse(inspect.getsource(native_runtime.decide))):
        if isinstance(node, ast.Set) and all(isinstance(e, ast.Constant) for e in node.elts):
            kinds = {e.value for e in node.elts}
            if "approve" in kinds:
                accepted = kinds
                break
    assert (
        accepted
        == set(runtime.DECISION_EFFECTS)
        == {
            "approve",
            "reject",
            "cancel",
            "always",
            "answer",
        }
    )
    assert runtime.decision_effect("always") == "standing"
    assert runtime.WIDENING_EFFECTS == {"standing"}
    assert {k for k, v in runtime.DECISION_EFFECTS.items() if v in ("decline", "stop")} == (
        runtime._DECLINES
    )
    for unknown in ("approve_always", "allow_always", "skip_permissions", "bypass", "", None, 7):
        assert runtime.decision_effect(unknown) == "standing"
        assert offers.peer_may(unknown) is False
    assert [k for k in runtime.DECISION_EFFECTS if offers.peer_may(k)] == [
        "approve",
        "reject",
        "answer",
    ]
    assert offers.PEER_EFFECTS.isdisjoint(runtime.WIDENING_EFFECTS)
    # No kind is spelled anywhere in the package: the runtime's table is the only list.
    kinds = set(runtime.DECISION_EFFECTS)
    for path in sorted((SRC / "federation").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in kinds - {"answer"}, (path.name, node.value)
    # `answer` is an EFFECT name too (`offers.PEER_EFFECTS`), and that is its only use there.
    # `decide` has exactly two callers: the operator's route and `together.py`.
    callers = set()
    for path in _sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "decide"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("runtime", "structured_runtime")
            ):
                callers.add(_rel(path))
    assert callers == {"routes/structured.py", "federation/together.py"}


@pytest.mark.anyio
@pytest.mark.parametrize("engine", _api_engines())
async def test_a_widening_choice_is_owner_only_on_every_runtime(owner, decisions, engine):
    made = owner.grant("send", key=f"{engine}:{uuid.uuid4()}")
    request = decisions.ask(made.session_key, choices=("approve", "reject", "cancel"))
    assert request["always"]  # the runtime offers a standing grant for it
    for widening in ("always", "cancel", "approve_always", "skip_permissions"):
        assert await owner.refused("decision.answer", answer(made, choice=widening)) == (
            "owner_only"
        )
    # even when a runtime listed it among a request's own choices
    decisions.ask(made.session_key, "req-2", choices=("approve", "always", "reject"))
    assert await owner.refused("decision.answer", answer(made, "req-2", "always")) == "owner_only"
    snap = await owner.call("session.snapshot", ref(made))
    assert [q["choices"] for q in snap["pending_requests"]] == [["approve", "reject"]] * 2
    assert decisions.calls == [] and decisions.effects == []
    assert owner.st.open_peer_decisions() == [] and peer_ledger() == []
    # the owner can still give it; the peer's one-time answer still works
    assert (await owner.call("decision.answer", answer(made)))["outcome"] == "accepted"


@pytest.mark.anyio
async def test_the_request_is_resolved_on_the_owner_and_must_be_pending_there(owner, decisions):
    made = owner.grant("send")
    key = made.session_key
    assert await owner.refused("decision.answer", answer(made, "req-9")) == "not_pending"
    decisions.ask(key, "req-1")
    assert await owner.refused("decision.answer", answer(made, "req-2")) == "not_pending"
    # an id that names two requests is not guessed at
    decisions.ask(key, "dup", turn_id=str(uuid.uuid4()))
    decisions.ask(key, "dup", turn_id=str(uuid.uuid4()))
    assert await owner.refused("decision.answer", answer(made, "dup")) == "not_pending"
    # a background task's request is never a peer's, whatever id it is named by
    decisions.ask(key, "bg-1", background_task={"task_id": "t1", "description": ""})
    assert await owner.refused("decision.answer", answer(made, "bg-1")) == "not_pending"
    snap = await owner.call("session.snapshot", ref(made))
    background = [q for q in snap["pending_requests"] if "choices" not in q]
    assert len(background) == 1  # and it is offered nothing
    # a choice the runtime does not offer for THIS request (it could only be declined)
    decisions.ask(key, "incomplete", choices=("reject", "cancel"))
    assert await owner.refused("decision.answer", answer(made, "incomplete")) == "invalid"
    assert decisions.calls == [] and owner.st.open_peer_decisions() == []
    assert (await owner.call("decision.answer", answer(made, "incomplete", "reject")))[
        "outcome"
    ] == "accepted"
    assert decisions.effects[0]["decision"] == "reject"


# ── decisions: one identity, replay, and the two crash windows ────────


@pytest.mark.anyio
async def test_one_decision_id_per_client_key_replayed_after_a_lost_answer(owner, decisions):
    made = owner.grant("send")
    decisions.ask(made.session_key)
    sent = answer(made)
    first = await owner.call("decision.answer", sent)
    # the answer was lost on the way back; the peer retries with the same key
    again = await owner.call("decision.answer", sent)
    restarted = owner.restarted()
    third = await restarted.call("decision.answer", sent)
    assert first == again == third and first["outcome"] == "accepted"
    assert len(decisions.effects) == 1 and len(decisions.calls) == 1  # no second call at all
    assert len({c["decision_id"] for c in decisions.calls}) == 1
    assert len(peer_ledger()) == 1 and len(authors(owner.st, made.session_key)) == 1


@pytest.mark.anyio
async def test_a_changed_choice_or_request_under_the_same_key_is_a_conflict(owner, decisions):
    made = owner.grant("send")
    decisions.ask(made.session_key, "req-1")
    decisions.ask(made.session_key, "req-2")
    sent = answer(made, "req-1", "approve")
    decisions.crash_before = True
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", sent)
    # the row is stored; the SAME key now arrives with another choice, then another request
    other = {**sent, "choice": "reject"}
    assert await owner.refused("decision.answer", other) == "conflict"
    assert await owner.refused("decision.answer", {**sent, "request_id": "req-2"}) == "conflict"
    assert decisions.effects == [] and len(decisions.calls) == 1  # refused: never applied
    out = await owner.call("decision.answer", sent)  # the original still replays
    assert out["outcome"] == "accepted" and decisions.effects[0]["decision"] == "approve"
    # after it settled, a changed choice is still a conflict and still calls nothing
    assert await owner.refused("decision.answer", other) == "conflict"
    assert len(decisions.effects) == 1


@pytest.mark.anyio
async def test_a_crash_between_the_stored_row_and_the_runtime_call_is_replayed_unchanged(
    owner, decisions
):
    made = owner.grant("send")
    decisions.ask(made.session_key)
    sent = answer(made, actor="Robin")
    decisions.crash_before = True
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", sent)
    row = owner.st.get_peer_decision(made.grant_id, sent["client_key"])
    assert row is not None and row.outcome is None  # persisted before the call
    assert decisions.effects == [] and authors(owner.st, made.session_key) == []
    assert not fence.held_elsewhere(owner.st.directory, made.grant_id)  # the crash released it

    restarted = owner.restarted()
    out = await restarted.call("decision.answer", sent)
    assert out["outcome"] == "accepted" and out["author"]["mine"] is True
    assert [c["decision_id"] for c in decisions.calls] == [row.decision_id, row.decision_id]
    assert decisions.calls[0] == decisions.calls[1]  # the stored decision, unchanged
    assert len(decisions.effects) == 1
    assert owner.st.get_peer_decision(made.grant_id, sent["client_key"]).outcome == "accepted"


@pytest.mark.anyio
async def test_a_crash_between_runtime_acceptance_and_attribution_recovers_the_real_winner(
    owner, decisions
):
    """The runtime accepted the peer's decision, and the process died before the author and the
    outcome were recorded. The retry causes no second effect and reads the winner from the
    journal: it does not assume the caller won."""
    made = owner.grant("send")
    decisions.ask(made.session_key)
    sent = answer(made, actor="Robin")
    decisions.crash_after = True
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", sent)
    row = owner.st.get_peer_decision(made.grant_id, sent["client_key"])
    assert row.outcome is None and authors(owner.st, made.session_key) == []
    assert len(decisions.effects) == 1 and peer_ledger() == []

    restarted = owner.restarted()
    out = await restarted.call("decision.answer", sent)
    assert out == {
        "outcome": "accepted",
        "author": {"via": "peer", "name": "GPU box", "mine": True, "actor": "Robin"},
    }
    assert len(decisions.calls) == 1 and len(decisions.effects) == 1  # NO second call
    (stored,) = authors(owner.st, made.session_key)
    assert stored["author"] == made.principal and stored["kind"] == "once"
    assert owner.st.get_peer_decision(made.grant_id, sent["client_key"]).outcome == "accepted"
    assert len(peer_ledger()) == 1  # audited once, on recovery


@pytest.mark.anyio
async def test_recovery_reports_the_owner_when_the_owner_actually_won(owner, decisions):
    """The peer's call died BEFORE the runtime took it; the operator then answered, and that
    answer's own attribution was lost too. The peer's retry must say the OWNER settled it."""
    made = owner.grant("send")
    request = decisions.ask(made.session_key)
    sent = answer(made)
    decisions.crash_before = True
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", sent)
    # the operator answers; the attribution hook never runs (as if the process died there)
    await decisions.decide(
        made.session_key,
        turn_id=request["turn_id"],
        request_id="req-1",
        decision_id=str(uuid.uuid4()),
        decision="reject",
        user="marcus",
    )
    assert authors(owner.st, made.session_key) == []
    calls = len(decisions.calls)

    out = await owner.restarted().call("decision.answer", sent)
    assert out == {"outcome": "settled", "author": {"via": "owner", "name": None, "mine": False}}
    assert len(decisions.calls) == calls and len(decisions.effects) == 1  # no call, one effect
    (stored,) = authors(owner.st, made.session_key)
    assert stored["author"] == "owner" and stored["kind"] == "decline"
    assert owner.st.get_peer_decision(made.grant_id, sent["client_key"]).outcome == "settled"
    assert peer_ledger() == []  # the peer's decision never took effect


@pytest.mark.anyio
async def test_recovery_guesses_no_author_when_the_journal_cannot_say(owner, decisions):
    made = owner.grant("send")
    decisions.ask(made.session_key)
    sent = answer(made)
    decisions.crash_after = True
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", sent)
    decisions.forgets = True  # the record is not there to read
    assert await owner.restarted().refused("decision.answer", sent) == "not_pending"
    assert authors(owner.st, made.session_key) == [] and peer_ledger() == []
    assert len(decisions.effects) == 1 and len(decisions.calls) == 1
    # a claim whose handoff is unknown decides nothing either, and is reported as it is
    other = owner.grant("send")
    decisions.ask(other.session_key)
    decisions.forgets = False
    late = answer(other)
    decisions.crash_after = True
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", late)
    for record in decisions.journal.values():
        if record["key"] == other.session_key:
            record["handoff"] = "uncertain"
    out = await owner.call("decision.answer", late)
    assert out == {"outcome": "uncertain", "author": None}
    assert authors(owner.st, other.session_key) == []
    assert owner.st.get_peer_decision(other.grant_id, late["client_key"]).outcome is None


@pytest.mark.anyio
async def test_the_round_settles_an_open_decision_from_the_journal_and_never_calls_decide(
    owner, decisions, monkeypatch
):
    made = owner.grant("send")
    decisions.ask(made.session_key)
    still = owner.grant("send")
    decisions.ask(still.session_key)
    sent, waiting = answer(made), answer(still)
    decisions.crash_after = True
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", sent)
    decisions.crash_before = True
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", waiting)
    calls = len(decisions.calls)
    assert await together.settle_open(owner.fed) == 0  # too fresh: an answer may be in flight
    monkeypatch.setattr(together, "OPEN_AFTER_S", 0.0)
    assert await together.settle_open(owner.fed) == 1
    assert owner.st.get_peer_decision(made.grant_id, sent["client_key"]).outcome == "accepted"
    (stored,) = authors(owner.st, made.session_key)
    assert stored["author"] == made.principal and len(peer_ledger()) == 1
    # the one whose request is still pending is left for its caller's retry: never delivered here
    assert owner.st.get_peer_decision(still.grant_id, waiting["client_key"]).outcome is None
    assert len(decisions.calls) == calls and len(decisions.effects) == 1
    assert "decide" not in {
        n.attr
        for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(together.settle_open))))
        if isinstance(n, ast.Attribute)
    }


# ── decisions: the runtime settles once ───────────────────────────────


async def _snapshot_authors(o: Owner, grant: store.Grant, peer=None) -> list[dict]:
    snap = await o.call("session.snapshot", ref(grant), peer)
    return [(d["request_id"], d["answer"], d["author"], d["mine"]) for d in snap["decisions"]]


@pytest.mark.anyio
@pytest.mark.parametrize("winner", ["owner", "peer"])
async def test_owner_and_peer_answer_at_once_and_everyone_reads_the_same_author(
    owner, decisions, winner
):
    made = owner.grant("send")
    watcher = trusted(owner.st, "Third box")
    watching = owner.grant("view", made.session_key, peer=watcher)
    request = decisions.ask(made.session_key)
    sent = answer(made)
    if winner == "peer":
        # the peer's call is inside the runtime when the operator's arrives
        decisions.gate = asyncio.Event()
        racing = asyncio.create_task(owner.call("decision.answer", sent))
        await decisions.started.wait()
        decisions.gate.set()
        out = await racing
        decisions.gate = None
        with pytest.raises(runtime.StructuredError) as lost:
            await decisions.owner_decides(owner.fed, made.session_key, request, "reject")
        assert lost.value.status == 409
        await owner.fed.owner_decided(made.session_key, request["turn_id"], "req-1")
        expected = {"via": "peer", "name": "GPU box"}
        assert out["outcome"] == "accepted"
    else:
        await decisions.owner_decides(owner.fed, made.session_key, request, "reject")
        out = await owner.call("decision.answer", sent)
        expected = {"via": "owner", "name": None}
        assert out == {"outcome": "settled", "author": {**expected, "mine": False}}
    assert len(decisions.effects) == 1  # ONE effect
    kind = "once" if winner == "peer" else "decline"
    mine = winner == "peer"
    assert await _snapshot_authors(owner, made) == [("req-1", kind, expected, mine)]
    assert await _snapshot_authors(owner, watching, watcher) == [("req-1", kind, expected, False)]
    from agent_sessions import share_api

    pane = share_api.decision_views(authors(owner.st, made.session_key))
    assert [(d["request_id"], d["answer"], d["author"]) for d in pane] == [
        ("req-1", kind, expected)
    ]
    # a retry of the loser, or of the winner, reads the same stored row
    assert (await owner.call("decision.answer", sent))["author"]["via"] == expected["via"]
    assert len(decisions.effects) == 1 and len(authors(owner.st, made.session_key)) == 1
    assert len(peer_ledger()) == (1 if winner == "peer" else 0)
    snap = await owner.call("session.snapshot", ref(watching), watcher)
    assert remote.check_snapshot(snap) is snap
    assert owner.peer.peer_id not in json.dumps(snap)  # a name, never an id


@pytest.mark.anyio
async def test_two_peers_under_two_grants_race_to_one_effect_and_one_author(owner, decisions):
    second = trusted(owner.st, "Second box")
    first_grant = owner.grant("send")
    second_grant = owner.grant("send", first_grant.session_key, peer=second)
    decisions.ask(first_grant.session_key)
    a, b = answer(first_grant, actor="Robin"), answer(second_grant, actor="Sam")
    decisions.gate = asyncio.Event()
    racing = [
        asyncio.create_task(owner.call("decision.answer", a)),
        asyncio.create_task(owner.call("decision.answer", b, second)),
    ]
    # Two grants are two fences: both calls are inside the runtime before either is let go.
    await until(lambda: len(decisions.calls) == 2)
    decisions.gate.set()
    one, two = await asyncio.gather(*racing)
    assert sorted(o["outcome"] for o in (one, two)) == ["accepted", "settled"]
    assert len(decisions.effects) == 1
    won_first = one["outcome"] == "accepted"
    name, actor = ("GPU box", "Robin") if won_first else ("Second box", "Sam")
    winner, loser = (one, two) if won_first else (two, one)
    assert winner["author"] == {"via": "peer", "name": name, "mine": True, "actor": actor}
    assert loser["author"] == {"via": "peer", "name": name, "mine": False, "actor": actor}
    views = (
        await _snapshot_authors(owner, first_grant),
        await _snapshot_authors(owner, second_grant, second),
    )
    assert [v[0][:3] for v in views] == [("req-1", "once", {"via": "peer", "name": name})] * 2
    assert sorted(v[0][3] for v in views) == [False, True]
    (stored,) = authors(owner.st, first_grant.session_key)
    assert stored["decision_id"] == decisions.effects[0]["decision_id"]
    assert len(peer_ledger()) == 1
    # both retry: the same single effect and the same stored author
    again = await asyncio.gather(
        owner.call("decision.answer", a), owner.call("decision.answer", b, second)
    )
    assert [o["author"]["name"] for o in again] == [name, name] and len(decisions.effects) == 1


@pytest.mark.anyio
async def test_a_late_answer_to_a_settled_request_names_who_settled_it(owner, decisions):
    made = owner.grant("send")
    request = decisions.ask(made.session_key)
    await decisions.owner_decides(owner.fed, made.session_key, request)
    out = await owner.call("decision.answer", answer(made))  # a key this owner never saw
    assert out == {"outcome": "settled", "author": {"via": "owner", "name": None, "mine": False}}
    assert owner.st.open_peer_decisions() == [] and len(decisions.effects) == 1
    # also when the operator's own attribution was never written: the journal still says
    con = sqlite3.connect(owner.st.db_path)
    con.execute("DELETE FROM decision_authors")
    con.commit()
    con.close()
    out = await owner.call("decision.answer", answer(made))
    assert out["outcome"] == "settled" and out["author"]["via"] == "owner"
    assert len(authors(owner.st, made.session_key)) == 1
    # Native request ids repeat from turn to turn. A NEW turn's `req-1` that was withdrawn
    # unanswered is not the old turn's: nobody settled it, and no author is borrowed.
    decisions.turn = str(uuid.uuid4())
    decisions.ask(made.session_key, "req-1")
    decisions.requests[made.session_key].clear()  # retracted, not decided
    assert await owner.refused("decision.answer", answer(made)) == "not_pending"
    assert len(authors(owner.st, made.session_key)) == 1 and len(decisions.effects) == 1


@pytest.mark.anyio
async def test_the_operators_own_decide_route_records_the_owner(auth_cfg, decisions, monkeypatch):
    monkeypatch.setattr(shares, "session_state", lambda prov, native: "live")
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    fed = c.app.state.federation
    key, unshared = api_key(), api_key()
    body = {"decision_id": str(uuid.uuid4()), "turn_id": decisions.turn, "request_id": "req-1",
            "decision": "approve"}  # fmt: skip
    # a session nobody ever shared records nothing, and the store is not even created
    decisions.ask(unshared)
    r = c.post(f"/api/structured/sessions/{unshared}/decisions", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    assert not fed.store.db_path.exists()

    peer = trusted(fed.store, "GPU box")
    made = grants.create(
        fed.store, session_key=key, peer_id=peer.peer_id, level="view", expires_in=None
    )
    decisions.ask(key)
    body = {**body, "decision_id": str(uuid.uuid4())}
    r = c.post(f"/api/structured/sessions/{key}/decisions", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    (stored,) = fed.store.decision_authors(key)
    assert stored["author"] == "owner" and stored["kind"] == "once"
    assert stored["request_id"] == "req-1" and stored["turn_id"] == decisions.turn
    assert fed.store.decision_authors(unshared) == []
    # a refused decision records nothing
    late = {**body, "decision_id": str(uuid.uuid4())}
    refused = c.post(f"/api/structured/sessions/{key}/decisions", json=late, headers=hdr)
    assert refused.status_code == 409 and len(fed.store.decision_authors(key)) == 1
    # attribution that cannot be written never fails the operator's own decision
    decisions.ask(key, "req-2")

    async def broken(*args, **kwargs):
        raise RuntimeError("the federation store is gone")

    monkeypatch.setattr(together, "_author", broken)
    body = {**body, "decision_id": str(uuid.uuid4()), "request_id": "req-2"}
    r = c.post(f"/api/structured/sessions/{key}/decisions", json=body, headers=hdr)
    assert r.status_code == 200 and len(decisions.effects) == 3
    assert made.grant_id


# ── decisions: missions, and revoke against answer ────────────────────


@pytest.mark.anyio
async def test_a_mission_held_sessions_decisions_are_never_a_peers(
    owner, decisions, held, monkeypatch
):
    made = owner.grant("send")
    decisions.ask(made.session_key)
    held.add(made.session_key)
    assert await owner.refused("decision.answer", answer(made)) == "held"
    snap = await owner.call("session.snapshot", ref(made))
    assert all("choices" not in q for q in snap["pending_requests"])  # offered nothing
    # …and the check is made again INSIDE the fence: a mission that takes it in between
    held.clear()
    real = together._admit

    def admitted(st, peer_id, args):
        held.add(made.session_key)
        return real(st, peer_id, args)

    monkeypatch.setattr(together, "_admit", admitted)
    assert await owner.refused("decision.answer", answer(made)) == "held"
    assert decisions.calls == [] and owner.st.open_peer_decisions() == []
    # a membership that cannot be read is not "no mission"
    monkeypatch.setattr(together, "_admit", real)
    held.clear()

    def unreadable(key, **_kw):
        raise OSError("missions.db is locked")

    monkeypatch.setattr(grants.missions, "session_mission", unreadable)
    assert await owner.refused("decision.answer", answer(made)) == "unavailable"
    assert decisions.calls == []
    # plan approval and mission completion are not requests: no verb names a mission
    assert not [v for v in verbs.PEER if "mission" in v or "plan" in v or "complete" in v]


@pytest.mark.anyio
@pytest.mark.parametrize("writer", ["same", "other"])
@pytest.mark.parametrize("action", ["revoke", "peer_revoke", "archive", "lower"])
async def test_answer_first_then_a_change_the_change_waits_and_lands_after(
    owner, decisions, live, waiting, action, writer
):
    from test_federation_fence import ender, landed, stores, untouched

    made = owner.grant("send")
    decisions.ask(made.session_key)
    generation = owner.st.generation()
    decisions.gate = asyncio.Event()
    answering = asyncio.create_task(owner.call("decision.answer", answer(made)))
    await decisions.started.wait()  # admitted, and inside the runtime under the grant's fence
    assert fence.held_elsewhere(owner.st.directory, made.grant_id)
    waiting.clear()
    changing = asyncio.create_task(ender(action, owner, made, stores(owner, writer), live)())
    assert await asyncio.to_thread(waiting.wait, 20), "the change never met the fence"
    # In the middle: NOTHING of the change is committed while the decision is in flight.
    assert untouched(owner, made, generation) and not changing.done()
    assert decisions.effects == []
    decisions.gate.set()
    result = (await asyncio.gather(answering, return_exceptions=True))[0]
    await asyncio.wait_for(changing, 20)
    assert landed(action, owner, made)
    assert len(decisions.effects) == 1  # admitted under a live grant, exactly once
    assert not fence.held_elsewhere(owner.st.directory, made.grant_id)
    # Its answer is the outcome, or `no_grant` when the change committed before the answer's
    # own second check ran: never anything in between.
    if isinstance(result, dict):
        assert result["outcome"] == "accepted"
    else:
        assert isinstance(result, ch.VerbError) and result.code == "no_grant"
    (stored,) = authors(owner.st, made.session_key)
    assert stored["author"] == made.principal  # recorded inside the fence, before the change


@pytest.mark.anyio
@pytest.mark.parametrize("writer", ["same", "other"])
@pytest.mark.parametrize("action", ["revoke", "peer_revoke", "archive", "lower", "extend"])
async def test_a_change_first_then_answer_nothing_is_decided(
    owner, decisions, live, action, writer
):
    from test_federation_fence import ender, landed, stores

    made = owner.grant("send")
    decisions.ask(made.session_key)
    await ender(action, owner, made, stores(owner, writer), live)()
    assert landed(action, owner, made)
    assert await owner.refused("decision.answer", answer(made)) == "no_grant"
    assert decisions.calls == [] and owner.st.open_peer_decisions() == []
    assert authors(owner.st, made.session_key) == []


@pytest.mark.anyio
async def test_a_change_between_the_arrival_check_and_the_fence_is_caught_inside_the_fence(
    owner, decisions, monkeypatch
):
    made = owner.grant("send")
    decisions.ask(made.session_key)
    real = together.inputs._refuse_held
    admitted: list = []

    async def then_revoked(session_key):
        await real(session_key)
        # After the arrival check and before the fence: another process revokes, just now.
        await asyncio.to_thread(grants.revoke, store.Store(owner.st.directory), made.grant_id)

    def inside(st, peer_id, args):
        admitted.append(st.get_grant(args["grant_id"]).state)
        return real_admit(st, peer_id, args)

    real_admit = together._admit
    monkeypatch.setattr(together.inputs, "_refuse_held", then_revoked)
    monkeypatch.setattr(together, "_admit", inside)
    assert await owner.refused("decision.answer", answer(made)) == "no_grant"
    assert admitted == ["revoked"]  # refused by the re-check INSIDE the fence, not by luck
    assert decisions.calls == [] and owner.st.open_peer_decisions() == []


@pytest.mark.anyio
async def test_a_peer_holds_at_most_four_decisions_in_flight_and_a_slow_one_times_out(
    owner, decisions, monkeypatch
):
    made = [owner.grant("send") for _ in range(5)]
    for grant in made:
        decisions.ask(grant.session_key)
    decisions.gate = asyncio.Event()
    racing = [asyncio.create_task(owner.call("decision.answer", answer(g))) for g in made[:4]]
    await until(lambda: len(decisions.calls) == 4)
    assert await owner.refused("decision.answer", answer(made[4])) == "busy"
    monkeypatch.setattr(together.inputs, "ANSWER_WAIT_S", 0.05)
    slow = answer(made[4])
    decisions.gate.set()
    await asyncio.gather(*racing)
    decisions.gate = asyncio.Event()
    assert await owner.refused("decision.answer", slow) == "timeout"
    assert fence.held_elsewhere(owner.st.directory, made[4].grant_id)  # it goes on, fenced
    decisions.gate.set()
    await until(lambda: len(decisions.effects) == 5)
    await until(lambda: not fence.held_elsewhere(owner.st.directory, made[4].grant_id))
    monkeypatch.setattr(together.inputs, "ANSWER_WAIT_S", 25.0)
    assert (await owner.call("decision.answer", slow))["outcome"] == "accepted"
    assert len(decisions.effects) == 5


# ── every authorization regression, against every new verb ────────────

OWNER_VERBS = {
    "access.request": (together.access_request, None),
    "access.status": (together.access_status, None),
    "access.withdraw": (together.access_withdraw, None),
    "decision.answer": (together.decision_answer, "send"),
    "note.list": (notes.note_list, None),
    "note.add": (notes.note_add, None),
    "note.edit": (notes.note_edit, None),
    "note.delete": (notes.note_delete, None),
    "note.read": (notes.note_mark_read, None),
}


def test_every_new_verb_checks_the_grant_first_and_again_as_its_last_await():
    """As the pins for the reading verbs and the input verbs: the first `await` is the per-call
    check, the last one is the same check, and the statement after it is `return`."""
    assert set(OWNER_VERBS) == {
        name
        for name in verbs.PEER
        if name.split(".")[0] in ("access", "decision", "note") and not name.endswith(".changed")
    }
    for name, (handler, need) in OWNER_VERBS.items():
        assert verbs.PEER[name].handle is handler
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        body = tree.body[0].body
        last, check = body[-1], body[-2]
        assert isinstance(last, ast.Return) and isinstance(last.value, ast.Name), name
        assert isinstance(check, ast.Expr) and isinstance(check.value, ast.Await), name
        first = next(n for n in ast.walk(tree) if isinstance(n, ast.Await))
        for call in (check.value.value, first.value):
            assert ast.unparse(call.func) == "sharing._authorize", name
            words = {k.arg: ast.unparse(k.value) for k in call.keywords}
            # every one of them reads an API session (#2056): a terminal grant is refused
            assert words.get("kind") == "grants.KIND_API", name
            assert words.get("need") == (None if need is None else repr(need)), name
    # the hints answer nothing about any session and carry a grant id only
    for name in ("note.changed", "decision.changed"):
        spec = verbs.PEER[name]
        assert spec.validate({"grant_id": GRANT_ID}) == {"grant_id": GRANT_ID}
        for extra in ({"grant_id": "x"}, {"revision": 1}, {"text": "hi"}, {"unread": 3}):
            bad = {"grant_id": GRANT_ID, **extra}
            with pytest.raises(ch.BadArgs):
                spec.validate(dict(bad))


def _calls(made: store.Grant, note_id: str) -> dict[str, dict]:
    """One well-formed call of every owner-answered verb, under ``made``."""
    return {
        "access.request": ask(made, "send"),
        "access.status": ref(made),
        "access.withdraw": ref(made),
        "decision.answer": answer(made),
        "note.list": ref(made, after_seq=0),
        "note.add": ref(made, client_key=str(uuid.uuid4()), text="a note"),
        "note.edit": ref(made, note_id=note_id, text="edited"),
        "note.delete": ref(made, note_id=note_id),
        "note.read": ref(made, seq=1),
    }


@pytest.mark.anyio
@pytest.mark.parametrize("action", ENDINGS)
async def test_every_new_verb_is_no_grant_after_the_grant_ended_or_changed(
    owner, decisions, live, action
):
    made = owner.grant("send" if action != "stale_revision" else "suggest")
    decisions.ask(made.session_key)
    mine = (await owner.call("note.add", ref(made, client_key=str(uuid.uuid4()), text="before")))[
        "note"
    ]["note_id"]
    before = (len(owner.st.list_access_requests()), owner.st.owner_notes(made.grant_id)[2])
    end(action, owner, made, live)
    assert set(_calls(made, mine)) == set(OWNER_VERBS)
    for name, args in _calls(made, mine).items():
        assert await owner.refused(name, args) == "no_grant", name
    assert decisions.calls == [] and owner.st.open_peer_decisions() == []
    assert len(owner.st.list_access_requests()) == before[0]
    if action != "expiry":  # (an expired grant's thread is the operator's to read; unchanged)
        assert owner.st.owner_notes(made.grant_id)[2] == before[1]  # no change was written


@pytest.mark.anyio
async def test_another_peers_grant_and_an_unknown_grant_answer_no_grant_too(owner, decisions):
    stranger = trusted(owner.st, "Other box")
    made = owner.grant("send")
    decisions.ask(made.session_key)
    mine = (await owner.call("note.add", ref(made, client_key=str(uuid.uuid4()), text="mine")))[
        "note"
    ]["note_id"]
    for name, args in _calls(made, mine).items():
        assert await owner.refused(name, args, stranger) == "no_grant", name
    ghost = SimpleNamespace(grant_id="z" * 22, revision=1)
    for name, args in _calls(ghost, mine).items():
        assert await owner.refused(name, args) == "no_grant", name
    assert decisions.calls == []
    (note,) = owner.st.owner_notes(made.grant_id)[1]
    assert note.text == "mine" and not note.deleted


@pytest.mark.anyio
async def test_a_peer_revoke_by_another_process_ends_every_new_verb_over_the_real_channel(
    tmp_path, monkeypatch, decisions, live, held
):
    """A second `Store` over the same directory (another app process) revokes the peer while the
    owner's rounds are paused, so the channel stays OPEN: only the per-call check stands
    between the peer and each verb."""
    from test_federation_sharing import paired

    async with paired(tmp_path, monkeypatch) as p:
        made = await asyncio.to_thread(
            grants.create,
            p.a.store,
            session_key=api_key(),
            peer_id=p.b.peer_id,
            level="send",
            expires_in=None,
            accept_risk=True,
        )
        decisions.ask(made.session_key)
        added = await p.call("note.add", ref(made, client_key=str(uuid.uuid4()), text="hello"))
        assert (await p.call("note.list", ref(made)))["changes"][0]["note"]["text"] == "hello"
        assert (await p.call("access.status", ref(made)))["level"] == "send"
        # malformed arguments reset the stream and reach no handler
        for verb, args in (
            ("decision.answer", {**answer(made), "turn_id": decisions.turn}),
            ("note.add", ref(made, client_key="nope", text="x")),
            ("note.list", ref(made, after_seq=-1)),
            ("access.request", ask(made, "owner")),
            ("note.changed", {"grant_id": made.grant_id, "text": "x"}),
            ("grant.raise", ask(made, "send")),
        ):
            assert await p.refused(verb, args) == "refused", verb
        p.pause_owner_rounds(monkeypatch)
        await asyncio.to_thread(store.Store(p.a.dir).revoke, p.b.peer_id)
        assert p.b.channel_for(p.a.peer_id) is not None  # the channel is still open
        for name, args in _calls(made, added["note"]["note_id"]).items():
            assert await p.refused(name, args, timeout=30) == "no_grant", name
        assert decisions.calls == []
        assert p.a.store.owner_notes(made.grant_id)[1][0].text == "hello"


# ── the bell ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_access_row_names_the_peer_and_the_session_never_the_note(owner, monkeypatch):
    pushed = _pushes(monkeypatch)
    made = owner.grant("view")
    asked = await owner.call("access.request", ask(made, "send", note=NOTE))
    await owner.call("access.request", ask(made, "send", note=NOTE))  # raised ONCE
    await owner.quiet()
    (row,) = bell()
    assert row["action_id"] == notices.access_key(asked["request_id"])
    assert row["title"] == "GPU box asks for Send access" and row["project"] == TITLE
    assert row["reason"] == notifications.EVENT_REASON["peer_access_request"]
    assert row["session_id"] == made.session_key
    (payload,) = pushed()
    for dump in (json.dumps(bell()), json.dumps(payload)):
        assert NOTE not in dump and "PLANTED" not in dump
    assert payload["title"] == row["title"] and payload["body"] == TITLE
    assert payload["url"].endswith(f"/s/codex-api/{made.session_key.partition(':')[2]}")


def _pushes(monkeypatch):
    """Subscribe one device and return a callable giving every push payload sent, decrypted."""
    import httpx

    from agent_sessions import webpush
    from test_webpush import _client_keypair, _decrypt

    priv, pub_raw = _client_keypair()
    auth = b"0123456789abcdef"
    notifications.subscribe(
        {
            "endpoint": "https://fcm.googleapis.com/fcm/send/ep/abc",
            "keys": {"p256dh": webpush._b64e(pub_raw), "auth": webpush._b64e(auth)},
        }
    )
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(201)

    monkeypatch.setattr(webpush, "_TRANSPORT", httpx.MockTransport(handler))
    return lambda: [json.loads(_decrypt(body, priv, pub_raw, auth)) for body in captured]


class Receiver:
    """The receiving instance's side of the bell, over a real store and a scripted owner."""

    def __init__(self, fed: service.Federation) -> None:
        self.fed, self.st = fed, fed.store
        self.peer = trusted(self.st, "Alpha")
        self.answers: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []
        fed.channel_for = lambda peer_id: self  # type: ignore[method-assign]
        self.closed = False

    async def call(self, verb: str, args: dict | None = None, **_kw) -> dict:
        self.calls.append((verb, dict(args or {})))
        out = self.answers.get(verb)
        if isinstance(out, Exception):
            raise out
        if out is None:
            raise ch.CallError("no_grant")
        return json.loads(json.dumps(out))

    def mirror(self, *rows: dict) -> None:
        self.st.replace_remote(self.peer.peer_id, list(rows))

    async def quiet(self) -> None:
        while self.fed._tasks:
            await asyncio.gather(*list(self.fed._tasks), return_exceptions=True)
            # A real yield: a done task leaves `_tasks` in a callback the loop runs one turn later,
            # and awaiting a finished gather does not yield (this spun for an hour on CI).
            await asyncio.sleep(0)

    def keys(self) -> list[str]:
        return sorted(r["action_id"] for r in bell())


def entry(grant_id: str = "g" * 22, level: str = "send", title: str = "Their session") -> dict:
    return {
        "grant_id": grant_id,
        "engine": "codex-api",
        "native_id": str(uuid.uuid4()),
        "title": title,
        "level": level,
        "expires_at": time.time() + 3600,
        "revision": 1,
        "kind": "api",
    }


def listed(*rows: dict, generation: int = 1) -> dict:
    return {
        "generation": generation,
        "next": None,
        "grants": [
            {
                "grant_id": r["grant_id"],
                "session": {"engine": r["engine"], "id": r["native_id"]},
                "title": r["title"],
                "level": r["level"],
                "expires_at": r["expires_at"],
                "revision": r["revision"],
            }
            for r in rows
        ],
    }


def snapshot_with(*requests: dict) -> dict:
    return {
        "link": "abc",
        "revision": 1,
        "event_cursor": 1,
        "turns": [],
        "omitted_turns": 0,
        "pending_requests": list(requests),
        "can_send": False,
        "owner_present": True,
    }


def page(unread: int) -> dict:
    return {"reset": False, "seq": 4, "changes": [], "more": False, "next": None, "unread": unread}


@pytest.fixture
def receiver():
    return Receiver(service.Federation(static=X25519PrivateKey.generate(), name="beta"))


@pytest.mark.anyio
async def test_a_complete_refresh_announces_what_is_shared_changed_and_ended(receiver):
    r, pid = receiver, receiver.peer.peer_id
    one, two = entry("g" * 22, "view", "First"), entry("h" * 22, "suggest", "Second")
    r.answers["share.list"] = listed(one, two)
    assert await remote.refresh(r.fed, pid) is True
    await r.quiet()
    shared = [notices.remote_key("share", pid, g) for g in ("g" * 22, "h" * 22)]
    assert r.keys() == shared
    rows = {row["action_id"]: row for row in bell()}
    first = rows[shared[0]]
    assert first["title"] == "Alpha shared a session with you" and first["project"] == "First"
    assert first["reason"] == notifications.EVENT_REASON["peer_shared"]
    assert first["session_id"] == "" and "peer:" not in json.dumps(bell())  # no remote key
    assert notifications._link(first) == f"/remote/{pid}/{'g' * 22}"
    # the same list again raises nothing new: each reason is raised ONCE
    assert await remote.refresh(r.fed, pid) is True
    await r.quiet()
    assert r.keys() == shared and len(bell()) == 2
    # opening the session reads its row
    inbox.opened(pid, "g" * 22)
    assert r.keys() == shared[1:]
    # a changed level replaces the row; a grant the list no longer has is retracted, and said
    r.answers["share.list"] = listed({**two, "level": "send", "revision": 2}, generation=2)
    # (a grant that is newly at `send` is asked about its pending requests: there are none)
    r.answers["session.snapshot"] = snapshot_with()
    notices.announce_remote(r.st, "note", pid, "g" * 22, "First")
    notices.announce_remote(r.st, "decision", pid, "g" * 22, "First")
    assert await remote.refresh(r.fed, pid) is True
    await r.quiet()
    assert r.keys() == sorted(
        [notices.remote_key("ended", pid, "g" * 22), notices.remote_key("share", pid, "h" * 22)]
    )
    assert ("session.snapshot", {"grant_id": "h" * 22, "revision": 2}) in r.calls
    rows = {row["action_id"]: row for row in bell()}
    assert rows[notices.remote_key("share", pid, "h" * 22)]["title"] == (
        "Alpha changed what it shares with you"
    )
    ended = rows[notices.remote_key("ended", pid, "g" * 22)]
    assert ended["title"] == "Alpha stopped sharing a session" and ended["project"] == "First"


#: The working-together WRITES of the receiving routes (`routes/federation.py`), each with the
#: validator its route passes: every one is `remote.read(…, retry=False)`.
TOGETHER_WRITES = [
    ("access.request", {"level": "send", "note": ""}, remote.check_access),
    ("access.withdraw", {}, remote.check_access_status),
    ("decision.answer", {"request_id": "req-1", "choice": "approve"}, remote.check_decision),
    ("note.add", {"client_key": str(uuid.uuid4()), "text": "hi"}, remote.check_note),
    ("note.edit", {"note_id": "n" * 22, "text": "hi"}, remote.check_note),
    ("note.delete", {"note_id": "n" * 22}, remote.check_note),
    ("note.read", {"seq": 1}, remote.check_unread),
]


@pytest.mark.anyio
@pytest.mark.parametrize("verb,extra,check", TOGETHER_WRITES, ids=[w[0] for w in TOGETHER_WRITES])
async def test_a_working_together_write_whose_answer_is_lost_is_peer_lost_never_not_sent(
    receiver, verb, extra, check
):
    """#2061's rule holds for every write this feature adds: an answer the owner withheld after
    its handler ran (`answer_lost`), or a channel that ended after the request was queued, is
    `peer_lost` (504): the effect may have happened. `peer_offline` (503) is only what provably
    never left. Each is sent ONCE."""
    from agent_sessions.routes import federation as fed_routes

    r, pid = receiver, receiver.peer.peer_id
    r.mirror(entry("g" * 22, "send", "First"))

    async def code(failure) -> str:
        r.calls.clear()
        r.answers[verb] = failure
        with pytest.raises(remote.ReadError) as exc:
            await remote.read(r.fed, pid, "g" * 22, verb, extra, check=check, retry=False)
        assert [v for v, _ in r.calls] == [verb]  # once: never repeated, never re-resolved
        await r.quiet()
        return exc.value.code

    assert await code(ch.CallError(ch.ANSWER_LOST)) == remote.PEER_LOST
    assert await code(ch.CallError("closed")) == remote.PEER_LOST
    assert await code(ch.CallError("closed", dispatched=False)) == "peer_offline"
    assert await code(ch.CallError("timeout")) == "timeout"
    lost = fed_routes._read_error(remote.ReadError(remote.PEER_LOST))
    assert (lost.status, lost.code) == (504, "peer_lost")
    # no channel at all: nothing was sent
    r.fed.channel_for = lambda peer_id: None
    r.calls.clear()
    with pytest.raises(remote.ReadError) as offline:
        await remote.read(r.fed, pid, "g" * 22, verb, extra, check=check, retry=False)
    assert offline.value.code == "peer_offline" and r.calls == []


@pytest.mark.anyio
async def test_a_failed_refresh_retracts_nothing_and_a_final_refused_read_retracts_the_grant(
    receiver,
):
    r, pid = receiver, receiver.peer.peer_id
    one = entry("g" * 22, "send", "First")
    r.mirror(one)
    for kind in ("shared", "decision", "note"):
        assert notices.announce_remote(r.st, kind, pid, "g" * 22, "First") is True
    before = r.keys()
    assert len(before) == 3
    # The owner revoked while this instance was away. Until it LEARNS that, nothing goes:
    for failure in (
        ch.CallError("timeout"),
        ch.CallError("closed"),
        {"generation": 1, "next": None, "grants": [{"grant_id": "bad"}]},  # malformed
        {"generation": "x", "grants": [], "next": None},
    ):
        r.answers["share.list"] = failure
        assert await remote.refresh(r.fed, pid) is False
        await r.quiet()
        assert r.keys() == before and len(r.st.list_remote()) == 1
    # offline: no channel at all
    del r.fed.channel_for
    assert await remote.refresh(r.fed, pid) is False
    await r.quiet()
    assert r.keys() == before
    r.fed.channel_for = lambda peer_id: r  # reconnected
    # a REFUSED read is re-resolved first (#2055): while the refresh it asks for fails, nothing
    # is known, the answer is 503 and the bell keeps its rows
    r.answers["share.list"] = ch.CallError("timeout")
    with pytest.raises(remote.ReadError) as unknown:
        await remote.read(r.fed, pid, "g" * 22, "session.snapshot", {}, check=remote.check_snapshot)
    assert unknown.value.code == "unavailable"
    await r.quiet()
    assert r.keys() == before
    # a refused WRITE is sent once and decides nothing about the bell: the background refresh
    # does (and it fails here)
    with pytest.raises(remote.ReadError) as write:
        await remote.read(
            r.fed, pid, "g" * 22, "note.read", {"seq": 1}, check=remote.check_unread, retry=False
        )
    assert write.value.code == "no_grant"
    await r.quiet()
    assert r.keys() == before
    # the owner still lists the grant and refuses it again under that revision: that refusal is
    # final, and it retracts everything about that grant
    r.answers["share.list"] = listed(one)
    with pytest.raises(remote.ReadError) as refused:
        await remote.read(r.fed, pid, "g" * 22, "session.snapshot", {}, check=remote.check_snapshot)
    assert refused.value.code == "no_grant"
    await r.quiet()
    assert r.keys() == []
    # …and so does the first COMPLETE refresh, when no read happened first
    for kind in ("shared", "decision", "note"):
        notices.announce_remote(r.st, kind, pid, "g" * 22, "First")
    r.answers["share.list"] = listed()
    assert await remote.refresh(r.fed, pid) is True
    await r.quiet()
    assert r.keys() == [notices.remote_key("ended", pid, "g" * 22)]
    assert "PLANTED" not in json.dumps(bell())


@pytest.mark.anyio
async def test_a_hint_is_checked_with_the_owner_before_a_row_is_raised_or_retracted(
    receiver, monkeypatch
):
    pushed = _pushes(monkeypatch)
    r, pid = receiver, receiver.peer.peer_id
    gid = "g" * 22
    r.mirror(entry(gid, "send", "Their session"))
    decision = notices.remote_key("decision", pid, gid)
    note = notices.remote_key("note", pid, gid)
    pending = {
        "request_id": "req-1",
        "turn_id": str(uuid.uuid4()),
        "kind": "Bash",
        "summary": QUESTION,
        "risk": None,
        "choices": ["approve", "reject"],
    }
    # a hint alone raises nothing: the owner is asked
    r.answers["session.snapshot"] = snapshot_with()
    r.answers["note.list"] = page(0)
    for kind in inbox.HINTS:
        r.fed.peer_hint(pid, gid, kind)
    await r.quiet()
    assert r.keys() == [] and {c[0] for c in r.calls} == {"session.snapshot", "note.list"}
    assert ("note.list", {"grant_id": gid, "revision": 1, "after_seq": 0, "limit": 0}) in r.calls

    r.answers["session.snapshot"] = snapshot_with(pending)
    r.answers["note.list"] = page(2)
    for _ in range(3):  # hinted three times: raised ONCE each
        for kind in inbox.HINTS:
            r.fed.peer_hint(pid, gid, kind)
        await r.quiet()
    assert r.keys() == sorted([decision, note])
    rows = {row["action_id"]: row for row in bell()}
    assert rows[decision]["title"] == "Alpha: a decision is waiting"
    assert rows[decision]["reason"] == notifications.EVENT_REASON["peer_decision"]
    assert rows[note]["title"] == "Alpha left a note" and rows[note]["project"] == "Their session"
    assert "PLANTED" not in json.dumps(bell())  # never the question's text
    assert notifications._link(rows[note]) == f"/remote/{pid}/{gid}"
    # the pushes: a fixed sentence with the peer's name, the session's title, the remote pane
    payloads = pushed()
    assert sorted(p["title"] for p in payloads) == [
        "Alpha left a note",
        "Alpha: a decision is waiting",
    ]
    assert {p["body"] for p in payloads} == {"Their session"}
    assert {p["url"] for p in payloads} == {f"/remote/{pid}/{gid}"}
    assert all(set(p) == {"title", "body", "url"} for p in payloads)
    assert "PLANTED" not in json.dumps(payloads)

    # settled: no answerable request is left
    r.answers["session.snapshot"] = snapshot_with({**pending, "choices": []})
    r.fed.peer_hint(pid, gid, inbox.DECISION)
    await r.quiet()
    assert r.keys() == [note]
    # read: the owner says nothing is unread any more
    inbox.notes_read(pid, gid, 1)
    assert r.keys() == [note]
    inbox.notes_read(pid, gid, 0)
    assert r.keys() == []
    # deleted or hidden on the owner: the next check finds nothing unread
    notices.announce_remote(r.st, "note", pid, gid, "Their session")
    r.answers["note.list"] = page(0)
    r.fed.peer_hint(pid, gid, inbox.NOTE)
    await r.quiet()
    assert r.keys() == []

    # level lowered below `send`: a complete refresh retracts the decision row
    r.answers["session.snapshot"] = snapshot_with(pending)
    r.fed.peer_hint(pid, gid, inbox.DECISION)
    await r.quiet()
    assert r.keys() == [decision]
    lowered = {**entry(gid, "view", "Their session"), "revision": 2}
    r.answers["share.list"] = listed(lowered, generation=2)
    assert await remote.refresh(r.fed, pid) is True
    await r.quiet()
    assert decision not in r.keys()
    # …and under a `view` grant a hint cannot raise it, whatever the owner's snapshot claims
    r.fed.peer_hint(pid, gid, inbox.DECISION)
    await r.quiet()
    assert decision not in r.keys()

    # a hint about a grant this instance does not hold reads nothing; an owner that cannot be
    # asked changes nothing; a refusal retracts
    calls = len(r.calls)
    r.fed.peer_hint(pid, "z" * 22, inbox.NOTE)
    r.fed.peer_hint(pid, gid, "share")
    await r.quiet()
    assert len(r.calls) == calls
    notices.announce_remote(r.st, "note", pid, gid, "Their session")
    r.answers["note.list"] = ch.CallError("timeout")
    r.fed.peer_hint(pid, gid, inbox.NOTE)
    await r.quiet()
    assert note in r.keys()
    r.answers["note.list"] = ch.CallError("no_grant")
    r.fed.peer_hint(pid, gid, inbox.NOTE)
    await r.quiet()
    assert r.keys() == []


@pytest.mark.anyio
async def test_revoking_a_peer_here_retracts_its_rows_at_once(receiver):
    r, pid = receiver, receiver.peer.peer_id
    other = trusted(r.st, "Gamma")
    r.mirror(entry("g" * 22))
    for kind in ("shared", "decision", "note", "ended"):
        notices.announce_remote(r.st, kind, pid, "g" * 22, "First")
    kept = notices.remote_key("share", other.peer_id, "g" * 22)
    notices.announce_remote(r.st, "shared", other.peer_id, "g" * 22, "Theirs")
    assert len(bell()) == 5
    del r.fed.channel_for
    await r.fed.revoke(pid)
    # Before the revoke answered, not on a later round: no background task is awaited here.
    assert r.keys() == [kept]
    # a revoke written by ANOTHER process is retracted on this process's next round
    notices.announce_remote(r.st, "shared", other.peer_id, "h" * 22, "Theirs")
    store.Store(r.st.directory).revoke(other.peer_id)
    await r.fed._dial_round()
    assert r.keys() == []


@pytest.mark.anyio
async def test_the_owner_hints_a_send_peer_when_pending_requests_change(owner, decisions, held):
    hinted: list[tuple[str, str, str]] = []
    owner.fed.hint = lambda peer_id, verb, grant_id: hinted.append((peer_id, verb, grant_id))
    owner.fed.channel_for = lambda peer_id: object()
    send, view = owner.grant("send"), owner.grant("view")
    await owner.fed._decision_hints()
    assert hinted == []  # nothing pending: nothing to say
    decisions.ask(send.session_key)
    decisions.ask(view.session_key)
    await owner.fed._decision_hints()
    await owner.fed._decision_hints()
    # only the grant that may answer, once per change, and a grant id is all it carries
    assert hinted == [(owner.peer.peer_id, "decision.changed", send.grant_id)]
    await decisions.decide(
        send.session_key, turn_id=decisions.turn, request_id="req-1",
        decision_id=str(uuid.uuid4()), decision="approve", user="marcus",
    )  # fmt: skip
    await owner.fed._decision_hints()
    assert len(hinted) == 2  # settled: the peer is told to look again
    # a mission-held session's requests are not the peer's: it is told there is nothing
    decisions.ask(send.session_key, "req-2")
    held.add(send.session_key)
    await owner.fed._decision_hints()
    assert len(hinted) == 2
    owner.fed.channel_for = lambda peer_id: None  # the peer is away: nothing is sent or kept
    held.clear()
    await owner.fed._decision_hints()
    assert len(hinted) == 2 and owner.fed._decision_marks == {}


def test_no_notice_can_carry_another_partys_text():
    """`notices._post` takes named fields; every title is a fixed sentence and a name. No
    function here takes a note, a request, a question or a payload."""
    tree = ast.parse((SRC / "federation" / "notices.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {
                "text",
                "note",
                "summary",
                "payload",
                "approved_text",
            }, node.lineno
    assert set(notifications.EVENT_REASON) == {
        "peer_suggestion",
        "peer_shared",
        "peer_share_changed",
        "peer_share_ended",
        "peer_share_left",  # #2201: the owner's row, a name and a title
        "peer_access_request",
        "peer_decision",
        "peer_note",
    }
    for kind, (_row, reason, sentence) in notices._REMOTE_TITLES.items():
        assert reason in notifications.EVENT_REASON and "{name}" in sentence, kind
        assert sentence.count("{") == 1  # the peer's name and nothing else is filled in
    # a hint handler passes on a grant id and a fixed kind; inbox passes a mirrored TITLE
    for module in ("inbox.py", "service.py"):
        names = _names(SRC / "federation" / module)
        assert not names & {"note_page", "owner_notes", "list_access_requests"}, module


# ── the routes ────────────────────────────────────────────────────────

REMOTE = f"/api/federation/remote/{'a' * 32}/{'g' * 22}"
KEY = str(uuid.UUID(int=7))
DECISION = {"request_id": "r", "offer": "o" * 32, "choice": "approve", "client_key": KEY}
OWNER_NOTES = f"/api/federation/grants/{'g' * 22}/notes"
NEW_WRITES = [
    ("post", f"/api/federation/access-requests/{'s' * 22}/decline", None),
    ("post", OWNER_NOTES, {"client_key": KEY, "text": "hi"}),
    ("post", f"{OWNER_NOTES}/read", {"seq": 1}),
    ("patch", f"{OWNER_NOTES}/{'n' * 22}", {"text": "hi"}),
    ("delete", f"{OWNER_NOTES}/{'n' * 22}", None),
    ("post", f"{OWNER_NOTES}/{'n' * 22}/hide", None),
    ("post", f"{REMOTE}/access", {"level": "send"}),
    ("post", f"{REMOTE}/access/withdraw", None),
    ("post", f"{REMOTE}/decisions",
     {"request_id": "r", "offer": "o" * 32, "choice": "approve", "client_key": KEY}),
    ("post", f"{REMOTE}/notes", {"client_key": KEY, "text": "hi"}),
    ("post", f"{REMOTE}/notes/read", {"seq": 1}),
    ("patch", f"{REMOTE}/notes/{'n' * 22}", {"text": "hi"}),
    ("delete", f"{REMOTE}/notes/{'n' * 22}", None),
    ("post", f"{REMOTE}/continue", {"target_engine": "claude", "mode": "quick"}),
    ("post", f"{REMOTE}/continue/{'h' * 32}/create", {"cwd": "/tmp"}),
    # the operator's own decide route, which now records who settled a request
    ("post", "/api/structured/sessions/codex-api:x/decisions",
     {"decision_id": KEY, "turn_id": KEY, "request_id": "r", "decision": "approve"}),
]  # fmt: skip
NEW_READS = [
    "/api/federation/access-requests",
    OWNER_NOTES,
    f"{REMOTE}/access",
    f"{REMOTE}/notes",
    f"{REMOTE}/continue/{'h' * 32}",
]


def _request(c, method, url, body, headers):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return getattr(c, method)(url, **kwargs)


def test_every_new_route_needs_a_session_and_every_new_write_csrf_and_origin(
    auth_cfg, monkeypatch, decisions
):
    c = _client(auth_cfg)
    called: list = []

    async def never(*args, **kwargs):
        called.append(args)
        raise AssertionError("reached without authorization")

    def never_sync(*args, **kwargs):
        called.append(args)
        raise AssertionError("reached without authorization")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(remote, "read", never)
        mp.setattr(continue_here, "prepare", never)
        mp.setattr(continue_here, "create", never)
        mp.setattr(continue_here, "preview", never_sync)
        mp.setattr(together, "decline_access", never_sync)
        mp.setattr(together, "access_listing", never_sync)
        for name in ("owner_thread", "owner_add", "owner_edit", "owner_delete", "owner_hide",
                     "owner_read"):  # fmt: skip
            mp.setattr(notes, name, never_sync)
        for url in NEW_READS:
            assert c.get(url).status_code == 401, url
        for method, url, body in NEW_WRITES:
            r = _request(c, method, url, body, {"Origin": auth_cfg.origin})
            assert r.status_code == 401, url
            r = _request(c, method, url, body, {"Origin": auth_cfg.origin, "X-CSRF-Token": "x"})
            assert r.status_code == 401, url  # a CSRF token without a session buys nothing
        hdr = _login(c, auth_cfg)
        for method, url, body in NEW_WRITES:
            no_csrf = _request(c, method, url, body, {"Origin": auth_cfg.origin})
            assert no_csrf.status_code == 403, url
            wrong = _request(c, method, url, body, {**hdr, "X-CSRF-Token": "0" * 43})
            assert wrong.status_code == 403, url
            evil = _request(c, method, url, body, {**hdr, "Origin": "https://evil.example"})
            assert evil.status_code == 403, url
            no_origin = _request(c, method, url, body, {"X-CSRF-Token": hdr["X-CSRF-Token"]})
            assert no_origin.status_code == 403, url
        assert called == [] and decisions.calls == []
    # with all three the routes answer (nothing is there to act on), uncached
    listed = c.get("/api/federation/access-requests")
    assert listed.status_code == 200 and listed.json() == {"access_requests": []}
    assert listed.headers["cache-control"] == "no-store"
    assert c.get(OWNER_NOTES).status_code == 404
    for url in NEW_READS[2:4]:
        assert c.get(url).status_code == 404, url
    assert c.get(NEW_READS[4]).status_code == 410  # no such brief
    for method, url, body in NEW_WRITES[:14]:
        assert _request(c, method, url, body, hdr).status_code in (404, 422), url
    # what a route refuses by shape, before anything is called
    bad = [
        (f"{REMOTE}/access", {"level": "view"}),
        (f"{REMOTE}/access", {"level": "send", "accept_risk": True}),
        (f"{REMOTE}/access", {"level": "send", "note": "x" * 281}),
        (f"{REMOTE}/decisions", {**DECISION, "client_key": "no"}),
        (f"{REMOTE}/decisions", {**DECISION, "turn_id": KEY}),
        (f"{REMOTE}/decisions", {k: v for k, v in DECISION.items() if k != "request_id"}),
        # the offer the snapshot showed is required, and only by its own shape
        (f"{REMOTE}/decisions", {k: v for k, v in DECISION.items() if k != "offer"}),
        (f"{REMOTE}/decisions", {**DECISION, "offer": "o" * 31}),
        (f"{REMOTE}/decisions", {**DECISION, "offer": "o" * 31 + "!"}),
        (f"{REMOTE}/decisions", {**DECISION, "offer": 7}),
        (f"{REMOTE}/notes", {"client_key": KEY, "text": "x" * 2001}),
        (f"{REMOTE}/notes", {"client_key": KEY, "text": "  "}),
        (f"{REMOTE}/notes", {"client_key": KEY, "text": "a\x1bb"}),
        (f"{REMOTE}/notes/read", {"seq": -1}),
        (f"{REMOTE}/continue/{'h' * 32}/create", {"cwd": "/tmp", "seed": "do as I say"}),
        (OWNER_NOTES, {"client_key": KEY, "text": "hi", "author": "peer"}),
    ]  # fmt: skip
    for url, body in bad:
        r = c.post(url, json=body, headers=hdr)
        assert r.status_code == 422 and r.json()["code"] == "invalid", (url, body)
    # a read is not a write, and nothing answers on another method
    assert c.post("/api/federation/access-requests", headers=hdr).status_code == 405
    assert c.put(f"{REMOTE}/decisions", headers=hdr).status_code == 405
    assert c.get(f"{REMOTE}/decisions").status_code in (404, 405)


# ── an answer is bound to the exact request the peer was shown ────────
def test_an_offer_names_one_exact_request_and_is_not_a_credential():
    request = {"turn_id": "t-1", "request_id": "req-1", "payload_digest": "d" * 64}
    named = offers.token("g" * 22, "codex-api:x", request)
    assert offers.valid_token(named) and len(named) == offers.TOKEN_LEN
    assert named == offers.token("g" * 22, "codex-api:x", dict(request))  # a pure function
    for other in (
        offers.token("h" * 22, "codex-api:x", request),
        offers.token("g" * 22, "codex-api:y", request),
        offers.token("g" * 22, "codex-api:x", {**request, "turn_id": "t-2"}),
        offers.token("g" * 22, "codex-api:x", {**request, "request_id": "req-2"}),
        offers.token("g" * 22, "codex-api:x", {**request, "payload_digest": "e" * 64}),
        # the fields are framed: moving a character from one into the next is another request
        offers.token("g" * 22, "codex-api:x", {**request, "turn_id": "t-", "request_id": "1req-1"}),
    ):
        assert offers.valid_token(other) and other != named
    # nothing names a request without a turn or an id: no token, and so nothing is offered
    assert offers.token("g" * 22, "codex-api:x", {"request_id": "req-1"}) is None
    assert offers.token("g" * 22, "codex-api:x", {"turn_id": "t-1"}) is None
    view = {"pending_requests": [{"request_id": "req-1"}]}
    raw = {"pending_requests": [{"request_id": "req-1", "choices": ["approve", "reject"]}]}
    offers.offer(view, raw, lambda text: text, "g" * 22, "codex-api:x")
    assert view == {"pending_requests": [{"request_id": "req-1"}]}
    for bad in (None, 7, "", "o" * 31, "o" * 33, "o" * 31 + "/", "o" * 31 + "\n", b"o" * 32):
        assert not offers.valid_token(bad)


@pytest.mark.anyio
async def test_an_answer_for_an_earlier_turns_request_of_the_same_id_is_not_pending(
    owner, decisions
):
    """Native request ids repeat from turn to turn. The answer a peer gave to turn 1's `req-1`
    arrives after the owner settled it and turn 2 reached its own `req-1`: it is refused, and
    nothing of it is recorded or sent."""
    made = owner.grant("send")
    first = decisions.ask(made.session_key)
    (shown,) = (await owner.call("session.snapshot", ref(made)))["pending_requests"]
    saved = answer(made, shown["request_id"], "approve", offer=shown["offer"])  # in flight
    await decisions.owner_decides(owner.fed, made.session_key, first, "reject")
    decisions.turn = str(uuid.uuid4())
    second = decisions.ask(made.session_key)  # the SAME id and payload, another turn
    assert (second["request_id"], second["payload_digest"]) == (
        first["request_id"],
        first["payload_digest"],
    )
    assert await owner.refused("decision.answer", saved) == "not_pending"
    assert len(decisions.calls) == 1 and len(decisions.effects) == 1  # only the owner's reject
    assert decisions.requests[made.session_key] == [second]  # still pending, untouched
    assert owner.st.get_peer_decision(made.grant_id, saved["client_key"]) is None
    assert owner.st.open_peer_decisions() == [] and peer_ledger() == []
    assert [a["author"] for a in authors(owner.st, made.session_key)] == ["owner"]
    # the request that IS pending is answered with the offer it is shown under now
    (now,) = (await owner.call("session.snapshot", ref(made)))["pending_requests"]
    assert now["offer"] != shown["offer"]
    out = await owner.call("decision.answer", answer(made, "req-1", offer=now["offer"]))
    assert out["outcome"] == "accepted" and decisions.effects[-1]["turn_id"] == decisions.turn


@pytest.mark.anyio
async def test_an_answer_for_a_request_replaced_inside_its_turn_is_not_pending(owner, decisions):
    """A reconnect inside one turn: the same id and turn, another payload. It is another
    request, whatever its id says."""
    made = owner.grant("send")
    decisions.ask(made.session_key, digest="a" * 64)
    (shown,) = (await owner.call("session.snapshot", ref(made)))["pending_requests"]
    saved = answer(made, "req-1", "approve", offer=shown["offer"])
    decisions.requests[made.session_key].clear()
    replaced = decisions.ask(made.session_key, digest="b" * 64)
    assert await owner.refused("decision.answer", saved) == "not_pending"
    assert decisions.calls == [] and decisions.requests[made.session_key] == [replaced]
    assert owner.st.get_peer_decision(made.grant_id, saved["client_key"]) is None
    assert owner.st.open_peer_decisions() == [] and authors(owner.st, made.session_key) == []


@pytest.mark.anyio
async def test_a_request_replaced_after_the_row_was_stored_is_refused_by_the_runtime(
    owner, decisions
):
    """The window after the fence's own snapshot: the row is stored for one payload, and by the
    time the runtime is asked the request under that id and turn is another. The stored digest
    travels with the call (`expected_digest`), the runtime refuses, and the retry finds its
    request gone."""
    made = owner.grant("send")
    decisions.ask(made.session_key, digest="a" * 64)
    sent = answer(made)

    def replace(_call) -> None:
        decisions.before_decide = None
        decisions.requests[made.session_key].clear()
        decisions.ask(made.session_key, digest="b" * 64)

    decisions.before_decide = replace
    assert await owner.refused("decision.answer", sent) == "not_pending"
    assert decisions.expected == ["a" * 64] and decisions.effects == []
    (still,) = decisions.requests[made.session_key]
    assert still["payload_digest"] == "b" * 64
    row = owner.st.get_peer_decision(made.grant_id, sent["client_key"])
    assert (row.digest, row.outcome) == ("a" * 64, "not_pending")
    assert await owner.refused("decision.answer", sent) == "not_pending"  # the retry too
    assert len(decisions.calls) == 1 and peer_ledger() == []


@pytest.mark.anyio
@pytest.mark.parametrize("attributed", ["store", "journal"])
async def test_a_late_answer_never_learns_who_settled_another_request_of_the_same_id(
    owner, decisions, attributed
):
    made = owner.grant("send")
    one = decisions.ask(made.session_key, digest="a" * 64)
    seen_one = offers.token(made.grant_id, made.session_key, one)
    await decisions.owner_decides(owner.fed, made.session_key, one)
    # the same id in the same turn, another payload, withdrawn unanswered: nobody settled it
    two = decisions.ask(made.session_key, digest="b" * 64)
    seen_two = offers.token(made.grant_id, made.session_key, two)
    decisions.requests[made.session_key].clear()
    if attributed == "journal":  # the owner's attribution was never written
        con = sqlite3.connect(owner.st.db_path)
        con.execute("DELETE FROM decision_authors")
        con.commit()
        con.close()
    assert await owner.refused("decision.answer", answer(made, offer=seen_two)) == "not_pending"
    out = await owner.call("decision.answer", answer(made, offer=seen_one))
    assert out == {"outcome": "settled", "author": {"via": "owner", "name": None, "mine": False}}
    # …nor that of a request of another turn, nor under an offer of its own making
    decisions.turn = str(uuid.uuid4())
    assert await owner.refused("decision.answer", answer(made, offer=seen_one)) == "not_pending"
    assert await owner.refused("decision.answer", answer(made, offer="o" * 32)) == "not_pending"
    assert len(decisions.calls) == 1 and owner.st.open_peer_decisions() == []
    assert len(authors(owner.st, made.session_key)) == 1


@pytest.mark.anyio
async def test_a_late_answer_learns_nothing_when_the_journal_cannot_be_read(
    owner, decisions, monkeypatch
):
    made = owner.grant("send")
    one = decisions.ask(made.session_key)
    await decisions.owner_decides(owner.fed, made.session_key, one)
    con = sqlite3.connect(owner.st.db_path)
    con.execute("DELETE FROM decision_authors")
    con.commit()
    con.close()

    async def unreadable(*_a, **_kw):
        raise runtime.StructuredError(503, "the journal cannot be read")

    monkeypatch.setattr(runtime, "decision_record", unreadable)
    assert await owner.refused("decision.answer", answer(made)) == "not_pending"
    assert authors(owner.st, made.session_key) == []  # nothing guessed, nothing written


@pytest.mark.anyio
async def test_a_retry_under_the_same_key_with_another_offer_is_a_conflict(owner, decisions):
    made = owner.grant("send")
    decisions.ask(made.session_key)
    sent = answer(made)
    decisions.crash_before = True  # the row is stored, the runtime never took the decision
    with pytest.raises(RuntimeError):
        await owner.call("decision.answer", sent)
    row = owner.st.get_peer_decision(made.grant_id, sent["client_key"])
    other_turn = offers.token(
        made.grant_id,
        made.session_key,
        {"turn_id": str(uuid.uuid4()), "request_id": "req-1", "payload_digest": row.digest},
    )
    other_payload = offers.token(
        made.grant_id,
        made.session_key,
        {"turn_id": row.turn_id, "request_id": "req-1", "payload_digest": "e" * 64},
    )
    for offer in (other_turn, other_payload, "o" * 32):
        assert await owner.refused("decision.answer", {**sent, "offer": offer}) == "conflict"
    assert len(decisions.calls) == 1 and decisions.effects == []  # only the crashed attempt
    assert owner.st.get_peer_decision(made.grant_id, sent["client_key"]) == row  # unchanged
    out = await owner.call("decision.answer", sent)  # the original still replays
    assert out["outcome"] == "accepted" and len(decisions.effects) == 1
    assert await owner.refused("decision.answer", {**sent, "offer": other_turn}) == "conflict"
    assert len(decisions.effects) == 1 and len(peer_ledger()) == 1


def test_the_receiver_takes_an_offer_by_shape_and_the_route_sends_it_back_unchanged(
    auth_cfg, monkeypatch
):
    request = {"request_id": "req-1", "turn_id": str(uuid.uuid4()), "choices": ["approve"]}
    good = snapshot_with({**request, "offer": "o" * 32})
    assert remote.check_snapshot(good) is good
    assert remote.check_snapshot(snapshot_with(request))  # a view grant is shown none
    for bad in (7, "", "o" * 31, "o" * 33, "o" * 31 + " ", ["o" * 32], {"offer": "o" * 32}):
        with pytest.raises(remote.Invalid):
            remote.check_snapshot(snapshot_with({**request, "offer": bad}))
    # the receiving route forwards it as it came, with the request id it belongs to
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    answers = {"decision.answer": {"outcome": "accepted", "author": {"via": "peer", "name": "R"}}}
    base, asked, _background, _stale = _mirrored(c, monkeypatch, answers, [])
    body = {"request_id": "req-1", "offer": "aB3_-" + "o" * 27, "choice": "approve",
            "client_key": str(uuid.uuid4())}  # fmt: skip
    r = c.post(f"{base}/decisions", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    ((verb, sent),) = asked
    assert verb == "decision.answer" and sent["offer"] == body["offer"]
    assert sent["request_id"] == "req-1" and sent["revision"] == 4
    assert verbs.PEER["decision.answer"].validate(dict(sent))["offer"] == body["offer"]


# ── the real runtime ──────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_on_the_real_runtime_a_peer_allows_once_and_cannot_widen(
    host, project, live, held, monkeypatch, source
):
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    o = Owner(service.Federation(static=X25519PrivateKey.generate(), name="owner"))
    made = o.grant("send", key)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="APPROVE")
    request = (await pending(key))["pending_requests"][0]
    assert request["choices"] == ["approve", "reject", "cancel"]

    snap = await o.call("session.snapshot", ref(made))
    (shown,) = snap["pending_requests"]
    assert shown["choices"] == ["approve", "reject"] and shown["turn_id"] == turn
    # the snapshot names the exact request: the token of ITS turn, id and payload digest
    assert shown["offer"] == offers.token(made.grant_id, key, request)
    assert "payload_digest" not in shown
    for widening in ("always", "cancel"):
        assert await o.refused(
            "decision.answer", answer(made, shown["request_id"], widening, offer=shown["offer"])
        ) == ("owner_only")
    sent = answer(made, shown["request_id"], "approve", actor="Robin", offer=shown["offer"])
    out = await o.call("decision.answer", sent)
    assert out == {
        "outcome": "accepted",
        "author": {"via": "peer", "name": "GPU box", "mine": True, "actor": "Robin"},
    }
    _, settled = await settle(key, turn)
    assert settled["reply"] == ("approved:accept" if source == "codex" else "approved:allow")
    row = o.st.get_peer_decision(made.grant_id, sent["client_key"])
    records = await runtime.decision_record(key, turn_id=turn, request_id=request["request_id"])
    assert [(r["decision_id"], r["decision"], r["actor"], r["handoff"]) for r in records] == [
        (row.decision_id, "approve", made.principal, "sent")
    ]
    assert records[0]["digest"] == request["payload_digest"] == row.digest
    # a retry, also after a restart, is answered from the store: one effect
    assert await o.restarted().call("decision.answer", sent) == out
    assert await o.refused("decision.answer", {**sent, "choice": "reject"}) == "conflict"
    late = await o.call(
        "decision.answer", answer(made, shown["request_id"], "reject", offer=shown["offer"])
    )
    assert late["outcome"] == "settled" and late["author"]["name"] == "GPU box"
    records = await runtime.decision_record(key, turn_id=turn, request_id=request["request_id"])
    assert len(records) == 1 and len(peer_ledger()) == 1


@pytest.mark.anyio
async def test_on_the_real_runtime_the_owner_and_a_peer_race_to_one_effect(
    host, project, live, held, monkeypatch
):
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    o = Owner(service.Federation(static=X25519PrivateKey.generate(), name="owner"))
    made = o.grant("send", key)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="APPROVE")
    request = (await pending(key))["pending_requests"][0]

    async def operator() -> str:
        try:
            await runtime.decide(
                key, turn_id=turn, request_id=request["request_id"], decision_id=ident(),
                decision="reject", user="marcus",
            )  # fmt: skip
        except runtime.StructuredError:
            return "lost"
        finally:
            await o.fed.owner_decided(key, turn, request["request_id"])
        return "won"

    sent = answer(
        made, request["request_id"], "approve", offer=offers.token(made.grant_id, key, request)
    )
    mine, theirs = await asyncio.gather(
        operator(), o.call("decision.answer", sent), return_exceptions=True
    )
    assert not isinstance(theirs, Exception), theirs
    assert (mine, theirs["outcome"]) in (("won", "settled"), ("lost", "accepted"))
    records = await runtime.decision_record(key, turn_id=turn, request_id=request["request_id"])
    assert [r["handoff"] for r in records].count("sent") == 1  # ONE effect
    (stored,) = authors(o.st, key)
    assert stored["author"] == ("owner" if mine == "won" else made.principal)
    assert theirs["author"]["via"] == ("owner" if mine == "won" else "peer")
    again = await o.call("decision.answer", sent)
    assert again == theirs  # the same stored author for a retry
    _, settled = await settle(key, turn)
    assert settled["reply"]


@pytest.mark.anyio
async def test_on_the_real_runtime_both_crash_windows_recover_without_a_second_effect(
    host, project, live, held, monkeypatch
):
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    o = Owner(service.Federation(static=X25519PrivateKey.generate(), name="owner"))
    made = o.grant("send", key)
    real_decide = runtime.decide

    async def died_before(*args, **kwargs):
        raise RuntimeError("the process died before the runtime took the decision")

    async def died_after(*args, **kwargs):
        await real_decide(*args, **kwargs)
        raise RuntimeError("the process died after the runtime accepted the decision")

    # ── window 1: the row is stored, the runtime was never reached ──
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="APPROVE")
    request = (await pending(key))["pending_requests"][0]
    sent = answer(made, request["request_id"], offer=offers.token(made.grant_id, key, request))
    monkeypatch.setattr(runtime, "decide", died_before)
    with pytest.raises(RuntimeError):
        await o.call("decision.answer", sent)
    row = o.st.get_peer_decision(made.grant_id, sent["client_key"])
    assert row.outcome is None
    assert await runtime.decision_record(key, turn_id=first, request_id=row.request_id) == []
    monkeypatch.setattr(runtime, "decide", real_decide)
    out = await o.restarted().call("decision.answer", sent)
    assert out["outcome"] == "accepted" and out["author"]["mine"] is True
    records = await runtime.decision_record(key, turn_id=first, request_id=row.request_id)
    assert [r["decision_id"] for r in records] == [row.decision_id]  # the stored identity
    await settle(key, first)

    # ── window 2: the runtime accepted, the author was never recorded ──
    second = ident()
    await runtime.submit_turn(key, operation_id=second, text="APPROVE")
    request = (await pending(key))["pending_requests"][0]
    sent = answer(made, request["request_id"], offer=offers.token(made.grant_id, key, request))
    monkeypatch.setattr(runtime, "decide", died_after)
    with pytest.raises(RuntimeError):
        await o.call("decision.answer", sent)
    row = o.st.get_peer_decision(made.grant_id, sent["client_key"])
    assert row.outcome is None and len(authors(o.st, key)) == 1  # only the first turn's
    calls: list = []

    async def counted(*args, **kwargs):
        calls.append(kwargs)
        return await real_decide(*args, **kwargs)

    monkeypatch.setattr(runtime, "decide", counted)
    for _ in range(400):  # the request leaves the snapshot once the journal folded the decision
        if not (await runtime.snapshot(key))["pending_requests"]:
            break
        await asyncio.sleep(0.05)
    out = await o.restarted().call("decision.answer", sent)
    assert out["outcome"] == "accepted" and out["author"]["mine"] is True
    assert calls == []  # recovered from the journal: the runtime was not called again
    records = await runtime.decision_record(key, turn_id=second, request_id=row.request_id)
    assert [(r["decision_id"], r["handoff"]) for r in records] == [(row.decision_id, "sent")]
    assert len(authors(o.st, key)) == 2 and len(peer_ledger()) == 2
    await settle(key, second)


@pytest.mark.anyio
async def test_on_the_real_runtime_a_saved_answer_never_reaches_the_next_turns_request(
    host, project, live, held, monkeypatch
):
    """The reviewer's reproduction (#2184): turn 1's request is shown, the owner rejects it,
    turn 2 reaches a request with the SAME native id, and the peer's saved approval of turn 1
    arrives. Turn 2's journal must hold no decision."""
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    o = Owner(service.Federation(static=X25519PrivateKey.generate(), name="owner"))
    made = o.grant("send", key)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="APPROVE")
    await pending(key)
    (shown,) = (await o.call("session.snapshot", ref(made)))["pending_requests"]
    saved = answer(made, shown["request_id"], "approve", actor="Robin", offer=shown["offer"])
    await runtime.decide(
        key, turn_id=first, request_id=shown["request_id"], decision_id=ident(),
        decision="reject", user="marcus",
    )  # fmt: skip
    await o.fed.owner_decided(key, first, shown["request_id"])
    await settle(key, first)

    second = ident()
    await runtime.submit_turn(key, operation_id=second, text="APPROVE")
    request = (await pending(key))["pending_requests"][0]
    assert request["request_id"] == shown["request_id"] and request["turn_id"] == second
    assert await o.refused("decision.answer", saved) == "not_pending"
    assert (
        await runtime.decision_record(key, turn_id=second, request_id=request["request_id"]) == []
    )
    still = (await runtime.snapshot(key))["pending_requests"]
    assert [q["request_id"] for q in still] == [request["request_id"]]  # nothing was sent
    assert o.st.get_peer_decision(made.grant_id, saved["client_key"]) is None
    assert o.st.open_peer_decisions() == [] and peer_ledger() == []
    assert [a["author"] for a in authors(o.st, key)] == ["owner"]  # turn 1's, and only that
    # the owner still decides turn 2 itself
    await runtime.decide(
        key, turn_id=second, request_id=request["request_id"], decision_id=ident(),
        decision="reject", user="marcus",
    )  # fmt: skip
    records = await runtime.decision_record(key, turn_id=second, request_id=request["request_id"])
    assert [(r["decision"], r["actor"]) for r in records] == [("reject", "marcus")]
    await settle(key, second)


# ── two real app instances ────────────────────────────────────────────


def test_two_real_instances_request_access_grant_it_and_answer_allow_once(
    two_instances, host, project, monkeypatch
):
    a, b = two_instances
    a_id, b_id = _pair_instances(a, b)
    monkeypatch.setattr(shares, "session_state", lambda prov, native: "live")
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    loop = a.server.servers[0].get_loop()

    def on_a(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(60)

    key = on_a(runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    native = key.partition(":")[2]
    monkeypatch.setattr(
        sharing,
        "_session_rows",
        lambda _engine: {
            native: SimpleNamespace(uuid=native, archived=False, first_user_message="")
        },
    )

    def mirrored(level: str):
        rows = b.http.get("/api/federation/remote", headers=b.read).json()["remote"]
        return next((r for r in rows if r["level"] == level), None)

    made = a.post("/grants", {"session_key": key, "peer_id": b_id})
    assert made.status_code == 201, made.text
    grant = made.json()["grant"]
    assert _wait(lambda: mirrored("view"))["revision"] == 1
    base = f"/api/federation/remote/{a_id}/{grant['grant_id']}"
    share_row = notices.remote_key("share", a_id, grant["grant_id"])
    _wait(lambda: [r for r in bell() if r["action_id"] == share_row])  # "shared with you"

    # ── presence, by name only ──
    snap = b.http.get(f"{base}/snapshot", headers=b.read).json()
    assert snap["owner_present"] is False and snap["pending_requests"] == []
    _wait(lambda: not [r for r in bell() if r["action_id"] == share_row])  # read
    (row,) = a.http.get(f"/api/federation/grants?session={key}", headers=a.read).json()["grants"]
    assert row["watching"] is True
    a.http.get(f"/api/structured/sessions/{key}", headers=a.read)  # the operator opens the pane
    assert b.http.get(f"{base}/snapshot", headers=b.read).json()["owner_present"] is True

    # ── a view peer cannot answer; it asks for more, without leaving BattleLab ──
    turn = ident()
    on_a(runtime.submit_turn(key, operation_id=turn, text="APPROVE"))
    request = on_a(pending(key))["pending_requests"][0]
    # a view grant is shown no offer; a well-formed one of its own making changes nothing
    assert (
        "offer" not in b.http.get(f"{base}/snapshot", headers=b.read).json()["pending_requests"][0]
    )
    decision = {"request_id": request["request_id"], "offer": "A" * 32, "choice": "approve",
                "client_key": str(uuid.uuid4()), "actor": "Robin"}  # fmt: skip
    refused = b.http.post(f"{base}/decisions", json=decision, headers=b.write)
    assert refused.status_code == 404 and refused.json()["code"] == "no_grant"
    assert b.http.post(f"{base}/access", json={"level": "send"}).status_code == 401
    asked = b.http.post(f"{base}/access", json={"level": "send", "note": NOTE}, headers=b.write)
    assert asked.status_code == 201, asked.text
    request_id = asked.json()["request_id"]
    assert b.http.post(f"{base}/access", json={"level": "send"}, headers=b.write).json() == (
        asked.json()
    )
    rows = a.http.get(f"/api/federation/access-requests?session={key}", headers=a.read).json()
    (seen,) = rows["access_requests"]
    assert (seen["note"], seen["state"], seen["peer_id"]) == (NOTE, "pending", b_id)
    access_row = notices.access_key(request_id)
    note = _wait(lambda: [r for r in bell() if r["action_id"] == access_row])[0]
    assert NOTE not in json.dumps(note)
    assert a.fed.store.get_grant(grant["grant_id"]).level == "view"  # nothing changed by itself

    # ── granting is the operator's grant change, through the dialog's route ──
    url = f"/api/federation/grants/{grant['grant_id']}"
    risky = a.http.patch(url, json={"level": "send", "revision": 1}, headers=a.write)
    assert risky.status_code == 422 and risky.json()["code"] == "risk"
    raised = a.http.patch(
        url, json={"level": "send", "revision": 1, "accept_risk": True}, headers=a.write
    )
    assert raised.status_code == 200, raised.text
    assert _wait(lambda: mirrored("send"))["revision"] == 2
    status = b.http.get(f"{base}/access", headers=b.read).json()
    assert status["level"] == "send" and status["request"]["state"] == "granted"
    _wait(lambda: not [r for r in bell() if r["action_id"] == access_row])  # retracted: granted

    # ── the peer answers "allow once"; the widening choice stays the owner's ──
    snap = b.http.get(f"{base}/snapshot", headers=b.read).json()
    (shown,) = snap["pending_requests"]
    assert shown["choices"] == ["approve", "reject"]
    assert shown["offer"] == offers.token(grant["grant_id"], key, request)
    decision["offer"] = shown["offer"]  # what a browser sends back, unchanged
    decision_row = notices.remote_key("decision", a_id, grant["grant_id"])
    _wait(lambda: [r for r in bell() if r["action_id"] == decision_row])  # the owner hinted
    widen = b.http.post(f"{base}/decisions", json={**decision, "choice": "always"},
                        headers=b.write)  # fmt: skip
    assert widen.status_code == 403 and widen.json()["code"] == "owner_only"
    done = b.http.post(f"{base}/decisions", json=decision, headers=b.write, timeout=60)
    assert done.status_code == 200, done.text
    assert done.json() == {
        "outcome": "accepted",
        "author": {"via": "peer", "name": a.state()["peers"][0]["name"], "mine": True,
                   "actor": "Robin"},
    }  # fmt: skip
    again = b.http.post(f"{base}/decisions", json=decision, headers=b.write, timeout=60)
    assert again.json() == done.json()  # the retry: the same decision, no second effect
    _, settled = on_a(settle(key, turn))
    assert settled["reply"] == "approved:accept"
    records = on_a(runtime.decision_record(key, turn_id=turn, request_id=request["request_id"]))
    assert [r["handoff"] for r in records] == ["sent"]
    # every party reads the same stored author
    pane = a.http.get(f"/api/structured/sessions/{key}", headers=a.read).json()["decisions"]
    theirs = b.http.get(f"{base}/snapshot", headers=b.read).json()["decisions"]
    assert (
        [d["author"] for d in pane]
        == [d["author"] for d in theirs]
        == [{"via": "peer", "name": a.state()["peers"][0]["name"]}]
    )
    assert theirs[0]["mine"] is True and pane[0]["mine"] is False
    _wait(lambda: not [r for r in bell() if r["action_id"] == decision_row])  # settled

    # ── revoke ends it ──
    assert a.post(f"/grants/{grant['grant_id']}/revoke").status_code == 200
    late = {**decision, "client_key": str(uuid.uuid4())}
    for path, body in (("decisions", late), ("access", {"level": "send"})):
        r = b.http.post(f"{base}/{path}", json=body, headers=b.write)
        assert r.status_code == 404 and r.json()["code"] == "no_grant", path
    assert b.http.get(f"{base}/access", headers=b.read).status_code == 404


NOTE_ANSWER = {
    "note_id": "n" * 22, "author": "peer", "mine": True, "actor": "", "text": "hello",
    "created_at": 1.0, "edited_at": None, "deleted": False,
}  # fmt: skip
#: Every receiving route of working together (#2059), with what it is. A WRITE changes something
#: on the owner (a request, an answer, a note, a read mark) and is sent once; a READ is
#: re-resolved once. Continue here is pinned in `test_federation_continue.py`.
TOGETHER_ROUTES = (
    ("POST", "access", {"level": "send"}, "access.request", "write"),
    ("GET", "access", None, "access.status", "read"),
    ("POST", "access/withdraw", None, "access.withdraw", "write"),
    (
        "POST",
        "decisions",
        {
            "request_id": "req-1",
            "offer": "o" * 32,
            "choice": "approve",
            "client_key": str(uuid.uuid4()),
        },
        "decision.answer",
        "write",
    ),
    ("GET", "notes", None, "note.list", "read"),
    ("POST", "notes", {"client_key": str(uuid.uuid4()), "text": "hello"}, "note.add", "write"),
    ("PATCH", f"notes/{'n' * 22}", {"text": "edited"}, "note.edit", "write"),
    ("DELETE", f"notes/{'n' * 22}", None, "note.delete", "write"),
    ("POST", "notes/read", {"seq": 1}, "note.read", "write"),
)


def test_every_working_together_write_is_sent_once_and_every_read_is_re_resolved_once(
    auth_cfg, monkeypatch
):
    """The suggest/send rule (#2055, `test_federation_inputs`) for every receiving route of
    #2059: a refused WRITE is `404` at once after exactly one call under the mirrored revision
    (the mirror refreshed in the background, the bell left to that refresh), and a refused READ
    is re-resolved once: refreshed inline, called once more under the refreshed revision, and
    `404` only when that second call is refused too."""
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    answers = {
        "access.request": {"request_id": "r" * 22, "level": "send", "state": "pending"},
        "access.status": {"level": "view", "request": None},
        "access.withdraw": {"level": "view", "request": None},
        "decision.answer": {"outcome": "accepted", "author": {"via": "peer", "name": "Robin"}},
        "note.list": {
            "reset": False, "seq": 0, "changes": [], "more": False, "next": None, "unread": 0,
        },
        "note.add": {"note": NOTE_ANSWER, "seq": 1},
        "note.edit": {"note": NOTE_ANSWER},
        "note.delete": {"note": NOTE_ANSWER},
        "note.read": {"unread": 0},
    }  # fmt: skip
    refused: list[str] = []
    base, asked, background, back_to_stale = _mirrored(c, monkeypatch, answers, refused)
    peer_id = base.split("/")[4]
    finals: list[tuple[str, str]] = []
    monkeypatch.setattr(
        c.app.state.federation, "refused_remote", lambda p, g: finals.append((p, g))
    )
    assert {verb for *_, verb, _kind in TOGETHER_ROUTES} == set(answers)

    def send(method: str, path: str, body):
        kwargs = {"headers": hdr} if method != "GET" else {}
        if body is not None:
            kwargs["json"] = body
        return c.request(method, f"{base}/{path}", **kwargs)

    for method, path, body, verb, kind in TOGETHER_ROUTES:
        assert back_to_stale()
        asked.clear()
        background.clear()
        finals.clear()
        refused[:] = [verb]
        r = send(method, path, body)
        if kind == "write":
            assert r.status_code == 404 and r.json()["code"] == "no_grant", verb
            assert [v for v, _ in asked] == [verb], verb  # once, no inline refresh
            assert asked[0][1]["revision"] == 4, verb  # the mirrored revision, never a newer one
            assert background == [peer_id], verb
            assert finals == [], verb  # one refusal of a write retracts nothing by itself
        else:
            assert r.status_code == 200 and r.json() == answers[verb], (verb, r.text)
            assert [(v, a.get("revision")) for v, a in asked] == [
                (verb, 4), ("refresh", None), (verb, 5),
            ], verb  # fmt: skip
            assert background == [] and finals == [], verb
            # refused again under the refreshed revision: final, 404, and no third call
            assert back_to_stale()
            asked.clear()
            refused[:] = [verb, verb]
            r = send(method, path, body)
            assert r.status_code == 404 and r.json()["code"] == "no_grant", verb
            assert [v for v, _ in asked] == [verb, "refresh", verb], verb
            assert finals == [(peer_id, "g" * 22)], verb
        assert refused == [], verb
