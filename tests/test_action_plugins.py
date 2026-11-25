"""Plugin actions (#2248 P2): the first NEW actions on the registry.

Each plugin is stored as ``{kind: "plugin", plugin, version: 1, inputs, from_delivery?}`` and goes
through the same seams as the five: strict validation, a consent scope whose every field the lines
disclose, `widened` (destination, recipient and folder are equality fields; mapped values stay a
ladder), the save check, and a run whose whole effect sits on one worker thread under the effect
lock, with the automation's authority and the connector's or project's state read again
immediately before the effect.

This file pins the contract shared by all of them and **Send a message**; each later plugin has its
own section.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time

import pytest

from agent_sessions import automation_effect_lock as effect_lock
from agent_sessions import automation_runner, template_vars, webhooks
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions import templates as tstore
from agent_sessions.actions import executors, plugins, registry
from agent_sessions.connectors import candidates, service
from agent_sessions.connectors import store as cstore
from agent_sessions.routes import automations as routes
from test_ask_messages import SendingMatrix, SendingTelegram
from test_automations import _mutations
from test_connectors_messengers import MX_ORIGIN, MX_TOKEN, TG_TOKEN, run
from test_webhook_events import PR, _deliver, _runs

NAME = "Ops ping"


@pytest.fixture(autouse=True)
def _loop_on(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    monkeypatch.setattr(effect_lock, "WRITER_WAIT_S", 0.3)


@pytest.fixture
def spawned(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(automation_runner, "spawn", lambda run, registry=None: out.append(run))
    return out


def _made(cfg: dict) -> str:
    aid = routes._create(copy.deepcopy(cfg))["id"]
    pub = routes.public(store.get(aid))
    routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )
    return aid


def _manual_run(aid: str) -> dict:
    res = store.begin_run(aid, trigger="manual", slot=f"manual:{time.time_ns()}", fire_at=None)
    assert res["claimed"], res
    return res["run"]


def _execute(run_: dict) -> dict:
    return asyncio.run(automation_runner.execute(run_, registry=None))


def _plugin(pid: str, inputs: dict, mapped: dict | None = None) -> dict:
    a = {"kind": "plugin", "plugin": pid, "version": 1, "inputs": inputs}
    return {**a, "from_delivery": mapped} if mapped else a


def _config(action: dict, trigger: dict | None = None) -> dict:
    return {
        "name": NAME,
        "trigger": trigger or {"kind": "manual"},
        "action": action,
        "policy": {"concurrency": "allow", "max_concurrent": 2},
    }


# ---- the contract every plugin shares -----------------------------------------------------------


def test_every_plugin_has_a_manifest_an_executor_and_a_module():
    assert registry.plugin_ids() == plugins.PLUGINS
    for pid in plugins.PLUGINS:
        a = registry.get(pid)
        assert a is not None and a.manifest.executor == pid
        assert executors.EXECUTORS[pid].id == pid
        mod = plugins.module(pid)
        assert mod.ID == pid and mod.LABEL == a.manifest.label
        for fn in ("validate_inputs", "pins", "target", "data", "lines", "check_save", "effect"):
            assert callable(getattr(mod, fn)), (pid, fn)


@pytest.mark.parametrize(
    "raw, why",
    [
        (_plugin("send_message", {}) | {"extra": 1}, "does not take extra"),
        (_plugin("nope", {}), "plugin must be one of"),
        ({**_plugin("send_message", {}), "version": 2}, "version must be 1"),
        ({**_plugin("send_message", {}), "version": True}, "version must be 1"),
        ({"kind": "plugin", "plugin": "send_message", "inputs": {}}, "needs version"),
        (_plugin("send_message", []), "inputs must be an object"),
    ],
)
def test_a_plugin_action_is_strict(raw, why):
    with pytest.raises(model.AutomationError, match=why):
        model.validate_action(raw)


def test_a_stored_plugin_action_that_no_longer_validates_runs_nothing():
    cfg = _config(_plugin("send_message", {"connector": "c_000000000000"}))
    assert model.coerce_config(cfg) is None


def test_the_five_keep_their_exact_shapes_and_receipts():
    """The plugin kind adds keys to NO scope of the five: their digests are pinned in
    `test_actions.py`, and none of them carries `inputs`."""
    from test_actions import SHAPES

    for raw, pins_, _golden in SHAPES.values():
        scope = model.scope_of(model.validate_config(copy.deepcopy(raw)), pins_)
        assert "inputs" not in scope and "plugin" not in (scope.get("target") or {})


def test_an_executor_refuses_another_plugins_action():
    action = _plugin("send_message", {"connector": "c_000000000000"})
    for pid in plugins.PLUGINS:
        if pid == "send_message":
            continue
        with pytest.raises(model.AutomationError):
            executors.EXECUTORS[pid].validate(action)
    with pytest.raises(model.AutomationError):
        executors.EXECUTORS["tell_hq"].validate(action)


# ---- Send a message -----------------------------------------------------------------------------


@pytest.fixture
def world(monkeypatch):
    from agent_sessions import connector_http

    tg, mx = SendingTelegram(), SendingMatrix()

    async def resolve(host, port):
        return {"api.telegram.org": ["149.154.167.220"], "matrix.example.org": ["10.0.0.7"]}[host]

    def route(request):
        return (tg if request.headers.get("host") == "api.telegram.org" else mx).handler(request)

    monkeypatch.setattr(connector_http, "_resolve", resolve)
    import httpx

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(route))
    yield tg, mx
    for iid in list(candidates._SNAP):
        candidates.forget(iid)


def _messenger(kind: str = "telegram") -> dict:
    inst = cstore.add(kind)
    body = {"token": TG_TOKEN} if kind == "telegram" else {"origin": MX_ORIGIN, "token": MX_TOKEN}
    cstore.patch(inst["id"], body)
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    run(candidates.list_candidates(inst["id"]))
    rid = "42" if kind == "telegram" else "!ops:example.org"
    cstore.set_recipients(inst["id"], [rid], candidates.snapshot(inst["id"]))
    return cstore.set_send(inst["id"], True)


def _send(iid: str, rid: str = "42", message: dict | None = None, mapped=None) -> dict:
    return _plugin(
        "send_message",
        {"connector": iid, "recipient": rid, "message": message or {"text": "backup finished"}},
        mapped,
    )


def _sent(tg) -> list[dict]:
    return tg.sent


def test_send_message_validates_strictly():
    good = _send("c_0123456789ab")
    assert model.validate_action(copy.deepcopy(good)) == good
    for inputs, why in [
        ({"connector": "c_0123456789ab", "recipient": "42"}, "needs message"),
        ({"connector": "x", "recipient": "42", "message": {"text": "t"}}, "connector must be"),
        (
            {"connector": "c_0123456789ab", "recipient": "a b", "message": {"text": "t"}},
            "recipient",
        ),
        (
            {"connector": "c_0123456789ab", "recipient": "42", "message": {"text": "t"}, "to": "x"},
            "does not take to",
        ),
        ({"connector": "c_0123456789ab", "recipient": "42", "message": {"text": " "}}, "empty"),
    ]:
        with pytest.raises(model.AutomationError, match=why):
            model.validate_action(_plugin("send_message", inputs))
    # A delivery fills a TEMPLATE's fields only, never plain text and never the recipient.
    mapped = {"recipient": {"path": "chat", "type": "text"}}
    with pytest.raises(model.AutomationError, match="values go with a template"):
        model.validate_action(_send("c_0123456789ab", mapped=mapped))


def test_send_message_is_saved_only_through_a_messenger_that_can_send_now(world):
    inst = cstore.add("telegram")
    with pytest.raises(model.AutomationError, match="can't send now"):
        routes._create(_config(_send(inst["id"])))
    ok = _messenger()
    with pytest.raises(model.AutomationError, match="isn't one you allowed"):
        routes._create(_config(_send(ok["id"], rid="77")))
    with pytest.raises(model.AutomationError, match="at most 4096"):
        routes._create(_config(_send(ok["id"], message={"text": "x" * 5000})))
    with pytest.raises(model.AutomationError, match="doesn't exist"):
        routes._create(_config(_send("c_0123456789ab")))
    assert routes._create(_config(_send(ok["id"])))["action"]["plugin"] == "send_message"


def test_a_template_with_a_secret_is_refused_for_a_message(world):
    inst = _messenger()
    template_vars.create_variable({"name": "token", "kind": "secret", "value": "s3cr3t-value-1234"})
    t = tstore.create_template(
        {
            "name": "t",
            "body": "deploy {{token}}",
            "fields": [{"name": "token", "source": "library", "kind": "secret"}],
        }
    )
    with pytest.raises(model.AutomationError, match="leave BattleLab in clear"):
        routes._create(_config(_send(inst["id"], message={"template_id": t["id"], "values": {}})))


def test_the_scope_names_the_messenger_and_the_recipient_and_widening_says_which(world):
    inst = _messenger()
    aid = _made(_config(_send(inst["id"])))
    pub = routes.public(store.get(aid))
    t = pub["scope"]["target"]
    assert t == {
        "plugin": "send_message",
        "version": 1,
        "connector": inst["id"],
        "connector_kind": "telegram",
        "recipient": "42",
        "sender": None,
    }
    text = "\n".join(pub["scope_lines"])
    assert "Telegram" in text and inst["id"] in text and "(42)" in text
    assert "Message (plain text):\nbackup finished" in pub["scope_lines"]
    assert "A delivery never changes the messenger, the recipient or the From address" in text
    other = {**pub["scope"], "target": {**t, "recipient": "43"}}
    assert model.widened(pub["scope"], other) == ["a different recipient"]
    other = {**pub["scope"], "target": {**t, "connector": "c_999999999999"}}
    assert model.widened(pub["scope"], other) == ["a different messenger"]


#: A schedule's fields are disclosed for a scheduled trigger; the plugins' scopes take one so that
#: every field the digest covers has a value the lines must show.
WEEKLY = {
    "kind": "schedule",
    "cadence": {"kind": "weekly", "days": ["mon", "fri"], "time": "07:30"},
    "tz": "Europe/Berlin",
}


def _scheduled(config: dict) -> dict:
    return {**config, "trigger": model.validate_trigger(WEEKLY)}


def _plugin_scopes() -> dict[str, dict]:
    """One scope per plugin, for the disclosure test. Pins as `compute_pins` would make them."""
    send = model.validate_config(
        _config(
            _send(
                "c_0123456789ab",
                message={"template_id": "tpl_1", "values": {"pr": "12"}},
                mapped={"title": {"path": "pull_request.title", "type": "text"}},
            ),
            trigger={"kind": "webhook", "webhook_id": "0a1b2c3d4e5f6a7b"},
        )
    )
    pins_ = {
        "template": {"id": "tpl_1", "digest": "d" * 64, "library": {}, "name": "PR"},
        "checklist": None,
        "plugin": {"of": "send_message", "connector": "c_0123456789ab", "kind": "smtp",
                   "sender": "bot@example.org"},
    }  # fmt: skip
    hook = model.validate_config(
        _config(
            _hook(fields={"job": "nightly"}, mapped={"n": {"path": "number", "type": "number"}}),
            trigger={"kind": "webhook", "webhook_id": "0a1b2c3d4e5f6a7b"},
        )
    )
    art = model.validate_config(
        _config(
            _plugin(
                "publish_artifact",
                {"project_id": "p1", "path": "reports/n.md", "title": "Nightly"},
                {"why": {"path": "pull_request.title", "type": "text"}},
            ),
            trigger={"kind": "webhook", "webhook_id": "0a1b2c3d4e5f6a7b"},
        )
    )
    art_pins = {
        "template": None,
        "checklist": None,
        "plugin": {"of": "publish_artifact", "cwd": {"path": "/w/shop", "real": "/srv/shop"}},
    }
    note = model.validate_config(
        _config(
            _plugin(
                "write_memory",
                {"project_id": "p1", "title": "Nightly", "message": {"text": "builds are green"}},
            )
        )
    )
    hook_trigger = {"kind": "webhook", "webhook_id": "0a1b2c3d4e5f6a7b"}
    title = {"title": {"path": "pull_request.title", "type": "text"}}
    cmd = model.validate_config(
        _config(
            _plugin(
                "run_command",
                {
                    "project_id": "p1",
                    "argv": [{"value": "git"}, {"value": "-C", "option": True},
                             {"value": "site"}, {"value": "tag"}, {"value": "--"},
                             {"from": "title"}],
                    "env": [{"name": "MODE", "value": "fast"}, {"name": "PR", "from": "title"}],
                    "timeout_minutes": 7,
                    "outputs": {"build_id": "text"},
                },
                title,
            ),
            trigger=hook_trigger,
        )
    )  # fmt: skip
    run_pins = {
        "cwd": {"path": "/w/shop", "real": "/srv/shop", "dev": 1, "ino": 2},
        "run_as": {"uid": 1000, "user": "op", "home": "/home/op"},
    }
    cmd_pins = {
        "template": None,
        "checklist": None,
        "plugin": {
            "of": "run_command",
            **run_pins,
            "program": {"real": "/usr/bin/git", "sha256": "a" * 64},
            "interpreter": [{"path": "/usr/bin/env", "real": "/usr/bin/env", "sha256": "b" * 64}],
        },
    }
    script = model.validate_config(
        _config(
            _plugin(
                "run_script",
                {
                    "project_id": "p1",
                    "script": "scripts/deploy.sh",
                    "args": [{"value": "--"}, {"from": "title"}],
                    "env": [{"name": "PR", "from": "title"}],
                    "outputs": {"preview_url": "text", "ok": "yes_no"},
                },
                title,
            ),
            trigger=hook_trigger,
        )
    )
    script_pins = {
        "template": None,
        "checklist": None,
        "plugin": {
            "of": "run_script",
            **run_pins,
            "script": {"path": "scripts/deploy.sh", "real": "/srv/shop/scripts/deploy.sh",
                       "sha256": "c" * 64},
            "interpreter": [{"path": "/bin/sh", "real": "/usr/bin/dash", "sha256": "d" * 64}],
        },
    }  # fmt: skip
    return {
        "run_command": model.scope_of(_scheduled(cmd), cmd_pins),
        "run_script": model.scope_of(_scheduled(script), script_pins),
        "write_memory": model.scope_of(_scheduled(note), {"template": None, "checklist": None}),
        "publish_artifact": model.scope_of(_scheduled(art), art_pins),
        "send_message": model.scope_of(_scheduled(send), pins_),
        "call_webhook": model.scope_of(_scheduled(hook), {"template": None, "checklist": None}),
    }


# Every plugin: a plugin without a scope here fails, so none escapes the disclosure check.
@pytest.mark.parametrize("pid", plugins.PLUGINS)
def test_describe_discloses_every_field_the_digest_covers(pid):
    scope = _plugin_scopes()[pid]
    base_lines = model.describe(scope)
    base_digest = routes.scope_digest(scope)
    for key, value in scope.items():
        if key == "version":
            continue
        for new, how in _mutations(value):
            mutated = {**scope, key: new}
            assert routes.scope_digest(mutated) != base_digest, (key, how)
            assert model.describe(mutated) != base_lines, f"{pid}: {key} ({how}) is not disclosed"


def test_a_message_run_sends_once_through_the_connector_and_records_it(world):
    tg, _mx = world
    inst = _messenger()
    aid = _made(_config(_send(inst["id"])))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    assert "sent to" in res["run"]["reason"]
    assert [b["text"] for b in tg.sent] == ["backup finished"] and tg.sent[0]["chat_id"] == 42
    got = store.get_run(res["run"]["id"])["inputs"]
    assert got["message"] == "backup finished" and "(42)" in got["to"]


def test_a_matrix_message_goes_to_the_saved_room(world):
    _tg, mx = world
    inst = _messenger("matrix")
    aid = _made(_config(_send(inst["id"], rid="!ops:example.org")))
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    assert [b["body"] for _txn, b in mx.puts] == ["backup finished"]


def test_a_disable_before_the_effect_sends_nothing(world, monkeypatch):
    tg, _mx = world
    aid = _made(_config(_send(_messenger()["id"])))
    real = store.set_inputs

    def then_disabled(rid, inputs):
        real(rid, inputs)
        routes._simple(aid, "disable")

    monkeypatch.setattr(store, "set_inputs", then_disabled)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "stopped" and "turned it off" in res["run"]["reason"]
    assert tg.sent == []


def test_a_disable_at_the_outbound_boundary_sends_nothing(world, monkeypatch):
    """The last authority read is the connector transport's final check, after the rate claim."""
    tg, _mx = world
    aid = _made(_config(_send(_messenger()["id"])))
    real = cstore.take_send

    def then_disabled(*a, **kw):
        real(*a, **kw)
        routes._simple(aid, "disable")

    monkeypatch.setattr(cstore, "take_send", then_disabled)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "stopped", res["run"]
    assert tg.sent == []


