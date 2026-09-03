"""The read-only forge adapter (#891, Phase 5b of #840).

The property this file exists to hold is the FIRST test: **there is no write method**. Not "we do
not call one" — the class does not define one. An adapter that could merge a PR is one bug away
from merging one, and it runs unattended on a timer; the narrowness is the safety property, and a
helpful `post()` added later would remove it silently, so this is the thing that has to fail.

Everything else here is about the three-way answer. `unknown` is not `False`: a forge that is down,
that refused the credential, or that answered a shape we do not understand has told us NOTHING
about the work, and reporting that as "the PR is not there" would make an outage look like a
mission going backwards.
"""

from __future__ import annotations

import httpx
import pytest

from agent_sessions import forge, mission_probes


@pytest.fixture(autouse=True)
def _no_network():
    """CI never touches the network: every test installs its own transport."""
    yield
    forge.set_transport_for_test(None)


def _client(handler, **kw):
    forge.set_transport_for_test(httpx.MockTransport(handler))
    return forge.ForgeClient(
        kind=kw.pop("kind", "forgejo"),
        base_url=kw.pop("base_url", "https://git.example/"),
        token=kw.pop("token", "t0ken"),
        owner=kw.pop("owner", "acme"),
    )


def _json(payload, status=200):
    return lambda req: httpx.Response(status, json=payload)


# ---- the boundary ---------------------------------------------------------------


def test_the_adapter_has_no_write_method_at_all():
    """Asserted BY NAME over the public surface, not by "we checked the call sites".

    A read-only client is only read-only while nobody adds a convenience. This enumerates what is
    actually callable, so the failure arrives at the moment somebody does.
    """
    public = {n for n in dir(forge.ForgeClient) if not n.startswith("_")}
    # The reads plus its own constants, and nothing else. Constants are named explicitly rather
    # than filtered by `isupper()`: a filter would let a future `SUBMIT_REVIEW = ...` through
    # exactly the door this test exists to hold shut.
    assert public == {
        "KINDS",
        "PR_PAGE",
        "PR_MAX_PAGES",
        "GITHUB_AUTHORITATIVE",
        "pull_request",
        "checks",
        "review",
        "merged",
        "run",
    }
    forbidden = {
        "post",
        "put",
        "patch",
        "delete",
        "merge",
        "comment",
        "approve",
        "create",
        "update",
        "close",
        "dismiss",
        "label",
        "write",
        "submit",
    }
    assert not (public & forbidden)
    # …and not privately either: a `_post` would be one `self._post(...)` away from a write.
    assert not any(n.lstrip("_") in forbidden for n in dir(forge.ForgeClient))


def test_a_credential_never_reaches_a_url_or_an_error():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(500, text="boom")

    c = _client(handler, token="s3cret")
    f = c.pull_request("acme/app", "topic")
    assert f.observed is False
    # The token rides in a HEADER and nowhere else — never a query parameter, which proxies log.
    assert "s3cret" not in str(seen[0].url)
    assert seen[0].headers.get("authorization") == "token s3cret"
    # …and it is not echoed back out in the reason the operator reads.
    assert "s3cret" not in f.detail


def test_a_refused_credential_is_UNKNOWN_not_a_false_fact():
    c = _client(lambda r: httpx.Response(403, json={}))
    f = c.pull_request("acme/app", "topic")
    assert f.observed is False
    assert "credential" in f.detail
    # `value` is meaningless on an unobserved fact, and every caller checks `observed` first.
    assert f.value is False


def test_an_unreachable_forge_is_UNKNOWN():
    def boom(req):
        raise httpx.ConnectError("nope", request=req)

    c = _client(boom)
    f = c.pull_request("acme/app", "topic")
    assert f.observed is False
    assert "unreachable" in f.detail


def test_a_bad_base_url_is_refused_at_construction():
    for bad in ("", "ftp://x/y", "not a url", "file:///etc/passwd"):
        with pytest.raises(forge.ForgeError):
            forge.ForgeClient(kind="forgejo", base_url=bad)


def test_an_unknown_kind_is_refused_rather_than_guessed():
    """Guessing at a response shape is how a probe reports a confident wrong answer."""
    with pytest.raises(forge.ForgeError):
        forge.ForgeClient(kind="bitbucket", base_url="https://x/")


def test_a_repo_name_cannot_escape_its_path_segment():
    seen: list[str] = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=[])

    c = _client(handler)
    c.pull_request("../../admin/secrets", "topic")
    assert "/../" not in seen[0]
    assert "%2F" in seen[0] or "..%2F" in seen[0]


