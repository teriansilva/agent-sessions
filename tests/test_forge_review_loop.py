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
    webhooks,
)
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions.routes import automations as routes
from agent_sessions.routes import hooks

#: The current test's list webhook: ``{"wid", "secret"}`` (set by `_world`).
WH: dict = {}

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
    # A webhook from the list (#2221) for the automations these tests make.
    pub, secret = webhooks.create("shop repo")
    WH.update(wid=pub["id"], secret=secret)
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
        "trigger": {"kind": "webhook", "webhook_id": WH["wid"]},
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
        "Starts when the webhook “shop repo”",
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
    pub, first = webhooks.create("ci", now=1000.0)
    wid = pub["id"]
    assert webhooks.secrets(wid, now=1000.0) == [first]
    assert first.encode() not in store.db_path().read_bytes()  # only the AES-GCM envelope
    second = webhooks.rotate(wid, now=2000.0)
    assert set(webhooks.secrets(wid, now=2000.0)) == {first, second}
    later = 2000.0 + webhooks.SECRET_OVERLAP_S + 1
    assert webhooks.secrets(wid, now=later) == [second]
    shown = webhooks.get(wid)
    assert shown["path"] == f"/hooks/w/{wid}" and shown["secret_set"]
    assert first not in json.dumps(shown) and second not in json.dumps(shown)


def test_a_shared_webhook_is_rotated_on_its_own_page_never_through_an_automation():
    aid = _made()
    with pytest.raises(store.StoreError, match="shared webhook"):
        store.new_hook_secret(aid)
    pub = routes.public(store.get(aid))["webhook"]
    assert pub["webhook_id"] == WH["wid"] and WH["secret"] not in json.dumps(pub)


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

    def handle(aid, body, headers, registry=None, source=None):
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


def _post(c, wid, body: bytes, sig: str | None, *, legacy: bool = False, header=None):
    headers = {"Content-Type": "application/json", "X-Forgejo-Event": "pull_request"}
    if sig is not None:
        name, prefix = header or ("X-Forgejo-Signature", "")
        headers[name] = prefix + sig
    base = "/hooks/a/" if legacy else "/hooks/w/"
    return c.post(f"{base}{wid}", content=body, headers=headers)


def _legacy(enabled: bool = True) -> tuple[str, str]:
    """A #2202 review loop as the v3 migration leaves it: its own legacy webhook (same id),
    a configuration in the old form, approved. Returns ``(automation id, secret)``."""
    cfg = model.validate_config({**_config(), "trigger": {"kind": "webhook", "scheme": "forgejo"}})
    row = store.create(cfg, model.compute_pins(cfg))
    aid = row["id"]
    import time as _t

    with store._tx() as con:
        con.execute(
            "INSERT INTO webhooks (id, name, revision, legacy_automation, created_at, updated_at) "
            "VALUES (?,?,1,?,?,?)",
            (aid, "old review loop", aid, _t.time(), _t.time()),
        )
        secret = webhooks._seal(con, aid, _t.time())
    if enabled:
        pub = routes.public(store.get(aid))
        routes._enable(
            aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
        )
    return aid, secret


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


def test_the_old_address_still_routes_to_its_own_automation_only(client):
    aid, secret = _legacy()
    other = _made()
    body = json.dumps(_pr()).encode()
    r = _post(client, aid, body, _sign(body, secret), legacy=True)
    assert r.status_code == 202
    assert [h[0] for h in client.handled] == [aid] and other not in [h[0] for h in client.handled]
    # It can't be picked by another automation.
    assert not webhooks.exists_pickable(aid)
    with pytest.raises(model.AutomationError, match="can't be picked"):
        automation_runner.check_save(
            model.validate_config({**_config(), "trigger": {"kind": "webhook", "webhook_id": aid}})
        )


def test_an_old_review_loops_config_needs_no_re_approval():
    aid, _secret = _legacy()
    row = store.get(aid)
    assert row["consented_scope"] == model.scope_of(row["config"], row["pins"])
    assert "webhook_source" not in row["consented_scope"]


