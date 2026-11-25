"""Webhooks through the BattleLab relay for a Home Free box (#2221 P1b, battlelab-cloud#101): the
agent's HookLink (register, renew, forward to the app), its peer-socket ops, and the app side
(sync, the relay address, unregister-before-delete)."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_sessions import automations_store as store
from agent_sessions import webhook_relay, webhooks
from agent_sessions.homefree import hooklink
from agent_sessions.homefree.peerpipe import PeerPipe
from agent_sessions.routes import webhooks as routes

PUB = "a" * 26
HID = "0" * 32


@pytest.fixture(autouse=True)
def _fresh_relay_state(monkeypatch):
    """The app side's module state (generation, last sync) starts clean in every test."""
    for name, value in (
        ("_lock", None),
        ("_gen", 0),
        ("_seen", None),
        ("_dirty", True),
        ("_last_sync", 0.0),
        ("_retry_at", 0.0),
        ("_retry_s", webhook_relay.RETRY_MIN_S),
        ("_want", 0),
    ):
        monkeypatch.setattr(webhook_relay, name, value)


class FakeWS:
    """A control connection: records what the agent sends; answers registers like the relay."""

    def __init__(
        self, link=None, *, refuse: str | None = None, refuse_unregister: str | None = None
    ):
        self.sent: list[dict] = []
        self.link = link
        self.refuse = refuse
        self.refuse_unregister = refuse_unregister
        self.delay = 0.0  # seconds before each reply (a slow relay)

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.link is None or msg.get("t") not in ("hook_register", "hook_unregister"):
            return
        refuse = self.refuse_unregister if msg["t"] == "hook_unregister" else None
        if self.refuse or refuse:
            reply = {"t": "hook_error", "rid": msg["rid"], "code": self.refuse or refuse}
        elif msg["t"] == "hook_register":
            reply = {
                "t": "hook_ok",
                "rid": msg["rid"],
                "ref": msg["ref"],
                "public_id": PUB,
                "url": f"https://{PUB}.hook.battlelabos.com/",
            }
        else:
            reply = {"t": "hook_ok", "rid": msg["rid"], "ref": msg["ref"]}
        asyncio.get_running_loop().call_later(self.delay, self.link.on_message, reply)


def _app(seen: list, status: int = 202):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"accepted": True})

    return httpx.MockTransport(handler)


def _request(ref="wh1", body=b'{"action":"opened"}', headers=None):
    return {
        "t": "hook_request",
        "hid": HID,
        "ref": ref,
        "headers": headers
        if headers is not None
        else {"content-type": "application/json", "x-forgejo-signature": "ab", "cookie": "x"},
        "body_b64": base64.b64encode(body).decode(),
    }


async def _settle(link):
    for _ in range(50):
        await asyncio.sleep(0)
        if not link._tasks:
            return
    await asyncio.gather(*list(link._tasks))


# ---- the agent's HookLink ------------------------------------------------------------------------


def test_sync_registers_and_a_reconnect_re_registers():
    async def go():
        link = hooklink.HookLink(app_port=3402)
        ws = FakeWS(link)
        link.attach(ws)
        got = (await link.sync(["wh1", "wh2"], 1))["hooks"]
        assert set(got) == {"wh1", "wh2"} and got["wh1"]["public_id"] == PUB
        link.detach()
        ws2 = FakeWS(link)
        link.attach(ws2)  # a new control connection
        for _ in range(20):
            await asyncio.sleep(0)
        link.detach()
        return ws2.sent

    sent = asyncio.run(go())
    assert sorted(m["ref"] for m in sent if m["t"] == "hook_register") == ["wh1", "wh2"]


def test_sync_is_strict_and_needs_a_relay():
    async def go():
        link = hooklink.HookLink(app_port=3402)
        with pytest.raises(hooklink.HookError, match="bad_request"):
            await link.sync(["BAD!"], 1)
        with pytest.raises(hooklink.HookError, match="bad_request"):
            await link.sync(["wh1"], True)
        with pytest.raises(hooklink.HookError, match="no_relay"):
            await link.sync(["wh1"], 1)

    asyncio.run(go())