# ---- the reads ------------------------------------------------------------------


def test_an_open_pr_for_the_branch_is_found_with_its_head():
    rows = [
        {"number": 7, "head": {"ref": "topic", "sha": "abc123", "repo": {"full_name": "acme/app"}}}
    ]
    c = _client(_json(rows))
    f = c.pull_request("acme/app", "topic")
    assert f.observed and f.value
    assert f.extra["number"] == 7 and f.extra["head_sha"] == "abc123"


def test_a_pr_for_a_DIFFERENT_branch_is_not_this_objective_s_pr():
    c = _client(
        _json(
            [
                {
                    "number": 7,
                    "head": {"ref": "other", "sha": "abc", "repo": {"full_name": "acme/app"}},
                }
            ]
        )
    )
    f = c.pull_request("acme/app", "topic")
    assert f.observed and f.value is False


def test_an_EMPTY_check_rollup_is_unknown_rather_than_green():
    """The case this install actually hits: Forgejo Actions post no commit statuses at all.

    "Nothing reported" is not "everything passed". Reading an empty rollup as success would let a
    mission's most important gate settle on an absence.
    """
    c = _client(_json({"state": "", "total_count": 0}))
    f = c.checks("acme/app", "abc123")
    assert f.observed is False
    assert "no checks" in f.detail


def test_a_green_rollup_settles_and_a_pending_one_does_not():
    assert _client(_json({"state": "success", "total_count": 3})).checks("r", "s").value is True
    pending = _client(_json({"state": "pending", "total_count": 3})).checks("r", "s")
    assert pending.observed and pending.value is False


def test_a_review_only_counts_at_the_CURRENT_head():
    """An approval of an earlier revision is not an approval of what is about to merge."""
    rows = [
        {"state": "APPROVED", "commit_id": "old", "official": True},
        {"state": "REQUEST_CHANGES", "commit_id": "head", "official": True},
    ]
    f = _client(_json(rows)).review("acme/app", 7, "head")
    assert f.observed and f.value is False
    # …and the stale approval does not leak through when nothing matches the head at all.
    stale = [{"state": "APPROVED", "commit_id": "old", "official": True}]
    f2 = _client(_json(stale)).review("acme/app", 7, "head")
    assert f2.observed and f2.value is False


def test_an_approval_at_the_head_settles():
    rows = [{"state": "APPROVED", "commit_id": "head", "official": True}]
    f = _client(_json(rows)).review("acme/app", 7, "head")
    assert f.observed and f.value is True


def test_merged_reads_the_MERGED_flag_not_the_closed_state():
    """A closed-unmerged PR is a real, different outcome — and not one to keep nudging."""
    closed = _client(_json({"state": "closed", "merged": False})).merged("acme/app", 7)
    assert closed.observed and closed.value is False
    assert (closed.extra or {}).get("closed") is True
    merged = _client(_json({"state": "closed", "merged": True, "merge_commit_sha": "z"}))
    assert merged.merged("acme/app", 7).value is True


def test_an_unexpected_shape_is_unknown():
    c = _client(_json({"not": "a list"}))
    assert c.pull_request("acme/app", "topic").observed is False


def test_github_sends_a_bearer_token_and_no_api_v1_prefix():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=[])

    forge.set_transport_for_test(httpx.MockTransport(handler))
    c = forge.ForgeClient(
        kind="github", base_url="https://api.github.com", token="gh", owner="acme"
    )
    c.pull_request("acme/app", "topic")
    assert seen[0].headers.get("authorization") == "Bearer gh"
    assert "/api/v1/" not in str(seen[0].url)


# ---- config ---------------------------------------------------------------------


def test_the_prefs_kind_list_and_the_adapter_agree():
    """Two lists, one truth. `prefs` deliberately does not import the HTTP module — so the thing
    that keeps them in step has to be a test, or a kind an operator can save becomes a kind the
    adapter refuses at construction and every probe answers `unknown` for ever."""
    from agent_sessions import prefs

    assert tuple(prefs.FORGE_KINDS) == tuple(forge.ForgeClient.KINDS)


def test_the_token_never_appears_in_the_public_view(tmp_path, monkeypatch):
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_forge({"enabled": True, "base_url": "https://git.example", "token": "s3cret"})
    pub = prefs.public_forge()
    assert "token" not in pub
    assert pub["token_set"] is True
    assert pub["configured"] is True
    assert "s3cret" not in repr(pub)
    # …and the server-side view DOES have it, or the probes could not authenticate.
    assert prefs.get_forge()["token"] == "s3cret"


