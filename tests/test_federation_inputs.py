"""Input from a peer (#2057): suggest and send, on the owner and on the receiving side.

`owner` is one real `service.Federation` over the test's own store. A verb is called the way the
channel calls it (the table's validator, then its handler, with the authenticated peer id as the
context), so a test is about the production validators and handlers. `submits` stands in for
`structured_runtime.submit_turn` only: it records every call and answers a repeat of an operation
id from its own journal, as the runtime does. The tests at the end use the real native runtime
and two real app instances.

The fence, the races in both orders and the two-process tests are in `test_federation_fence.py`.
"""

# ruff: noqa: F811 — fixtures imported from the other federation test modules
from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

import agent_sessions
from agent_sessions import (
    native_journal,
    notifications,
    orchestrator_ledger,
    prefs,
    review,
    share_api,
    shares,
    webpush,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.federation import channel as ch
from agent_sessions.federation import (
    fence,
    grants,
    inputs,
    noise,
    notices,
    remote,
    service,
    sharing,
    store,
    verbs,
)
from agent_sessions.routes import public_api
from federation_helpers import until
from test_federation_grants import api_key, live, trusted  # noqa: F401 — `live` is a fixture
from test_federation_routes import Instance, _client, _login, _wait, two_instances  # noqa: F401
from test_federation_sharing import _pair_instances
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401 — fixtures
from test_share_api import _session, _visitor, env  # noqa: F401 — fixtures

SRC = Path(agent_sessions.__file__).parent
TEXT = "PLANTED-SUGGESTION-TEXT please run the deploy"
ACTOR = "Robin-PLANTED-ACTOR"
TITLE = "The session's title"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Submits:
    """`structured_runtime.submit_turn`, scripted. `calls` is every call; `delivered` is what a
    runtime would have run: a repeat of an operation id is answered from `journal`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.delivered: list[tuple[str, str, str]] = []
        self.journal: dict[str, str] = {}
        self.result: dict = {"state": "running", "handoff": "sent"}
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.finished = 0

    async def submit_turn(self, key, *, operation_id, text, expected_revision=None, **_kw):
        self.calls.append((key, operation_id, text))
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            if operation_id not in self.journal:
                self.journal[operation_id] = text
                self.delivered.append((key, operation_id, text))
            elif self.journal[operation_id] != text:
                raise runtime.StructuredError(409, "operation id already used")
            return {"session_key": key, "operation_id": operation_id, **self.result}
        finally:
            self.finished += 1


@pytest.fixture
def submits(monkeypatch):
    fake = Submits()
    monkeypatch.setattr(runtime, "submit_turn", fake.submit_turn)
    return fake


@pytest.fixture
def held(monkeypatch):
    """The session keys a mission holds."""
    keys: set[str] = set()
    monkeypatch.setattr(
        grants.missions, "session_mission", lambda key, **_kw: "msn_x" if key in keys else None
    )
    return keys


class Owner:
    def __init__(self, fed: service.Federation) -> None:
        self.fed, self.st = fed, fed.store
        self.peer = trusted(self.st, "GPU box")

    def grant(self, level: str = "send", key: str | None = None, peer=None, **kw) -> store.Grant:
        return grants.create(
            self.st,
            session_key=key or api_key(),
            peer_id=(peer or self.peer).peer_id,
            level=level,
            expires_in=kw.get("expires_in"),
            accept_risk=level == "send",
        )

    async def call(self, verb: str, args: dict, peer=None) -> dict:
        """As the channel does: the table's validator, then its handler."""
        spec = verbs.PEER[verb]
        clean = spec.validate(dict(args))
        context = SimpleNamespace(context=(self.fed, (peer or self.peer).peer_id))
        return await spec.handle(context, clean)

    async def refused(self, verb: str, args: dict, peer=None) -> str:
        with pytest.raises(ch.VerbError) as exc:
            await self.call(verb, args, peer)
        return exc.value.code

    async def quiet(self) -> None:
        """Let the background work (the bell row, a retraction) finish.

        The `sleep(0)` is load-bearing. A task leaves `fed._tasks` in its done-callback, which
        the loop runs one turn AFTER the task finished. `gather` over tasks that are all done
        returns a finished future, and awaiting that does not yield: without a real yield this
        loop would spin for ever on a task that is done and not yet discarded (it did, for an
        hour, on a loaded CI runner)."""
        while self.fed._tasks:
            await asyncio.gather(*list(self.fed._tasks), return_exceptions=True)
            await asyncio.sleep(0)

    def suggestion(self, suggestion_id: str) -> store.Suggestion:
        return self.st.get_suggestion(suggestion_id)

    def restarted(self) -> Owner:
        """The same store under a NEW service and store object: a restart of the owner."""
        again = Owner.__new__(Owner)
        again.fed = service.Federation(static=self.fed._static, name="owner")
        again.st, again.peer = again.fed.store, self.peer
        return again


@pytest.fixture
def owner(live, held, submits, monkeypatch):
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    return Owner(service.Federation(static=X25519PrivateKey.generate(), name="owner"))


def msg(grant: store.Grant, text: str = TEXT, **extra) -> dict:
    return {
        "grant_id": grant.grant_id,
        "revision": grant.revision,
        "client_key": extra.pop("client_key", None) or str(uuid.uuid4()),
        "text": text,
        **extra,
    }


def ref(grant: store.Grant, **extra) -> dict:
    return {"grant_id": grant.grant_id, "revision": grant.revision, **extra}


def expire(st: store.Store, grant_id: str) -> None:
    con = sqlite3.connect(st.db_path)
    con.execute("UPDATE grants SET expires_at = ? WHERE grant_id = ?", (time.time() - 1, grant_id))
    con.commit()
    con.close()


def bell() -> list[dict]:
    return notifications._read(notifications._notifications_path())


def ledger_rows() -> list[dict]:
    return [r for r in orchestrator_ledger.read_all() if r.get("source") == "peer"]


def refusal(fn, *args, **kwargs) -> grants.GrantError:
    with pytest.raises(grants.GrantError) as exc:
        fn(*args, **kwargs)
    return exc.value


# ── the store: schema 4 ───────────────────────────────────────────────


def _old_store(directory, version: int) -> bytes:
    """A store exactly as an earlier build left it at ``version`` (2: #2054, 3: #2056's grant
    kind), with one trusted peer and one live grant: an API grant, and at schema 3 a terminal
    grant as well."""
    directory.mkdir()
    con = sqlite3.connect(directory / store.DB_FILE)
    steps = (store._SCHEMA_V1, store._SCHEMA_V2, store._SCHEMA_V3)[:version]
    for stmt in (s for step in steps for s in step):
        con.execute(stmt)
    key = noise.public_bytes(X25519PrivateKey.generate())
    con.execute(
        "INSERT INTO peers (peer_id, public_key, name, state, endpoints, revision, created_at,"
        " updated_at) VALUES (?, ?, 'old box', 'trusted', '[]', 1, 1.0, 1.0)",
        ("a" * 32, key),
    )
    con.execute(
        "INSERT INTO grants (grant_id, session_key, principal, level, expires_at, revision, state,"
        " created_at) VALUES ('g' || ?, 'codex-api:x', ?, 'view', ?, 4, 'live', 1.0)",
        ("0" * 21, "peer:" + "a" * 32, time.time() + 3600),
    )
    if version >= 3:
        con.execute(
            "INSERT INTO grants (grant_id, session_key, principal, level, expires_at, revision,"
            " state, created_at, kind) VALUES ('t' || ?, 'claude:y', ?, 'view', ?, 2, 'live',"
            " 1.0, 'terminal')",
            ("0" * 21, "peer:" + "a" * 32, time.time() + 3600),
        )
    con.execute(f"PRAGMA user_version={version}")
    con.commit()
    con.close()
    return key


@pytest.mark.parametrize("version", [2, 3])
def test_an_older_store_migrates_in_place_to_4_and_a_backup_is_taken_first(tmp_path, version):
    """Schema 3 is #2056's (the grant kind); this build's input tables are schema 4. A store at
    either earlier version takes the steps it lacks, in place, after a complete copy."""
    directory = tmp_path / "fed"
    key = _old_store(directory, version)
    path = directory / store.DB_FILE

    st = store.Store(directory)
    kept = {g.grant_id[0]: g for g in st.list_grants()}  # the first use migrates
    assert kept["g"].level == "view" and kept["g"].revision == 4 and kept["g"].kind == "api"
    if version >= 3:  # a terminal grant keeps its kind and its level
        assert kept["t"].kind == "terminal" and kept["t"].level == "view"
    assert st.by_key(key).name == "old box"
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 4
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"suggestions", "peer_turns", "grants", "peers"} <= tables
    assert "kind" in {r[1] for r in con.execute("PRAGMA table_info(grants)")}
    con.close()

    (backup,) = directory.glob(f"{store.DB_FILE}.bak-v{version}-*")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    old = sqlite3.connect(backup)
    # Taken BEFORE the migration: still the old schema, with the grant and without the new tables.
    assert old.execute("PRAGMA user_version").fetchone()[0] == version
    assert old.execute("SELECT revision FROM grants WHERE grant_id LIKE 'g%'").fetchone()[0] == 4
    assert not old.execute("SELECT 1 FROM sqlite_master WHERE name='suggestions'").fetchone()
    old.close()
    store.Store(directory).list_peers()  # a second open copies nothing
    assert len(list(directory.glob("*.bak-*"))) == 1


def test_a_store_from_a_newer_build_is_refused_untouched(tmp_path):
    st = store.Store(tmp_path / "fed")
    trusted(st)
    con = sqlite3.connect(st.db_path)
    con.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    con.close()
    before = st.db_path.read_bytes()
    with pytest.raises(store.StoreUnsupported):
        store.Store(tmp_path / "fed").list_suggestions()
    assert st.db_path.read_bytes() == before and not list((tmp_path / "fed").glob("*.bak-*"))


# ── the levels ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("level", "default", "longest"),
    [("view", 7 * 86400, 90 * 86400), ("suggest", 7 * 86400, 30 * 86400), ("send", 3600, 86400)],
)
def test_each_level_has_its_default_and_its_longest_expiry(owner, level, default, longest):
    made = owner.grant(level)
    assert made.level == level and abs(made.expires_at - time.time() - default) < 30
    assert owner.grant(level, expires_in=longest).expires_at - time.time() > longest - 30
    over = refusal(
        grants.create,
        owner.st,
        session_key=api_key(),
        peer_id=owner.peer.peer_id,
        level=level,
        expires_in=longest + 1,
        accept_risk=True,
    )
    assert over.code == "invalid" and over.status == 422
    # and a change is held to the same maximum
    assert (
        refusal(
            grants.change,
            owner.st,
            made.grant_id,
            expires_in=longest + 1,
            revision=1,
            accept_risk=True,
        ).code
        == "invalid"
    )


