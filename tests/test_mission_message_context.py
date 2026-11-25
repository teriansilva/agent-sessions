"""`/message` and `/context` route contracts (#852, Phase 2a of #840).

The store-level turn tests live in `test_mission_turns.py`; these pin what the ROUTES guarantee —
auth, CSRF, the idempotency answers a client actually receives, and the security envelope
`/context` does **not** inherit by being adjacent to the file panel.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_sessions import missions
from agent_sessions.main import create_app


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """These tests are about the ROUTE, not about whether an AI endpoint is configured.

    `/message` preflights configuration before writing the operator event (so an unconfigured
    endpoint cannot leave an orphan), and these tests patch `ask` rather than standing up a real
    endpoint — so without this they would all 409 on a condition none of them is testing. The
    dedicated unconfigured test overrides this deliberately.
    """
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes.review, "_require_config", lambda: {})


@pytest.fixture
def mission(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    return missions.create_mission("do it")["id"]


# --- auth + CSRF ------------------------------------------------------------------------------


def test_message_requires_login(auth_cfg, mission):
    c = _client(auth_cfg)
    assert c.post(f"/api/missions/{mission}/message", json={}).status_code == 401


def test_message_requires_csrf(auth_cfg, mission):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "hi", "turn_id": "t1"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_context_requires_login(auth_cfg, mission):
    c = _client(auth_cfg)
    assert c.get(f"/api/missions/{mission}/context").status_code == 401


# --- request shape ----------------------------------------------------------------------------


def test_message_rejects_an_unparseable_body_before_mutating(auth_cfg, mission):
    """422 before anything happens — the defaults on this surface are the effectful ones."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        content=b'{"message": "hi"',
        headers={
            "X-CSRF-Token": csrf,
            "Origin": auth_cfg.origin,
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 422
    assert missions.unresolved_turn_keys() == set(), "no claim may be taken on a bad request"


@pytest.mark.parametrize(
    "body", [{"message": "hi"}, {"turn_id": "t1"}, {"message": "  ", "turn_id": "t1"}]
)
def test_message_requires_both_a_message_and_a_turn_id(auth_cfg, mission, body):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json=body,
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_the_same_turn_id_with_different_text_is_refused_not_replayed(auth_cfg, mission):
    """A used key on new content is a different turn. Replaying would answer a question nobody
    asked — and would report success for an instruction that was never sent."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    missions.claim_turn(mission, "t1", "sha-of-something-else")
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "a different message", "turn_id": "t1"},
        headers=hdr,
    )
    assert r.status_code == 422
    assert "already used" in r.json()["detail"]


def test_a_replay_while_the_turn_is_live_answers_in_progress_and_calls_nothing(
    auth_cfg, mission, monkeypatch
):
    """202 in-progress, and crucially NOT a second model call."""
    from agent_sessions.routes import missions as mroutes

    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("the model must not be called for a live replay")

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", boom)
    import hashlib

    missions.claim_turn(mission, "t1", hashlib.sha256(b"hi").hexdigest())

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "hi", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 202
    assert r.json()["state"] == "in_progress"
    assert called["n"] == 0


# --- /context: the envelope it does NOT inherit -----------------------------------------------


def test_context_is_never_cacheable_on_success(auth_cfg, mission, tmp_path):
    """Git status carries absolute paths. The no-store middleware is gated on the `/api/files/`
    and `/api/git/` prefixes, so this route inherits nothing by being adjacent."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/missions/{mission}/context")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


def test_context_is_never_cacheable_on_an_auth_failure(auth_cfg, mission):
    """`Depends(logged_in)` raises its 401 BEFORE any handler runs — the escape the middleware's
    own docstring records having to close, reopened by any route under a new prefix."""
    c = _client(auth_cfg)
    r = c.get(f"/api/missions/{mission}/context")
    assert r.status_code == 401
    assert r.headers.get("cache-control") == "no-store"


