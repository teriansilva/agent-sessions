"""HQ's Dispatches (#1837): the issue's acceptance tests 1 and 3–10 (test 2, a model failure, is
parametrized in ``test_hq_dispatch.py``).

Where the property is about PROCESSES — two workers, a kill between two writes — the test uses real
child processes and SIGKILL (``hq_dispatch_harness.spawn``), and every model request goes to one
REAL loopback endpoint that counts requests from all of them. A same-process test could not prove
"one claim across processes" or "a kill leaves the record whole": that is what the flock and the
create-only claim are for.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import hq_dispatch_harness as H
from agent_sessions import automation_runner, hq_dispatch, notifications, prefs
from test_hq_dispatch import (
    announce_automation,
    announce_escalation,
    announce_needs_you,
    request_entries,
)


@pytest.fixture
def hq():
    e = H.FakeEndpoint()
    H.set_up_hq(e.base)
    yield e
    e.close()


def run_pass(now: float) -> dict:
    return asyncio.run(hq_dispatch.pass_once(now=now))


def doc() -> dict:
    return hq_dispatch._read_strict()


def joined() -> dict:
    """The memberships, or none when the store does not exist yet."""
    return doc()["member_of"] if hq_dispatch._path().exists() else {}


def rows() -> list[dict]:
    return notifications._read(notifications._notifications_path())


def _subs(tmp_path):
    """One stand-in device: the real `fanout` reads it and builds the real payload; only the
    sender (`_send_all`) is captured."""
    p = notifications._subs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([{"id": "d", "endpoint": "https://push.example/x", "keys": {}}]))


def _capture_pushes(monkeypatch, log: list):
    monkeypatch.setattr(
        notifications, "_send_all", lambda payload, *a, **k: log.append(("push", payload)) or {}
    )


# ---- 1. the row (and push, where the producer pushes) comes first, without the model ------------


@pytest.mark.parametrize("producer", ["needs-you", "automation"])
def test_1_the_row_and_push_come_first_without_the_model(producer, hq, monkeypatch, tmp_path):
    hq.hang = True  # the endpoint is hanging: it never answers
    monkeypatch.setattr(hq_dispatch, "CALL_CAP_S", 0.5)
    _subs(tmp_path)
    log: list = []
    _capture_pushes(monkeypatch, log)
    if producer == "needs-you":
        announce_needs_you(monkeypatch)
        keys = {"title", "body", "url", "tag"}
    else:
        announce_automation()
        keys = {"title", "body", "url"}
    # Before any model request: the row with its fixed text, and the push.
    assert hq.count() == 0
    (row,) = rows()
    assert row["title"] in ("the session's title", "Automation failed: nightly docs")
    (push,) = [p for kind, p in log if kind == "push"]
    assert set(json.loads(push)) == keys
    assert "line" not in push.decode() and "HQ" not in json.loads(push)["title"]
    # The model is asked afterwards, and never touches either.
    run_pass(time.time() + 61)
    assert hq.count() == 1
    assert rows() == [row] and len(log) == 1


def test_1_a_mission_escalation_writes_its_row_first_and_sends_no_push(hq, monkeypatch, tmp_path):
    hq.hang = True
    _subs(tmp_path)
    fanouts: list = []
    monkeypatch.setattr(notifications, "fanout", lambda *a, **k: fanouts.append(a))
    announce_escalation()
    assert hq.count() == 0
    (row,) = rows()
    assert row["escalation"] is True and row["reason"] == "mission supervisor"
    assert fanouts == []


# ---- 3. concurrent workers and a kill -----------------------------------------------------------


def test_3_two_worker_processes_on_one_sealed_bundle_make_one_claim_and_one_call(hq, tmp_path):
    announce_automation()
    now = str(time.time() + 61)
    # Both workers are lined up AFTER housekeeping and before either claims, then let go at once.
    a = H.spawn(tmp_path, H.PASS, ["gate:sealed"], now)
    b = H.spawn(tmp_path, H.PASS, ["gate:sealed"], now)
    assert H.wait_at(a) == "sealed#1" and H.wait_at(b) == "sealed#1"
    H.gate(tmp_path).touch()
    H.finish(a), H.finish(b)
    assert hq.count() == 1
    (c,) = doc()["claims"].values()
    assert c["state"] == "written" and len(doc()["lines"]) == 1


def test_3_a_worker_killed_after_the_claim_settles_gave_up_and_is_never_called(hq, tmp_path):
    announce_automation()
    t = time.time() + 61
    w = H.spawn(tmp_path, H.PASS, ["claimed"], str(t))
    assert H.wait_at(w) == "claimed#1"
    w.kill()
    w.wait(10)
    (c,) = doc()["claims"].values()
    assert c["state"] == "attempt" and hq.count() == 0
    H.finish(H.spawn(tmp_path, H.PASS, (), str(t + hq_dispatch.GIVE_UP_S)))
    (c,) = doc()["claims"].values()
    assert c["state"] == "gave_up"
    for dt in (200, 5000, 86400):
        H.finish(H.spawn(tmp_path, H.PASS, (), str(t + dt)))
    assert hq.count() == 0 and doc()["lines"] == {}


def test_3_a_worker_killed_after_the_endpoint_received_its_request(hq, tmp_path):
    """The ambiguous outcome, on purpose: the endpoint has the request, the worker dies before the
    line. One request counted, ``gave_up``, and no second request, ever."""
    announce_automation()
    hq.hang = True
    t = time.time() + 61
    w = H.spawn(tmp_path, H.PASS, (), str(t))
    deadline = time.monotonic() + 60
    while hq.count() == 0:
        assert time.monotonic() < deadline and w.poll() is None
        time.sleep(0.05)
    w.kill()
    w.wait(10)
    hq.hang = False
    hq.release.set()
    for dt in (hq_dispatch.GIVE_UP_S, 500, 9000):
        H.finish(H.spawn(tmp_path, H.PASS, (), str(t + dt)))
    (c,) = doc()["claims"].values()
    assert c["state"] == "gave_up" and hq.count() == 1 and doc()["lines"] == {}


# ---- 4. a lost task -----------------------------------------------------------------------------


def test_4_a_row_written_and_pushed_then_killed_before_joining_is_dispatched_once(hq, tmp_path):
    pushes = tmp_path / "pushes.jsonl"
    p = H.spawn(
        tmp_path, H.PRODUCE, ["enqueue"], json.dumps([["abc", "r1", "nightly"]]), str(pushes)
    )
    assert H.wait_at(p) == "enqueue#1"
    # The child's background worker writes its reservation off the push's path; let it land.
    deadline = time.monotonic() + 10
    while not (hq_dispatch._path().exists() and doc()["pending"]):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    p.kill()
    p.wait(10)
    (row,) = rows()
    assert row["dispatch"] == "automation:abc:r1"
    assert len(pushes.read_text().splitlines()) == 1  # it was pushed
    assert not joined()  # …and never joined
    # The dead producer left its hand-off reservation; reconciliation will find it stale.
    assert list(doc()["pending"]) == ["automation:abc:r1"]
    t = time.time()
    H.finish(H.spawn(tmp_path, H.PASS, (), str(t + 1)))  # too fresh: its push may be running
    assert not hq_dispatch._path().exists() or not doc()["member_of"]
    G = hq_dispatch.RECONCILE_GRACE_S
    H.finish(H.spawn(tmp_path, H.PASS, (), str(t + G + 1)))  # the reconcile pass joins it
    assert list(doc()["member_of"]) == ["automation:abc:r1"]
    for dt in (G + 62, G + 300, 9000):
        H.finish(H.spawn(tmp_path, H.PASS, (), str(t + dt)))
    assert hq.count() == 1 and len(doc()["lines"]) == 1
    # Reconciled: the row's structured fields only — the name was enqueue-only.
    (entry,) = request_entries(hq)
    assert set(entry) == {"class", "age_min"} and entry["class"] == "automation failed"


# ---- 5. bundles under concurrency and crashes ---------------------------------------------------


def test_5_bundles_under_concurrency_and_crashes(hq, tmp_path):
    pushes = tmp_path / "pushes.jsonl"
    # Three producer processes at once. P2 dies between its third row write and its enqueue; P3
    # dies right after its second membership write. P1 announces eight.
    p1 = H.spawn(
        tmp_path,
        H.PRODUCE,
        (),
        json.dumps([[f"pa{i}", "r", f"p1-{i}"] for i in range(8)]),
        str(pushes),
    )
    p2 = H.spawn(
        tmp_path,
        H.PRODUCE,
        ["enqueue#3"],
        json.dumps([[f"pb{i}", "r", f"p2-{i}"] for i in range(4)]),
        str(pushes),
    )
    p3 = H.spawn(
        tmp_path,
        H.PRODUCE,
        ["joined#2"],
        json.dumps([[f"pc{i}", "r", f"p3-{i}"] for i in range(4)]),
        str(pushes),
    )
    for p in (p2, p3):
        H.wait_at(p)
        p.kill()
        p.wait(10)
    H.finish(p1)
    announced = {r["dispatch"] for r in rows()}
    assert len(announced) == 8 + 3 + 2
    assert len(doc()["member_of"]) == 8 + 2 + 2  # P2's third row was never joined
    # The writers' clocks start from the newest bundle, so every one has sealed by time.
    t0 = max(float(b["opened_at"]) for b in doc()["bundles"].values())

    # The order every step below relies on, pinned: B1 is the full bundle (10, opened first), B2
    # the rest (2).
    b1, b2 = sorted(doc()["bundles"], key=lambda b: doc()["bundles"][b]["opened_at"])
    assert [len(doc()["bundles"][b]["members"]) for b in (b1, b2)] == [10, 2]

    # W1 dies after claiming the oldest sealed bundle (B1), before sending.
    w1 = H.spawn(tmp_path, H.PASS, ["claimed"], str(t0 + 61))
    H.wait_at(w1)
    w1.kill()
    w1.wait(10)
    assert set(doc()["claims"]) == {b1}
    # W2 claims the next (B2), sends, and dies once the endpoint has its request. P2's lost row is
    # still inside the reconcile grace, so no pass has joined it yet.
    hq.hang = True
    w2 = H.spawn(tmp_path, H.PASS, (), str(t0 + 62))
    deadline = time.monotonic() + 60
    while hq.count() == 0:
        assert time.monotonic() < deadline and w2.poll() is None
        time.sleep(0.05)
    w2.kill()
    w2.wait(10)
    assert set(doc()["claims"]) == {b1, b2} and len(doc()["member_of"]) == 12
    hq.hang = False
    hq.release.set()
    # Two workers well after, lined up at the same step: the stale attempts give up, P2's lost row
    # is reconciled into a bundle of its own (sealed, its window long past), claimed once, written.
    late = str(t0 + hq_dispatch.RECONCILE_GRACE_S + 100)
    w3 = H.spawn(tmp_path, H.PASS, ["gate:sealed"], late)
    w4 = H.spawn(tmp_path, H.PASS, ["gate:sealed"], late)
    assert H.wait_at(w3) == "sealed#1" and H.wait_at(w4) == "sealed#1"
    H.gate(tmp_path).touch()
    H.finish(w3), H.finish(w4)
    H.finish(H.spawn(tmp_path, H.PASS, (), str(t0 + 9000)))

    d = doc()
    # Every enqueued or reconciled tag is in exactly one bundle.
    assert set(d["member_of"]) == announced
    for tag, bid in d["member_of"].items():
        assert [b for b, rec in d["bundles"].items() if tag in rec["members"]] == [bid]
    # Every bundle has at most one claim and one line; a line only for a written claim.
    assert set(d["lines"]) <= {b for b, c in d["claims"].items() if c["state"] == "written"}
    # At most one request per claimed bundle: each request is exactly one bundle's facts.
    input_of = {b: [d["facts"][t] for t in rec["members"]] for b, rec in d["bundles"].items()}
    per_bundle: dict[str, int] = {}
    for i in range(hq.count()):
        owners = [b for b, facts in input_of.items() if facts == request_entries(hq, i)]
        assert len(owners) == 1, "a request is not exactly one bundle's members"
        (b,) = owners
        per_bundle[b] = per_bundle.get(b, 0) + 1
    assert all(n == 1 for n in per_bundle.values())
    assert set(per_bundle) <= set(d["claims"])
    states = [c["state"] for c in d["claims"].values()]
    assert hq.count() >= states.count("written") + states.count("failed")
    # A bundle with no request is gave_up; the one killed after sending is too.
    for b, c in d["claims"].items():
        if b not in per_bundle:
            assert c["state"] == "gave_up"
    assert states.count("gave_up") == 2 and states.count("written") == 1
    assert hq.count() == 2


# ---- 6. an enqueue failure doesn't touch the push -----------------------------------------------


def test_6_an_enqueue_failure_sends_exactly_the_payloads_sent_without_it(hq, monkeypatch, tmp_path):
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    _subs(tmp_path)

    def produce(store: str) -> list[bytes]:
        monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / store))
        log: list = []
        _capture_pushes(monkeypatch, log)
        announce_needs_you(monkeypatch)
        announce_automation()
        return [p for _k, p in log]

    def boom(*a, **k):
        raise OSError("the dispatch store is full")

    with monkeypatch.context() as m:
        m.setattr(hq_dispatch, "announce", lambda *a, **k: None)  # as before this change
        before = produce("before.json")
    with monkeypatch.context() as m:
        m.setattr(hq_dispatch, "enqueue", boom)
        failing = produce("failing.json")
    assert len(before) == 2 and failing == before  # byte-identical
    assert not joined()
    # The next reconcile pass joins the members from their rows.
    run_pass(1_800_000_000.0 + hq_dispatch.RECONCILE_GRACE_S + 1)
    assert len(doc()["member_of"]) == 2


# ---- 7. history survives the ring ---------------------------------------------------------------


def test_7_history_survives_dismissal_and_the_ring(hq, monkeypatch):
    announce_automation(name="nightly docs", reason="DISPLAY-ONLY-REASON")
    announce_needs_you(monkeypatch, title="DISPLAY-ONLY-SESSION-TITLE")
    announce_escalation(reason="DISPLAY-ONLY-ESCALATION")
    (bid,) = doc()["bundles"]
    notifications.dismiss(None)
    for i in range(notifications.NOTIFY_MAX + 5):
        notifications.add(
            title=f"newer {i}", project="", session_id="", engine="", action_id=f"n{i}"
        )
    assert not any("dispatch" in r for r in rows())
    run_pass(time.time() + 61)
    one = hq_dispatch.one_view(bid)["dispatch"]
    got = {(m["class"], m["title"], m["reason"], m["link"]) for m in one["members"]}
    assert got == {
        (
            "automation failed",
            "Automation failed: nightly docs",
            "failed: DISPLAY-ONLY-REASON",
            "/mission/automations/abc",
        ),
        (
            "needs you",
            "DISPLAY-ONLY-SESSION-TITLE",
            "Asks you a question",
            "/s/claude/11111111-1111-4111-8111-111111111111",
        ),
        (
            "mission escalation",
            "DISPLAY-ONLY-ESCALATION",
            "mission supervisor",
            "/s/claude/11111111-1111-4111-8111-111111111111",
        ),
    }
    assert all(m["ts"] > 0 for m in one["members"])
    wire = hq.bodies()[0]
    for m in one["members"]:
        for v in (m["title"], m["reason"], m["link"]):
            assert v not in wire


# ---- 8. a lost enqueue, per producer, at the model request --------------------------------------


def test_8_a_lost_enqueue_is_reconciled_with_the_reduced_shape(hq, monkeypatch):
    s = H.SENTINELS
    prefs.set_hq_contact("off")  # no persona text on the wire to confuse the scan

    def boom(*a, **k):
        raise RuntimeError("lost")

    with monkeypatch.context() as m:
        m.setattr(hq_dispatch, "enqueue", boom)
        announce_needs_you(monkeypatch, title=s["session_title"] + " " + s["row_title"])
        announce_escalation(
            reason=f"{s['objective']}: {s['escalation_reason']}", title="SENTINEL-MISSION-NAME"
        )
        automation_runner.notify(
            {
                "automation": {"id": "abc", "name": "SENTINEL-AUTOMATION-NAME"},
                "run": {"id": "r1", "outcome": "refused", "reason": s["run_reason"]},
                "episode": ("open", "automation:abc:r1"),
            }
        )
    assert not joined() and len(rows()) == 3
    assert not doc()["pending"]  # a failed enqueue gives its reservation back
    t = time.time()
    run_pass(t + hq_dispatch.RECONCILE_GRACE_S + 1)
    run_pass(t + hq_dispatch.RECONCILE_GRACE_S + 62)
    assert hq.count() == 1
    wire = hq.bodies()[0]
    for v in [*s.values(), "SENTINEL-MISSION-NAME", "SENTINEL-AUTOMATION-NAME", "refused"]:
        assert v not in wire, v
    for e in request_entries(hq):
        assert set(e) <= hq_dispatch.REDUCED_KEYS[e["class"]]
        assert {"class", "age_min"} <= set(e)
        assert not {"mission", "automation", "outcome"} & set(e)
        if e["class"] != "automation failed":
            assert e["project"] == "api"
    assert {e["class"] for e in request_entries(hq)} == {
        "needs you",
        "mission escalation",
        "automation failed",
    }


# ---- 9. the 24 h window -------------------------------------------------------------------------


@pytest.mark.parametrize("outage", ["longer than 24 h", "inside the window"])
def test_9_a_lost_enqueue_is_reconciled_only_within_24_hours(outage, hq, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("lost")

    with monkeypatch.context() as m:
        m.setattr(hq_dispatch, "enqueue", boom)
        announce_automation()
    (row,) = rows()
    # The writer's first pass after its outage.
    back = row["ts"] + hq_dispatch.RECONCILE_S + (1 if outage == "longer than 24 h" else -3600)
    for dt in (0, 61, 900):
        run_pass(back + dt)
    assert rows() == [row]  # the row survives either way, with its fixed text
    if outage == "longer than 24 h":
        assert not hq_dispatch._path().exists() or not doc()["member_of"]
        assert hq.count() == 0
    else:
        assert list(doc()["member_of"]) == ["automation:abc:r1"]
        assert hq.count() == 1 and len(doc()["lines"]) == 1


# ---- 10. the loss boundary ----------------------------------------------------------------------


@pytest.mark.parametrize("dismissed", [True, False])
def test_10_the_loss_boundary(dismissed, hq, tmp_path):
    pushes = tmp_path / "pushes.jsonl"
    events = [["a1", "r", "first"], ["a2", "r", "second"], ["a3", "r", "third"]]
    p = H.spawn(tmp_path, H.PRODUCE, ["enqueue#2"], json.dumps(events), str(pushes))
    assert H.wait_at(p) == "enqueue#2"
    p.kill()
    p.wait(10)
    lost = next(r for r in rows() if r["dispatch"] == "automation:a2:r")
    if dismissed:
        notifications.dismiss([lost["id"]])  # before the next pass
    t = time.time()
    G = hq_dispatch.RECONCILE_GRACE_S
    for dt in (1, 62, 125, G + 1, G + 62, G + 400):
        H.finish(H.spawn(tmp_path, H.PASS, (), str(t + dt)))
    d = doc()
    if dismissed:
        assert list(d["member_of"]) == ["automation:a1:r"]
        assert hq.count() == 1
        assert [e.get("automation") for e in request_entries(hq)] == ["first"]
    else:
        assert set(d["member_of"]) == {"automation:a1:r", "automation:a2:r"}
        # The first's bundle is written on its own; the lost one is reconciled once the grace has
        # passed, into a bundle of its own (its window long sealed), with the reduced shape.
        assert hq.count() == 2
        assert request_entries(hq, 0) == [
            {"age_min": 0, "automation": "first", "class": "automation failed", "outcome": "failed"}
        ]
        (reduced,) = request_entries(hq, 1)
        assert set(reduced) == {"class", "age_min"} and reduced["age_min"] >= 5
    assert len(d["lines"]) == hq.count()
    assert not any("a3" in tag for tag in d["member_of"])  # never announced at all