def test_an_empty_token_PRESERVES_and_null_CLEARS(tmp_path, monkeypatch):
    """The three-way contract the Settings form depends on. Getting it backwards means a form that
    round-trips a masked value silently erases a working credential."""
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_forge({"base_url": "https://git.example", "token": "keepme"})
    prefs.set_forge({"owner": "acme", "token": ""})
    assert prefs.get_forge()["token"] == "keepme"
    prefs.set_forge({"token": prefs.AI_REVIEW_KEY_MASK})
    assert prefs.get_forge()["token"] == "keepme"
    prefs.set_forge({"token": None})
    assert prefs.get_forge()["token"] == ""


def test_configured_does_not_require_a_token(tmp_path, monkeypatch):
    """A public forge is readable without one, and demanding a credential the probes do not need
    would turn a working setup into a permanent `unknown`."""
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_forge({"enabled": True, "base_url": "https://git.example"})
    assert prefs.public_forge()["configured"] is True


def test_a_bad_forge_patch_is_refused_rather_than_coerced():
    from agent_sessions import prefs

    assert prefs.validate_forge_patch({"base_url": "ftp://x/"})
    assert prefs.validate_forge_patch({"kind": "bitbucket"})
    assert prefs.validate_forge_patch({"enabled": "yes"})
    # An unknown key is REJECTED, not ignored: an ignored key is how a typo silently no-ops and
    # the operator concludes the setting does not work.
    assert prefs.validate_forge_patch({"basurl": "https://x"})
    assert prefs.validate_forge_patch({"base_url": "https://git.example"}) is None


def test_a_malformed_stored_block_degrades_to_not_configured(tmp_path, monkeypatch):
    """An unreadable forge setting makes the probes answer `unknown`; it must not raise into the
    supervisor sweep."""
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    assert prefs._coerce_forge("not a dict")["enabled"] is False
    assert prefs._coerce_forge({"kind": 7, "base_url": None})["kind"] == "forgejo"


# ---- #897 review: credentials belong to ONE authority -------------------------------------


def test_changing_the_HOST_drops_a_retained_token(tmp_path, monkeypatch):
    """Editing the endpoint must not silently re-point an existing credential.

    The form submits the edited `base_url` and omits an unchanged token; without this the next
    save would send the operator's forge token to whatever host was typed.
    """
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_forge({"enabled": True, "base_url": "https://git.example", "token": "s3cret"})
    prefs.set_forge({"base_url": "https://evil.example"})
    assert prefs.get_forge()["token"] == ""


def test_a_PATH_change_is_not_an_authority_change(tmp_path, monkeypatch):
    """Otherwise a trailing slash throws the operator's credential away."""
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_forge({"base_url": "https://git.example", "token": "keep"})
    prefs.set_forge({"base_url": "https://git.example/"})
    assert prefs.get_forge()["token"] == "keep"


def test_a_token_supplied_WITH_the_new_host_is_kept(tmp_path, monkeypatch):
    """ "This credential, that host" is the operator saying it explicitly, and is fine."""
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_forge({"base_url": "https://a.example", "token": "old"})
    prefs.set_forge({"base_url": "https://b.example", "token": "new"})
    assert prefs.get_forge()["token"] == "new"


def test_a_token_is_never_kept_on_a_PLAINTEXT_endpoint(tmp_path, monkeypatch):
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_forge({"base_url": "http://git.example", "token": "s3cret"})
    assert prefs.get_forge()["token"] == ""
    # …and loopback IS allowed, narrowly and by name: a forge on 127.0.0.1 has no wire to sniff,
    # and refusing it would make an ordinary local setup impossible.
    prefs.set_forge({"base_url": "http://127.0.0.1:3000", "token": "local"})
    assert prefs.get_forge()["token"] == "local"


def test_a_lookalike_loopback_host_is_NOT_exempt(tmp_path, monkeypatch):
    """`evil-127.0.0.1.example` is what a substring test lets through."""
    from agent_sessions import prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs.set_forge({"base_url": "http://evil-127.0.0.1.example", "token": "s3cret"})
    assert prefs.get_forge()["token"] == ""


def test_URL_USERINFO_is_refused_so_it_cannot_reach_the_public_config():
    """`https://user:pass@host` is a credential in a field `/api/config` echoes to the browser —
    a hole straight through the write-only boundary the token field exists to keep."""
    from agent_sessions import prefs

    assert prefs.validate_forge_patch({"base_url": "https://u:p@git.example"})
    assert prefs.validate_forge_patch({"base_url": "https://u@git.example"})
    assert prefs.validate_forge_patch({"base_url": "https://git.example/?token=abc"})
    assert prefs.validate_forge_patch({"base_url": "https://git.example/#f"})
    assert prefs.validate_forge_patch({"base_url": "https://git.example/api"}) is None