def test_context_takes_no_path_from_the_client(auth_cfg, mission):
    """The cwd comes from the mission row and nowhere else, so there is nothing to traverse with.
    A path query parameter must be inert, not merely validated."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/missions/{mission}/context", params={"path": "../../etc"})
    assert r.status_code == 200
    body = r.json()
    assert body["cwd"] == "" or "etc" not in body["cwd"]


def test_context_fails_closed_when_git_is_unavailable_and_leaks_no_path(
    auth_cfg, tmp_path, monkeypatch
):
    """A named failure KIND, never the path that caused it.

    `cwd` itself is a deliberate part of this response (§14) — the property is that a failure
    does not smuggle additional filesystem detail out through the error, which is how an
    exception message leaks a path the response was never meant to carry.
    """
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    secret = tmp_path / "very-secret-dir"
    secret.mkdir()
    mid = missions.create_mission("x", project_id="p", cwd=str(secret))["id"]

    from agent_sessions.routes import missions as mroutes

    def boom(*a, **k):
        raise RuntimeError(f"failed reading {secret}")

    monkeypatch.setattr(mroutes.gitpanel, "git_status", boom)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/missions/{mid}/context")
    assert r.status_code == 200
    assert r.json()["git"] is None
    assert r.json()["git_error"] == "RuntimeError"
    assert str(secret) not in r.json()["git_error"]
    assert "failed reading" not in r.text, "the exception message must not reach the client"


# --- round 1 of #862 review: the happy path, which nothing exercised end-to-end ----------------


def _fake_ask(answer="the answer", intent="instruct", actions=()):
    """A stand-in for `orchestrator_chat.ask` that RESERVES like the real one does.

    The real `ask` calls `reserve_write` with the ids it is about to append — that receipt is
    what recovery reads, and since #881 it is also where an in-progress response gets the
    actions to show. A fake that skips it makes the turn's receipt empty, so the same turn
    answered with actions on the first call and without them on the next, and the difference
    was the fixture rather than the code (a fixture kinder than the producer tests nothing).
    """

    async def ask(text, history=None, *, reserve_write=None, **kw):
        ids = [str(a.get("id")) for a in actions if a.get("id")]
        if ids and reserve_write is not None:
            reserve_write(ids)
        return {"intent": intent, "answer": answer, "actions": list(actions)}

    return ask


def test_a_completed_turn_returns_the_models_answer(auth_cfg, mission, monkeypatch):
    """`orchestrator_chat.ask` returns `answer`. Reading `reply` made every successful turn
    respond `null` — and no test caught it, because they all drove the claim machinery and never
    a completed call."""
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _fake_ask())
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "hi", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "the answer"
    assert body["state"] == "done"
    assert body["intent"] == "instruct"


def test_a_replay_returns_the_same_shape_as_the_first_answer(auth_cfg, mission, monkeypatch):
    """ "Byte-identical replay" is only true if the replay carries the same FIELDS — a caller must
    not have to know whether it was the first to ask."""
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _fake_ask())
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    payload = {"message": "hi", "turn_id": "t1"}
    first = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr).json()

    # …and the model must not be called a second time.
    async def boom(*a, **k):
        raise AssertionError("a replay must not call the model")

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", boom)
    again = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr).json()

    # FULL equality, with nothing permitted to differ. My previous version wrote
    # `{**first, "replayed": True}`, which does not test the contract — it DOCUMENTS the
    # violation and passes. Both paths now return the stored record itself, so they are identical
    # by construction rather than by keeping two builders in step.
    assert again == first


def test_a_turn_lands_in_the_mission_timeline(auth_cfg, mission, monkeypatch):
    """The whole point of `/message` is that chat turns become mission events. The idempotency
    machinery was built and the actual feature was not — the turn settled and the timeline stayed
    empty."""
    from agent_sessions import missions as mstore
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _fake_ask(answer="done that"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    c.post(
        f"/api/missions/{mission}/message",
        json={"message": "please do it", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    kinds = [e["kind"] for e in mstore.get_mission(mission)["events"]]
    assert kinds.count("operator_msg") >= 1
    assert "assistant_msg" in kinds


def test_an_unknown_mission_is_a_404_on_both_routes(auth_cfg, tmp_path, monkeypatch):
    """A foreign-key failure surfacing as 500 tells the caller "we broke" about a request that was
    merely wrong; a fabricated 200 renders an empty console for a thing that never existed."""
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    ghost = "msn_" + "0" * 32
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)

    r = c.post(
        f"/api/missions/{ghost}/message",
        json={"message": "hi", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 404, r.text

    r2 = c.get(f"/api/missions/{ghost}/context")
    assert r2.status_code == 404, r2.text
    assert r2.headers.get("cache-control") == "no-store", "errors stay uncacheable too"


# --- round 5 of #862 review ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [{"message": {"a": 1}, "turn_id": "t1"}, {"message": 42, "turn_id": "t1"}]
)
def test_a_non_string_message_is_refused_before_anything_is_claimed(auth_cfg, mission, bad):
    """`str(...)` on arbitrary JSON quietly turned a dict into its repr and sent it to the model."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json=bad,
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422
    assert missions.unresolved_turn_keys() == set(), "no claim may be taken on a refused request"


