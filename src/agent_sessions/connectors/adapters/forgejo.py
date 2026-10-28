"""The `forgejo-api` adapter (#1750): read-only, exactly these routes under the instance's origin.

* ``GET /api/v1/user`` and one ``repos/search`` page — the probe
* ``GET /api/v1/repos/search?q=`` — repos
* ``GET /api/v1/repos/issues/search?q=&type=issues|pulls`` — issues and PRs across repos
* ``GET /api/v1/repos/{o}/{r}`` and ``…/branches/{branch}`` — a repo, its default-branch head sha
* ``GET /api/v1/repos/{o}/{r}/issues/{n}`` and ``…/issues/{n}/comments`` — one issue (bounded)
* ``GET /api/v1/repos/{o}/{r}/pulls/{n}`` — a PR's head sha
* ``GET /api/v1/repos/{o}/{r}/commits/{sha}/status`` — the combined status
* ``GET /api/v1/repos/{o}/{r}/contents/{path}?ref={sha}`` — a directory, or one text file

Every path is built here from VALIDATED parts, each segment percent-encoded on its own; nothing is
concatenated from model text. The adapter holds no token and no client: `connector_http._request`
attaches the credential and is the only thing that sends. Returned text is raw third-party text;
the HQ layer (`connectors.hq`) guards and labels it before the model sees any of it.
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import quote

from ... import connector_http
from ...connector_http import Bound, ConnectorError

NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
BRANCH_MAX = 250
PATH_MAX = 1000
SEGMENT_MAX = 255
SEARCH_LIMIT = 20
COMMENTS_MAX = 50
CHECKS_KEPT = 50
#: Forgejo's maximum page size for the commit-status route, and how many pages a count may read.
STATUS_PAGE_SIZE = 50
STATUS_PAGES_MAX = 6
DIR_MAX = 200
FILE_MAX_BYTES = 256 * 1024


def _seg(part: str) -> str:
    return quote(part, safe="")


def valid_name(v: object) -> bool:
    return isinstance(v, str) and NAME_RE.fullmatch(v) is not None and not v.endswith(".git")


def valid_path(p: object) -> bool:
    """A repository-relative path: segments, no empty, ``.`` or ``..`` segment, no leading
    slash, no control characters, bounded."""
    if not isinstance(p, str) or len(p) > PATH_MAX:
        return False
    if p == "":
        return True
    if p.startswith("/") or "\\" in p or not p.isprintable():
        return False
    parts = p.split("/")
    return all(s not in ("", ".", "..") and len(s) <= SEGMENT_MAX for s in parts)


def _repo_path(o: str, r: str) -> str:
    if not (valid_name(o) and valid_name(r)):
        raise ConnectorError("refused", "not a repository name")
    return f"/api/v1/repos/{_seg(o)}/{_seg(r)}"


async def _get(bound: Bound, path: str, params: dict | None = None) -> object:
    return await connector_http._request(bound, "GET", path, params)


def _str(v: object, cap: int = 100_000) -> str:
    return v[:cap] if isinstance(v, str) else ""


def _int(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _login(user: object) -> str:
    return _str(user.get("login"), 100) if isinstance(user, dict) else ""


def _sha(v: object) -> str:
    if not isinstance(v, str) or not SHA_RE.fullmatch(v):
        raise ConnectorError("bad_reply", "the forge answered without a commit sha")
    return v


async def probe(bound: Bound) -> dict:
    user = await _get(bound, "/api/v1/user")
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise ConnectorError("bad_reply", "the forge did not answer as Forgejo does")
    page = await _get(bound, "/api/v1/repos/search", {"limit": SEARCH_LIMIT})
    data = page.get("data") if isinstance(page, dict) else None
    if not isinstance(data, list):
        raise ConnectorError("bad_reply", "the forge did not answer as Forgejo does")
    return {"login": user["login"], "repos": len(data)}


async def search(bound: Bound, kind: str, query: str) -> list[dict]:
    """``[{owner, repo, number?, title, state?, description?}]`` — raw text."""
    if kind == "repos":
        page = await _get(bound, "/api/v1/repos/search", {"q": query, "limit": SEARCH_LIMIT})
        rows = page.get("data") if isinstance(page, dict) else None
        out: list[dict] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            owner = (
                (row.get("owner") or {}).get("login")
                if isinstance(row.get("owner"), dict)
                else None
            )
            name = row.get("name")
            if valid_name(owner) and valid_name(name):
                out.append(
                    {"owner": owner, "repo": name, "title": f"{owner}/{name}",
                     "description": _str(row.get("description"))}
                )  # fmt: skip
        return out[:SEARCH_LIMIT]
    if kind not in ("issues", "pulls"):
        raise ConnectorError("refused", "kind must be repos, issues or pulls")
    rows = await _get(
        bound,
        "/api/v1/repos/issues/search",
        # Forgejo's cross-repository search defaults to OPEN only; past discussion is closed.
        {"q": query, "type": kind, "state": "all", "limit": SEARCH_LIMIT},
    )
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        repo = row.get("repository") if isinstance(row.get("repository"), dict) else {}
        owner, name, number = repo.get("owner"), repo.get("name"), row.get("number")
        if valid_name(owner) and valid_name(name) and _int(number):
            out.append(
                {
                    "owner": owner,
                    "repo": name,
                    "number": number,
                    "title": _str(row.get("title")),
                    "state": _str(row.get("state"), 20),
                    "pull": row.get("pull_request") is not None,
                }
            )
    return out[:SEARCH_LIMIT]


async def read_repo(bound: Bound, o: str, r: str) -> dict:
    base = _repo_path(o, r)
    repo = await _get(bound, base)
    if not isinstance(repo, dict):
        raise ConnectorError("bad_reply", "the forge answered something other than a repository")
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch or len(branch) > BRANCH_MAX:
        raise ConnectorError("bad_reply", "the repository has no default branch")
    head = await _get(bound, f"{base}/branches/{_seg(branch)}")
    commit = head.get("commit") if isinstance(head, dict) else None
    sha = _sha(commit.get("id") if isinstance(commit, dict) else None)
    return {
        "owner": o,
        "repo": r,
        "description": _str(repo.get("description")),
        "default_branch": branch,
        "head_sha": sha,
        "open_issues": _int(repo.get("open_issues_count")),
        "open_pulls": _int(repo.get("open_pr_counter")),
        "archived": bool(repo.get("archived")),
    }


async def read_issue(bound: Bound, o: str, r: str, n: int) -> dict:
    base = _repo_path(o, r)
    if not _int(n):
        raise ConnectorError("refused", "not an issue number")
    issue = await _get(bound, f"{base}/issues/{n}")
    if not isinstance(issue, dict):
        raise ConnectorError("bad_reply", "the forge answered something other than an issue")
    # Forgejo's comments route returns the WHOLE thread (it ignores `limit`). A thread over the
    # transport's body cap degrades to the issue alone, said so, rather than failing the read.
    comments_complete = True
    try:
        comments = await _get(bound, f"{base}/issues/{n}/comments")
    except ConnectorError as e:
        if e.code != "too_large":
            raise
        comments, comments_complete = [], False
    rows = comments if isinstance(comments, list) else []
    out = {
        "owner": o,
        "repo": r,
        "number": n,
        "title": _str(issue.get("title")),
        "state": _str(issue.get("state"), 20),
        "author": _login(issue.get("user")),
        "body": _str(issue.get("body")),
        "comments": [
            {
                "author": _login(c.get("user")),
                "created": _str(c.get("created_at"), 40),
                "body": _str(c.get("body")),
            }
            for c in rows[-COMMENTS_MAX:]
            if isinstance(c, dict)
        ],
        "pull": None,
        "comments_complete": comments_complete,
        "comments_total": len(rows) if comments_complete else None,
    }  # fmt: skip
    if issue.get("pull_request") is not None:
        pr = await _get(bound, f"{base}/pulls/{n}")
        head = pr.get("head") if isinstance(pr, dict) else None
        out["pull"] = {
            "head_sha": _sha(head.get("sha") if isinstance(head, dict) else None),
            "merged": bool(pr.get("merged")) if isinstance(pr, dict) else False,
        }
    return out


#: Forgejo's commit-status severity, worst first: the combined state is the worst of every
#: context's latest state. A state this build does not know counts as `pending` (never green).
STATUS_SEVERITY = ("error", "failure", "warning", "pending", "success")


def _severity(state: str) -> int:
    return (
        STATUS_SEVERITY.index(state)
        if state in STATUS_SEVERITY
        else STATUS_SEVERITY.index("pending")
    )


async def read_ci(bound: Bound, o: str, r: str, sha: str) -> dict:
    """The combined state over ALL contexts, the ``CHECKS_KEPT`` worst checks, and how many
    there are.

    Forgejo PAGES this route, its page cap is an instance setting (``MAX_RESPONSE_ITEMS``), and
    the ``state`` it returns is computed over THAT PAGE only. So the walk:

    * takes the page size from what page 1 actually returned, goes on while pages come back that
      full, and ends at a shorter or empty page, or one that adds no new context (a forge that
      ignores ``page``);
    * identifies a context by its FULL text, uncut (only the shown text is cut; memory is
      already bounded by the transport's body cap times ``STATUS_PAGES_MAX``);
    * computes the combined state itself, the worst over every distinct context
      (`STATUS_SEVERITY`), and keeps the worst checks first, so a failure is never hidden.

    If ``STATUS_PAGES_MAX`` pages are read without reaching the end, the total is a lower bound
    (``checks_total_capped``) and the state is only what was seen: a failure seen stays a
    failure, but nothing seen-so-far can claim ``success`` (``incomplete``)."""
    base = _repo_path(o, r)
    sha = _sha(sha)
    latest: dict[str, str] = {}  # full context → its state, in first-seen order
    page_size = 0
    capped = True
    for page in range(1, STATUS_PAGES_MAX + 1):
        st = await _get(
            bound, f"{base}/commits/{sha}/status", {"page": page, "limit": STATUS_PAGE_SIZE}
        )
        if not isinstance(st, dict):
            raise ConnectorError("bad_reply", "the forge answered something other than a status")
        rows = st.get("statuses") if isinstance(st.get("statuses"), list) else []
        rows = [c for c in rows if isinstance(c, dict)]
        if page == 1:
            page_size = len(rows)
        fresh = 0
        for c in rows:
            raw = c.get("context")
            context = raw if isinstance(raw, str) else ""  # the FULL text is the identity
            if context in latest:
                continue
            latest[context] = _str(c.get("status"), 20)
            fresh += 1
        if not rows or fresh == 0 or len(rows) < page_size:
            capped = False  # reached the end
            break
    if latest:
        worst = min(latest.values(), key=_severity)
        state = worst if worst in STATUS_SEVERITY else "pending"
    else:
        state = ""  # no statuses at all: Forgejo reports this as pending with zero contexts
    if capped and state not in ("error", "failure"):
        state = "incomplete"
    ordered = sorted(latest.items(), key=lambda kv: _severity(kv[1]))  # stable: worst first
    return {
        "sha": sha,
        "state": state,
        "checks": [{"context": c[:200], "state": st} for c, st in ordered[:CHECKS_KEPT]],
        "checks_total": len(latest),
        "checks_total_capped": capped,
    }


def _contents_path(o: str, r: str, path: str) -> str:
    if not valid_path(path):
        raise ConnectorError("refused", "not a repository path")
    base = f"{_repo_path(o, r)}/contents"
    return base if path == "" else base + "/" + "/".join(_seg(s) for s in path.split("/"))


async def list_dir(bound: Bound, o: str, r: str, sha: str, path: str) -> list[dict]:
    sha = _sha(sha)
    rows = await _get(bound, _contents_path(o, r, path), {"ref": sha})
    if not isinstance(rows, list):
        raise ConnectorError("refused", "that path is not a directory")
    out = []
    for e in rows[:DIR_MAX]:
        if not isinstance(e, dict):
            continue
        name, kind = e.get("name"), e.get("type")
        child = f"{path}{name}" if path == "" or path.endswith("/") else f"{path}/{name}"
        if isinstance(name, str) and kind in ("dir", "file") and valid_path(child):
            out.append({"name": name, "type": kind, "path": child, "size": _int(e.get("size"))})
    return out


async def read_file(bound: Bound, o: str, r: str, sha: str, path: str) -> dict:
    sha = _sha(sha)
    if path == "":
        raise ConnectorError("refused", "not a file path")
    f = await _get(bound, _contents_path(o, r, path), {"ref": sha})
    if not isinstance(f, dict) or f.get("type") != "file":
        raise ConnectorError("refused", "that path is not a file")
    if _int(f.get("size")) > FILE_MAX_BYTES or f.get("content") is None:
        raise ConnectorError("too_large", "that file is too large to read here")
    if f.get("encoding") != "base64" or not isinstance(f.get("content"), str):
        raise ConnectorError("bad_reply", "the forge answered the file in an unknown encoding")
    try:
        raw = base64.b64decode(f["content"], validate=False)
    except (binascii.Error, ValueError):
        raise ConnectorError("bad_reply", "the file's content could not be decoded") from None
    if len(raw) > FILE_MAX_BYTES:
        raise ConnectorError("too_large", "that file is too large to read here")
    if b"\x00" in raw:
        raise ConnectorError("binary", "that file is binary; only text is read")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConnectorError("binary", "that file is not UTF-8 text; only text is read") from None
    return {"path": path, "sha": sha, "size": len(raw), "text": text}
