"""Public links for API sessions (#1404 P2) — what a visitor sees and what a `send` link may do.

Unit tests pin the projection (allowlist + redaction) and the fenced mutation seam. The
end-to-end tests drive the public routes through an in-process ASGI client on the SAME event loop
as the native runtime, against the real contained workers and scripted agents of all three
clients (``test_native_runtime``'s fixtures).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import httpx
import pytest

from agent_sessions import missions, share_api, shares, template_secrets
from agent_sessions import structured_runtime as runtime
from agent_sessions.main import create_app
from agent_sessions.routes import public_api
from agent_sessions.routes import upload as upload_routes
from test_native_background_dispatch import (
    _notification_turn_in_flight,
    _state,
    _with_running_task,
)
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401 — fixtures

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---- projection ----------------------------------------------------------------------------


def _operator_snapshot() -> dict:
    return {
        "session_key": "codex-api:x",
        "revision": 7,
        "event_cursor": 7,
        "cwd": "/home/op/secret-project",
        "state": "idle",
        "active_turn": None,
        "model_requested": "gpt-x",
        "model_effective": "gpt-x",
        "bypass": True,
        "turns": [
            {
                "turn_id": "t1",
                "operation_id": "op-operator",
                "state": "completed",
                "text": "deploy with token s3cret-value-123",
                "reply": "done",
                "reason": "error at /home/op/secret-project/x.py",
                "context": {"mission_id": "m1"},
                "attachments": [
                    {"stored": "20261012-101010-a.png", "name": "a.png", "mime": "image/png"},
                    {"stored": "20261012-101010-b.pdf", "name": "b.pdf", "mime": "application/pdf"},
                ],
                "tools": [{"name": "shell", "outcome": "ok", "summary": "ls s3cret-value-123"}],
            },
            {"turn_id": "t2", "operation_id": "op-visitor", "state": "completed", "text": "hi"},
        ],
        "pending_requests": [
            {
                "request_id": "r1",
                "turn_id": "t1",
                "kind": "shell",
                "summary": "rm -rf /home/op/secret-project\nsecond line",
                "payload": {"cmd": "rm"},
                "always": [{"saved_in": "/home/op/.codex/rules"}],
                "operator_only": True,
                "context": {"mission_id": "m1"},
            }
        ],
        "native": {"native_id": "abc", "worker": "w-uuid"},
        "read_only": None,
        "images": True,
        "files": True,
    }


def test_the_projection_is_an_allowlist():
    authors = {"op-visitor": {"name": "Alex", "share_id": "s1"}}
    r = share_api._Redactor(["s3cret-value-123"])
    view = share_api.project(_operator_snapshot(), authors, r, "s1")
    assert set(view) == {
        "revision",
        "event_cursor",
        "state",
        "active_turn",
        "model",
        "turns",
        "omitted_turns",
        "pending_requests",
        "read_only",
        "images",
        "background",
    }
    assert view["background"] is False
    flat = repr(view)
    for leak in (
        "secret-project/x.py",  # the error `reason`
        "w-uuid",
        "native_id",
        "mission_id",
        "op-operator",
        "op-visitor",
        "saved_in",
        "payload",
        "bypass",
        "s3cret-value-123",
        "second line",
    ):
        assert leak not in flat, leak
    t1, t2 = view["turns"]
    assert t1["author"] is None and t2["author"] == {"name": "Alex", "via": "public_link"}
    assert "[secret]" in t1["text"] and "[secret]" in t1["tools"][0]["summary"]
    # An image: readable by an OPAQUE handle; never the stored name (it carries a filename).
    assert t1["attachments"][0]["image"] == share_api.image_handle("s1", "20261012-101010-a.png")
    assert "stored" not in t1["attachments"][0]
    assert "image" not in t1["attachments"][1] and "stored" not in t1["attachments"][1]
    # One line of the request, as the conversation itself would show the command; no payload,
    # choices, grants or where a grant would be saved.
    assert view["pending_requests"] == [
        {
            "request_id": "r1",
            "turn_id": "t1",
            "kind": "shell",
            "summary": "rm -rf /home/op/secret-project",
            "risk": None,
        }
    ]
    assert "reason" not in t1 and "operation_id" not in t1


def test_background_work_is_one_bit_and_needs_a_live_worker():
    r = share_api._Redactor([])
    snap = _operator_snapshot()
    snap["native"] = {"native_id": "abc", "worker": "w-uuid", "background_active": True}
    view = share_api.project(snap, {}, r, "s1")
    assert view["background"] is True
    assert "w-uuid" not in repr(view) and "native_id" not in repr(view)
    snap["native"] = {"native_id": "abc", "worker": None, "background_active": True}
    assert share_api.project(snap, {}, r, "s1")["background"] is False


def test_events_carry_cursors_and_kinds_only():
    page = {
        "revision": 3,
        "next_cursor": 3,
        "events": [{"cursor": 3, "kind": "tool", "data": {"output": "secret", "worker_id": "w"}}],
    }
    assert share_api.project_events(page, "L1") == {
        "link": "L1",
        "revision": 3,
        "next_cursor": 3,
        "events": [{"cursor": 3, "kind": "tool"}],
    }


def test_the_operator_view_names_a_visitors_turn():
    snap = _operator_snapshot()
    share_api.overlay_authors(snap, {"op-visitor": {"name": "Alex", "share_id": "s1"}})
    assert snap["turns"][0]["author"] is None
    assert snap["turns"][1]["author"] == {"name": "Alex", "via": "public_link"}


def test_an_unreadable_secret_store_refuses_the_projection(monkeypatch):
    def boom():
        raise template_secrets.RedactionUnavailable("store unreadable")

    monkeypatch.setattr(template_secrets, "redaction_values", boom)
    with pytest.raises(Exception) as e:
        public_api._redactor()
    assert getattr(e.value, "status_code", None) == 503


# ---- the fenced seam -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_revoke_lands_after_an_in_flight_send_never_between(tmp_path):
    st = shares.ShareStore(tmp_path / "shares.db")
    share, _ = st.create(session_key="codex-api:x", kind="api", mode="send")
    started, release = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def effect_body():
        started.set()
        await release.wait()
        order.append("effect")
        return {"state": "running"}

    send = asyncio.create_task(public_api._fenced(st, share.share_id, effect_body))
    await started.wait()
    revoker = asyncio.create_task(
        asyncio.to_thread(lambda: (st.revoke(share.share_id), order.append("revoked")))
    )
    await asyncio.sleep(0.1)
    assert order == []  # the revoke waits for the fence the send holds
    release.set()
    await asyncio.wait_for(send, 5)
    await asyncio.wait_for(revoker, 5)
    assert order == ["effect", "revoked"]
    ran = []
    with pytest.raises(Exception) as e:  # the next send sees it dead and never runs its effect

        async def never():
            ran.append(True)

        await public_api._fenced(st, share.share_id, never)
    assert getattr(e.value, "status_code", None) == 404 and ran == []


@pytest.mark.anyio
async def test_an_archive_during_a_fenced_send_does_not_deadlock(tmp_path, monkeypatch):
    """The send holds the fence in a worker thread while its effect runs on the loop; archiving's
    revoke must run off the loop or the two deadlock (#1404 P2)."""
    monkeypatch.setenv("AGENT_SESSIONS_SHARES_DB", str(tmp_path / "s" / "shares.db"))
    shares.reset_for_tests()
    st = shares.store()
    share, _ = st.create(session_key="codex-api:y", kind="api", mode="send")
    started, release = asyncio.Event(), asyncio.Event()

    async def effect_body():
        started.set()
        await release.wait()
        return {}

    send = asyncio.create_task(public_api._fenced(st, share.share_id, effect_body))
    await started.wait()

    async def archive():
        async with shares.archiving("codex-api:y"):
            pass

    arch = asyncio.create_task(archive())
    await asyncio.sleep(0.1)
    release.set()  # the loop must still be free to finish the effect
    await asyncio.wait_for(asyncio.gather(send, arch), 10)
    assert st.active(share.share_id) is None


# ---- end to end, through the routes, on the native runtime ---------------------------------


class _Env:
    def __init__(self, app, cfg):
        self.app, self.cfg = app, cfg

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://testserver",
            headers={"Origin": self.cfg.origin},
        )