def test_a_forwarded_delivery_reaches_the_app_byte_exact_with_allowed_headers_only():
    seen: list = []

    async def go():
        link = hooklink.HookLink(app_port=3402, transport=_app(seen))
        ws = FakeWS(link)
        link.attach(ws)
        await link.sync(["wh1"], 1)
        body = b'{"a": 1, "b": "\\u00e9"}\n'
        link.on_message(_request(body=body))
        await _settle(link)
        link.detach()
        return ws.sent, body

    sent, body = asyncio.run(go())
    (req,) = seen
    assert str(req.url) == "http://127.0.0.1:3402/hooks/w/wh1" and req.method == "POST"
    assert req.content == body
    assert req.headers["x-forgejo-signature"] == "ab" and "cookie" not in req.headers
    assert {"t": "hook_response", "hid": HID, "status": 202} in sent


@pytest.mark.parametrize(
    "msg, app_status, want",
    [
        (_request(ref="unknown"), 202, 404),  # not a synced webhook
        ({**_request(), "body_b64": "!!notbase64"}, 202, 404),
        (_request(body=b"x" * (hooklink.BODY_MAX + 1)), 202, 413),
        (_request(), 503, 404),  # the app's other answers are not passed on
        (_request(), 429, 429),
    ],
)
def test_forward_outcomes(msg, app_status, want):
    seen: list = []

    async def go():
        link = hooklink.HookLink(app_port=3402, transport=_app(seen, app_status))
        ws = FakeWS(link)
        link.attach(ws)
        await link.sync(["wh1"], 1)
        link.on_message(msg)
        await _settle(link)
        link.detach()
        return [m for m in ws.sent if m["t"] == "hook_response"]

    assert asyncio.run(go()) == [{"t": "hook_response", "hid": HID, "status": want}]


def test_a_full_agent_answers_429_without_forwarding():
    seen: list = []

    async def go():
        link = hooklink.HookLink(app_port=3402, transport=_app(seen))
        ws = FakeWS(link)
        link.attach(ws)
        await link.sync(["wh1"], 1)
        link._inflight = hooklink.INFLIGHT_MAX
        link.on_message(_request())
        await _settle(link)
        link.detach()
        return [m for m in ws.sent if m["t"] == "hook_response"]

    assert asyncio.run(go()) == [{"t": "hook_response", "hid": HID, "status": 429}]
    assert seen == []


def test_the_peer_socket_ops_are_strict():
    async def go():
        link = hooklink.HookLink(app_port=3402)
        pipe = PeerPipe.__new__(PeerPipe)
        pipe._hooks = link
        no = await pipe._hook({"op": "hook_sync", "refs": ["wh1"], "gen": 1, "extra": 1})
        no_gen = await pipe._hook({"op": "hook_sync", "refs": ["wh1"]})
        offline = await pipe._hook({"op": "hook_sync", "refs": ["wh1"], "gen": 1})
        stale = await pipe._hook({"op": "hook_sync", "refs": ["wh1"], "gen": 1})
        status = await pipe._hook({"op": "hook_status"})
        pipe._hooks = None
        none = await pipe._hook({"op": "hook_unregister", "ref": "wh1", "gen": 2})
        return no, no_gen, offline, stale, status, none

    no, no_gen, offline, stale, status, none = asyncio.run(go())
    assert no == no_gen == {"ok": False, "code": "bad_request"}
    assert offline == {"ok": False, "code": "no_relay"}
    assert stale == {"ok": False, "code": "stale"}
    assert status["ok"] is True and status["gen"] == 1 and status["attached"] is False
    assert none == {"ok": False, "code": "no_relay"}


def test_a_refused_unregister_keeps_the_webhook_forwarding():
    seen: list = []

    async def go():
        link = hooklink.HookLink(app_port=3402, transport=_app(seen))
        ws = FakeWS(link, refuse_unregister="busy")
        link.attach(ws)
        await link.sync(["wh1"], 1)
        with pytest.raises(hooklink.HookError, match="busy"):
            await link.unregister("wh1", 2)
        link.on_message(_request())
        await _settle(link)
        ws.refuse_unregister = None
        await link.unregister("wh1", 3)  # confirmed: now it is gone here too
        link.on_message(_request())
        await _settle(link)
        link.detach()
        return [m["status"] for m in ws.sent if m["t"] == "hook_response"]

    assert asyncio.run(go()) == [202, 404]
    assert len(seen) == 1