def test_every_refusal_is_the_same_404(client):
    _made()
    aid_old, secret_old = _legacy(enabled=False)
    body = json.dumps(_pr()).encode()
    good = _sign(body, WH["secret"])
    answers = [
        _post(client, "doesnotexist", body, good),
        _post(client, "BAD-ID!", body, good),
        _post(client, aid_old, body, _sign(body, secret_old), legacy=True),  # disabled
        _post(client, aid_old, body, _sign(body, secret_old)),  # legacy isn't a list webhook
        _post(client, WH["wid"], body, None),
        _post(client, WH["wid"], body, _sign(body, "wrong")),
        _post(client, WH["wid"], body + b" ", good),
    ]
    assert {(r.status_code, r.text) for r in answers} == {(404, '{"detail":"Not Found"}')}
    assert client.handled == []
    # Bad signatures for an existing webhook are counted on it, never shown as runs.
    assert webhooks.get(WH["wid"])["unsigned_24h"] == 3


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
    cfg2 = model.validate_config(
        {**_config(), "trigger": {"kind": "webhook", "webhook_id": other["id"]}}
    )
    assert "a different webhook" in model.widened(
        scope, model.scope_of(cfg2, model.compute_pins(cfg2))
    )


def test_a_new_automation_cannot_use_the_old_per_automation_form():
    with pytest.raises(model.AutomationError, match="pick a webhook"):
        routes._create({**_config(), "trigger": {"kind": "webhook", "scheme": "forgejo"}})


def test_the_hook_route_binds_nothing_but_its_id(auth_cfg):
    """The unauthenticated route reads its body and headers itself; FastAPI binds only the id, and
    no auth dependency sneaks in (it has none by design)."""
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


# ==== PR 2: the return leg =======================================================================


def _review_payload(record, rev=SHA_A, verdict="changes", rtype=None, number=41, **over):
    marker = (
        f"<!-- battlelab-review: record={record} item=pr#{number} rev={rev} verdict={verdict} -->"
    )
    rtype = rtype or (
        "pull_request_review_approved" if verdict == "approve" else "pull_request_review_rejected"
    )
    return _pr(
        number=number,
        sha=rev,
        action="reviewed",
        review={"type": rtype, "content": f"Looks off.\n{marker}"},
        sender={"login": REVIEWER},
        **over,
    )


def _dispatched(aid, rev=SHA_A, item="pr#41", n=None):
    res = store.begin_review(
        aid,
        slot=f"review:{REPO}:{item}:{rev}",
        repo=REPO,
        item=item,
        revision=rev,
        expect_revision=store.get(aid)["revision"],
        inputs={"item": item, "revision": rev},
    )
    return res["record"]


def _returns(spawned):
    return [r for r in spawned if (r.get("inputs") or {}).get("leg") == "return"]


def test_an_accepted_verdict_is_journaled_before_any_send(spawned, forge_says):
    aid = _made()
    rec = _dispatched(aid)
    forge_says["pr#41"] = (SHA_A, True)
    _deliver(aid, _review_payload(rec))
    (run,) = _returns(spawned)
    assert run["state"] == "dispatching"
    assert run["inputs"]["verdict"] == "changes" and run["inputs"]["round"] == 1
    assert run["inputs"]["session_marker"] == "claude-api:abc-123"
    (r,) = store.review_records(aid, _key("pr#41"))
    assert r["state"] == "verdict_accepted" and r["verdict"] == "changes"
    # Review text never travels: only the typed facts.
    assert "Looks off" not in json.dumps(run["inputs"])


def test_a_second_verdict_for_the_same_review_changes_nothing(spawned, forge_says):
    aid = _made()
    rec = _dispatched(aid)
    forge_says["pr#41"] = (SHA_A, True)
    _deliver(aid, _review_payload(rec, verdict="approve"))
    # Approval then rejection for the same record, under another signed body: nothing at all.
    _deliver(aid, _review_payload(rec, verdict="changes"))
    assert len(_returns(spawned)) == 1
    assert store.review_records(aid, _key("pr#41"))[0]["verdict"] == "approve"


def test_a_verdict_for_a_superseded_review_is_ignored_as_stale(spawned, forge_says):
    aid = _made()
    old = _dispatched(aid, rev=SHA_A)
    _dispatched(aid, rev=SHA_B)  # a push dispatched B
    forge_says["pr#41"] = (SHA_B, True)
    _deliver(aid, _review_payload(old, rev=SHA_A))
    assert _returns(spawned) == []
    (run,) = [r for r in _runs(aid) if r["outcome"] == "ignored"]
    assert "superseded" in run["reason"]


