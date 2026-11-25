"""Notify routes (#1841, #1746 P3b): a copy of each bell event to the Commander's own Telegram chat,
Matrix DM or mail address, edited to "✓ resolved" when the event is handled.

Against the REAL route ledger, the real notification store and its real producers, and fake
providers that record every request (an `httpx.MockTransport` for Telegram and Matrix; the mail
transport replaced at its one entry point). The cases that are about PROCESSES — two workers on
one ledger, a kill between claim and send — use real child processes and SIGKILL, with the
provider's requests appended to a shared file so every process is counted.

The issue's acceptance cases are numbered as in #1841 (1–11, 10b); the rest follow.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    connector_http,
    connector_smtp,
    hq_dispatch,
    missions,
    notifications,
    notify_routes,
    template_secrets,
)
from agent_sessions.connectors import candidates, service, store
from agent_sessions.connectors.adapters import matrix
from test_hq_dispatch import MID, SID, announce_automation, announce_escalation, announce_needs_you

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://battlelab.example"
TG_TOKEN = "123456789:AAH_sentinel-bot-token-abcdefghijk"
OTHER_BOT_TOKEN = "987654321:AAH_another-bot-token-qwertyuiop"
MX_TOKEN = "syt_c2VudGluZWw_matrix-access-token-0123456789"
MX_ORIGIN = "https://matrix.example.org"
BOT, ME, EVE = "@hq:example.org", "@marcus:example.org", "@eve:example.org"
DM = "!dm:example.org"
SID2 = "claude:22222222-2222-4222-8222-222222222222"


def run(coro):
    return asyncio.run(coro)


class Providers:
    """Telegram and Matrix as far as a route uses them. Every request is kept (``requests``) and,
    when ``log`` is set, appended to that file as one JSON line (so child processes count too)."""

    def __init__(self, log: Path | None = None) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.log = log
        self.bots = {TG_TOKEN: "hq_bot", OTHER_BOT_TOKEN: "other_bot"}
        self.send_status = 200
        self.edit_status: list[int] = []  # one status per edit, then 200
        self.edit_not_modified = False
        self.members: list[str] | None = [BOT, ME]
        self.members_status = 200
        self.encrypted = False
        self._mid = 100
        self._events: dict[str, str] = {}

    def _keep(self, kind: str, path: str, body: dict) -> None:
        self.requests.append((kind, path, body))
        if self.log is not None:
            with self.log.open("a") as fh:
                fh.write(json.dumps([kind, path, body]) + "\n")

    def sends(self) -> list[dict]:
        return [b for k, _p, b in self.requests if k == "send"]

    def edits(self) -> list[dict]:
        return [b for k, _p, b in self.requests if k == "edit"]

    def telegram(self, request: httpx.Request) -> httpx.Response:
        token, _, method = request.url.path.removeprefix("/bot").partition("/")
        if token not in self.bots:
            return httpx.Response(401, json={"ok": False})
        if method == "getMe":
            return httpx.Response(
                200, json={"ok": True, "result": {"is_bot": True, "username": self.bots[token]}}
            )
        if method == "getUpdates":
            chat = {"id": 42, "type": "private", "first_name": "Marcus", "username": "marcus_b"}
            group = {"id": -1009, "type": "supergroup", "title": "Release crew"}
            return httpx.Response(
                200,
                json={"ok": True, "result": [
                    {"update_id": 1, "message": {"message_id": 1, "chat": chat}},
                    {"update_id": 2, "message": {"message_id": 2, "chat": group}},
                ]},
            )  # fmt: skip
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            self._keep("send", method, body)
            if self.send_status != 200:
                return httpx.Response(self.send_status, json={"ok": False})
            self._mid += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self._mid}})
        if method == "editMessageText":
            self._keep("edit", method, body)
            status = self.edit_status.pop(0) if self.edit_status else 200
            if self.edit_not_modified:
                return httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "description": "Bad Request: message is not modified: specified new "
                        "message content and reply markup are exactly the same",
                    },
                )
            if status != 200:
                return httpx.Response(status, json={"ok": False})
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": body["message_id"]}}
            )
        return httpx.Response(404, json={"ok": False})

    def matrix(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {MX_TOKEN}":
            return httpx.Response(401, json={})
        path = unquote(request.url.raw_path.decode().split("?")[0]).removeprefix(
            "/_matrix/client/v3/"
        )
        if path == "account/whoami":
            return httpx.Response(200, json={"user_id": BOT})
        if path == "joined_rooms":
            return httpx.Response(200, json={"joined_rooms": [DM]})
        if path == f"rooms/{DM}/state/m.room.name":
            return httpx.Response(200, json={"name": "Marcus"})
        if path == f"rooms/{DM}/state/m.room.encryption":
            if self.encrypted:
                return httpx.Response(200, json={"algorithm": "m.megolm.v1.aes-sha2"})
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
        if path == f"rooms/{DM}/joined_members":
            if self.members_status != 200:
                return httpx.Response(self.members_status, json={})
            if self.members is None:
                return httpx.Response(200, json={"joined": ["not", "a", "map"]})
            return httpx.Response(200, json={"joined": {u: {} for u in self.members}})
        if path.startswith(f"rooms/{DM}/send/m.room.message/"):
            body = json.loads(request.content or b"{}")
            txn = path.rsplit("/", 1)[1]
            edit = body.get("m.relates_to", {}).get("rel_type") == "m.replace"
            self._keep("edit" if edit else "send", txn, body)
            if not edit and self.send_status != 200:
                return httpx.Response(self.send_status, json={})
            status = self.edit_status.pop(0) if edit and self.edit_status else 200
            if status != 200:
                return httpx.Response(status, json={})
            eid = self._events.setdefault(txn, f"$ev{len(self._events)}")
            return httpx.Response(200, json={"event_id": eid})
        return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})

    def route(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("host") == "api.telegram.org":
            return self.telegram(request)
        return self.matrix(request)


def _install(monkeypatch, p: Providers) -> None:
    async def resolve(host, port):
        return {"api.telegram.org": ["149.154.167.220"], "matrix.example.org": ["10.0.0.7"]}[host]

    monkeypatch.setattr(connector_http, "_resolve", resolve)
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(p.route))


@pytest.fixture
def prov(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", ORIGIN)
    p = Providers()
    _install(monkeypatch, p)
    yield p
    for iid in list(candidates._SNAP):
        candidates.forget(iid)


def _settings(dest: str, **kw) -> dict:
    return store.check_notify_body(
        {
            "enabled": True,
            "destination_id": dest,
            "classes": list(store.NOTIFY_CLASSES),
            "window_s": 0,
            "quiet": None,
            "cap_per_hour": 20,
            "include_line": False,
            **kw,
        }
    )


def enable(iid: str, dest: str, **kw) -> dict:
    inst = next(i for i in store.read()["instances"] if i["id"] == iid)
    s = _settings(dest, **kw)
    ident = run(service.notify_identity(inst, dest)) if s["enabled"] else None
    return store.set_notify(iid, s, generation=inst["generation"], identity=ident)


def tg_route(**kw) -> str:
    inst = store.add("telegram")
    store.patch(inst["id"], {"token": TG_TOKEN})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    run(candidates.list_candidates(inst["id"]))
    store.set_recipients(inst["id"], ["42", "-1009"], candidates.snapshot(inst["id"]))
    enable(inst["id"], "42", **kw)
    return inst["id"]


def mx_route(prov: Providers | None = None, change: dict | None = None, **kw) -> str:
    """A Matrix route to the DM; ``change`` is applied to the room after it was saved as a
    recipient and before notify is switched on."""
    inst = store.add("matrix")
    store.patch(inst["id"], {"origin": MX_ORIGIN, "token": MX_TOKEN})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    run(candidates.list_candidates(inst["id"]))
    store.set_recipients(inst["id"], [DM], candidates.snapshot(inst["id"]))
    for k, v in (change or {}).items():
        setattr(prov, k, v)
    enable(inst["id"], DM, **kw)
    return inst["id"]


def pass_(dt: float = 1.0) -> dict:
    return run(notify_routes.pass_once(now=time.time() + dt))


def ledger() -> dict:
    return notify_routes.read_strict()


def deliveries() -> list[dict]:
    return list(ledger()["deliveries"].values())


def only() -> dict:
    (d,) = deliveries()
    return d


def announce(monkeypatch, *sids: str) -> None:
    """Several sessions need you at once: one sync opens an episode (and a bell row) for each."""
    from agent_sessions import needs_you_notify
    from agent_sessions.routes import pulse as pulse_routes

    rows = [{"id": s, "title": "t", "project": {"name": "api"}, "engine": "claude",
             "kind": "question", "action": {}} for s in sids]  # fmt: skip
    monkeypatch.setattr(
        pulse_routes,
        "build_needs_you",
        lambda *_a, **_k: {"rows": rows, "needs_you_ids": list(sids)},
    )
    needs_you_notify.sync_once()


def close_needs_you(monkeypatch) -> None:
    """The session no longer needs you: its episode closes (and its tag joins the closed ledger)."""
    from agent_sessions import needs_you_notify
    from agent_sessions.routes import pulse as pulse_routes

    monkeypatch.setattr(
        pulse_routes, "build_needs_you", lambda *_a, **_k: {"rows": [], "needs_you_ids": []}
    )
    needs_you_notify.sync_once()


def needs_you_tag() -> str:
    return next(
        r["dispatch"]
        for r in notifications._read(notifications._notifications_path())
        if str(r.get("dispatch", "")).startswith("needs-you:")
    )


# ---- 1. never announces what the bell didn't ----------------------------------------------------


def test_1_a_route_sends_only_bell_rows_after_its_since(prov, monkeypatch):
    announce_automation(rid="before")  # a bell row from before the route existed
    iid = tg_route()
    assert pass_() == {"claimed": 0, "edits": 0}
    assert prov.sends() == []
    # A row with no valid tag (a partial-send alert carries none) is never a member either.
    bad = {"action_id": "automation:abc:partial", "dispatch_tag": "automation:abc"}
    notifications.add(title="t", project="p", session_id="", engine="", reason="r", **bad)
    announce_automation(rid="r2")
    pass_()
    (sent,) = prov.sends()
    assert sent["chat_id"] == 42 and sent["link_preview_options"] == {"is_disabled": True}
    assert (
        sent["text"]
        == f"BattleLab · 1 event\n• Automation failed — {ORIGIN}/mission/automations/abc"
    )
    assert only()["state"] == "delivered_final"  # an automation failure never resolves
    assert iid in store.read()["instances"][0]["id"]


def test_1_a_tag_still_pushing_waits_for_the_next_pass(prov, monkeypatch):
    tg_route()
    announce_automation(rid="r3")
    tag = "automation:abc:r3"
    hq_dispatch.begin_flight([{"dispatch": tag}])
    try:
        pass_()
        assert prov.sends() == [] and ledger()["member_of"] == {}
    finally:
        hq_dispatch.end_flight([tag])
    pass_()
    assert len(prov.sends()) == 1


# ---- 2. closed while HQ's line is generating / while the provider call is in flight --------------


def test_2_closed_while_hqs_line_is_generating_sends_nothing(prov, monkeypatch):
    tg_route(include_line=True)
    announce_needs_you(monkeypatch)
    # HQ's line for it is still being written: its bundle is claimed, not settled.
    tag = needs_you_tag()
    d = hq_dispatch._read_strict()
    bid = d["member_of"][tag]
    d["claims"][bid] = {"state": "attempt", "at": time.time(), "code": ""}
    hq_dispatch._write(d)
    assert pass_()["waiting"] == 1 and prov.sends() == []
    close_needs_you(monkeypatch)
    pass_(notify_routes.LINE_WAIT_S + 5)
    assert prov.sends() == [] and only()["state"] == "skipped_closed"


def test_2_closed_while_the_provider_call_is_in_flight_is_edited_by_reconcile(prov, monkeypatch):
    tg_route()
    announce_needs_you(monkeypatch)

    async def close_now():
        close_needs_you(monkeypatch)

    monkeypatch.setattr(notify_routes, "_HOOK_BEFORE_SEND", close_now)
    pass_()
    monkeypatch.setattr(notify_routes, "_HOOK_BEFORE_SEND", None)
    assert only()["state"] == "delivered" and len(prov.sends()) == 1
    pass_()
    (edit,) = prov.edits()
    assert edit["message_id"] == 101 and edit["text"].startswith("BattleLab · ✓ resolved\n• ✓ ")
    assert only()["state"] == "retracted"
    pass_()
    assert len(prov.edits()) == 1  # nothing left to mark


def test_2_an_outcome_that_cant_be_known_is_unconfirmed_and_never_sent_again(prov, monkeypatch):
    tg_route()
    prov.send_status = 502
    announce_automation(rid="r4")
    pass_()
    assert only()["state"] == "unconfirmed" and len(prov.sends()) == 1
    prov.send_status = 200
    pass_(), pass_(30)
    assert len(prov.sends()) == 1


# ---- 3. an unreadable notification store before the send --------------------------------------


def _bell_unreadable_once(monkeypatch):
    """The bell reads for the join, then is unreadable for the send's strict re-read."""
    real = notify_routes._bell
    calls = {"n": 0}

    def bell():
        calls["n"] += 1
        return real() if calls["n"] == 1 else None

    monkeypatch.setattr(notify_routes, "_bell", bell)
    return lambda: monkeypatch.setattr(notify_routes, "_bell", real)