@pytest.mark.anyio
async def test_a_terminal_grant_is_view_only_and_takes_no_input(owner, submits):
    """#2056 x #2057: a terminal session is shared ``view`` and nothing else. No level above it
    is granted or changed to, at the operator rules and again in the store, and every input verb
    refuses a terminal grant as `no_grant` (the uniform refusal), also one whose row was made to
    say ``send`` behind this build's back."""
    term_key = f"claude:{uuid.uuid4()}"  # a pty agent: a terminal session
    for level in ("suggest", "send"):
        err = refusal(
            grants.create,
            owner.st,
            session_key=term_key,
            peer_id=owner.peer.peer_id,
            level=level,
            expires_in=None,
            accept_risk=True,
        )
        assert err.code == "invalid" and "view only" in err.detail
    term = owner.grant("view", key=term_key)
    assert (term.kind, term.level) == ("terminal", "view")
    for level in ("suggest", "send"):
        err = refusal(
            grants.change,
            owner.st,
            term.grant_id,
            expires_in=None,
            revision=1,
            level=level,
            accept_risk=True,
        )
        assert err.code == "invalid" and "view only" in err.detail
    # The store refuses it again, for a caller that skipped the rules.
    with pytest.raises(store.Invalid):
        owner.st.change_grant(term.grant_id, expires_at=time.time() + 60, revision=1, level="send")
    with pytest.raises(store.Invalid):
        owner.st.create_grant(
            session_key=f"claude:{uuid.uuid4()}",
            peer_id=owner.peer.peer_id,
            level="suggest",
            expires_at=time.time() + 60,
            kind="terminal",
        )
    assert owner.st.get_grant(term.grant_id).level == "view"
    assert [e["event"] for e in owner.st.grant_events(term.grant_id)] == ["granted"]

    async def every_input_verb_refuses(made: store.Grant) -> None:
        assert await owner.refused("input.suggest", msg(made)) == "no_grant"
        assert await owner.refused("input.send", msg(made)) == "no_grant"
        assert await owner.refused("input.suggestions", ref(made)) == "no_grant"
        assert await owner.refused("input.withdraw", ref(made, suggestion_id="s" * 22)) == (
            "no_grant"
        )

    await every_input_verb_refuses(term)
    # A terminal row that says `send` (written behind this build's back) is still no input.
    con = sqlite3.connect(owner.st.db_path)
    con.execute("UPDATE grants SET level = 'send' WHERE grant_id = ?", (term.grant_id,))
    con.commit()
    con.close()
    await every_input_verb_refuses(term)
    with pytest.raises(store.NotGranted):
        owner.st.add_suggestion(
            grant_id=term.grant_id,
            peer_id=owner.peer.peer_id,
            revision=1,
            client_key=str(uuid.uuid4()),
            text=TEXT,
            actor="",
        )
    await owner.quiet()
    assert submits.calls == [] and owner.st.list_suggestions() == []
    assert bell() == []


def test_send_is_never_a_default_and_needs_the_operators_accept_risk(owner):
    assert grants.DEFAULT_LEVEL == "view" and grants.DEFAULT_EXPIRY_S["send"] == 3600
    for accept in (False, None, "true", 1, "yes"):
        err = refusal(
            grants.create,
            owner.st,
            session_key=api_key(),
            peer_id=owner.peer.peer_id,
            level="send",
            expires_in=None,
            accept_risk=accept,
        )
        assert (err.code, err.status) == ("risk", 422), accept
        assert "accept_risk" in err.detail
    assert owner.st.list_grants() == []  # nothing was granted on the way

    # raising to `send`, and giving a `send` grant more time, need it again
    low = owner.grant("suggest")
    assert refusal(grants.change, owner.st, low.grant_id, expires_in=None, revision=1, level="send")
    assert owner.st.get_grant(low.grant_id).level == "suggest"
    raised = grants.change(
        owner.st, low.grant_id, expires_in=None, revision=1, level="send", accept_risk=True
    )
    assert raised.level == "send" and raised.revision == 2
    assert raised.expires_at - time.time() < 3600 + 30  # the new level's default, never longer
    err = refusal(grants.change, owner.st, low.grant_id, expires_in=7200, revision=2)
    assert err.code == "risk"
    # lowering needs nothing, and never extends
    lowered = grants.change(owner.st, low.grant_id, expires_in=None, revision=2, level="view")
    assert lowered.level == "view" and lowered.expires_at == raised.expires_at
    events = [e["event"] for e in owner.st.grant_events(low.grant_id)]
    assert events == ["granted", "changed", "changed"]  # every change: a revision and an event


def test_a_send_grant_is_refused_for_a_session_a_mission_holds(owner, held, monkeypatch):
    key = api_key()
    held.add(key)
    err = refusal(
        grants.create,
        owner.st,
        session_key=key,
        peer_id=owner.peer.peer_id,
        level="send",
        expires_in=None,
        accept_risk=True,
    )
    assert (err.code, err.status) == ("held", 409)
    low = owner.grant("suggest", key)  # viewing and suggesting stay possible
    err = refusal(
        grants.change,
        owner.st,
        low.grant_id,
        expires_in=None,
        revision=1,
        level="send",
        accept_risk=True,
    )
    assert err.code == "held" and owner.st.get_grant(low.grant_id).level == "suggest"

    # a membership that cannot be read is not "no mission"
    def broken(key, **_kw):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(grants.missions, "session_mission", broken)
    err = refusal(
        grants.create,
        owner.st,
        session_key=api_key(),
        peer_id=owner.peer.peer_id,
        level="send",
        expires_in=None,
        accept_risk=True,
    )
    assert err.status == 503


# ── the verbs' arguments ──────────────────────────────────────────────


def test_the_text_budget_and_the_receipt_codes_are_the_runtimes_own():
    assert inputs.MAX_TEXT == public_api.MAX_TEXT
    assert inputs.HANDOFFS == native_journal.HANDOFFS == remote.HANDOFFS


@pytest.mark.parametrize("verb", ["input.suggest", "input.send"])
def test_malformed_arguments_never_reach_a_handler(verb):
    good = {"grant_id": "g" * 22, "revision": 1, "client_key": str(uuid.uuid4()), "text": "hi"}
    validate = verbs.PEER[verb].validate
    assert validate(dict(good))["actor"] == ""
    assert validate({**good, "actor": " Robin\n‮"})["actor"] == "Robin"
    assert len(validate({**good, "text": "x" * inputs.MAX_TEXT})["text"]) == inputs.MAX_TEXT
    for bad in (
        {**good, "text": ""},
        {**good, "text": "   \n"},
        {**good, "text": "x" * (inputs.MAX_TEXT + 1)},
        {**good, "text": 7},
        {**good, "client_key": "not-a-uuid"},
        {**good, "client_key": good["client_key"].upper()},
        {**good, "client_key": uuid.UUID(good["client_key"]).hex},
        {**good, "client_key": None},
        {**good, "actor": "x" * 1000},
        {**good, "actor": 7},
        {**good, "revision": 0},
        {**good, "grant_id": "../x"},
        # a peer never names an operation, a session, a level or an expiry
        {**good, "operation_id": str(uuid.uuid4())},
        {**good, "session_key": "codex-api:x"},
        {**good, "level": "send"},
        {**good, "expires_in": 86400},
    ):
        with pytest.raises(ch.BadArgs):
            validate(dict(bad))
    if verb == "input.send":
        assert validate({**good, "expected_revision": 5})["expected_revision"] == 5
        for bad in (True, -1, "5", 1.5):
            with pytest.raises(ch.BadArgs):
                validate({**good, "expected_revision": bad})
    else:
        with pytest.raises(ch.BadArgs):
            validate({**good, "expected_revision": 5})


# ── refusals ──────────────────────────────────────────────────────────

ENDINGS = ["revoke", "expiry", "stale_revision", "peer_revoke", "archive"]


def end(action: str, o: Owner, made: store.Grant, live_states: dict) -> None:
    if action == "revoke":
        grants.revoke(o.st, made.grant_id)
    elif action == "expiry":
        expire(o.st, made.grant_id)
    elif action == "stale_revision":
        grants.change(
            o.st,
            made.grant_id,
            expires_in=1800,
            revision=made.revision,
            accept_risk=made.level == "send",
        )
    elif action == "peer_revoke":
        o.st.revoke(o.peer.peer_id)
    else:
        o.st.end_grants_for_session(made.session_key)
        live_states[made.session_key] = "archived"


@pytest.mark.anyio
@pytest.mark.parametrize("action", ENDINGS)
async def test_input_after_a_grant_ended_or_changed_is_no_grant(owner, submits, live, action):
    made = owner.grant("send")
    earlier = await owner.call("input.suggest", msg(made))
    end(action, owner, made, live)
    assert await owner.refused("input.send", msg(made)) == "no_grant"
    assert await owner.refused("input.suggest", msg(made)) == "no_grant"
    assert await owner.refused("input.suggestions", ref(made)) == "no_grant"
    withdraw = ref(made, suggestion_id=earlier["suggestion_id"])
    assert await owner.refused("input.withdraw", withdraw) == "no_grant"
    assert submits.calls == []
    assert len(owner.st.list_suggestions()) == 1  # nothing new was recorded


@pytest.mark.anyio
async def test_a_view_grant_cannot_suggest_or_send_and_a_suggest_grant_cannot_send(owner, submits):
    view, suggest, send = owner.grant("view"), owner.grant("suggest"), owner.grant("send")
    assert await owner.refused("input.suggest", msg(view)) == "no_grant"
    assert await owner.refused("input.send", msg(view)) == "no_grant"
    assert await owner.refused("input.send", msg(suggest)) == "no_grant"
    assert submits.calls == [] and owner.st.list_suggestions() == []
    # each level includes the ones below it
    assert (await owner.call("input.suggest", msg(suggest)))["state"] == "pending"
    assert (await owner.call("input.suggest", msg(send)))["state"] == "pending"
    assert (await owner.call("input.send", msg(send)))["operation_id_known"] is True
    assert (await owner.call("input.suggestions", ref(view)))["suggestions"] == []
    # another peer's grant is no grant at all
    other = trusted(owner.st, "other")
    assert await owner.refused("input.send", msg(send), other) == "no_grant"
    assert await owner.refused("input.suggest", msg(send), other) == "no_grant"
    assert len(submits.calls) == 1


@pytest.mark.anyio
async def test_a_mission_that_takes_the_session_later_makes_suggest_and_send_answer_held(
    owner, submits, held
):
    made = owner.grant("send")
    assert (await owner.call("input.send", msg(made)))["handoff"] == "sent"
    held.add(made.session_key)
    assert await owner.refused("input.send", msg(made)) == "held"
    assert await owner.refused("input.suggest", msg(made)) == "held"
    assert len(submits.calls) == 1 and owner.st.list_suggestions() == []
    held.clear()
    assert (await owner.call("input.suggest", msg(made)))["state"] == "pending"


@pytest.mark.anyio
async def test_the_mission_check_is_made_again_inside_the_fence(owner, submits, held, monkeypatch):
    """The early check passes; a mission takes the session before the fenced re-check."""
    made = owner.grant("send")
    real = inputs._admit_send

    def admit(st, peer_id, args):
        held.add(made.session_key)
        return real(st, peer_id, args)

    monkeypatch.setattr(inputs, "_admit_send", admit)
    assert await owner.refused("input.send", msg(made)) == "held"
    assert submits.calls == [] and owner.st.get_peer_turn(made.grant_id, "x") is None


# ── no verb changes a grant ───────────────────────────────────────────


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                found.add(node.asname)
    return found


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