@pytest.fixture
def env(auth_cfg):
    return _Env(create_app(auth_cfg), auth_cfg)


async def _visitor(env, key, *, mode="send", name="Alex"):
    st = env.app.state.share_store
    share, token = st.create(session_key=key, kind="api", mode=mode)
    v = env.client()
    r = await v.post(
        "/api/public/unlock", json={"token": token, "name": name} if name else {"token": token}
    )
    assert r.status_code == 200, r.text
    v.headers["X-CSRF-Token"] = r.json()["csrf"]
    return v, share


async def _session(source, work):
    snap = await runtime.create_session(ENGINES[source], str(work), operation_id=ident())
    return snap["session_key"]


async def _settle_visitor_turn(env, key):
    authors = env.app.state.share_store.authors(key)
    assert len(authors) == 1
    (op,) = authors
    return await settle(key, op)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude", "opencode"])
async def test_a_send_link_sends_as_the_visitor_and_both_views_say_so(env, host, project, source):  # noqa: F811
    key = await _session(source, project)
    v, share = await _visitor(env, key)
    ck = str(uuid.uuid4())
    async with contextlib.aclosing(v):
        r = await v.post("/api/public/turns", json={"client_key": ck, "text": "hello from Alex"})
        assert r.status_code in (200, 202), r.text
        again = await v.post(
            "/api/public/turns", json={"client_key": ck, "text": "hello from Alex"}
        )
        assert again.status_code in (200, 202)  # a retry reuses the operation: one turn
        await _settle_visitor_turn(env, key)
        view = (await v.get("/api/public/session")).json()
        events = (await v.get("/api/public/events?after=0")).json()
    texts = [t for t in view["turns"] if t["text"] == "hello from Alex"]
    assert len(texts) == 1 and texts[0]["author"] == {"name": "Alex", "via": "public_link"}
    assert texts[0]["mine"] is True  # sent through this link: Send now may be offered
    # Both reads are bound to the link the cookie names (#1444): the viewer refuses another's.
    assert view["link"] == events["link"] == share_api.link_id(share.share_id)
    assert view["can_send"] is True
    for leak in ("cwd", "native", "operation_id", str(project)):
        assert leak not in repr(view), leak
    op_snap = await runtime.snapshot(key)  # the operator's own view is untouched…
    assert op_snap["cwd"]
    async with env.client() as op:  # …and its route names the visitor
        await op.post("/login", data={"username": "marcus", "password": "hunter2"})
        op.headers["X-CSRF-Token"] = (await op.get("/api/config")).json()["csrf"]
        row = (await op.get(f"/api/structured/sessions/{key}")).json()
    mine = [t for t in row["turns"] if t["text"] == "hello from Alex"]
    assert mine[0]["author"] == {"name": "Alex", "via": "public_link"}