@pytest.mark.parametrize("then", ["open", "closed", "late"])
def test_3_an_unreadable_store_before_the_send_holds_it_and_a_later_pass_decides(
    then, prov, monkeypatch
):
    tg_route()
    announce_needs_you(monkeypatch)
    restore = _bell_unreadable_once(monkeypatch)
    pass_()
    restore()
    assert prov.sends() == [] and only()["state"] == "held_unknown"
    if then == "open":
        pass_()
        assert len(prov.sends()) == 1 and only()["state"] == "delivered"
    elif then == "closed":
        close_needs_you(monkeypatch)
        pass_()
        assert prov.sends() == [] and only()["state"] == "skipped_closed"
    else:
        pass_(notify_routes.DELAY_CAP_S + 5)
        assert prov.sends() == [] and only()["state"] == "expired"


def test_3_an_unreadable_route_ledger_sends_nothing_and_is_never_rewritten(prov, monkeypatch):
    tg_route()
    p = notify_routes._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    announce_automation(rid="r5")
    assert pass_() == {"skipped": "store unreadable"}
    assert prov.sends() == [] and p.read_text() == "{not json"


# ---- 4. a delayed or restarted reconcile ---------------------------------------------------------


def test_4_a_late_reconcile_inside_30_days_still_edits_and_past_it_settles(prov, monkeypatch):
    tg_route()
    announce(monkeypatch, SID, SID2)
    pass_(), pass_()
    assert [d["state"] for d in deliveries()] == ["delivered", "delivered"]
    close_needs_you(monkeypatch)
    pass_(20 * 86400)  # the worker was down for 20 days
    assert len(prov.edits()) == 2 and {d["state"] for d in deliveries()} == {"retracted"}


