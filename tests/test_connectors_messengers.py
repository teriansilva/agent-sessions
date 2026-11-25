"""HQ connectors, P2a first half (#1832): Telegram and Matrix — recipients and the send switch.
Nothing is sent in this phase.

Against fake Telegram and Matrix servers that record every request:

* the bundled send-only manifests; the transport writes Telegram's token into the path only, after
  vetting, and it never reaches an error, a log, a response or the store;
* Telegram discovery reads chat METADATA only, sends no ``offset``, and a webhook bot is reported;
* Matrix eligibility: an encrypted room, or one whose encryption could not be checked, is
  unavailable; only a 404 for that state means unencrypted;
* the allowlist is set only from a server snapshot at the current generation, eligible entries
  only, labels the server's;
* the send switch: on needs a test and bound recipients; a different bot or origin switches it OFF
  in the same write with the reason kept, and it never comes back by itself; a rotation of the same
  bot keeps it;
* an older build still reads the store after unrelated writes, and after messenger connectors were
  removed — audit trail included.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import connector_http
from agent_sessions.connectors import candidates, hq, kinds, manifest, service, store
from agent_sessions.main import create_app
from test_ask_orchestrator import App

TG_TOKEN = "123456789:AAH_sentinel-bot-token-abcdefghijk"
TG_TOKEN2 = "123456789:AAH_rotated-bot-token-zyxwvutsrqp"
OTHER_BOT_TOKEN = "987654321:AAH_another-bot-token-qwertyuiop"
MX_TOKEN = "syt_c2VudGluZWw_matrix-access-token-0123456789"
MX_ORIGIN = "https://matrix.example.org"
SENTINEL = "SECRET-MESSAGE-BODY-do-not-copy-7f3a"
TG_IP, MX_IP = "149.154.167.220", "10.0.0.7"


def run(coro):
    return asyncio.run(coro)


class Telegram:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bots = {TG_TOKEN: "hq_bot", TG_TOKEN2: "hq_bot", OTHER_BOT_TOKEN: "other_bot"}
        self.webhook = False
        self.updates = [
            {"update_id": 1, "message": {"message_id": 1, "text": SENTINEL, "caption": SENTINEL,
             "from": {"id": 42, "first_name": "Marcus", "username": "marcus"},
             "chat": {"id": 42, "type": "private", "first_name": "Marcus", "username": "marcus"}}},
            {"update_id": 2, "message": {"message_id": 2, "text": SENTINEL,
             "chat": {"id": -1009, "type": "supergroup", "title": "Release crew"}}},
            {"update_id": 3, "edited_message": {"message_id": 1, "text": SENTINEL,
             "chat": {"id": 42, "type": "private", "first_name": "Marcus", "username": "marcus"}}},
        ]  # fmt: skip

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        token, _, method = path.removeprefix("/bot").partition("/")
        if token not in self.bots:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if method == "getMe":
            return httpx.Response(
                200, json={"ok": True, "result": {"is_bot": True, "username": self.bots[token]}}
            )
        if method == "getUpdates":
            if self.webhook:
                return httpx.Response(409, json={"ok": False, "description": "Conflict"})
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        return httpx.Response(404, json={"ok": False})


class Matrix:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.user = "@hq:example.org"
        self.rooms = {
            "!ops:example.org": {"name": "Ops", "encrypted": False, "members": 4},
            "!sec:example.org": {"name": "Security", "encrypted": True, "members": 3},
        }
        self.encryption_status: dict[str, int] = {}  # room → forced status for its encryption read
        self.members_status: dict[str, int] = {}  # room → forced status for its member list

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {MX_TOKEN}":
            return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})
        path = request.url.raw_path.decode().split("?")[0]
        from urllib.parse import unquote

        path = unquote(path).removeprefix("/_matrix/client/v3/")
        if path == "account/whoami":
            return httpx.Response(200, json={"user_id": self.user})
        if path == "joined_rooms":
            return httpx.Response(200, json={"joined_rooms": list(self.rooms)})
        for rid, room in self.rooms.items():
            if path == f"rooms/{rid}/state/m.room.name":
                if room["name"] is None:
                    return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
                return httpx.Response(200, json={"name": room["name"]})
            if path == f"rooms/{rid}/state/m.room.encryption":
                if rid in self.encryption_status:
                    return httpx.Response(self.encryption_status[rid], json={})
                if room["encrypted"]:
                    return httpx.Response(200, json={"algorithm": "m.megolm.v1.aes-sha2"})
                return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
            if path == f"rooms/{rid}/joined_members":
                if rid in self.members_status:
                    return httpx.Response(self.members_status[rid], json={})
                return httpx.Response(
                    200, json={"joined": {f"@u{i}:example.org": {} for i in range(room["members"])}}
                )
        return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})


@pytest.fixture
def fakes(monkeypatch):
    tg, mx = Telegram(), Matrix()

    async def resolve(host, port):
        return {"api.telegram.org": [TG_IP], "matrix.example.org": [MX_IP]}[host]

    def route(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host")
        return (tg if host == "api.telegram.org" else mx).handler(request)

    monkeypatch.setattr(connector_http, "_resolve", resolve)
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(route))
    yield tg, mx
    for iid in list(candidates._SNAP):
        candidates.forget(iid)


def _telegram(token: str = TG_TOKEN) -> dict:
    inst = store.add("telegram")
    store.patch(inst["id"], {"token": token})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    return store.read()["instances"][-1]


def _matrix() -> dict:
    inst = store.add("matrix")
    store.patch(inst["id"], {"origin": MX_ORIGIN, "token": MX_TOKEN})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    return store.read()["instances"][-1]


def _view(iid: str) -> dict:
    return next(i for i in service.catalog()["instances"] if i["id"] == iid)


def _save(iid: str, ids: list[str]) -> dict:
    return store.set_recipients(iid, ids, candidates.snapshot(iid))


# ---- manifests and transport ---------------------------------------------------------------------


def test_the_messenger_manifests_are_send_only_and_reviewed():
    b = manifest.bundled()
    for mid, adapter in (("telegram", "telegram-bot"), ("matrix", "matrix-cs")):
        m = b[mid]
        assert m.adapter == adapter and m.category == "messenger"
        assert m.reads == () and m.sends == ("message",)
        assert m.public()["capabilities"] == {"read": [], "send": ["message"]}
    assert b["telegram"].fixed_origin == "https://api.telegram.org"
    assert b["forgejo"].sends == () and kinds.ADAPTER_SENDS.get("forgejo-api") is None


def test_telegram_tokens_ride_in_the_path_only_and_never_in_an_error(fakes, caplog):
    tg, _ = fakes
    caplog.set_level(logging.DEBUG)
    inst = _telegram()
    rq = tg.requests[0]
    assert rq.url.path == f"/bot{TG_TOKEN}/getMe" and "authorization" not in rq.headers
    assert rq.headers["host"] == "api.telegram.org" and rq.url.host == TG_IP
    # A failure carries no URL: the token must not ride on it.
    store.patch(inst["id"], {"token": "123456789:AAH_wrong-token-abcdefghijklmno"})
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "failed" and TG_TOKEN not in json.dumps(out)
    assert "wrong-token" not in json.dumps(out)
    assert TG_TOKEN not in caplog.text and TG_TOKEN not in json.dumps(service.catalog())
    assert TG_TOKEN.encode() not in store.store_path().read_bytes()


def test_a_token_that_could_change_the_path_is_refused_before_anything_is_sent(fakes):
    tg, _ = fakes
    inst = store.add("telegram")
    store.patch(inst["id"], {"token": "12345:AAH-x/../../admin?a=b#cdefghijkl"})
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "failed" and out["code"] == "invalid_token"
    assert tg.requests == []


def test_the_messenger_paths_are_the_only_ones_reachable(fakes):
    tg, _ = fakes
    inst = _telegram()
    b = candidates._bound(store.read()["instances"][0])
    for method, path in (
        ("POST", "/sendMessage"),
        ("GET", "/sendMessage"),
        ("GET", "/deleteWebhook"),
    ):
        with pytest.raises(connector_http.ConnectorError):
            run(connector_http._request(b, method, path))
    assert all(r.url.path.endswith(("/getMe", "/getUpdates")) for r in tg.requests)
    assert inst["id"]


# ---- discovery -----------------------------------------------------------------------------------


def test_telegram_discovery_takes_chat_metadata_only_and_acknowledges_nothing(fakes, caplog):
    tg, _ = fakes
    caplog.set_level(logging.DEBUG)
    inst = _telegram()
    out = run(candidates.list_candidates(inst["id"]))
    assert out["candidates"] == [
        {"id": "42", "label": "Marcus (@marcus)", "kind": "private chat", "allowed": False},
        {"id": "-1009", "label": "Release crew", "kind": "supergroup", "allowed": False},
    ]
    q = [r.url.params for r in tg.requests if r.url.path.endswith("/getUpdates")][0]
    assert q.get("limit") == "100" and "offset" not in q
    blob = json.dumps(out) + json.dumps(candidates.snapshot(inst["id"])) + caplog.text
    assert SENTINEL not in blob
    assert SENTINEL.encode() not in store.store_path().read_bytes()


def test_a_webhook_bot_is_reported_in_words(fakes):
    tg, _ = fakes
    inst = _telegram()
    tg.webhook = True
    with pytest.raises(connector_http.ConnectorError) as e:
        run(candidates.list_candidates(inst["id"]))
    assert e.value.code == "webhook_active" and "separate bot" in e.value.detail


@pytest.mark.parametrize("status", [403, 500])
def test_a_matrix_room_whose_encryption_cannot_be_checked_is_unavailable(fakes, status):
    _, mx = fakes
    inst = _matrix()
    mx.encryption_status["!ops:example.org"] = status
    out = {c["id"]: c for c in run(candidates.list_candidates(inst["id"]))["candidates"]}
    assert "couldn't be checked" in out["!ops:example.org"]["unavailable"]
    assert "encrypted" in out["!sec:example.org"]["unavailable"]


def test_matrix_lists_rooms_with_members_and_only_a_404_means_unencrypted(fakes):
    inst = _matrix()
    out = {c["id"]: c for c in run(candidates.list_candidates(inst["id"]))["candidates"]}
    assert out["!ops:example.org"] == {
        "id": "!ops:example.org", "label": "Ops", "kind": "room", "members": 4, "allowed": False,
    }  # fmt: skip
    assert out["!sec:example.org"]["unavailable"].startswith("encrypted")


def test_a_matrix_encryption_lookup_that_times_out_is_unavailable(fakes, monkeypatch):
    _, mx = fakes
    inst = _matrix()
    real = mx.handler

    def slow(request):
        if (
            "m.room.encryption" in request.url.raw_path.decode()
            and "ops" in request.url.raw_path.decode()
        ):
            raise httpx.ReadTimeout("timed out", request=request)
        return real(request)

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(slow))
    out = {c["id"]: c for c in run(candidates.list_candidates(inst["id"]))["candidates"]}
    assert "couldn't be checked" in out["!ops:example.org"]["unavailable"]


@pytest.mark.parametrize(
    "reply",
    ["403", "timeout", {"joined": ["@a:example.org"]}, {"members": {}}, ["@a:example.org"]],
    ids=["forbidden", "timeout", "joined-not-a-map", "no-joined", "not-an-object"],
)
def test_a_matrix_room_whose_members_cannot_be_counted_is_unavailable_and_cannot_be_saved(
    fakes, monkeypatch, reply
):
    """Hermes 7605: who would read a message is part of the consent, so a room whose member list
    fails, times out or comes back in the wrong shape is not eligible, and a save of it is
    refused."""
    _, mx = fakes
    inst = _matrix()
    real = mx.handler

    def members(request):
        path = request.url.raw_path.decode()
        if "joined_members" in path and "ops" in path:
            if reply == "403":
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
            if reply == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json=reply)
        return real(request)

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(members))
    out = {c["id"]: c for c in run(candidates.list_candidates(inst["id"]))["candidates"]}
    assert out["!ops:example.org"]["unavailable"] == "its members couldn't be checked"
    assert "members" not in out["!ops:example.org"]
    with pytest.raises(store.Refused) as e:
        _save(inst["id"], ["!ops:example.org"])
    assert e.value.code == "unknown_recipient"
    assert store.read()["instances"][0]["recipients"] == []


# ---- the allowlist -------------------------------------------------------------------------------


def test_recipients_come_only_from_the_snapshot_and_labels_are_the_servers(fakes):
    inst = _telegram()
    with pytest.raises(store.Refused) as e:
        _save(inst["id"], ["42"])  # nothing listed yet
    assert e.value.code == "stale_candidates"
    run(candidates.list_candidates(inst["id"]))
    with pytest.raises(store.Refused) as e:
        _save(inst["id"], ["42", "777"])
    assert e.value.code == "unknown_recipient"
    assert store.read()["instances"][0]["recipients"] == []  # all or nothing
    saved = _save(inst["id"], ["42"])
    assert saved["recipients"] == [
        {"id": "42", "label": "Marcus (@marcus)", "kind": "private chat"}
    ]
    assert saved["bound"] == {"origin": "https://api.telegram.org", "identity": "hq_bot"}


def test_an_ineligible_room_cannot_be_saved_or_kept(fakes):
    _, mx = fakes
    inst = _matrix()
    run(candidates.list_candidates(inst["id"]))
    with pytest.raises(store.Refused) as e:
        _save(inst["id"], ["!sec:example.org"])
    assert e.value.code == "unknown_recipient"
    _save(inst["id"], ["!ops:example.org"])
    # Encrypted after the save: listed again as ineligible, it can't be kept.
    mx.rooms["!ops:example.org"]["encrypted"] = True
    out = {c["id"]: c for c in run(candidates.list_candidates(inst["id"]))["candidates"]}
    assert out["!ops:example.org"]["allowed"] and out["!ops:example.org"]["unavailable"]
    with pytest.raises(store.Refused):
        _save(inst["id"], ["!ops:example.org"])


def test_a_generation_that_moved_between_list_and_save_is_stale(fakes):
    inst = _telegram()
    run(candidates.list_candidates(inst["id"]))
    store.patch(inst["id"], {"token": TG_TOKEN2})  # a new generation
    run(service.run_test(inst["id"]))
    with pytest.raises(store.Refused) as e:
        _save(inst["id"], ["42"])
    assert e.value.code == "stale_candidates"


# ---- the send switch and its binding -------------------------------------------------------------


def test_send_needs_a_test_and_bound_recipients(fakes):
    inst = _telegram()
    with pytest.raises(store.Refused) as e:
        store.set_send(inst["id"], True)
    assert e.value.code == "needs_recipients"
    run(candidates.list_candidates(inst["id"]))
    _save(inst["id"], ["42"])
    store.set_send(inst["id"], True)
    view = _view(inst["id"])
    assert view["send"] is True and view["recipients_bound"] is True
    assert store.sendable(store.read()["instances"][0])
    store.set_send(inst["id"], False)
    assert _view(inst["id"])["send"] is False


def test_a_rotated_token_of_the_same_bot_keeps_the_consent(fakes):
    inst = _telegram()
    run(candidates.list_candidates(inst["id"]))
    _save(inst["id"], ["42"])
    store.set_send(inst["id"], True)
    store.patch(inst["id"], {"token": TG_TOKEN2})
    assert _view(inst["id"])["send"] is False  # untested now
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    view = _view(inst["id"])
    assert view["send"] is True and view["send_off"] is None and view["recipients_bound"]


def test_a_different_bot_switches_send_off_and_never_back_by_itself(fakes):
    inst = _telegram()
    run(candidates.list_candidates(inst["id"]))
    _save(inst["id"], ["42"])
    store.set_send(inst["id"], True)
    store.patch(inst["id"], {"token": OTHER_BOT_TOKEN})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    view = _view(inst["id"])
    assert view["send"] is False and view["recipients_bound"] is False
    assert view["send_off"]["reason"] == "identity_changed"
    assert (view["send_off"]["was"], view["send_off"]["now"]) == ("hq_bot", "other_bot")
    assert view["recipients"] == [{"id": "42", "label": "Marcus (@marcus)", "kind": "private chat"}]
    assert view["recipients_chosen_for"] == "hq_bot"
    stored = store.read()["instances"][0]
    assert stored["enabled"]["send"] is False  # off IN THE STORE, not just in the view
    assert store.read()["audit"][-1]["op"] in ("disabled", "tested")
    # A second passing test does not bring it back; switching on is refused until a re-save.
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    assert _view(inst["id"])["send"] is False
    with pytest.raises(store.Refused) as e:
        store.set_send(inst["id"], True)
    assert e.value.code == "recipients_unbound"
    run(candidates.list_candidates(inst["id"]))
    _save(inst["id"], ["42"])
    store.set_send(inst["id"], True)
    view = _view(inst["id"])
    assert view["send"] is True and view["send_off"] is None
    assert view["recipients_chosen_for"] == "other_bot"


def test_a_homeserver_move_is_a_new_consent(fakes, monkeypatch):
    inst = _matrix()
    run(candidates.list_candidates(inst["id"]))
    _save(inst["id"], ["!ops:example.org"])
    store.set_send(inst["id"], True)

    async def resolve(host, port):
        return {"matrix.example.org": [MX_IP], "chat.example.net": [MX_IP]}[host]

    monkeypatch.setattr(connector_http, "_resolve", resolve)
    store.patch(inst["id"], {"origin": "https://chat.example.net", "token": MX_TOKEN})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    view = _view(inst["id"])
    assert view["send"] is False and view["send_off"]["reason"] == "origin_changed"


def test_a_replacement_bot_cannot_keep_a_chat_it_cannot_see(fakes):
    """Hermes 7603 (1): a saved recipient is kept without being listed only by the SAME bot. A
    different bot re-binds only what it lists itself, on the server and on the page."""
    tg, _ = fakes
    inst = _telegram()
    run(candidates.list_candidates(inst["id"]))
    _save(inst["id"], ["42"])
    store.set_send(inst["id"], True)
    # The same bot, the chat gone quiet: an unlisted saved chat is still kept.
    tg.updates = []
    assert run(candidates.list_candidates(inst["id"]))["candidates"] == []
    assert [r["id"] for r in _save(inst["id"], ["42"])["recipients"]] == ["42"]
    # Another bot that has never seen chat 42 cannot take it over.
    store.patch(inst["id"], {"token": OTHER_BOT_TOKEN})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    assert run(candidates.list_candidates(inst["id"]))["candidates"] == []
    with pytest.raises(store.Refused) as e:
        _save(inst["id"], ["42"])
    assert e.value.code == "unknown_recipient"
    view = _view(inst["id"])
    assert view["send_off"]["reason"] == "identity_changed" and view["recipients_bound"] is False
    with pytest.raises(store.Refused) as e:
        store.set_send(inst["id"], True)
    assert e.value.code == "recipients_unbound"


def test_a_moved_homeserver_cannot_keep_a_room_it_does_not_list(fakes, monkeypatch):
    _, mx = fakes
    inst = _matrix()
    run(candidates.list_candidates(inst["id"]))
    _save(inst["id"], ["!ops:example.org"])

    async def resolve(host, port):
        return {"matrix.example.org": [MX_IP], "chat.example.net": [MX_IP]}[host]

    monkeypatch.setattr(connector_http, "_resolve", resolve)
    store.patch(inst["id"], {"origin": "https://chat.example.net", "token": MX_TOKEN})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    mx.rooms = {}
    run(candidates.list_candidates(inst["id"]))
    with pytest.raises(store.Refused) as e:
        _save(inst["id"], ["!ops:example.org"])
    assert e.value.code == "unknown_recipient"
    assert _view(inst["id"])["send_off"]["reason"] == "origin_changed"


def test_long_names_and_unnamed_rooms_fit_the_store_and_read_back(fakes):
    """Hermes 7603 (2): every candidate is made to fit the stored schema before it is offered, so
    a save can never leave a store the strict read refuses."""
    tg, mx = fakes
    first, last = "F" * 60, "L" * 60
    tg.updates = [{"update_id": 9, "message": {"message_id": 9, "text": SENTINEL, "chat": {
        "id": 77, "type": "private", "first_name": first, "last_name": last,
    }}}]  # fmt: skip
    t = _telegram()
    (c,) = run(candidates.list_candidates(t["id"]))["candidates"]
    # Two capped 60-character parts make 121: cut to fit, visibly.
    assert c["label"] == f"{first} {last}"[: store.LABEL_MAX - 1] + "…"
    _save(t["id"], ["77"])
    assert store.read()["instances"][0]["recipients"][0]["label"] == c["label"]  # strict read

    long_ok = "!" + "a" * 150 + ":example.org"  # fits the store's id; no name → the id, cut
    too_long = "!" + "a" * 200 + ":" + "b" * 100  # the adapter accepts it, the store can't hold it
    mx.rooms = {
        long_ok: {"name": None, "encrypted": False, "members": 2},
        too_long: {"name": None, "encrypted": False, "members": 2},
        "!ops:example.org": {"name": "Ops", "encrypted": False, "members": 4},
    }
    m = _matrix()
    listed = run(candidates.list_candidates(m["id"]))["candidates"]
    assert [x["id"] for x in listed] == [long_ok, "!ops:example.org"]
    assert listed[0]["label"] == long_ok[: store.LABEL_MAX - 1] + "…"
    _save(m["id"], [long_ok, "!ops:example.org"])
    stored = store.read()["instances"][1]["recipients"]  # strict read
    assert [r["id"] for r in stored] == [long_ok, "!ops:example.org"]
    with pytest.raises(store.Refused):
        store.set_recipients(m["id"], [too_long], candidates.snapshot(m["id"]))


def test_a_write_the_strict_read_would_refuse_is_refused_and_the_store_stays_readable(fakes):
    inst = _telegram()
    snap = {
        "generation": store.read()["instances"][0]["generation"],
        "candidates": {"42": {"label": "x" * (store.LABEL_MAX + 1), "kind": "private chat",
                              "eligible": True}},
    }  # fmt: skip
    with pytest.raises(store.Refused) as e:
        store.set_recipients(inst["id"], ["42"], snap)
    assert e.value.code == "invalid_record"
    assert store.read()["instances"][0]["recipients"] == []


def test_read_cannot_be_switched_on_for_a_send_only_connector(fakes, auth_cfg):
    """Hermes 7603 (3): the manifest's reads are enforced at the store, and HQ's listing never
    meets a send-only connector, even one an older store marked readable."""
    inst = _telegram()
    with pytest.raises(store.Refused) as e:
        store.set_capability(inst["id"], True)
    assert e.value.code == "no_reads"
    app = App(auth_cfg)
    r = app.c.post(
        f"/api/connectors/instances/{inst['id']}/capabilities", json={"read": True}, headers=app.hdr
    )
    assert r.status_code == 422 and r.json()["code"] == "no_reads"
    assert store.read()["instances"][0]["enabled"]["read"] is False
    # A store that says otherwise (hand-edited, or an earlier build) is still not read through.
    doc = store.read()
    doc["instances"][0]["enabled"]["read"] = True
    store._write(store.store_path(), doc)
    assert store.readable(store.read()["instances"][0]) is False
    listing = run(hq.list_connectors(hq.TurnConnectors()))[0]
    assert listing == {"connectors": []}


# ---- rollback: an older build still reads the store ----------------------------------------------


def _previous_build_accepts(doc: dict) -> bool:
    """The rules of the build before #1832 that this change could break, copied: an instance has
    the base keys (plus `account` on a basic connector), `enabled` is exactly `{read}`, and every
    audit row has exactly five keys and one of the old ops."""
    base = set(store._INSTANCE_KEYS)
    ops = {"added", "changed", "tested", "enabled", "disabled", "removed"}
    for inst in doc["instances"]:
        if set(inst) not in (base, base | {"account"}):
            return False
        if set(inst["enabled"]) != {"read"}:
            return False
    return all(
        set(r) == {"at", "op", "id", "manifest_id", "generation"} and r["op"] in ops
        for r in doc["audit"]
    )


def test_an_older_build_reads_the_store_after_unrelated_writes_and_after_removal(fakes):
    forge = store.add("forgejo")
    store.patch(forge["id"], {"origin": "https://git.example", "token": "fjtok-0123456789abcdef"})
    assert _previous_build_accepts(store.read())  # read-only records never gain `send`
    inst = _telegram()
    run(candidates.list_candidates(inst["id"]))
    _save(inst["id"], ["42"])
    store.set_send(inst["id"], True)
    store.patch(inst["id"], {"token": OTHER_BOT_TOKEN})  # an identity change …
    run(service.run_test(inst["id"]))
    assert not _previous_build_accepts(store.read())  # a messenger is present: refused, closed
    store.remove(inst["id"])
    doc = json.loads(store.store_path().read_text())
    assert _previous_build_accepts(doc)  # … and the whole store, audit trail included, reads again


# ---- the routes ----------------------------------------------------------------------------------


def test_the_recipient_routes_need_login_csrf_and_exact_bodies(fakes, auth_cfg):
    anon = TestClient(create_app(auth_cfg), base_url="https://testserver")
    inst = _telegram()
    base = f"/api/connectors/instances/{inst['id']}"
    assert anon.get(f"{base}/recipients/candidates").status_code == 401
    app = App(auth_cfg)
    c, hdr = app.c, app.hdr
    listed = c.get(f"{base}/recipients/candidates")
    assert listed.status_code == 200 and listed.json()["candidates"][0]["id"] == "42"
    assert c.put(f"{base}/recipients", json={"ids": ["42"]}).status_code == 403  # no CSRF
    # A client can't supply a label (or anything else): only ids.
    r = c.put(f"{base}/recipients", json={"ids": ["42"], "labels": {"42": "Boss"}}, headers=hdr)
    assert r.status_code == 422
    r = c.post(f"{base}/capabilities", json={"send": True}, headers=hdr)
    assert r.status_code == 422 and r.json()["code"] == "needs_recipients"
    r = c.put(f"{base}/recipients", json={"ids": ["42"]}, headers=hdr)
    assert r.status_code == 200 and r.json()["recipients"][0]["label"] == "Marcus (@marcus)"
    r = c.post(f"{base}/capabilities", json={"send": True}, headers=hdr)
    assert r.status_code == 200 and r.json()["send"] is True
    assert c.post(f"{base}/capabilities", json={"send": 1}, headers=hdr).status_code == 422
    assert (
        c.post(f"{base}/capabilities", json={"read": True, "send": True}, headers=hdr).status_code
        == 422
    )
    assert TG_TOKEN not in listed.text + r.text