def test_a_verdict_before_the_next_webhook_is_stale_because_the_forge_moved(spawned, forge_says):
    aid = _made()
    rec = _dispatched(aid, rev=SHA_A)
    forge_says["pr#41"] = (SHA_B, True)  # B was pushed; its webhook has not arrived yet
    _deliver(aid, _review_payload(rec, rev=SHA_A))
    assert _returns(spawned) == []
    assert "the forge says the current revision is" in _runs(aid)[0]["reason"]
    assert store.review_records(aid, _key("pr#41"))[0]["state"] == "dispatched"


@pytest.mark.parametrize(
    "payload_of, why",
    [
        (lambda rec: _review_payload("nosuchrecord"), "has no review nosuchrecord"),
        (lambda rec: _review_payload(rec, rev=SHA_B), "was for"),
        (
            lambda rec: _review_payload(
                rec, verdict="approve", rtype="pull_request_review_rejected"
            ),
            "submitted as rejected",
        ),
        (
            lambda rec: _review_payload(rec, rtype="pull_request_review_comment"),
            "comment-only review",
        ),
    ],
)
def test_verdicts_that_do_not_bind_to_a_dispatched_review_are_ignored(
    payload_of, why, spawned, forge_says
):
    aid = _made()
    rec = _dispatched(aid)
    forge_says["pr#41"] = (SHA_A, True)
    forge_says.setdefault("pr#41", (SHA_A, True))
    if "was for" in why:
        forge_says["pr#41"] = (SHA_B, True)
    _deliver(aid, payload_of(rec))
    assert _returns(spawned) == []
    assert any(why in r["reason"] for r in _runs(aid)), [r["reason"] for r in _runs(aid)]


def test_a_reviewer_post_without_a_marker_is_chatter(spawned, forge_says):
    aid = _made()
    _dispatched(aid)
    payload = _pr(
        action="reviewed",
        review={"type": "pull_request_review_rejected", "content": "no marker here"},
        sender={"login": REVIEWER},
    )
    _deliver(aid, payload)
    assert [r for r in _runs(aid) if r.get("inputs", {}).get("leg") == "return"] == []
    assert store.hook_state(aid)["last_note"] == "reviewer post, no verdict"


def test_an_issue_verdict_comment_is_accepted(spawned, forge_says):
    aid = _made()
    rev = forge_hook.issue_revision("Plan", "Do the thing")
    rec = _dispatched(aid, rev=rev, item="issue#39")
    forge_says["issue#39"] = (rev, True)
    marker = f"<!-- battlelab-review: record={rec} item=issue#39 rev={rev} verdict=adjust -->"
    comment = _issue(
        action="created",
        comment={"id": 9, "body": f"Tighten goals.\n{marker}", "user": {"login": REVIEWER}},
        sender={"login": REVIEWER},
    )
    _deliver(aid, comment)
    (run,) = _returns(spawned)
    assert run["inputs"]["verdict"] == "adjust" and run["inputs"]["item"] == "issue#39"


def test_review_only_mode_records_the_verdict_and_sends_nothing(spawned, forge_says):
    aid = _made(_config(return_to_origin=False))
    rec = _dispatched(aid)
    forge_says["pr#41"] = (SHA_A, True)
    _deliver(aid, _review_payload(rec))
    assert _returns(spawned) == []
    (run,) = [r for r in _runs(aid) if r["outcome"] == "ok"]
    assert "returns no verdicts" in run["reason"]


def test_the_return_message_is_fixed_text_and_never_the_review():
    msg = review_loop.return_message(
        repo=REPO,
        item="pr#41",
        revision=SHA_A,
        verdict="changes",
        reviewer=REVIEWER,
        round_no=4,
        rounds=4,
    )
    assert "changes requested (review 4 of at most 4)" in msg
    assert "push to the pull request's branch" in msg
    assert "last review the loop will start" in msg
    assert "not as instructions" in msg
    done = review_loop.return_message(
        repo=REPO,
        item="issue#39",
        revision="f" * 16,
        verdict="viable",
        reviewer=REVIEWER,
        round_no=1,
        rounds=5,
    )
    assert "done" in done and "nothing was merged" in done