def test_4_a_copy_past_the_window_settles_retract_window_passed(prov, monkeypatch):
    tg_route()
    announce_needs_you(monkeypatch)
    pass_()
    pass_(31 * 86400)
    assert only()["state"] == "retract_window_passed" and prov.edits() == []


# ---- 5. two processes on one ledger, and a kill between claim and send --------------------------

_CHILD = """
import asyncio, json, os, sys, time
import httpx
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["ROOT"], "tests"))
from agent_sessions import connector_http, notify_routes
from test_notify_routes import Providers
STOP = set(json.loads(sys.argv[1]))
def hook(name):
    if "gate:" + name in STOP:
        print("AT " + name, flush=True)
        while not os.path.exists(os.environ["GATE"]):
            time.sleep(0.02)
    elif name in STOP:
        print("AT " + name, flush=True)
        time.sleep(600)
notify_routes._POINT = hook
p = Providers(Path(os.environ["REQLOG"]))
async def resolve(host, port):
    return ["149.154.167.220"]
connector_http._resolve = resolve
connector_http._TRANSPORT = httpx.MockTransport(p.route)
print(json.dumps(asyncio.run(notify_routes.pass_once(now=float(sys.argv[2])))), flush=True)
"""


def _spawn(tmp_path: Path, stop: list[str], now: float) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        ROOT=str(ROOT),
        GATE=str(tmp_path / "gate"),
        REQLOG=str(tmp_path / "requests.jsonl"),
        PYTHONPATH=str(ROOT / "src"),
    )
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_CHILD), json.dumps(stop), str(now)],
        env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def _at(proc: subprocess.Popen, timeout: float = 60) -> str:
    import select

    deadline = time.monotonic() + timeout
    while True:
        ready, _, _ = select.select([proc.stdout], [], [], max(0.1, deadline - time.monotonic()))
        assert time.monotonic() < deadline, "the child never reached its point"
        if ready:
            line = proc.stdout.readline()
            if not line:
                raise AssertionError(proc.stderr.read()[-3000:])
            if line.startswith("AT "):
                return line[3:].strip()


