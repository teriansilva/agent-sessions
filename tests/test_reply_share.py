"""Reply links (#1956): one finished reply of an API session, sealed, read only, on any domain.

Pinned here: the snapshot carries the reply and a tool COUNT and nothing else; secrets are
redacted; the seal round-trips with and without a password, refuses a wrong key / password and a
moved ``expires_at``; the public routes hand out ciphertext only, cookie-less, with one 404 for
every ending; ``AUTH_MODE=none`` serves nothing; archiving a session ends its reply links; every
API client's real reply can be shared.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from agent_sessions import reply_share, share_api, shares, template_secrets
from agent_sessions import structured_runtime as runtime
from agent_sessions.main import create_app
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401 — fixtures

SECRET = "s3cret-value-123"
FIXTURE = (
    Path(__file__).resolve().parents[1] / "web" / "src" / "reply" / "fixtures" / "envelopes.json"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _redactor(secrets=(SECRET,)):
    return share_api._Redactor(list(secrets))


def _turn(**over) -> dict:
    t = {
        "turn_id": "t1",
        "state": "completed",
        "text": "operator prompt with /home/op/secret-project",
        "reply": f"The fix: rotate {SECRET} and restart.",
        "reply_ts": 1_800_000_000,
        "reply_truncated": False,
        "tools": [
            {"name": "shell", "outcome": "ok", "summary": "cat /home/op/secret-project/.env"},
            {"name": "apply_patch", "outcome": "ok", "summary": "src/auth.py"},
        ],
        "tools_truncated": True,
        "attachments": [{"stored": "20261012-101010-a.png", "name": "a.png", "mime": "image/png"}],
    }
    t.update(over)
    return t


# ---- the snapshot ----------------------------------------------------------------------------


def test_the_snapshot_is_the_reply_and_a_tool_count_nothing_else():
    body = reply_share.content(_turn(), "Codex", _redactor())
    assert set(body) == {"v", "agent", "reply", "ts", "tools_hidden", "tools_more"}
    assert body["reply"] == "The fix: rotate [secret] and restart."
    assert body["tools_hidden"] == 2 and body["tools_more"] is True
    flat = json.dumps(body)
    for leak in (
        SECRET,
        "shell",
        "apply_patch",
        "cat /home",
        "src/auth.py",
        "operator prompt",
        "secret-project",
        "a.png",
        "20261012",
    ):
        assert leak not in flat, leak


@pytest.mark.parametrize(
    "state", ["running", "queued", "failed", "interrupted", "awaiting_approval"]
)
def test_only_a_finished_reply_can_be_shared(state):
    with pytest.raises(reply_share.ReplyShareError):
        reply_share.content(_turn(state=state), "Codex", _redactor())


def test_a_turn_without_a_reply_is_refused():
    with pytest.raises(reply_share.ReplyShareError):
        reply_share.content(_turn(reply="   "), "Codex", _redactor())


def test_a_cut_reply_is_trimmed_and_marked_like_a_public_link_shows_it():
    long = "x" * 5000 + SECRET
    body = reply_share.content(_turn(reply=long, reply_truncated=True), "Codex", _redactor())
    assert body["reply"].startswith(share_api.CUT_MARK) and body["reply"].endswith(
        share_api.CUT_MARK
    )
    assert SECRET not in body["reply"]


# ---- the seal --------------------------------------------------------------------------------


def _body():
    return reply_share.content(_turn(), "Claude", _redactor())


def test_seal_round_trips_and_the_key_is_only_in_the_link():
    env, k = reply_share.seal(_body(), share_id="AAAAAAAAAAAAAAAAAAAAAA", expires_at=1_900_000_000)
    assert reply_share.open_envelope(env, k) == _body()
    flat = json.dumps(env)
    assert k not in flat and "rotate" not in flat and "Claude" not in flat
    assert env["password"] is False and env["salt"] is None
    assert len(reply_share.unb64u(k)) == 32


def test_a_password_is_a_second_factor():
    env, k = reply_share.seal(
        _body(), share_id="AAAAAAAAAAAAAAAAAAAAAA", expires_at=None, password="hunter2"
    )
    assert env["password"] is True and env["salt"]
    assert reply_share.open_envelope(env, k, "hunter2") == _body()
    with pytest.raises(InvalidTag):
        reply_share.open_envelope(env, k, "hunter3")
    with pytest.raises(InvalidTag):
        reply_share.open_envelope(env, reply_share.b64u(b"\0" * 32), "hunter2")


def test_the_id_and_expiry_are_bound_into_the_ciphertext():
    env, k = reply_share.seal(_body(), share_id="AAAAAAAAAAAAAAAAAAAAAA", expires_at=1_900_000_000)
    for field, value in (("expires_at", 2_000_000_000), ("expires_at", None), ("id", "B" * 22)):
        moved = {**env, field: value}
        with pytest.raises(InvalidTag):
            reply_share.open_envelope(moved, k)


def test_an_oversized_reply_is_refused():
    body = {**_body(), "reply": "y" * (reply_share.MAX_ENVELOPE_BYTES + 1)}
    with pytest.raises(reply_share.ReplyShareError):
        reply_share.seal(body, share_id="AAAAAAAAAAAAAAAAAAAAAA", expires_at=None)


def test_expiry_choices():
    assert reply_share.expiry("never", now=100) is None
    assert reply_share.expiry("1h", now=100) == 3700
    assert reply_share.expiry("7d", now=0) == 7 * 86400
    for bad in ("2d", "", None, 5):
        with pytest.raises(reply_share.ReplyShareError):
            reply_share.expiry(bad)


def test_the_browser_fixture_opens_with_this_implementation():
    """`web/src/reply/fixtures/envelopes.json` is sealed by this module and opened by
    `web/src/reply/seal.ts` in vitest: both sides of the format are held to one file."""
    fx = json.loads(FIXTURE.read_text())
    for case in fx["cases"]:
        got = reply_share.open_envelope(case["envelope"], case["key"], case.get("password"))
        assert got == case["content"], case["name"]


# ---- routes ----------------------------------------------------------------------------------


class _Env:
    def __init__(self, app, cfg):
        self.app, self.cfg = app, cfg

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://testserver",
            headers={"Origin": self.cfg.origin},
        )

    async def operator(self) -> httpx.AsyncClient:
        op = self.client()
        await op.post("/login", data={"username": "marcus", "password": "hunter2"})
        op.headers["X-CSRF-Token"] = (await op.get("/api/config")).json()["csrf"]
        return op


@pytest.fixture
def env(auth_cfg):
    return _Env(create_app(auth_cfg), auth_cfg)


async def _finished_turn(source, work, text="hello") -> tuple[str, str]:
    snap = await runtime.create_session(ENGINES[source], str(work), operation_id=ident())
    key = snap["session_key"]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=text)
    _, t = await settle(key, turn)
    assert t["state"] == "completed", t
    return key, turn


@pytest.mark.anyio
@pytest.mark.parametrize("source", sorted(ENGINES))
async def test_every_api_client_s_reply_can_be_shared_and_read_back(env, host, project, source):  # noqa: F811
    key, turn = await _finished_turn(source, project)
    op = await env.operator()
    async with contextlib.aclosing(op):
        r = await op.post(
            "/api/reply-shares", json={"session": key, "turn_id": turn, "expires_in": "1d"}
        )
        assert r.status_code == 201, r.text
        made = r.json()
        assert made["path"] == f"/r/{made['share']['share_id']}"
        listed = (await op.get(f"/api/reply-shares?session={key}")).json()["shares"]
        assert [s["share_id"] for s in listed] == [made["share"]["share_id"]]
        assert made["key"] not in json.dumps(listed)  # the key is shown once, never listed
        assert not any("envelope" in row or "key" in row for row in listed)
    async with env.client() as visitor:
        page = await visitor.get(made["path"])
        assert page.status_code == 200
        r = await visitor.get(f"/api/public/reply/{made['share']['share_id']}")
        assert r.status_code == 200
        assert "set-cookie" not in {k.lower() for k in r.headers}
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
    body = reply_share.open_envelope(r.json(), made["key"])
    assert body["reply"] == "echo:hello"
    assert body["tools_hidden"] == 0
    assert "hello" not in r.text  # ciphertext only on the wire


@pytest.mark.anyio
async def test_a_command_s_steps_are_counted_never_shown(env, host, project):  # noqa: F811
    key, turn = await _finished_turn("codex", project, text="RUN:printenv SUPER_SECRET_TOKEN")
    op = await env.operator()
    async with contextlib.aclosing(op):
        made = (await op.post("/api/reply-shares", json={"session": key, "turn_id": turn})).json()
    async with env.client() as visitor:
        r = await visitor.get(f"/api/public/reply/{made['share']['share_id']}")
    body = reply_share.open_envelope(r.json(), made["key"])
    assert body["tools_hidden"] >= 1
    assert "commandExecution" not in json.dumps(body)


@pytest.mark.anyio
async def test_revoked_expired_and_unknown_are_one_404(env, host, project, monkeypatch):  # noqa: F811
    key, turn = await _finished_turn("claude", project)
    op = await env.operator()
    async with contextlib.aclosing(op):
        a = (await op.post("/api/reply-shares", json={"session": key, "turn_id": turn})).json()
        b = (await op.post("/api/reply-shares", json={"session": key, "turn_id": turn})).json()
        assert (await op.delete(f"/api/reply-shares/{a['share']['share_id']}")).status_code == 200
        assert (await op.delete(f"/api/reply-shares/{a['share']['share_id']}")).status_code == 404
    store = env.app.state.share_store if hasattr(env.app.state, "share_store") else shares.store()
    later = store._clock() + 8 * 86400
    monkeypatch.setattr(store, "_clock", lambda: later)  # past b's 7-day default
    async with env.client() as visitor:
        bodies = set()
        for sid in (a["share"]["share_id"], b["share"]["share_id"], "Z" * 22, "short", "x/../y"):
            r = await visitor.get(f"/api/public/reply/{sid}")
            assert r.status_code == 404, sid
            bodies.add(r.text)
        assert len(bodies) <= 2  # the route's 404, and the router's own for a path that can't match


@pytest.mark.anyio
async def test_the_operator_routes_need_login_csrf_and_an_api_session(env, host, project):  # noqa: F811
    key, turn = await _finished_turn("opencode", project)
    async with env.client() as anon:
        r = await anon.post("/api/reply-shares", json={"session": key, "turn_id": turn})
        assert r.status_code in (401, 403)
    op = await env.operator()
    async with contextlib.aclosing(op):
        r = await op.post(
            "/api/reply-shares",
            json={"session": key, "turn_id": turn},
            headers={"X-CSRF-Token": "nope"},
        )
        assert r.status_code == 403
        r = await op.post(
            "/api/reply-shares",
            json={"session": "claude:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f", "turn_id": turn},
        )
        assert r.status_code == 422  # a terminal session's reply can't be shared
        r = await op.post("/api/reply-shares", json={"session": key, "turn_id": ident()})
        assert r.status_code == 404
        r = await op.post(
            "/api/reply-shares", json={"session": key, "turn_id": turn, "expires_in": "2d"}
        )
        assert r.status_code == 422


@pytest.mark.anyio
async def test_a_password_link_still_serves_only_ciphertext(env, host, project):  # noqa: F811
    key, turn = await _finished_turn("codex", project)
    op = await env.operator()
    async with contextlib.aclosing(op):
        made = (
            await op.post(
                "/api/reply-shares",
                json={"session": key, "turn_id": turn, "password": "open sesame"},
            )
        ).json()
        assert made["share"]["has_password"] is True
    async with env.client() as visitor:
        env_json = (await visitor.get(f"/api/public/reply/{made['share']['share_id']}")).json()
    assert env_json["password"] is True
    assert reply_share.open_envelope(env_json, made["key"], "open sesame")["reply"] == "echo:hello"


@pytest.mark.anyio
async def test_archiving_a_session_ends_its_reply_links(env, host, project):  # noqa: F811
    key, turn = await _finished_turn("claude", project)
    op = await env.operator()
    async with contextlib.aclosing(op):
        made = (await op.post("/api/reply-shares", json={"session": key, "turn_id": turn})).json()
    shares.store().revoke_for_session(key, reason="archived")
    async with env.client() as visitor:
        r = await visitor.get(f"/api/public/reply/{made['share']['share_id']}")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_secrets_unreadable_means_nothing_is_shared(env, host, project, monkeypatch):  # noqa: F811
    key, turn = await _finished_turn("codex", project)

    def boom():
        raise template_secrets.RedactionUnavailable("store unreadable")

    monkeypatch.setattr(template_secrets, "redaction_values", boom)
    op = await env.operator()
    async with contextlib.aclosing(op):
        r = await op.post("/api/reply-shares", json={"session": key, "turn_id": turn})
        assert r.status_code == 503
        assert (await op.get("/api/reply-shares")).json()["shares"] == []


@pytest.mark.anyio
async def test_none_mode_serves_nothing_and_makes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    from agent_sessions.auth import AuthConfig

    cfg = AuthConfig.from_env()
    app = create_app(cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:9",
        headers={"Origin": "http://127.0.0.1:9"},
    ) as c:
        assert (await c.get("/r/" + "A" * 22)).status_code == 404
        assert (await c.get("/api/public/reply/" + "A" * 22)).status_code == 404
        csrf = (await c.get("/api/config")).json()["csrf"]
        r = await c.post(
            "/api/reply-shares",
            json={"session": "codex-api:x", "turn_id": "t"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 409


def test_sweep_drops_an_expired_link_s_ciphertext(tmp_path):
    now = [1000.0]
    store = shares.ShareStore(tmp_path / "shares.db", clock=lambda: now[0])
    store.reply_create(
        share_id="A" * 22,
        session_key="codex-api:x",
        turn_id="t",
        agent="Codex",
        has_password=False,
        expires_at=1100,
        envelope="{}",
    )
    assert store.reply_envelope("A" * 22) == "{}"
    now[0] = 1200
    assert store.reply_envelope("A" * 22) is None
    store.sweep()
    import sqlite3

    con = sqlite3.connect(tmp_path / "shares.db")
    assert con.execute("SELECT envelope FROM reply_shares").fetchone() == (None,)
