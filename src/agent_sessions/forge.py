"""A READ-ONLY forge client — the evidence half of MISSION CONTROL's follow-through (#891).

#840's premise is that a mission knows what finishing means and *checks* it: "those are facts a
server can check, and mission control checks them." #883 shipped the vocabulary — the closed
`PROBE_KINDS` set, a per-kind argument schema, and an authority model in which a model may select
a template index and can never author a probe target. This module is the half that goes and looks.

**Read-only means there is no write method on this class at all.** Not "we do not call one" — the
class does not define one, and `tests/test_forge.py` asserts the absence by name over the public
surface. An adapter that *could* merge a PR is one bug away from merging one, and this runs
unattended on a timer with nobody watching. The narrowness is the safety property; a helpful
`post()` added later would silently remove it, so the test is the thing that has to fail.

**This module is a new outbound-HTTP capability**, and it joins the inventory in
`tests/test_prompts_registry.py` in the same change that adds the first call — `HTTP_MODULES` for
"may hold a client at all", and a counted `POST_SITES` entry so a *second* call beside an approved
one is a mismatch rather than an inherited pass. The AST checker is an accident detector rather
than a boundary (CLAUDE.md is explicit about that); the inventory is how a new door becomes a
review conversation instead of a diff nobody reads twice.

**`unknown` is a first-class answer and it is not `failed`.** Three outcomes, kept apart at every
layer above this one:

* observed-true — the fact holds;
* observed-false — we looked, and it does not;
* **could not look** — no credential, endpoint down, timeout, a shape we do not understand.

Collapsing the third into either of the others is the same lie as a degraded probe re-serving its
last answer as current, one layer up. So every method returns a `Fact`, and a `Fact` carries
whether it is an observation at all.

**Credentials never leave the process.** The token is read server-side, sent only as a header,
and never placed on a command line (`ps` is world-readable on this host), never echoed into an
error body, and never written to a log — `_why` names the exception KIND and nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger(__name__)

#: Per-request timeout. A probe is a fallible external call on a timer, so it is bounded twice:
#: here, and by the sweep that calls it. Deliberately short — this asks a forge for one small JSON
#: document, and a forge that cannot answer in this long is "could not look", which is a real and
#: useful answer.
DEFAULT_TIMEOUT_S = 15.0

#: Bytes of a forge answer this module will accept. A PR list or a status rollup is a few KiB; a
#: megabyte is a fault or an attack, and either way it must cost a bounded amount. Enforced on the
#: STREAM, so an over-cap body is abandoned rather than downloaded and then measured.
RESPONSE_MAX = 512 * 1024

#: How much is read at a time. The cap above bounds the TOTAL; this bounds a single allocation,
#: which is a different guarantee — see `_get`.
READ_CHUNK = 64 * 1024


def request_timeout() -> float:
    raw = os.getenv("AGENT_SESSIONS_FORGE_TIMEOUT")
    try:
        v = float(raw) if raw else DEFAULT_TIMEOUT_S
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return v if 1.0 <= v <= 120.0 else DEFAULT_TIMEOUT_S


#: Test seam, mirroring `review._TRANSPORT`. CI never touches the network: the tests install an
#: `httpx.MockTransport` here. It is a module global rather than a constructor argument so the
#: production call path has no parameter a caller could point somewhere else.
_TRANSPORT: httpx.BaseTransport | None = None


def set_transport_for_test(t: httpx.BaseTransport | None) -> None:
    global _TRANSPORT
    _TRANSPORT = t


class ForgeError(RuntimeError):
    """The adapter could not answer. Never conflated with "the fact is false"."""


@dataclass(frozen=True)
class Fact:
    """One observation, or an explicit refusal to claim one.

    ``observed`` is the discriminator and it is checked BEFORE ``value`` everywhere: a `Fact` with
    ``observed=False`` has a meaningless ``value``, and code that reads the value first turns "we
    could not look" into "it is not there".
    """

    observed: bool
    value: bool = False
    #: What was seen, for the operator and for the timeline. Never carries a credential, a token,
    #: or a raw exception repr.
    detail: str = ""
    #: Free-form, bounded, JSON-safe extras the objective row keeps (a PR number, a head SHA).
    extra: dict | None = None

    @staticmethod
    def unknown(why: str) -> Fact:
        return Fact(observed=False, detail=why)

    @staticmethod
    def seen(value: bool, detail: str = "", **extra) -> Fact:
        return Fact(observed=True, value=value, detail=detail, extra=extra or None)


def _why(e: Exception) -> str:
    """What to TELL THE OPERATOR about a failure, and it depends on who wrote the message.

    A `ForgeError` raised in this module carries an authored literal — "forge refused the
    credential", "the forge redirected (302) — …" — with no URL and no credential in it, and those
    are exactly the sentences an operator needs to act on. Anything else is named by CLASS and
    nothing more: an httpx error's repr embeds the request URL, and a URL can carry a token in a
    query string even though this module never puts one there. Same rule `mission_context`'s git
    read follows.
    """
    if isinstance(e, ForgeError):
        return str(e)
    return f"forge unreachable ({type(e).__name__})"


class ForgeClient:
    """The read side of one forge. **No method here writes anything.**

    `kind` selects the response shapes. `forgejo` is what this org runs and is what the tests
    drive; `gitea` shares its API surface (Forgejo is a Gitea fork and the endpoints used here are
    identical), and `github` differs in three places that are handled explicitly rather than hoped
    for: the checks rollup lives under a different path, reviews report `state` in upper case, and
    a merged PR is `merged: true` alongside `state: "closed"` rather than a distinct state.
    """

    #: The kinds whose response shapes are handled. An unknown kind is a refusal at construction,
    #: not a silent fall-through to the Forgejo shapes — guessing at a shape is how a probe
    #: reports a confident wrong answer.
    KINDS = ("forgejo", "gitea", "github")

    def __init__(self, *, kind: str, base_url: str, token: str = "", owner: str = "") -> None:
        if kind not in self.KINDS:
            raise ForgeError(f"unknown forge kind {kind!r}")
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ForgeError("no forge base_url configured")
        parts = urlsplit(base)
        # The same rule the probe-target validator applies to `http_status`: a scheme this module
        # does not speak is a request of a kind the operator did not think they were configuring.
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ForgeError("forge base_url must be an http(s) URL")
        self.kind = kind
        self.base = base
        self._token = token or ""
        self.owner = (owner or "").strip()

    # -------------------------------------------------------------- wire

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            # A header, and only ever a header. Never a query parameter (URLs are logged by
            # proxies and land in `_why`-adjacent places), never an argv.
            h["Authorization"] = (
                f"Bearer {self._token}" if self.kind == "github" else f"token {self._token}"
            )
        return h

    def _get(self, path: str, params: dict | None = None) -> object:
        """THE one outbound call in this module. Counted in `POST_SITES`.

        `trust_env=False` for the same reason `review._client` sets it: httpx otherwise honours
        ambient `HTTP_PROXY` / `SSL_CERT_FILE` from whatever environment the service happens to
        inherit, which makes the request's actual destination depend on the shell that started it.

        TLS verification is left at its default (on). There is no flag here to turn it off, which
        is deliberate — a probe that trusts any certificate is a probe whose answer means nothing.

        **STREAMED, with the cap applied to the DOWNLOAD.** `client.get()` buffers the whole
        response and a cap applied to the result afterwards has already paid for every byte — a
        1 MiB answer arrives in full before a 64 KiB limit gets a look at it (#897 review). A
        forge that returns an unbounded body, by fault or by design, must cost this process a
        bounded amount, so the stream is abandoned at the cap.

        There is also a TOTAL deadline, not just a per-read timeout: a peer that keeps yielding a
        byte before every timeout expires can hold a connection open indefinitely without any
        single read ever being slow.
        """
        url = f"{self.base}{path}"
        budget = request_timeout()
        deadline = time.monotonic() + budget
        # Redirects are not followed here either — same reasoning as the objective probes, and
        # httpx's default is already `False`, stated explicitly so it cannot be relaxed silently.
        with httpx.Client(
            timeout=budget, transport=_TRANSPORT, trust_env=False, follow_redirects=False
        ) as client:
            with client.stream("GET", url, params=params or None, headers=self._headers()) as r:
                if r.status_code < 400:
                    chunks: list[bytes] = []
                    size = 0
                    # THE CHUNK SIZE IS THE BOUND, not just the running total (#897 re-review 5,
                    # finding 6). Checking after a yield means the yield itself is unbounded: one
                    # 2 MiB decoded chunk sails through a 512 KiB cap because the check runs once
                    # it is already in memory. A compressed response has exactly that shape —
                    # httpx decodes before it yields, so the wire size says nothing about what is
                    # allocated. Asking for a bounded chunk puts the limit in front of the
                    # allocation instead of behind it.
                    for chunk in r.iter_bytes(chunk_size=READ_CHUNK):
                        if time.monotonic() > deadline:
                            raise ForgeError("the forge took too long to answer")
                        size += len(chunk)
                        if size > RESPONSE_MAX:
                            raise ForgeError(f"the forge answered more than {RESPONSE_MAX} bytes")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                else:
                    body = b""
        if 300 <= r.status_code < 400:
            # A REDIRECT IS NOT A RESULT (#897 re-review, finding 3). The operator authorised one
            # authority; following a 30x lets the far end name a different one and sends the
            # configured token there. Refused rather than allowlisted — an allowlist of redirect
            # destinations is a blocklist problem in disguise.
            raise ForgeError(
                f"the forge redirected ({r.status_code}) — point the objective at the final "
                "address; a probe does not follow redirects"
            )
        if r.status_code == 401 or r.status_code == 403:
            # NOT a false fact: "we were not allowed to look" is unknown, and reporting it as
            # "the PR is not there" would let a mis-scoped token read as a mission going backwards.
            raise ForgeError("forge refused the credential")
        if r.status_code == 404:
            raise ForgeError("forge path not found")
        if r.status_code >= 400:
            raise ForgeError(f"forge answered {r.status_code}")
        try:
            return json.loads(body)
        except ValueError as e:
            raise ForgeError(f"forge answer was not JSON ({type(e).__name__})") from None

    @staticmethod
    def _seg(s: str) -> str:
        """One path segment, escaped. A repo or branch name reaches here from operator config or
        from a git remote; neither is a reason to let a `../` into a request path."""
        return quote(str(s), safe="")

    def _repo_path(self, repo: str) -> str:
        r = (repo or "").strip().strip("/")
        if "/" in r:
            owner, name = r.split("/", 1)
        else:
            owner, name = self.owner, r
        if not owner or not name:
            raise ForgeError("no repository to ask about")
        return f"/repos/{self._seg(owner)}/{self._seg(name)}"

    @property
    def _api(self) -> str:
        return "" if self.kind == "github" else "/api/v1"

    # -------------------------------------------------------------- reads

    #: Pages of pull requests to walk before giving up. A branch's PR is normally on page 1;
    #: 50 rows was a silent ceiling that returned "no PR" for a busy repository (#897 review).
    #: Bounded rather than exhaustive: an unbounded walk is an unbounded probe.
    PR_PAGE = 50
    PR_MAX_PAGES = 6

    def _head_matches(self, pr: dict, repo: str, branch: str) -> bool:
        """Does this PR's head identify the branch we mean?

        `head.ref` ALONE is not identity: a fork or another repository can have a branch of the
        same name, and matching on the name picked whichever came back first (#897 review).

        **A missing `head.repo` is a REFUSAL, not a fallback** (#897 re-review, finding 3, twice).
        The docstring said so for a round while the code returned `True`, which is the worst of
        both: a same-named fork, or one whose repository has since been deleted, is selected by
        branch label alone and then steers checks, review and merge evidence to a pull request
        that is not this mission's. There is no safe guess available — the field is the identity —
        so an answer that omits it does not match anything. The cost is an objective that stays
        unmet on a forge that will not say; the cost of the other direction is a mission settled
        on somebody else's PR.
        """
        head = pr.get("head")
        if not isinstance(head, dict) or str(head.get("ref") or "") != branch:
            return False
        hrepo = head.get("repo")
        if not isinstance(hrepo, dict):
            return False
        full = str(hrepo.get("full_name") or "")
        if not full:
            return False
        want = repo if "/" in repo else f"{self.owner}/{repo}"
        return full.lower() == want.strip("/").lower()

    def _find_pr(
        self, repo: str, branch: str, *, state: str, head_sha: str = ""
    ) -> tuple[dict | None, bool]:
        """`(pr, searched_all)` — the first PR in `state` whose head identifies `branch`.

        `head_sha` narrows it further to a specific commit — required for a CLOSED PR, where the
        branch name alone is not identity because names are reused.

        **The second element is the point** (#897 re-review, finding 4). Returning a bare `None`
        collapsed two different answers into one: "this repository has no such PR" and "the page
        budget ran out before we could say". The caller reported both as observed-false — "no PR
        for topic" — which is the "could not look is not false" contract broken at the one place
        the whole three-way split exists for, and it drives a nudge about work that may well have
        happened. `review()` and `run()` already keep the two apart; this now does too.
        """
        for page in range(1, self.PR_MAX_PAGES + 1):
            rows = self._get(
                f"{self._api}{self._repo_path(repo)}/pulls",
                {"state": state, "limit": self.PR_PAGE, "per_page": self.PR_PAGE, "page": page},
            )
            if not isinstance(rows, list):
                raise ForgeError("forge answered an unexpected shape")
            for pr in rows:
                if not isinstance(pr, dict) or not self._head_matches(pr, repo, branch):
                    continue
                if head_sha:
                    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
                    if str(head.get("sha") or "") != head_sha:
                        continue
                return pr, True
            if len(rows) < self.PR_PAGE:
                return None, True  # the last page, so the search WAS complete
        return None, False

    def pull_request(
        self,
        repo: str,
        branch: str,
        *,
        include_closed: bool = True,
        head_sha: str = "",
    ) -> Fact:
        """The pull request for this branch, open **or closed**.

        `include_closed` is on by default and that is the fix for a gate that could never be met:
        searching only OPEN pull requests means that the moment a PR merges it vanishes from the
        list, so `forge_merged` never got a number to ask about and reported "there is no open PR
        yet" for ever (#897 review). A merged PR is exactly the state that objective exists to
        observe.

        **A CLOSED PR must additionally match `head_sha`, and a branch name is not identity.**
        Branch names are reusable — `devopsagent/<slug>` is reused constantly here — so
        rediscovering a *closed* PR by name alone lets an older merged PR for the same name settle
        `forge_merged` for work that has not opened a PR at all. Permanently, since a merged PR
        never changes. An OPEN PR needs no such check: there can only be one open PR per head
        branch, so the name does identify it.

        Where the local head is unknown, a closed match is refused rather than guessed: `unknown`
        is a worse answer than a right one and a much better answer than a wrong one.
        """
        budget = self.PR_MAX_PAGES * self.PR_PAGE
        try:
            pr, complete = self._find_pr(repo, branch, state="open")
            state = "open"
            if pr is not None and head_sha:
                # THE OPEN PR HAS TO BE ABOUT THIS CHECKOUT (#897 re-review 5, finding 2).
                #
                # Only the CLOSED search used the local head, on the reasoning that a branch name
                # identifies at most one open PR. It does — but identifying the PR is not the same
                # as identifying the COMMIT, and everything downstream is about the commit: the
                # PR's head SHA is what `checks` and `review` are then asked about. With unpushed
                # work in the checkout, that settles a mission's gates on the last thing that was
                # pushed while the work being judged is still local.
                #
                # Unknown rather than false: the PR is really open and the operator has really not
                # pushed, and neither of those is "the objective does not hold".
                open_head = str(
                    (pr.get("head") if isinstance(pr.get("head"), dict) else {}).get("sha") or ""
                )
                if open_head and open_head != head_sha:
                    return Fact.unknown(
                        f"PR #{pr.get('number')} is open at {open_head[:12]}, and this checkout "
                        f"is at {head_sha[:12]} — push before its checks can be about this work"
                    )
            if pr is None and not complete:
                return Fact.unknown(
                    f"more than {budget} open pull requests, and none of the ones read is for "
                    f"{branch} — the search did not finish"
                )
            if pr is None and include_closed:
                if not head_sha:
                    return Fact.seen(
                        False,
                        f"no open PR for {branch}, and no local head to identify a closed one",
                    )
                candidate, complete = self._find_pr(repo, branch, state="closed", head_sha=head_sha)
                if candidate is None and not complete:
                    return Fact.unknown(
                        f"more than {budget} closed pull requests, and none of the ones read is "
                        f"at this head — the search did not finish"
                    )
                if candidate is None:
                    return Fact.seen(False, f"no PR for {branch} at this head")
                pr, state = candidate, "closed"
        except ForgeError as e:
            return Fact.unknown(str(e))
        except httpx.HTTPError as e:
            return Fact.unknown(_why(e))
        if pr is None:
            return Fact.seen(False, f"no PR for {branch}")
        num = pr.get("number")
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        sha = head.get("sha")
        return Fact.seen(
            state == "open",
            f"PR #{num} is {state} for {branch}",
            number=num,
            head_sha=str(sha) if sha else None,
            pr_state=state,
            merged=bool(pr.get("merged")),
        )

    def checks(self, repo: str, sha: str) -> Fact:
        """Are the checks for this head SHA green?

        **The rollup, not the per-check list.** The list carries stale duplicate `pending` rows,
        which is why the workflow doc tells agents to read `/status` and not `/statuses` — the same
        trap applies here, one layer down.

        Forgejo's Actions post no commit statuses at all on this install, so an EMPTY rollup is
        `unknown`, never green: "nothing reported" is not "everything passed", and treating it as
        green would let a mission's most important gate settle on an absence.
        """
        if not sha:
            return Fact.unknown("no head SHA to check")
        try:
            if self.kind == "github":
                data = self._get(f"{self._repo_path(repo)}/commits/{self._seg(sha)}/status")
            else:
                data = self._get(
                    f"{self._api}{self._repo_path(repo)}/commits/{self._seg(sha)}/status"
                )
        except ForgeError as e:
            return Fact.unknown(str(e))
        except httpx.HTTPError as e:
            return Fact.unknown(_why(e))
        if not isinstance(data, dict):
            return Fact.unknown("forge answered an unexpected shape")
        state = str(data.get("state") or "").lower()
        # A ROLLUP HAS TO BE SHAPED LIKE ONE (#897 re-review 5, finding 5). Rejecting only an
        # integer zero left two ways to be green on nothing: `{state: "success"}` with no count at
        # all, and `{state: "success", total_count: "0"}` — a string, which `isinstance(_, int)`
        # never matched. Both settled a mission's most important gate on an absence, which is the
        # exact claim `checks` exists to refuse; this install's Forgejo posts no statuses at all,
        # so "nothing reported" is the COMMON case rather than a malformed edge.
        #
        # So the count is required, must parse as an integer, and must be positive. Anything else
        # is `unknown`: we did not learn that the checks are green.
        raw_total = data.get("total_count")
        if isinstance(raw_total, bool) or raw_total is None:
            return Fact.unknown("forge answered a status rollup with no check count")
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            return Fact.unknown("forge answered a status rollup with an unreadable check count")
        if not state or total <= 0:
            return Fact.unknown("no checks have reported for this commit")
        if state == "success":
            return Fact.seen(True, "every reported check is green", state=state)
        if state in ("pending", ""):
            return Fact.seen(False, "checks are still running", state=state)
        return Fact.seen(False, f"checks are {state}", state=state)

    def review(self, repo: str, number: int, sha: str) -> Fact:
        """Has the PR been APPROVED at this head SHA?

        Filtered by `commit_id`, which is the whole point: an approval of an earlier revision is
        not an approval of what is about to merge, and the workflow says so explicitly.
        """
        if not number:
            return Fact.unknown("no PR to read reviews from")
        try:
            path = f"{self._api}{self._repo_path(repo)}/pulls/{int(number)}/reviews"
            # PAGED. The default page is not "every review": a PR with a long review history —
            # which is exactly what this org's PRs look like — pushes the current verdict off
            # page 1, and reading only that page returns "no review at the current head yet" for
            # an approved PR (#897 review).
            rows = []
            complete = False
            for page in range(1, self.PR_MAX_PAGES + 1):
                chunk = self._get(
                    path, {"limit": self.PR_PAGE, "per_page": self.PR_PAGE, "page": page}
                )
                if not isinstance(chunk, list):
                    return Fact.unknown("forge answered an unexpected shape")
                rows.extend(chunk)
                if len(chunk) < self.PR_PAGE:
                    complete = True
                    break
            if not complete:
                # THE PAGE BUDGET RAN OUT, so this is a PREFIX of the review history and reducing
                # it as if it were the whole thing can invert the verdict: an approval on page 1
                # and the same reviewer's later request-for-changes on page 7 read as "approved"
                # (#897 re-review, finding 5). An incomplete history is not a verdict.
                return Fact.unknown(
                    f"this PR has more than {self.PR_MAX_PAGES * self.PR_PAGE} reviews — "
                    "too many to read a current verdict from"
                )
        except (ForgeError, ValueError) as e:
            return Fact.unknown(str(e) if isinstance(e, ForgeError) else "bad PR number")
        except httpx.HTTPError as e:
            return Fact.unknown(_why(e))
        # PER REVIEWER, latest wins — not "whatever came back last" (#897 review). A list
        # reduced by response order lets one approval among several reviewers, or an approval
        # followed by that same reviewer requesting changes, decide the gate by accident.
        #
        # DISMISSED and STALE reviews are dropped outright: a dismissed approval is an approval
        # the forge has explicitly withdrawn, and treating it as current is the same class of
        # error as accepting one from a superseded revision.
        latest: dict[str, tuple[float, str]] = {}
        for rv in rows:
            if not isinstance(rv, dict):
                continue
            if rv.get("dismissed") is True or rv.get("stale") is True:
                continue
            # REVIEWER AUTHORITY IS REQUIRED, and how it is evidenced is provider-specific
            # (#897 re-review, finding 4).
            #
            # Forgejo and Gitea mark a review `official` when it comes from a requested or
            # permitted reviewer. GitHub has no such field — it reports `author_association` — so
            # treating "absent" as authorised meant any account that could SEE a GitHub PR could
            # settle its approval gate by approving it: the whole weight of the gate resting on a
            # field the provider never sends.
            #
            # So each provider is asked in its own terms, and a review whose authority cannot be
            # established does not count. Failing closed costs an `unknown`; failing open costs
            # the gate.
            if not self._authoritative(rv):
                continue
            # `commit_id` absent is not "matches" — an unfiltered review would let an approval of
            # a superseded revision settle the gate.
            if sha and str(rv.get("commit_id") or "") != sha:
                continue
            st = str(rv.get("state") or "").upper()
            if st not in ("APPROVED", "REQUEST_CHANGES", "CHANGES_REQUESTED"):
                continue
            who = str((rv.get("user") or {}).get("login") or rv.get("id") or "")
            when = float(rv.get("submitted_at_ts") or rv.get("id") or 0)
            prev = latest.get(who)
            if prev is None or when >= prev[0]:
                latest[who] = (when, st)
        states = {s for _, s in latest.values()}
        # ANY outstanding request for changes blocks, whatever else was said. An approval does not
        # override another reviewer's objection.
        if states & {"REQUEST_CHANGES", "CHANGES_REQUESTED"}:
            return Fact.seen(False, "changes were requested", state="REQUEST_CHANGES")
        if "APPROVED" in states:
            return Fact.seen(True, "approved at the current head", state="APPROVED")
        return Fact.seen(False, "no review at the current head yet")

    #: GitHub associations that carry merge-gating authority — `COLLABORATOR` and above. A
    #: `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR` or `NONE` can approve a public PR and must not
    #: settle anything.
    GITHUB_AUTHORITATIVE = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

    def _authoritative(self, rv: dict) -> bool:
        """Does this review come from somebody whose approval gates a merge?

        Provider-specific by necessity, and **fail-closed** on both branches: a forge that has not
        told us who may gate a merge has not told us this review counts.
        """
        if self.kind == "github":
            assoc = str(rv.get("author_association") or "").upper()
            return assoc in self.GITHUB_AUTHORITATIVE
        # Forgejo / Gitea: `official` is exactly this fact, and absent is not "yes".
        return rv.get("official") is True

    def merged(self, repo: str, number: int) -> Fact:
        """Is the PR merged? `merged` — never `state == "closed"`, which also means abandoned."""
        if not number:
            return Fact.unknown("no PR to check")
        try:
            data = self._get(f"{self._api}{self._repo_path(repo)}/pulls/{int(number)}")
        except (ForgeError, ValueError) as e:
            return Fact.unknown(str(e) if isinstance(e, ForgeError) else "bad PR number")
        except httpx.HTTPError as e:
            return Fact.unknown(_why(e))
        if not isinstance(data, dict):
            return Fact.unknown("forge answered an unexpected shape")
        if data.get("merged") is True:
            return Fact.seen(True, "merged", sha=str(data.get("merge_commit_sha") or "") or None)
        if str(data.get("state") or "").lower() == "closed":
            # Closed-unmerged is a real, DIFFERENT outcome from "not yet". The supervisor should
            # not keep nudging a PR somebody closed.
            return Fact.seen(False, "the PR was closed without merging", closed=True)
        return Fact.seen(False, "not merged yet")

    def run(self, repo: str, workflow: str, branch: str) -> Fact:
        """Did the most recent run of this workflow on this branch succeed?

        **The endpoint is provider-specific.** Forgejo and Gitea expose `/actions/tasks`; GitHub
        exposes `/actions/runs`, and sending a GitHub install to `/actions/tasks` gets a 404 —
        so a configured GitHub `forge_run` objective could never obtain the fact it advertises
        (#897 review). One place, chosen by kind, rather than a shape guess at the response.
        """
        try:
            if self.kind == "github":
                path = f"{self._repo_path(repo)}/actions/runs"
                base: dict = {"per_page": self.PR_PAGE}
                if branch:
                    base["branch"] = branch
            else:
                path = f"{self._api}{self._repo_path(repo)}/actions/tasks"
                base = {"limit": self.PR_PAGE}
            # PAGED, with the same incomplete-result discipline as the reviews (#897 re-review,
            # finding 7). A single page means newer runs from other workflows can strand a
            # perfectly valid objective indefinitely — and this repository fires several workflows
            # on every push, so that is the ordinary case rather than an edge.
            rows: list = []
            complete = False
            for page in range(1, self.PR_MAX_PAGES + 1):
                chunk = self._get(path, {**base, "page": page})
                if isinstance(chunk, dict):
                    chunk = chunk.get("workflow_runs") or []
                if not isinstance(chunk, list):
                    return Fact.unknown("forge answered an unexpected shape")
                rows.extend(chunk)
                if len(chunk) < self.PR_PAGE:
                    complete = True
                    break
        except ForgeError as e:
            return Fact.unknown(str(e))
        except httpx.HTTPError as e:
            return Fact.unknown(_why(e))
        # The response nests its entries under a MISLEADINGLY NAMED key — `workflow_runs` holds
        # TASKS, and `.id` is a task id rather than a run number. Documented in the workflow;
        # repeated here because reading it as runs is the mistake it invites.
        # Both providers nest under the SAME misleading key — on Forgejo `workflow_runs` holds
        # TASKS and `.id` is a task id, not a run number. Unwrapped per page above; noted here
        # because reading them as runs is the mistake the name invites.
        for t in rows:
            if not isinstance(t, dict):
                continue
            if workflow and str(t.get("name") or "") != workflow:
                continue
            if branch:
                # AN ABSENT BRANCH IS NOT A MATCH (#897 re-review 5, finding 4). Accepting `""`
                # meant a run the provider did not attribute to any branch settled a
                # branch-scoped gate — the same fail-open shape as the missing `head.repo` on a
                # pull request, one endpoint along. A run we cannot place is not this branch's.
                if str(t.get("head_branch") or "") != branch:
                    continue
            status = str(t.get("status") or "").lower()
            if status in ("success", "completed"):
                concl = str(t.get("conclusion") or "").lower()
                if not concl:
                    if self.kind == "github":
                        # GitHub always reports a conclusion on a completed run. Its ABSENCE is a
                        # shape we do not understand, and reading it as success is inventing the
                        # answer — the one thing a completed-but-unconcluded run does not tell us.
                        return Fact.unknown("the run says it completed but reported no conclusion")
                    # Forgejo tasks use `status` alone and legitimately carry no `conclusion`;
                    # `status == "success"` IS the verdict there.
                    if status != "success":
                        return Fact.unknown(
                            f"the run says it is {status} but reported no conclusion"
                        )
                    return Fact.seen(True, "the last run succeeded")
                ok = concl == "success"
                return Fact.seen(ok, f"the last run {'succeeded' if ok else concl}")
            return Fact.seen(False, f"the last run is {status or 'unknown'}")
        if not complete:
            return Fact.unknown(
                "the requested workflow was not among the newest "
                f"{self.PR_MAX_PAGES * self.PR_PAGE} runs"
            )
        return Fact.unknown("no run found for that workflow")