def _sent_by_children(tmp_path: Path) -> list:
    p = tmp_path / "requests.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [r for r in rows if r[0] == "send"]


def test_5_two_worker_processes_on_one_ledger_deliver_once(prov, tmp_path):
    tg_route()
    announce_automation(rid="r6")
    now = time.time() + 1
    a = _spawn(tmp_path, ["gate:housekept"], now)
    b = _spawn(tmp_path, ["gate:housekept"], now)
    assert _at(a) == "housekept" and _at(b) == "housekept"
    (tmp_path / "gate").touch()
    a.communicate(timeout=60), b.communicate(timeout=60)
    assert a.returncode == 0 and b.returncode == 0
    assert len(_sent_by_children(tmp_path)) == 1
    assert only()["state"] == "delivered_final"


@pytest.mark.parametrize(("point", "state"), [("housekept", "failed"), ("armed", "unconfirmed")])
def test_5_a_kill_between_claim_and_send_never_delivers_twice(point, state, prov, tmp_path):
    tg_route()
    announce_automation(rid="r7")
    child = _spawn(tmp_path, [point], time.time() + 1)
    assert _at(child) == point
    child.send_signal(signal.SIGKILL)
    child.wait(30)
    pass_()  # this process finds the dead owner's claim
    d = only()
    assert d["state"] == state and d["code"] == "interrupted"
    pass_(30)
    assert prov.sends() == [] and _sent_by_children(tmp_path) == []


# ---- 6. edits act only on delivered or retract_failed copies ------------------------------------


@pytest.mark.parametrize("state", ["skipped_closed", "unconfirmed", "failed", "skipped_quiet"])
def test_6_an_edit_acts_only_on_a_delivered_or_retract_failed_copy(state, prov, monkeypatch):
    tg_route()
    announce_needs_you(monkeypatch)
    pass_()
    doc = ledger()
    (d,) = doc["deliveries"].values()
    d["state"] = state
    notify_routes._write(doc)
    close_needs_you(monkeypatch)
    pass_(), pass_(4000)
    assert prov.edits() == [] and only()["state"] == state


# ---- 7. quiet hours and the cap are enforced at the claim ---------------------------------------


