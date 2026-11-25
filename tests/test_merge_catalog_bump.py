"""`.forgejo/scripts/merge_catalog_bump.py` and its workflow (#1867).

The bump PR merges on its own only when every autonomous-merge guard holds, so each guard is
pinned here ALONE: starting from a PR that merges, one fact is changed and the merge must not
happen. A guard that only ever failed together with another would pass these tests while doing
nothing. The API is a fake keyed by path; no test touches the network.
"""

from __future__ import annotations

import copy
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".forgejo" / "scripts" / "merge_catalog_bump.py"
WF = ROOT / ".forgejo" / "workflows" / "agent-catalog-bump-merge.yml"
BUMP_WF = ROOT / ".forgejo" / "workflows" / "agent-catalog-bump.yml"

spec = importlib.util.spec_from_file_location("merge_catalog_bump", SCRIPT)
mcb = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name] = mcb  # dataclasses resolve their module by name
spec.loader.exec_module(mcb)

SHA = "a" * 40
OLD = "b" * 40
MERGED = "c" * 40
REPO = {"full_name": "superstatus.io/agent-sessions"}
TITLE = "chore(agents): catalog — claude 2.1.317"


def _pr(**over) -> dict:
    pr = {
        "number": 7,
        "state": "open",
        "merged": False,
        "draft": False,
        "title": TITLE,
        "user": {"login": "infrabot"},
        "labels": [{"name": "type:chore"}, {"name": "area:app"}],
        "head": {"ref": mcb.BRANCH, "sha": SHA, "repo": dict(REPO)},
        "base": {"ref": "main", "repo": dict(REPO)},
        "mergeable": True,
    }
    pr.update(over)
    return pr


def _review(rid: int, state: str = "APPROVED", sha: str = SHA, login: str = "marcusaiassistant"):
    return {
        "id": rid,
        "state": state,
        "commit_id": sha,
        "dismissed": False,
        "user": {"login": login},
    }


class FakeApi:
    """Answers GETs from `self.routes`; records POSTs. A missing route is an API error."""

    def __init__(self, pr: dict | None = None):
        self.pr = _pr() if pr is None else pr
        self.extra_pulls: list[dict] = []
        self.files = [
            {"filename": "release/recipes/linux-x64/claude.json", "status": "changed"},
            {"filename": "src/agent_sessions/agent_catalog.json", "status": "changed"},
        ]
        self.reviews = [_review(10, "REQUEST_CHANGES", OLD), _review(11)]
        self.comments = [{"user": {"login": "infrabot"}}, {"user": {"login": "marcusaiassistant"}}]
        self.status = {"state": "success", "statuses": [{"context": "pr-validate"}]}
        self.posts: list[tuple[str, dict]] = []
        self.merge_lands = True
        self.merge_error: str | None = None
        self.head_after_merge: str | None = None
        self.bump_last = False
        self.server_page: int | None = None  # comments: None = paging ignored (measured)
        self.rereads = 0
        self.on_reread = None  # called on each GET /pulls/<n> before any merge was posted

    def get(self, path: str):
        base, _, query = path.partition("?")
        n = (self.pr or {}).get("number")
        if base == "/pulls":
            mine = [self.pr] if self.pr else []
            pulls = [*self.extra_pulls, *mine] if self.bump_last else [*mine, *self.extra_pulls]
            page = int(re.search(r"page=(\d+)", query).group(1))
            return pulls[(page - 1) * mcb.PAGE : page * mcb.PAGE]
        if base == f"/pulls/{n}/files":
            return self.files if "page=1" in query else []
        if base == f"/pulls/{n}/reviews":
            return self.reviews if "page=1" in query else []
        if base == f"/issues/{n}/comments":
            if not isinstance(self.comments, list) or self.server_page is None:
                return self.comments  # this Forgejo: paging ignored, every page is the whole list
            # A server that does page them, at its own size whatever `limit` asked for.
            page = int(re.search(r"page=(\d+)", query).group(1))
            return self.comments[(page - 1) * self.server_page : page * self.server_page]
        if base == f"/commits/{SHA}/status":
            return self.status
        if base == f"/pulls/{n}":
            if not self.posts:
                self.rereads += 1
                if self.on_reread:
                    self.on_reread(self)
            pr = copy.deepcopy(self.pr)
            if self.posts and self.merge_lands and self.merge_error is None:
                pr.update(state="closed", merged=True, merge_commit_sha=MERGED)
            if self.posts and self.head_after_merge:
                pr["head"]["sha"] = self.head_after_merge
            return pr
        raise mcb.ApiError(f"GET {path}: HTTP 404")

    def post(self, path: str, payload: dict):
        self.posts.append((path, payload))
        if self.merge_error:
            raise mcb.ApiError(self.merge_error)
        return None