@pytest.mark.parametrize("change", ["send_off", "recipient_gone", "removed"])
def test_a_revoked_or_deleted_messenger_refuses_the_run(world, change):
    tg, _mx = world
    inst = _messenger()
    aid = _made(_config(_send(inst["id"])))
    if change == "send_off":
        cstore.set_send(inst["id"], False)
    elif change == "recipient_gone":
        # 42 withdrawn: the allowlist now holds another chat only, so sending stays on.
        run(candidates.list_candidates(inst["id"]))
        cstore.set_recipients(inst["id"], ["-1009"], candidates.snapshot(inst["id"]))
    else:
        cstore.remove(inst["id"])
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused", res["run"]
    assert tg.sent == []


def test_a_cancel_during_the_send_then_a_disable_reports_in_flight_and_one_send(world, monkeypatch):
    tg, _mx = world
    aid = _made(_config(_send(_messenger()["id"])))
    entered, release = threading.Event(), threading.Event()
    real = tg.handler

    def slow(request):
        if request.url.path.endswith("/sendMessage"):
            entered.set()
            assert release.wait(10)
        return real(request)

    tg.handler = slow
    run_ = _manual_run(aid)

    async def go():
        task = asyncio.ensure_future(automation_runner.execute(run_, registry=None))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()  # a second cancel while it joins the worker
        disabled = await asyncio.to_thread(routes._simple, aid, "disable")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return disabled

    disabled = asyncio.run(go())
    assert disabled["in_flight"] is True
    assert len(tg.sent) == 1
    assert _runs(aid)[0]["outcome"] == "ok"


