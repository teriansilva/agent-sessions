"""The `github-rest` adapter (#1826): github.com's REST API, read-only, exactly these routes under
the fixed origin ``https://api.github.com``.

* ``GET /user`` and one ``/user/repos`` page — the probe
* ``GET /search/repositories?q=`` — repos
* ``GET /search/issues?q=<query> is:issue|is:pr`` — issues and PRs
* ``GET /repos/{o}/{r}`` and ``…/branches/{branch}`` — a repo, its default-branch head sha
* ``GET /repos/{o}/{r}/issues/{n}`` and ``…/issues/{n}/comments`` — one issue (the newest page)
* ``GET /repos/{o}/{r}/pulls/{n}`` — a PR's head sha and whether it merged
* ``GET /repos/{o}/{r}/commits/{sha}/status``, ``…/actions/runs?head_sha=`` and
  ``…/commits/{sha}/check-runs`` — CI, every source the token can see (`read_ci`)
* ``GET /repos/{o}/{r}/contents/{path}?ref={sha}`` — a directory, or one text file

The same interface as `forgejo` (`common`'s docstring). Paths are built from validated parts, each
segment encoded on its own; `connector_http` holds the token, the fixed headers and the path rule.
"""

from __future__ import annotations

import re

from ... import connector_http
from ...connector_http import Bound, ConnectorError
from .common import (
    BRANCH_MAX,
    COMMENTS_MAX,
    SEARCH_LIMIT,
    ci_result,
    contents_path,
    count,
    dir_entries,
    file_text,
    login,
    seg,
    sha,
    text,
    valid_name,
    valid_path,
)

__all__ = ["valid_name", "valid_path"]

#: GitHub's largest page, and how many pages one CI read may walk per system.
PAGE_MAX = 100
CI_PAGES_MAX = 3
#: A classic token's scopes (`X-OAuth-Scopes`) that only READ. Every other scope can change
#: something (`repo`, `admin:repo_hook`, `write:org`, `workflow`, `gist`, `notifications`,
#: `delete_repo`, …), so the rule is an allowlist of read-only scopes, never a list of write ones a
#: new GitHub scope could slip past (Hermes on #1831).
READ_ONLY_SCOPE_PREFIX = "read:"
READ_ONLY_SCOPES = frozenset({"user:email"})


def _writes(scope: str) -> bool:
    return not (scope.startswith(READ_ONLY_SCOPE_PREFIX) or scope in READ_ONLY_SCOPES)


#: A search word that would choose issues or PRs itself (`is:pr`, `-is:issue`, `type:pr`, …).
TYPE_QUALIFIER = re.compile(r"-?(?:is|type):(?:issues?|prs?|pull-requests?)", re.IGNORECASE)

#: A run's conclusion as a status state (`common.STATUS_SEVERITY`). An unknown conclusion
#: falls to `pending` there, never to success.
CONCLUSIONS = {
    "success": "success",
    "neutral": "success",
    "skipped": "success",
    "failure": "failure",
    "timed_out": "failure",
    "cancelled": "failure",
    "action_required": "failure",
    "startup_failure": "failure",
    "stale": "pending",
}


def _repo_path(o: str, r: str) -> str:
    if not (valid_name(o) and valid_name(r)):
        raise ConnectorError("refused", "not a repository name")
    return f"/repos/{seg(o)}/{seg(r)}"


async def _get(bound: Bound, path: str, params: dict | None = None, **kw) -> object:
    return await connector_http._request(bound, "GET", path, params, **kw)


async def probe(bound: Bound) -> dict:
    """Who the token belongs to, how many repositories its first page shows, and a warning when a
    classic token grants write scopes (#1746 D3: warn, never refuse)."""
    seen: dict[str, str] = {}
    user = await _get(bound, "/user", headers_out=seen)
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise ConnectorError("bad_reply", "the address did not answer as GitHub does")
    repos = await _get(bound, "/user/repos", {"per_page": SEARCH_LIMIT})
    if not isinstance(repos, list):
        raise ConnectorError("bad_reply", "the address did not answer as GitHub does")
    out: dict = {"login": user["login"], "repos": len(repos)}
    granted = {s.strip() for s in seen.get("x-oauth-scopes", "").split(",") if s.strip()}
    wide = sorted(s for s in granted if _writes(s))
    if wide:
        out["warning"] = (
            "This token can also write (" + ", ".join(wide) + "). HQ only reads; a fine-grained, "
            "read-only token is safer."
        )
    return out


