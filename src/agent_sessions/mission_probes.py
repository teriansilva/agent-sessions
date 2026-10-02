"""The probe runner — what turns an objective from a sentence into a fact (#891, Phase 5b of #840).

#883 shipped the vocabulary and the authority model; #885 shipped the supervisor that follows
through. Neither could settle anything, because nothing went and looked: `missions.py:202` says
"Phase 1 has no probe runner", and Phase 5a did not add one. So `patch_objectives` could only ever
write `waived`, `assess().likely_done` was reachable only by waiving every gate, and the
supervisor nudged against objectives that could not move. This module closes that.

**The authority model is #883's, unchanged, and this module must not weaken it.** A model selects
a playbook template by INDEX; `probe` and `probe_args` are read from the operator's template and
are refused as model input at every route. So the runner reads its target from the **stored
objective row** and from operator config — never from anything a model produced, in this pass or
any other. That is what makes "no request is issued for a model-proposed URL" assertable on the
HTTP client rather than merely on a stored row: there is no path from model text to a target at
all, so there is nothing to fail open.

**Three outcomes, and the third is the one that matters.** Observed-true settles; observed-false
records the fact and leaves the objective unmet for the supervisor to act on; **could-not-look**
settles nothing, stamps the time, and renders stale with its reason. Collapsing that third case
into either of the others is exactly the "degraded probe silently reports the previous answer as
current" lie #840 names — the same shape as a stale 200, one layer up.

**`service_live` is not `change_live`.** `http_status` asks whether *something* answers;
`http_revision` asks whether **this** revision is live, by looking for a marker the server already
knows about (the merge SHA, or an operator-supplied string) in the response. A target that answered
200 before a deploy — and a stale instance still answering 200 after it — leaves a revision
objective unsettled while a status objective may legitimately go green. Two probes because they are
two questions.

**A probe is a fallible external call on a timer**, so it follows the pattern `agent_usage`
established: bounded per sweep, off the event loop, single-flight, with a failure keeping the last
good figures rather than blanking the row.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import forge as forge_mod
from . import gitpanel, missions, prefs

log = logging.getLogger(__name__)

#: Probe kinds this runner evaluates. `none` is the operator's to settle and `supervisor_judged` is
#: the supervisor's independent reading of the session output (#1088, `mission_judge`) — neither is
#: a fact a server can fetch, so neither is run here.
#: Listed explicitly rather than derived by subtraction: a kind added to `PROBE_KINDS` should have
#: to be considered here rather than silently inherit a runner.
MECHANICAL: frozenset[str] = frozenset(
    {
        "git_local",
        "forge_pr",
        "forge_checks",
        "forge_review",
        "forge_merged",
        "forge_run",
        "http_status",
        "http_revision",
    }
)

#: How many objectives one mission's sweep will probe. A ceiling on WORK per pass; the sweep
#: revisits, so nothing is permanently skipped.
PROBES_PER_MISSION = 12

DEFAULT_HTTP_TIMEOUT_S = 10.0

#: Bytes of a revision probe's response body that are read. A marker check does not need the whole
#: page, and an unbounded read is a probe that a large response can stall.
REVISION_BODY_MAX = 64 * 1024

_TRANSPORT: httpx.BaseTransport | None = None


def set_transport_for_test(t: httpx.BaseTransport | None) -> None:
    """Test seam for the two HTTP probes, mirroring `forge.set_transport_for_test`."""
    global _TRANSPORT
    _TRANSPORT = t


def http_timeout() -> float:
    raw = os.getenv("AGENT_SESSIONS_PROBE_TIMEOUT")
    try:
        v = float(raw) if raw else DEFAULT_HTTP_TIMEOUT_S
    except ValueError:
        return DEFAULT_HTTP_TIMEOUT_S
    return v if 1.0 <= v <= 120.0 else DEFAULT_HTTP_TIMEOUT_S


Fact = forge_mod.Fact


# ---------------------------------------------------------------- targets


def _repo_for(mission: dict, args: dict) -> str:
    """Which repository a forge probe is about.

    Order, and it is deliberate: the objective's own `repo` (an operator typed it into a playbook),
    then the mission checkout's `origin` remote, then nothing. Never a model-authored value —
    `probe_args` is refused as model input at the route, so anything here came from a human or
    from the checkout itself.
    """
    named = str(args.get("repo") or "").strip()
    if named:
        return named
    cwd = str(mission.get("cwd") or "")
    if not cwd:
        return ""
    url = gitpanel.remote_url(cwd)
    if not url:
        return ""
    # `owner/name` out of either URL shape, without trusting the rest of it. The value is parsed
    # and never executed: it reaches a request PATH (escaped by the adapter) and nothing else.
    path = url
    if "://" in url:
        path = urlsplit(url).path
    elif ":" in url:  # scp-style `git@host:owner/name.git`
        path = url.split(":", 1)[1]
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def _branch_for(mission: dict, args: dict) -> str:
    named = str(args.get("branch") or "").strip()
    if named:
        return named
    cwd = str(mission.get("cwd") or "")
    if not cwd:
        return ""
    try:
        return str(gitpanel.git_status(cwd).get("branch") or "")
    except Exception:  # noqa: BLE001 — an unreadable checkout is `unknown`, never a false fact
        return ""


# ---------------------------------------------------------------- the probes


def _probe_git_local(mission: dict, args: dict) -> Fact:
    """Is there a branch, and is it not the default one?

    "A branch exists" is the shipped playbook's first gate, and it means *work has somewhere to
    live* — so being on `main` does not satisfy it. The check is deliberately weak: it is the one
    objective that needs no network, and its job is to notice that nothing has started yet.
    """
    cwd = str(mission.get("cwd") or "")
    if not cwd:
        return Fact.unknown("the mission has no folder yet")
    try:
        st = gitpanel.git_status(cwd)
    except Exception as e:  # noqa: BLE001
        return Fact.unknown(f"the checkout could not be read ({type(e).__name__})")
    if st.get("repo") is None:
        return Fact.unknown("that folder is not a git repository")
    branch = str(st.get("branch") or "")
    want = str(args.get("branch") or "").strip()
    if want:
        return Fact.seen(branch == want, f"on {branch or 'a detached HEAD'}", branch=branch)
    if not branch:
        return Fact.seen(False, "the checkout is on a detached HEAD")
    if branch in ("main", "master"):
        return Fact.seen(False, f"still on {branch}", branch=branch)
    return Fact.seen(True, f"on {branch}", branch=branch)


def _client_for(target: Target) -> forge_mod.ForgeClient | None:
    """The client for the authority this probe was BOUND to — not for whatever prefs say now.

    Re-reading `prefs.get_forge()` here was the last hole in the target fence (#897 re-review,
    finding 1). The digest is taken from A, the request went to whatever the config held at the
    moment the client was built, and the digest taken afterwards read A again — so an A→B→A
    change during the request settled an answer from B against a row bound to A, with every
    comparison agreeing. A digest cannot see an ABA; not re-reading can.

    So the destination comes from the `Target` itself and the config is consulted for exactly one
    thing: the CREDENTIAL, which is not part of where a request goes and is deliberately not in
    the digest. `prefs` drops a retained token when the host changes, so the token on hand always
    belongs to the host currently configured — and if that is no longer the bound host, the
    request is refused below rather than sent with the wrong key.
    """
    if not target.forge_enabled or not target.forge_base.strip():
        return None
    cfg = {}
    with contextlib.suppress(Exception):
        cfg = prefs.get_forge()
    # The token is only usable when the configured authority is still the bound one. Anything
    # else would send a credential minted for one host to another.
    same = (
        str(cfg.get("kind") or "") == target.forge_kind
        and str(cfg.get("base_url") or "") == target.forge_base
    )
    try:
        return forge_mod.ForgeClient(
            kind=target.forge_kind,
            base_url=target.forge_base,
            token=str(cfg.get("token") or "") if same else "",
            owner=target.forge_owner,
        )
    except forge_mod.ForgeError:
        return None


def _probe_http(kind: str, args: dict, mission: dict) -> Fact:
    """`http_status` and `http_revision` — the only two probes that fetch an operator URL.

    The URL is REQUIRED by the schema and can only have come from a playbook a human typed, which
    is the whole SSRF answer: there is no server-derivable default and no model path to this value.
    """
    url = str(args.get("url") or "")
    if not url:
        return Fact.unknown("no URL configured")
    budget = http_timeout()
    deadline = time.monotonic() + budget
    status = 0
    body = ""
    try:
        with httpx.Client(
            timeout=budget,
            transport=_TRANSPORT,
            trust_env=False,
            # REDIRECTS ARE NOT FOLLOWED (#897 re-review, finding 3).
            #
            # The operator authorised ONE address. `follow_redirects=True` let the far end choose
            # the next one, so a public endpoint could bounce the server to loopback, RFC1918 or
            # `169.254.169.254` — and the two probes then hand back exactly the two oracles that
            # makes useful: `http_status` a status oracle, `http_revision` a body-marker oracle.
            # A reproduction followed a 302 to the metadata address and settled the marker true.
            #
            # Not followed at all rather than validated per hop: an allowlist of destinations is a
            # blocklist problem in disguise (every round is another "did we think of this scheme /
            # rebind / IPv6 form"), and a probe target that answers with a redirect is a
            # misconfiguration the operator should see rather than one the server should chase.
            follow_redirects=False,
        ) as client:
            # STREAMED, and the cap applies to the DOWNLOAD (#897 review, twice).
            #
            # `client.get()` buffers the whole response, so slicing `r.text` afterwards has
            # already paid for every byte — a configured or compromised target can force
            # unbounded memory into this process, on a timer. The first pass fixed this in the
            # forge adapter and missed the second outbound client, which is exactly why the
            # inventory counts call SITES rather than modules.
            #
            # The deadline is TOTAL, not per-read: a peer that yields one byte before each
            # timeout expires never trips a per-I/O limit and can hold the connection for ever.
            with client.stream("GET", url) as r:
                status = r.status_code
                if 300 <= status < 400:
                    # Said plainly, because "unknown" with no reason reads as an outage.
                    return Fact.unknown(
                        f"the target redirected ({status}) — point the objective at the final "
                        "address; a probe does not follow redirects"
                    )
                chunks: list[bytes] = []
                size = 0
                truncated = False
                for chunk in r.iter_bytes():
                    if time.monotonic() > deadline:
                        return Fact.unknown("the target took too long to answer")
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= REVISION_BODY_MAX:
                        # The cap holds; what changes is what a MISS then means. See below.
                        truncated = True
                        break
                body = b"".join(chunks)[:REVISION_BODY_MAX].decode("utf-8", "replace")
    except httpx.HTTPError as e:
        # Unreachable is UNKNOWN. A deploy target that is down has not told us the change is
        # absent — it has told us nothing.
        return Fact.unknown(f"unreachable ({type(e).__name__})")

    if kind == "http_status":
        want = args.get("expect_status")
        ok = (status == int(want)) if isinstance(want, int) else (200 <= status < 400)
        return Fact.seen(ok, f"answered {status}", status=status)

    # `http_revision`. **A 200 is not the answer here** — that is `service_live`'s question, and
    # conflating them is the stale-200 bug: a target that answered 200 before the deploy, and a
    # stale instance still answering 200 after it, both look identical on status alone. So this
    # looks for a MARKER, and the marker is the string the operator configured.
    #
    # THE MARKER: what the operator configured, or the mission's own merge SHA.
    #
    # The merge-SHA fallback is #891's `change_live` contract and it now HAS a producer (#897
    # re-review 5, finding 7): `forge_merged` observes the merge commit and `note_merge_sha`
    # records it on the mission, write-once. A static playbook cannot name a SHA that does not
    # exist when it is written, which is exactly why the fallback is the interesting half.
    marker = str(args.get("expect") or "").strip() or str(mission.get("merge_sha") or "").strip()
    if not marker:
        # Unknown, never green — and it SAYS WHICH is missing, because the two fixes are
        # different: type a marker into the playbook, or wait for the merge to be observed.
        # Reporting "live" from a status code is precisely the claim this probe exists to refuse.
        return Fact.unknown(
            "no revision marker: this objective has no `expect`, and nothing has observed a "
            "merge for this mission yet"
        )
    if status >= 400:
        return Fact.seen(False, f"answered {status}", status=status)
    if marker in body:
        return Fact.seen(True, "the expected revision is live", status=status)
    if truncated:
        # A MISS IN A PREFIX IS NOT A MISS (#897 re-review, finding 5). The cap stops the download
        # partway, so "the marker is not in what we read" and "the marker is not there" are
        # different statements — and reporting the first as the second says a deploy has not
        # landed because the page is large. That is the same shape as the stale 200 this probe
        # exists to refuse, pointing the other way.
        return Fact.unknown(
            f"the first {REVISION_BODY_MAX} bytes do not carry the marker, and the rest was not "
            "read — put the revision marker nearer the top of the response, or point the "
            "objective at a smaller endpoint"
        )
    return Fact.seen(
        False,
        f"answered {status}, but not the expected revision",
        status=status,
    )


@dataclass(frozen=True)
class Target:
    """Where a probe is actually pointed, **fully resolved** (#897 re-review, finding 2).

    The first version digested only what was *stored* — `probe`, `probe_args`, the forge block,
    the mission's `cwd` and `merge_sha` — and then let the probe resolve the rest for itself.
    That left the whole derived half outside the fence: `_repo_for` reads the checkout's `origin`
    remote, `_branch_for` reads its current branch, and the PR lookup reads local HEAD. Change a
    remote, switch a branch, or commit while a request is in flight and every stored field is
    identical while the destination is not — so the fence held and the answer was about somewhere
    else.

    Resolving once, here, fixes a second thing beside the digest: the probe is handed **this**
    resolution rather than repeating the reads, so what was fenced and what was asked cannot
    diverge. Two resolutions of the same target is the same bug in a smaller window.

    No credential is a field. A token is not part of WHERE a request goes, and putting one in a
    digest is a way to leak it through a comparison.
    """

    probe: str
    args_json: str
    forge_kind: str
    forge_base: str
    forge_owner: str
    forge_enabled: bool
    cwd: str
    merge_sha: str
    #: Derived — none of these appear in `probe_args`, all of them decide the destination.
    repo: str
    branch: str
    remote: str
    head: str

    @property
    def digest(self) -> str:
        """One opaque comparable string. Opaque so a new field is a change at this end only."""
        parts = [
            self.probe,
            self.args_json,
            self.forge_kind,
            self.forge_base,
            self.forge_owner,
            str(self.forge_enabled),
            self.cwd,
            self.merge_sha,
            self.repo,
            self.branch,
            self.remote,
            self.head,
        ]
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:32]


def resolve_target(mission: dict, obj: dict) -> Target:
    """Read every input a probe's destination depends on, once. Never raises."""
    cfg = {}
    with contextlib.suppress(Exception):
        cfg = prefs.get_forge()
    args = obj.get("probe_args") if isinstance(obj.get("probe_args"), dict) else {}
    cwd = str(mission.get("cwd") or "")
    branch = _branch_for(mission, args)
    remote = ""
    head = ""
    repo = ""
    # Each read suppressed on its own. An unreadable checkout is a target with fewer known parts,
    # never an exception out of a function every probe calls first.
    with contextlib.suppress(Exception):
        repo = _repo_for(mission, args)
    if cwd:
        with contextlib.suppress(Exception):
            remote = str(gitpanel.remote_url(cwd) or "")
        with contextlib.suppress(Exception):
            head = str(gitpanel.head_sha(cwd, branch) or "")
    return Target(
        probe=str(obj.get("probe") or ""),
        args_json=json.dumps(args, sort_keys=True, default=str),
        forge_kind=str(cfg.get("kind") or ""),
        forge_base=str(cfg.get("base_url") or ""),
        forge_owner=str(cfg.get("owner") or ""),
        forge_enabled=bool(cfg.get("enabled") or False),
        cwd=cwd,
        merge_sha=str(mission.get("merge_sha") or ""),
        repo=repo,
        branch=branch,
        remote=remote,
        head=head,
    )


def target_digest(mission: dict, obj: dict) -> str:
    """The resolved target as one comparable string. See :class:`Target`."""
    return resolve_target(mission, obj).digest


def _about_pr(fact: Fact, number: object, head: str) -> Fact:
    """`fact`, carrying the PR `number` and `head_sha` it was answered about (#983).

    Added beside the probe's own extras, never over them, and for an `unknown` answer too: which
    PR was asked about is true either way, and the renderer refuses a stale fact on its own.
    """
    extra = dict(fact.extra or {})
    extra["number"] = int(number)  # type: ignore[call-overload]
    if head:
        extra["head_sha"] = str(head)
    return Fact(observed=fact.observed, value=fact.value, detail=fact.detail, extra=extra)


def probe_one(mission: dict, obj: dict, *, target: Target | None = None) -> Fact:
    """Evaluate ONE objective. Never raises: an unexpected failure is `unknown`, not a false fact.

    A mission's follow-through must not stop because one probe hit something nobody anticipated,
    and a bare `except` that returned `False` would turn every such surprise into a report that the
    work had gone backwards.

    `target` is the resolution the caller fenced on. Passing it is what makes the fence mean
    anything: resolving the repo, branch and HEAD a second time here would ask a question about a
    destination nobody bound (#897 re-review, finding 2). Omitted only by callers that are not
    settling a row — tests, and the ad-hoc single probe — which resolve it themselves.
    """
    kind = str(obj.get("probe") or "")
    if kind not in MECHANICAL:
        return Fact.unknown(f"{kind} is not a probe the server can run")
    args = obj.get("probe_args") if isinstance(obj.get("probe_args"), dict) else {}
    tgt = target if target is not None else resolve_target(mission, obj)
    try:
        if kind == "git_local":
            return _probe_git_local(mission, args)
        if kind in ("http_status", "http_revision"):
            return _probe_http(kind, args, mission)

        client = _client_for(tgt)
        if client is None:
            # NOT a failure of the work. "We were not told where to look" is unknown.
            return Fact.unknown("no forge is configured")
        repo = tgt.repo
        if not repo:
            return Fact.unknown("no repository to ask about")
        branch = tgt.branch

        # `forge_run` is a BRANCH-AND-WORKFLOW fact and never needed a pull request. Forcing it
        # through the PR prerequisite made a perfectly answerable question unanswerable whenever
        # no PR existed yet (#897 review).
        if kind == "forge_run":
            return client.run(repo, str(args.get("workflow") or ""), branch)

        # `forge_pr` asks whether a PR is OPEN, so it is the one caller that does not want a
        # closed one folded in.
        if kind == "forge_pr":
            return client.pull_request(repo, branch, include_closed=False)

        # The three that need a PR first. Resolved here rather than in the adapter so the adapter
        # stays a thin read of one endpoint each, and so "there is no PR yet" is reported once, in
        # the operator's terms, rather than three times in three shapes.
        #
        # OPEN OR CLOSED: a merged PR is not open, and searching only open ones meant the
        # `merged` gate could never be observed at all — it reported "there is no open PR yet"
        # for ever, which is the state that objective exists to see (#897 review).
        # The LOCAL head, so a closed PR can be identified by commit rather than by a reusable
        # branch name (#897 review). Unknown ⇒ the adapter refuses a closed match rather than
        # guessing, which is the right direction: an objective that stays unmet is recoverable,
        # one that is permanently met by another branch's old PR is not.
        pr = client.pull_request(repo, branch, include_closed=True, head_sha=tgt.head)
        if not pr.observed:
            return pr
        num = (pr.extra or {}).get("number")
        if not num:
            return Fact.seen(False, "there is no PR yet")
        head = (pr.extra or {}).get("head_sha") or ""
        # THE PR THIS ANSWER IS ABOUT is kept beside the answer (#983). It was resolved here and
        # then dropped, so a checks objective could not say which PR its state belonged to — and
        # a direction quoting `{pr}` would have had to borrow one from a sibling objective, which
        # is exactly the join that must not exist.
        if kind == "forge_checks":
            return _about_pr(client.checks(repo, head), num, head)
        if kind == "forge_review":
            return _about_pr(client.review(repo, int(num), head), num, head)
        if kind == "forge_merged":
            return _about_pr(client.merged(repo, int(num)), num, head)
    except Exception as e:  # noqa: BLE001 — see the docstring: surprises are `unknown`
        log.debug("probe %s failed: %s", kind, type(e).__name__)
        return Fact.unknown(f"the probe could not run ({type(e).__name__})")
    return Fact.unknown(f"{kind} has no runner")


#: Probes whose answer can CHANGE after it first becomes true. Checks go red when the head
#: advances, an approval is dismissed, a PR is reopened — so a gate that stops being asked the
#: moment it is first satisfied lets a completion proposal quote evidence about a commit that is
#: no longer the head (#897 review). These are re-probed even while `met`; the settlement is not
#: moved backwards automatically (that is the operator's call), but the OBSERVATION is refreshed
#: so the staleness is visible and the proposal has something true to read.
MUTABLE: frozenset[str] = frozenset(
    {
        "forge_checks",
        "forge_review",
        "forge_pr",
        "http_status",
        # Added after #897's re-review, and each for a concrete way the fact stops being true: a
        # newer workflow run can fail; a deployment can be rolled back so the revision that was
        # live no longer is; a checkout can move to a different branch. `forge_merged` is the only
        # forge fact that does not come back — a merge does not un-merge.
        "forge_run",
        "http_revision",
        "git_local",
    }
)


@contextlib.contextmanager
def _fact_fence():
    """The byte-one fence for a probe's STORE write — never for its request (#983 review).

    A pending supervisor nudge is rendered from this objective's binding and observation, and
    delivery re-renders them inside the PTY write fence immediately before byte one. A probe
    committing between that re-read and the byte would put a head the operator was never shown
    under text they approved. Taking `session_input.fact_transaction` orders the two: the write
    either lands first (and delivery refuses the changed facts) or waits until byte one is out.

    Lock order: ``session_input._lock → authfence → missions._write_lock``, the order every fenced
    mutation takes. Raises `session_input.AuthorityFenceBusy` when the fence cannot be had.
    """
    from . import session_input

    with session_input.fact_transaction():
        yield


#: Where the per-mission sweep stopped, so the next pass starts after it.
_CURSOR_PREFIX = "probe_cursor:"


def _eligible(mission: dict) -> list[dict]:
    """Objectives this runner will ask about, in a stable order.

    `waived` is excluded permanently — the operator has said it is not required, and asking anyway
    spends a request to contradict them. `met` is excluded only for IMMUTABLE probes: see
    :data:`MUTABLE`.
    """
    out = []
    for o in mission.get("objectives") or []:
        kind = str(o.get("probe") or "")
        if kind not in MECHANICAL:
            continue
        state = str(o.get("state") or "")
        if state == "waived":
            continue
        if state == "met" and kind not in MUTABLE:
            continue
        out.append(o)
    return out


def run_for_mission(mission_id: str, *, path=None) -> dict:
    """Probe one mission's objectives and record what was seen. Synchronous; runs OFF the loop.

    **The per-pass cap is a ceiling on WORK, never a prefix of the list.** The first version took
    `[:PROBES_PER_MISSION]` of a stable order with no cursor, so a mission with more unsettled
    objectives than the cap never reached the tail — objective 13 was asked about zero times, for
    ever (#897 review). That is the third instance of the starvation family #888 found three of,
    and the fix is the same one: a **durable** cursor, so the window advances and a restart does
    not send it back to the beginning.
    """
    try:
        mission = missions.get_mission(mission_id, events_limit=1, path=path)
    except Exception:  # noqa: BLE001
        return {"probed": 0, "settled": 0, "unknown": 0}
    if mission is None:
        return {"probed": 0, "settled": 0, "unknown": 0}

    from . import session_input  # the fence's busy signal; imported here, as `_fact_fence` does

    eligible = _eligible(mission)
    if not eligible:
        return {"probed": 0, "settled": 0, "unknown": 0}

    # Resume after the key the last pass finished on. Keyed on the OBJECTIVE KEY rather than an
    # index: the list changes between passes, and an index into a list that has moved points at a
    # different objective — the "an index is not an identity" family again.
    cursor = ""
    with contextlib.suppress(Exception):
        cursor = str(missions.get_supervisor_state(_CURSOR_PREFIX + mission_id, path=path) or "")
    start = 0
    if cursor:
        for i, o in enumerate(eligible):
            if str(o.get("key")) == cursor:
                start = i + 1
                break
    order = eligible[start:] + eligible[:start]
    rows = order[:PROBES_PER_MISSION]

    probed = settled = unknown = 0
    for o in rows:
        key = str(o["key"])
        # BIND FIRST, then ask. `bind_probe_target` writes the fully resolved destination and a
        # fresh generation onto the row, in its own transaction, before a single byte goes out —
        # so what this answer will be adjudicated against is durable, visible to any other runner,
        # and survives a restart. `None` means the objective moved (or went) between selecting it
        # and issuing the request, and the right response is not to issue one.
        before = resolve_target(mission, o)
        # FENCED against byte one (#983): the binding is part of what a pending supervisor nudge's
        # facts rest on. Lock order: session_input._lock → authfence → missions._write_lock.
        # Only the store write is fenced; the forge request below runs outside it.
        try:
            with _fact_fence():
                gen = missions.bind_probe_target(
                    mission_id,
                    key,
                    target=before.digest,
                    expect_probe=str(o.get("probe") or ""),
                    expect_args=o.get("probe_args"),
                    path=path,
                )
        except session_input.AuthorityFenceBusy:
            # Could not be ordered against a delivery: issue no request, the next pass retries.
            log.debug("probe for %s/%s skipped: the authorization fence was busy", mission_id, key)
            continue
        if gen is None:
            continue
        # The SAME resolution the row is now bound to is handed to the probe. Re-resolving inside
        # would ask about a destination nobody fenced.
        fact = probe_one(mission, o, target=before)
        probed += 1
        if not fact.observed:
            unknown += 1
        # RE-RESOLVED AFTER THE ANSWER, and adjudicated inside the settling transaction rather
        # than here. The forge config and the checkout are both part of where this answer came
        # FROM, neither appears in `probe_args`, and both can be changed by the operator
        # mid-flight — but the store cannot read either, so the comparison is split: the caller
        # supplies the two digests, the transaction requires them to be the same value the row is
        # bound to, at the generation it was bound at. A pre/post pair held in this loop's locals
        # (what this replaces) could not see a second runner and left the gap between the
        # comparison and the write unguarded (#897 re-review, finding 2).
        after = resolve_target(mission, o)
        try:
            was = str(o.get("state") or "")
            # FENCED against byte one (#983), after the request has returned — never across it.
            # Lock order: session_input._lock → authfence → missions._write_lock. A busy fence
            # raises here and lands in the `except` below: the answer is discarded, not settled.
            with _fact_fence():
                row = missions.observe_objective(
                    mission_id,
                    key,
                    observed=fact.observed,
                    value=fact.value,
                    detail=fact.detail,
                    extra=fact.extra,
                    # THE IDENTITY THIS PROBE WAS ABOUT, compared inside the settling
                    # transaction. A probe is an external call that can outlive the objective it
                    # was issued for: drop and re-add the same key with a different probe while an
                    # HTTP request is in flight and the old answer would settle the new target
                    # (#897 review). The row is only written when the objective is still the one
                    # that was asked about.
                    expect_probe=str(o.get("probe") or ""),
                    expect_args=o.get("probe_args"),
                    # …and the EXTERNAL target too: the forge, the checkout, the derived repo and
                    # branch, the remote, the local HEAD. The transaction requires the row still to
                    # be bound to this destination at this generation, so an answer from an
                    # authority the operator has since changed — or one a second runner has
                    # superseded — cannot settle the row.
                    expect_target=after.digest,
                    expect_gen=gen,
                    path=path,
                )
            if row is None:
                log.debug("probe target moved under %s/%s — discarded", mission_id, key)
            elif fact.observed and fact.value:
                # THE MERGE SHA, RECORDED FROM THE ONE THING THAT KNOWS IT (#897 re-review 5,
                # finding 7). `forge_merged` carries the merge commit; `http_revision` needs a
                # revision to look for and a playbook cannot name one that does not exist yet.
                # Write-once in the store, and only from an observation that actually settled.
                sha = str((fact.extra or {}).get("sha") or "")
                if str(o.get("probe") or "") == "forge_merged" and sha:
                    # Fenced too: the merge SHA is part of the resolved target a direction's facts
                    # are checked against (#983). Same lock order as the writes above.
                    with contextlib.suppress(Exception), _fact_fence():
                        missions.note_merge_sha(mission_id, sha, path=path)
            if row is not None and was != "met" and str(row.get("state")) == "met":
                settled += 1
        except Exception:  # noqa: BLE001 — one unwritable row must not stop the rest
            log.debug("could not record probe for %s/%s", mission_id, o.get("key"))
    if rows:
        with contextlib.suppress(Exception):
            missions.set_supervisor_state(
                _CURSOR_PREFIX + mission_id, str(rows[-1].get("key") or ""), path=path
            )
    return {"probed": probed, "settled": settled, "unknown": unknown}