def test_7_quiet_hours_skip_at_the_claim_and_nothing_is_queued(prov, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    h = datetime.now(ZoneInfo("Europe/Berlin")).hour
    quiet = {"from": f"{h:02d}:00", "to": f"{(h + 2) % 24:02d}:00", "tz": "Europe/Berlin"}
    iid = tg_route(quiet=quiet)
    announce_automation(rid="r8")
    pass_()
    assert prov.sends() == [] and only()["state"] == "skipped_quiet"
    pass_(3 * 3600)  # after the window: the skipped event is not sent late
    assert prov.sends() == []
    assert notify_routes.deliveries(iid)[0]["state"] == "skipped_quiet"


def test_7_the_hourly_cap_skips_at_the_claim(prov):
    tg_route(cap_per_hour=1)
    announce_automation(rid="r9")
    pass_()
    announce_automation(rid="r10")
    pass_()
    assert len(prov.sends()) == 1
    assert sorted(d["state"] for d in deliveries()) == ["delivered_final", "skipped_rate"]


def test_quiet_hours_cross_midnight():
    import calendar

    q = {"from": "22:00", "to": "07:00", "tz": "UTC"}

    def at(hhmm: str) -> float:
        return calendar.timegm(time.strptime(f"2026-11-12 {hhmm}", "%Y-%m-%d %H:%M"))

    assert notify_routes.in_quiet(q, at("23:30")) and notify_routes.in_quiet(q, at("06:59"))
    assert not notify_routes.in_quiet(q, at("07:00")) and not notify_routes.in_quiet(q, at("21:59"))
    day = {"from": "09:00", "to": "17:00", "tz": "UTC"}
    assert notify_routes.in_quiet(day, at("12:00")) and not notify_routes.in_quiet(day, at("17:00"))


# ---- 8. a mixed bundle --------------------------------------------------------------------------


def test_8_a_mixed_bundle_leaves_out_the_closed_and_marks_the_late_resolved(prov, monkeypatch):
    calls = {"open": True}
    monkeypatch.setattr(missions, "escalation_settled", lambda *_a, **_k: not calls["open"])
    tg_route(window_s=60)
    announce_needs_you(monkeypatch, sid=SID2)  # resolved BEFORE the send: left out
    tag_ny = needs_you_tag()
    esc = announce_escalation()  # resolved AFTER delivery: marked by reconcile
    announce_automation(rid="r11")  # never resolves
    pass_()
    assert prov.sends() == []  # the bundle is still open
    close_needs_you(monkeypatch)
    pass_(65)
    (sent,) = prov.sends()
    lines = sent["text"].split("\n")
    assert lines[0] == "BattleLab · 2 events"
    assert lines[1].startswith("• Mission escalation — " + ORIGIN + "/s/claude/")
    assert lines[2] == f"• Automation failed — {ORIGIN}/mission/automations/abc"
    d = only()
    assert tag_ny in d["members"] and tag_ny not in d["sent"] and esc in d["sent"]
    calls["open"] = False
    pass_(70)
    (edit,) = prov.edits()
    assert (
        edit["text"].split("\n")
        == [
            "BattleLab · 2 events",  # the automation line keeps the header
            "• ✓ " + lines[1][2:],
            lines[2],
        ]
    )
    assert only()["state"] == "retracted"
    # Never a member twice: more passes, and a re-announced tag, send nothing more.
    pass_(80), pass_(90)
    assert len(prov.sends()) == 1 and len(prov.edits()) == 1


# ---- 9. an episode evicted from the closed ledger is still marked -------------------------------


def test_9_a_tag_evicted_from_the_closed_ledger_still_has_its_copy_edited(prov, monkeypatch):
    tg_route()
    announce_needs_you(monkeypatch)
    pass_()
    close_needs_you(monkeypatch)
    # CLOSED_MAX eviction: neither the closed ledger nor the episodes name it any more.
    p = notifications._notifications_path()
    rows, announced, episodes = notifications._load_doc_strict(p)
    kept = {k: v for k, v in episodes.items() if not k.startswith(("closed:", "closing:"))}
    notifications._write(p, rows, announced=announced, episodes=kept)
    pass_()
    assert len(prov.edits()) == 1 and only()["state"] == "retracted"


# ---- 10 / 10b. capacity pressure on the route ledger --------------------------------------------


def _fill(n: int, *, state: str, cls: str, start: float, route: str = "c_000000000001") -> list:
    """``n`` old bundles of one member each, straight into the ledger."""
    doc = ledger()
    rbs = []
    for i in range(n):
        rb = f"r_{len(doc['bundles']):016x}"
        tag = {
            "needs you": f"needs-you:{SID}:{len(doc['bundles']):x}",
            "automation failed": f"automation:abc:x{len(doc['bundles'])}",
        }[cls]
        t = start + i
        doc["bundles"][rb] = {"route": route, "opened_at": t, "seals_at": t, "sealed": True,
                              "members": [tag]}  # fmt: skip
        doc["member_of"][f"{route}|{tag}"] = rb
        doc["snap"][f"{route}|{tag}"] = {"class": cls, "label": "Asks you a question",
                                         "link": f"/s/claude/{i}", "ts": t}  # fmt: skip
        doc["deliveries"][rb] = notify_routes._delivery(
            state, t, settled_at=t, members=[tag], sent=[tag], message_id="1", counted=True
        )
        rbs.append(rb)
    notify_routes._write(doc)
    return rbs


def _housekeep(now: float) -> None:
    notify_routes._housekeep(now, {}, None)


def test_10_final_rows_go_first_and_every_row_owing_an_edit_keeps_it(monkeypatch):
    monkeypatch.setattr(notify_routes, "KEEP_MAX", 6)
    t0 = time.time() - 86400
    owed = _fill(4, state="delivered", cls="needs you", start=t0)
    final = _fill(4, state="skipped_closed", cls="needs you", start=t0 + 100)  # newer
    _housekeep(time.time())
    doc = ledger()
    assert set(owed) <= set(doc["bundles"]) and len(doc["bundles"]) == 6
    assert sorted(set(final) & set(doc["bundles"])) == sorted(final[2:])  # the oldest final go


def test_10_when_every_row_owes_an_edit_the_oldest_is_evicted_never_silently(monkeypatch):
    monkeypatch.setattr(notify_routes, "KEEP_MAX", 3)
    route = "c_000000000001"
    owed = _fill(4, state="delivered", cls="needs you", start=time.time() - 86400, route=route)
    _housekeep(time.time())
    doc = ledger()
    assert owed[0] not in doc["bundles"] and set(owed[1:]) <= set(doc["bundles"])
    (tomb,) = doc["evicted"]
    assert tomb["rb"] == owed[0] and tomb["members"][0]["label"] == "Asks you a question"
    listed = notify_routes.deliveries(route)
    assert [e["state"] for e in listed][-1] == "retract_evicted"


def test_10b_mixed_class_saturation_prunes_automation_copies_first_and_keeps_tombstones(
    monkeypatch,
):
    monkeypatch.setattr(notify_routes, "KEEP_MAX", 4)
    route = "c_000000000001"
    t0 = time.time() - 86400
    autos = _fill(3, state="delivered_final", cls="automation failed", start=t0 + 50, route=route)
    owed = _fill(3, state="delivered", cls="needs you", start=t0, route=route)
    _housekeep(time.time())
    doc = ledger()
    assert set(owed) <= set(doc["bundles"]) and doc["evicted"] == []
    assert len(set(autos) & set(doc["bundles"])) == 1
    # Now only owed rows are left over the bound: the oldest is evicted with a tombstone, which
    # outlives the pruned bundle and a restart (a fresh read of the file).
    monkeypatch.setattr(notify_routes, "KEEP_MAX", 2)
    _housekeep(time.time())
    assert [t["rb"] for t in ledger()["evicted"]] == [owed[0]]
    assert "retract_evicted" in [e["state"] for e in notify_routes.deliveries(route)]


def test_10b_the_201st_eviction_drops_exactly_the_oldest_tombstone(monkeypatch):
    route = "c_000000000001"
    doc = ledger()
    now = time.time()
    doc["evicted"] = [
        {"rb": f"r_{i:016x}", "route": route, "delivered_at": now - 1000 + i,
         "evicted_at": now - 1000 + i, "members": []}
        for i in range(200)
    ]  # fmt: skip
    notify_routes._write(doc)
    monkeypatch.setattr(notify_routes, "KEEP_MAX", 1)
    owed = _fill(2, state="delivered", cls="needs you", start=now - 86400, route=route)
    _housekeep(now)
    ev = notify_routes.read_strict()["evicted"]  # read again from disk: a restart
    assert len(ev) == 200 and ev[0]["rb"] == f"r_{1:016x}" and ev[-1]["rb"] == owed[0]


# ---- 11. a failed final edit --------------------------------------------------------------------


def test_11_a_failed_final_edit_is_retried_without_a_further_change(prov, monkeypatch):
    tg_route()
    announce_needs_you(monkeypatch)
    pass_()
    close_needs_you(monkeypatch)
    prov.edit_status = [502]
    pass_(2)
    assert only()["state"] == "retract_failed" and len(prov.edits()) == 1
    pass_(30)  # before its backoff: not yet
    assert len(prov.edits()) == 1
    pass_(70)
    assert len(prov.edits()) == 2 and only()["state"] == "retracted"


def test_11_five_failed_edits_give_up(prov, monkeypatch):
    tg_route()
    announce_needs_you(monkeypatch)
    pass_()
    close_needs_you(monkeypatch)
    prov.edit_status = [500] * 5
    t = 2.0
    for wait in (0, *notify_routes.EDIT_BACKOFF_S[:4]):
        t += wait + 1
        pass_(t)
    assert len(prov.edits()) == 5 and only()["state"] == "retract_gave_up"


def test_11_message_is_not_modified_counts_as_marked(prov, monkeypatch):
    tg_route()
    announce_needs_you(monkeypatch)
    pass_()
    close_needs_you(monkeypatch)
    prov.edit_not_modified = True
    pass_()
    assert only()["state"] == "retracted"


# ---- the Dispatches route line ------------------------------------------------------------------


def test_the_dispatch_route_line_joins_by_member_tag(prov, monkeypatch):
    tg_route()
    announce_automation(rid="r12")
    pass_()
    doc = ledger()
    sent = notify_routes.dispatch_routes(doc, ["automation:abc:r12"])
    assert sent == [{"instance": sent[0]["instance"], "connector": "Telegram",
                     "state": "delivered_final", "sent": True, "resolved": False}]  # fmt: skip
    # A member left out of a copy that went out reads skipped, never sent.
    (d,) = doc["deliveries"].values()
    d["sent"] = []
    assert notify_routes.dispatch_routes(doc, ["automation:abc:r12"])[0]["state"] == (
        "skipped_closed"
    )
    d["state"] = "skipped_quiet"
    assert notify_routes.dispatch_routes(doc, ["automation:abc:r12"])[0]["state"] == (
        "skipped_quiet"
    )
    assert notify_routes.dispatch_routes(doc, ["automation:abc:other"]) == []


def test_the_dispatches_read_carries_routes_and_omits_them_when_the_ledger_is_unreadable(prov):
    tg_route()
    announce_automation(rid="r13")
    pass_()
    tag = "automation:abc:r13"
    d = hq_dispatch._read_strict()
    bid = d["member_of"][tag]
    d["claims"][bid] = {"state": "written", "at": time.time(), "code": ""}
    d["lines"][bid] = {"text": "Nightly docs broke again.", "contact": "HQ", "at": time.time()}
    hq_dispatch._write(d)
    (entry,) = hq_dispatch.list_view()["dispatches"]
    assert entry["routes"][0]["state"] == "delivered_final"
    notify_routes._path().write_text("{broken")
    (entry,) = hq_dispatch.list_view()["dispatches"]
    assert "routes" not in entry


# ---- Matrix identity, at enable and before every send -------------------------------------------


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"members": [BOT, ME, EVE]}, "This room has 3 people"),
        ({"members": [BOT, ME, EVE, "@x:example.org"]}, "This room has 4 people"),
        ({"members_status": 403}, "couldn't check who is in that room"),
        ({"members": None}, "couldn't check who is in that room"),
        ({"members": [BOT, "Not An Id"]}, "couldn't check who is in that room"),
        ({"encrypted": True}, "encrypted"),
    ],
)
def test_matrix_at_enable_the_room_must_be_just_the_bot_and_one_person(change, words, prov):
    with pytest.raises(store.Refused) as e:
        mx_route(prov, change)
    assert words in str(e.value)
    assert not any(i["enabled"].get("notify") for i in store.read()["instances"])


