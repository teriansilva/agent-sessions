"""The webhook address (`POST /hooks/w/{id}`, #2221) and a delivery's binding to the webhook it came
through, plus what is left of the retired review loop (#2202, removed in #2222).

Moved here from `tests/test_forge_review_loop.py` when the review loop was retired: the signature,
the route (delivery, header forms, test delivery, uniform 404, 413, kill switch, rate limit) and
the webhook-binding tests. They used a review loop as their automation; they now use a generic one
(`tell_hq`), since the route and the binding never depended on the action. Read
`docs/invariants/webhooks.md`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_sessions import automation_effect_lock as effect_lock
from agent_sessions import automation_runner, forge_hook, webhook_actions, webhooks
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions.routes import automations as routes
from agent_sessions.routes import hooks

#: The current test's list webhook: ``{"wid", "secret"}`` (set by `_world`).
WH: dict = {}

REPO = "example-org/shop"
SHA_A = "a" * 40


# ---- fixtures -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    monkeypatch.setattr(effect_lock, "WRITER_WAIT_S", 0.2)
    automation_runner._TASKS.clear()
    hooks.reset_for_tests()
    pub, secret = webhooks.create("shop repo")
    WH.update(wid=pub["id"], secret=secret)
    yield
    pending = [t for t in automation_runner._TASKS if not t.done()]
    automation_runner._TASKS.clear()
    assert not pending, f"run tasks still pending after the test: {pending}"


@pytest.fixture
def spawned(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(automation_runner, "spawn", lambda run, registry=None: out.append(run))
    return out


def _config(wid: str | None = None) -> dict:
    return {
        "name": "On a push",
        "trigger": {"kind": "webhook", "webhook_id": wid or WH["wid"]},
        "action": {"kind": "tell_hq", "message": {"text": "a push arrived"}},
    }


def _made(cfg: dict | None = None, *, enabled: bool = True) -> str:
    aid = routes._create(cfg or _config())["id"]
    if enabled:
        pub = routes.public(store.get(aid))
        routes._enable(
            aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
        )
    return aid


def _runs(aid: str) -> list[dict]:
    return store.list_runs(aid, limit=200)["runs"]


def _pr(number=41, sha=SHA_A, action="synchronized") -> dict:
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "state": "open",
            "title": "Fix checkout totals",
            "user": {"login": "marcus"},
            "head": {"sha": sha, "ref": "devopsagent/fix-totals"},
            "draft": False,
        },
        "repository": {"full_name": REPO, "html_url": f"https://git.example.com/{REPO}"},
        "sender": {"login": "marcus"},
    }


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _old_review_loop(*, enabled: bool = True) -> tuple[str, str]:
    """A #2202 review loop as an install upgraded from before #2222 still holds it: its stored
    config in the old form (`scheme` trigger, `forge_review_loop` action), approved, its legacy
    webhook (same id, with a secret) and rows in the v2 review tables. Built by SQL, because the
    model no longer accepts that config. Returns ``(automation id, secret)``."""
    aid = _made(_config(), enabled=enabled)
    old = {
        "name": "Review loop",
        "trigger": {"kind": "webhook", "scheme": "forgejo"},
        "action": {
            "kind": "forge_review_loop",
            "project_id": "shop",
            "repo": REPO,
            "reviewer": {"specialist_id": "shop-reviewer", "version": 3},
            "reviewer_login": "shop-reviewer-bot",
            "legs": {"issues": True, "pulls": True},
            "max_rounds": {"issue": 5, "pr": 4},
            "return_to_origin": True,
        },
        "policy": store.get(aid)["config"]["policy"],
    }
    ts = time.time()
    with store._tx() as con:
        con.execute("UPDATE automations SET config=? WHERE id=?", (json.dumps(old), aid))
        con.execute(
            "INSERT INTO webhooks (id, name, revision, legacy_automation, created_at, updated_at) "
            "VALUES (?,?,1,?,?,?)",
            (aid, "old review loop", aid, ts, ts),
        )
        secret = webhooks._seal(con, aid, ts)
        con.execute(
            "INSERT INTO automation_review_items (automation_id, item, reviews, updated_at) "
            "VALUES (?,?,?,?)",
            (aid, f"{REPO}#pr#41", 2, ts),
        )
        con.execute(
            "INSERT INTO automation_review_records (id, automation_id, item, revision, round, "
            "run_id, session_key, state, verdict, created_at, accepted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("rec1", aid, f"{REPO}#pr#41", SHA_A, 2, "run1", None, "dispatched", "", ts, None),
        )
    return aid, secret


# ---- the signature ------------------------------------------------------------------------------


def test_signature_is_hmac_sha256_of_the_raw_body_under_any_valid_secret():
    body = b'{"x": 1}'
    good = _sign(body, "s2")
    assert forge_hook.signature_ok(body, good, ["s1", "s2"])
    assert forge_hook.signature_ok(body, good.upper(), ["s2"])  # hex case is not identity
    assert not forge_hook.signature_ok(body, good, ["s1"])
    assert not forge_hook.signature_ok(body + b" ", good, ["s2"])
    assert not forge_hook.signature_ok(body, None, ["s2"])
    assert not forge_hook.signature_ok(body, "not-hex", ["s2"])
    assert not forge_hook.signature_ok(body, good, [])


# ---- the route ----------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    handled: list = []

    async def _nothing():
        return None

    def handle(aid, body, headers, registry=None, source=None):
        # Recorded at the CALL, which the route makes before it answers; the coroutine it
        # schedules may run after the response.
        handled.append((aid, body, headers))
        return _nothing()

    monkeypatch.setattr(webhook_actions, "handle", handle)
    app = FastAPI()
    hooks.register(app)
    with TestClient(app) as c:
        c.handled = handled
        yield c


def _post(c, wid, body: bytes, sig: str | None, *, retired: bool = False, header=None):
    headers = {"Content-Type": "application/json", "X-Forgejo-Event": "pull_request"}
    if sig is not None:
        name, prefix = header or ("X-Forgejo-Signature", "")
        headers[name] = prefix + sig
    base = "/hooks/a/" if retired else "/hooks/w/"
    return c.post(f"{base}{wid}", content=body, headers=headers)


def test_a_signed_delivery_reaches_every_enabled_automation_that_picked_the_webhook(client):
    a = _made()
    b = _made()
    off = _made(enabled=False)
    body = json.dumps(_pr()).encode()
    r = _post(client, WH["wid"], body, _sign(body, WH["secret"]))
    assert r.status_code == 202 and r.json() == {"accepted": True}
    assert r.headers["cache-control"] == "no-store"
    assert sorted(h[0] for h in client.handled) == sorted([a, b])
    assert off not in [h[0] for h in client.handled]


@pytest.mark.parametrize(
    "header",
    [("X-Forgejo-Signature", ""), ("X-Gitea-Signature", ""), ("X-Hub-Signature-256", "sha256=")],
)
def test_any_common_signature_header_works_no_provider_needed(client, header):
    _made()
    body = json.dumps(_pr()).encode()
    r = _post(client, WH["wid"], body, _sign(body, WH["secret"]), header=header)
    assert r.status_code == 202


def test_a_list_webhook_accepts_a_test_delivery_before_any_automation_uses_it(client):
    pub, secret = webhooks.create("new one")
    body = json.dumps(_pr()).encode()
    assert _post(client, pub["id"], body, _sign(body, secret)).status_code == 202
    assert client.handled == []
    seen = webhooks.get(pub["id"])
    assert seen["last_delivery_at"] is not None and seen["last_event"] == "pull_request"
    paths = {f["path"]: f["type"] for f in seen["last_shape"]}
    assert paths["pull_request.number"] == "number" and paths["pull_request.draft"] == "yes/no"
    # Names and types only: no value from the delivery is kept.
    assert "Fix checkout totals" not in json.dumps(seen) and SHA_A not in json.dumps(seen)


def test_every_refusal_is_the_same_404(client):
    _made()
    aid_old, secret_old = _old_review_loop()
    body = json.dumps(_pr()).encode()
    good = _sign(body, WH["secret"])
    answers = [
        _post(client, "doesnotexist", body, good),
        _post(client, "BAD-ID!", body, good),
        _post(client, WH["wid"], body, None),
        _post(client, WH["wid"], body, _sign(body, "wrong")),
        _post(client, WH["wid"], body + b" ", good),
        _post(client, aid_old, body, _sign(body, secret_old)),  # legacy isn't a list webhook
    ]
    assert {(r.status_code, r.text) for r in answers} == {(404, '{"detail":"Not Found"}')}
    assert client.handled == []
    # Bad signatures for an existing webhook are counted on it, never shown as runs.
    assert webhooks.get(WH["wid"])["unsigned_24h"] == 3


def test_the_retired_review_loop_address_is_the_uniform_404_for_every_id(client):
    """#2202's `/hooks/a/{id}` was retired with the review loop (#2222): a correctly signed
    delivery for an old, approved, enabled review loop is refused exactly like an unknown id, and
    nothing is read or handed on."""
    aid_old, secret_old = _old_review_loop()
    other = _made()
    body = json.dumps(_pr()).encode()
    answers = [
        _post(client, aid_old, body, _sign(body, secret_old), retired=True),
        _post(client, other, body, _sign(body, WH["secret"]), retired=True),
        _post(client, "doesnotexist", body, None, retired=True),
    ]
    assert {(r.status_code, r.text) for r in answers} == {(404, '{"detail":"Not Found"}')}
    assert all(r.headers["cache-control"] == "no-store" for r in answers)
    assert client.handled == []
    assert webhooks.get(aid_old)["last_delivery_at"] is None


def test_an_oversized_body_is_413_before_anything_is_looked_up(client):
    r = _post(client, WH["wid"], b"x" * (forge_hook.BODY_MAX + 1), "0" * 64)
    assert r.status_code == 413


def test_the_kill_switch_refuses_every_delivery(client, monkeypatch):
    _made()
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "0")
    body = json.dumps(_pr()).encode()
    assert _post(client, WH["wid"], body, _sign(body, WH["secret"])).status_code == 503
    assert client.handled == []


def test_deliveries_are_rate_limited_per_webhook_after_the_signature(client, monkeypatch):
    _made()
    monkeypatch.setattr(hooks._PER, "limit", 2)
    body = json.dumps(_pr()).encode()
    sig = _sign(body, WH["secret"])
    codes = [_post(client, WH["wid"], body, sig).status_code for _ in range(3)]
    assert codes == [202, 202, 429]


def test_a_webhook_in_use_cannot_be_deleted():
    _made()
    with pytest.raises(webhooks.WebhookError, match="used by an automation"):
        webhooks.delete(WH["wid"])
    pub, _s = webhooks.create("spare")
    webhooks.delete(pub["id"])
    with pytest.raises(store.NotFound):
        webhooks.get(pub["id"])


def test_the_consent_names_the_webhook_and_a_different_one_needs_consent():
    cfg = model.validate_config(_config())
    scope = model.scope_of(cfg, model.compute_pins(cfg))
    assert scope["webhook_source"] == WH["wid"]
    assert any("“shop repo”" in line for line in model.describe(scope))
    other, _s = webhooks.create("other")
    cfg2 = model.validate_config(_config(other["id"]))
    assert "a different webhook" in model.widened(
        scope, model.scope_of(cfg2, model.compute_pins(cfg2))
    )


def test_a_new_automation_cannot_use_the_old_per_automation_form():
    with pytest.raises(model.AutomationError):
        routes._create({**_config(), "trigger": {"kind": "webhook", "scheme": "forgejo"}})


def test_the_hook_route_binds_nothing_but_its_id(auth_cfg):
    """The unauthenticated routes read their body and headers themselves; FastAPI binds only the
    id, and no auth dependency sneaks in (they have none by design)."""
    from fastapi.dependencies.utils import get_flat_dependant

    from agent_sessions.main import create_app

    app = create_app(auth_cfg)
    found = [r for r in app.routes if getattr(r, "path", "").startswith("/hooks/")]
    assert sorted(r.path for r in found) == ["/hooks/a/{aid}", "/hooks/w/{wid}"]
    for r in found:
        flat = get_flat_dependant(r.dependant)
        assert not (
            flat.query_params or flat.body_params or flat.header_params or flat.cookie_params
        )
        assert len(flat.path_params) == 1 and r.methods == {"POST"}


# ---- what is left of a retired review loop (#2222) ----------------------------------------------


def test_an_old_review_loop_loads_unreadable_runs_nothing_and_deletes_cleanly(spawned):
    aid, secret = _old_review_loop()
    row = store.get(aid)
    # The model no longer knows the action (or the `scheme` trigger): it loads with no config.
    assert row["config"] is None
    with pytest.raises(model.AutomationError, match="can't be read"):
        routes._merged(row, {"name": "renamed"})
    # A delivery handed to it, through any webhook, starts and records nothing.
    body = json.dumps(_pr()).encode()
    for source in (aid, WH["wid"], None):
        asyncio.run(
            webhook_actions.handle(
                aid, body, {"x-forgejo-event": "pull_request"}, registry=None, source=source
            )
        )
    assert spawned == [] and _runs(aid) == []
    # Its legacy webhook is still readable, never pickable, and lists it as its user.
    pub = webhooks.get(aid)
    assert pub["legacy_automation"] == aid and not pub["pickable"]
    assert not webhooks.exists_pickable(aid)
    # Deleting it removes its legacy webhook, its secret and its review-table rows too.
    assert webhooks.secrets(aid) == [secret]
    store.delete(aid, expect_revision=row["revision"])
    with pytest.raises(store.NotFound):
        store.get(aid)
    with pytest.raises(store.NotFound):
        webhooks.get(aid)
    assert webhooks.secrets(aid) == []
    with store._read() as con:
        for table in ("automation_review_items", "automation_review_records"):
            n = con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE automation_id=?",  # noqa: S608
                (aid,),
            ).fetchone()[0]
            assert n == 0, table
    # A shared webhook it never used is untouched.
    assert webhooks.get(WH["wid"])["id"] == WH["wid"]


# ---- the delivery is bound to the webhook it came through (Hermes on #2225) ----------------------


def _switch_to(aid, wid):
    """Move automation ``aid`` to webhook ``wid`` and approve that, as the operator would."""
    pub = routes.public(store.get(aid))
    trig = {"kind": "webhook", "webhook_id": wid}
    cfg = model.validate_config({**_config(), "trigger": trig})
    scope = model.scope_of(cfg, model.compute_pins(cfg))
    routes._patch(
        aid,
        {
            "revision": pub["revision"],
            "trigger": trig,
            "consent": True,
            "scope_digest": routes.scope_digest(scope),
        },
    )


def test_a_delivery_never_runs_under_a_webhook_it_did_not_come_through(spawned):
    aid = _made()
    old = WH["wid"]
    new, _s = webhooks.create("other")
    # Routed to `aid` through `old`; before the handler runs, the operator moves `aid` to `new`
    # and approves it.
    _switch_to(aid, new["id"])
    body = json.dumps(_pr()).encode()
    asyncio.run(webhook_actions.handle(aid, body, {}, registry=None, source=old))
    assert spawned == [] and _runs(aid) == []
    # Through the webhook it now listens on, the same delivery runs.
    asyncio.run(webhook_actions.handle(aid, body, {}, registry=None, source=new["id"]))
    assert len(spawned) == 1


def test_admission_refuses_a_delivery_from_another_webhook():
    aid = _made()
    other, _s = webhooks.create("other")
    res = store.begin_delivery(
        aid,
        slot="delivery:x",
        expect_revision=store.get(aid)["revision"],
        inputs={},
        source=other["id"],
    )
    assert res.get("stale_config") and res["run"] is None
    assert (
        store.record_delivery(
            aid, slot="rejected:x", outcome="refused", reason="r", inputs={}, source=other["id"]
        )
        is None
    )
    assert _runs(aid) == []


def test_a_webhook_and_a_reference_to_it_can_never_dangle():
    gone, _s = webhooks.create("gone")
    webhooks.delete(gone["id"])
    cfg = model.validate_config(_config(gone["id"]))
    # Created (or retargeted) after the deletion: refused inside the writing transaction.
    with pytest.raises(store.StoreError, match="doesn't exist"):
        store.create(cfg, model.compute_pins(cfg))
    aid = _made()
    with pytest.raises(store.StoreError, match="doesn't exist"):
        store.mutate(aid, lambda row: {"config": cfg})
    # Deleted after a reference exists: refused inside the deleting transaction.
    with pytest.raises(webhooks.WebhookError, match="used by an automation"):
        webhooks.delete(WH["wid"])


def test_a_rejected_delivery_racing_a_webhook_switch_charges_nothing(monkeypatch):
    """The switch lands between the handler's reads and the recording transaction: no claim, no
    run and no failure accounting under the newly approved webhook (Hermes on #2225)."""
    aid = _made()
    old = WH["wid"]
    new, _s = webhooks.create("other")
    real_tx = store._tx
    switched = {"done": False}

    def tx_then_switch():
        if not switched["done"]:
            switched["done"] = True
            _switch_to(aid, new["id"])  # commits before the recording transaction starts
        return real_tx()

    monkeypatch.setattr(store, "_tx", tx_then_switch)
    before = store.get(aid)["consecutive_failures"]
    got = store.record_delivery(
        aid, slot="rejected:race", outcome="refused", reason="rejected: x", inputs={}, source=old
    )
    monkeypatch.setattr(store, "_tx", real_tx)
    assert got is None and switched["done"]
    assert _runs(aid) == []
    assert store.get(aid)["consecutive_failures"] == before
    with store._read() as con:
        assert not con.execute(
            "SELECT 1 FROM automation_claims WHERE automation_id=? AND slot=?",
            (aid, "rejected:race"),
        ).fetchone()