def test_an_older_request_never_undoes_a_newer_one():
    async def go():
        link = hooklink.HookLink(app_port=3402)
        ws = FakeWS(link)
        link.attach(ws)
        await link.sync(["wh1"], 5)
        await link.unregister("wh1", 6)
        with pytest.raises(hooklink.HookError, match="stale"):
            await link.sync(["wh1", "wh2"], 4)  # a slow snapshot from before the unregister
        with pytest.raises(hooklink.HookError, match="stale"):
            await link.unregister("wh2", 6)
        await link.sync(["wh2"], 7)
        link.detach()
        return link._refs, [(m["t"], m["ref"]) for m in ws.sent]

    refs, sent = asyncio.run(go())
    after = sent[sent.index(("hook_unregister", "wh1")) + 1 :]
    assert refs == {"wh2"} and after == [("hook_register", "wh2")]  # wh1 never came back


def test_status_shows_a_restart_and_a_reconnect():
    async def go():
        a = hooklink.HookLink(app_port=3402)
        a.attach(FakeWS(a))
        first = await a.sync(["wh1"], 1)
        a.detach()
        a.attach(FakeWS(a))
        reconnected = a.status()
        a.detach()
        b = hooklink.HookLink(app_port=3402)  # the agent restarted
        b.attach(FakeWS(b))
        restarted = b.status()
        b.detach()
        return first, reconnected, restarted, b._refs

    first, reconnected, restarted, refs = asyncio.run(go())
    assert (first["boot"], first["epoch"]) == (reconnected["boot"], 1)
    assert reconnected["epoch"] == 2 and reconnected["attached"] is True
    assert restarted["boot"] != first["boot"] and refs == set()


# ---- the app side --------------------------------------------------------------------------------


def test_a_fresh_store_is_at_v4_with_the_relay_columns():
    webhooks.list_all()
    with store._read() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
        cols = {r[1] for r in con.execute("PRAGMA table_info(webhooks)")}
    assert {"relay_public_id", "relay_url"} <= cols


def test_sync_records_the_relay_address_and_clears_an_unconfirmed_one(monkeypatch):
    a, _ = webhooks.create("a")
    b, _ = webhooks.create("b")

    async def ask(request, timeout=60.0):
        assert request["op"] == "hook_sync" and sorted(request["refs"]) == sorted(
            [a["id"], b["id"]]
        )
        assert isinstance(request["gen"], int)
        return {
            "ok": True,
            "hooks": {a["id"]: {"public_id": PUB, "url": f"https://{PUB}.hook.x/"}},
            "boot": "b1",
            "epoch": 1,
        }

    monkeypatch.setattr(webhook_relay, "_ask", ask)
    asyncio.run(webhook_relay.sync_all())
    assert webhooks.get(a["id"])["relay_url"] == f"https://{PUB}.hook.x/"
    assert webhooks.get(b["id"])["relay_url"] is None


def test_a_bad_relay_address_is_never_stored():
    a, _ = webhooks.create("a")
    with pytest.raises(webhooks.WebhookError):
        webhooks.set_relay(a["id"], "NOT-BASE32", "https://x/")
    with pytest.raises(webhooks.WebhookError):
        webhooks.set_relay(a["id"], PUB, "https://evil.example/")


@pytest.fixture
def api():
    app = FastAPI()
    routes.register(app, logged_in=lambda: "operator", csrf_guard=lambda: None)
    with TestClient(app) as c:
        yield c


def test_create_on_a_home_free_box_shows_the_relay_address(api, monkeypatch):
    monkeypatch.setattr(webhook_relay, "available", lambda: True)

    async def ask(request, timeout=60.0):
        return {
            "ok": True,
            "hooks": {
                r: {"public_id": PUB, "url": f"https://{PUB}.hook.x/"} for r in request["refs"]
            },
        }

    monkeypatch.setattr(webhook_relay, "_ask", ask)
    made = api.post("/api/webhooks", json={"name": "shop"}).json()
    assert made["webhook"]["relay_url"] == f"https://{PUB}.hook.x/" and len(made["secret"]) == 64


def test_create_with_the_relay_offline_still_works_with_the_direct_address(api, monkeypatch):
    monkeypatch.setattr(webhook_relay, "available", lambda: True)

    async def ask(request, timeout=60.0):
        raise webhook_relay.RelayError("no_relay")

    monkeypatch.setattr(webhook_relay, "_ask", ask)
    made = api.post("/api/webhooks", json={"name": "shop"})
    assert made.status_code == 201 and made.json()["webhook"]["relay_url"] is None