def test_a_webhook_fills_template_fields_and_never_the_recipient(world, spawned):
    tg, _mx = world
    inst = _messenger()
    t = tstore.create_template(
        {"name": "pr", "body": "PR {{number}}: {{title}}", "fields": [{"name": "number"},
                                                                       {"name": "title"}]}
    )  # fmt: skip
    pub, _ = webhooks.create("ci")
    mapped = {
        "number": {"path": "pull_request.number", "type": "number"},
        "title": {"path": "pull_request.title", "type": "text"},
    }
    action = _send(inst["id"], message={"template_id": t["id"], "values": {}}, mapped=mapped)
    aid = _made(_config(action, trigger={"kind": "webhook", "webhook_id": pub["id"]}))
    # The payload names another chat: nothing maps it, so it changes nothing.
    _deliver(aid, pub["id"], {**PR, "chat_id": 99, "recipient": "99"})
    assert len(spawned) == 1
    res = _execute(spawned[0])
    assert res["run"]["outcome"] == "ok", res["run"]
    assert tg.sent == [
        {"chat_id": 42, "text": "PR 41: Fix totals", "link_preview_options": {"is_disabled": True}}
    ]


def test_a_secret_registered_after_the_save_never_leaves(world):
    tg, _mx = world
    inst = _messenger()
    aid = _made(_config(_send(inst["id"], message={"text": "key is hunter2-very-secret-value"})))
    template_vars.create_variable(
        {"name": "key", "kind": "secret", "value": "hunter2-very-secret-value"}
    )
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "secret" in res["run"]["reason"]
    assert tg.sent == []