@pytest.mark.anyio
async def test_a_view_link_cannot_send_and_a_terminal_link_sees_nothing_here(env, host, project):  # noqa: F811
    key = await _session("codex", project)
    v, _ = await _visitor(env, key, mode="view")
    async with contextlib.aclosing(v):
        r = await v.post("/api/public/turns", json={"client_key": str(uuid.uuid4()), "text": "x"})
        assert r.status_code == 403
        assert (await v.get("/api/public/session")).json()["can_send"] is False
    st = env.app.state.share_store
    term, token = st.create(
        session_key="claude:11111111-1111-1111-1111-111111111111", kind="terminal", mode="view"
    )
    t = env.client()
    async with contextlib.aclosing(t):
        await t.post("/api/public/unlock", json={"token": token})
        assert (await t.get("/api/public/session")).status_code == 404


@pytest.mark.anyio
async def test_a_secret_in_the_transcript_is_redacted_for_the_visitor_only(
    env,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["s3cret-value-123"])
    key = await _session("codex", project)
    # The OPERATOR's turn carries the secret; the agent echoes it in its reply.
    op = ident()
    await runtime.submit_turn(key, operation_id=op, text="deploy with s3cret-value-123")
    await settle(key, op)
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        await v.post(
            "/api/public/turns",
            json={"client_key": str(uuid.uuid4()), "text": "use s3cret-value-123 please"},
        )
        await _settle_visitor_turn(env, key)
        view = (await v.get("/api/public/session")).json()
        events = (await v.get("/api/public/events?after=0&limit=100")).json()
    operator_turn = next(t for t in view["turns"] if t["author"] is None)
    visitor_turn = next(t for t in view["turns"] if t["author"])
    assert "s3cret-value-123" not in operator_turn["text"] + (operator_turn["reply"] or "")
    assert "[secret]" in operator_turn["text"]
    assert "s3cret-value-123" not in (visitor_turn["reply"] or "")  # the agent's echo: redacted
    # …while the visitor's OWN text is shown as typed (no marker to test guesses against).
    assert visitor_turn["text"] == "use s3cret-value-123 please"
    assert all(set(e) == {"cursor", "kind"} for e in events["events"])
    raw = await runtime.snapshot(key)
    assert any("s3cret-value-123" in (t.get("text") or "") for t in raw["turns"])


@pytest.mark.anyio
async def test_a_mission_held_session_can_be_watched_not_sent_to(env, host, project, monkeypatch):  # noqa: F811
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)
    monkeypatch.setattr(missions, "session_mission", lambda k, **kw: "mission-1")
    async with contextlib.aclosing(v):
        view = (await v.get("/api/public/session")).json()
        assert view["can_send"] is False and view["mission_held"] is True
        r = await v.post("/api/public/turns", json={"client_key": str(uuid.uuid4()), "text": "x"})
        assert r.status_code == 409
    assert env.app.state.share_store.authors(key) == {}


@pytest.mark.anyio
async def test_a_visitor_attaches_only_their_own_upload_and_reads_only_shown_images(
    env,
    host,  # noqa: F811
    project,  # noqa: F811
):
    key = await _session("codex", project)
    v, share = await _visitor(env, key)
    theirs, _ = upload_routes.store_upload([PNG], "operator-private.png")  # the operator's
    async with contextlib.aclosing(v):
        up = await v.post("/api/public/upload", content=PNG, headers={"Content-Type": "image/png"})
        assert up.status_code == 201, up.text
        mine, mine_image = up.json()["stored"], up.json()["image"]
        refused = await v.post(
            "/api/public/turns",
            json={"client_key": str(uuid.uuid4()), "text": "look", "attachments": [theirs.name]},
        )
        assert refused.status_code == 422
        theirs_image = share_api.image_handle(share.share_id, theirs.name)
        assert (await v.get(f"/api/public/uploads/{theirs_image}")).status_code == 404
        assert (await v.get(f"/api/public/uploads/{theirs.name}")).status_code == 404
        assert (await v.get(f"/api/public/uploads/{mine_image}")).status_code == 404  # not shown
        sent = await v.post(
            "/api/public/turns",
            json={"client_key": str(uuid.uuid4()), "text": "look", "attachments": [mine]},
        )
        assert sent.status_code in (200, 202), sent.text
        await _settle_visitor_turn(env, key)
        assert (await v.get(f"/api/public/uploads/{mine}")).status_code == 404  # stored ≠ handle
        img = await v.get(f"/api/public/uploads/{mine_image}")
        assert img.status_code == 200 and img.content == PNG
        assert img.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_public_upload_is_bounded_and_sniffed(env, host, project):  # noqa: F811
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        big = PNG + b"\x00" * (public_api.MAX_PUBLIC_IMAGE_BYTES + 1)
        r = await v.post("/api/public/upload", content=big, headers={"Content-Type": "image/png"})
        assert r.status_code == 413
        r = await v.post(
            "/api/public/upload",
            content=b"%PDF-1.7 not an image",
            headers={"Content-Type": "image/png"},
        )
        assert r.status_code == 415
        r = await v.post("/api/public/upload", content=b"", headers={"Content-Type": "image/png"})
        assert r.status_code == 422