@pytest.mark.anyio
async def test_no_verb_extends_raises_or_recreates_a_grant(owner, submits):
    assert set(verbs.PEER) == {
        "peer.hello",
        "peer.ping",
        "peer.bye",
        "share.list",
        "session.snapshot",
        "session.events",
        "session.history",
        "share.changed",
        "input.suggest",
        "input.suggestions",
        "input.withdraw",
        "input.send",
        "terminal.frame",  # reads only (#2056)
    }
    made = owner.grant("send")
    before = (owner.st.get_grant(made.grant_id), owner.st.generation())
    made_id = (await owner.call("input.suggest", msg(made)))["suggestion_id"]
    await owner.call("input.suggestions", ref(made))
    await owner.call("input.withdraw", ref(made, suggestion_id=made_id))
    await owner.call("input.send", msg(made))
    assert (owner.st.get_grant(made.grant_id), owner.st.generation()) == before
    assert [e["event"] for e in owner.st.grant_events(made.grant_id)] == ["granted"]
    assert len(owner.st.list_grants()) == 1

    # In the source: the functions that create or change a grant are named by the operator
    # routes (and the store's own callers in `grants.py`), and by no module a verb runs in.
    writers = {"create_grant", "change_grant"}
    for path in _sources():
        names = _names(path)
        if _rel(path) not in ("federation/store.py", "federation/grants.py"):
            assert not names & writers, _rel(path)
    modules = ("inputs", "sharing", "verbs", "remote", "channel", "notices", "fence", "terminal")
    for module in modules:
        tree = ast.parse((SRC / "federation" / f"{module}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert (node.value.id, node.attr) not in {
                    ("grants", "create"),
                    ("grants", "change"),
                }, module


# ── input.suggest ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_same_client_key_makes_one_suggestion_also_across_a_restart(owner, submits):
    made = owner.grant("suggest")
    first = msg(made)
    one = await owner.call("input.suggest", first)
    two = await owner.call("input.suggest", first)
    three = await owner.restarted().call("input.suggest", {**first, "text": "another wording"})
    assert one == two == three and one["state"] == "pending"
    (row,) = owner.st.list_suggestions()
    assert row.text == TEXT and row.client_key == first["client_key"]  # the first one stands
    # the operation id was minted with the suggestion, by the owner
    assert uuid.UUID(row.operation_id) and row.operation_id != first["client_key"]
    await owner.quiet()
    assert len(bell()) == 1  # and one notification, not three
    assert submits.calls == []  # a suggestion is not a delivery


@pytest.mark.anyio
async def test_the_sixth_pending_suggestion_is_busy(owner):
    made = owner.grant("suggest")
    ids = [(await owner.call("input.suggest", msg(made)))["suggestion_id"] for _ in range(5)]
    assert await owner.refused("input.suggest", msg(made)) == "busy"
    # the bound is on PENDING ones: a decided or withdrawn one frees a place
    await owner.call("input.withdraw", ref(made, suggestion_id=ids[0]))
    assert (await owner.call("input.suggest", msg(made)))["state"] == "pending"
    assert await owner.refused("input.suggest", msg(made)) == "busy"
    # and it is per grant
    assert (await owner.call("input.suggest", msg(owner.grant("suggest"))))["state"] == "pending"


@pytest.mark.anyio
async def test_twenty_concurrent_suggestions_leave_exactly_five_pending(owner):
    made = owner.grant("suggest")
    results = await asyncio.gather(
        *(owner.call("input.suggest", msg(made, text=f"s{i}")) for i in range(20)),
        return_exceptions=True,
    )
    accepted = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, ch.VerbError)]
    assert len(accepted) == 5 and len(refused) == 15 and {r.code for r in refused} == {"busy"}
    assert sum(s.state == "pending" for s in owner.st.list_suggestions()) == 5


def test_twenty_threads_over_two_store_objects_leave_exactly_five_pending(owner):
    """The bound is the transaction's, not a lock of one process: two store objects over one
    file stand for two processes."""
    made = owner.grant("suggest")
    stores = [owner.st, store.Store(owner.st.directory)]
    go = threading.Barrier(20)
    outcomes: list[str] = []

    def suggest(i: int) -> None:
        go.wait()
        try:
            stores[i % 2].add_suggestion(
                grant_id=made.grant_id,
                peer_id=owner.peer.peer_id,
                revision=1,
                client_key=str(uuid.uuid4()),
                text=f"s{i}",
                actor="",
            )
            outcomes.append("ok")
        except store.Full:
            outcomes.append("full")

    threads = [threading.Thread(target=suggest, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(outcomes) == ["full"] * 15 + ["ok"] * 5
    assert len(owner.st.list_suggestions()) == 5


@pytest.mark.anyio
async def test_the_peer_reads_states_without_text_and_withdraws_only_its_own(owner):
    made = owner.grant("suggest")
    key = str(uuid.uuid4())
    one = await owner.call("input.suggest", msg(made, client_key=key))
    listed = await owner.call("input.suggestions", ref(made))
    assert remote.check_suggestions(listed) is listed
    (row,) = listed["suggestions"]
    assert row["suggestion_id"] == one["suggestion_id"] and row["client_key"] == key
    assert row["state"] == "pending" and TEXT not in json.dumps(listed)

    # another peer's grant does not reach it, and neither does another grant of the same peer
    other = trusted(owner.st, "other")
    theirs = owner.grant("suggest", peer=other)
    mine_too = owner.grant("suggest")
    for g, peer in ((theirs, other), (mine_too, None)):
        args = ref(g, suggestion_id=one["suggestion_id"])
        assert await owner.refused("input.withdraw", args, peer) == "not_found"
        assert owner.suggestion(one["suggestion_id"]).state == "pending"
    unknown = ref(made, suggestion_id="z" * 22)
    assert await owner.refused("input.withdraw", unknown) == "not_found"

    gone = await owner.call("input.withdraw", ref(made, suggestion_id=one["suggestion_id"]))
    assert remote.check_suggestion(gone)["state"] == "withdrawn"
    assert await owner.refused("input.withdraw", ref(made, suggestion_id=gone["suggestion_id"]))
    assert owner.suggestion(one["suggestion_id"]).end_reason == "peer"


# ── deciding ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_approve_delivers_the_text_once_under_the_suggestions_own_operation_id(
    owner, submits
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made, actor=ACTOR))
    await owner.quiet()
    before = owner.suggestion(one["suggestion_id"])
    assert submits.calls == []  # nothing is delivered by suggesting

    done = await inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    assert done.state == "approved" and done.handoff == "sent" and done.edited is False
    assert done.approved_by == "marcus" and done.approved_text == TEXT
    assert submits.delivered == [(made.session_key, before.operation_id, TEXT)]

    # a duplicate approve is a no-op, a different text is refused: never a second turn
    again = await inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    same = await inputs.decide_approve(owner.fed, one["suggestion_id"], text=TEXT, approver="m")
    assert again == same == done
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(owner.fed, one["suggestion_id"], text="other", approver="m")
    assert (exc.value.status, exc.value.state) == (409, "approved")
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_reject(owner.fed, one["suggestion_id"], approver="marcus")
    assert (exc.value.status, exc.value.state) == (409, "approved")
    assert len(submits.calls) == 1

    # attribution: the peer's display name HERE, the label it gave, via `peer`
    authors = owner.st.peer_authors(made.session_key)
    view = share_api.author_view(share_api.with_peer_authors({}, authors)[before.operation_id])
    assert view == {"name": "GPU box", "via": "peer", "actor": ACTOR}

    # audit: an approval and a delivery, with ids and without the text
    rows = ledger_rows()
    assert [r["verb"] for r in rows] == ["peer_approved", "peer_delivered"]
    for row in rows:
        assert row["peer_id"] == owner.peer.peer_id and row["grant_id"] == made.grant_id
        assert row["operation_id"] == before.operation_id and row["actor"] == ACTOR
        assert row["session_id"] == made.session_key and row["state"] == "observed"
    assert rows[1]["handoff"] == "sent"
    assert TEXT not in json.dumps(orchestrator_ledger.read_all())


@pytest.mark.anyio
async def test_an_edited_approval_delivers_the_operators_text_and_says_so(owner, submits):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    edited = "the operator's own wording"
    done = await inputs.decide_approve(
        owner.fed, one["suggestion_id"], text=edited, approver="marcus"
    )
    assert done.edited is True and done.approved_text == edited and done.text == TEXT
    assert [t for _, _, t in submits.delivered] == [edited]
    entry = share_api.with_peer_authors({}, owner.st.peer_authors(made.session_key))[
        done.operation_id
    ]
    assert share_api.author_view(entry) == {"name": "GPU box", "via": "peer", "edited": True}
    # an edited text holds the operator's words: its own peer does not read it back unredacted
    assert "share_id" not in entry
    assert all(r["edited"] is True for r in ledger_rows())
    assert edited not in json.dumps(orchestrator_ledger.read_all())
    for bad in ("", "   ", 7, "x" * (inputs.MAX_TEXT + 1)):
        with pytest.raises(inputs.InputError) as exc:
            await inputs.decide_approve(owner.fed, one["suggestion_id"], text=bad, approver="m")
        assert exc.value.status in (409, 422)
    assert len(submits.calls) == 1


@pytest.mark.anyio
async def test_reject_delivers_nothing_and_is_final(owner, submits):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made, actor=ACTOR))
    gone = await inputs.decide_reject(owner.fed, one["suggestion_id"], approver="marcus")
    assert gone.state == "rejected" and gone.approved_by == "marcus"
    for decide in (inputs.decide_approve, inputs.decide_reject):
        with pytest.raises(inputs.InputError) as exc:
            await decide(owner.fed, one["suggestion_id"], approver="marcus")
        assert (exc.value.status, exc.value.state) == (409, "rejected")
    assert await owner.refused("input.withdraw", ref(made, suggestion_id=one["suggestion_id"]))
    assert submits.calls == [] and owner.st.peer_authors(made.session_key) == {}
    (row,) = ledger_rows()
    assert row["verb"] == "peer_rejected" and row["peer_id"] == owner.peer.peer_id
    assert TEXT not in json.dumps(orchestrator_ledger.read_all())
    for unknown in ("z" * 22, "../x", None, 7):
        with pytest.raises(inputs.InputError) as exc:
            await inputs.decide_reject(owner.fed, unknown, approver="marcus")
        assert exc.value.status == 404


@pytest.mark.anyio
async def test_approve_reject_and_withdraw_fired_together_have_exactly_one_winner(owner, submits):
    made = owner.grant("suggest")
    winners: set[str] = set()
    for _round in range(12):
        one = await owner.call("input.suggest", msg(made))
        sid = one["suggestion_id"]
        delivered = len(submits.delivered)
        results = await asyncio.gather(
            inputs.decide_approve(owner.fed, sid, approver="marcus"),
            inputs.decide_reject(owner.fed, sid, approver="marcus"),
            owner.call("input.withdraw", ref(made, suggestion_id=sid)),
            return_exceptions=True,
        )
        won = [r for r in results if not isinstance(r, Exception)]
        lost = [r for r in results if isinstance(r, Exception)]
        assert len(won) == 1 and len(lost) == 2, results
        final = owner.suggestion(sid).state
        assert final in ("approved", "rejected", "withdrawn")
        winners.add(final)
        for err in lost:
            if isinstance(err, inputs.InputError):
                assert err.status == 409
                # the state that won (an approval is seen mid-way as `approving`)
                assert err.state in ({"approving", "approved"} if final == "approved" else {final})
            else:
                assert isinstance(err, ch.VerbError) and err.code == "conflict"
        assert len(submits.delivered) - delivered == (1 if final == "approved" else 0)
    assert winners  # whoever won each round, nothing was delivered twice or half-decided


@pytest.mark.anyio
async def test_a_withdraw_after_approving_is_refused(owner, submits):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    await asyncio.to_thread(inputs._begin_approval, owner.st, one["suggestion_id"], None, "marcus")
    assert owner.suggestion(one["suggestion_id"]).state == "approving"
    args = ref(made, suggestion_id=one["suggestion_id"])
    assert await owner.refused("input.withdraw", args) == "conflict"
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_reject(owner.fed, one["suggestion_id"], approver="marcus")
    assert (exc.value.status, exc.value.state) == (409, "approving")
    assert owner.suggestion(one["suggestion_id"]).state == "approving"