# ---- #897 review: the reads that were wrong -----------------------------------------------


def test_a_MERGED_pr_is_still_findable(monkeypatch):
    """The gate that could never be observed.

    Searching only OPEN pull requests means the moment a PR merges it leaves the list — so
    `forge_merged` never got a number and reported "there is no open PR yet" for ever.
    """

    def handler(req):
        state = req.url.params.get("state")
        if state == "open":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "number": 7,
                    "merged": True,
                    "head": {"ref": "topic", "sha": "s", "repo": {"full_name": "acme/app"}},
                }
            ],
        )

    c = _client(handler)
    # The HEAD SHA identifies it: a branch name is reusable, so a closed PR found by name alone
    # could belong to entirely different work (#897 re-review).
    f = c.pull_request("acme/app", "topic", head_sha="s")
    # `value` is "is it OPEN" — false — but the NUMBER is what the merge probe needs.
    assert f.observed and f.value is False
    assert f.extra["number"] == 7 and f.extra["pr_state"] == "closed"


def test_a_REUSED_branch_name_does_not_inherit_an_old_closed_PR():
    """`devopsagent/<slug>` names are reused constantly here.

    Rediscovering a CLOSED pr by name alone lets an older merged PR settle `forge_merged` for work
    that has not opened a PR at all — permanently, because a merge does not un-merge.
    """

    def handler(req):
        if req.url.params.get("state") == "open":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "number": 7,
                    "merged": True,
                    "head": {"ref": "topic", "sha": "OLD", "repo": {"full_name": "acme/app"}},
                }
            ],
        )

    c = _client(handler)
    f = c.pull_request("acme/app", "topic", head_sha="NEW")
    assert f.observed and f.value is False
    assert not (f.extra or {}).get("number"), "the old PR must not be adopted"


def test_no_local_head_REFUSES_a_closed_match_rather_than_guessing():
    def handler(req):
        if req.url.params.get("state") == "open":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "number": 7,
                    "merged": True,
                    "head": {"ref": "topic", "sha": "x", "repo": {"full_name": "acme/app"}},
                }
            ],
        )

    f = _client(handler).pull_request("acme/app", "topic", head_sha="")
    assert f.observed and f.value is False
    assert not (f.extra or {}).get("number")
    assert "no local head" in f.detail


def test_the_pr_search_walks_PAST_THE_FIRST_PAGE(monkeypatch):
    pages = {
        1: [
            {
                "number": i,
                "head": {"ref": f"other-{i}", "sha": "x", "repo": {"full_name": "acme/app"}},
            }
            for i in range(50)
        ],
        2: [
            {"number": 99, "head": {"ref": "topic", "sha": "s", "repo": {"full_name": "acme/app"}}}
        ],
    }

    def handler(req):
        return httpx.Response(200, json=pages.get(int(req.url.params.get("page", 1)), []))

    f = _client(handler).pull_request("acme/app", "topic")
    assert f.observed and f.extra["number"] == 99


def test_a_SAME_NAMED_branch_in_a_FORK_is_not_our_pr():
    """`head.ref` alone is not identity; a fork can have a branch of the same name."""
    rows = [
        {"number": 5, "head": {"ref": "topic", "sha": "s", "repo": {"full_name": "mallory/app"}}}
    ]
    f = _client(_json(rows)).pull_request("acme/app", "topic")
    assert f.observed and f.value is False and not (f.extra or {}).get("number")


def test_a_DISMISSED_approval_does_not_count():
    rows = [{"state": "APPROVED", "commit_id": "head", "dismissed": True, "user": {"login": "a"}}]
    f = _client(_json(rows)).review("acme/app", 7, "head")
    assert f.observed and f.value is False


def test_one_reviewer_s_LATER_objection_beats_their_earlier_approval():
    """Reduced per reviewer, latest wins — not "whatever came back last in the list"."""
    rows = [
        {"state": "APPROVED", "commit_id": "h", "user": {"login": "a"}, "id": 1, "official": True},
        {
            "state": "REQUEST_CHANGES",
            "commit_id": "h",
            "user": {"login": "a"},
            "id": 2,
            "official": True,
        },
    ]
    assert _client(_json(rows)).review("r", 7, "h").value is False
    # …and the reverse order settles, because it is the same reviewer changing their mind back.
    rows2 = [
        {
            "state": "REQUEST_CHANGES",
            "commit_id": "h",
            "user": {"login": "a"},
            "id": 1,
            "official": True,
        },
        {"state": "APPROVED", "commit_id": "h", "user": {"login": "a"}, "id": 2, "official": True},
    ]
    assert _client(_json(rows2)).review("r", 7, "h").value is True