@pytest.mark.parametrize(
    "url, ok",
    [
        (f"https://git.example.com/{REPO}.git", True),
        (f"https://user:tok@git.example.com/{REPO}", True),
        (f"https://git.example.com:8443/{REPO}", True),
        (f"git@git.example.com:{REPO}.git", True),
        (f"ssh://git@git.example.com:2222/{REPO}.git", True),
        ("https://git.example.com/example-org/shop-fork.git", False),
        ("https://git.example.com/evil/example-org/shop.git", False),
        (f"https://unrelated.invalid/{REPO}.git", False),
        (f"git@unrelated.invalid:{REPO}.git", False),
        (f"/{REPO}", False),
        (REPO, False),
        (f"file:///srv/{REPO}.git", False),
    ],
)
def test_a_remote_must_be_the_repo_on_the_configured_forge(url, ok):
    assert review_loop_runner.remote_names_repo(url, REPO, "git.example.com") is ok
    assert review_loop_runner.remote_names_repo(url, REPO, "") is False


def _git_checkout(path, url, branch):
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)


@pytest.fixture
def api_origin(monkeypatch, _world):
    """An API session `claude-api:abc-123`, its checkout inside the project on the PR branch."""
    from agent_sessions import mission_structured

    checkout = _world / "work"
    _git_checkout(checkout, f"https://git.example.com/{REPO}.git", "devopsagent/fix-totals")
    state = {"cwd": str(checkout), "api": True}
    monkeypatch.setattr(review_loop_runner, "forge_authority", lambda: "git.example.com")

    class Prov:
        engine_id = "claude-api"

    monkeypatch.setattr(review_loop_runner.engines, "parse_key", lambda k: (Prov(), "abc-123"))
    monkeypatch.setattr(mission_structured, "is_api", lambda _p: state["api"])

    async def target(_key):
        return {"cwd": state["cwd"], "state": "idle", "live": True}

    monkeypatch.setattr(mission_structured, "automation_target", target)
    return state


def _pins(folder) -> dict:
    return {"cwd": {"real": str(folder)}}


def _resolve(inputs, folder):
    action = model.validate_action(_config()["action"])
    return asyncio.run(review_loop_runner.resolve_origin(inputs, action, _pins(folder)))


PR_INPUTS = {
    "item": "pr#41",
    "head_ref": "devopsagent/fix-totals",
    "session_marker": "claude-api:abc-123",
}


def test_the_origin_is_the_marked_api_session_on_the_prs_branch(api_origin, _world):
    assert _resolve(PR_INPUTS, _world) == ("claude-api:abc-123", "", False)


def test_a_terminal_origin_gets_nothing(api_origin, _world):
    api_origin["api"] = False
    key, why, terminal = _resolve(PR_INPUTS, _world)
    assert terminal and "terminal session" in why


def test_an_origin_outside_the_project_or_on_another_branch_is_refused(
    api_origin, _world, tmp_path
):
    other = tmp_path / "elsewhere"
    _git_checkout(other, f"https://git.example.com/{REPO}.git", "devopsagent/fix-totals")
    api_origin["cwd"] = str(other)
    assert "outside the review loop's project" in _resolve(PR_INPUTS, _world)[1]
    api_origin["cwd"] = str(_world / "work")
    assert (
        "not on devopsagent/other"
        in _resolve({**PR_INPUTS, "head_ref": "devopsagent/other"}, _world)[1]
    )
    fork = _world / "fork"
    _git_checkout(fork, "https://git.example.com/someone/shop.git", "devopsagent/fix-totals")
    api_origin["cwd"] = str(fork)
    assert "not a clone of" in _resolve(PR_INPUTS, _world)[1]


def test_without_a_marker_one_branch_match_is_used_and_two_are_ambiguous(
    api_origin, _world, monkeypatch
):
    no_marker = {**PR_INPUTS, "session_marker": ""}
    monkeypatch.setattr(review_loop_runner, "_branch_candidates", lambda *a: ["claude-api:abc-123"])
    assert _resolve(no_marker, _world)[0] == "claude-api:abc-123"
    monkeypatch.setattr(
        review_loop_runner, "_branch_candidates", lambda *a: ["claude-api:a", "claude-api:b"]
    )
    key, why, _t = _resolve(no_marker, _world)
    assert key is None and "2 API sessions" in why
    monkeypatch.setattr(review_loop_runner, "_branch_candidates", lambda *a: [])
    assert "no session record" in _resolve(no_marker, _world)[1]