@pytest.mark.anyio
async def test_approve_is_refused_while_a_mission_holds_the_session_and_after_the_grant_ended(
    owner, submits, held
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    held.add(made.session_key)
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    assert (exc.value.code, exc.value.status) == ("held", 409)
    assert owner.suggestion(one["suggestion_id"]).state == "pending"  # it can be approved later
    held.clear()
    expire(owner.st, made.grant_id)  # expired on the clock, not yet swept
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    assert (exc.value.code, exc.value.status) == ("ended", 409)
    assert submits.calls == []


# ── crash, restart, replay ────────────────────────────────────────────


@pytest.mark.anyio
async def test_approve_crash_before_the_submit_restart_replays_once_with_the_recorded_text(
    owner, submits
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    sid = one["suggestion_id"]
    # Step 1 commits, then the process dies: nothing was submitted.
    begun = await asyncio.to_thread(
        inputs._begin_approval, owner.st, sid, "edited before the crash", "m"
    )
    assert begun.state == "approving" and submits.calls == []

    again = owner.restarted()
    assert await inputs.resume_all(again.fed) == 1
    done = again.suggestion(sid)
    assert done.state == "approved" and done.handoff == "sent" and done.approved_by == "m"
    assert submits.delivered == [(made.session_key, begun.operation_id, "edited before the crash")]
    # a second sweep, a duplicate approve and an approve with another text: never a second turn
    assert await inputs.resume_all(again.fed) == 0
    await inputs.decide_approve(again.fed, sid, approver="marcus")
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(again.fed, sid, text="something else", approver="marcus")
    assert exc.value.status == 409
    assert len(submits.calls) == 1
    assert [r["verb"] for r in ledger_rows()] == ["peer_approved", "peer_delivered"]


@pytest.mark.anyio
async def test_the_service_replays_approving_rows_when_it_starts(owner, submits, monkeypatch):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    await asyncio.to_thread(inputs._begin_approval, owner.st, one["suggestion_id"], None, "marcus")
    again = owner.restarted()
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.05)
    again.fed.start()
    try:
        await until(lambda: again.suggestion(one["suggestion_id"]).state == "approved")
    finally:
        await again.fed.stop()
    assert len(submits.delivered) == 1


@pytest.mark.anyio
async def test_a_lost_answer_after_the_submit_is_replayed_by_the_next_approve_not_resent(
    owner, submits
):
    """The submit reached the runtime and the answer was lost before `approved` was written."""
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    sid = one["suggestion_id"]
    begun = await asyncio.to_thread(inputs._begin_approval, owner.st, sid, None, "marcus")
    await runtime.submit_turn(made.session_key, operation_id=begun.operation_id, text=TEXT)
    assert owner.suggestion(sid).state == "approving"
    with pytest.raises(inputs.InputError):  # another text after step 1: refused, never applied
        await inputs.decide_approve(owner.fed, sid, text="changed my mind", approver="marcus")
    done = await inputs.decide_approve(owner.fed, sid, approver="marcus")
    assert done.state == "approved"
    # submitted again with the SAME id and text; the runtime answered it from its journal
    assert [c[1] for c in submits.calls] == [begun.operation_id] * 2
    assert len(submits.delivered) == 1


@pytest.mark.anyio
async def test_a_submit_the_runtime_refuses_leaves_approving_and_is_replayed(owner, submits):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    sid = one["suggestion_id"]
    submits.error = runtime.StructuredError(503, "the native runtime is unavailable")
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(owner.fed, sid, approver="marcus")
    assert exc.value.status == 503 and owner.suggestion(sid).state == "approving"
    assert not fence.held_elsewhere(owner.st.directory, made.grant_id)  # it raised: released
    submits.error = None
    assert (await inputs.decide_approve(owner.fed, sid, approver="marcus")).state == "approved"
    assert len(submits.delivered) == 1 and len({c[1] for c in submits.calls}) == 1


REPLAY_RACES = ["revoke", "expiry", "peer_revoke", "archive", "mission", "lowered"]


@pytest.mark.anyio
@pytest.mark.parametrize("race", REPLAY_RACES)
@pytest.mark.parametrize("via", ["start_up_sweep", "next_approve", "round"])
async def test_a_replay_that_finds_the_approval_no_longer_good_submits_nothing_and_withdraws(
    owner,
    submits,
    held,
    live,
    race,
    via,
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    sid = one["suggestion_id"]
    await asyncio.to_thread(inputs._begin_approval, owner.st, sid, None, "marcus")
    if race == "mission":
        held.add(made.session_key)
    elif race == "lowered":
        grants.change(owner.st, made.grant_id, expires_in=None, revision=1, level="view")
    else:
        end(race, owner, made, live)
    assert owner.suggestion(sid).state == "approving"  # ending a grant does not decide it

    again = owner.restarted()
    if via == "start_up_sweep":
        await inputs.resume_all(again.fed)
    elif via == "next_approve":
        gone = await inputs.decide_approve(again.fed, sid, approver="marcus")
        assert gone.state == "withdrawn"
    elif race != "mission":  # the round settles approvals whose GRANT ended
        await inputs.settle_ended(again.fed)
    else:
        await inputs.resume_all(again.fed)
    after = again.suggestion(sid)
    assert after.state == "withdrawn" and submits.calls == []
    assert after.end_reason == ("mission_held" if race == "mission" else "grant_ended")
    assert again.st.peer_authors(made.session_key) == {}
    # withdrawn is final: no later approve delivers it
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(again.fed, sid, approver="marcus")
    assert (exc.value.status, exc.value.state) == (409, "withdrawn")
    assert submits.calls == []


@pytest.mark.anyio
async def test_a_replay_that_cannot_read_decides_nothing(owner, submits, live):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    sid = one["suggestion_id"]
    await asyncio.to_thread(inputs._begin_approval, owner.st, sid, None, "marcus")
    live[made.session_key] = "unreadable"
    with pytest.raises(inputs.InputError) as exc:
        await inputs.resume_approving(owner.fed, sid)
    assert exc.value.status == 503
    assert await inputs.resume_all(owner.fed) == 0
    assert owner.suggestion(sid).state == "approving" and submits.calls == []
    live.clear()
    assert await inputs.resume_all(owner.fed) == 1
    assert owner.suggestion(sid).state == "approved" and len(submits.delivered) == 1


# ── who can approve: the source ───────────────────────────────────────


def _users(name: str) -> set[str]:
    return {_rel(p) for p in _sources() if name in _names(p)}


def _functions_naming(path: Path, name: str) -> set[str]:
    """The top-level functions of ``path`` whose body names ``name``."""
    tree = ast.parse(path.read_text())
    out = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            inner = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            inner |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if name in inner:
                out.add(node.name)
    return out


def test_the_approval_is_made_from_the_operator_route_only():
    """`decide_approve` MAKES an approval. Only the operator route names it; the store write
    behind it is named only by it. Nothing else in the tree can move a suggestion toward
    delivery, whatever mode an automation runs in."""
    assert _users("decide_approve") == {"federation/inputs.py", "routes/federation.py"}
    assert _users("decide_reject") == {"federation/inputs.py", "routes/federation.py"}
    # the `pending → approving` write: the store defines it, `inputs._begin` calls it, and
    # `_begin` is called from `decide_approve` and nowhere else
    assert _users("begin_approval") == {"federation/store.py", "federation/inputs.py"}
    inputs_py = SRC / "federation" / "inputs.py"
    assert _functions_naming(inputs_py, "begin_approval") == {"_begin_approval"}
    assert _functions_naming(inputs_py, "_begin_approval") == {"decide_approve"}
    assert _users("_begin_approval") == {"federation/inputs.py"}
    # in the route module, the one caller is the approve route
    routes_py = SRC / "routes" / "federation.py"
    tree = ast.parse(routes_py.read_text())
    callers = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name.startswith("federation_")
        and any(isinstance(n, ast.Attribute) and n.attr == "decide_approve" for n in ast.walk(node))
    ]
    assert callers == ["federation_suggestion_approve"]


def test_the_replay_is_called_from_inside_the_federation_package_only():
    for name in ("resume_approving", "resume_all", "settle_ended"):
        users = _users(name)
        assert users and all(u.startswith("federation/") for u in users), (name, users)
    # and the replay never reaches the write that MAKES an approval
    inputs_py = SRC / "federation" / "inputs.py"
    for fn in ("resume_approving", "resume_all", "settle_ended", "_recheck", "_deliver_approved"):
        assert fn not in _functions_naming(inputs_py, "begin_approval")
        assert fn not in _functions_naming(inputs_py, "_begin_approval")


def test_suggestion_text_is_read_from_the_store_by_the_federation_package_and_its_routes_only():
    allowed = lambda rel: rel.startswith("federation/") or rel == "routes/federation.py"  # noqa: E731
    readers = {
        "get_suggestion",
        "list_suggestions",
        "approving_suggestions",
        "add_suggestion",
        "Suggestion",
        "_suggestion",
    }
    for path in _sources():
        rel = _rel(path)
        if allowed(rel):
            continue
        assert not _names(path) & readers, rel
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # no SQL of its own over the table, and no other way to the module that reads it
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                assert not (
                    "suggestions" in lowered
                    and any(w in lowered for w in ("select ", "update ", "insert ", " from "))
                    and "federation" in lowered
                ), rel
                assert "from suggestions" not in lowered and "peer_turns" not in lowered, rel
            if isinstance(node, ast.ImportFrom) and node.module and "federation" in node.module:
                assert not {a.name for a in node.names} & {"inputs"}, rel
                assert not node.module.endswith("federation.inputs"), rel
    # the store's text-bearing reads are used by these modules and no other
    assert _users("list_suggestions") == {"federation/store.py", "federation/inputs.py"}
    assert _users("get_suggestion") == {"federation/store.py", "federation/inputs.py"}
    # what leaves the package for other modules carries no text: the authors and the notice
    assert "text" not in store.Store.peer_authors.__code__.co_names
    for module in ("notices.py", "sharing.py", "remote.py", "verbs.py", "service.py"):
        names = _names(SRC / "federation" / module)
        assert not names & {"get_suggestion", "list_suggestions"}, module


# ── input.send ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_send_delivers_one_turn_per_client_key_and_records_its_author_first(owner, submits):
    made = owner.grant("send")
    first = msg(made, actor=ACTOR, expected_revision=3)
    seen: list[dict | None] = []
    real = submits.submit_turn

    async def spy(key, **kw):
        # the row is there BEFORE the submit: a crash after this point still knows the author
        seen.append(owner.st.get_peer_turn(made.grant_id, first["client_key"]))
        return await real(key, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runtime, "submit_turn", spy)
        answer = await owner.call("input.send", first)
        again = await owner.call("input.send", first)
    assert answer == again == {"operation_id_known": True, "handoff": "sent"}
    assert remote.check_sent(answer) is answer
    row = owner.st.get_peer_turn(made.grant_id, first["client_key"])
    assert seen[0] is not None and seen[0]["operation_id"] == row["operation_id"]
    assert row["via"] == "send" and row["actor"] == ACTOR and row["handoff"] == "sent"
    assert row["operation_id"] not in (first["client_key"], made.grant_id)  # owner-minted
    # the same key: the same operation, submitted again, delivered once
    assert submits.calls == [(made.session_key, row["operation_id"], TEXT)] * 2
    assert len(submits.delivered) == 1
    # a retry with other words is the runtime's refusal, not a second turn
    assert await owner.refused("input.send", {**first, "text": "other words"}) == "conflict"
    assert len(submits.delivered) == 1
    # another key is another turn
    await owner.call("input.send", msg(made))
    assert len(submits.delivered) == 2

    (entry, _other) = ledger_rows()  # one entry per TURN, not per call
    assert entry["verb"] == "peer_delivered" and entry["operation_id"] == row["operation_id"]
    assert entry["peer_id"] == owner.peer.peer_id and entry["grant_id"] == made.grant_id
    assert entry["actor"] == ACTOR and entry["handoff"] == "sent"
    assert TEXT not in json.dumps(orchestrator_ledger.read_all())
    authors = share_api.with_peer_authors({}, owner.st.peer_authors(made.session_key))
    assert share_api.author_view(authors[row["operation_id"]]) == {
        "name": "GPU box",
        "via": "peer",
        "actor": ACTOR,
    }
    assert authors[row["operation_id"]]["share_id"] == made.grant_id  # its own text, to itself


@pytest.mark.anyio
async def test_a_lost_answer_then_a_retry_also_across_a_restart_delivers_once(
    owner, submits, monkeypatch
):
    made = owner.grant("send")
    first = msg(made)
    monkeypatch.setattr(inputs, "ANSWER_WAIT_S", 0.05)
    submits.gate = asyncio.Event()
    # the caller's wait ends first: it is told `timeout`, and the delivery goes on
    assert await owner.refused("input.send", first) == "timeout"
    assert submits.finished == 0 and fence.held_elsewhere(owner.st.directory, made.grant_id)
    submits.gate.set()
    await until(lambda: submits.finished == 1)
    await until(lambda: not fence.held_elsewhere(owner.st.directory, made.grant_id))
    submits.gate = None
    monkeypatch.setattr(inputs, "ANSWER_WAIT_S", 25.0)

    op = owner.st.get_peer_turn(made.grant_id, first["client_key"])["operation_id"]
    answer = await owner.call("input.send", first)  # the retry learns the outcome
    restarted = await owner.restarted().call("input.send", first)  # and so does one after a restart
    assert answer == restarted == {"operation_id_known": True, "handoff": "sent"}
    assert [c[1] for c in submits.calls] == [op, op, op]
    assert submits.delivered == [(made.session_key, op, TEXT)]
    assert len([r for r in ledger_rows() if r["verb"] == "peer_delivered"]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"state": "running", "handoff": "sent"}, "sent"),
        ({"state": "idle", "handoff": "queued"}, "queued"),
        ({"state": "running", "handoff": "uncertain"}, "uncertain"),
        ({"state": "failed", "handoff": "not_sent"}, "not_sent"),
        # a runtime without a receipt (the chat adapter): null, whatever its state says
        ({"state": "running"}, None),
        ({"state": "completed"}, None),
        ({"state": "failed"}, None),
        ({"state": "running", "handoff": None}, None),
        # not one of the runtime's codes: not passed on, and not guessed
        ({"state": "running", "handoff": "delivered!"}, None),
        ({"state": "running", "handoff": ["sent"]}, None),
    ],
)
async def test_the_answer_is_the_runtimes_handoff_and_never_its_state(
    owner, submits, result, expected
):
    submits.result = result
    made = owner.grant("send")
    answer = await owner.call("input.send", msg(made))
    assert answer == {"operation_id_known": True, "handoff": expected}
    assert "state" not in answer
    assert len(submits.calls) == 1  # `uncertain`, `not_sent` and no receipt: never retried here

    low = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(low))
    done = await inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    assert done.state == "approved" and done.handoff == expected
    assert inputs.view(done)["handoff"] == expected and "state_of_session" not in inputs.view(done)
    assert len(submits.calls) == 2