def _run(api: FakeApi, *argv: str) -> int:
    return mcb.main(list(argv), api=api, sleep=lambda _s: None)


def _merged(api: FakeApi) -> bool:
    return any(path.endswith("/merge") for path, _ in api.posts)


# --- the happy path --------------------------------------------------------------------------


def test_an_approved_green_bump_is_squash_merged_pinned_to_the_checked_head(capsys):
    api = FakeApi()
    assert _run(api) == 0
    ((path, payload),) = api.posts
    assert path == "/pulls/7/merge"
    assert payload["Do"] == "squash"
    assert payload["MergeTitleField"] == TITLE
    assert payload["delete_branch_after_merge"] is True
    assert payload["head_commit_id"] == SHA
    assert "review 11" in payload["MergeMessageField"]
    assert f"Merged #7 as {MERGED}" in capsys.readouterr().out


def test_dry_run_decides_but_never_merges(capsys):
    api = FakeApi()
    assert _run(api, "--dry-run") == 0
    assert api.posts == []
    assert "would squash-merge #7" in capsys.readouterr().out


def test_no_bump_pr_is_nothing_to_do():
    api = FakeApi()
    api.pr = None
    api.extra_pulls = [_pr(number=8, head={"ref": "devopsagent/other", "sha": SHA, "repo": REPO})]
    assert _run(api) == 0
    assert api.posts == []


@pytest.mark.parametrize(
    "head_ref, base_ref",
    [(mcb.BRANCH + "-2", "main"), ("x/" + mcb.BRANCH, "main"), (mcb.BRANCH, "release")],
)
def test_only_the_exact_branch_into_main_is_a_candidate(head_ref, base_ref):
    api = FakeApi()
    api.pr = _pr(head={"ref": head_ref, "sha": SHA, "repo": REPO}, base={"ref": base_ref})
    assert _run(api) == 0
    assert api.posts == []


def test_the_candidate_is_found_past_the_first_page():
    api = FakeApi()
    api.bump_last = True
    api.extra_pulls = [
        _pr(number=100 + i, head={"ref": f"devopsagent/x{i}", "sha": SHA, "repo": REPO})
        for i in range(mcb.PAGE)
    ]
    assert _run(api) == 0
    assert _merged(api)


# --- each guard, alone, blocks the merge -----------------------------------------------------


def _waits(api: FakeApi) -> None:
    assert _run(api) == 0
    assert not _merged(api)


def _refuses(api: FakeApi) -> None:
    assert _run(api) == 1
    assert not _merged(api)


def test_a_pr_opened_by_anyone_but_the_bot_is_refused():
    _refuses(FakeApi(_pr(user={"login": "someone"})))


def test_a_pr_from_a_fork_is_refused():
    head = {"ref": mcb.BRANCH, "sha": SHA, "repo": {"full_name": "someone/agent-sessions"}}
    _refuses(FakeApi(_pr(head=head)))


def test_two_open_bump_prs_are_refused():
    api = FakeApi()
    api.extra_pulls = [_pr(number=8)]
    _refuses(api)


@pytest.mark.parametrize("over", [{"draft": True}, {"title": "WIP: " + TITLE}])
def test_a_draft_waits(over):
    _waits(FakeApi(_pr(**over)))


@pytest.mark.parametrize("label", ["hold", "do-not-merge"])
def test_a_hold_label_waits(label):
    _waits(FakeApi(_pr(labels=[{"name": "type:chore"}, {"name": label}])))