def test_ANOTHER_reviewer_s_objection_is_not_overridden_by_an_approval():
    rows = [
        {"state": "REQUEST_CHANGES", "commit_id": "h", "user": {"login": "a"}, "id": 1},
        {"state": "APPROVED", "commit_id": "h", "user": {"login": "b"}, "id": 2},
    ]
    assert _client(_json(rows)).review("r", 7, "h").value is False


def test_an_OVER_CAP_body_is_ABANDONED_rather_than_downloaded_and_then_measured():
    """A cap applied after `client.get()` has already paid for every byte."""
    sent = {"n": 0}

    def handler(req):
        def gen():
            chunk = b"x" * 65536
            for _ in range(40):  # 2.5 MiB if anyone lets it run
                sent["n"] += len(chunk)
                yield chunk

        return httpx.Response(200, content=gen())

    c = _client(handler)
    f = c.pull_request("acme/app", "topic")
    assert f.observed is False and "more than" in f.detail
    assert sent["n"] <= forge.RESPONSE_MAX + 65536, f"downloaded {sent['n']} bytes"


# ---- #897 re-review: provider contracts ----------------------------------------------------


def test_reviews_are_PAGED():
    """A long review history pushes the current verdict off page 1 — which is what this org's PRs
    look like — and reading only that page reports "no review yet" for an approved PR."""
    pages = {
        1: [{"state": "COMMENT", "commit_id": "h", "user": {"login": f"u{i}"}} for i in range(50)],
        2: [
            {
                "state": "APPROVED",
                "commit_id": "h",
                "user": {"login": "boss"},
                "id": 9,
                "official": True,
            }
        ],
    }

    def handler(req):
        return httpx.Response(200, json=pages.get(int(req.url.params.get("page", 1)), []))

    assert _client(handler).review("acme/app", 7, "h").value is True


def test_a_NON_OFFICIAL_approval_does_not_settle_the_gate():
    """Anyone with read access can leave a comment-review. Accepting those means any account that
    can see the PR can settle its approval gate."""
    rows = [{"state": "APPROVED", "commit_id": "h", "official": False, "user": {"login": "rando"}}]
    assert _client(_json(rows)).review("acme/app", 7, "h").value is False


def test_GITHUB_asks_for_workflow_RUNS_not_forgejo_s_tasks():
    """`/actions/tasks` is a 404 on GitHub, so a configured GitHub `forge_run` objective could
    never obtain the fact it advertises."""
    seen: list[str] = []

    def handler(req):
        seen.append(str(req.url.path))
        return httpx.Response(200, json={"workflow_runs": []})

    forge.set_transport_for_test(httpx.MockTransport(handler))
    c = forge.ForgeClient(kind="github", base_url="https://api.github.com", owner="acme")
    c.run("acme/app", "ci", "topic")
    assert seen == ["/repos/acme/app/actions/runs"], seen
    # …and forgejo still gets its own endpoint, under /api/v1.
    seen.clear()
    c2 = forge.ForgeClient(kind="forgejo", base_url="https://git.example", owner="acme")
    c2.run("acme/app", "ci", "topic")
    assert seen == ["/api/v1/repos/acme/app/actions/tasks"], seen


# ---- #897 re-review round 3 -----------------------------------------------------------------


def test_a_REDIRECT_is_not_followed_out_of_the_authorised_authority():
    """Finding 3. A 30x is the forge naming a destination the operator did not configure.

    Following it sends the configured token — and the answer it produces — somewhere outside the
    authority the operator approved. An allowlist of redirect destinations is a blocklist problem
    in disguise, so there is no allowlist: a redirect is `unknown` and the operator is told to
    point the objective at the final address instead.
    """
    hops: list[str] = []

    def handler(req):
        hops.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://evil.example/api/v1/x"}, json={})

    f = _client(handler).pull_request("acme/app", "topic")
    assert f.observed is False and "redirect" in f.detail
    assert len(hops) == 1, f"followed the redirect: {hops}"
    # …and the probe layer answers the same way rather than inheriting httpx's default.
    hops.clear()
    mission_probes.set_transport_for_test(httpx.MockTransport(handler))
    try:
        g = mission_probes.probe_one(
            {"cwd": "", "merge_sha": ""},
            {"probe": "http_status", "probe_args": {"url": "https://svc.example/healthz"}},
        )
    finally:
        mission_probes.set_transport_for_test(None)
    assert g.observed is False and "redirect" in g.detail
    assert len(hops) == 1, f"followed the redirect: {hops}"


