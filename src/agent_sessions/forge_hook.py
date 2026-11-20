"""A Forgejo webhook delivery: its signature, and the typed event read from its body (#2202).

Read `docs/invariants/forge-review-loop.md` before changing anything here.

**The body is untrusted outside input that ends up near an agent.** This module reads a CLOSED set
of fields, each type-checked, and nothing else: the repository's full name, the item number, the
PR head sha and ref, the draft flag and state, the author and sender logins, the formal review
type, and the two closed markers (the reviewer's verdict marker, the working session's record). A
title and body are only hashed (the issue revision) or scanned for those markers; no free text
leaves this module. A field that fails its type makes the whole delivery ``Rejected``: nothing is
partially read.

**The event type comes from the signed body's SHAPE**, never from the ``X-Forgejo-Event`` header,
which the signature does not cover. The header is recorded for display only.

**The signature** is Forgejo's: ``X-Forgejo-Signature`` = hex HMAC-SHA256 of the raw body, keyed
with the webhook secret. Compared in constant time against every secret currently valid for the
automation (the current one, and a rotated one inside its overlap window).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

#: The largest body a delivery may carry (#1201 §4).
BODY_MAX = 256 * 1024
SIGNATURE_HEADER = "x-forgejo-signature"
EVENT_HEADER = "x-forgejo-event"
DELIVERY_HEADER = "x-forgejo-delivery"

_SIG_RE = re.compile(r"^[0-9a-f]{64}$")
#: Forgejo's owner/repo names (alphanumerics, ``-``, ``_``, ``.``), at most 100 characters each.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
#: A git branch name, conservatively: no spaces, no control characters, no ``..``, bounded.
_REF_RE = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9._/@+-]{1,200}(?<!/)(?<!\.lock)$")
_DISPLAY_RE = re.compile(r"[^A-Za-z0-9_.:-]")

#: The verdict marker the reviewer must emit, verbatim from its brief (#2202).
VERDICT_MARKER_RE = re.compile(
    r"<!--\s*battlelab-review:\s*record=([a-z0-9]{1,64})\s+item=(pr|issue)#([0-9]{1,9})\s+"
    r"rev=([0-9a-f]{16}|[0-9a-f]{40})\s+verdict=([a-z]{1,16})\s*-->"
)
#: The working session's record in a PR body (`docs/agent-workflow.md` §6).
SESSION_PR_MARKER_RE = re.compile(
    r"<!--\s*agent-session-pr:\s*engine=([a-z0-9_-]{1,32})\s+id=([A-Za-z0-9_.-]{1,128})\s*-->"
)
#: An issue's session records (§5 claim, §1 author), read from its comments.
SESSION_ISSUE_MARKER_RE = re.compile(
    r"<!--\s*agent-session(?:-author)?:\s*engine=([a-z0-9_-]{1,32})\s+id=([A-Za-z0-9_.-]{1,128})"
    r"\s*-->"
)

VERDICTS = {"pr": ("approve", "changes"), "issue": ("viable", "adjust", "clarify")}
#: The verdicts that end a revision's loop.
APPROVING = frozenset({"approve", "viable"})
#: Forgejo's formal review types, mapped to the marker's verdict they must agree with.
REVIEW_TYPES = {
    "pull_request_review_approved": "approve",
    "pull_request_review_rejected": "changes",
    "pull_request_review_comment": None,
}


class Rejected(ValueError):
    """A validly signed delivery whose body this module refuses to read. ``reason`` is ours."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def signature_ok(body: bytes, header: str | None, secrets: list[str]) -> bool:
    """Whether ``header`` is the hex HMAC-SHA256 of ``body`` under any of ``secrets``.

    Every secret is tried (no early exit on the first mismatch), so the time taken does not depend
    on which secret matched or how far a forged digest got."""
    if not isinstance(header, str):
        return False
    got = header.strip().lower()
    if not _SIG_RE.match(got):
        # Still spend a comparison per secret: an unsigned and a badly signed delivery take the
        # same time class.
        got = "0" * 64
        ok = False
    else:
        ok = True
    match = False
    for secret in secrets:
        want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        match = hmac.compare_digest(want, got) or match
    return ok and match