@pytest.mark.anyio
async def test_a_visitor_can_stop_the_running_turn(env, host, project):  # noqa: F811
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        await v.post("/api/public/turns", json={"client_key": str(uuid.uuid4()), "text": "HANG"})
        (op,) = env.app.state.share_store.authors(key)
        for _ in range(200):
            snap = await runtime.snapshot(key)
            if snap.get("active_turn"):
                break
            await asyncio.sleep(0.05)
        r = await v.post("/api/public/interrupt", json={"turn_id": snap["active_turn"]})
        assert r.status_code == 202, r.text
        await settle(key, op)


@pytest.mark.anyio
async def test_send_now_is_only_for_the_visitors_own_queued_message(env, host, project):  # noqa: F811
    key = await _session("codex", project)
    active, operator_queued = ident(), ident()
    await runtime.submit_turn(key, operation_id=active, text="HANG")
    for _ in range(200):
        if (await runtime.snapshot(key)).get("active_turn"):
            break
        await asyncio.sleep(0.05)
    await runtime.submit_turn(key, operation_id=operator_queued, text="operator later")
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        r = await v.post("/api/public/send-now", json={"queued_turn_id": operator_queued})
        assert r.status_code == 403
    await runtime.interrupt(key, operation_id=ident(), turn_id=active)


@pytest.mark.anyio
async def test_send_now_reaches_past_a_turn_the_agent_runs_for_itself(env, host, project):  # noqa: F811
    """No operator turn is running, but the agent is (#1427): the visitor's own queued message
    goes now, through the same background target the operator pane uses."""
    key = await _with_running_task(project, lifecycle=True)
    await _notification_turn_in_flight(key, project, "hold-notification")
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        r = await v.post("/api/public/turns", json={"client_key": str(uuid.uuid4()), "text": "two"})
        assert r.status_code in (200, 202), r.text
        (mine,) = env.app.state.share_store.authors(key)
        for _ in range(100):
            snap = await runtime.snapshot(key)
            if snap["native"].get("background_turn"):
                break
            await asyncio.sleep(0.05)
        assert snap["active_turn"] is None and snap["native"]["background_turn"]
        assert await _state(key, mine) == "queued"
        r = await v.post("/api/public/send-now", json={"queued_turn_id": mine})
        assert r.status_code == 202, r.text
    _, turn = await settle(key, mine)
    assert turn["reply"] == "echo:two"


@pytest.mark.anyio
async def test_a_retry_key_from_another_session_is_refused(env, host, project):  # noqa: F811
    st = env.app.state.share_store
    share, _ = st.create(session_key="codex-api:a", kind="api", mode="send")
    ck = str(uuid.uuid4())
    st.record_turn(share_id=share.share_id, session_key="codex-api:a", client_key=ck, author="A")
    with pytest.raises(shares.ShareError):
        st.record_turn(
            share_id=share.share_id, session_key="codex-api:b", client_key=ck, author="A"
        )


def test_the_server_mints_operation_ids(tmp_path):
    st = shares.ShareStore(tmp_path / "shares.db")
    share, _ = st.create(session_key="codex-api:a", kind="api", mode="send")
    op1, fresh1 = st.record_turn(
        share_id=share.share_id, session_key="codex-api:a", client_key="k1", author="A"
    )
    op2, fresh2 = st.record_turn(
        share_id=share.share_id, session_key="codex-api:a", client_key="k1", author="B"
    )
    assert op1 == op2 and fresh1 and not fresh2
    assert uuid.UUID(op1)
    assert st.authors("codex-api:a")[op1]["name"] == "A"  # a retry keeps the first name


# ---- independent review of #1423 -----------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_sends_never_starve_the_default_executor(tmp_path):
    """B1: each fence holder used to park a DEFAULT-executor thread while its facade call — which
    itself needs default-executor threads — ran; enough concurrent sends wedged the whole app."""
    import concurrent.futures

    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=4))
    st = shares.ShareStore(tmp_path / "shares.db")
    # More links than default-pool threads: the per-link lock alone can't save a design that
    # parks fence holders in the default pool.
    links = [st.create(session_key=f"codex-api:{i}", kind="api", mode="send")[0] for i in range(6)]

    async def facade_call():
        await asyncio.sleep(0.05)
        return await asyncio.to_thread(lambda: {"state": "running"})

    sends = [public_api._fenced(st, links[i % 6].share_id, facade_call) for i in range(12)]
    done = await asyncio.wait_for(asyncio.gather(*sends), 20)
    assert len(done) == 12
    assert await asyncio.wait_for(asyncio.to_thread(lambda: 1), 5) == 1  # not wedged