@pytest.mark.parametrize(
    "entry",
    [
        {"filename": "scripts/bump-agent-recipes", "status": "changed"},
        {"filename": ".forgejo/workflows/agent-catalog-bump.yml", "status": "changed"},
        {"filename": "release/recipes/linux-x64/sub/claude.json", "status": "changed"},
        {"filename": "release/recipes/linux-x64/claude.sh", "status": "changed"},
        {"filename": "release/recipes/linux-x64/new.json", "status": "added"},
        {"filename": "release/recipes/linux-x64/kimi.json", "status": "removed"},
        {
            "filename": "release/recipes/linux-x64/claude.json",
            "status": "renamed",
            "previous_filename": "release/recipes/linux-x64/old.json",
        },
        {"filename": "src/agent_sessions/agent_catalog.json.bak", "status": "added"},
    ],
)
def test_any_file_the_bump_does_not_edit_is_refused(entry):
    api = FakeApi()
    api.files = [*api.files, entry]
    _refuses(api)


def test_a_pr_with_no_files_is_refused():
    api = FakeApi()
    api.files = []
    _refuses(api)


def test_foreign_paths_are_refused_even_when_approved_and_green():
    # Refusal does not depend on any later guard: the PR is approved, green and mergeable.
    api = FakeApi()
    api.files = [{"filename": "src/agent_sessions/main.py", "status": "changed"}]
    assert _run(api) == 1
    assert api.posts == []


def test_no_review_on_the_head_waits():
    api = FakeApi()
    api.reviews = [_review(10, "APPROVED", OLD)]
    _waits(api)


@pytest.mark.parametrize("state", ["REQUEST_CHANGES", "COMMENT"])
def test_a_later_non_approval_on_the_head_waits(state):
    api = FakeApi()
    api.reviews = [_review(11), _review(12, state)]
    _waits(api)


def test_the_latest_review_wins_by_id_not_list_order():
    api = FakeApi()
    api.reviews = [_review(12, "REQUEST_CHANGES"), _review(11)]
    _waits(api)


def test_a_dismissed_approval_waits():
    api = FakeApi()
    api.reviews = [{**_review(11), "dismissed": True}]
    _waits(api)


def test_an_approval_from_another_account_does_not_count():
    api = FakeApi()
    api.reviews = [_review(11, login="infrabot")]
    _waits(api)


def test_a_human_review_waits():
    api = FakeApi()
    api.reviews = [*api.reviews, _review(5, "COMMENT", OLD, login="operator")]
    _waits(api)


def test_a_human_comment_waits(capsys):
    api = FakeApi()
    api.comments = [*api.comments, {"user": {"login": "operator"}}]
    _waits(api)
    assert "operator" in capsys.readouterr().out


@pytest.mark.parametrize(
    "status",
    [
        {"state": "pending", "statuses": [{"context": "pr-validate"}]},
        {"state": "failure", "statuses": [{"context": "pr-validate"}]},
        {"state": "error", "statuses": [{"context": "pr-validate"}]},
        {"state": "success", "statuses": []},
        {"state": "success"},
        {},
    ],
)
def test_anything_but_a_populated_success_rollup_waits(status):
    api = FakeApi()
    api.status = status
    _waits(api)


@pytest.mark.parametrize("mergeable", [False, None])
def test_a_conflict_or_unknown_mergeability_waits(mergeable):
    _waits(FakeApi(_pr(mergeable=mergeable)))


# --- every guard is read again right before the merge ----------------------------------------


def test_every_guard_is_read_again_from_a_fresh_pr_before_the_merge():
    api = FakeApi()
    assert _run(api) == 0
    assert api.rereads == 1
    assert _merged(api)


def _late(change, api: FakeApi | None = None):
    api = api or FakeApi()
    api.on_reread = change
    assert _run(api) == 0
    assert not _merged(api)


def test_a_hold_label_that_lands_after_the_first_read_stops_the_merge():
    _late(lambda a: a.pr["labels"].append({"name": "hold"}))


def test_a_human_comment_that_lands_after_the_first_read_stops_the_merge():
    _late(lambda a: a.comments.append({"user": {"login": "operator"}}))


def test_a_later_review_that_lands_after_the_first_read_stops_the_merge():
    _late(lambda a: a.reviews.append(_review(99, "REQUEST_CHANGES")))


def test_a_rollup_that_turns_after_the_first_read_stops_the_merge():
    _late(lambda a: a.status.update(state="failure"))