# ---- Call a webhook -----------------------------------------------------------------------------

HOOK_URL = "https://hooks.example.org/battlelab?team=ops"


class Receiver:
    """A fake destination: records each request; answers what `mode` says."""

    def __init__(self) -> None:
        self.requests: list = []
        self.mode = "ok"  # ok | redirect | 400 | 500 | drop
        self.addresses = {
            "hooks.example.org": ["93.184.216.34"],
            "lan.example.org": ["10.0.0.5"],
            # Hermes on #2275: an IPv6 address that CARRIES a private IPv4 one reaches that host.
            "nat64.example.org": ["64:ff9b::a00:5"],  # NAT64 of 10.0.0.5
            "sixtofour.example.org": ["2002:a00:5::"],  # 6to4 of 10.0.0.5
            "nat64-lo.example.org": ["64:ff9b::7f00:1"],  # NAT64 of 127.0.0.1
            "nat64-public.example.org": ["64:ff9b::5db8:d822"],  # NAT64 of 93.184.216.34
        }
        self.resolve_delay = 0.0
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    async def resolve(self, host, port):
        if self.resolve_delay:
            await asyncio.sleep(self.resolve_delay)
        return self.addresses[host]

    def handler(self, request):
        import httpx

        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        self.requests.append(request)
        if self.mode == "redirect":
            return httpx.Response(302, headers={"location": "https://elsewhere.example.org/x"})
        if self.mode == "drop":
            raise httpx.ReadTimeout("no reply", request=request)
        if self.mode in ("400", "500"):
            return httpx.Response(int(self.mode), json={})
        return httpx.Response(204)


@pytest.fixture
def receiver(monkeypatch):
    import httpx

    from agent_sessions.actions import call_webhook

    r = Receiver()
    monkeypatch.setattr(call_webhook, "_resolve", r.resolve)
    monkeypatch.setattr(call_webhook, "_TRANSPORT", httpx.MockTransport(r.handler))
    return r


def _hook(dest: str = HOOK_URL, fields=None, mapped=None, allow_private=None) -> dict:
    inputs: dict = {"destination": dest, "fields": fields or {"job": "nightly"}}
    if allow_private is not None:
        inputs["allow_private"] = allow_private
    return _plugin("call_webhook", inputs, mapped)


def _hook_made(action: dict, trigger: dict | None = None) -> tuple[str, str]:
    out = routes._create(copy.deepcopy(_config(action, trigger)))
    secret = out["destination_secret"]
    pub = routes.public(store.get(out["id"]))
    routes._enable(
        out["id"],
        {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]},
    )
    return out["id"], secret


@pytest.mark.parametrize(
    "dest, extra, why",
    [
        ("http://hooks.example.org/x", {}, "must be an https address"),
        ("https://user:pw@hooks.example.org/x", {}, "user name or password"),
        ("https://hooks.example.org/x#frag", {}, "#fragment"),
        ("https:///x", {}, "needs a host"),
        ("https://10.0.0.5/x", {}, "private or local network"),
        ("https://127.0.0.1/x", {}, "private or local network"),
        ("https://169.254.169.254/latest", {"allow_private": True}, "link-local"),
        ("https://hooks.example.org/a b", {}, "spaces or control"),
    ],
)
def test_the_destination_is_https_only_and_private_addresses_are_refused(dest, extra, why):
    with pytest.raises(model.AutomationError, match=why):
        model.validate_action(_plugin("call_webhook", {"destination": dest, **extra}))


def test_call_webhook_validates_strictly():
    got = model.validate_action(_hook("HTTPS://Hooks.Example.org:443/battlelab?team=ops"))
    assert got["inputs"] == {
        "destination": HOOK_URL,
        "allow_private": False,
        "fields": {"job": "nightly"},
    }
    assert model.validate_action(_hook("https://10.0.0.5/x", allow_private=True))
    for inputs, why in [
        ({"destination": HOOK_URL, "fields": {"Bad": "x"}}, "not a field name"),
        ({"destination": HOOK_URL, "allow_private": "yes"}, "true or false"),
        ({"destination": HOOK_URL, "url": "x"}, "does not take url"),
        ({"destination": HOOK_URL, "fields": {f"f{i}": "x" for i in range(17)}}, "at most 16"),
    ]:
        with pytest.raises(model.AutomationError, match=why):
            model.validate_action(_plugin("call_webhook", inputs))
    mapped = {"job": {"path": "action", "type": "text"}}
    with pytest.raises(model.AutomationError, match="fixed value and a value from the delivery"):
        model.validate_action(_hook(mapped=mapped))


def test_the_secret_is_made_at_save_shown_once_and_never_again(receiver):
    out = routes._create(copy.deepcopy(_config(_hook())))
    aid, secret = out["id"], out["destination_secret"]
    assert len(secret) == 64 and out["destination"]["secret_set"] is True
    pub = routes.public(store.get(aid))
    assert "destination_secret" not in pub and secret not in json.dumps(pub)
    assert pub["destination"] == {
        "url": HOOK_URL,
        "secret_set": True,
        "secret_made_at": pub["destination"]["secret_made_at"],
    }
    # A save that keeps the destination shows nothing; a new destination gets a new secret.
    again = routes._patch(aid, {"name": "renamed", "revision": pub["revision"]})
    assert "destination_secret" not in again
    moved = routes._patch(
        aid,
        {"action": _hook("https://hooks.example.org/other"), "revision": again["revision"]},
    )
    assert moved["destination_secret"] not in (None, secret)
    fresh = routes._new_destination_secret(aid)
    assert fresh["secret"] not in (secret, moved["destination_secret"])


def test_a_new_secret_is_only_for_an_automation_that_calls_a_webhook(world):
    aid = routes._create(_config(_send(_messenger()["id"])))["id"]
    assert routes.public(store.get(aid))["destination"] is None
    with pytest.raises(model.AutomationError, match="does not call a webhook"):
        routes._new_destination_secret(aid)