def test_an_oversized_message_is_refused_like_the_sibling_route(auth_cfg, mission):
    """Same bound the orchestrator chat route enforces. Unbounded operator text would otherwise
    become an unbounded DURABLE mission event, and reach the model on the way."""
    from agent_sessions import orchestrator_chat

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "x" * (orchestrator_chat.QUERY_MAX + 1), "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422
    assert "too long" in r.json()["detail"]
    assert missions.unresolved_turn_keys() == set()


def test_an_instruction_is_delivered_the_way_the_sibling_route_delivers_it(
    auth_cfg, mission, monkeypatch
):
    """Under yolo a chat instruction produces `approved` records exactly as a pass does, so it
    must be DELIVERED exactly as a pass does. Without this the turn reports success while the
    instruction merely sits live in the ledger, blocking that session until expiry."""
    from agent_sessions.routes import missions as mroutes

    delivered: list = []

    async def fake_deliver(actions, registry=None):
        delivered.append([a.get("id") for a in actions])

    monkeypatch.setattr(mroutes.actuator, "deliver_pass_actions", fake_deliver)
    monkeypatch.setattr(
        mroutes.orchestrator_chat,
        "ask",
        _fake_ask(answer="on it", intent="instruct", actions=[{"id": "act-1"}]),
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "do the thing", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200, r.text
    assert delivered == [["act-1"]], "an approved instruction was never handed to the actuator"


def test_a_read_only_question_never_dispatches(auth_cfg, mission, monkeypatch):
    """`ask()` overloads `actions`: for `history` it holds recent LEDGER ROWS shown for audit.
    Dispatching unconditionally would let a question type an old approved row into a live
    session under yolo."""
    from agent_sessions.routes import missions as mroutes

    delivered: list = []

    async def fake_deliver(actions, registry=None):
        delivered.append(actions)

    monkeypatch.setattr(mroutes.actuator, "deliver_pass_actions", fake_deliver)
    monkeypatch.setattr(
        mroutes.orchestrator_chat,
        "ask",
        _fake_ask(answer="here is what I did", intent="history", actions=[{"id": "old-act"}]),
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    c.post(
        f"/api/missions/{mission}/message",
        json={"message": "what did you do?", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert delivered == [], "a read-only question caused a write"


def test_message_and_pulse_chat_share_one_single_flight(auth_cfg, mission, monkeypatch):
    """Two unfenced paths to the actuator would mean two sets of guards, and the newer one is
    always the weaker. `/message` takes the SAME flight kind the sibling chat route takes, so a
    mission turn cannot interleave with a Pulse turn or with the delivery of what it approved."""
    from agent_sessions import aitasks
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _fake_ask())
    seen: list = []

    real = aitasks.single_flight

    def spy(kind, label):
        seen.append((kind, label))
        return real(kind, label)

    monkeypatch.setattr(mroutes.aitasks, "single_flight", spy)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    c.post(
        f"/api/missions/{mission}/message",
        json={"message": "hi", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert ("pulse-chat", "orchestrate") in seen, "the mission turn ran outside the chat flight"


def test_a_busy_flight_is_refused_before_anything_durable_is_written(
    auth_cfg, mission, monkeypatch
):
    """Another turn holding the flight is a transient condition of the SYSTEM, not an outcome of
    this turn — no model call happened.

    THE COMMON CASE: the flight is busy when the request arrives, so the route says so BEFORE it
    claims anything. Nothing durable exists, so nothing is consumed and nothing is left behind,
    and the same `turn_id` works once the flight clears.

    That ordering is what makes the operator event living inside the claim transaction safe:
    with the transient conditions checked first, there is nothing yet for a release to be
    inconsistent with — and so no release is needed (see the sibling test for the race that gets
    past this check).
    """
    from agent_sessions.routes import missions as mroutes

    busy = {"now": True}
    monkeypatch.setattr(mroutes.aitasks, "is_running", lambda kind: busy["now"])
    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _fake_ask(answer="second time"))

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    payload = {"message": "hi", "turn_id": "t1"}

    before = [e["kind"] for e in missions.get_mission(mission)["events"]].count("operator_msg")
    r = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr)
    assert r.status_code == 409, r.text
    assert missions.get_turn(mission, "t1") is None, "a transient 409 consumed the turn id"
    mid = [e["kind"] for e in missions.get_mission(mission)["events"]].count("operator_msg")
    assert mid == before, "a 409 before the claim left a message in the timeline"

    # …and the SAME turn_id works once the condition clears.
    busy["now"] = False
    again = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr)
    assert again.status_code == 200, again.text
    assert again.json()["answer"] == "second time"

    # EXACTLY ONE operator event across the whole 409 → retry sequence.
    after = [e["kind"] for e in missions.get_mission(mission)["events"]].count("operator_msg")
    assert after == before + 1, f"the operator message was recorded {after - before} times"


def test_a_flight_that_goes_busy_AFTER_the_check_settles_forward(auth_cfg, mission, monkeypatch):
    """The narrow race past the early-out: free when the request checked, busy when `ask` got
    there. The operator's message is already in the timeline by then, so the turn SETTLES rather
    than releasing.

    This is the half of #871 decision 3 that matters. Releasing here would delete the claim and
    leave the message behind with nothing owning it — the "released the claim but not the event"
    state that produced five consecutive fix-caused-the-next-defect rounds on #852. The cost is
    that this `turn_id` is spent; the benefit is that there is no inconsistent state to reach.
    """
    from agent_sessions import aitasks
    from agent_sessions.routes import missions as mroutes

    def busy(kind, label):
        raise aitasks.AlreadyRunning(kind)

    # The early-out says free; the flight itself says busy. That IS the race.
    monkeypatch.setattr(mroutes.aitasks, "is_running", lambda kind: False)
    monkeypatch.setattr(mroutes.aitasks, "single_flight", busy)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    payload = {"message": "hi", "turn_id": "t1"}

    before = [e["kind"] for e in missions.get_mission(mission)["events"]].count("operator_msg")
    r = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr)
    assert r.status_code == 409, r.text

    # SETTLED, not released and not left running: the turn owns the message it wrote.
    row = missions.get_turn(mission, "t1")
    assert row is not None, "the claim was released, orphaning the operator message"
    assert row["state"] != "in_progress", "a failed turn was left in flight"

    # …exactly one message, and no second copy from any retry path.
    after = [e["kind"] for e in missions.get_mission(mission)["events"]].count("operator_msg")
    assert after == before + 1, f"the operator message was recorded {after - before} times"

    # …and a retry of the SAME id replays that outcome rather than reaching the model again.
    again = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr)
    assert again.status_code == 200, again.text
    assert again.json()["state"] != "in_progress"
    final = [e["kind"] for e in missions.get_mission(mission)["events"]].count("operator_msg")
    assert final == before + 1, "the replay appended a second copy of the message"