def display(value: object, limit: int = 64) -> str:
    """A header value as the run history may show it: a closed alphabet, bounded, never raw."""
    if not isinstance(value, str):
        return ""
    return _DISPLAY_RE.sub("", value)[:limit]


def issue_revision(title: str, body: str) -> str:
    """An issue's revision: ``sha256(title + "\\n" + body)[:16]`` (#2202)."""
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Event:
    """What a delivery IS, typed. ``kind`` is one of:

    * ``issue`` — an issue was opened or edited (``action``), revision = title+body digest;
    * ``pr`` — a pull request was opened, reopened, pushed (``synchronized``) or edited;
    * ``pr_review`` — a formal review was submitted (``review_type``, ``verdict``);
    * ``issue_comment`` — a comment was created on an issue (never on a PR: ``is_pull``);
    * ``other`` — any other shape (a ping, a push, a label change): never a run.
    """

    kind: str
    action: str
    repo: str
    number: int = 0
    open: bool = True
    author: str = ""
    sender: str = ""
    head_sha: str = ""
    head_ref: str = ""
    #: The PR head's repository (``owner/name``) — a fork for a contributor's PR, else the base.
    head_repo: str = ""
    draft: bool = False
    revision: str = ""
    review_type: str = ""
    #: ``(record, item_kind, number, rev, verdict)`` when exactly one well-formed marker is present.
    marker: tuple[str, str, int, str, str] | None = None
    marker_problem: str = ""
    #: ``"engine:id"`` from a PR body's session record, when exactly one is present.
    session_marker: str = ""
    comment_id: int = 0

    @property
    def item(self) -> str:
        kind = "pr" if self.kind in ("pr", "pr_review") else "issue"
        return f"{kind}#{self.number}"


def _obj(raw: object, what: str) -> dict:
    if not isinstance(raw, dict):
        raise Rejected(f"{what} is not an object")
    return raw