def test_an_issue_origin_comes_from_its_newest_session_record(api_origin, _world, monkeypatch):
    class Client:
        def issue_session_marker(self, repo, number):
            return forge.Fact.seen(True, "", session="claude-api:abc-123")

    monkeypatch.setattr(review_loop_runner, "forge_client", lambda *a: (Client(), ""))
    key, why, _t = _resolve({"item": "issue#39", "session_marker": "ignored:x"}, _world)
    assert (key, why) == ("claude-api:abc-123", "")


def _accepted_return_run(aid, forge_says, spawned, verdict="changes"):
    rec = _dispatched(aid)
    forge_says["pr#41"] = (SHA_A, True)
    _deliver(aid, _review_payload(rec, verdict=verdict))
    (run,) = _returns(spawned)
    return run


def test_the_verdict_is_sent_as_one_guarded_api_turn(spawned, forge_says, monkeypatch):
    aid = _made()
    run = _accepted_return_run(aid, forge_says, spawned)
    sent: dict = {}

    async def resolve(inputs, action, pins):
        return "claude-api:abc-123", "", False

    async def send(r, cfg, pins, **_kw):
        sent.update(cfg["action"])
        automation_runner._delivered(r["id"], "ok", "delivered as a structured turn")
        return "ok", "delivered as a structured turn"

    monkeypatch.setattr(review_loop_runner, "resolve_origin", resolve)
    monkeypatch.setattr(automation_runner, "_send_structured", send)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "ok"
    assert sent["session_key"] == "claude-api:abc-123"
    assert "changes requested" in sent["message"]["text"]
    assert "Looks off" not in sent["message"]["text"]


def test_an_undelivered_verdict_is_announced_once_and_never_counted(
    spawned, forge_says, monkeypatch
):
    aid = _made()
    run = _accepted_return_run(aid, forge_says, spawned)
    announced: list = []

    async def resolve(inputs, action, pins):
        return "claude:terminal", "it is a terminal session: nothing unattended is typed", True

    monkeypatch.setattr(review_loop_runner, "resolve_origin", resolve)
    monkeypatch.setattr(automation_runner, "_announce", lambda *a, **k: announced.append(a))
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "undelivered"
    assert "open that session and tell it yourself" in res["run"]["reason"]
    assert len(announced) == 1 and announced[0][0].startswith("Review verdict not delivered")
    assert store.get(aid)["consecutive_failures"] == 0


def test_a_busy_origin_is_undelivered_not_retried(spawned, forge_says, monkeypatch):
    aid = _made()
    run = _accepted_return_run(aid, forge_says, spawned)

    async def resolve(inputs, action, pins):
        return "claude-api:abc-123", "", False

    async def send(r, cfg, pins, **_kw):
        return "refused", "the session was busy"

    monkeypatch.setattr(review_loop_runner, "resolve_origin", resolve)
    monkeypatch.setattr(automation_runner, "_send_structured", send)
    monkeypatch.setattr(automation_runner, "_announce", lambda *a, **k: None)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "undelivered" and "busy" in res["run"]["reason"]


def test_a_crash_between_acceptance_and_send_is_interrupted_announced_and_never_resent(
    spawned, forge_says, monkeypatch
):
    from agent_sessions import automation_loop

    aid = _made()
    run = _accepted_return_run(aid, forge_says, spawned)
    # The owner process is gone: recovery marks it interrupted, and nothing replays it.
    monkeypatch.setattr(store, "owner_alive", lambda _t: False)
    ids = store.recover_interrupted()
    assert run["id"] in ids  # (the admitted review's own run is recovered too, silently)
    announced: list = []
    monkeypatch.setattr(automation_runner, "_announce", lambda *a, **k: announced.append(a))
    automation_loop._announce_interrupted_returns(ids)
    assert len(announced) == 1 and "may not have been delivered" in announced[0][0]
    assert store.get_run(run["id"])["outcome"] == "interrupted"
    # The review stays counted and the record stays answered: a redelivery sends nothing.
    _deliver(aid, _review_payload(store.review_records(aid, _key("pr#41"))[0]["id"]))
    assert len(_returns(spawned)) == 1
    assert store.review_count(aid, _key("pr#41")) == 1