def test_a_head_that_moves_after_the_first_read_stops_the_merge():
    def move(a):
        a.pr["head"]["sha"] = OLD

    _late(move)


# --- the merge is proven, not assumed --------------------------------------------------------


def test_a_merge_the_api_reported_but_did_not_make_goes_red():
    api = FakeApi()
    api.merge_lands = False
    assert _run(api) == 1
    assert _merged(api)


def test_a_refused_merge_on_the_same_head_goes_red():
    api = FakeApi()
    api.merge_error = "POST /pulls/7/merge: HTTP 405"
    assert _run(api) == 1


def test_a_head_that_moved_before_the_merge_waits_for_the_next_run(capsys):
    api = FakeApi()
    api.merge_error = "POST /pulls/7/merge: HTTP 409"
    api.head_after_merge = OLD
    assert _run(api) == 0
    assert "moved past" in capsys.readouterr().out


def test_an_api_error_is_exit_2_and_merges_nothing():
    api = FakeApi()
    api.status = None  # GET /commits/<sha>/status answers something that is not an object
    assert _run(api) == 2
    assert api.posts == []


def test_without_api_or_token_nothing_runs(monkeypatch):
    monkeypatch.delenv("API", raising=False)
    monkeypatch.delenv("INFRABOT_TOKEN", raising=False)
    assert mcb.main([]) == 2
    monkeypatch.setenv("API", "http://example.invalid/api/v1/repos/o/r")
    monkeypatch.setenv("INFRABOT_TOKEN", "t")
    assert mcb.main([]) == 2


def test_the_token_is_a_header_only():
    api = mcb.Api("https://example.invalid/api/v1/repos/o/r", "s3cret")
    assert api.headers["Authorization"] == "token s3cret"
    assert "s3cret" not in api.base
    with pytest.raises(mcb.ApiError):
        mcb.Api("http://example.invalid", "s3cret")


# --- the workflow ----------------------------------------------------------------------------


def _wf() -> dict:
    return yaml.safe_load(WF.read_text())


def _on(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True))


def test_it_runs_on_a_schedule_and_on_dispatch():
    on = _on(_wf())
    assert on["schedule"] and "workflow_dispatch" in on


def test_it_has_its_own_concurrency_group():
    # Sharing the bump's group could cancel a pending bump run queued behind a merge run.
    mine = _wf()["concurrency"]["group"]
    bump = yaml.safe_load(BUMP_WF.read_text())["concurrency"]["group"]
    assert mine != bump


def test_only_the_merge_step_holds_the_token_and_only_on_main():
    steps = _wf()["jobs"]["merge"]["steps"]
    holders = [s for s in steps if "INFRABOT_FORGEJO_TOKEN" in str(s.get("env", {}))]
    assert [s["name"] for s in holders] == ["Merge the approved bump PR"]
    bump_steps = yaml.safe_load(BUMP_WF.read_text())["jobs"]["bump"]["steps"]
    (bump_pr,) = [s for s in bump_steps if s.get("name") == "Open or refresh the bump PR"]
    assert holders[0]["if"] == bump_pr["if"]
    assert holders[0]["run"].strip() == "python3 .forgejo/scripts/merge_catalog_bump.py"
    assert "--dry-run" not in holders[0]["run"]


def test_the_checkout_keeps_no_credential_and_carries_the_script():
    (checkout,) = [s for s in _wf()["jobs"]["merge"]["steps"] if "checkout" in s.get("uses", "")]
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["sparse-checkout"] == ".forgejo/scripts/merge_catalog_bump.py"
    assert SCRIPT.is_file()


def test_the_script_merges_only_the_branch_the_bump_pushes():
    env = yaml.safe_load(BUMP_WF.read_text())["jobs"]["bump"]["env"]
    assert env["BRANCH"] == mcb.BRANCH


# --- ambiguity fails closed ------------------------------------------------------------------


@pytest.mark.parametrize("listing", ["files", "reviews"])
def test_a_listing_that_never_ends_is_an_api_error_not_a_pass(listing):
    api = FakeApi()
    item = getattr(api, listing)[0]
    orig = api.get

    def get(path):
        if path.startswith(f"/pulls/7/{listing}?"):
            page = int(re.search(r"page=(\d+)", path).group(1))
            return [{**item, "seq": page}]  # a new, non-empty page every time
        return orig(path)

    api.get = get
    assert _run(api) == 2
    assert api.posts == []