def _str(raw: object, what: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(raw, str) or (pattern is not None and not pattern.match(raw)):
        raise Rejected(f"{what} is not well-formed")
    return raw


def _text(raw: object, what: str) -> str:
    """A free-text field, used only to hash or scan for a closed marker. ``None`` reads as ""."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise Rejected(f"{what} is not text")
    return raw


def _int(raw: object, what: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or not 0 < raw < 10**9:
        raise Rejected(f"{what} is not a number")
    return raw


def _login(raw: object, what: str) -> str:
    return _str(_obj(raw, what).get("login"), f"{what} login", _LOGIN_RE)


def _repo(payload: dict) -> str:
    repo = _obj(payload.get("repository"), "repository")
    full = _str(repo.get("full_name"), "repository name")
    parts = full.split("/")
    if len(parts) != 2 or not all(_NAME_RE.match(p) for p in parts):
        raise Rejected("repository name is not well-formed")
    return full


def _head_repo(head: dict) -> str:
    """``head.repo.full_name`` when Forgejo sends it (well-formed), else "" — never a guess."""
    repo = head.get("repo")
    full = repo.get("full_name") if isinstance(repo, dict) else None
    if not isinstance(full, str):
        return ""
    parts = full.split("/")
    if len(parts) != 2 or not all(_NAME_RE.match(p) for p in parts):
        raise Rejected("head repository name is not well-formed")
    return full


def _state_open(raw: object, what: str) -> bool:
    state = _str(raw, f"{what} state")
    if state not in ("open", "closed"):
        raise Rejected(f"{what} state is not open or closed")
    return state == "open"


def _one(regex: re.Pattern[str], text: str) -> tuple[re.Match[str] | None, str]:
    """The ONE match of ``regex`` in ``text``, or why there is none to use."""
    found = list(regex.finditer(text))
    if not found:
        return None, "no marker"
    if len(found) > 1:
        return None, "more than one marker"
    return found[0], ""


def verdict_marker(text: str) -> tuple[tuple[str, str, int, str, str] | None, str]:
    m, why = _one(VERDICT_MARKER_RE, text)
    if m is None:
        if why == "no marker" and "battlelab-review:" in text:
            return None, "the verdict marker is not well-formed"
        return None, why
    record, kind, number, rev, verdict = m.groups()
    if verdict not in VERDICTS[kind]:
        return None, f"{verdict!r} is not a {kind} verdict"
    if (kind == "pr") != (len(rev) == 40):
        return None, "the marker's revision does not fit its item"
    return (record, kind, int(number), rev, verdict), ""


def session_marker(regex: re.Pattern[str], text: str, *, newest: bool = False) -> str:
    """``"engine:id"`` from a session record, or ``""``. ``newest`` takes the LAST of several
    (an issue's records accumulate as sessions take it over); otherwise exactly one is required."""
    found = list(regex.finditer(text))
    if not found or (len(found) > 1 and not newest):
        return ""
    engine, sid = found[-1].groups()
    return f"{engine}:{sid}"


def parse(body: bytes) -> Event:
    """The typed event in a validly signed ``body``. Raises `Rejected` for a body that is not
    well-formed where it matters; a body of another shape is ``Event(kind="other")``."""
    if len(body) > BODY_MAX:
        raise Rejected("the delivery is too large")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise Rejected("the delivery is not JSON") from None
    payload = _obj(payload, "the delivery")
    action = payload.get("action")
    if "repository" not in payload or not isinstance(action, str):
        return Event(kind="other", action="", repo="")
    repo = _repo(payload)
    action = _str(action, "action", re.compile(r"^[a-z_]{1,32}$"))
    sender = _login(payload.get("sender"), "sender")

    pr = payload.get("pull_request")
    if isinstance(pr, dict) and "comment" not in payload:
        pr = _obj(pr, "pull request")
        number = _int(pr.get("number"), "pull request number")
        head = _obj(pr.get("head"), "pull request head")
        review = payload.get("review")
        common = {
            "action": action,
            "repo": repo,
            "number": number,
            "open": _state_open(pr.get("state"), "pull request"),
            "author": _login(pr.get("user"), "pull request author"),
            "sender": sender,
            "head_sha": _str(head.get("sha"), "head sha", _SHA_RE),
            "head_ref": _str(head.get("ref"), "head ref", _REF_RE),
            "head_repo": _head_repo(head),
            "draft": bool(pr.get("draft") is True)
            or _text(pr.get("title"), "title").lstrip().upper().startswith(("WIP:", "[WIP]")),
            "session_marker": session_marker(SESSION_PR_MARKER_RE, _text(pr.get("body"), "body")),
        }
        if isinstance(review, dict):
            rtype = _str(review.get("type"), "review type")
            if rtype not in REVIEW_TYPES:
                raise Rejected("review type is not one Forgejo sends")
            marker, problem = verdict_marker(_text(review.get("content"), "review"))
            return Event(
                kind="pr_review", review_type=rtype, marker=marker, marker_problem=problem, **common
            )
        return Event(kind="pr", revision=common["head_sha"], **common)

    issue = payload.get("issue")
    if isinstance(issue, dict):
        issue = _obj(issue, "issue")
        number = _int(issue.get("number"), "issue number")
        comment = payload.get("comment")
        is_pull = payload.get("is_pull") is True or isinstance(issue.get("pull_request"), dict)
        common = {
            "action": action,
            "repo": repo,
            "number": number,
            "open": _state_open(issue.get("state"), "issue"),
            "author": _login(issue.get("user"), "issue author"),
            "sender": sender,
        }
        if isinstance(comment, dict):
            if is_pull:
                return Event(kind="other", action=action, repo=repo, number=number, sender=sender)
            comment = _obj(comment, "comment")
            marker, problem = verdict_marker(_text(comment.get("body"), "comment"))
            return Event(
                kind="issue_comment",
                marker=marker,
                marker_problem=problem,
                comment_id=_int(comment.get("id"), "comment id"),
                **common,
            )
        if is_pull:
            return Event(kind="other", action=action, repo=repo, number=number, sender=sender)
        rev = issue_revision(_text(issue.get("title"), "title"), _text(issue.get("body"), "body"))
        return Event(kind="issue", revision=rev, **common)
    return Event(kind="other", action=action, repo=repo, sender=sender)