def test_delete_removes_it_at_the_relay_first_and_keeps_it_if_the_relay_cant_confirm(
    api, monkeypatch
):
    a, _ = webhooks.create("a")
    webhooks.set_relay(a["id"], PUB, f"https://{PUB}.hook.x/")
    calls: list = []

    async def refuse(request, timeout=60.0):
        calls.append(request)
        raise webhook_relay.RelayError("no_relay")

    monkeypatch.setattr(webhook_relay, "_ask", refuse)
    r = api.delete(f"/api/webhooks/{a['id']}")
    assert r.status_code == 502 and webhooks.get(a["id"])["relay_url"]
    # the unregister, then the re-registration owed by its unknown outcome (offline too here)
    assert [c["op"] for c in calls] == ["hook_unregister", "hook_sync"]
    assert calls[0]["ref"] == a["id"] and a["id"] in calls[1]["refs"]

    async def confirm(request, timeout=60.0):
        return {"ok": True}

    monkeypatch.setattr(webhook_relay, "_ask", confirm)
    assert api.delete(f"/api/webhooks/{a['id']}").json() == {"deleted": a["id"]}


def test_a_sync_and_a_delete_never_overlap_and_generations_rise(monkeypatch):
    a, _ = webhooks.create("a")
    webhooks.set_relay(a["id"], PUB, f"https://{PUB}.hook.x/")
    log: list = []

    async def ask(request, timeout=60.0):
        log.append((request["op"], request.get("gen"), "start"))
        if request["op"] == "hook_sync":
            await asyncio.sleep(0.05)  # a slow sync: the delete must wait for it
        log.append((request["op"], request.get("gen"), "end"))
        return {"ok": True, "hooks": {}, "boot": "b", "epoch": 1}

    monkeypatch.setattr(webhook_relay, "_ask", ask)

    async def go():
        sync = asyncio.create_task(webhook_relay.sync_all())
        await asyncio.sleep(0)
        await webhook_relay.delete(a["id"], lambda: webhooks.delete(a["id"]))
        await sync

    asyncio.run(go())
    assert [(op, edge) for op, _, edge in log] == [
        ("hook_sync", "start"),
        ("hook_sync", "end"),
        ("hook_unregister", "start"),
        ("hook_unregister", "end"),
    ]
    assert log[0][1] < log[2][1]
    with pytest.raises(store.NotFound):
        webhooks.get(a["id"])


def test_a_clock_gone_back_is_bumped_past_the_agent_once(monkeypatch):
    sent: list = []

    async def ask(request, timeout=60.0):
        sent.append(request)
        if request["op"] == "hook_status":
            return {"ok": True, "gen": 10**19}
        if request["gen"] <= 10**19:
            raise webhook_relay.RelayError("stale")
        return {"ok": True, "hooks": {}, "boot": "b", "epoch": 1}

    monkeypatch.setattr(webhook_relay, "_ask", ask)
    asyncio.run(webhook_relay.sync_all())
    assert [r["op"] for r in sent] == ["hook_sync", "hook_status", "hook_sync"]
    assert sent[-1]["gen"] == 10**19 + 1


def test_a_restarted_or_reconnected_agent_is_synced_again_soon(monkeypatch):
    status = {"ok": True, "boot": "b1", "epoch": 1, "attached": True, "gen": 1}

    async def ask(request, timeout=60.0):
        if request["op"] == "hook_status":
            return status
        return {"ok": True, "hooks": {}, "complete": True, "boot": "b1", "epoch": 1}

    monkeypatch.setattr(webhook_relay, "_ask", ask)

    async def due():
        return await webhook_relay._due()

    assert asyncio.run(due()) is True  # never synced yet
    asyncio.run(webhook_relay.sync_all())
    assert asyncio.run(due()) is False
    status["epoch"] = 2  # the agent reconnected (an address may have been confirmed meanwhile)
    assert asyncio.run(due()) is True
    status.update(epoch=1, boot="b2")  # the agent restarted with an empty set
    assert asyncio.run(due()) is True
    status["attached"] = False  # no relay connection yet: nothing to sync to
    assert asyncio.run(due()) is False