@pytest.mark.anyio
async def test_a_hung_facade_call_times_out_and_releases_the_fence(tmp_path, monkeypatch):
    monkeypatch.setattr(public_api, "EFFECT_TIMEOUT_S", 0.3)
    st = shares.ShareStore(tmp_path / "shares.db")
    share, _ = st.create(session_key="codex-api:z", kind="api", mode="send")

    async def never():
        await asyncio.Event().wait()

    with pytest.raises(Exception) as e:
        await public_api._fenced(st, share.share_id, never)
    assert getattr(e.value, "status_code", None) == 503
    assert await asyncio.wait_for(asyncio.to_thread(st.revoke, share.share_id), 5) is True


SECRET = "ghp_A1b2C3d4E5f6G7h8"


@pytest.mark.parametrize(
    "text,side,leaks",
    [
        ("x" * 50 + SECRET[:12], "end", SECRET[:12]),  # cut after the start of a secret
        (SECRET[-12:] + " and more", "start", SECRET[-12:]),  # a reply kept only its tail
        ("ends with " + SECRET[:4], "end", SECRET[:4]),
    ],
)
def test_a_secret_split_by_a_cut_leaves_no_fragment(text, side, leaks):
    r = share_api._Redactor([SECRET])
    out = r(text, **{side: True})
    assert leaks not in out and share_api.CUT_MARK in out


def test_text_that_was_not_cut_answers_no_guesses():
    """Masking fragments of UNtruncated text would be an oracle: a visitor sends "...ghp_A" and
    learns from whether it comes back masked (#1423 review)."""
    r = share_api._Redactor([SECRET])
    assert r("hello " + SECRET[:6]) == "hello " + SECRET[:6]
    snap = _operator_snapshot()
    snap["turns"][1].update(text="hello " + SECRET[:6], text_truncated=False)
    assert share_api.project(snap, {}, r, "s1")["turns"][1]["text"] == "hello " + SECRET[:6]
    snap["turns"][1]["text_truncated"] = True  # the runtime cut it there: now it is masked
    assert SECRET[:6] not in share_api.project(snap, {}, r, "s1")["turns"][1]["text"]


@pytest.mark.parametrize("after_guess", [0, 3, 20])
def test_a_right_guess_and_a_wrong_one_look_the_same_at_a_cut(after_guess):
    """Hermes on #1423: the visitor controls their text AND (through the shared snapshot budget)
    where the runtime cuts it. Run both guesses through the runtime's real `_turn` bounding and
    the projection: the visitor must get byte-identical output either way."""
    from agent_sessions import structured_runtime as srt

    r = share_api._Redactor([SECRET])
    known = SECRET[:6]  # a recognizable prefix ("ghp_A1")
    outs = []
    for guess in (SECRET[6], "Z"):
        text = "pad " + known + guess + " trailing words the cut removes"
        # the visitor's budget games put the cut `after_guess` characters past the guess
        budget = [len("pad " + known) + 1 + after_guess]
        bounded = srt._turn({"turn_id": "t1", "text": text, "state": "completed"}, budget)
        assert bounded["text_truncated"] is True
        outs.append(share_api.project({"turns": [bounded]}, {}, r, "s1"))
    assert outs[0] == outs[1]
    assert SECRET[:4] not in repr(outs[0])


def test_the_trim_never_depends_on_what_the_secrets_contain():
    a = share_api._Redactor(["ghp_A1b2C3d4E5f6G7h8"])
    b = share_api._Redactor(["zzzzzzzzzzzzzzzzzzzz"])
    text = "x" * 600 + "ghp_A1b2"
    assert a(text, end=True) == b(text, end=True)  # same length secrets: same trim, same output
    assert a.trim == b.trim == share_api.CUT_TRIM_MIN


def test_edge_masking_is_fast_on_a_dense_snapshot_and_a_long_secret():
    import time as _time

    long_secret = "-----BEGIN KEY-----" + "Qx7" * 1000  # ~3 kB, like a PEM key
    r = share_api._Redactor([long_secret, SECRET])
    turns = [
        {
            "turn_id": f"t{n}",
            "operation_id": f"t{n}",
            "state": "completed",
            "text": "y" * 20_000,
            "text_truncated": True,
            "reply": "z" * 20_000,
            "reply_truncated": True,
            "tools": [{"name": "shell", "outcome": "ok", "summary": "w" * 500} for _ in range(64)],
        }
        for n in range(50)
    ]
    # CPU time, not wall time: the bound catches a slow (e.g. quadratic) masking pass, and a
    # loaded shared runner stretching wall time must not fail it (3.1–3.5 s wall seen in CI).
    started = _time.process_time()
    share_api.project({"turns": turns}, {}, r, "s1")
    assert _time.process_time() - started < 3.0


def test_request_summaries_are_redacted_before_they_are_cut():
    r = share_api._Redactor([SECRET])
    summary = "y" * 190 + SECRET + " tail"
    out = share_api._request({"request_id": "1", "summary": summary}, r)["summary"]
    assert SECRET[:5] not in out