def test_a_capped_item_raises_one_bell_row(spawned, forge_says, monkeypatch):
    aid = _made(_config(max_rounds={"pr": 1}))
    announced: list = []
    monkeypatch.setattr(automation_runner, "_announce", lambda *a, **k: announced.append(a))
    monkeypatch.setattr(automation_runner, "_in_bell", lambda k: any(x[2] == k for x in announced))
    for sha in (SHA_A, SHA_B, "c" * 40):
        forge_says["pr#41"] = (sha, True)
        _deliver(aid, _pr(sha=sha))
    assert len(spawned) == 1
    assert len(announced) == 1 and "reached its limit" in announced[0][0]


def test_the_forge_reads_an_issues_newest_session_record_without_its_text(monkeypatch):
    import httpx

    old = "<!-- agent-session: engine=claude-api id=old-1 -->"
    new = "<!-- agent-session-author: engine=claude-api id=new-2 -->"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/repos/{REPO}/issues/39/comments"
        return httpx.Response(200, json=[{"body": old}, {"body": "chat"}, {"body": new}])

    forge.set_transport_for_test(httpx.MockTransport(handler))
    try:
        c = forge.ForgeClient(kind="forgejo", base_url="https://git.example.com")
        fact = c.issue_session_marker(REPO, 39)
        assert fact.observed and fact.extra == {"session": "claude-api:new-2"}
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


def test_verdict_acceptance_is_bound_to_the_classified_revision_and_repository():
    aid = _made()
    rec = _dispatched(aid)
    seen = store.get(aid)["revision"]
    pub = routes.public(store.get(aid))
    routes._patch(aid, {"revision": pub["revision"], "name": "renamed"})
    res = store.accept_verdict(
        aid,
        slot=f"verdict:{REPO}:pr:41:{rec}",
        repo=REPO,
        expect_revision=seen,
        record=rec,
        item="pr#41",
        revision=SHA_A,
        verdict="changes",
        current=SHA_A,
        inputs={},
    )
    assert res.get("stale_config") and res["run"] is None
    assert store.review_records(aid, _key())[0]["state"] == "dispatched"
    # Another repository's item never matches this record, whatever its number.
    res = store.accept_verdict(
        aid,
        slot=f"verdict:example-org/other:pr:41:{rec}",
        repo="example-org/other",
        expect_revision=store.get(aid)["revision"],
        record=rec,
        item="pr#41",
        revision=SHA_A,
        verdict="changes",
        current=SHA_A,
        inputs={},
    )
    assert res.get("stale_config")  # the automation is not for that repository at all


def test_a_fork_prs_origin_may_be_a_clone_of_the_fork(api_origin, _world):
    fork = _world / "forkwork"
    _git_checkout(fork, "https://git.example.com/contributor/shop.git", "devopsagent/fix-totals")
    api_origin["cwd"] = str(fork)
    assert "not a clone of" in _resolve(PR_INPUTS, _world)[1]
    with_fork = {**PR_INPUTS, "head_repo": "contributor/shop"}
    assert _resolve(with_fork, _world) == ("claude-api:abc-123", "", False)


def test_parse_reads_the_head_repository_and_refuses_a_malformed_one():
    p = _pr()
    p["pull_request"]["head"]["repo"] = {"full_name": "contributor/shop"}
    assert forge_hook.parse(json.dumps(p).encode()).head_repo == "contributor/shop"
    p["pull_request"]["head"]["repo"] = {"full_name": "a/b/c"}
    with pytest.raises(forge_hook.Rejected, match="head repository"):
        forge_hook.parse(json.dumps(p).encode())


def test_a_checkout_on_another_server_never_fits(api_origin, _world):
    elsewhere = _world / "mirror"
    _git_checkout(elsewhere, f"https://unrelated.invalid/{REPO}.git", "devopsagent/fix-totals")
    api_origin["cwd"] = str(elsewhere)
    assert "not a clone of" in _resolve(PR_INPUTS, _world)[1]


@pytest.mark.parametrize("item", ["pr#41", "issue#39"])
def test_a_late_verdict_on_a_closed_item_is_ignored(item, spawned, forge_says):
    aid = _made()
    if item == "pr#41":
        rec = _dispatched(aid)
        forge_says[item] = (SHA_A, False)  # merged or closed since
        _deliver(aid, _review_payload(rec))
    else:
        rev = forge_hook.issue_revision("Plan", "Do the thing")
        rec = _dispatched(aid, rev=rev, item=item)
        forge_says[item] = (rev, False)
        marker = f"<!-- battlelab-review: record={rec} item={item} rev={rev} verdict=adjust -->"
        _deliver(
            aid,
            _issue(
                action="created",
                comment={"id": 9, "body": marker, "user": {"login": REVIEWER}},
                sender={"login": REVIEWER},
            ),
        )
    assert _returns(spawned) == []
    assert any("is closed on the forge" in r["reason"] for r in _runs(aid))
    assert store.review_records(aid, _key(item))[0]["state"] == "dispatched"