def test_a_failed_sync_is_owed_until_one_completes(monkeypatch):
    async def offline(request, timeout=60.0):
        raise webhook_relay.RelayError("no_relay")

    clock = [1000.0]
    monkeypatch.setattr(webhook_relay, "_ask", offline)
    monkeypatch.setattr(webhook_relay, "_now", lambda: clock[0])
    with pytest.raises(webhook_relay.RelayError):
        asyncio.run(webhook_relay.sync_all())
    assert webhook_relay._dirty is True
    assert asyncio.run(webhook_relay._due()) is False  # backing off
    clock[0] += webhook_relay.RETRY_MIN_S
    assert asyncio.run(webhook_relay._due()) is True
    with pytest.raises(webhook_relay.RelayError):
        asyncio.run(webhook_relay.sync_all())
    clock[0] += webhook_relay.RETRY_MIN_S
    assert asyncio.run(webhook_relay._due()) is False  # the backoff doubled
    clock[0] += webhook_relay.RETRY_MIN_S
    assert asyncio.run(webhook_relay._due()) is True


def test_a_delete_refused_after_the_relay_removal_registers_it_again(api, monkeypatch):
    a, _ = webhooks.create("a")
    webhooks.set_relay(a["id"], PUB, f"https://{PUB}.hook.x/")
    calls: list = []

    async def ask(request, timeout=60.0):
        calls.append(request)
        hooks = {
            r: {"public_id": PUB, "url": f"https://{PUB}.hook.x/"} for r in request.get("refs", [])
        }
        return {"ok": True, "hooks": hooks, "boot": "b", "epoch": 1}

    def picked_meanwhile(wid):
        raise webhooks.WebhookError("this webhook is used by an automation; change that first", 409)

    monkeypatch.setattr(webhook_relay, "_ask", ask)
    monkeypatch.setattr(webhooks, "delete", picked_meanwhile)
    r = api.delete(f"/api/webhooks/{a['id']}")
    assert r.status_code == 409
    assert [c["op"] for c in calls] == ["hook_unregister", "hook_sync"]
    assert a["id"] in calls[1]["refs"] and calls[0]["gen"] < calls[1]["gen"]
    assert webhooks.get(a["id"])["relay_url"] == f"https://{PUB}.hook.x/"


def _bridge(link, *, lose: set | None = None):
    """The app's ``_ask`` wired to a real HookLink through the peer socket's handler. A request
    whose op is in ``lose`` runs in the agent, but its reply never reaches the app."""
    pipe = PeerPipe.__new__(PeerPipe)
    pipe._hooks = link
    lose = lose if lose is not None else set()

    async def ask(request, timeout=60.0):
        answer = await pipe._hook(request)
        if request["op"] in lose:
            raise webhook_relay.RelayError("no_relay")
        if answer.get("ok") is not True:
            raise webhook_relay.RelayError(answer.get("code") or "refused")
        return answer

    return ask


def test_a_registration_that_failed_is_retried_on_the_same_connection(monkeypatch):
    a, _ = webhooks.create("a")
    clock = [1000.0]
    monkeypatch.setattr(webhook_relay, "_now", lambda: clock[0])

    async def go():
        link = hooklink.HookLink(app_port=3402)
        ws = FakeWS(link, refuse="busy")
        link.attach(ws)
        monkeypatch.setattr(webhook_relay, "_ask", _bridge(link))
        await webhook_relay.sync_all()  # the relay refuses this once
        first = (webhooks.get(a["id"])["relay_url"], webhook_relay._dirty)
        ws.refuse = None  # it recovers, no reconnect
        clock[0] += webhook_relay.RETRY_MIN_S
        due = await webhook_relay._due()
        await webhook_relay.sync_all()
        link.detach()
        return first, due

    first, due = asyncio.run(go())
    assert first == (None, True) and due is True
    assert webhooks.get(a["id"])["relay_url"] == f"https://{PUB}.hook.battlelabos.com/"
    assert webhook_relay._dirty is False


def test_an_address_not_reached_this_time_stays_shown(monkeypatch):
    a, _ = webhooks.create("a")
    webhooks.set_relay(a["id"], PUB, f"https://{PUB}.hook.x/")

    async def ask(request, timeout=60.0):  # the relay stopped answering before this ref
        return {"ok": True, "hooks": {}, "refused": [], "complete": False, "boot": "b", "epoch": 1}

    monkeypatch.setattr(webhook_relay, "_ask", ask)
    asyncio.run(webhook_relay.sync_all())
    assert webhooks.get(a["id"])["relay_url"] == f"https://{PUB}.hook.x/"
    assert webhook_relay._dirty is True