def test_a_run_posts_once_signed_with_the_destinations_secret(receiver):
    import hashlib
    import hmac

    from agent_sessions.actions import destination_secrets

    aid, secret = _hook_made(_hook())
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    assert len(receiver.requests) == 1
    req = receiver.requests[0]
    assert req.method == "POST" and str(req.url).endswith("/battlelab?team=ops")
    assert req.headers["host"] == "hooks.example.org"
    body = req.content
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert req.headers["x-battlelab-signature"] == want
    doc = json.loads(body)
    assert doc["fields"] == {"job": "nightly"} and doc["automation"] == aid
    assert doc["run"] == res["run"]["id"]
    # The secret is never in a response, a run record or any store file at rest.
    run_ = store.get_run(res["run"]["id"])
    assert secret not in json.dumps(run_) and secret not in json.dumps(routes.public_run(run_))
    assert secret not in json.dumps(routes.public(store.get(aid)))
    assert want not in json.dumps(run_)
    for f in [store.db_path(), destination_secrets.path()]:
        for p in f.parent.glob(f.name + "*"):
            assert secret.encode() not in p.read_bytes(), p


def test_a_redirect_is_reported_and_never_followed(receiver):
    receiver.mode = "redirect"
    aid, _ = _hook_made(_hook())
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "failed" and "never followed" in res["run"]["reason"]
    assert len(receiver.requests) == 1  # nothing went to the Location


@pytest.mark.parametrize(
    "host, why",
    [
        ("nat64.example.org", "private or local"),
        ("sixtofour.example.org", "private or local"),
        ("nat64-lo.example.org", "always refused"),
    ],
)
def test_an_ipv6_address_carrying_a_private_ipv4_one_is_refused(receiver, host, why):
    """Hermes on #2275: NAT64 (64:ff9b::/96) and 6to4 (2002::/16) addresses are judged as the
    IPv4 address they carry, like `net_vetting.unsafe_ip` does; only `::ffff:` was before."""
    aid, _ = _hook_made(_hook(f"https://{host}/hook"))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused", res["run"]
    assert why in res["run"]["reason"] and receiver.requests == []


def test_a_nat64_address_of_a_public_host_is_still_public(receiver):
    aid, _ = _hook_made(_hook("https://nat64-public.example.org/hook"))
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    assert len(receiver.requests) == 1


@pytest.mark.parametrize("literal", ["[64:ff9b::a00:5]", "[2002:a00:5::]", "[::ffff:10.0.0.5]"])
def test_a_literal_ipv6_carrying_a_private_ipv4_is_refused_at_save(literal):
    with pytest.raises(model.AutomationError, match="private or local network"):
        model.validate_action(_plugin("call_webhook", {"destination": f"https://{literal}/x"}))


def test_a_name_that_resolves_to_a_private_address_is_refused_unless_allowed(receiver):
    aid, _ = _hook_made(_hook("https://lan.example.org/hook"))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "private or local" in res["run"]["reason"]
    assert receiver.requests == []
    aid2, _ = _hook_made(_hook("https://lan.example.org/hook", allow_private=True))
    assert _execute(_manual_run(aid2))["run"]["outcome"] == "ok"
    assert len(receiver.requests) == 1


def test_a_missing_secret_refuses_the_run_and_sends_nothing(receiver):
    from agent_sessions.actions import destination_secrets

    aid, _ = _hook_made(_hook())
    destination_secrets.drop(aid)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "no signing secret" in res["run"]["reason"]
    assert receiver.requests == []


def test_no_answer_is_never_sent_again(receiver):
    receiver.mode = "drop"
    aid, _ = _hook_made(_hook())
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "failed" and "not sent again" in res["run"]["reason"]
    assert len(receiver.requests) == 1


def test_a_disable_before_the_post_sends_nothing(receiver, monkeypatch):
    aid, _ = _hook_made(_hook())
    real = store.set_inputs

    def then_disabled(rid, inputs):
        real(rid, inputs)
        routes._simple(aid, "disable")

    monkeypatch.setattr(store, "set_inputs", then_disabled)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "stopped" and receiver.requests == []


def test_a_cancel_during_the_post_then_a_disable_reports_in_flight_and_one_post(receiver):
    receiver.gate = threading.Event()
    aid, _ = _hook_made(_hook())
    run_ = _manual_run(aid)

    async def go():
        task = asyncio.ensure_future(automation_runner.execute(run_, registry=None))
        while not receiver.entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        disabled = await asyncio.to_thread(routes._simple, aid, "disable")
        receiver.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return disabled

    disabled = asyncio.run(go())
    assert disabled["in_flight"] is True
    assert len(receiver.requests) == 1
    assert _runs(aid)[0]["outcome"] == "ok"


def test_a_delivery_fills_typed_fields_and_never_the_destination(receiver, spawned):
    pub, _ = webhooks.create("ci")
    mapped = {
        "number": {"path": "pull_request.number", "type": "number"},
        "draft": {"path": "pull_request.draft", "type": "yes_no"},
    }
    aid, _ = _hook_made(_hook(mapped=mapped), trigger={"kind": "webhook", "webhook_id": pub["id"]})
    _deliver(aid, pub["id"], {**PR, "destination": "https://evil.example.org/x"})
    res = _execute(spawned[0])
    assert res["run"]["outcome"] == "ok", res["run"]
    (req,) = receiver.requests
    assert req.headers["host"] == "hooks.example.org"
    assert json.loads(req.content)["fields"] == {"draft": False, "job": "nightly", "number": 41}


def test_the_destination_is_an_equality_field_of_the_consent(receiver):
    aid, _ = _hook_made(_hook())
    scope = routes.public(store.get(aid))["scope"]
    moved = {**scope, "target": {**scope["target"], "destination": "https://x.example.org/"}}
    assert model.widened(scope, moved) == ["a different destination"]
    opened = {**scope, "target": {**scope["target"], "allow_private": True}}
    assert model.widened(scope, opened) == ["which network addresses it may reach changed"]
    text = "\n".join(routes.public(store.get(aid))["scope_lines"])
    assert HOOK_URL in text and "job = nightly" in text and "X-BattleLab-Signature" in text


