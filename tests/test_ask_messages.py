"""HQ proposes a message, the Commander taps Send (#1833, #1746 P2a D7).

A real HQ turn stages the card (`list_connectors` → `list_recipients` → `propose_message`), and the
tap goes through the real decide route, against fake Telegram and Matrix servers that record every
request. Pinned here:

* the text sent is the stored entry's, byte for byte, and the decide body carries nothing else;
* the fence: a recipient removed, a generation moved, a secret registered since, a Matrix room that
  became encrypted or can't be checked — `stale`, nothing sent;
* outcomes from the provider's own facts: `sent` needs its message id; a dropped connection after
  the request went out is `unconfirmed` (Telegram: one request, never retried; Matrix: the same
  txnId once more, then `sent`); a refused connection is `failed` with nothing sent;
* one tap sends once (a second tap returns the state); the rate cap is counted at the claim;
* the two-process race and the SIGKILL cases are in `test_ask_messages_processes.py`.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from agent_sessions import ask_messages, connector_http, prefs, review, template_secrets
from agent_sessions.connectors import candidates, service, store
from test_ask_orchestrator import App, Script, answer, call
from test_connectors_messengers import (
    MX_ORIGIN,
    MX_TOKEN,
    TG_TOKEN,
    Matrix,
    Telegram,
    run,
)

TRICKY = "Deploy at 22:00–23:00 UTC.\n\n\t• first line\n• ẞ café שלום 🙏\nend"


class SendingTelegram(Telegram):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []
        self.mode = "ok"  # ok | drop | refuse | 500 | 400 | no_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            self.requests.append(request)
            if self.mode == "refuse":
                raise httpx.ConnectError("refused", request=request)
            body = json.loads(request.content)
            self.sent.append(body)
            if self.mode == "drop":
                raise httpx.RemoteProtocolError("dropped", request=request)
            if self.mode in ("500", "400"):
                return httpx.Response(int(self.mode), json={"ok": False})
            if self.mode == "no_id":
                return httpx.Response(200, json={"ok": True, "result": {}})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})
        return super().handler(request)


class SendingMatrix(Matrix):
    def __init__(self) -> None:
        super().__init__()
        self.puts: list[tuple[str, dict]] = []
        self.drop_first = False
        self.after_first = None  # called when the first PUT is dropped
        self.events: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.requests.append(request)
            if request.headers.get("authorization") != f"Bearer {MX_TOKEN}":
                return httpx.Response(401, json={})
            path = request.url.raw_path.decode()
            txn = path.rsplit("/", 1)[-1]
            self.puts.append((txn, json.loads(request.content)))
            # Idempotent by txnId: a repeat answers the event already stored.
            eid = self.events.setdefault(txn, f"$event{len(self.events)}")
            if self.drop_first and len(self.puts) == 1:
                if self.after_first is not None:
                    self.after_first()
                raise httpx.ReadTimeout("no reply", request=request)
            return httpx.Response(200, json={"event_id": eid})
        return super().handler(request)


@pytest.fixture
def world(monkeypatch, auth_cfg):
    tg, mx = SendingTelegram(), SendingMatrix()

    async def resolve(host, port):
        return {"api.telegram.org": ["149.154.167.220"], "matrix.example.org": ["10.0.0.7"]}[host]

    def route(request: httpx.Request) -> httpx.Response:
        return (tg if request.headers.get("host") == "api.telegram.org" else mx).handler(request)

    monkeypatch.setattr(connector_http, "_resolve", resolve)
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(route))
    prefs.set_ai_review(
        {"egress_acknowledged": True, "enabled": True, "base_url": "https://ai.test/v1",
         "api_key": "sk-ask-test", "model": "m"}
    )  # fmt: skip
    app = App(auth_cfg)
    # ONE event loop for the whole test: a tap answers 202 and the send runs on as a server task,
    # which a per-request loop would cancel the moment the response is written.
    app.c.__enter__()
    try:
        yield app, tg, mx
    finally:
        app.c.__exit__(None, None, None)
        for iid in list(candidates._SNAP):
            candidates.forget(iid)


def _messenger(kind: str) -> dict:
    inst = store.add(kind)
    body = {"token": TG_TOKEN} if kind == "telegram" else {"origin": MX_ORIGIN, "token": MX_TOKEN}
    store.patch(inst["id"], body)
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    run(candidates.list_candidates(inst["id"]))
    rid = "42" if kind == "telegram" else "!ops:example.org"
    store.set_recipients(inst["id"], [rid], candidates.snapshot(inst["id"]))
    return store.set_send(inst["id"], True)


def _propose(app: App, monkeypatch, text: str, *, recipient_index: int = 0):
    script = Script(
        call("list_connectors", {}, cid="c1"),
        call("list_recipients", {"connector_index": 0}, cid="c2"),
        call(
            "propose_message",
            {"connector_index": 0, "recipient_index": recipient_index, "text": text},
            cid="c3",
        ),
        answer("Here it is; tap Send when you're ready."),
    )
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    cid = app.new()
    turn = app.ask(cid, "tell marcus the deploy window").json()["turn"]
    assert turn["status"] == "done", turn
    (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
    return cid, card, script


def _decide(app: App, pid: str, body: dict):
    return app.c.post(f"/api/ask/proposals/{pid}/decide", json=body, headers=app.hdr)


def _settled(app: App, cid: str, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    while True:
        (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
        if card["state"] != "deciding" or time.monotonic() > end:
            return card
        time.sleep(0.05)


# ---- staging and the bytes ---------------------------------------------------------------------


def test_a_proposed_message_is_a_card_with_the_exact_text_and_a_tap_sends_those_bytes(
    world, monkeypatch
):
    app, tg, _ = world
    _messenger("telegram")
    cid, card, script = _propose(app, monkeypatch, "  " + TRICKY.replace("\n", "\r\n") + "  \x07")
    assert card["kind"] == "message" and card["state"] == "pending"
    assert card["text"] == TRICKY  # CRLF → LF, the bell gone, outer spaces trimmed; nothing else
    assert card["recipient"] == "Marcus (@marcus)" and card["connector"] == "Telegram"
    tool = json.loads(script.tool_results(3)[-1]["content"])
    assert tool["proposed"] is True and "nothing has been sent" in tool["note"]
    assert tg.sent == []  # proposing sends nothing
    r = _decide(app, card["action_id"], {"decision": "send"})
    assert r.status_code == 202
    done = _settled(app, cid)
    assert done["state"] == "sent"
    (sent,) = tg.sent
    assert sent == {"chat_id": 42, "text": TRICKY, "link_preview_options": {"is_disabled": True}}
    # The same tap again returns the state; nothing more is sent.
    again = _decide(app, card["action_id"], {"decision": "send"})
    assert again.status_code == 200 and again.json()["proposal"]["state"] == "sent"
    assert len(tg.sent) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "send", "text": "something else"},
        {"decision": "send", "recipient": "-1009"},
        {"decision": "send", "inputs": {}},
        {"decision": "send", "bypass": False},
    ],
)
def test_a_send_carries_nothing_but_the_decision(world, monkeypatch, body):
    app, tg, _ = world
    _messenger("telegram")
    _cid, card, _ = _propose(app, monkeypatch, "hello")
    assert _decide(app, card["action_id"], body).status_code == 422
    assert tg.sent == []


def test_confirm_or_start_on_a_message_is_refused(world, monkeypatch):
    app, tg, _ = world
    _messenger("telegram")
    _cid, card, _ = _propose(app, monkeypatch, "hello")
    for decision in ("confirm", "start"):
        assert _decide(app, card["action_id"], {"decision": decision}).status_code == 422
    assert tg.sent == []


def test_a_message_over_the_providers_limit_is_refused_at_staging(world, monkeypatch):
    app, _tg, _ = world
    _messenger("telegram")
    script = Script(
        call("list_connectors", {}, cid="c1"),
        call("list_recipients", {"connector_index": 0}, cid="c2"),
        call(
            "propose_message",
            {"connector_index": 0, "recipient_index": 0, "text": "x" * 4097},
            cid="c3",
        ),
        answer("too long"),
    )
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    cid = app.new()
    app.ask(cid, "send a long one")
    assert [p for t in app.get(cid)["turns"] for p in t["proposals"]] == []
    assert "4096" in script.tool_results(3)[-1]["content"]


def test_a_credential_shaped_message_is_refused_at_staging(world, monkeypatch):
    app, _tg, _ = world
    _messenger("telegram")
    secret = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    script = Script(
        call("list_connectors", {}, cid="c1"),
        call("list_recipients", {"connector_index": 0}, cid="c2"),
        call(
            "propose_message",
            {"connector_index": 0, "recipient_index": 0, "text": secret},
            cid="c3",
        ),
        answer("no"),
    )
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    cid = app.new()
    app.ask(cid, "send the key")
    assert [p for t in app.get(cid)["turns"] for p in t["proposals"]] == []


# ---- the fence -------------------------------------------------------------------------------


def test_a_recipient_removed_before_the_tap_is_stale_and_nothing_is_sent(world, monkeypatch):
    app, tg, _ = world
    inst = _messenger("telegram")
    cid, card, _ = _propose(app, monkeypatch, "hello")
    run(candidates.list_candidates(inst["id"]))
    store.set_recipients(inst["id"], ["-1009"], candidates.snapshot(inst["id"]))
    r = _decide(app, card["action_id"], {"decision": "send"})
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "stale"
    assert "no longer an allowed recipient" in r.json()["proposal"]["reason"]
    assert tg.sent == []


def test_a_generation_moved_before_the_tap_is_stale(world, monkeypatch):
    app, tg, _ = world
    inst = _messenger("telegram")
    _cid, card, _ = _propose(app, monkeypatch, "hello")
    store.patch(inst["id"], {"token": TG_TOKEN.replace("sentinel", "rotatedd")})
    r = _decide(app, card["action_id"], {"decision": "send"})
    assert r.json()["proposal"]["state"] == "stale" and tg.sent == []


def test_a_secret_registered_after_staging_makes_the_card_stale(world, monkeypatch):
    app, tg, _ = world
    _messenger("telegram")
    word = "zq7-project-codename-blackbird"
    _cid, card, _ = _propose(app, monkeypatch, f"ship {word} tonight")
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [word, *([] if True else [])])
    r = _decide(app, card["action_id"], {"decision": "send"})
    assert r.json()["proposal"]["state"] == "stale" and tg.sent == []


@pytest.mark.parametrize("change", ["encrypted", "left", "lookup_403", "members_403"])
def test_a_matrix_room_is_checked_live_at_the_send(world, monkeypatch, change):
    app, _tg, mx = world
    _messenger("matrix")
    cid, card, _ = _propose(app, monkeypatch, "hello room")
    if change == "encrypted":
        mx.rooms["!ops:example.org"]["encrypted"] = True
    elif change == "left":
        del mx.rooms["!ops:example.org"]
    elif change == "lookup_403":
        mx.encryption_status["!ops:example.org"] = 403
    else:  # who would read it can't be checked (#1832, Hermes 7605)
        mx.members_status["!ops:example.org"] = 403
    assert _decide(app, card["action_id"], {"decision": "send"}).status_code == 202
    done = _settled(app, cid)
    assert done["state"] == "stale" and mx.puts == []
    assert (
        done["reason"].startswith("that room can't be used:")
        or "no longer in that room" in done["reason"]
    ), done["reason"]


# ---- the outbound boundary (Hermes 7610) -------------------------------------------------------


def test_a_recipient_withdrawn_after_the_claim_is_never_sent_to(world, monkeypatch):
    """The claim saw the chat allowed; the Commander withdrew it before the request left. The
    final check at the outbound boundary sees the allowlist as it is then: nothing is sent."""
    app, tg, _ = world
    inst = _messenger("telegram")
    cid, card, _ = _propose(app, monkeypatch, "hello")

    async def withdraw():
        await candidates.list_candidates(inst["id"])
        store.set_recipients(inst["id"], ["-1009"], candidates.snapshot(inst["id"]))

    monkeypatch.setattr(ask_messages, "_HOOK_BEFORE_SEND", withdraw)
    assert _decide(app, card["action_id"], {"decision": "send"}).status_code == 202
    done = _settled(app, cid)
    assert done["state"] == "stale", done
    assert "no longer an allowed recipient" in done["reason"] and tg.sent == []


def test_a_withdrawal_completed_just_after_the_last_check_may_still_send_once(world, monkeypatch):
    """The accepted exception (#1833; operator, PR #1839 comments 118515 and 118748), the other
    side of the test above: the last check is not held across the write, so a withdrawal that
    completes after it returns is not caught and this one message goes out. It goes out once,
    and the allowlist keeps the withdrawal."""
    app, tg, _ = world
    inst = _messenger("telegram")
    cid, card, _ = _propose(app, monkeypatch, "hello")
    run(candidates.list_candidates(inst["id"]))
    snap = candidates.snapshot(inst["id"])
    real = ask_messages._final_check

    def withdraw_after_the_check(row):
        check = real(row)

        def checked_then_withdrawn(fenced: bool) -> None:
            check(fenced)
            store.set_recipients(inst["id"], ["-1009"], snap)

        return checked_then_withdrawn

    monkeypatch.setattr(ask_messages, "_final_check", withdraw_after_the_check)
    assert _decide(app, card["action_id"], {"decision": "send"}).status_code == 202
    done = _settled(app, cid)
    assert done["state"] == "sent" and len(tg.sent) == 1, done
    allowed = {
        r["id"] for i in store.read()["instances"] if i["id"] == inst["id"] for r in i["recipients"]
    }
    assert allowed == {"-1009"}


def test_a_secret_registered_after_the_claim_never_leaves(world, monkeypatch):
    """A secret registered between the claim and the request: the final check runs under the
    registration fence against the secrets known then, and no request is made."""
    app, tg, _ = world
    _messenger("telegram")
    word = "zq7-codename-nightjar-4417"
    cid, card, _ = _propose(app, monkeypatch, f"ship {word} tonight")
    monkeypatch.setattr(template_secrets, "_typed", {})

    async def register():
        template_secrets.register_typed([word])

    monkeypatch.setattr(ask_messages, "_HOOK_BEFORE_SEND", register)
    assert _decide(app, card["action_id"], {"decision": "send"}).status_code == 202
    done = _settled(app, cid)
    assert done["state"] == "stale" and "now holds a secret" in done["reason"], done
    assert tg.sent == [] and not any(word in json.dumps(b) for b in tg.sent)


def test_a_matrix_card_carries_the_rooms_live_member_count(world, monkeypatch):
    """Who would read it is on the card before Send: counted live when HQ staged it, through the
    real candidate → stored recipient → proposal path."""
    app, _tg, _mx = world
    _messenger("matrix")
    _cid, card, _ = _propose(app, monkeypatch, "hello room")
    assert (card["recipient_kind"], card["recipient_members"]) == ("room", 4)


def test_a_matrix_room_that_cannot_be_counted_now_is_refused_at_staging(world, monkeypatch):
    app, _tg, mx = world
    _messenger("matrix")
    mx.members_status["!ops:example.org"] = 403
    script = Script(
        call("list_connectors", {}, cid="c1"),
        call("list_recipients", {"connector_index": 0}, cid="c2"),
        call("propose_message", {"connector_index": 0, "recipient_index": 0, "text": "hi"},
             cid="c3"),
        answer("I couldn't propose that."),
    )  # fmt: skip
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    cid = app.new()
    assert app.ask(cid, "tell ops hi").json()["turn"]["status"] == "done"
    assert [p for t in app.get(cid)["turns"] for p in t["proposals"]] == []
    assert "members couldn't be checked" in json.dumps(script.bodies[-1])
    assert mx.puts == []


def test_the_final_check_runs_again_before_the_first_byte_on_a_real_connection(monkeypatch):
    """With a real httpcore connection (not a mock transport), the check runs a second time from
    the ``send_request_headers.started`` trace, after connect and before any byte is written."""
    import http.server
    import threading

    hits: list[str] = []

    class Homeserver(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("content-length") or 0))
            hits.append(self.path)
            body = b'{"event_id": "$e1"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Homeserver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(connector_http, "_TRANSPORT", None)
        monkeypatch.setattr(connector_http, "_credential", lambda bound: ("Bearer t", ""))
        bound = connector_http.Bound(
            instance_id="c_0123456789ab", generation=1,
            origin=f"http://127.0.0.1:{srv.server_address[1]}", auth_scheme="Bearer",
            label="Matrix", adapter="matrix-cs", for_read=False, for_send=True,
        )  # fmt: skip
        calls: list[bool] = []

        def refuse_at_the_byte(fenced: bool) -> None:
            calls.append(fenced)
            if not fenced:
                raise connector_http.ConnectorError("secret_now", "the message now holds a secret")

        out = run(connector_http._send(bound, "!ops:example.org", "hi", "k1",
                                       final_check=refuse_at_the_byte))  # fmt: skip
        assert calls == [True, False]
        assert (out["outcome"], out["code"]) == ("failed", "secret_now") and hits == []
        out = run(connector_http._send(bound, "!ops:example.org", "hi", "k2",
                                       final_check=lambda fenced: None))  # fmt: skip
        assert out["outcome"] == "sent" and len(hits) == 1
    finally:
        srv.shutdown()


# ---- outcomes --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "state", "requests"),
    [
        ("drop", "unconfirmed", 1),  # the request went out; no answer → never retried
        ("500", "unconfirmed", 1),
        ("no_id", "unconfirmed", 1),
        ("400", "failed", 1),  # the provider's own refusal
        ("refuse", "failed", 1),  # refused before a byte left (the attempt is recorded)
    ],
)
def test_telegram_outcomes_come_from_the_providers_facts(world, monkeypatch, mode, state, requests):
    app, tg, _ = world
    _messenger("telegram")
    cid, card, _ = _propose(app, monkeypatch, "hello")
    tg.mode = mode
    _decide(app, card["action_id"], {"decision": "send"})
    done = _settled(app, cid)
    assert done["state"] == state, done
    assert len([r for r in tg.requests if r.url.path.endswith("/sendMessage")]) == requests
    assert TG_TOKEN not in json.dumps(done)


def test_matrix_repeats_the_same_txn_once_and_the_homeserver_deduplicates(world, monkeypatch):
    app, _tg, mx = world
    _messenger("matrix")
    cid, card, _ = _propose(app, monkeypatch, "hello room")
    mx.drop_first = True
    _decide(app, card["action_id"], {"decision": "send"})
    done = _settled(app, cid)
    assert done["state"] == "sent"
    assert [t for t, _ in mx.puts] == [card["action_id"], card["action_id"]]
    assert mx.puts[0][1] == {"msgtype": "m.text", "body": "hello room"}


@pytest.mark.parametrize("change", ["encrypted", "left", "members_403"])
def test_a_matrix_repeat_rechecks_the_room_live_and_skips_an_ineligible_one(
    world, monkeypatch, change
):
    """The first PUT got no answer; before the repeat the room changed. No repeat is made, and the
    outcome stays `unconfirmed` (the first may have been stored) (Hermes 7612)."""
    app, _tg, mx = world
    _messenger("matrix")
    cid, card, _ = _propose(app, monkeypatch, "hello room")
    mx.drop_first = True

    def changed():
        if change == "encrypted":
            mx.rooms["!ops:example.org"]["encrypted"] = True
        elif change == "left":
            del mx.rooms["!ops:example.org"]
        else:
            mx.members_status["!ops:example.org"] = 403

    mx.after_first = changed
    _decide(app, card["action_id"], {"decision": "send"})
    done = _settled(app, cid)
    assert done["state"] == "unconfirmed", done
    assert [t for t, _ in mx.puts] == [card["action_id"]]


def test_the_rate_cap_is_counted_at_the_claim(world, monkeypatch):
    app, tg, _ = world
    _messenger("telegram")
    monkeypatch.setattr(store, "SEND_RATE_MAX", 1)
    cid1, card1, _ = _propose(app, monkeypatch, "one")
    _decide(app, card1["action_id"], {"decision": "send"})
    assert _settled(app, cid1)["state"] == "sent"
    cid2, card2, _ = _propose(app, monkeypatch, "two")
    r = _decide(app, card2["action_id"], {"decision": "send"})
    assert (
        r.json()["proposal"]["state"] == "failed" and r.json()["proposal"]["code"] == "rate_limited"
    )
    assert len(tg.sent) == 1


def test_a_send_switched_off_is_refused_at_the_token_fetch(world):
    _app, tg, _ = world
    inst = _messenger("telegram")
    store.set_send(inst["id"], False)
    bound = ask_messages._bound(store.read()["instances"][0])
    out = run(connector_http._send(bound, "42", "hi", "k" * 32))
    assert out["outcome"] == "failed" and out["code"] == "send_disabled"
    assert tg.sent == []


def test_dismiss_sends_nothing(world, monkeypatch):
    app, tg, _ = world
    _messenger("telegram")
    _cid, card, _ = _propose(app, monkeypatch, "hello")
    r = _decide(app, card["action_id"], {"decision": "dismiss"})
    assert r.json()["proposal"]["state"] == "dismissed" and tg.sent == []


def test_a_message_card_not_read_out_says_to_use_the_screen():
    """A message card voice can't confirm (over `voice_text.verbatim`'s limit) still gets a line on
    a call, pointing at the screen (Hermes 7610)."""
    from agent_sessions import voice_text

    out = voice_text.card_sentences(
        {"proposals": [{"action_id": "a" * 32, "kind": "message", "recipient": "Ops"}]}
    )
    assert out == [
        "I've drafted a message to Ops. It can't be read out word for word; "
        "tap it on your screen to send it."
    ]