def test_reported_actions_are_re_read_from_the_ledger_after_delivery(
    auth_cfg, mission, monkeypatch
):
    """`deliver_pass_actions` omits an action another caller already claimed or settled, so the
    helper's own list can be stale. Reporting it would tell the operator a tap is still needed
    for something already delivered — the ledger is the authority on state."""
    from agent_sessions import orchestrator_ledger as ledger
    from agent_sessions.routes import missions as mroutes

    async def noop_deliver(actions, registry=None):
        return None

    monkeypatch.setattr(mroutes.actuator, "deliver_pass_actions", noop_deliver)
    monkeypatch.setattr(
        mroutes.orchestrator_chat,
        "ask",
        _fake_ask(intent="instruct", actions=[{"id": "act-1", "state": "approved"}]),
    )
    # The ledger says it has since been delivered.
    ledger.append(
        {"id": "act-1", "state": "approved", "verb": "continue", "session_id": "claude:z"}
    )
    ledger.transition("act-1", "delivered")

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    c.post(
        f"/api/missions/{mission}/message",
        json={"message": "do it", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    seq = missions.get_turn(mission, "t1")
    assert seq["state"] == "done"


def test_a_delivery_failure_is_not_reported_as_a_delivered_instruction(
    auth_cfg, mission, monkeypatch
):
    """Delivery failure is its OWN outcome, not a crash after the ledger write.

    Letting it fall into the generic handler made it look like a completed write, which settles
    `done` from the mere presence of the action — reporting success for an instruction still
    sitting `approved` and undelivered.
    """
    from agent_sessions import orchestrator_ledger as ledger
    from agent_sessions.routes import missions as mroutes

    async def boom(actions, registry=None):
        raise RuntimeError("the pty went away")

    monkeypatch.setattr(mroutes.actuator, "deliver_pass_actions", boom)
    monkeypatch.setattr(
        mroutes.orchestrator_chat,
        "ask",
        _fake_ask(intent="instruct", actions=[{"id": "act-1"}]),
    )
    ledger.append(
        {"id": "act-1", "state": "approved", "verb": "continue", "session_id": "claude:z"}
    )

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "do it", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200, r.text
    # The action's real state is the ledger's, and the response must show it rather than imply
    # delivery: `approved` is in flight and still needs a tap.
    act = r.json()["actions"][0]
    assert act["state"] == "approved"
    assert act["projection"] == "in_flight_revocable"
    assert act["can_approve"] is False and act["can_reject"] is True


def test_mission_responses_carry_the_shared_action_projection(auth_cfg, mission, monkeypatch):
    """The mission response is the THIRD producer #852 names, and it was returning bare ids —
    leaving the console to re-derive controls from a state field, which is the drift the shared
    helper exists to end."""
    from agent_sessions import orchestrator_ledger as ledger
    from agent_sessions.routes import missions as mroutes

    async def noop(actions, registry=None):
        return None

    monkeypatch.setattr(mroutes.actuator, "deliver_pass_actions", noop)
    monkeypatch.setattr(
        mroutes.orchestrator_chat,
        "ask",
        _fake_ask(intent="instruct", actions=[{"id": "act-live"}, {"id": "act-gone"}]),
    )
    ledger.append(
        {"id": "act-live", "state": "proposed", "verb": "continue", "session_id": "claude:z"}
    )

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    body = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "do it", "turn_id": "t1"},
        headers=hdr,
    ).json()

    # The turn is IN PROGRESS, because `act-live` is still live — decision 2, enforced on the
    # completion path as well as in the reconciler (#881). The projections ride on it anyway:
    # "still running" with nothing to look at tells the operator nothing about what is waiting.
    assert body["state"] == "in_progress"
    by_id = {a["id"]: a for a in body["actions"]}
    assert by_id["act-live"]["projection"] == "actionable"
    assert by_id["act-live"]["can_approve"] is True
    # An id the ledger no longer holds asserts NO outcome — inventing one would be worse than
    # saying nothing.
    assert by_id["act-gone"]["projection"] == "historical"
    assert by_id["act-gone"]["state"] is None

    # Asking again reconciles the parked turn. `act-gone` was RESERVED and never appeared, which
    # is honestly `indeterminate` — an append that may or may not have landed — and the answer
    # says so rather than claiming an outcome.
    again = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "do it", "turn_id": "t1"},
        headers=hdr,
    ).json()
    assert again["state"] == "indeterminate"
    assert "missing from the ledger" in (again["answer"] or "")

    # …and THAT answer is stored, so a further replay is byte-identical.
    third = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "do it", "turn_id": "t1"},
        headers=hdr,
    ).json()
    assert third == again, "the settled answer drifted"