def test_a_rotation_racing_a_destination_change_makes_the_secret_for_the_new_address(
    receiver, monkeypatch
):
    """Hermes on #2275: a rotation that read the OLD address must not overwrite the secret a
    concurrent save just made for the new one. The rotation re-reads the stored address inside
    the secrets lock, under the same effect-lock writer a save holds."""
    from agent_sessions.actions import destination_secrets

    aid, _ = _hook_made(_hook())
    routes._simple(aid, "disable")  # an enabled one would ask consent for the new address
    new_url = "https://hooks.example.org/moved"
    real = destination_secrets.mint
    raced: list[dict] = []

    def save_moves_it_first(*a, **kw):
        if not raced:
            # The operator's save of a new address lands between the rotation's start (it saw
            # the old address) and its mint.
            row = store.get(aid)
            raced.append(
                routes._patch(aid, {"action": _hook(new_url), "revision": row["revision"]})
            )
        return real(*a, **kw)

    monkeypatch.setattr(destination_secrets, "mint", save_moves_it_first)
    out = routes._new_destination_secret(aid)
    assert raced and raced[0]["destination"]["url"] == new_url
    assert out["destination"] == {
        "url": new_url,
        "secret_set": True,
        "secret_made_at": out["destination"]["secret_made_at"],
    }
    assert destination_secrets.signing_secret(aid, new_url) == out["secret"]
    assert destination_secrets.signing_secret(aid, HOOK_URL) is None
    assert routes.public(store.get(aid))["destination"]["secret_set"] is True


def test_a_rotation_that_cannot_take_the_fence_from_a_running_send_mints_nothing(
    receiver, monkeypatch
):
    """Hermes on #2275: a run holds the effect lock with the OLD secret loaded until its first
    byte. A rotation whose writer wait runs out must not replace the secret (the run would send
    with a retired one after the rotation said it had stopped): it refuses, retryable, and the
    old secret still signs."""
    import threading

    from agent_sessions import automation_effect_lock as effect_lock
    from agent_sessions.actions import destination_secrets

    aid, old = _hook_made(_hook())
    monkeypatch.setattr(effect_lock, "WRITER_WAIT_S", 0.05)
    holding, release = threading.Event(), threading.Event()

    def a_run_mid_send():
        with effect_lock.runner_sync(aid) as held:
            assert held
            holding.set()
            release.wait(10)

    t = threading.Thread(target=a_run_mid_send)
    t.start()
    try:
        assert holding.wait(5)
        with pytest.raises(model.AutomationError, match="sending right now") as e:
            routes._new_destination_secret(aid)
        assert e.value.status == 409
        assert destination_secrets.signing_secret(aid, HOOK_URL) == old  # nothing was minted
    finally:
        release.set()
        t.join(10)
    fresh = routes._new_destination_secret(aid)  # once the run is done, it rotates
    assert fresh["secret"] != old
    assert destination_secrets.signing_secret(aid, HOOK_URL) == fresh["secret"]


def test_a_rotation_of_an_automation_that_stopped_calling_a_webhook_is_refused(
    receiver, monkeypatch, world
):
    from agent_sessions.actions import destination_secrets

    aid, old = _hook_made(_hook())
    msg = _send(_messenger()["id"])
    real = destination_secrets.mint

    def switched_first(*a, **kw):
        routes._patch(
            aid, {"action": msg, "revision": store.get(aid)["revision"], "consent": False}
        )
        return real(*a, **kw)

    monkeypatch.setattr(destination_secrets, "mint", switched_first)
    # The automation is enabled: a different action widens, so turn it off first.
    routes._simple(aid, "disable")
    with pytest.raises(model.AutomationError, match="does not call a webhook") as e:
        routes._new_destination_secret(aid)
    assert e.value.status == 409
    assert destination_secrets.signing_secret(aid, HOOK_URL) == old  # nothing was minted


@pytest.mark.parametrize(
    "value",
    ["ghp_0123456789abcdefABCDEF0123456789abcd", "Bearer sk-live-abcdef0123456789abcdef0123"],
)
def test_a_fixed_field_that_looks_like_a_credential_is_refused_at_save(receiver, value):
    with pytest.raises(model.AutomationError, match="looks like a credential"):
        routes._create(_config(_hook(fields={"token": value})))


def test_a_fixed_field_holding_a_stored_secret_is_refused_at_save(receiver):
    template_vars.create_variable({"name": "tok", "kind": "secret", "value": "plain-but-secret-77"})
    with pytest.raises(model.AutomationError, match="holds a stored secret"):
        routes._create(_config(_hook(fields={"note": "use plain-but-secret-77 please"})))


def test_a_timeout_before_the_request_is_not_sent_never_may_have_been_delivered(
    receiver, monkeypatch
):
    from agent_sessions.actions import call_webhook

    aid, _ = _hook_made(_hook())
    monkeypatch.setattr(call_webhook, "TOTAL_TIMEOUT_S", 0.2)
    receiver.resolve_delay = 2.0
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "failed", res["run"]
    assert "nothing was sent" in res["run"]["reason"]
    assert "may have been delivered" not in res["run"]["reason"]
    assert receiver.requests == []


# ---- Publish an artifact ------------------------------------------------------------------------


@pytest.fixture
def shop(tmp_path, monkeypatch):
    from agent_sessions import projects

    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "shop"
    (root / "reports").mkdir(parents=True)
    (root / "reports" / "nightly.md").write_text("# Nightly\nall green\n")
    (root / "site").mkdir()
    (root / "site" / "index.html").write_text("<h1>hi</h1>")
    (root / "leak.env.txt").write_text("API_KEY=sk-live-abcdef0123456789abcdef0123456789\n")
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "x.md").write_text("x")
    proj = projects.create("shop", folders=[str(root)], default_folder=str(root))
    return {"id": proj.id, "root": root, "other": other}


def _artifact(pid: str, path: str = "reports/nightly.md", **extra) -> dict:
    mapped = extra.pop("mapped", None)
    return _plugin(
        "publish_artifact",
        {
            "project_id": pid,
            "path": path,
            "title": "Nightly report",
            "why": "the 03:00 run",
            **extra,
        },
        mapped,
    )


@pytest.mark.parametrize(
    "path, why",
    [
        ("/etc/passwd", "never absolute"),
        ("~/x", "never absolute"),
        ("../elsewhere/x.md", "no .."),
        ("reports/../../x", "no .."),
        (".git/config", "hidden"),
        (".", "not all of it"),
    ],
)
def test_the_artifact_path_is_relative_and_inside_the_project(path, why):
    with pytest.raises(model.AutomationError, match=why):
        model.validate_action(_artifact("p1", path))