def test_a_lost_unregister_reply_registers_the_retained_webhook_again(monkeypatch):
    a, _ = webhooks.create("a")
    deleted: list = []

    async def go():
        link = hooklink.HookLink(app_port=3402)
        link.attach(FakeWS(link))
        monkeypatch.setattr(webhook_relay, "_ask", _bridge(link))
        await webhook_relay.sync_all()
        assert a["id"] in link._refs
        monkeypatch.setattr(webhook_relay, "_ask", _bridge(link, lose={"hook_unregister"}))
        with pytest.raises(webhook_relay.RelayError):
            await webhook_relay.delete(a["id"], lambda: deleted.append(a["id"]))
        link.detach()
        return link._refs

    refs = asyncio.run(go())
    assert deleted == [] and a["id"] in refs  # the agent forwards it again
    assert webhooks.get(a["id"])["relay_url"]


def test_a_lost_unregister_reply_with_the_agent_gone_too_is_owed(monkeypatch):
    a, _ = webhooks.create("a")
    clock = [1000.0]
    monkeypatch.setattr(webhook_relay, "_now", lambda: clock[0])

    async def go():
        link = hooklink.HookLink(app_port=3402)
        link.attach(FakeWS(link))
        monkeypatch.setattr(webhook_relay, "_ask", _bridge(link))
        await webhook_relay.sync_all()
        monkeypatch.setattr(
            webhook_relay, "_ask", _bridge(link, lose={"hook_unregister", "hook_sync"})
        )
        with pytest.raises(webhook_relay.RelayError):
            await webhook_relay.delete(a["id"], lambda: None)
        link.detach()

    asyncio.run(go())
    assert webhook_relay._dirty is True
    clock[0] += webhook_relay.RETRY_MIN_S
    assert asyncio.run(webhook_relay._due()) is True


def test_a_cancelled_unregister_leaves_a_sync_owed_at_once(monkeypatch):
    a, _ = webhooks.create("a")

    async def hang(request, timeout=60.0):
        if request["op"] == "hook_sync":
            return {"ok": True, "hooks": {}, "complete": True, "boot": "b", "epoch": 1}
        await asyncio.sleep(3600)

    monkeypatch.setattr(webhook_relay, "_ask", hang)

    async def go():
        await webhook_relay.sync_all()
        assert webhook_relay._dirty is False
        task = asyncio.create_task(webhook_relay.delete(a["id"], lambda: None))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await webhook_relay._due()

    assert asyncio.run(go()) is True


def test_a_create_that_times_out_behind_an_older_sync_is_registered_by_the_next_poll(monkeypatch):
    webhooks.create("a")
    webhooks.create("b")
    monkeypatch.setattr(webhook_relay, "available", lambda: True)
    monkeypatch.setattr(routes, "CREATE_RELAY_WAIT_S", 0.02)
    app = FastAPI()
    routes.register(app, logged_in=lambda: "operator", csrf_guard=lambda: None)

    async def go():
        link = hooklink.HookLink(app_port=3402)
        ws = FakeWS(link)
        ws.delay = 0.03  # each registration answers slowly: the older sync outlasts the wait
        link.attach(ws)
        monkeypatch.setattr(webhook_relay, "_ask", _bridge(link))
        older = asyncio.create_task(webhook_relay.sync_all())
        await asyncio.sleep(0.01)  # it has its snapshot (a, b) and holds the lock
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            made = await client.post("/api/webhooks", json={"name": "new"})
        await older
        new = made.json()["webhook"]
        after_older = (new["id"] in link._refs, await webhook_relay._due())
        await webhook_relay.sync_all()  # the next poll
        link.detach()
        return made.status_code, new, after_older, link._refs

    status, new, (listed, due), refs = asyncio.run(go())
    assert status == 201 and new["relay_url"] is None
    assert listed is False and due is True  # the older sync didn't settle the newer revision
    assert new["id"] in refs and webhooks.get(new["id"])["relay_url"]
    assert webhook_relay._dirty is False