@pytest.mark.parametrize(
    ("change", "state"),
    [
        ({"members": [BOT, EVE]}, "failed"),  # the same count, another person
        ({"members": [BOT, ME, EVE]}, "failed"),  # a third member
        ({"members": [BOT]}, "failed"),  # the pinned person left
        ({"encrypted": True}, "failed"),
        ({"members_status": 403}, "held_unknown"),
        ({"members": None}, "held_unknown"),
    ],
)
def test_matrix_before_every_send_the_room_is_still_the_pinned_two(change, state, prov):
    mx_route()
    view = next(i for i in service.catalog()["instances"])
    assert view["notify"]["identity"] == {"id": DM, "label": "Marcus", "member": ME}
    for k, v in change.items():
        setattr(prov, k, v)
    announce_automation(rid="r14")
    pass_()
    assert prov.sends() == [] and only()["state"] == state
    if state == "failed":
        assert only()["code"] == "destination_changed"


def test_matrix_a_delivered_copy_is_marked_with_an_m_replace(prov, monkeypatch):
    mx_route()
    announce_needs_you(monkeypatch)
    pass_()
    (sent,) = prov.sends()
    assert sent["msgtype"] == "m.text" and sent["body"].startswith("BattleLab · 1 event")
    close_needs_you(monkeypatch)
    pass_()
    (edit,) = prov.edits()
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$ev0"}
    assert edit["m.new_content"]["body"].startswith("BattleLab · ✓ resolved")
    assert only()["state"] == "retracted"