def test_publish_artifact_validates_strictly():
    with pytest.raises(model.AutomationError, match="needs a title"):
        model.validate_action(_artifact("p1", title=""))
    with pytest.raises(model.AutomationError, match="only title and why"):
        model.validate_action(_artifact("p1", mapped={"path": {"path": "x", "type": "text"}}))
    ok = model.validate_action(
        _artifact("p1", title="", mapped={"title": {"path": "t", "type": "text"}})
    )
    assert ok["from_delivery"] == {"title": {"path": "t", "type": "text"}}


def test_an_out_of_scope_or_missing_item_is_refused_at_save(shop):
    with pytest.raises(model.AutomationError, match="does not exist"):
        routes._create(_config(_artifact(shop["id"], "reports/nope.md")))
    os_link = shop["root"] / "out"
    os_link.symlink_to(shop["other"])
    with pytest.raises(model.AutomationError, match="symbolic link"):
        routes._create(_config(_artifact(shop["id"], "out")))
    from agent_sessions import prefs

    prefs.set_folder_exclusions([str(shop["root"])])
    with pytest.raises(model.AutomationError, match="outside your project folders"):
        routes._create(_config(_artifact(shop["id"])))


def test_a_run_publishes_a_private_artifact_and_records_its_link(shop):
    from agent_sessions import artifact_store

    aid = _made(_config(_artifact(shop["id"])))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    assert "published privately" in res["run"]["reason"]
    out = store.get_run(res["run"]["id"])["inputs"]["output"]
    got = artifact_store.get(out["artifact"])
    assert got["title"] == "Nightly report" and got["visibility"] == "private"
    assert got["origin"]["engine"] == "automation"
    # The next run is a new revision behind the same link, still private.
    (shop["root"] / "reports" / "nightly.md").write_text("# Nightly\none red\n")
    res2 = _execute(_manual_run(aid))
    out2 = store.get_run(res2["run"]["id"])["inputs"]["output"]
    assert out2["artifact"] == out["artifact"] and "revision 2" in res2["run"]["reason"]
    # Once the operator shares it, a run never puts new content behind the public link.
    artifact_store.share(out["artifact"])
    res3 = _execute(_manual_run(aid))
    out3 = store.get_run(res3["run"]["id"])["inputs"]["output"]
    assert out3["artifact"] != out["artifact"]
    assert artifact_store.get(out3["artifact"])["visibility"] == "private"


def _first_publish(shop) -> tuple[str, str]:
    """An automation that has published once (revision 1, private): ``(automation, artifact)``."""
    aid = _made(_config(_artifact(shop["id"])))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    return aid, store.get_run(res["run"]["id"])["inputs"]["output"]["artifact"]


def _went_public_mid_publish(shop, monkeypatch, make_public, where: str) -> None:
    """Hermes on #2276: the operator makes the artifact public AFTER the run chose it (from a
    private snapshot) and BEFORE the store's revision transaction. The new content must go to a new
    PRIVATE artifact; the public one keeps its revision."""
    from agent_sessions import artifact_store

    aid, art = _first_publish(shop)
    (shop["root"] / "reports" / "nightly.md").write_text("# Nightly\nnew content\n")
    fired: list[bool] = []
    # Right before the store's publish call, or while its files are being staged.
    name = "publish" if where == "publish" else "_judge_staged"
    real = getattr(artifact_store, name)

    def public_now(*a, **kw):
        if not fired:
            fired.append(True)
            make_public(art)
        return real(*a, **kw)

    monkeypatch.setattr(artifact_store, name, public_now)
    res = _execute(_manual_run(aid))
    assert fired, "the share never landed between the choice and the commit"
    assert res["run"]["outcome"] == "ok", res["run"]
    out = store.get_run(res["run"]["id"])["inputs"]["output"]
    assert out["artifact"] != art, "new content went behind the public link"
    public = artifact_store.get(art)
    assert public["revision"] == 1 and public["visibility"] == "public"
    fresh = artifact_store.get(out["artifact"])
    assert fresh["visibility"] == "private" and fresh["revision"] == 1
    assert "published privately" in res["run"]["reason"]


def _share_box(art: str) -> None:
    from agent_sessions import artifact_store

    artifact_store.share(art)


def _relay_copy(art: str) -> None:
    from agent_sessions import artifact_store

    artifact_store.set_relay(art, "relaypub0001", "https://r.example.org/x/", 1)


def _relay_pending(art: str) -> None:
    from agent_sessions import artifact_store

    artifact_store.mark_relay_pending(art, True)


@pytest.mark.parametrize("make_public", [_share_box, _relay_copy, _relay_pending])
@pytest.mark.parametrize("where", ["publish", "staging"])
def test_an_artifact_made_public_between_the_choice_and_the_commit_is_never_revised(
    shop, monkeypatch, make_public, where
):
    _went_public_mid_publish(shop, monkeypatch, make_public, where)


def test_a_file_with_a_secret_refuses_the_publish(shop):
    aid = _made(_config(_artifact(shop["id"], "leak.env.txt")))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused", res["run"]
    assert "nothing was published" in res["run"]["reason"]
    from agent_sessions import artifact_store

    assert artifact_store.list_artifacts()["items"] == []


def test_an_item_moved_out_of_scope_after_save_is_refused(shop):
    from agent_sessions import artifact_store, prefs

    aid = _made(_config(_artifact(shop["id"])))
    prefs.set_folder_exclusions([str(shop["root"] / "reports")])
    res = _execute(_manual_run(aid))
    assert (
        res["run"]["outcome"] == "refused"
        and "outside your project folders" in res["run"]["reason"]
    )
    assert artifact_store.list_artifacts()["items"] == []


def test_a_disable_before_the_publish_publishes_nothing(shop, monkeypatch):
    from agent_sessions import artifact_store

    aid = _made(_config(_artifact(shop["id"])))
    real = store.set_inputs

    def then_disabled(rid, inputs):
        real(rid, inputs)
        routes._simple(aid, "disable")

    monkeypatch.setattr(store, "set_inputs", then_disabled)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "stopped"
    assert artifact_store.list_artifacts()["items"] == []


def test_a_cancel_during_the_publish_then_a_disable_reports_in_flight_and_one_artifact(
    shop, monkeypatch
):
    from agent_sessions import artifact_store

    aid = _made(_config(_artifact(shop["id"])))
    entered, release = threading.Event(), threading.Event()
    real = artifact_store.publish

    def slow(origin, args, **kw):
        entered.set()
        assert release.wait(10)
        return real(origin, args, **kw)

    monkeypatch.setattr(artifact_store, "publish", slow)
    run_ = _manual_run(aid)

    async def go():
        task = asyncio.ensure_future(automation_runner.execute(run_, registry=None))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        disabled = await asyncio.to_thread(routes._simple, aid, "disable")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return disabled

    disabled = asyncio.run(go())
    assert disabled["in_flight"] is True
    assert len(artifact_store.list_artifacts()["items"]) == 1
    assert _runs(aid)[0]["outcome"] == "ok"