def test_a_GITHUB_approval_without_reviewer_authority_does_not_settle():
    """Finding 4 [security]. GitHub has no `official` field — it reports `author_association`.

    Accepting an absent field meant any account that could SEE a public GitHub PR could settle its
    approval gate by approving it: the entire weight of the gate resting on a field the provider
    never sends. Fail closed, and ask each provider in its own terms.
    """
    who = lambda assoc: [  # noqa: E731 — one shape, four spellings
        {"state": "APPROVED", "commit_id": "h", "user": {"login": "x"}, "author_association": assoc}
    ]
    gh = dict(kind="github", base_url="https://api.github.com")
    for outsider in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", ""):
        f = _client(_json(who(outsider)), **gh).review("acme/app", 7, "h")
        assert f.value is False, outsider
    # …an ABSENT association is refused too. "The forge did not say" is not "yes".
    bare = [{"state": "APPROVED", "commit_id": "h", "user": {"login": "x"}}]
    assert _client(_json(bare), **gh).review("acme/app", 7, "h").value is False
    for insider in ("OWNER", "MEMBER", "COLLABORATOR"):
        f = _client(_json(who(insider)), **gh).review("acme/app", 7, "h")
        assert f.value is True, insider
    # …and Forgejo keeps being asked in ITS terms: `official`, where absent is also not "yes".
    assert _client(_json(bare)).review("acme/app", 7, "h").value is False
    ok = [{"state": "APPROVED", "commit_id": "h", "user": {"login": "x"}, "official": True}]
    assert _client(_json(ok)).review("acme/app", 7, "h").value is True


def test_a_TRUNCATED_review_feed_is_UNKNOWN_not_an_APPROVAL():
    """Finding 5. If the last allowed page is full, what came back is a PREFIX of the history.

    Reducing a prefix as though it were the whole thing can invert the verdict: an approval on
    page 1 and that same reviewer's later request-for-changes on page 7 read as "approved".
    """
    full = [
        {"state": "APPROVED", "commit_id": "h", "user": {"login": f"u{i}"}, "official": True}
        for i in range(forge.ForgeClient.PR_PAGE)
    ]

    def handler(req):
        return httpx.Response(200, json=full)  # every page full, for ever

    f = _client(handler).review("acme/app", 7, "h")
    assert f.observed is False, f
    assert "too many" in f.detail

    # A budget that is NOT exhausted still answers — the guard is the full last page, not paging.
    def short(req):
        page = int(req.url.params.get("page", 1))
        return httpx.Response(200, json=full if page == 1 else full[:1])

    assert _client(short).review("acme/app", 7, "h").value is True


def test_forge_run_is_PAGED_with_the_same_incomplete_result_discipline():
    """Finding 7. One page of runs strands an objective whenever other workflows push past it.

    This repository fires several workflows on every push, so "the workflow I want is not in the
    newest 50 runs" is the ordinary case, not an edge — and answering `unknown` for ever is how
    that looked before.
    """
    pages: list[int] = []

    def handler(req):
        page = int(req.url.params.get("page", 1))
        pages.append(page)
        if page == 1:
            rows = [
                {"name": "other", "status": "success"} for _ in range(forge.ForgeClient.PR_PAGE)
            ]
        else:
            rows = [{"name": "ci", "status": "success", "conclusion": "success"}]
        return httpx.Response(200, json={"workflow_runs": rows})

    f = _client(handler, kind="github", base_url="https://api.github.com").run("acme/app", "ci", "")
    assert f.observed and f.value is True, f
    assert pages == [1, 2], pages

    # …and an exhausted budget says so rather than reporting "no run found", which reads as a
    # workflow that has never run.
    def endless(req):
        rows = [{"name": "other", "status": "success"} for _ in range(forge.ForgeClient.PR_PAGE)]
        return httpx.Response(200, json={"workflow_runs": rows})

    g = _client(endless, kind="github", base_url="https://api.github.com").run("acme/app", "ci", "")
    assert g.observed is False and "not among the newest" in g.detail, g


# ---- #897 re-review round 4 -------------------------------------------------------------------