def test_a_crash_inside_the_real_send_keeps_the_journal_and_announces_it(
    spawned, forge_says, monkeypatch, _world
):
    """Through the REAL `_send_structured`: the transport dies mid-send. The run's journal still
    names the return leg, so recovery announces it — and it is never resent."""
    from agent_sessions import automation_loop, mission_structured

    aid = _made()
    run = _accepted_return_run(aid, forge_says, spawned)

    class Prov:
        engine_id = "claude-api"

    async def resolve(inputs, action, pins):
        return "claude-api:abc-123", "", False

    async def target(_key):
        return {"cwd": str(_world), "state": "idle", "live": True}

    async def send(**kw):
        raise asyncio.CancelledError  # the app stops while the turn is in flight

    monkeypatch.setattr(review_loop_runner, "resolve_origin", resolve)
    monkeypatch.setattr(automation_runner.engines, "parse_key", lambda k: (Prov(), "abc-123"))
    monkeypatch.setattr(automation_runner, "target_state", lambda _e: (True, ""))
    monkeypatch.setattr(automation_runner, "scope_ok", lambda _c: True)
    monkeypatch.setattr(mission_structured, "automation_target", target)
    monkeypatch.setattr(mission_structured, "automation_send", send)
    announced: list = []
    monkeypatch.setattr(automation_runner, "_announce", lambda *a, **k: announced.append(a))
    monkeypatch.setattr(automation_runner, "_in_bell", lambda k: any(x[2] == k for x in announced))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(automation_runner.execute(run, registry=None))
    after = store.get_run(run["id"])
    assert after["outcome"] == "interrupted"
    assert after["inputs"]["leg"] == "return" and after["inputs"]["item"] == "pr#41"
    assert "changes requested" in after["inputs"]["message"]  # the masked message rides along
    # Announced by the CANCELLATION path itself — orphan recovery never sees a settled run.
    assert len(announced) == 1 and "may not have been delivered" in announced[0][0]
    assert run["id"] not in store.recover_interrupted()
    automation_loop._announce_interrupted_returns([run["id"]])  # and never twice
    assert len(announced) == 1


def _comment_pages(pages):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=pages[page - 1] if page <= len(pages) else [])

    return httpx.MockTransport(handler)


def test_the_newest_session_record_is_read_across_pages():
    full = forge.ForgeClient.COMMENT_PAGE
    old = {"body": "<!-- agent-session: engine=claude-api id=old-1 -->"}
    new = {"body": "<!-- agent-session: engine=claude-api id=new-2 -->"}
    pages = [[old] + [{"body": "x"}] * (full - 1), [{"body": "y"}, new]]
    forge.set_transport_for_test(_comment_pages(pages))
    try:
        c = forge.ForgeClient(kind="forgejo", base_url="https://git.example.com")
        assert c.issue_session_marker(REPO, 39).extra == {"session": "claude-api:new-2"}
    finally:
        forge.set_transport_for_test(None)


def test_a_thread_longer_than_the_bound_fails_closed():
    full = forge.ForgeClient.COMMENT_PAGE
    old = {"body": "<!-- agent-session: engine=claude-api id=old-1 -->"}
    pages = [[old] + [{"body": "x"}] * (full - 1)] + [[{"body": "x"}] * full] * 4
    pages.append([{"body": "<!-- agent-session: engine=claude-api id=takeover -->"}])
    forge.set_transport_for_test(_comment_pages(pages))
    try:
        c = forge.ForgeClient(kind="forgejo", base_url="https://git.example.com")
        fact = c.issue_session_marker(REPO, 39)
        assert not fact.observed and "too many comments" in fact.detail
    finally:
        forge.set_transport_for_test(None)