def test_the_receipt_is_read_from_the_handoff_field_alone():
    assert inputs.receipt({"handoff": "uncertain", "state": "completed"}) == "uncertain"
    assert inputs.receipt({"state": "running", "delivery": "sent"}) is None
    assert inputs.receipt(None) is None and inputs.receipt("sent") is None
    # the module never loops over a submit and never reads `state`
    source = (SRC / "federation" / "inputs.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.For | ast.While | ast.AsyncFor):
            names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            names |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert not names & {"submit_turn", "_submit", "_send", "_deliver_approved"}
    assert '"state"' not in ast.unparse(
        next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "receipt")
    )
    assert source.count("runtime.submit_turn(") == 1


@pytest.mark.anyio
async def test_what_the_runtime_refuses_is_refused_here_with_a_code_and_nothing_else(
    owner, submits
):
    made = owner.grant("send")
    for status, code in ((409, "conflict"), (422, "invalid"), (429, "busy"), (503, "unavailable")):
        submits.error = runtime.StructuredError(status, "an operator-only sentence /home/op")
        assert await owner.refused("input.send", msg(made)) == code
        assert not fence.held_elsewhere(owner.st.directory, made.grant_id)  # raised: released
    submits.error = runtime.StructuredError(404, "gone")
    assert await owner.refused("input.send", msg(made)) == "no_grant"


@pytest.mark.anyio
async def test_a_peer_holds_at_most_four_deliveries_in_flight(owner, submits):
    made = [owner.grant("send") for _ in range(inputs.DELIVERIES_PER_PEER + 1)]
    submits.gate = asyncio.Event()
    tasks = [
        asyncio.create_task(owner.call("input.send", msg(g)))
        for g in made[: inputs.DELIVERIES_PER_PEER]
    ]
    await until(lambda: len(submits.calls) == inputs.DELIVERIES_PER_PEER)
    assert await owner.refused("input.send", msg(made[-1])) == "busy"
    submits.gate.set()
    await asyncio.gather(*tasks)
    await until(lambda: not owner.fed.deliveries)
    assert (await owner.call("input.send", msg(made[-1])))["handoff"] == "sent"


def test_every_input_verb_checks_the_grant_first_and_again_as_its_last_await():
    """As `test_federation_sharing`'s pin for the reading verbs: the first `await` is the
    per-call check, the last one is the same check, and the statement after it is `return`."""
    import inspect
    import textwrap

    needs = {
        inputs.input_send: "send",
        inputs.input_suggest: "suggest",
        inputs.input_suggestions: None,
        inputs.input_withdraw: None,
    }
    for handler, need in needs.items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        body = tree.body[0].body
        last, check = body[-1], body[-2]
        assert isinstance(last, ast.Return) and isinstance(last.value, ast.Name), handler.__name__
        assert isinstance(check, ast.Expr) and isinstance(check.value, ast.Await)
        first = next(n for n in ast.walk(tree) if isinstance(n, ast.Await))
        for call in (check.value.value, first.value):
            assert ast.unparse(call.func) == "sharing._authorize", handler.__name__
            keywords = {k.arg: k.value for k in call.keywords}
            # Input is for API sessions only: a terminal grant (view only) is `no_grant` here.
            assert ast.unparse(keywords["kind"]) == "grants.KIND_API", handler.__name__
            level = keywords["need"].value if "need" in keywords else None
            assert level == need, handler.__name__


# ── withdrawal with the grant, the notification and its retraction ────


@pytest.mark.anyio
async def test_the_bell_row_and_the_push_name_the_peer_and_the_session_never_the_text(
    owner, monkeypatch
):
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
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made, actor=ACTOR))
    await owner.quiet()

    (row,) = bell()
    assert row["title"] == "GPU box suggests a message" and row["project"] == TITLE
    assert row["reason"] == notifications.EVENT_REASON["peer_suggestion"]
    assert row["session_id"] == made.session_key
    assert row["action_id"] == notices.notice_key(one["suggestion_id"])
    assert row["escalation"] is False and "dispatch" not in row
    assert TEXT not in json.dumps(row) and ACTOR not in json.dumps(row)

    (body,) = captured
    payload = json.loads(_decrypt(body, priv, pub_raw, auth))
    engine, _, native = made.session_key.partition(":")
    assert payload == {
        "title": "GPU box suggests a message",
        "body": TITLE,
        "url": f"/s/{engine}/{native}",
    }
    assert TEXT.encode() not in _decrypt(body, priv, pub_raw, auth)


ROW_ENDINGS = ["approve", "reject", "withdraw", "revoke", "peer_revoke", "archive", "lowered"]


@pytest.mark.anyio
@pytest.mark.parametrize("how", ROW_ENDINGS)
async def test_the_notification_is_retracted_when_the_suggestion_stops_being_pending(
    owner,
    submits,
    live,
    how,
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    other = await owner.call("input.suggest", msg(owner.grant("suggest")))  # stays pending
    await owner.quiet()
    assert len(bell()) == 2
    sid = one["suggestion_id"]
    if how == "approve":
        await inputs.decide_approve(owner.fed, sid, approver="marcus")
    elif how == "reject":
        await inputs.decide_reject(owner.fed, sid, approver="marcus")
    elif how == "withdraw":
        await owner.call("input.withdraw", ref(made, suggestion_id=sid))
    elif how == "lowered":
        grants.change(owner.st, made.grant_id, expires_in=None, revision=1, level="view")
    elif how == "peer_revoke":
        # the other suggestion is that peer's too: it goes with the peer
        await owner.fed.revoke(owner.peer.peer_id)
    else:
        end(how, owner, made, live)
    await owner.quiet()
    await inputs.settle_ended(owner.fed)  # what the dialer round does, for any process's write

    state = owner.suggestion(sid).state
    assert state == {"approve": "approved", "reject": "rejected"}.get(how, "withdrawn")
    kept = [] if how == "peer_revoke" else [notices.notice_key(other["suggestion_id"])]
    assert [r["action_id"] for r in bell()] == kept
    assert owner.st.unretracted() == []
    if how in ("revoke", "peer_revoke", "archive", "lowered"):
        reason = {"lowered": "grant_lowered", "archive": "grant_archived"}.get(how)
        assert owner.suggestion(sid).end_reason == (reason or f"grant_{how}d")


@pytest.mark.anyio
async def test_ending_a_grant_withdraws_its_pending_suggestions_in_the_same_transaction(owner):
    made = owner.grant("suggest")
    ids = [(await owner.call("input.suggest", msg(made)))["suggestion_id"] for _ in range(3)]
    await inputs.decide_reject(owner.fed, ids[0], approver="marcus")
    keep = await owner.call("input.suggest", msg(owner.grant("suggest")))
    # one statement over one connection sees both changes or neither
    grants.revoke(owner.st, made.grant_id)
    assert [owner.suggestion(i).state for i in ids] == ["rejected", "withdrawn", "withdrawn"]
    assert owner.suggestion(keep["suggestion_id"]).state == "pending"
    source = (SRC / "federation" / "store.py").read_text()
    end_grants = source[source.index("def _end_grants") : source.index("def generation")]
    assert "_withdraw_pending(con" in end_grants  # inside the ending transaction's own function


@pytest.mark.anyio
async def test_a_pending_suggestion_expires_after_a_day_or_with_its_grant(owner):
    short = owner.grant("suggest", expires_in=3600)
    long = owner.grant("suggest")
    a = await owner.call("input.suggest", msg(short))
    b = await owner.call("input.suggest", msg(long))
    now = time.time()
    assert abs(owner.suggestion(a["suggestion_id"]).expires_at - short.expires_at) < 1
    assert abs(owner.suggestion(b["suggestion_id"]).expires_at - now - 86400) < 30
    assert owner.st.expire_suggestions(now=now + 86400 - 60) == 1  # the short grant's
    assert owner.st.expire_suggestions(now=now + 86400 + 60) == 1
    assert {owner.suggestion(x["suggestion_id"]).state for x in (a, b)} == {"expired"}
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(owner.fed, b["suggestion_id"], approver="marcus")
    assert (exc.value.status, exc.value.state) == (409, "expired")
    # the sweep that tidies grants does this too
    c = await owner.call("input.suggest", msg(long))
    con = sqlite3.connect(owner.st.db_path)
    con.execute(
        "UPDATE suggestions SET expires_at = 1 WHERE suggestion_id = ?", (c["suggestion_id"],)
    )
    con.commit()
    con.close()
    with pytest.raises(inputs.InputError):  # past its time, not yet swept: not approvable
        await inputs.decide_approve(owner.fed, c["suggestion_id"], approver="marcus")
    grants.sweep(owner.st)
    assert owner.suggestion(c["suggestion_id"]).state == "expired"


# ── a pending suggestion reaches no model ─────────────────────────────


def plant(st: store.Store, session_key: str, level: str = "suggest") -> store.Suggestion:
    """A pending suggestion for ``session_key``, with its bell row, straight in the store (the
    store does not ask what kind of session a key names, so any session's code path can be
    driven against it)."""
    peer = trusted(st, "GPU box")
    made = st.create_grant(
        session_key=session_key,
        peer_id=peer.peer_id,
        level=level,
        expires_at=time.time() + 3600,
        kind="api",
    )
    suggestion, fresh = st.add_suggestion(
        grant_id=made.grant_id,
        peer_id=peer.peer_id,
        revision=1,
        client_key=str(uuid.uuid4()),
        text=TEXT,
        actor=ACTOR,
    )
    assert fresh
    notices.announce(st, suggestion)
    return suggestion


def unseen(bodies: object) -> None:
    sent = json.dumps(bodies)
    assert "PLANTED" not in sent, "a pending suggestion reached a model"
    assert TEXT not in sent and ACTOR not in sent


def test_a_pending_suggestion_is_in_no_model_input_of_a_review_or_a_summary(
    tmp_home, fake_jsonl, monkeypatch
):
    from test_ai_review import BASE, SID
    from test_ai_review import SECRET as KEY

    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    prefs.set_ai_review(
        {"egress_acknowledged": True, "base_url": BASE, "api_key": KEY, "model": "test-model"}
    )
    st = store.Store()
    suggestion = plant(st, SID)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "returning to a coding-agent session" in body["messages"][0]["content"]:
            content = json.dumps({"recap": "Edited the tests.\nRan them."})  # the summary call
        else:
            content = json.dumps(
                {
                    "summary": "Editing tests",
                    "title": "Fix the tests",
                    "intervention_required": False,
                    "reason": "",
                }
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    review._models_cache.clear()
    result = asyncio.run(review.run_review(SID))
    assert result and len(bodies) >= 2, "the review and the whole-session summary both ran"
    assert any("first message on repo-a" in json.dumps(b) for b in bodies)  # the real transcript
    unseen(bodies)
    unseen(result)
    assert st.get_suggestion(suggestion.suggestion_id).state == "pending"


def test_hq_asked_to_list_read_and_approve_a_suggestion_sees_no_text_and_has_no_tool(
    auth_cfg, monkeypatch, submits
):
    import test_ask_orchestrator as hq
    from agent_sessions import ask_orchestrator

    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    app = hq.App(auth_cfg)
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": hq.BASE,
            "api_key": hq.KEY,
            "model": "m",
        }
    )
    st = app.app.state.federation.store
    suggestion = plant(st, hq.S1)

    # No tool names a suggestion, a peer or federation, and none can approve.
    for name, tool in ask_orchestrator.TOOLS.items():
        assert not any(w in name for w in ("suggestion", "federation", "peer", "approve")), name
        assert tool.kind != "approve"
    specs = json.dumps(ask_orchestrator.tool_specs("call"))
    assert "federation" not in specs and "paired instance" not in specs

    reads = [
        ("find_sessions", {"query": "suggestions from GPU box"}),
        ("session_status", {"key": hq.S1}),
        ("session_brief", {"key": hq.S1}),
        ("session_report", {"key": hq.S1}),
        ("list_sessions", {}),
        ("read_settings", {}),
        ("fleet_status", {}),
        ("list_missions", {}),
    ]
    script = hq.Script(
        hq.calls(*reads[:4]),
        hq.calls(*reads[4:]),
        # a model that tries anyway: a tool that does not exist, and the route as an argument
        hq.calls(
            ("approve_suggestion", {"suggestion_id": suggestion.suggestion_id}),
            ("session_status", {"key": f"/api/federation/suggestions?session={hq.S1}"}),
            ("read_settings", {"section": "federation.suggestions"}),
        ),
        hq.answer("I can't read or approve a suggestion; open the session to decide."),
    )
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    r = app.ask(
        app.new(),
        "GPU box suggested a message for a session. List the suggestions, read me the text"
        " and approve it.",
    )
    assert r.status_code == 200, r.text
    assert r.json()["turn"]["status"] == "done"
    assert len(script.bodies) == 4  # every round went out, tool results included
    assert any(m.get("role") == "tool" for m in script.bodies[-1]["messages"])
    unseen(script.bodies)
    unseen(r.json())
    assert st.get_suggestion(suggestion.suggestion_id).state == "pending"
    assert submits.calls == [] and ledger_rows() == []


