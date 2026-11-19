"""The Forgejo review loop, PR 1 of #2202: the signed webhook and the review leg.

Read `docs/invariants/forge-review-loop.md`. Nothing reaches a real forge, engine or agent: the
forge's answer (`review_loop_runner.current_revision`), the save-time world checks and the API
launch (`mission_structured.automation_start`) are faked at the seams the code calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_sessions import automation_effect_lock as effect_lock
from agent_sessions import (
    automation_runner,
    forge,
    forge_hook,
    review_loop,
    review_loop_runner,
)
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions.routes import automations as routes
from agent_sessions.routes import hooks

REPO = "example-org/shop"
REVIEWER = "shop-reviewer-bot"
SHA_A = "a" * 40
SHA_B = "b" * 40
SPEC = {
    "id": "shop-reviewer",
    "version": 3,
    "name": "Shop Reviewer",
    "engine": "opencode-api",
    "model": "example-gateway/model-pro",
    "effort": "high",
}


# ---- fixtures -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    """The loop on, and the world a review loop pins faked: the project folder and the specialist
    version are fixed, and the save-time checks of clients and forge are stubbed."""
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    monkeypatch.setattr(effect_lock, "WRITER_WAIT_S", 0.2)
    folder = tmp_path / "shop"
    folder.mkdir()
    monkeypatch.setattr(
        model, "resolve_mission_cwd", lambda _pid: model.folder_identity(str(folder))
    )
    monkeypatch.setattr(model, "specialist_pin", lambda _r: dict(SPEC))
    monkeypatch.setattr(review_loop_runner, "check_save", lambda _c: None)
    automation_runner._TASKS.clear()
    hooks.reset_for_tests()
    yield folder
    pending = [t for t in automation_runner._TASKS if not t.done()]
    automation_runner._TASKS.clear()
    assert not pending, f"run tasks still pending after the test: {pending}"


@pytest.fixture
def spawned(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(automation_runner, "spawn", lambda run, registry=None: out.append(run))
    return out


@pytest.fixture
def forge_says(monkeypatch):
    """What the forge reports as the item's current revision: ``{item: (rev, open)}`` or an
    exception string for "could not look"."""
    state: dict = {}

    def current(repo, item, origin):
        got = state.get(item)
        if got is None:
            return None, False, "the forge did not answer"
        return got[0], got[1], ""

    monkeypatch.setattr(review_loop_runner, "current_revision", current)
    return state


def _config(**action) -> dict:
    act = {
        "kind": "forge_review_loop",
        "project_id": "shop",
        "repo": REPO,
        "reviewer": {"specialist_id": SPEC["id"], "version": SPEC["version"]},
        "reviewer_login": REVIEWER,
    }
    act.update(action)
    return {
        "name": "Review loop",
        "trigger": {"kind": "webhook", "scheme": "forgejo"},
        "action": act,
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


def _pr(number=41, sha=SHA_A, action="synchronized", **over) -> dict:
    pr = {
        "number": number,
        "state": "open",
        "title": "Fix checkout totals",
        "body": "<!-- agent-session-pr: engine=claude-api id=abc-123 -->\nbody",
        "user": {"login": "marcus"},
        "head": {"sha": sha, "ref": "devopsagent/fix-totals"},
        "draft": False,
    }
    pr.update(over.pop("pr", {}))
    body = {
        "action": action,
        "number": number,
        "pull_request": pr,
        "repository": {"full_name": REPO, "html_url": f"https://git.example.com/{REPO}"},
        "sender": {"login": "marcus"},
    }
    body.update(over)
    return body


def _issue(number=39, title="Plan", body="Do the thing", action="opened", **over) -> dict:
    out = {
        "action": action,
        "number": number,
        "issue": {
            "number": number,
            "state": "open",
            "title": title,
            "body": body,
            "user": {"login": "marcus"},
        },
        "repository": {"full_name": REPO, "html_url": f"https://git.example.com/{REPO}"},
        "sender": {"login": "marcus"},
    }
    out.update(over)
    return out


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _deliver(aid: str, payload: dict, *, registry=None) -> None:
    asyncio.run(review_loop_runner.handle(aid, json.dumps(payload).encode(), {}, registry=registry))


# ---- signature and parsing ----------------------------------------------------------------------


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


def test_parse_types_a_pull_request_push_and_reads_only_closed_fields():
    ev = forge_hook.parse(json.dumps(_pr()).encode())
    assert (ev.kind, ev.action, ev.item, ev.revision) == ("pr", "synchronized", "pr#41", SHA_A)
    assert ev.head_ref == "devopsagent/fix-totals"
    assert ev.session_marker == "claude-api:abc-123"
    assert not ev.draft
    # The title and body are NOT carried: only the closed fields above.
    assert "Fix checkout" not in repr(ev)


def test_parse_types_an_issue_and_its_revision_covers_title_and_body():
    a = forge_hook.parse(json.dumps(_issue(title="T", body="B")).encode())
    b = forge_hook.parse(json.dumps(_issue(title="T2", body="B")).encode())
    c = forge_hook.parse(json.dumps(_issue(title="T", body="B2")).encode())
    assert a.kind == "issue" and a.item == "issue#39"
    assert a.revision == forge_hook.issue_revision("T", "B")
    assert len({a.revision, b.revision, c.revision}) == 3


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda p: p["pull_request"]["head"].update(sha="zz"), "head sha"),
        (lambda p: p["pull_request"]["head"].update(ref="a b"), "head ref"),
        (lambda p: p["pull_request"].update(number="41"), "number"),
        (lambda p: p["repository"].update(full_name="a/b/c"), "repository"),
        (lambda p: p["sender"].update(login="x y"), "sender"),
        (lambda p: p["pull_request"].update(state="merged"), "state"),
    ],
)
def test_a_type_failing_field_rejects_the_whole_delivery(mutate, why):
    p = _pr()
    mutate(p)
    with pytest.raises(forge_hook.Rejected, match=why):
        forge_hook.parse(json.dumps(p).encode())


def test_an_oversized_or_non_json_body_is_rejected():
    with pytest.raises(forge_hook.Rejected, match="too large"):
        forge_hook.parse(b" " * (forge_hook.BODY_MAX + 1))
    with pytest.raises(forge_hook.Rejected, match="not JSON"):
        forge_hook.parse(b"{")


def test_a_ping_or_push_shape_is_other():
    assert forge_hook.parse(b'{"zen": "hi", "hook_id": 1}').kind == "other"
    push = {"ref": "refs/heads/main", "repository": {"full_name": REPO}, "sender": {"login": "m"}}
    assert forge_hook.parse(json.dumps(push).encode()).kind == "other"


def test_the_verdict_marker_is_closed_and_must_appear_once():
    rec = "abc123"
    ok = f"<!-- battlelab-review: record={rec} item=pr#41 rev={SHA_A} verdict=changes -->"
    assert forge_hook.verdict_marker(ok)[0] == (rec, "pr", 41, SHA_A, "changes")
    assert forge_hook.verdict_marker(ok + ok)[1] == "more than one marker"
    assert "not a pr verdict" in forge_hook.verdict_marker(ok.replace("changes", "viable"))[1]
    assert "does not fit" in forge_hook.verdict_marker(ok.replace(SHA_A, "a" * 16))[1]
    assert forge_hook.verdict_marker("<!-- battlelab-review: oops -->")[1] == (
        "the verdict marker is not well-formed"
    )


# ---- decisions ----------------------------------------------------------------------------------


def _action(**over) -> dict:
    return model.validate_action(_config(**over)["action"])


def test_classify_starts_a_review_for_a_pushed_open_pr_keyed_by_its_revision():
    d = review_loop.classify(_action(), forge_hook.parse(json.dumps(_pr()).encode()))
    assert (d.kind, d.slot, d.item, d.revision) == (
        "review",
        f"review:{REPO}:pr:41:{SHA_A}",
        "pr#41",
        SHA_A,
    )


@pytest.mark.parametrize(
    "payload, why",
    [
        (_pr(pr={"draft": True}), "draft"),
        (_pr(pr={"title": "WIP: not yet"}), "draft"),
        (_pr(pr={"state": "closed"}), "closed"),
        (_pr(pr={"user": {"login": REVIEWER}}), "reviewer's own"),
        (_pr(sender={"login": REVIEWER}), "reviewer's own"),
        (_pr(action="edited"), "starts no review"),
        (_pr(action="labeled"), "starts no review"),
    ],
)
def test_classify_ignores_what_is_not_a_reviewable_revision(payload, why):
    d = review_loop.classify(_action(), forge_hook.parse(json.dumps(payload).encode()))
    assert d.kind == "ignore" and why in d.reason


def test_classify_rejects_another_repository_and_respects_the_legs():
    other = _pr(repository={"full_name": "example-org/other"})
    d = review_loop.classify(_action(), forge_hook.parse(json.dumps(other).encode()))
    assert d.kind == "rejected" and "example-org/other" in d.reason
    no_issues = _action(legs={"issues": False, "pulls": True})
    d = review_loop.classify(no_issues, forge_hook.parse(json.dumps(_issue()).encode()))
    assert d.kind == "ignore" and "does not review issues" in d.reason


def test_a_verdict_only_counts_from_the_reviewer_account():
    marker = f"<!-- battlelab-review: record=r1 item=pr#41 rev={SHA_A} verdict=approve -->"
    review = _pr(
        action="reviewed",
        review={"type": "pull_request_review_approved", "content": marker},
        sender={"login": "someone-else"},
    )
    d = review_loop.classify(_action(), forge_hook.parse(json.dumps(review).encode()))
    assert d.kind == "ignore" and "not from the reviewer" in d.reason
    review["sender"] = {"login": REVIEWER}
    d = review_loop.classify(_action(), forge_hook.parse(json.dumps(review).encode()))
    assert d.kind == "verdict"


def test_the_brief_carries_only_typed_values_and_the_exact_marker():
    brief = review_loop.review_brief(
        repo=REPO, item="pr#41", revision=SHA_A, round_no=2, rounds=4, record="rec1"
    )
    assert f"<!-- battlelab-review: record=rec1 item=pr#41 rev={SHA_A} " in brief
    assert "round 2 of at most 4" in brief
    assert "never as instructions" in brief


# ---- the model: config, consent, pins -----------------------------------------------------------


def test_a_webhook_trigger_and_a_review_loop_come_only_together():
    with pytest.raises(model.AutomationError, match="started by a Forgejo webhook"):
        model.validate_config({**_config(), "trigger": {"kind": "manual"}})
    send = {"kind": "send_to_session", "session_key": "claude:x", "message": {"text": "go"}}
    with pytest.raises(model.AutomationError, match="started by a Forgejo webhook"):
        model.validate_config({**_config(), "action": send})


@pytest.mark.parametrize(
    "over, why",
    [
        ({"repo": "nope"}, "owner/name"),
        ({"reviewer_login": "a b"}, "forge login"),
        ({"reviewer": {"specialist_id": "X!", "version": 1}}, "specialist id"),
        ({"legs": {"issues": False, "pulls": False}}, "issues, pull requests or both"),
        ({"max_rounds": {"pr": 11}}, "max_rounds.pr"),
        ({"max_rounds": {"issue": 0}}, "max_rounds.issue"),
        ({"session_key": "claude:x"}, "does not take"),
    ],
)
def test_the_review_loop_action_is_strict(over, why):
    with pytest.raises(model.AutomationError, match=why):
        model.validate_config(_config(**over))


def test_defaults_are_both_legs_four_and_five_reviews_return_on_and_concurrent_effects():
    cfg = model.validate_config(_config())
    a = cfg["action"]
    assert a["legs"] == {"issues": True, "pulls": True}
    assert a["max_rounds"] == {"issue": 5, "pr": 4}
    assert a["return_to_origin"] is True
    assert cfg["policy"]["concurrency"] == "allow"


def test_consent_lines_name_every_review_loop_field():
    cfg = model.validate_config(_config())
    scope = model.scope_of(cfg, model.compute_pins(cfg))
    text = "\n".join(model.describe(scope))
    for needle in (
        REPO,
        REVIEWER,
        "Shop Reviewer (shop-reviewer v3 · effort high)",
        "as an API session",
        "4 rounds per pull request and 5 per issue",
        "a terminal session gets nothing",
        "Never posts, merges or labels",
        "Starts when Forgejo sends a signed event",
        "opencode-api · example-gateway/model-pro",
    ):
        assert needle in text, needle


@pytest.mark.parametrize(
    "over",
    [
        {"repo": "example-org/other"},
        {"reviewer_login": "other-bot"},
        {"max_rounds": {"pr": 5}},
        {"max_rounds": {"pr": 3}},  # stricter, but still a different grant
        {"legs": {"issues": True, "pulls": False}},
        {"return_to_origin": False},
        {"reviewer": {"specialist_id": SPEC["id"], "version": 4}},
    ],
)
def test_every_review_loop_change_needs_consent(over, monkeypatch):
    old = model.validate_config(_config())
    new = model.validate_config(_config(**over))
    old_scope = model.scope_of(old, model.compute_pins(old))
    if "reviewer" in over:
        monkeypatch.setattr(model, "specialist_pin", lambda _r: {**SPEC, "version": 4})
    assert model.widened(old_scope, model.scope_of(new, model.compute_pins(new)))


def test_a_specialist_edited_archived_or_deleted_is_a_drift(monkeypatch):
    cfg = model.validate_config(_config())
    pins = model.compute_pins(cfg)
    for missing in (
        "that specialist was edited to a newer version",
        "that specialist is archived",
        "that specialist version no longer exists",
    ):
        monkeypatch.setattr(model, "specialist_pin", lambda _r, m=missing: {**SPEC, "missing": m})
        assert model.pins_drift(pins, model.compute_pins(cfg)) == missing


def test_run_now_is_refused_for_a_review_loop():
    aid = _made()
    with pytest.raises(automation_runner.RunRefused, match="only when its webhook"):
        asyncio.run(automation_runner.run_now(aid, registry=None))
    assert _runs(aid) == []


# ---- the store: secrets, admission, cap ---------------------------------------------------------


def test_the_secret_is_shown_once_stored_encrypted_and_rotates_with_an_overlap():
    aid = _made()
    first = store.new_hook_secret(aid, now=1000.0)
    assert store.hook_secrets(aid, now=1000.0) == [first]
    raw = store.db_path().read_bytes()
    assert first.encode() not in raw  # only the AES-GCM envelope is stored
    second = store.new_hook_secret(aid, now=2000.0)
    assert set(store.hook_secrets(aid, now=2000.0)) == {first, second}
    later = 2000.0 + store.SECRET_OVERLAP_S + 1
    assert store.hook_secrets(aid, now=later) == [second]
    pub = routes.public(store.get(aid))["webhook"]
    assert pub["path"] == f"/hooks/a/{aid}" and pub["secret_set"]
    assert first not in json.dumps(pub) and second not in json.dumps(pub)


def test_only_a_webhook_automation_has_a_secret():
    send = {
        "name": "n",
        "trigger": {"kind": "manual"},
        "action": {"kind": "send_to_session", "session_key": "claude:x", "message": {"text": "go"}},
    }
    row = store.create(model.validate_config(send), {})
    with pytest.raises(store.StoreError, match="only a webhook automation"):
        store.new_hook_secret(row["id"])


def _admit(aid, slot, item="pr#41", rev=SHA_A, repo=REPO, expect=None):
    expect = store.get(aid)["revision"] if expect is None else expect
    return store.begin_review(
        aid, slot=slot, repo=repo, item=item, revision=rev, expect_revision=expect, inputs={}
    )


def _key(item="pr#41", repo=REPO):
    return review_loop.item_key(repo, item)


def test_admission_reserves_a_lifetime_review_and_writes_the_record_in_one_step():
    aid = _made(_config(max_rounds={"pr": 2}))
    one = _admit(aid, "review:pr:41:a", rev=SHA_A)
    assert one["claimed"] and one["round"] == 1 and one["run"]["state"] == "dispatching"
    assert one["run"]["inputs"]["record"] == one["record"]
    two = _admit(aid, "review:pr:41:b", rev=SHA_B)
    assert two["round"] == 2
    recs = {r["id"]: r for r in store.review_records(aid, _key("pr#41"))}
    assert recs[one["record"]]["state"] == "superseded"
    assert recs[two["record"]]["state"] == "dispatched"
    # The cap counts LAUNCHES over the item's life: a third revision starts nothing.
    three = _admit(aid, "review:pr:41:c", rev="c" * 40)
    assert three.get("capped") and three["run"]["outcome"] == "capped"
    assert store.review_count(aid, _key("pr#41")) == 2
    # Another item has its own count.
    assert _admit(aid, "review:pr:42:a", item="pr#42")["round"] == 1


def test_admission_is_bound_to_the_automation_revision_it_was_classified_against():
    aid = _made()
    seen = store.get(aid)["revision"]
    # The operator changes (and re-approves) the automation while the forge is being read.
    pub = routes.public(store.get(aid))
    routes._patch(aid, {"revision": pub["revision"], "name": "renamed"})
    res = _admit(aid, "review:pr:41:a", expect=seen)
    assert res.get("stale_config") and not res["claimed"] and res["run"] is None
    assert store.review_count(aid, _key()) == 0


def test_a_config_change_during_the_forge_read_is_classified_again(
    spawned, forge_says, monkeypatch
):
    aid = _made()
    calls = {"n": 0}

    def current(repo, item, origin):
        calls["n"] += 1
        if calls["n"] == 1:
            # The repository changes, with consent, while this read is in flight.
            pub = routes.public(store.get(aid))
            new = {**_config(repo="example-org/other")["action"]}
            body = {"revision": pub["revision"], "action": new}
            scope = model.scope_of(
                model.validate_config({**_config(), "action": new}),
                model.compute_pins(model.validate_config({**_config(), "action": new})),
            )
            body.update(consent=True, scope_digest=routes.scope_digest(scope))
            routes._patch(aid, body)
        return SHA_A, True, ""

    monkeypatch.setattr(review_loop_runner, "current_revision", current)
    _deliver(aid, _pr())  # a delivery for example-org/shop
    assert spawned == []  # re-classified against the new repo: rejected, never admitted
    (run,) = _runs(aid)
    assert run["outcome"] == "refused" and "not example-org/other" in run["reason"]


def test_item_history_is_per_repository():
    aid = _made(_config(max_rounds={"pr": 1}))
    assert _admit(aid, "review:shop:pr:41:a")["round"] == 1
    pub = routes.public(store.get(aid))
    new = _config(repo="example-org/other", max_rounds={"pr": 1})
    cfg = model.validate_config(new)
    scope = model.scope_of(cfg, model.compute_pins(cfg))
    routes._patch(
        aid,
        {
            "revision": pub["revision"],
            "action": new["action"],
            "consent": True,
            "scope_digest": routes.scope_digest(scope),
        },
    )
    other = _admit(aid, "review:other:pr:41:b", repo="example-org/other", rev=SHA_B)
    assert other["round"] == 1 and not other.get("capped")  # never inherits shop's count
    assert store.review_count(aid, _key(repo=REPO)) == 1
    assert store.review_count(aid, _key(repo="example-org/other")) == 1


def test_the_same_revision_is_admitted_once_even_after_a_reopen():
    aid = _made()
    assert _admit(aid, "review:pr:41:a")["claimed"]
    again = _admit(aid, "review:pr:41:a")
    assert not again["claimed"] and again["run"] is None
    assert store.review_count(aid, _key("pr#41")) == 1


def test_concurrent_admissions_for_one_item_never_exceed_the_cap():
    aid = _made(_config(max_rounds={"pr": 3}))
    results: list[dict] = []
    barrier = threading.Barrier(6)

    def go(i: int) -> None:
        barrier.wait()
        results.append(_admit(aid, f"review:pr:41:{i}", rev=f"{i:x}" * 40))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rounds = sorted(r["round"] for r in results if r.get("round"))
    assert rounds == [1, 2, 3]
    # The rest is capped, or skipped while three launches are in flight — never a fourth review.
    rest = [r for r in results if not r.get("round")]
    assert len(rest) == 3
    assert all(r.get("capped") or r["run"]["outcome"] == "skipped" for r in rest)
    assert store.review_count(aid, _key("pr#41")) == 3


def test_a_skip_for_the_daily_cap_records_the_run_without_consuming_the_claim(monkeypatch):
    aid = _made()
    real = store._eligibility
    monkeypatch.setattr(store, "_eligibility", lambda *a, **k: ("skip", "skipped: daily cap"))
    skipped = _admit(aid, "review:pr:41:a")
    assert not skipped["claimed"] and skipped["run"]["outcome"] == "skipped"
    monkeypatch.setattr(store, "_eligibility", real)
    assert _admit(aid, "review:pr:41:a")["claimed"]  # redelivered later, it runs


def test_a_disabled_or_unapproved_automation_admits_nothing():
    aid = _made(enabled=False)
    res = _admit(aid, "review:pr:41:a")
    assert not res["claimed"] and res["run"] is None
    assert store.review_count(aid, _key("pr#41")) == 0


# ---- the runtime: decide, reconcile with the forge, admit ---------------------------------------


def test_a_current_revision_launches_one_review(spawned, forge_says):
    aid = _made()
    forge_says["pr#41"] = (SHA_A, True)
    _deliver(aid, _pr())
    _deliver(aid, _pr())  # the same delivery again: claimed once
    assert len(spawned) == 1
    run = spawned[0]
    assert run["inputs"]["item"] == "pr#41" and run["inputs"]["round"] == 1
    assert run["inputs"]["headers"] == "recorded, not trusted"


def test_a_late_delivery_of_an_old_revision_is_ignored_as_stale(spawned, forge_says):
    aid = _made()
    forge_says["pr#41"] = (SHA_B, True)  # the forge already reports B
    _deliver(aid, _pr(sha=SHA_A))
    assert spawned == []
    (run,) = _runs(aid)
    assert run["outcome"] == "ignored" and "stale delivery" in run["reason"]
    assert store.review_count(aid, _key("pr#41")) == 0  # a stale delivery consumes no review


def test_an_unreachable_forge_fails_closed(spawned, forge_says):
    aid = _made()
    _deliver(aid, _pr())  # forge_says has nothing: "could not look"
    assert spawned == []
    (run,) = _runs(aid)
    assert run["outcome"] == "refused"
    assert review_loop_runner.UNCONFIRMED in run["reason"]
    assert store.review_count(aid, _key("pr#41")) == 0


def test_a_closed_item_on_the_forge_starts_nothing(spawned, forge_says):
    aid = _made()
    forge_says["pr#41"] = (SHA_A, False)
    _deliver(aid, _pr())
    assert spawned == []
    assert _runs(aid)[0]["outcome"] == "ignored"


def test_another_repository_is_a_recorded_refusal_and_replaying_it_records_nothing_twice(
    spawned, forge_says
):
    aid = _made()
    other = _pr(repository={"full_name": "example-org/other"})
    _deliver(aid, other)
    _deliver(aid, other)
    (run,) = _runs(aid)
    assert run["outcome"] == "refused" and "example-org/other" in run["reason"]
    assert spawned == []


def test_a_malformed_body_is_a_recorded_rejection(spawned, forge_says):
    aid = _made()
    bad = _pr()
    bad["pull_request"]["head"]["sha"] = "nope"
    _deliver(aid, bad)
    (run,) = _runs(aid)
    assert run["outcome"] == "refused" and run["reason"].startswith("rejected:")


def test_chatter_is_not_a_run_but_updates_the_last_delivery(spawned, forge_says):
    aid = _made()
    comment = _issue(action="created", comment={"id": 5, "body": "lgtm", "user": {"login": "m"}})
    _deliver(aid, comment)
    assert _runs(aid) == []
    state = store.hook_state(aid)
    assert state["last_delivery_at"] is not None
    assert "not from the reviewer" in state["last_note"]


def test_a_payload_cannot_name_a_session_folder_or_engine(spawned, forge_says):
    aid = _made()
    forge_says["pr#41"] = (SHA_A, True)
    _deliver(
        aid,
        _pr(session_key="claude:evil", folder="/etc", engine="claude", project_id="other"),
    )
    (run,) = spawned
    scope = run["scope"]["config"]["action"]
    assert scope["project_id"] == "shop" and "session_key" not in scope
    assert "evil" not in json.dumps(run["inputs"])


def test_issue_edits_before_any_verdict_are_bounded_by_the_cap(spawned, forge_says):
    aid = _made(_config(max_rounds={"issue": 2}, return_to_origin=False))
    for i in range(4):
        payload = _issue(body=f"v{i}", action="edited")
        forge_says["issue#39"] = (forge_hook.issue_revision("Plan", f"v{i}"), True)
        _deliver(aid, payload)
    assert len(spawned) == 2
    assert [r["outcome"] for r in _runs(aid)].count("capped") == 2


# ---- the launch ---------------------------------------------------------------------------------


def test_the_reviewer_launches_from_its_pinned_specialist_version(spawned, forge_says, monkeypatch):
    aid = _made()
    forge_says["pr#41"] = (SHA_A, True)
    _deliver(aid, _pr())
    (run,) = spawned
    seen: dict = {}

    class Out:
        ok, launched, started, briefed = True, True, True, True
        key, teardown, reason, state, seed_outcome = "opencode-api:s1", "", "", "", ""

    async def start(**kw):
        seen.update(kw)
        kw["on_key"]("opencode-api:s1")
        return Out()

    from agent_sessions import mission_structured

    class Prov:
        engine_id = "opencode-api"

    monkeypatch.setattr(mission_structured, "automation_start", start)
    monkeypatch.setattr(review_loop_runner.engines, "get", lambda _e: Prov())
    monkeypatch.setattr(mission_structured, "is_api", lambda _p: True)
    monkeypatch.setattr(automation_runner, "target_state", lambda _e: (True, ""))
    monkeypatch.setattr(automation_runner, "folder_ok", lambda _f: (True, ""))
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    assert seen["specialist"] == ("shop-reviewer", 3)
    assert seen["model"] == "example-gateway/model-pro" and seen["effort"] == "high"
    assert f"record={run['inputs']['record']} item=pr#41 rev={SHA_A}" in seen["brief"]
    (rec,) = store.review_records(aid, _key("pr#41"))
    assert rec["session_key"] == "opencode-api:s1"


def test_a_terminal_reviewer_is_refused_at_launch(spawned, forge_says, monkeypatch):
    aid = _made()
    forge_says["pr#41"] = (SHA_A, True)
    _deliver(aid, _pr())
    (run,) = spawned
    from agent_sessions import mission_structured

    monkeypatch.setattr(review_loop_runner.engines, "get", lambda _e: object())
    monkeypatch.setattr(mission_structured, "is_api", lambda _p: False)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "refused"
    assert "not on an API client" in res["run"]["reason"]
    assert store.review_count(aid, _key("pr#41")) == 1  # the reserved review stays counted


# ---- save-time checks ---------------------------------------------------------------------------


def test_save_refuses_a_terminal_reviewer_and_a_missing_forge(monkeypatch, _world):
    monkeypatch.undo()
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    from agent_sessions import mission_structured, prefs
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes, "_resolve_cwd", lambda _p: ("shop", str(_world)))
    monkeypatch.setattr(automation_runner, "scope_ok", lambda _c: True)
    monkeypatch.setattr(model, "specialist_pin", lambda _r: dict(SPEC))

    class Prov:
        engine_id = "opencode-api"

    monkeypatch.setattr(review_loop_runner.engines, "get", lambda _e: Prov())
    cfg = model.validate_config(_config())
    monkeypatch.setattr(mission_structured, "is_api", lambda _p: False)
    with pytest.raises(model.AutomationError, match="must run on an API client"):
        review_loop_runner.check_save(cfg)
    monkeypatch.setattr(mission_structured, "is_api", lambda _p: True)
    monkeypatch.setattr(automation_runner, "target_state", lambda _e: (True, ""))
    monkeypatch.setattr(prefs, "get_forge", lambda: {"enabled": False, "base_url": ""})
    with pytest.raises(model.AutomationError, match="no forge is configured"):
        review_loop_runner.check_save(cfg)
    monkeypatch.setattr(
        prefs,
        "get_forge",
        lambda: {"enabled": True, "kind": "forgejo", "base_url": "https://git.example.com"},
    )
    review_loop_runner.check_save(cfg)  # all good


def test_a_token_is_never_sent_to_another_forge(monkeypatch):
    from agent_sessions import prefs

    monkeypatch.setattr(
        prefs,
        "get_forge",
        lambda: {
            "enabled": True,
            "kind": "forgejo",
            "base_url": "https://git.example.com",
            "token": "t0k3n",
        },
    )
    client, why = review_loop_runner.forge_client("https://evil.example.net")
    assert client is None and "another forge" in why
    client, why = review_loop_runner.forge_client("https://git.example.com")
    assert isinstance(client, forge.ForgeClient)


def test_the_forge_client_reads_an_issue_revision_without_writing(monkeypatch):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/repos/{REPO}/issues/39"
        return httpx.Response(200, json={"title": "T", "body": "B", "state": "open"})

    forge.set_transport_for_test(httpx.MockTransport(handler))
    try:
        c = forge.ForgeClient(kind="forgejo", base_url="https://git.example.com")
        fact = c.issue_by_number(REPO, 39)
        assert fact.observed and fact.value
        assert fact.extra["revision"] == forge_hook.issue_revision("T", "B")
        assert "T" not in json.dumps(fact.extra)
    finally:
        forge.set_transport_for_test(None)


# ---- the route ----------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    handled: list = []

    async def _nothing():
        return None

    def handle(aid, body, headers, registry=None):
        # Recorded at the CALL, which the route makes before it answers; the coroutine it
        # schedules may run after the response.
        handled.append((aid, body, headers))
        return _nothing()

    monkeypatch.setattr(review_loop_runner, "handle", handle)
    app = FastAPI()
    hooks.register(app)
    with TestClient(app) as c:
        c.handled = handled
        yield c


def _post(c, aid, body: bytes, sig: str | None):
    headers = {"Content-Type": "application/json", "X-Forgejo-Event": "pull_request"}
    if sig is not None:
        headers["X-Forgejo-Signature"] = sig
    return c.post(f"/hooks/a/{aid}", content=body, headers=headers)


def test_a_signed_delivery_is_accepted_and_echoes_nothing(client):
    aid = _made()
    secret = store.new_hook_secret(aid)
    body = json.dumps(_pr()).encode()
    r = _post(client, aid, body, _sign(body, secret))
    assert r.status_code == 202 and r.json() == {"accepted": True}
    assert r.headers["cache-control"] == "no-store"
    assert client.handled[0][0] == aid and client.handled[0][1] == body


def test_every_refusal_is_the_same_404(client):
    aid = _made()
    secret = store.new_hook_secret(aid)
    off = _made(enabled=False)
    store.new_hook_secret(off)
    no_secret = _made()
    body = json.dumps(_pr()).encode()
    answers = [
        _post(client, "doesnotexist", body, _sign(body, secret)),
        _post(client, "BAD-ID!", body, _sign(body, secret)),
        _post(client, off, body, _sign(body, secret)),
        _post(client, no_secret, body, _sign(body, secret)),
        _post(client, aid, body, None),
        _post(client, aid, body, _sign(body, "wrong")),
        _post(client, aid, body + b" ", _sign(body, secret)),
    ]
    assert {(r.status_code, r.text) for r in answers} == {(404, '{"detail":"Not Found"}')}
    assert client.handled == []
    # The bad signatures for an existing automation are counted, never shown as runs.
    assert store.hook_state(aid)["unsigned_24h"] == 3
    assert _runs(aid) == []


def test_an_oversized_body_is_413_before_anything_is_looked_up(client):
    aid = _made()
    store.new_hook_secret(aid)
    r = _post(client, aid, b"x" * (forge_hook.BODY_MAX + 1), "0" * 64)
    assert r.status_code == 413


def test_the_kill_switch_refuses_every_delivery(client, monkeypatch):
    aid = _made()
    secret = store.new_hook_secret(aid)
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "0")
    body = json.dumps(_pr()).encode()
    assert _post(client, aid, body, _sign(body, secret)).status_code == 503
    assert client.handled == []


def test_deliveries_are_rate_limited_per_automation_after_the_signature(client, monkeypatch):
    aid = _made()
    secret = store.new_hook_secret(aid)
    monkeypatch.setattr(hooks._PER, "limit", 2)
    body = json.dumps(_pr()).encode()
    codes = [_post(client, aid, body, _sign(body, secret)).status_code for _ in range(3)]
    assert codes == [202, 202, 429]


def test_the_secret_route_shows_it_once():
    aid = _made()
    first = store.new_hook_secret(aid)
    pub = routes.public(store.get(aid))
    assert first not in json.dumps(pub)


def test_the_hook_route_binds_nothing_but_its_id(auth_cfg):
    """The unauthenticated route reads its body and headers itself; FastAPI binds only the id, and
    no auth dependency sneaks in (it has none by design)."""
    from fastapi.dependencies.utils import get_flat_dependant

    from agent_sessions.main import create_app

    app = create_app(auth_cfg)
    found = [r for r in app.routes if getattr(r, "path", "").startswith(hooks.PREFIX)]
    assert [r.path for r in found] == ["/hooks/a/{aid}"]
    flat = get_flat_dependant(found[0].dependant)
    assert not (flat.query_params or flat.body_params or flat.header_params or flat.cookie_params)
    assert {p.name for p in flat.path_params} == {"aid"}
    assert found[0].methods == {"POST"}


@pytest.mark.parametrize("head_repo", [REPO, "contributor/shop"])
def test_the_current_head_is_read_for_same_repo_and_fork_prs(head_repo):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == f"/api/v1/repos/{REPO}/pulls/41"
        return httpx.Response(
            200,
            json={
                "state": "open",
                "head": {"sha": SHA_A, "ref": "fix", "repo": {"full_name": head_repo}},
                "base": {"ref": "main", "repo": {"full_name": REPO}},
            },
        )

    forge.set_transport_for_test(httpx.MockTransport(handler))
    try:
        c = forge.ForgeClient(kind="forgejo", base_url="https://git.example.com")
        fact = c.review_pull(REPO, 41)
        assert fact.observed and fact.value and fact.extra == {"head_sha": SHA_A}
    finally:
        forge.set_transport_for_test(None)


def test_a_pr_targeting_another_repository_is_not_this_items():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": "open",
                "head": {"sha": SHA_A, "repo": {"full_name": REPO}},
                "base": {"repo": {"full_name": "example-org/other"}},
            },
        )

    forge.set_transport_for_test(httpx.MockTransport(handler))
    try:
        c = forge.ForgeClient(kind="forgejo", base_url="https://git.example.com")
        assert not c.review_pull(REPO, 41).observed
    finally:
        forge.set_transport_for_test(None)