def test_a_failed_delivery_is_visible_on_the_first_answer_and_on_replay(
    auth_cfg, mission, monkeypatch
):
    """An outcome the operator cannot see is not an outcome that was reported.

    Storing the failure only in `result_meta` meant the turn read `done`, the action stayed
    `approved`, and nothing in either response said why.
    """
    from agent_sessions import orchestrator_ledger as ledger
    from agent_sessions.routes import missions as mroutes

    async def boom(actions, registry=None):
        raise RuntimeError("the pty went away")

    monkeypatch.setattr(mroutes.actuator, "deliver_pass_actions", boom)
    monkeypatch.setattr(
        mroutes.orchestrator_chat,
        "ask",
        _fake_ask(intent="instruct", actions=[{"id": "act-1"}]),
    )
    ledger.append(
        {
            "id": "act-1",
            "state": "approved",
            "verb": "continue",
            "session_id": "claude:z",
            # PROVENANCE, as a real append through the turn's reservation carries it. Without it
            # recovery cannot match the action to its turn and correctly concludes the reserved
            # write never landed.
            "turn_id": "t1",
            "mission_id": mission,
        }
    )

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    payload = {"message": "do it", "turn_id": "t1"}

    first = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr).json()
    assert first["delivery_error"] == "RuntimeError"
    assert first["actions"][0]["state"] == "approved", "still undelivered, and it must say so"

    # The turn stays OPEN because the action is still `approved` (decision 2), and the failure
    # survives on the reply that follows — `settle_turn` used to be the only thing that stored
    # `delivery_error`, so a turn held open would have reported it once and lost it.
    replay = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr).json()
    assert (
        replay["delivery_error"] == "RuntimeError"
    ), "the failure must survive the replay, not just the first answer"
    assert replay["actions"][0]["state"] == "approved"