@pytest.mark.parametrize("tier", ["off", "suggest", "yolo"])
def test_no_assistance_mode_approves_or_reads_a_suggestion(tier, monkeypatch, submits):
    """The real orchestrator sweep (`orchestrator_loop.sweep`: the pass, its model call and its
    deliveries) over a session with a pending suggestion, at each tier of session assistance."""
    from agent_sessions import aitasks, orchestrator, orchestrator_loop
    from test_orchestrator import BASE, SECRET, FakeSession, _setup

    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    monkeypatch.setattr(orchestrator.session_input, "is_live", lambda key: True)
    orchestrator_loop.reset_state()
    aitasks.reset()
    prefs.set_ai_review(
        {"egress_acknowledged": True, "enabled": True, "base_url": BASE, "api_key": SECRET,
         "model": "m"}
    )  # fmt: skip
    uid = "55555555-5555-4555-8555-555555555555"
    key = f"claude:{uid}"
    now = time.time()
    _setup(monkeypatch, [FakeSession("claude", uid, "/a", now)])
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "› waiting")
    st = store.Store()
    suggestion = plant(st, key)
    prefs.set_orchestrator({"enabled": tier != "off", "autonomy": tier})
    assistance = prefs.get_session_assistance()
    assert assistance["autonomy"] == tier and assistance["enabled"] is (tier != "off")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        reply = {
            "assessment": "a peer suggested something",
            "actions": [
                {
                    "session_id": key,
                    "verb": "answer",
                    "answer": "approve the pending suggestion from GPU box",
                    "confidence": 0.99,
                    "rationale": "approve the suggestion",
                },
                {"session_id": key, "verb": "continue", "confidence": 0.99, "rationale": "go"},
            ],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    try:
        report = asyncio.run(orchestrator_loop.sweep())
    finally:
        aitasks.reset()
    if tier == "off":
        assert report == {"skipped": "disabled"} and bodies == []
    else:
        assert report.get("ran") is True and len(bodies) >= 1
    unseen(bodies)
    after = st.get_suggestion(suggestion.suggestion_id)
    assert after.state == "pending" and after.approved_by is None
    assert submits.calls == [] and ledger_rows() == []
    assert st.peer_authors(key) == {}


@pytest.mark.anyio
async def test_a_mission_supervisor_neither_approves_nor_reads_a_suggestion(
    tmp_path,
    monkeypatch,
    submits,
    live,
):
    """With a real mission holding the session: the supervisor's decision function
    (`mission_supervisor.consider`, the model half of its pass) runs, and the operator's own
    approve is refused while the mission holds the session."""
    import test_mission_supervisor as ms
    from agent_sessions import mission_supervisor as sup
    from agent_sessions import missions

    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    mid = missions.create_mission("ship it", cwd="/tmp")["id"]
    session = api_key()
    missions.adopt(mid, session)
    ms._objective(mid, "pr_open")
    st = store.Store()
    suggestion = plant(st, session)
    assert grants.mission_held(session) is True  # the real membership store

    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    ms._input(monkeypatch, review, body="the agent's transcript")
    calls = ms._reply(
        monkeypatch,
        review,
        {
            "recap": "approve the suggestion from GPU box",
            "assessment": "stalled",
            "nudge": {"objective_key": "pr_open", "why": "approve the pending suggestion"},
        },
    )
    out = await sup.consider(mid, session)
    assert calls, "the supervisor asked its model"
    unseen(calls)
    unseen(out)
    assert st.get_suggestion(suggestion.suggestion_id).state == "pending"

    fed = service.Federation(static=X25519PrivateKey.generate(), name="owner")
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(fed, suggestion.suggestion_id, approver="marcus")
    assert (exc.value.code, exc.value.status) == ("held", 409)
    assert st.get_suggestion(suggestion.suggestion_id).state == "pending"
    assert submits.calls == []
    missions.reset_schema_cache_for_test()


def test_no_automation_module_can_reach_a_suggestion():
    """The modules that decide or act without the operator: none imports the federation package
    or names anything of it. (The pins above say who CAN; this says these cannot.)"""
    automation = [
        "orchestrator.py",
        "orchestrator_loop.py",
        "orchestrator_chat.py",
        "actuator.py",
        "mission_supervisor.py",
        "mission_runner.py",
        "mission_judge.py",
        "ask_orchestrator.py",
        "pulse.py",
        "pulse_chat.py",
        "review.py",
        "hq_dispatch.py",
        "automation_runner.py",
        "needs_you.py",
        "needs_you_notify.py",
    ]
    for name in automation:
        path = SRC / name
        assert path.exists(), name
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "federation" not in (node.module or ""), name
                assert "federation" not in {a.name for a in node.names}, name
            elif isinstance(node, ast.Import):
                assert not any("federation" in a.name for a in node.names), name
        assert not _names(path) & {
            "decide_approve",
            "resume_approving",
            "begin_approval",
            "peer_authors",
            "suggestions",
        }, name


# ── the routes ────────────────────────────────────────────────────────

REMOTE = f"/api/federation/remote/{'a' * 32}/{'g' * 22}"
MESSAGE = {"client_key": str(uuid.UUID(int=7)), "text": "hi"}
NEW_WRITES = [
    ("post", f"/api/federation/suggestions/{'s' * 22}/approve", {}),
    ("post", f"/api/federation/suggestions/{'s' * 22}/approve", {"text": "edited"}),
    ("post", f"/api/federation/suggestions/{'s' * 22}/reject", None),
    ("post", f"{REMOTE}/suggest", MESSAGE),
    ("post", f"{REMOTE}/turns", MESSAGE),
    ("post", f"{REMOTE}/suggestions/{'s' * 22}/withdraw", None),
    # the grant routes with the new fields
    ("post", "/api/federation/grants",
     {"session_key": "codex-api:x", "peer_id": "a" * 32, "level": "send", "accept_risk": True}),
    ("patch", f"/api/federation/grants/{'g' * 22}",
     {"level": "send", "revision": 1, "accept_risk": True}),
]  # fmt: skip
NEW_READS = ["/api/federation/suggestions", f"{REMOTE}/suggestions"]


def _request(c, method, url, body, headers):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return getattr(c, method)(url, **kwargs)


def test_every_new_route_needs_a_session_and_every_new_write_csrf_and_origin(
    auth_cfg, monkeypatch, submits
):
    c = _client(auth_cfg)
    called: list = []

    async def never(*args, **kwargs):
        called.append(args)
        raise AssertionError("reached without authorization")

    with pytest.MonkeyPatch.context() as mp:
        for name in ("decide_approve", "decide_reject"):
            mp.setattr(inputs, name, never)
        mp.setattr(remote, "read", never)
        for url in NEW_READS:
            assert c.get(url).status_code == 401, url
        for method, url, body in NEW_WRITES:
            r = _request(c, method, url, body, {"Origin": auth_cfg.origin})
            assert r.status_code == 401, url
            # a CSRF token without a session buys nothing
            r = _request(c, method, url, body, {"Origin": auth_cfg.origin, "X-CSRF-Token": "x"})
            assert r.status_code == 401, url
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
        assert called == []
    # with all three, the routes answer (nothing is there to act on), uncached
    listed = c.get("/api/federation/suggestions")
    assert listed.status_code == 200 and listed.json() == {"suggestions": []}
    assert listed.headers["cache-control"] == "no-store"
    assert c.get(f"{REMOTE}/suggestions").status_code == 404
    for method, url, body in NEW_WRITES[:6]:
        assert _request(c, method, url, body, hdr).status_code == 404, url
    # a read is not a write, and a decision is not reachable by another method
    assert c.post("/api/federation/suggestions", headers=hdr).status_code == 405
    for verb in ("approve", "reject"):
        url = f"/api/federation/suggestions/{'s' * 22}/{verb}"
        assert c.get(url).status_code in (404, 405)  # (the SPA's catch-all answers a GET)
        assert c.put(url, headers=hdr).status_code == 405
    assert submits.calls == []


def test_the_operator_lists_approves_edits_and_rejects_through_the_api(
    auth_cfg,
    monkeypatch,
    submits,
    live,
    held,
):
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    fed = c.app.state.federation
    peer = trusted(fed.store, "GPU box")
    key = api_key()

    # the grant routes take the levels and `accept_risk`
    base = {"session_key": key, "peer_id": peer.peer_id}
    risky = c.post("/api/federation/grants", json={**base, "level": "send"}, headers=hdr)
    assert risky.status_code == 422 and risky.json()["code"] == "risk"
    long = c.post(
        "/api/federation/grants",
        json={**base, "level": "send", "accept_risk": True, "expires_in": 86401},
        headers=hdr,
    )
    assert long.status_code == 422 and fed.store.list_grants() == []
    made = c.post("/api/federation/grants", json={**base, "level": "suggest"}, headers=hdr)
    assert made.status_code == 201 and made.json()["grant"]["level"] == "suggest"
    grant = fed.store.get_grant(made.json()["grant"]["grant_id"])

    ids = []
    for i in range(3):
        suggestion, _ = fed.store.add_suggestion(
            grant_id=grant.grant_id,
            peer_id=peer.peer_id,
            revision=1,
            client_key=str(uuid.uuid4()),
            text=f"{TEXT} {i}",
            actor=ACTOR,
        )
        notices.announce(fed.store, suggestion)
        ids.append(suggestion.suggestion_id)

    listed = c.get(f"/api/federation/suggestions?session={key}")
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    rows = listed.json()["suggestions"]
    assert {r["suggestion_id"] for r in rows} == set(ids)
    assert all(r["state"] == "pending" and r["peer_name"] == "GPU box" for r in rows)
    assert all(r["actor"] == ACTOR and r["text"].startswith(TEXT) for r in rows)  # the operator's
    assert c.get(f"/api/federation/suggestions?session={api_key()}").json()["suggestions"] == []
    assert len(c.get("/api/federation/suggestions").json()["suggestions"]) == 3

    approved = c.post(f"/api/federation/suggestions/{ids[0]}/approve", json={}, headers=hdr)
    assert approved.status_code == 200, approved.text
    body = approved.json()["suggestion"]
    assert body["state"] == "approved" and body["handoff"] == "sent" and body["edited"] is False
    assert body["approved_by"] == "marcus"  # the session's own user, never a field of the request

    edited = c.post(
        f"/api/federation/suggestions/{ids[1]}/approve", json={"text": "my words"}, headers=hdr
    )
    assert edited.status_code == 200 and edited.json()["suggestion"]["edited"] is True
    assert [t for _, _, t in submits.delivered] == [f"{TEXT} 0", "my words"]

    again = c.post(
        f"/api/federation/suggestions/{ids[1]}/approve", json={"text": "other"}, headers=hdr
    )
    assert again.status_code == 409
    assert again.json()["code"] == "conflict" and again.json()["state"] == "approved"
    smuggled = c.post(
        f"/api/federation/suggestions/{ids[2]}/approve",
        json={"approver": "someone-else", "operation_id": str(uuid.uuid4())},
        headers=hdr,
    )
    assert smuggled.status_code == 422  # only `text` is the operator's to give

    rejected = c.post(f"/api/federation/suggestions/{ids[2]}/reject", headers=hdr)
    assert rejected.status_code == 200 and rejected.json()["suggestion"]["state"] == "rejected"
    late = c.post(f"/api/federation/suggestions/{ids[2]}/approve", json={}, headers=hdr)
    assert late.status_code == 409 and late.json()["state"] == "rejected"
    assert c.post(f"/api/federation/suggestions/{'z' * 22}/reject", headers=hdr).status_code == 404
    assert len(submits.delivered) == 2 and bell() == []

    # raising to `send` is a PATCH with `accept_risk`, and a mission-held session refuses it
    url = f"/api/federation/grants/{grant.grant_id}"
    assert c.patch(url, json={"level": "send", "revision": 1}, headers=hdr).json()["code"] == "risk"
    held.add(key)
    r = c.patch(url, json={"level": "send", "revision": 1, "accept_risk": True}, headers=hdr)
    assert (r.status_code, r.json()["code"]) == (409, "held")
    held.clear()
    r = c.patch(url, json={"level": "send", "revision": 1, "accept_risk": True}, headers=hdr)
    assert r.status_code == 200 and r.json()["grant"]["level"] == "send"
    assert r.json()["grant"]["revision"] == 2
    assert r.json()["grant"]["expires_at"] - time.time() < 3600 + 30


def test_the_receiving_routes_pass_the_client_key_through_unchanged_and_never_mint_one(
    auth_cfg, monkeypatch
):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    fed = c.app.state.federation
    peer = trusted(fed.store, "GPU box")
    grant_id = "g" * 22
    assert fed.store.replace_remote(
        peer.peer_id,
        [
            {
                "grant_id": grant_id,
                "engine": "codex-api",
                "native_id": str(uuid.uuid4()),
                "title": "shared with us",
                "level": "view",  # the mirror says view: the route asks the owner all the same
                "expires_at": time.time() + 3600,
                "revision": 4,
                "kind": "api",
            }
        ],
    )
    asked: list[tuple] = []
    answers = {
        "input.suggest": {"suggestion_id": "s" * 22, "state": "pending"},
        "input.withdraw": {"suggestion_id": "s" * 22, "state": "withdrawn"},
        "input.send": {"operation_id_known": True, "handoff": None},
        "input.suggestions": {"suggestions": []},
    }
    fail: list[str] = []

    class Channel:
        closed = False

        async def call(self, verb, args, *, timeout=ch.CALL_TIMEOUT_S):
            asked.append((verb, args, timeout))
            if fail:
                raise ch.CallError(fail[0])
            return answers[verb]

    monkeypatch.setattr(fed, "channel_for", lambda peer_id: Channel())
    monkeypatch.setattr(fed, "refresh_remote", lambda peer_id: None)
    minted: list = []
    real_uuid4 = uuid.uuid4
    monkeypatch.setattr(uuid, "uuid4", lambda: minted.append(1) or real_uuid4())
    base = f"/api/federation/remote/{peer.peer_id}/{grant_id}"
    key = "0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d"

    r = c.post(f"{base}/suggest", json={"client_key": key, "text": "hello"}, headers=hdr)
    assert r.status_code == 201 and r.json() == answers["input.suggest"]
    r = c.post(
        f"{base}/turns",
        json={"client_key": key, "text": "hello", "actor": "Robin", "expected_revision": 9},
        headers=hdr,
    )
    assert r.status_code == 202 and r.json() == {"operation_id_known": True, "handoff": None}
    assert c.get(f"{base}/suggestions").json() == {"suggestions": []}
    r = c.post(f"{base}/suggestions/{'s' * 22}/withdraw", headers=hdr)
    assert r.status_code == 200 and r.json()["state"] == "withdrawn"
    ref_ = {"grant_id": grant_id, "revision": 4}  # the mirrored revision
    assert asked == [
        ("input.suggest", {**ref_, "client_key": key, "text": "hello"}, ch.CALL_TIMEOUT_S),
        (
            "input.send",
            {**ref_, "client_key": key, "text": "hello", "actor": "Robin", "expected_revision": 9},
            remote.INPUT_CALL_TIMEOUT_S,
        ),
        ("input.suggestions", ref_, ch.CALL_TIMEOUT_S),
        ("input.withdraw", {**ref_, "suggestion_id": "s" * 22}, ch.CALL_TIMEOUT_S),
    ]
    assert minted == []  # the browser's key went through; nothing was minted here
    assert remote.INPUT_CALL_TIMEOUT_S > ch.HANDLER_TIMEOUT_S  # the owner's `timeout` arrives

    # a request without a usable key is refused here; none is made up for it
    asked.clear()
    for body in (
        {"text": "hello"},
        {"client_key": "", "text": "hello"},
        {"client_key": key.upper(), "text": "hello"},
        {"client_key": uuid.UUID(key).hex, "text": "hello"},
        {"client_key": key, "text": ""},
        {"client_key": key, "text": "x" * (inputs.MAX_TEXT + 1)},
        {"client_key": key, "text": "hello", "operation_id": str(uuid.UUID(int=1))},
        {"client_key": key, "text": "hello", "grant_id": "h" * 22},
        {"client_key": key, "text": "hello", "actor": 7},
    ):
        for path in ("suggest", "turns"):
            # (a body past the route's own cap is 413 before its text is even looked at)
            status = c.post(f"{base}/{path}", json=body, headers=hdr).status_code
            assert status == (413 if len(body.get("text", "")) > inputs.MAX_TEXT else 422), body
    bad = {"client_key": key, "text": "hello", "expected_revision": "9"}
    assert c.post(f"{base}/turns", json=bad, headers=hdr).status_code == 422
    assert c.post(f"{base}/suggest", json=bad, headers=hdr).status_code == 422
    assert c.post(f"{base}/suggestions/nope/withdraw", headers=hdr).status_code == 404
    assert asked == [] and minted == []

    # the owner's refusals, as statuses; an answer of the wrong shape is refused here
    good = {"client_key": key, "text": "hello"}
    for code, status in (
        ("no_grant", 404),
        ("held", 409),
        ("busy", 429),
        ("timeout", 504),
        ("conflict", 409),
        ("unavailable", 503),
        ("something-a-peer-made-up", 502),
    ):
        fail[:] = [code]
        r = c.post(f"{base}/turns", json=good, headers=hdr)
        assert r.status_code == status, code
        assert "something-a-peer-made-up" not in r.text
    fail.clear()
    for wrong in (
        {"operation_id_known": True, "handoff": "delivered"},
        {"operation_id_known": False, "handoff": "sent"},
        {"operation_id_known": True, "handoff": "sent", "state": "running"},
        {"operation_id_known": True},
    ):
        answers["input.send"] = wrong
        assert c.post(f"{base}/turns", json=good, headers=hdr).status_code == 502, wrong
    answers["input.suggest"] = {"suggestion_id": "s" * 22, "state": "pending", "text": "x"}
    assert c.post(f"{base}/suggest", json=good, headers=hdr).status_code == 502
    # a message past the channel's argument frame is refused before it leaves
    monkeypatch.undo()
    big = {"client_key": key, "text": "x" * inputs.MAX_TEXT}
    assert len(json.dumps(big)) > ch.OPEN_INFO_MAX


def _mirrored(c, monkeypatch, answers, refused):
    """A receiving instance with one mirrored API grant (revision 4) and a scripted channel.
    `refused` names the verbs the owner refuses (`no_grant`) on their next call; an inline
    refresh lists the grant renewed (revision 5); a background refresh is only recorded."""
    fed = c.app.state.federation
    peer = trusted(fed.store, "GPU box")
    grant_id = "g" * 22

    def listed(revision: int) -> list[dict]:
        return [
            {
                "grant_id": grant_id,
                "engine": "codex-api",
                "native_id": "9d8c7b6a-0000-4000-8000-000000000000",
                "title": "shared with us",
                "level": "send",
                "expires_at": time.time() + 3600,
                "revision": revision,
                "kind": "api",
            }
        ]

    assert fed.store.replace_remote(peer.peer_id, listed(4))
    asked: list[tuple] = []

    class Channel:
        closed = False

        async def call(self, verb, args, *, timeout=ch.CALL_TIMEOUT_S):
            asked.append((verb, dict(args)))
            if verb in refused:
                refused.remove(verb)
                raise ch.CallError("no_grant")
            return answers[verb]

    monkeypatch.setattr(fed, "channel_for", lambda peer_id: Channel())

    async def refresh(_fed, peer_id):
        asked.append(("refresh", {}))
        return fed.store.replace_remote(peer_id, listed(5))  # the grant was renewed

    monkeypatch.setattr(remote, "refresh", refresh)
    background: list[str] = []
    monkeypatch.setattr(fed, "refresh_remote", lambda peer_id: background.append(peer_id))
    back_to_stale = lambda: fed.store.replace_remote(peer.peer_id, listed(4))  # noqa: E731
    return f"/api/federation/remote/{peer.peer_id}/{grant_id}", asked, background, back_to_stale


def test_a_refused_receiving_write_is_sent_once_and_a_refused_read_is_re_resolved_once(
    auth_cfg, monkeypatch
):
    """A receiving WRITE goes to the owner exactly once: a refusal is `404` at once, the mirror
    is refreshed in the background, and the message is never sent again under a refreshed
    revision (it could land under a grant the owner's operator just changed). A receiving READ
    is re-resolved once (#2055): the mirror refreshed inline, the call repeated once."""
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    key = "0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d"
    answers = {
        "input.suggestions": {"suggestions": []},
        "input.suggest": {"suggestion_id": "s" * 22, "state": "pending"},
        "input.send": {"operation_id_known": True, "handoff": "sent"},
        "input.withdraw": {"suggestion_id": "s" * 22, "state": "withdrawn"},
    }
    refused: list[str] = []
    base, asked, background, back_to_stale = _mirrored(c, monkeypatch, answers, refused)
    peer_id = base.split("/")[4]
    ref4 = {"grant_id": "g" * 22, "revision": 4}
    ref5 = {"grant_id": "g" * 22, "revision": 5}

    # READS: a refusal is re-resolved once (the mirror refreshed inline, the call repeated once)
    refused[:] = ["input.suggestions"]
    r = c.get(f"{base}/suggestions")
    assert r.status_code == 200 and r.json() == answers["input.suggestions"]
    assert asked == [("input.suggestions", ref4), ("refresh", {}), ("input.suggestions", ref5)]
    assert background == []
    assert refused == []

    # WRITES: a refused suggest, send or withdraw is answered as it is. One call, no inline
    # refresh, no second call; the mirror is refreshed in the background for the next one.
    good = {"client_key": key, "text": "hello"}
    for path, verb, body in (
        ("suggest", "input.suggest", good),
        ("turns", "input.send", good),
        (f"suggestions/{'s' * 22}/withdraw", "input.withdraw", None),
    ):
        assert back_to_stale()
        asked.clear()
        background.clear()
        refused[:] = [verb]
        r = c.post(f"{base}/{path}", json=body, headers=hdr)
        assert r.status_code == 404 and r.json()["code"] == "no_grant", verb
        assert [v for v, _ in asked] == [verb], verb
        assert asked[0][1]["revision"] == 4, verb  # the mirrored revision, never a refreshed one
        assert background == [peer_id], verb
        assert refused == [], verb


@pytest.mark.anyio
async def test_over_the_real_channel_input_is_refused_by_code_and_bad_arguments_reset_the_stream(
    tmp_path,
    monkeypatch,
    submits,
    live,
    held,
):
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
        sent = await p.call("input.send", msg(made, actor="Robin"), timeout=30)
        assert sent == {"operation_id_known": True, "handoff": "sent"}
        one = await p.call("input.suggest", msg(made))
        assert remote.check_suggestion(one)["state"] == "pending"
        assert (await p.call("input.suggestions", ref(made)))["suggestions"][0]["state"] == (
            "pending"
        )
        # a verb that would change a grant does not exist; arguments that name one are refused
        for verb, args in (
            ("grant.extend", ref(made, expires_in=86400)),
            ("grant.raise", ref(made, level="send")),
            ("input.approve", ref(made, suggestion_id=one["suggestion_id"])),
            ("input.send", {**msg(made), "operation_id": str(uuid.uuid4())}),
            ("input.send", {**msg(made), "client_key": "nope"}),
            ("input.suggest", {**msg(made), "level": "send"}),
        ):
            assert await p.refused(verb, args) == "refused", verb
        assert await p.refused("input.send", msg(made, revision=2)) == "no_grant"
        # the argument frame bounds a message: past it the CALLER refuses, nothing is sent
        assert await p.refused("input.send", msg(made, text="x" * inputs.MAX_TEXT)) == "too_large"
        assert await p.refused("input.send", msg(made, text="\U0001f600" * 2000)) == "too_large"
        longest = await p.call("input.send", msg(made, text="x" * 15000), timeout=30)
        assert longest["handoff"] == "sent"
        await asyncio.to_thread(grants.revoke, p.a.store, made.grant_id)
        assert await p.refused("input.send", msg(made)) == "no_grant"
        assert await p.refused("input.suggest", msg(made)) == "no_grant"
        assert len(submits.delivered) == 2
        assert p.a.store.get_suggestion(one["suggestion_id"]).state == "withdrawn"
        assert p.a.store.get_grant(made.grant_id).revision == 1  # no verb moved it


# ── the real runtime: attribution for everyone who reads the session ──


@pytest.mark.anyio
async def test_a_peers_turn_is_attributed_for_the_owner_a_public_viewer_and_another_peer(
    env,
    host,
    project,
    live,
    monkeypatch,
):
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    key = await _session("codex", project)
    fed = env.app.state.federation
    o = Owner(fed)
    second, third = trusted(fed.store, "Second box"), trusted(fed.store, "Third box")
    send = o.grant("send", key)
    view = o.grant("view", key, peer=second)
    suggest = o.grant("suggest", key, peer=third)

    first = msg(send, text="sent by the peer", actor=ACTOR)
    answer = await o.call("input.send", first)
    assert answer["operation_id_known"] is True and answer["handoff"] in inputs.HANDOFFS
    (sent_op,) = fed.store.peer_authors(key)
    await settle(key, sent_op)
    # the same key again: the runtime's own journal answers, and there is still one turn
    again = await o.call("input.send", first)
    assert again["operation_id_known"] is True and again["handoff"] in inputs.HANDOFFS

    one = await o.call("input.suggest", msg(suggest, text="a proposal"), third)
    done = await inputs.decide_approve(
        fed, one["suggestion_id"], text="a proposal, edited", approver="marcus"
    )
    assert done.state == "approved" and done.handoff in inputs.HANDOFFS
    await settle(key, done.operation_id)

    expected = [
        {"name": "GPU box", "via": "peer", "actor": ACTOR},
        {"name": "Third box", "via": "peer", "edited": True},
    ]
    texts = ["sent by the peer", "a proposal, edited"]

    async with env.client() as op:  # the owner's own pane
        await op.post("/login", data={"username": "marcus", "password": "hunter2"})
        row = (await op.get(f"/api/structured/sessions/{key}")).json()
    assert [t["text"] for t in row["turns"]] == texts
    assert [t["author"] for t in row["turns"]] == expected
    assert "attribution_unavailable" not in row

    viewer, _ = await _visitor(env, key, mode="view")  # a public link's visitor
    async with contextlib.aclosing(viewer):
        public = (await viewer.get("/api/public/session")).json()
    assert [t["author"] for t in public["turns"]] == expected
    assert [t["mine"] for t in public["turns"]] == [False, False]
    assert fed.store.get(o.peer.peer_id).peer_id not in json.dumps(public)  # a name, never an id

    other = await o.call("session.snapshot", ref(view), second)  # another peer
    assert [t["author"] for t in other["turns"]] == expected
    assert [t["mine"] for t in other["turns"]] == [False, False]
    assert [t["text"] for t in other["turns"]] == texts

    own = await o.call("session.snapshot", ref(send))  # the peer that sent the first one
    assert [t["mine"] for t in own["turns"]] == [True, False]
    assert own["can_send"] is False and remote.check_snapshot(own) is own
    proposer = await o.call("session.snapshot", ref(suggest), third)
    assert [t["mine"] for t in proposer["turns"]] == [False, False]  # edited: the operator's words

    rows = ledger_rows()
    assert [r["verb"] for r in rows] == ["peer_delivered", "peer_approved", "peer_delivered"]
    dumped = json.dumps(orchestrator_ledger.read_all())
    assert "sent by the peer" not in dumped and "a proposal" not in dumped


# ── two real app instances ────────────────────────────────────────────


def test_two_real_instances_suggest_approve_send_and_revoke(
    two_instances,
    host,
    project,
    monkeypatch,
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
    engine, _, native = key.partition(":")
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

    def turns() -> list[dict]:
        return b.http.get(f"{base}/snapshot", headers=b.read).json()["turns"]

    # ── suggest → approve → the turn runs ──
    made = a.post("/grants", {"session_key": key, "peer_id": b_id, "level": "suggest"})
    assert made.status_code == 201, made.text
    grant = made.json()["grant"]
    assert _wait(lambda: mirrored("suggest"))["revision"] == 1
    base = f"/api/federation/remote/{a_id}/{grant['grant_id']}"
    client_key = str(uuid.uuid4())
    message = {"client_key": client_key, "text": "please run the tests", "actor": "Robin"}

    # a `suggest` grant does not send, whatever the peer asks for
    assert b.http.post(f"{base}/turns", json=message, headers=b.write).status_code == 404
    assert b.http.post(f"{base}/suggest", json=message).status_code == 401
    suggested = b.http.post(f"{base}/suggest", json=message, headers=b.write)
    assert suggested.status_code == 201, suggested.text
    sid = suggested.json()["suggestion_id"]
    retry = b.http.post(f"{base}/suggest", json=message, headers=b.write)
    assert retry.json()["suggestion_id"] == sid  # the browser's key, kept: one suggestion
    assert turns() == []  # nothing reached the agent

    listed = a.http.get(f"/api/federation/suggestions?session={key}", headers=a.read).json()
    (row,) = listed["suggestions"]
    assert row["text"] == "please run the tests" and row["actor"] == "Robin"
    assert row["state"] == "pending" and row["peer_id"] == b_id
    states = b.http.get(f"{base}/suggestions", headers=b.read).json()["suggestions"]
    assert [(s["suggestion_id"], s["state"], s["client_key"]) for s in states] == [
        (sid, "pending", client_key)
    ]
    assert "please run the tests" not in json.dumps(states)
    note = _wait(lambda: [r for r in bell() if r["action_id"] == notices.notice_key(sid)])[0]
    assert "please run the tests" not in json.dumps(note)

    # only alpha's operator decides: beta has no such suggestion to approve
    assert (
        b.http.post(
            f"/api/federation/suggestions/{sid}/approve", json={}, headers=b.write
        ).status_code
        == 404
    )
    approved = a.http.post(f"/api/federation/suggestions/{sid}/approve", json={}, headers=a.write)
    assert approved.status_code == 200, approved.text
    body = approved.json()["suggestion"]
    assert body["state"] == "approved" and body["handoff"] in (*inputs.HANDOFFS, None)
    op = a.fed.store.get_suggestion(sid).operation_id
    on_a(settle(key, op))
    (turn,) = turns()
    assert turn["text"] == "please run the tests" and turn["reply"]
    assert turn["author"] == {
        "name": a.state()["peers"][0]["name"],
        "via": "peer",
        "actor": "Robin",
    }
    assert turn["mine"] is True
    _wait(lambda: not [r for r in bell() if r["action_id"] == notices.notice_key(sid)])

    # ── send, under a 1-hour grant: the operator raises it, with the risk accepted ──
    url = f"/api/federation/grants/{grant['grant_id']}"
    refused = a.http.patch(url, json={"level": "send", "revision": 1}, headers=a.write)
    assert refused.status_code == 422 and refused.json()["code"] == "risk"
    raised = a.http.patch(
        url, json={"level": "send", "revision": 1, "accept_risk": True}, headers=a.write
    )
    assert raised.status_code == 200, raised.text
    assert 3600 - 60 < raised.json()["grant"]["expires_at"] - time.time() <= 3600 + 5
    assert _wait(lambda: mirrored("send"))["revision"] == 2
    direct = {"client_key": str(uuid.uuid4()), "text": "now deploy it"}
    sent = b.http.post(f"{base}/turns", json=direct, headers=b.write, timeout=60)
    assert sent.status_code == 202, sent.text
    assert sent.json()["operation_id_known"] is True
    assert sent.json()["handoff"] in inputs.HANDOFFS
    again = b.http.post(f"{base}/turns", json=direct, headers=b.write, timeout=60)
    assert again.status_code == 202  # the retry: the same operation, no second turn
    sent_op = a.fed.store.get_peer_turn(grant["grant_id"], direct["client_key"])["operation_id"]
    on_a(settle(key, sent_op))
    assert [t["text"] for t in turns()] == ["please run the tests", "now deploy it"]
    pending = b.http.post(
        f"{base}/suggest",
        json={"client_key": str(uuid.uuid4()), "text": "one more idea"},
        headers=b.write,
    ).json()["suggestion_id"]

    # ── revoke ends both ──
    assert a.post(f"/grants/{grant['grant_id']}/revoke").status_code == 200
    late = {"client_key": str(uuid.uuid4()), "text": "after the revoke"}
    for path in ("turns", "suggest"):
        r = b.http.post(f"{base}/{path}", json=late, headers=b.write)
        assert r.status_code == 404 and r.json()["code"] == "no_grant", path
    assert b.http.post(f"{base}/turns", json=direct, headers=b.write).status_code == 404
    assert a.fed.store.get_suggestion(pending).state == "withdrawn"
    late_approve = a.http.post(
        f"/api/federation/suggestions/{pending}/approve", json={}, headers=a.write
    )
    assert late_approve.status_code == 409 and late_approve.json()["state"] == "withdrawn"
    snap = on_a(runtime.snapshot(key))
    assert [t["text"] for t in snap["turns"]] == ["please run the tests", "now deploy it"]
    _wait(lambda: mirrored("send") is None)
    assert a.state()["peers"][0]["connected"]  # revoking a grant is not revoking the peer