@pytest.mark.parametrize("listing", ["/pulls?", "/pulls/7/files?", "/pulls/7/reviews?"])
def test_a_listing_page_that_is_not_a_list_is_an_api_error(listing):
    api = FakeApi()
    orig = api.get
    api.get = lambda path: {"message": "nope"} if path.startswith(listing) else orig(path)
    assert _run(api) == 2
    assert api.posts == []


def _bot_comments(n: int) -> list[dict]:
    return [{"user": {"login": ("infrabot", "marcusaiassistant")[i % 2]}} for i in range(n)]


@pytest.mark.parametrize("server_page", [None, 30])
def test_many_bot_comments_still_merge(server_page):
    # This Forgejo ignores paging on issue comments (every page is the whole list): reading
    # until an empty page would never end, and the bump would never merge.
    api = FakeApi()
    api.server_page = server_page
    api.comments = _bot_comments(43)
    assert _run(api) == 0
    assert _merged(api)


@pytest.mark.parametrize("server_page", [None, 30])
def test_a_human_comment_past_the_servers_first_page_waits(server_page, capsys):
    # Hermes on #1869: a server paging comments at 30 hides the operator's 31st, a hold.
    api = FakeApi()
    api.server_page = server_page
    api.comments = [*_bot_comments(30), {"user": {"login": "operator"}}]
    _waits(api)
    assert "operator" in capsys.readouterr().out


@pytest.mark.parametrize("server_page", [None, 30])
def test_a_human_comment_past_the_first_page_that_lands_before_the_merge_stops_it(server_page):
    api = FakeApi()
    api.server_page = server_page
    api.comments = _bot_comments(30)
    _late(lambda a: a.comments.append({"user": {"login": "operator"}}), api)


@pytest.mark.parametrize("listing", ["files", "reviews"])
def test_a_short_server_page_is_not_the_last_page(listing):
    # A server page size below `limit` returns full pages shorter than PAGE; stopping on the
    # first short page would drop the rest. Only an empty page ends a listing.
    api = FakeApi()
    extra = (
        {"filename": "src/agent_sessions/main.py", "status": "changed"}
        if listing == "files"
        else _review(5, "COMMENT", OLD, login="operator")
    )
    first = [getattr(api, listing)[-1]] * 30
    orig = api.get

    def get(path):
        if path.startswith(f"/pulls/7/{listing}?"):
            page = int(re.search(r"page=(\d+)", path).group(1))
            return {1: first, 2: [extra]}.get(page, [])
        return orig(path)

    api.get = get
    assert _run(api) in (0, 1)
    assert not _merged(api)


def test_comments_that_are_not_a_list_are_an_api_error():
    api = FakeApi()
    api.comments = {"message": "nope"}
    assert _run(api) == 2
    assert api.posts == []


_CLAUSE = re.compile(r"^github\.(event_name|ref) == '([^']*)'$")


def _guard(expression: str, context: dict) -> bool:
    result = False
    for clause in expression.split("||"):
        match = _CLAUSE.match(clause.strip())
        assert match, f"unsupported guard clause: {clause!r}"
        result = result or context[match.group(1)] == match.group(2)
    return result


def _merge_step() -> dict:
    (step,) = [
        s for s in _wf()["jobs"]["merge"]["steps"] if s.get("name") == "Merge the approved bump PR"
    ]
    return step


@pytest.mark.parametrize("ref", ["refs/heads/devopsagent/x", "refs/tags/v1.0.0", "main"])
def test_a_dispatch_off_main_never_runs_the_credentialed_step(ref):
    assert not _guard(_merge_step()["if"], {"event_name": "workflow_dispatch", "ref": ref})


@pytest.mark.parametrize("ref", ["main", "refs/heads/main", ""])
def test_a_scheduled_run_merges(ref):
    assert _guard(_merge_step()["if"], {"event_name": "schedule", "ref": ref})


def test_a_dispatch_on_main_merges():
    assert _guard(
        _merge_step()["if"], {"event_name": "workflow_dispatch", "ref": "refs/heads/main"}
    )