def test_every_string_field_is_redacted_including_risk():
    r = share_api._Redactor([SECRET])
    snap = _operator_snapshot()
    snap["turns"][0]["state"] = SECRET
    snap["turns"][0]["tools"][0]["outcome"] = SECRET
    snap["turns"][0]["tools"][0]["risk"] = {"why": [SECRET]}
    assert SECRET not in repr(share_api.project(snap, {}, r, "s1"))


@pytest.mark.anyio
async def test_a_view_link_cannot_upload(env, host, project):  # noqa: F811
    key = await _session("codex", project)
    v, _ = await _visitor(env, key, mode="view")
    async with contextlib.aclosing(v):
        r = await v.post("/api/public/upload", content=PNG, headers={"Content-Type": "image/png"})
        assert r.status_code == 403


@pytest.mark.anyio
async def test_a_mission_held_session_takes_no_upload_or_send_now(env, host, project, monkeypatch):  # noqa: F811
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)
    monkeypatch.setattr(missions, "session_mission", lambda k, **kw: "mission-1")
    async with contextlib.aclosing(v):
        up = await v.post("/api/public/upload", content=PNG, headers={"Content-Type": "image/png"})
        assert up.status_code == 409
        now = await v.post("/api/public/send-now", json={"queued_turn_id": "anything"})
        assert now.status_code == 409


@pytest.mark.anyio
async def test_interrupt_only_stops_the_running_turn(env, host, project):  # noqa: F811
    """Not an operator's QUEUED message: Stop means the turn running now."""
    key = await _session("codex", project)
    active, operator_queued = ident(), ident()
    await runtime.submit_turn(key, operation_id=active, text="HANG")
    for _ in range(200):
        if (await runtime.snapshot(key)).get("active_turn"):
            break
        await asyncio.sleep(0.05)
    await runtime.submit_turn(key, operation_id=operator_queued, text="operator later")
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        r = await v.post("/api/public/interrupt", json={"turn_id": operator_queued})
        assert r.status_code == 409
        r = await v.post("/api/public/interrupt", json={"turn_id": str(uuid.uuid4())})
        assert r.status_code == 409
    queued = next(
        t for t in (await runtime.snapshot(key))["turns"] if t["turn_id"] == operator_queued
    )
    assert queued["state"] not in ("interrupted", "failed")
    await runtime.interrupt(key, operation_id=ident(), turn_id=active)


@pytest.mark.anyio
async def test_a_terminal_link_gets_no_api_events(env):
    st = env.app.state.share_store
    _share, token = st.create(
        session_key="claude:11111111-1111-1111-1111-111111111111", kind="terminal", mode="view"
    )
    t = env.client()
    async with contextlib.aclosing(t):
        await t.post("/api/public/unlock", json={"token": token})
        assert (await t.get("/api/public/events")).status_code == 404
        assert (await t.get("/api/public/uploads/20261012-101010-a.png")).status_code == 404


@pytest.mark.anyio
async def test_uploads_per_link_are_capped(env, host, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(public_api, "MAX_UPLOADS_PER_SHARE", 2)
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        codes = [
            (
                await v.post(
                    "/api/public/upload", content=PNG, headers={"Content-Type": "image/png"}
                )
            ).status_code
            for _ in range(3)
        ]
    assert codes == [201, 201, 429]


@pytest.mark.anyio
async def test_a_session_without_images_takes_no_upload(env, host, project, monkeypatch):  # noqa: F811
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)
    real = public_api._snapshot

    async def no_images(k):
        snap = await real(k)
        return {**snap, "images": False}

    monkeypatch.setattr(public_api, "_snapshot", no_images)
    async with contextlib.aclosing(v):
        r = await v.post("/api/public/upload", content=PNG, headers={"Content-Type": "image/png"})
        assert r.status_code == 409