def test_an_archived_mission_refuses_a_turn(auth_cfg, mission, monkeypatch):
    """A turn is an ordinary mutation — it writes the timeline and can issue a live instruction —
    so it takes the same lifecycle fence every other mutation takes.

    Without it an archived mission could mutate its sensitive timeline and reach the actuator with
    no unarchive predecessor, while racing retention's deletion of that same record.
    """
    from agent_sessions.routes import missions as mroutes

    called = {"n": 0}

    async def never(*a, **k):
        called["n"] += 1
        raise AssertionError("an archived mission must not reach the model")

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", never)
    missions.set_state(mission, "draft", "abandoned")
    import asyncio as _aio

    from agent_sessions import mission_archive

    _aio.run(mission_archive.archive_mission(mission))

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "hi", "turn_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 409, r.text
    assert "archived" in r.json()["detail"]
    assert called["n"] == 0
    assert missions.get_turn(mission, "t1") is None


def test_an_unconfigured_endpoint_leaves_no_orphan_event(auth_cfg, mission, monkeypatch):
    """Every transient precondition settles BEFORE the event is written, not just the flight.

    `ask` checks configuration internally and raises from inside — well after the append — so an
    unconfigured endpoint left an orphan `operator_msg` and the retry appended a second one, each
    eviction costing a retained recap at the cap.
    """
    from agent_sessions import review
    from agent_sessions.routes import missions as mroutes

    def unconfigured():
        raise review.NotConfiguredError("no endpoint")

    monkeypatch.setattr(mroutes.review, "_require_config", unconfigured)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    before = [e["kind"] for e in missions.get_mission(mission)["events"]].count("operator_msg")

    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "hi", "turn_id": "t1"},
        headers=hdr,
    )
    assert r.status_code == 409, r.text
    after = [e["kind"] for e in missions.get_mission(mission)["events"]].count("operator_msg")
    assert after == before, "an unconfigured 409 left an orphan operator event"
    assert missions.get_turn(mission, "t1") is None