def test_room_facts_member_ids_are_none_for_any_id_that_is_not_a_user_id(prov):
    inst = store.add("matrix")
    store.patch(inst["id"], {"origin": MX_ORIGIN, "token": MX_TOKEN})
    run(service.run_test(inst["id"]))
    b = candidates._bound(store.read()["instances"][0])
    assert run(matrix.room_facts(b, DM))["member_ids"] == sorted([BOT, ME])
    prov.members = [BOT, "@Upper:example.org"]
    facts = run(matrix.room_facts(b, DM))
    assert facts["member_ids"] is None and facts["members"] == 2
    out = run(candidates.list_candidates(inst["id"]))
    assert all("member_ids" not in c for c in out["candidates"])


# ---- after a route changes: retract_lost, and edits after notify is only switched off -----------


@pytest.mark.parametrize("change", ["generation", "rebind", "destination", "removed"])
def test_after_a_connector_change_an_open_copy_settles_retract_lost(change, prov, monkeypatch):
    iid = tg_route()
    announce_needs_you(monkeypatch)
    pass_()
    if change == "generation":
        store.patch(iid, {"token": "123456789:AAH_rotated-bot-token-zyxwvutsrqp"})
        prov.bots["123456789:AAH_rotated-bot-token-zyxwvutsrqp"] = "hq_bot"
        run(service.run_test(iid))
    elif change == "rebind":
        store.patch(iid, {"token": OTHER_BOT_TOKEN})
        run(service.run_test(iid))
    elif change == "destination":
        store.set_recipients(iid, ["-1009"], candidates.snapshot(iid))
    else:
        store.remove(iid)
    inst = next((i for i in store.read()["instances"] if i["id"] == iid), None)
    assert inst is None or not store.notify_on(inst)
    close_needs_you(monkeypatch)
    pass_()  # no route is enabled now: the ledger's open row still drives the pass
    assert prov.edits() == [] and only()["state"] == "retract_lost"


def test_after_notify_is_only_switched_off_a_later_resolution_still_edits(prov, monkeypatch):
    iid = tg_route()
    announce_needs_you(monkeypatch)
    pass_()
    store.set_notify(iid, {**_settings("42"), "enabled": False}, generation=0, identity=None)
    close_needs_you(monkeypatch)
    pass_()
    assert len(prov.edits()) == 1 and only()["state"] == "retracted"
    announce_automation(rid="r15")
    pass_()
    assert len(prov.sends()) == 1  # off: no new copy


def test_a_secret_registered_after_composing_is_refused_at_the_fence(prov, monkeypatch):
    tg_route()
    announce_automation(rid="r16")

    async def register():
        template_secrets.register_typed(["mission/automations"])

    monkeypatch.setattr(notify_routes, "_HOOK_BEFORE_SEND", register)
    pass_()
    assert prov.sends() == []
    d = only()
    assert d["state"] == "failed" and d["code"] == "secret"


def test_mail_settles_retract_unsupported(prov, monkeypatch):
    calls = []

    async def fake_send(bound, recipient, text, idem, sender, *, final_check=None):
        final_check(True)
        calls.append((recipient, text, bound.for_notify))
        return {"outcome": "sent", "code": "", "message_id": "<x@y>"}

    monkeypatch.setattr(connector_smtp, "_send", fake_send)
    monkeypatch.setattr(
        connector_smtp, "probe", lambda b: asyncio.sleep(0, {"login": "me@example.com", "repos": 0})
    )
    inst = store.add("smtp")
    me = "me@example.com"
    cfg = {"origin": "smtps://mail.example.com", "account": me, "token": "app-password-123456"}
    store.patch(inst["id"], {**cfg, "sender": me})
    from agent_sessions.connectors.adapters import ADAPTERS

    monkeypatch.setattr(ADAPTERS["smtp"], "probe", connector_smtp.probe)
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    g = store.read()["instances"][0]["generation"]
    store.set_addresses(inst["id"], [{"address": "me@example.com", "label": ""}], g)
    enable(inst["id"], "me@example.com")
    announce_needs_you(monkeypatch)
    pass_()
    assert [(r, f) for r, _t, f in calls] == [("me@example.com", "send")]
    assert only()["state"] == "retract_unsupported"
    close_needs_you(monkeypatch)
    pass_()
    assert len(calls) == 1 and only()["state"] == "retract_unsupported"


# ---- the store: notify turned off in the same write --------------------------------------------


def test_a_route_turns_off_in_the_same_write_and_says_why(prov):
    iid = tg_route()
    view = lambda: next(i for i in service.catalog()["instances"] if i["id"] == iid)  # noqa: E731
    assert view()["notify"]["enabled"] is True and view()["notify_ok"] is True
    assert view()["notify"]["identity"] == {"id": "42", "label": "Marcus (@marcus_b)", "member": ""}
    store.set_recipients(iid, ["-1009"], candidates.snapshot(iid))
    v = view()
    assert v["notify"]["enabled"] is False and v["notify"]["off"]["reason"] == "destination_removed"
    # A failed re-test turns it off too.
    store.set_recipients(iid, ["42"], candidates.snapshot(iid))
    enable(iid, "42")
    prov.bots.pop(TG_TOKEN)
    run(service.run_test(iid))
    assert view()["notify"]["off"]["reason"] in ("needs_test", "recipients_unbound")