@pytest.mark.anyio
async def test_a_broken_share_store_never_takes_the_operator_view_down(
    env,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    key = await _session("codex", project)

    def boom(*_a, **_k):
        raise shares.SharesUnavailable("store missing")

    monkeypatch.setattr(shares.ShareStore, "authors", boom)
    async with contextlib.aclosing(env.client()) as op:
        await op.post("/login", data={"username": "marcus", "password": "hunter2"})
        r = await op.get(f"/api/structured/sessions/{key}")
    assert r.status_code == 200


# ---- Hermes on #1423 -----------------------------------------------------------------------


def test_an_operator_filename_holding_a_secret_never_reaches_a_visitor():
    r = share_api._Redactor(["EXAMPLESECRET42"])
    snap = _operator_snapshot()
    snap["turns"][0]["attachments"] = [
        {
            "stored": "20261012-101010-token-EXAMPLESECRET42.png",
            "name": "token-EXAMPLESECRET42.png",
            "mime": "image/png",
        }
    ]
    view = share_api.project(snap, {}, r, "s1")
    assert "EXAMPLESECRET42" not in repr(view)
    handles = share_api.image_handles(snap, "s1")
    assert view["turns"][0]["attachments"][0]["image"] in handles  # still resolvable server-side


@pytest.mark.anyio
async def test_concurrent_uploads_cannot_overrun_the_cap(env, host, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(public_api, "MAX_UPLOADS_PER_SHARE", 2)
    key = await _session("codex", project)
    v, share = await _visitor(env, key)
    # Create missions.db first: five first-opens racing `PRAGMA journal_mode=WAL` is a test
    # artefact ("database is locked"), not what this test is about.
    missions.session_mission(key)
    async with contextlib.aclosing(v):
        results = await asyncio.gather(
            *[
                v.post("/api/public/upload", content=PNG, headers={"Content-Type": "image/png"})
                for _ in range(5)
            ]
        )
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 201, 429, 429, 429]
    assert env.app.state.share_store.count_upload_rows(share.share_id) == 2


def test_a_refused_upload_hands_its_slot_back(tmp_path):
    st = shares.ShareStore(tmp_path / "shares.db")
    share, _ = st.create(session_key="codex-api:x", kind="api", mode="send")
    a = st.reserve_upload(share.share_id, 1)
    assert a is not None and st.reserve_upload(share.share_id, 1) is None
    st.release_upload(share.share_id, a)
    assert st.reserve_upload(share.share_id, 1) is not None


@pytest.mark.anyio
async def test_attribution_that_cant_be_read_is_unknown_never_the_operators(
    env,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
    tmp_path,  # noqa: F811
):
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        await v.post("/api/public/turns", json={"client_key": str(uuid.uuid4()), "text": "from A"})
        await _settle_visitor_turn(env, key)

    async def operator_row():
        async with contextlib.aclosing(env.client()) as op:
            await op.post("/login", data={"username": "marcus", "password": "hunter2"})
            return (await op.get(f"/api/structured/sessions/{key}")).json()

    def boom(*_a, **_k):
        raise RuntimeError("store unreadable")

    with monkeypatch.context() as m:
        m.setattr(shares.ShareStore, "authors", boom)
        row = await operator_row()
    assert row["attribution_unavailable"] is True
    assert all(t["author"] == {"name": None, "via": "unknown"} for t in row["turns"])
    # The store going missing (marker left behind) is the same: unknown, not "no visitors".
    db = shares.db_path()
    for f in db.parent.glob("shares.db*"):
        if f != shares.marker_path(db):
            f.rename(tmp_path / f.name)
    row = await operator_row()
    assert all(t["author"] == {"name": None, "via": "unknown"} for t in row["turns"])


def test_an_abandoned_upload_slot_stops_counting(tmp_path):
    now = {"t": 1_000_000.0}
    st = shares.ShareStore(tmp_path / "shares.db", clock=lambda: now["t"])
    share, _ = st.create(session_key="codex-api:x", kind="api", mode="send")
    assert st.reserve_upload(share.share_id, 1) is not None  # …and the upload crashed
    assert st.reserve_upload(share.share_id, 1) is None
    now["t"] += shares.PENDING_UPLOAD_TTL_S + 1
    assert st.reserve_upload(share.share_id, 1) is not None


@pytest.mark.anyio
async def test_an_unusable_secret_key_answers_503_not_500(env, host, project, monkeypatch):  # noqa: F811
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)

    def unusable(*_a, **_k):
        raise template_secrets.SecretKeyUnavailable("bad key")

    async with contextlib.aclosing(v):
        with monkeypatch.context() as m:
            m.setattr(share_api, "image_handle", unusable)
            snap = await runtime.snapshot(key)
            assert snap is not None
            m.setattr(public_api.share_api, "project", lambda *a, **k: unusable())
            r = await v.get("/api/public/session")
    assert r.status_code == 503