async def search(bound: Bound, kind: str, query: str) -> list[dict]:
    """``[{owner, repo, number?, title, state?, description?, pull?}]`` — raw text."""
    if kind == "repos":
        page = await _get(bound, "/search/repositories", {"q": query, "per_page": SEARCH_LIMIT})
        rows = page.get("items") if isinstance(page, dict) else None
        out: list[dict] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            owner = login(row.get("owner"))
            name = row.get("name")
            if valid_name(owner) and valid_name(name):
                out.append(
                    {"owner": owner, "repo": name, "title": f"{owner}/{name}",
                     "description": text(row.get("description"))}
                )  # fmt: skip
        return out[:SEARCH_LIMIT]
    if kind not in ("issues", "pulls"):
        raise ConnectorError("refused", "kind must be repos, issues or pulls")
    # The tool's `kind` decides issue vs PR: a type qualifier in the model's query is dropped, so
    # the two can never disagree (rows that still do are dropped by `hq.search`).
    words = [w for w in query.split() if not TYPE_QUALIFIER.fullmatch(w)]
    words.append("is:pr" if kind == "pulls" else "is:issue")
    page = await _get(bound, "/search/issues", {"q": " ".join(words), "per_page": SEARCH_LIMIT})
    rows = page.get("items") if isinstance(page, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        # `repository_url` is https://api.github.com/repos/<owner>/<repo>: the last two segments.
        parts = text(row.get("repository_url"), 400).rsplit("/", 2)
        owner, name = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
        number = row.get("number")
        if valid_name(owner) and valid_name(name) and count(number):
            out.append(
                {
                    "owner": owner,
                    "repo": name,
                    "number": number,
                    "title": text(row.get("title")),
                    "state": text(row.get("state"), 20),
                    "pull": row.get("pull_request") is not None,
                }
            )
    return out[:SEARCH_LIMIT]


async def read_repo(bound: Bound, o: str, r: str) -> dict:
    base = _repo_path(o, r)
    repo = await _get(bound, base)
    if not isinstance(repo, dict):
        raise ConnectorError("bad_reply", "GitHub answered something other than a repository")
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch or len(branch) > BRANCH_MAX:
        raise ConnectorError("bad_reply", "the repository has no default branch")
    head = await _get(bound, f"{base}/branches/{seg(branch)}")
    commit = head.get("commit") if isinstance(head, dict) else None
    head_sha = sha(commit.get("sha") if isinstance(commit, dict) else None)
    return {
        "owner": o,
        "repo": r,
        "description": text(repo.get("description")),
        "default_branch": branch,
        "head_sha": head_sha,
        # GitHub counts open issues AND pull requests together, and has no separate PR count.
        "open_issues": count(repo.get("open_issues_count")),
        "open_pulls": None,
        "archived": bool(repo.get("archived")),
    }


async def read_issue(bound: Bound, o: str, r: str, n: int) -> dict:
    base = _repo_path(o, r)
    if not count(n):
        raise ConnectorError("refused", "not an issue number")
    issue = await _get(bound, f"{base}/issues/{n}")
    if not isinstance(issue, dict):
        raise ConnectorError("bad_reply", "GitHub answered something other than an issue")
    # GitHub pages comments oldest first and reports how many there are, so the NEWEST are the
    # last page: one request for up to COMMENTS_MAX of them, however long the thread is.
    total = count(issue.get("comments"))
    rows: list = []
    # A page of very long comments can exceed the transport's body cap. Then the issue is read
    # without its comments, and says so, as the Forgejo adapter does (Hermes on #1831): the
    # thread being long must never make the issue itself unreadable.
    comments_complete = True
    if total:
        last = (total + COMMENTS_MAX - 1) // COMMENTS_MAX
        try:
            got = await _get(
                bound, f"{base}/issues/{n}/comments", {"per_page": COMMENTS_MAX, "page": last}
            )
        except ConnectorError as e:
            if e.code != "too_large":
                raise
            got, comments_complete = [], False
        rows = got if isinstance(got, list) else []
    out = {
        "owner": o,
        "repo": r,
        "number": n,
        "title": text(issue.get("title")),
        "state": text(issue.get("state"), 20),
        "author": login(issue.get("user")),
        "body": text(issue.get("body")),
        "comments": [
            {
                "author": login(c.get("user")),
                "created": text(c.get("created_at"), 40),
                "body": text(c.get("body")),
            }
            for c in rows[-COMMENTS_MAX:]
            if isinstance(c, dict)
        ],
        "pull": None,
        "comments_complete": comments_complete,
        "comments_total": total if comments_complete else None,
    }  # fmt: skip
    if issue.get("pull_request") is not None:
        pr = await _get(bound, f"{base}/pulls/{n}")
        head = pr.get("head") if isinstance(pr, dict) else None
        out["pull"] = {
            "head_sha": sha(head.get("sha") if isinstance(head, dict) else None),
            "merged": bool(pr.get("merged")) if isinstance(pr, dict) else False,
        }
    return out


async def _walk(bound: Bound, path: str, key: str, params: dict, add) -> bool:
    """Page one CI source, handing each row to ``add``. True when the cap stopped the walk before
    ``total_count`` was reached (the result can then never read as green)."""
    seen_rows = 0
    for page in range(1, CI_PAGES_MAX + 1):
        got = await _get(bound, path, {**params, "per_page": PAGE_MAX, "page": page})
        if not isinstance(got, dict):
            raise ConnectorError("bad_reply", "GitHub answered something other than CI results")
        rows = [c for c in got.get(key) or [] if isinstance(c, dict)]
        for c in rows:
            add(c)
        seen_rows += len(rows)
        if not rows or seen_rows >= count(got.get("total_count")) or len(rows) < PAGE_MAX:
            return False
    return True


def _run_state(run: dict) -> str:
    """A workflow run's or check run's state: its conclusion once completed, else `pending`."""
    if run.get("status") != "completed":
        return "pending"
    return CONCLUSIONS.get(text(run.get("conclusion"), 40), "pending")


def _name(v: object) -> str:
    return v if isinstance(v, str) else ""


def _workflow_key(run: dict) -> str:
    """``actions · <name> · <workflow file>``: a run is identified by its WORKFLOW, not its display
    name. Two workflow files may share a name ("CI"); keyed by name, the newer one's success would
    hide the other's failure (Hermes on #1831). The file name keeps it readable; the numeric
    workflow id stands in when the path is missing."""
    path = _name(run.get("path")).rsplit("/", 1)[-1].split("@", 1)[0]
    wid = run.get("workflow_id")
    ident = path or (str(wid) if isinstance(wid, int) and not isinstance(wid, bool) else "?")
    return f"actions · {_name(run.get('name'))} · {ident}"


async def read_ci(bound: Bound, o: str, r: str, commit: str) -> dict:
    """GitHub's CI for one commit, from every source a read-only token can see, merged.

    * **commit statuses** (`Commit statuses: read`), keyed ``status · <context>``;
    * **GitHub Actions workflow runs** for the commit (`Actions: read`), keyed
      ``actions · <name> · <workflow file>`` (one per workflow, the newest run). A repository
      that only uses Actions has no statuses at all, so the status route alone would call a red
      run "no checks";
    * **check runs** from apps other than Actions, keyed ``check · <app> · <name>``. A fine-grained
      personal access token has no Checks permission (#1826, Hermes), so on a private repository
      GitHub refuses this route: the result then says these are not visible, and a result that
      could not see a source never reads as green (``incomplete``).

    Each source keeps its own namespace, so a green status and a failed check sharing a name are
    two entries, and the worst of all of them is the state. The newest row per key wins (GitHub
    lists newest first)."""
    base = _repo_path(o, r)
    commit = sha(commit)
    latest: dict[str, str] = {}

    def keep(key: str, state: str) -> None:
        if key not in latest:
            latest[key] = state

    capped = await _walk(
        bound, f"{base}/commits/{commit}/status", "statuses", {},
        lambda c: keep(f"status · {_name(c.get('context'))}", text(c.get("state"), 20)),
    )  # fmt: skip
    capped |= await _walk(
        bound, f"{base}/actions/runs", "workflow_runs", {"head_sha": commit},
        lambda c: keep(_workflow_key(c), _run_state(c)),
    )  # fmt: skip

    def check(c: dict) -> None:
        app = c.get("app") if isinstance(c.get("app"), dict) else {}
        slug = _name(app.get("slug"))
        if slug == "github-actions":
            return  # already read, per workflow, from the Actions route
        keep(f"check · {slug or 'app'} · {_name(c.get('name'))}", _run_state(c))

    hidden = False
    try:
        capped |= await _walk(bound, f"{base}/commits/{commit}/check-runs", "check_runs", {}, check)
    except ConnectorError as e:
        if e.code not in ("rejected", "not_found"):
            raise
        hidden = True
    out = ci_result(commit, latest, capped)
    if hidden:
        # What could not be seen may have failed: never green, like a capped walk.
        if out["state"] not in ("error", "failure"):
            out["state"] = "incomplete"
        out["note"] = (
            "check runs from apps other than GitHub Actions are not visible to this token "
            "(fine-grained tokens have no Checks permission)"
        )
    return out


async def list_dir(bound: Bound, o: str, r: str, commit: str, path: str) -> list[dict]:
    commit = sha(commit)
    rows = await _get(bound, contents_path(_repo_path(o, r), path), {"ref": commit})
    return dir_entries(rows, path)


async def read_file(bound: Bound, o: str, r: str, commit: str, path: str) -> dict:
    commit = sha(commit)
    if path == "":
        raise ConnectorError("refused", "not a file path")
    f = await _get(bound, contents_path(_repo_path(o, r), path), {"ref": commit})
    return file_text(f, path, commit)