def test_a_PR_with_NO_head_repo_is_refused_rather_than_matched_by_BRANCH_NAME():
    """Finding 3, twice now: the docstring said "refused" while the code said `return True`.

    `head.ref` is a label, not an identity. A fork — or a PR whose head repository has since been
    deleted, which is exactly when the field goes missing — can carry the same branch name, and
    accepting it steers checks, review and merge evidence to somebody else's pull request. There
    is no safe guess: the field IS the identity, so an answer without it matches nothing.
    """
    for head in (
        {"ref": "topic", "sha": "s"},  # absent
        {"ref": "topic", "sha": "s", "repo": None},  # null (a deleted fork)
        {"ref": "topic", "sha": "s", "repo": {}},  # present and empty
        {"ref": "topic", "sha": "s", "repo": {"full_name": ""}},
    ):
        f = _client(_json([{"number": 7, "head": head}])).pull_request("acme/app", "topic")
        assert f.observed and f.value is False, head
        assert not (f.extra or {}).get("number"), head
    # …and the same answer WITH the identity is accepted, so this is a fence and not a refusal
    # of everything.
    ok = [{"number": 7, "head": {"ref": "topic", "sha": "s", "repo": {"full_name": "acme/app"}}}]
    assert _client(_json(ok)).pull_request("acme/app", "topic").value is True


def test_a_CAPPED_pr_search_is_UNKNOWN_not_no_pr():
    """Finding 4. "We could not look" is not "it is not there" — the contract this whole feature
    is built on, broken at the one call that reports the objective's headline fact.

    Six full pages with no match means page seven was never read. Reporting observed-false drives
    a nudge about work that may well have happened.
    """
    ours = {"full_name": "acme/app"}
    full = [
        {"number": i, "head": {"ref": f"other-{i}", "sha": "x", "repo": ours}}
        for i in range(forge.ForgeClient.PR_PAGE)
    ]

    f = _client(lambda r: httpx.Response(200, json=full)).pull_request("acme/app", "topic")
    assert f.observed is False, f
    assert "did not finish" in f.detail

    # A budget that is NOT exhausted still answers false — the guard is the full last page.
    def short(req):
        page = int(req.url.params.get("page", 1))
        return httpx.Response(200, json=full if page == 1 else full[:1])

    g = _client(short).pull_request("acme/app", "topic")
    assert g.observed is True and g.value is False


def test_a_CAPPED_closed_pr_search_is_UNKNOWN_too():
    """The same rule on the second search. `forge_merged` reads this one, and a false "no PR"
    there says the work was never merged."""
    ours = {"full_name": "acme/app"}
    full = [
        {"number": i, "head": {"ref": "topic", "sha": f"other-{i}", "repo": ours}}
        for i in range(forge.ForgeClient.PR_PAGE)
    ]

    def handler(req):
        if req.url.params.get("state") == "open":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=full)

    f = _client(handler).pull_request("acme/app", "topic", head_sha="NEW")
    assert f.observed is False and "did not finish" in f.detail


# ---- #897 re-review round 5 -------------------------------------------------------------------


def test_a_SINGLE_DECODED_CHUNK_cannot_exceed_the_response_cap():
    """Finding 6. The cap was checked AFTER a yield, so the yield itself was unbounded.

    One 2 MiB decoded chunk sails through a 512 KiB cap because the check only runs once it is
    already in memory — and a compressed response has exactly that shape, since httpx decodes
    before it yields. Asked for a bounded chunk instead, the limit sits in front of the
    allocation rather than behind it.
    """
    asked: list[int | None] = []

    class OneBigChunk(httpx.BaseTransport):
        def handle_request(self, request):
            class Stream(httpx.SyncByteStream):
                def __iter__(inner):
                    # A stream that ignores `chunk_size` is exactly the case the old check could
                    # not survive; the guard has to be the running total AND the request.
                    yield b"x" * (2 * 1024 * 1024)

            return httpx.Response(200, stream=Stream(), headers={"content-type": "text/plain"})

    real = httpx.Response.iter_bytes

    def spy(self, chunk_size=None):
        asked.append(chunk_size)
        return real(self, chunk_size=chunk_size)

    forge.set_transport_for_test(OneBigChunk())
    c = forge.ForgeClient(kind="forgejo", base_url="https://git.example/", owner="acme")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.Response, "iter_bytes", spy)
        f = c.pull_request("acme/app", "topic")
    # The read is BOUNDED at the request, not only measured afterwards…
    assert asked and asked[0] == forge.READ_CHUNK, asked
    # …and an over-cap answer is still abandoned rather than parsed.
    assert f.observed is False and "more than" in f.detail