def test_a_forge_that_ignores_paging_returns_the_whole_thread_at_once():
    full = forge.ForgeClient.COMMENT_PAGE
    thread = [{"body": "x"}] * (full + 20) + [
        {"body": "<!-- agent-session: engine=claude-api id=last -->"}
    ]
    forge.set_transport_for_test(_comment_pages([thread] * 9))
    try:
        c = forge.ForgeClient(kind="forgejo", base_url="https://git.example.com")
        assert c.issue_session_marker(REPO, 39).extra == {"session": "claude-api:last"}
    finally:
        forge.set_transport_for_test(None)


def test_a_verdict_respects_the_daily_cap_and_can_be_redelivered(spawned, forge_says, monkeypatch):
    aid = _made()
    rec = _dispatched(aid)
    forge_says["pr#41"] = (SHA_A, True)
    real = store._eligibility
    monkeypatch.setattr(store, "_eligibility", lambda *a, **k: ("skip", "skipped: daily cap"))
    _deliver(aid, _review_payload(rec))
    assert _returns(spawned) == []
    assert any(r["outcome"] == "skipped" and r["inputs"].get("leg") == "return" for r in _runs(aid))
    assert store.review_records(aid, _key())[0]["state"] == "dispatched"
    monkeypatch.setattr(store, "_eligibility", real)
    _deliver(aid, _review_payload(rec))  # redelivered once eligible: accepted now
    assert len(_returns(spawned)) == 1


def test_an_undelivered_alert_survives_a_lost_settlement_acknowledgement(spawned, forge_says):
    aid = _made()
    run = _accepted_return_run(aid, forge_says, spawned)
    first = store.finish_run(run["id"], "undelivered", "no session record")
    assert first["alert"] == f"automation:{aid}:{run['id']}:undelivered"
    again = store.finish_run(run["id"], "undelivered", "no session record")
    assert again["settled"] is False and not again.get("alert")
    owed = automation_runner._lost_ack(again, run["id"])
    assert owed["alert"] == f"automation:{aid}:{run['id']}:undelivered"
    assert store.get(aid)["consecutive_failures"] == 0


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


def test_a_delivery_never_runs_under_a_webhook_it_did_not_come_through(spawned, forge_says):
    aid = _made()
    old = WH["wid"]
    new, _s = webhooks.create("other")
    forge_says["pr#41"] = (SHA_A, True)
    # Routed to `aid` through `old`; before the handler runs, the operator moves `aid` to `new`
    # and approves it.
    _switch_to(aid, new["id"])
    asyncio.run(
        review_loop_runner.handle(aid, json.dumps(_pr()).encode(), {}, registry=None, source=old)
    )
    assert spawned == [] and _runs(aid) == []
    # Through the webhook it now listens on, the same delivery runs.
    asyncio.run(
        review_loop_runner.handle(
            aid, json.dumps(_pr()).encode(), {}, registry=None, source=new["id"]
        )
    )
    assert len(spawned) == 1


def test_admission_refuses_a_delivery_from_another_webhook():
    aid = _made()
    other, _s = webhooks.create("other")
    res = store.begin_review(
        aid,
        slot="review:x",
        repo=REPO,
        item="pr#41",
        revision=SHA_A,
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
    cfg = model.validate_config(
        {**_config(), "trigger": {"kind": "webhook", "webhook_id": gone["id"]}}
    )
    # Created (or retargeted) after the deletion: refused inside the writing transaction.
    with pytest.raises(store.StoreError, match="doesn't exist"):
        store.create(cfg, model.compute_pins(cfg))
    aid = _made()
    with pytest.raises(store.StoreError, match="doesn't exist"):
        store.mutate(aid, lambda row: {"config": cfg})
    # Deleted after a reference exists: refused inside the deleting transaction.
    with pytest.raises(webhooks.WebhookError, match="used by an automation"):
        webhooks.delete(WH["wid"])


def test_deleting_an_old_review_loop_deletes_its_own_webhook_and_secret():
    aid, secret = _legacy(enabled=False)
    shared_user = _made(enabled=False)
    assert webhooks.secrets(aid) == [secret]
    store.delete(aid, expect_revision=store.get(aid)["revision"])
    assert webhooks.secrets(aid) == []
    with pytest.raises(store.NotFound):
        webhooks.get(aid)
    # A shared webhook used by another automation stays.
    assert webhooks.get(WH["wid"])["used_by"] == [{"id": shared_user, "name": "Review loop"}]


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