def test_notify_needs_a_private_chat_and_the_origin(prov, monkeypatch):
    iid = tg_route()
    with pytest.raises(store.Refused) as e:
        enable(iid, "-1009")
    assert e.value.code == "not_private"
    monkeypatch.delenv("AGENT_SESSIONS_ORIGIN")
    store.set_notify(iid, {**_settings("42"), "enabled": False}, generation=0, identity=None)
    with pytest.raises(store.Refused) as e:
        enable(iid, "42")
    assert e.value.code == "needs_origin"
    v = next(i for i in service.catalog()["instances"] if i["id"] == iid)
    assert v["notify_ok"] is False and "AGENT_SESSIONS_ORIGIN" in v["notify_why"]


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"window_s": 45}, "bad_window"),
        ({"cap_per_hour": 0}, "bad_cap"),
        ({"cap_per_hour": 61}, "bad_cap"),
        ({"classes": ["watch"]}, "bad_classes"),
        ({"classes": []}, "bad_classes"),
        ({"quiet": {"from": "22:00", "to": "07:00", "tz": "Mars/Olympus"}}, "bad_quiet"),
        ({"quiet": {"from": "25:00", "to": "07:00", "tz": "UTC"}}, "bad_quiet"),
        ({"extra": 1}, "bad_body"),
        ({"window_s": 60.0}, "bad_window"),  # a float is refused with the field's own words
    ],
)
def test_the_notify_body_is_checked_field_by_field(patch, code):
    body = {"enabled": True, "destination_id": "42", "classes": ["needs you"], "window_s": 60,
            "quiet": None, "cap_per_hour": 20, "include_line": False, **patch}  # fmt: skip
    with pytest.raises(store.Refused) as e:
        store.check_notify_body(body)
    assert e.value.code == code


def test_a_store_without_notify_is_byte_for_byte_what_an_older_build_reads(prov):
    inst = store.add("telegram")
    store.patch(inst["id"], {"token": TG_TOKEN})
    rec = store.read()["instances"][0]
    assert "notify" not in rec and set(rec["enabled"]) == {"read", "send"}


# ---- the API ------------------------------------------------------------------------------------


def test_the_api_saves_a_route_and_lists_its_deliveries(prov, auth_cfg, monkeypatch):
    from test_ask_orchestrator import App

    a = App(auth_cfg)
    iid = tg_route()
    store.set_notify(iid, {**_settings("42"), "enabled": False}, generation=0, identity=None)
    body = {"enabled": True, "destination_id": "42", "classes": ["automation failed"],
            "window_s": 0, "quiet": None, "cap_per_hour": 5, "include_line": False}  # fmt: skip
    r = a.c.post(f"/api/connectors/instances/{iid}/notify", json=body, headers=a.hdr)
    assert r.status_code == 200, r.text
    assert r.json()["notify"]["enabled"] is True and r.json()["notify"]["cap_per_hour"] == 5
    r = a.c.post(
        f"/api/connectors/instances/{iid}/notify",
        json={**body, "destination_id": "-1009"},
        headers=a.hdr,
    )
    assert r.status_code == 409 and "private chat" in r.json()["detail"]
    announce_automation(rid="r17")
    pass_()
    r = a.c.get(f"/api/connectors/instances/{iid}/deliveries")
    (row,) = r.json()["deliveries"]
    assert row["state"] == "delivered_final" and row["members"][0]["label"] == "Automation failed"
    assert "text" not in row and "BattleLab ·" not in r.text
    notify_routes._path().write_text("{broken")
    r = a.c.get(f"/api/connectors/instances/{iid}/deliveries")
    assert r.status_code == 503 and r.json()["code"] == "ledger_unreadable"
    r = TestClient(a.app, base_url="https://testserver").post(
        f"/api/connectors/instances/{iid}/notify", json=body
    )
    assert r.status_code in (401, 403)


def test_escalation_settled_reads_the_missions_store(monkeypatch, tmp_path):
    db = tmp_path / "m.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    assert missions.escalation_settled(MID, "0" * 12, 1) is True  # no such mission: settled
    assert SID  # the needs-you session used across this file


def test_a_needs_you_member_is_unknown_when_the_store_is_unreadable():
    r = notify_routes.Resolver(None)
    assert r(f"needs-you:{SID}:1a") == "unknown" and r("automation:abc:r1") == "open"


@pytest.mark.parametrize("damage", ["no_snapshot", "no_membership", "foreign_sent"])
def test_a_hand_edited_ledger_with_a_broken_cross_reference_is_unreadable(damage, prov):
    """Hermes 7985: no reader meets a KeyError; the file is unreadable (503, routes omitted)."""
    iid = tg_route()
    announce_automation(rid="r20")
    pass_()
    doc = ledger()
    key = next(iter(doc["snap"]))
    if damage == "no_snapshot":
        del doc["snap"][key]
    elif damage == "no_membership":
        del doc["member_of"][key]
    else:
        next(iter(doc["deliveries"].values()))["sent"].append("automation:abc:elsewhere")
    notify_routes._write(doc)
    with pytest.raises(notify_routes.StoreUnreadable):
        notify_routes.deliveries(iid)
    assert pass_() == {"skipped": "store unreadable"}