def test_a_delivery_fills_the_title_and_never_the_path(shop, spawned):
    from agent_sessions import artifact_store

    pub, _ = webhooks.create("ci")
    action = _artifact(
        shop["id"], title="", mapped={"title": {"path": "pull_request.title", "type": "text"}}
    )
    aid = _made(_config(action, trigger={"kind": "webhook", "webhook_id": pub["id"]}))
    _deliver(aid, pub["id"], {**PR, "path": "../elsewhere/x.md"})
    res = _execute(spawned[0])
    assert res["run"]["outcome"] == "ok", res["run"]
    (item,) = artifact_store.list_artifacts()["items"]
    assert item["title"] == "Fix totals"
    files = [f["path"] for f in artifact_store.listing(item["id"])]
    assert files == ["nightly.md"]


def test_a_repointed_project_folder_needs_approval_again(shop, tmp_path):
    from agent_sessions import projects

    aid = _made(_config(_artifact(shop["id"])))
    new = tmp_path / "shop2" / "reports"
    new.mkdir(parents=True)
    (new / "nightly.md").write_text("x")
    projects.update(
        shop["id"], folders=[str(tmp_path / "shop2")], default_folder=str(tmp_path / "shop2")
    )
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "approval again" in res["run"]["reason"]
    assert store.get(aid)["needs_reapproval"]


# ---- Write a memory note ------------------------------------------------------------------------


@pytest.fixture
def notebook(tmp_path, monkeypatch):
    from agent_sessions import memory, projects

    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    pid = projects.create("demo", folders=[str(ws)]).id
    memory.set_enabled(pid, True)
    return pid


def _note(pid: str, text: str = "The nightly build needs 12 GB of RAM", **kw) -> dict:
    return _plugin(
        "write_memory",
        {
            "project_id": pid,
            "title": kw.pop("title", "Nightly build memory"),
            "message": {"text": text},
        },
        kw.pop("mapped", None),
    )


def _notes(pid: str) -> list[dict]:
    from agent_sessions import memory

    return memory.notes(pid)


def test_write_memory_validates_strictly():
    with pytest.raises(model.AutomationError, match="needs a title"):
        model.validate_action(_note("p1", title=" "))
    with pytest.raises(model.AutomationError, match="does not take slug"):
        model.validate_action(
            _plugin(
                "write_memory",
                {"project_id": "p1", "title": "t", "message": {"text": "x"}, "slug": "s"},
            )
        )


def test_a_note_is_saved_only_where_memory_is_on(notebook, tmp_path):
    from agent_sessions import memory, projects

    (tmp_path / "off").mkdir()
    other = projects.create("off", folders=[str(tmp_path / "off")]).id
    with pytest.raises(model.AutomationError, match="memory is off"):
        routes._create(_config(_note(other)))
    with pytest.raises(model.AutomationError, match="doesn't exist"):
        routes._create(_config(_note("nope")))
    assert routes._create(_config(_note(notebook)))["action"]["plugin"] == "write_memory"
    memory.set_enabled(notebook, False)


def test_a_run_files_the_note_through_the_memory_service_once(notebook):
    aid = _made(_config(_note(notebook)))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    assert "filed in project memory" in res["run"]["reason"]
    (n,) = _notes(notebook)
    assert n["name"] == "Nightly build memory" and "12 GB" in json.dumps(n)


def test_a_note_with_a_credential_is_held_back_by_the_review(notebook):
    aid = _made(_config(_note(notebook, "deploy key ghp_0123456789abcdefABCDEF0123456789abcd")))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused", res["run"]
    assert "held back by BattleLab's review" in res["run"]["reason"]
    assert _notes(notebook) == []


def test_memory_switched_off_after_save_refuses_the_run(notebook):
    from agent_sessions import memory

    aid = _made(_config(_note(notebook)))
    memory.set_enabled(notebook, False)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "nothing was filed" in res["run"]["reason"]


def test_a_disable_before_the_note_files_nothing(notebook, monkeypatch):
    aid = _made(_config(_note(notebook)))
    real = store.set_inputs

    def then_disabled(rid, inputs):
        real(rid, inputs)
        routes._simple(aid, "disable")

    monkeypatch.setattr(store, "set_inputs", then_disabled)
    assert _execute(_manual_run(aid))["run"]["outcome"] == "stopped"
    assert _notes(notebook) == []


def test_a_cancel_during_the_write_then_a_disable_reports_in_flight_and_one_note(
    notebook, monkeypatch
):
    from agent_sessions import memory_service

    aid = _made(_config(_note(notebook)))
    entered, release = threading.Event(), threading.Event()
    real = memory_service.submit

    def slow(*a, **kw):
        entered.set()
        assert release.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(memory_service, "submit", slow)
    run_ = _manual_run(aid)

    async def go():
        task = asyncio.ensure_future(automation_runner.execute(run_, registry=None))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        disabled = await asyncio.to_thread(routes._simple, aid, "disable")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return disabled

    disabled = asyncio.run(go())
    assert disabled["in_flight"] is True
    assert len(_notes(notebook)) == 1
    assert _runs(aid)[0]["outcome"] == "ok"


def test_a_delivery_fills_the_notes_template_and_never_the_project(notebook, spawned, tmp_path):
    from agent_sessions import memory, projects

    (tmp_path / "other").mkdir()
    other = projects.create("other", folders=[str(tmp_path / "other")]).id
    memory.set_enabled(other, True)
    t = tstore.create_template(
        {"name": "pr note", "body": "PR {{title}} was opened", "fields": [{"name": "title"}]}
    )
    pub, _ = webhooks.create("ci")
    action = _plugin(
        "write_memory",
        {
            "project_id": notebook,
            "title": "PR opened",
            "message": {"template_id": t["id"], "values": {}},
        },
        {"title": {"path": "pull_request.title", "type": "text"}},
    )
    aid = _made(_config(action, trigger={"kind": "webhook", "webhook_id": pub["id"]}))
    _deliver(aid, pub["id"], {**PR, "project_id": other})
    res = _execute(spawned[0])
    assert res["run"]["outcome"] == "ok", res["run"]
    assert "Fix totals" in json.dumps(_notes(notebook)) and _notes(other) == []