def test_a_replay_does_not_drift_when_an_action_moves_on(auth_cfg, mission, monkeypatch):
    """The stored answer is a SNAPSHOT, not a live re-read.

    Hydrating from the current ledger made "identical replay" false the moment an action moved:
    the same stored turn answered `in_flight_revocable` and later `settled`. A stored answer that
    changes is not a stored answer — this is the same immutable-projection shape Phase 1 uses for
    decision events.
    """
    from agent_sessions import orchestrator_ledger as ledger
    from agent_sessions.routes import missions as mroutes

    async def noop(actions, registry=None):
        return None

    monkeypatch.setattr(mroutes.actuator, "deliver_pass_actions", noop)
    monkeypatch.setattr(
        mroutes.orchestrator_chat,
        "ask",
        _fake_ask(intent="instruct", actions=[{"id": "act-1"}]),
    )
    ledger.append(
        {
            "id": "act-1",
            "state": "approved",
            "verb": "continue",
            "session_id": "claude:z",
            # PROVENANCE, as a real append through the turn's reservation carries it. Without it
            # recovery cannot match the action to its turn and correctly concludes the reserved
            # write never landed.
            "turn_id": "t1",
            "mission_id": mission,
        }
    )

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    payload = {"message": "do it", "turn_id": "t1"}

    # While the action is LIVE the turn stays open (decision 2), and an open turn deliberately
    # reports the CURRENT state — there is no stored answer to drift yet.
    live = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr).json()
    assert live["state"] == "in_progress"
    assert live["actions"][0]["projection"] == "in_flight_revocable"

    # The action finishes, so the next call reconciles the parked turn and FREEZES its snapshot.
    ledger.transition("act-1", "delivered")
    settled = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr).json()
    assert settled["state"] != "in_progress"
    assert settled["actions"][0]["projection"] == "settled"

    # …and now the world moves again. The stored answer must not follow it: a replay that
    # changes is not a stored answer.
    ledger.transition("act-1", "observed")
    again = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr).json()
    assert again == settled, "the stored answer drifted with the ledger"


def test_a_SETTLED_turn_is_replayable_while_a_flight_is_running(auth_cfg, mission, monkeypatch):
    """Reading back a stored answer is a database read. It needs no model call, so it must not be
    refused because a model call is happening elsewhere.

    The transient early-outs (#871 decision 3, corrected) originally sat ahead of `claim_turn`
    unconditionally — and `claim_turn` is what detects a replay, so a settled turn stopped being
    readable the moment any flight was in progress. The kind of defect that looks fine until
    someone is actually using the feature.
    """
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _fake_ask(answer="first answer"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    payload = {"message": "hi", "turn_id": "t1"}

    first = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr)
    assert first.status_code == 200, first.text
    assert first.json()["answer"] == "first answer"

    # …now a flight is running. The replay is still a replay.
    monkeypatch.setattr(mroutes.aitasks, "is_running", lambda kind: True)
    again = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr)
    assert again.status_code == 200, again.text
    assert again.json()["answer"] == "first answer"


def test_a_SETTLED_turn_is_replayable_with_no_configured_endpoint(auth_cfg, mission, monkeypatch):
    """The same rule from the other side: an operator whose endpoint broke must still be able to
    read the answers they already have."""
    from agent_sessions import review
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _fake_ask(answer="first answer"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    payload = {"message": "hi", "turn_id": "t1"}
    assert c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr).status_code == 200

    def unconfigured():
        raise review.NotConfiguredError("no endpoint")

    monkeypatch.setattr(mroutes.review, "_require_config", unconfigured)
    again = c.post(f"/api/missions/{mission}/message", json=payload, headers=hdr)
    assert again.status_code == 200, again.text
    assert again.json()["answer"] == "first answer"


def test_a_REUSED_turn_id_still_says_what_is_actually_wrong(auth_cfg, mission, monkeypatch):
    """422, not 409. The same key with different text is a caller error, and a transient 409
    masking it sends the operator to look at the wrong thing entirely."""
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes.orchestrator_chat, "ask", _fake_ask(answer="first answer"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert (
        c.post(
            f"/api/missions/{mission}/message",
            json={"message": "hi", "turn_id": "t1"},
            headers=hdr,
        ).status_code
        == 200
    )

    monkeypatch.setattr(mroutes.aitasks, "is_running", lambda kind: True)
    r = c.post(
        f"/api/missions/{mission}/message",
        json={"message": "DIFFERENT text", "turn_id": "t1"},
        headers=hdr,
    )
    assert r.status_code == 422, r.text