def test_a_ROLLUP_WITHOUT_A_COUNT_is_unknown_rather_than_green():
    """Finding 5. Rejecting only an integer zero left two ways to be green on nothing.

    This install's Forgejo posts no commit statuses at all, so "nothing reported" is the COMMON
    case — settling a mission's most important gate on an absence is the failure `checks` exists
    to prevent, and a string `"0"` walked straight past `isinstance(_, int)`.
    """
    for body in (
        {"state": "success"},  # no count at all
        {"state": "success", "total_count": "0"},  # a string zero
        {"state": "success", "total_count": None},
        {"state": "success", "total_count": True},  # a bool is not a count
        {"state": "success", "total_count": "many"},
    ):
        f = _client(_json(body)).checks("acme/app", "abc123")
        assert f.observed is False, body
    # …and a real rollup still settles, so this is a shape check rather than a refusal.
    ok = _client(_json({"state": "success", "total_count": 3})).checks("acme/app", "abc123")
    assert ok.observed and ok.value is True
    # A string count that PARSES is fine — the objection was to accepting nothing, not to JSON
    # that spells a number differently.
    s = _client(_json({"state": "success", "total_count": "3"})).checks("acme/app", "abc123")
    assert s.observed and s.value is True


def test_a_RUN_WITHOUT_BRANCH_PROVENANCE_does_not_settle_a_branch_gate():
    """Finding 4. Accepting an empty `head_branch` let a run the provider did not attribute to any
    branch settle a branch-scoped gate — the same fail-open shape as a missing `head.repo`."""
    rows = [{"name": "ci", "status": "completed", "conclusion": "success", "head_branch": ""}]
    gh = dict(kind="github", base_url="https://api.github.com")
    f = _client(_json({"workflow_runs": rows}), **gh).run("acme/app", "ci", "topic")
    assert f.observed is False, f
    # …with the branch named, it settles.
    rows2 = [{"name": "ci", "status": "completed", "conclusion": "success", "head_branch": "topic"}]
    g = _client(_json({"workflow_runs": rows2}), **gh).run("acme/app", "ci", "topic")
    assert g.observed and g.value is True
    # …and a run for ANOTHER branch is not this one's.
    rows3 = [{"name": "ci", "status": "completed", "conclusion": "success", "head_branch": "main"}]
    h = _client(_json({"workflow_runs": rows3}), **gh).run("acme/app", "ci", "topic")
    assert h.observed is False


def test_a_COMPLETED_GITHUB_RUN_WITH_NO_CONCLUSION_is_unknown():
    """Finding 4, the other half. GitHub always reports a conclusion on a completed run, so its
    absence is a shape we do not understand — and reading it as success invents the one thing the
    response did not say."""
    gh = dict(kind="github", base_url="https://api.github.com")
    rows = [{"name": "ci", "status": "completed", "head_branch": "topic"}]
    f = _client(_json({"workflow_runs": rows}), **gh).run("acme/app", "ci", "topic")
    assert f.observed is False and "conclusion" in f.detail
    # Forgejo TASKS legitimately carry no conclusion — `status` is the verdict there — so the
    # same shape settles on that provider. Two providers, asked in their own terms.
    rows2 = [{"name": "ci", "status": "success", "head_branch": "topic"}]
    g = _client(_json({"workflow_runs": rows2})).run("acme/app", "ci", "topic")
    assert g.observed and g.value is True


def test_an_OPEN_PR_AT_ANOTHER_HEAD_is_not_evidence_about_this_checkout():
    """Finding 2. Identifying the PR is not identifying the COMMIT.

    Only the closed search used the local head, on the reasoning that a branch name identifies at
    most one open PR — it does, but everything downstream is about the PR's head SHA: that is what
    `checks` and `review` are then asked about. With unpushed work in the checkout, the gates
    settle on the last thing that was pushed.
    """
    ours = {"full_name": "acme/app"}
    rows = [{"number": 7, "head": {"ref": "topic", "sha": "OLD", "repo": ours}}]
    f = _client(_json(rows)).pull_request("acme/app", "topic", head_sha="NEW")
    # UNKNOWN, not false: the PR is really open and the operator has really not pushed. Neither
    # of those is "the objective does not hold".
    assert f.observed is False, f
    assert "push" in f.detail
    assert not (f.extra or {}).get("number")
    # …and at the SAME head it settles, so this is a fence and not a refusal of open PRs.
    same = [{"number": 7, "head": {"ref": "topic", "sha": "NEW", "repo": ours}}]
    g = _client(_json(same)).pull_request("acme/app", "topic", head_sha="NEW")
    assert g.observed and g.value is True and g.extra["number"] == 7
    # …and with NO local head there is nothing to compare, which is the ordinary case.
    h = _client(_json(rows)).pull_request("acme/app", "topic")
    assert h.observed and h.value is True