@pytest.mark.anyio
async def test_an_unusable_key_for_the_link_id_answers_503_on_session_and_events(
    env,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Every snapshot and events page carries the keyed link identity (#1444): an unusable key
    is a 503, never a 500 or an answer the viewer can't bind to its link."""
    key = await _session("codex", project)
    v, _ = await _visitor(env, key)

    def unusable(*_a, **_k):
        raise template_secrets.SecretKeyUnavailable("bad key")

    async with contextlib.aclosing(v):
        with monkeypatch.context() as m:
            m.setattr(share_api, "link_id", unusable)
            session = await v.get("/api/public/session")
            events = await v.get("/api/public/events?after=0")
    assert session.status_code == 503 and events.status_code == 503


def test_terminal_escapes_cannot_stretch_a_secret_past_the_trim():
    r = share_api._Redactor([SECRET])
    coloured = "".join("\x1b[38;5;196m" + ch for ch in SECRET)
    out = r("x" * 50 + coloured[: len(coloured) // 2], end=True)
    assert "\x1b" not in out and SECRET[:6] not in out
    assert r("a\x00b\x07c\td\ne") == "abc\td\ne"


def test_a_visitors_own_turn_is_shown_as_typed_and_others_are_redacted():
    r = share_api._Redactor([SECRET])
    authors = {
        "op-mine": {"name": "A", "share_id": "s1"},
        "op-other": {"name": "B", "share_id": "s2"},
    }
    snap = {
        "turns": [
            {"turn_id": "a", "operation_id": "op-mine", "text": "guess " + SECRET},
            {"turn_id": "b", "operation_id": "op-other", "text": "guess " + SECRET},
            {"turn_id": "c", "operation_id": "op-operator", "text": "op " + SECRET},
        ]
    }
    turns = share_api.project(snap, authors, r, "s1")["turns"]
    assert turns[0]["text"] == "guess " + SECRET  # theirs: no [secret] marker to read guesses from
    assert SECRET not in turns[1]["text"] and SECRET not in turns[2]["text"]


# ---- Hermes on #1423 (71379cb0): uploads fenced and bounded ---------------------------------


def test_send_now_eligibility_is_per_link_never_by_name():
    """`mine` is what Send now is offered on, and it is what the server checks: the LINK the turn
    came through. A rename on the same link keeps it; the same name on another link never gets it
    (Hermes on #1444)."""
    r = share_api._Redactor([])
    authors = {
        "op-mine": {"name": "Alex", "share_id": "s1"},  # sent as Alex, now renamed: still mine
        "op-other": {"name": "Alex", "share_id": "s2"},  # same name, another link
    }
    snap = {
        "turns": [
            {"turn_id": "a", "operation_id": "op-mine", "state": "queued", "text": "x"},
            {"turn_id": "b", "operation_id": "op-other", "state": "queued", "text": "y"},
            {"turn_id": "c", "operation_id": "op-operator", "state": "queued", "text": "z"},
        ]
    }
    turns = share_api.project(snap, authors, r, "s1")["turns"]
    assert [t["mine"] for t in turns] == [True, False, False]
    assert "s2" not in repr(turns) and "s1" not in repr(turns)  # no link id is named


def test_a_link_id_is_opaque_stable_and_per_link():
    a, b = share_api.link_id("share-A"), share_api.link_id("share-B")
    assert a == share_api.link_id("share-A") and a != b
    assert "share-A" not in a and len(a) == 24


def test_a_live_upload_can_never_outlive_its_slot():
    """Expiring a pending slot is only safe because no request may stream longer than the TTL."""
    assert shares.PENDING_UPLOAD_TTL_S >= 5 * public_api.UPLOAD_READ_TIMEOUT_S


@pytest.mark.anyio
async def test_a_stalled_upload_times_out_and_hands_its_slot_back(env, host, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(public_api, "UPLOAD_READ_TIMEOUT_S", 0.3)
    key = await _session("codex", project)
    v, share = await _visitor(env, key)

    async def slow():
        yield PNG[:8]
        await asyncio.sleep(2)
        yield PNG[8:]

    async with contextlib.aclosing(v):
        r = await v.post(
            "/api/public/upload", content=slow(), headers={"Content-Type": "image/png"}
        )
    assert r.status_code == 408
    assert env.app.state.share_store.count_upload_rows(share.share_id) == 0


@pytest.mark.anyio
async def test_a_link_revoked_mid_upload_writes_nothing(env, host, project):  # noqa: F811
    key = await _session("codex", project)
    v, share = await _visitor(env, key)
    st = env.app.state.share_store
    before = set(upload_routes.ensure_uploads_dir().iterdir())
    gate = asyncio.Event()

    async def body():
        yield PNG[:8]
        await gate.wait()  # the operator revokes while the body is still streaming
        yield PNG[8:]

    async def revoke_then_release():
        await asyncio.sleep(0.2)
        await asyncio.to_thread(st.revoke, share.share_id)
        gate.set()

    async with contextlib.aclosing(v):
        r, _ = await asyncio.gather(
            v.post("/api/public/upload", content=body(), headers={"Content-Type": "image/png"}),
            revoke_then_release(),
        )
    assert r.status_code == 404
    assert st.count_upload_rows(share.share_id) == 0
    assert set(upload_routes.ensure_uploads_dir().iterdir()) == before


@pytest.mark.anyio
async def test_an_upload_whose_slot_is_gone_leaves_no_file(env, host, project, monkeypatch):  # noqa: F811
    key = await _session("codex", project)
    v, share = await _visitor(env, key)
    before = set(upload_routes.ensure_uploads_dir().iterdir())
    monkeypatch.setattr(shares.ShareStore, "fill_upload", lambda self, *a: False)
    async with contextlib.aclosing(v):
        r = await v.post("/api/public/upload", content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 409
    assert set(upload_routes.ensure_uploads_dir().iterdir()) == before


@pytest.mark.anyio
async def test_a_visitor_can_never_interrupt_the_agents_own_turn(env, host, project, monkeypatch):  # noqa: F811
    """#1438: the agent's own turn is the owner's to stop. Refused by name — for the shown turn
    and for the live target — before any interrupt reaches the runtime."""
    key = await _session("claude", project)
    own, target = ident(), ident()
    real = await runtime.snapshot(key)
    shown = {
        **real,
        "turns": [*real["turns"], {"kind": "agent", "turn_id": own, "state": "running"}],
        "native": {**(real.get("native") or {}), "background_turn": target},
    }

    async def snapshot(_key):
        return shown

    async def never(*_args, **_kwargs):
        pytest.fail("a visitor's interrupt of the agent's own turn reached the runtime")

    monkeypatch.setattr(public_api, "_snapshot", snapshot)
    monkeypatch.setattr(runtime, "interrupt", never)
    v, _ = await _visitor(env, key)
    async with contextlib.aclosing(v):
        for turn_id in (own, target):
            r = await v.post("/api/public/interrupt", json={"turn_id": turn_id})
            assert r.status_code == 403
            assert "owner" in r.json()["detail"]
