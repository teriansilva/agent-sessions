"""What a public link shows of an API session (#1404 P2) — the projection and the attribution.

The operator's snapshot (``structured_runtime.snapshot``) is built for the operator: it names the
working directory, the live worker, mission correlation, approval payloads and where grants are
saved. A visitor gets a projection built by ALLOWLIST — a field reaches them only if it is named
here — and every string in it goes through the same secret-value redactor ``review._post_chat``
uses. The rules are pinned in ``docs/invariants/public-shares.md``.

Attribution: a visitor's turn is recorded in the share store (``ShareStore.record_turn``) under an
operation id the server minted, so both views can say whose it was — ``{"name": …, "via":
"public_link"}`` — without touching the native journal or the workers' IPC.
"""

from __future__ import annotations

import re

from . import template_secrets

# Approval summaries are one line in the visitor view: they know something waits for the owner,
# not what exactly it would run or where a grant would be saved.
REQUEST_SUMMARY_MAX = 200


UNKNOWN_AUTHOR = {"name": None, "via": "unknown"}


def author_view(entry: dict | None) -> dict | None:
    """The attribution both views carry for a visitor's turn; None for the operator's own."""
    if not entry:
        return None
    return {"name": entry["name"], "via": "public_link"}


def overlay_authors(snapshot: dict, authors: dict[str, dict] | None) -> dict:
    """Stamp ``author`` on every turn of an OPERATOR snapshot (in place; returns it).

    The operator's turns get ``None`` and a visitor's turn names the visitor. ``authors=None``
    means attribution could not be read: every turn is then marked UNKNOWN, never ``None`` —
    labelling a visitor's instruction as the operator's would be worse than saying we can't tell
    (Hermes on #1423)."""
    for turn in snapshot.get("turns") or []:
        if turn.get("kind") == "agent":
            continue  # nobody sent it (#1438): no author, and never "sender unknown"
        if authors is None:
            turn["author"] = dict(UNKNOWN_AUTHOR)
        else:
            turn["author"] = author_view(authors.get(turn.get("operation_id")))
    if authors is None:
        snapshot["attribution_unavailable"] = True
    return snapshot


def link_id(share_id: str) -> str:
    """The OPAQUE identity of a link, as its own visitor may hold it (#1444 review): a keyed
    fingerprint of the share id, never the id itself. The viewer stamps every snapshot and event
    page it is given with it and refuses an answer for another link — the share cookie is one per
    browser, so a second tab unlocking another link changes what this tab's polls return."""
    return template_secrets.keyed_digest("public-share-link", share_id)[:24]


def image_handle(share_id: str, stored: str) -> str:
    """The OPAQUE name a visitor gets for an image: a keyed fingerprint of (link, stored name).
    The stored name carries the uploader's original filename, which can itself hold a secret, so
    it never reaches a visitor; the read-back route maps the handle back server-side
    (Hermes on #1423)."""
    return template_secrets.keyed_digest("public-share-image", f"{share_id}\0{stored}")[:32]


# Where the runtime CUT a text, a fixed region is trimmed at the cut — independent of what it
# contains (#1423, Hermes and the independent review). The runtime bounds texts before this
# projection sees them: a turn's text at its end (`structured_runtime._turn`), a reply to its last
# 200k characters (`native_runtime.project`) and then to its first 20k, a tool summary at 500. A
# `send` visitor controls both their own text and, through the shared snapshot budget, where those
# cuts fall, so ANY masking that depends on whether a fragment matches a stored secret is a
# guessing oracle ("…ghp_A" masked, "…ghp_B" echoed). The trim never looks at the secrets' content:
# it removes `trim` characters at every cut end, so a fragment of a secret split there is gone and
# a right guess and a wrong one produce the same output. `trim` is at least `CUT_TRIM_MIN` and
# grows, in `CUT_TRIM_STEP` buckets, only with the LONGEST secret's length — the only thing the
# inventory can tell a visitor, coarsely.
CUT_TRIM_MIN = 256
CUT_TRIM_STEP = 256
CUT_MARK = "…"
# C0 controls other than tab and newline, plus DEL.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
TOOL_SUMMARY_CAP = 500


class _Redactor:
    """Every string a visitor gets passes through here: whole secrets (and their JSON/URL forms)
    become ``[secret]``. With ``start=``/``end=`` — the end(s) the runtime cut — a fixed region is
    trimmed there and marked ``…``, whatever it contained."""

    def __init__(self, secrets: list[str]):
        self._secrets = secrets
        longest = max((len(v) for s in secrets for v in template_secrets._variants(s)), default=0)
        need = max(CUT_TRIM_MIN, longest)
        self.trim = -(-need // CUT_TRIM_STEP) * CUT_TRIM_STEP  # rounded up to a bucket

    def __call__(self, value, *, start: bool = False, end: bool = False, redact: bool = True):
        if isinstance(value, str) and value:
            # Terminal escapes and C0 controls first: a visitor needs neither, and escapes between
            # a secret's characters would stretch it past a fixed trim (#1423 review).
            value = _CONTROL_RE.sub("", template_secrets._ANSI_RE.sub("", value))
            if redact:
                value = template_secrets.redact_text(value, self._secrets)
            if start:
                value = CUT_MARK + value[self.trim :]
            if end:
                value = value[: max(0, len(value) - self.trim)] + CUT_MARK
        return value

    def deep(self, value):
        """The same over a nested structure (a risk mark is a small dict)."""
        if isinstance(value, dict):
            return {k: self.deep(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        return self(value)


def redactor() -> _Redactor:
    """The secret-value redactor for one request. Fails closed: an unreadable secret store raises
    ``template_secrets.RedactionUnavailable`` and the caller refuses to answer."""
    return _Redactor(template_secrets.redaction_values())


def _attachments(items, r: _Redactor, share_id: str) -> list[dict]:
    out = []
    for a in items or []:
        mime = a.get("mime") or ""
        entry = {"name": r(a.get("name") or ""), "mime": r(mime)}
        if mime.startswith("image/") and a.get("stored"):
            # readable through /api/public/uploads/{image}; never the stored name itself
            entry["image"] = image_handle(share_id, a["stored"])
        out.append(entry)
    return out


def _tools(items, r: _Redactor) -> list[dict]:
    out = []
    for t in items or []:
        row = {
            "name": r(t.get("name")),
            "outcome": r(t.get("outcome")),
            # cut at TOOL_SUMMARY_CAP by the runtime when it was longer
            "summary": r(t.get("summary"), end=len(t.get("summary") or "") >= TOOL_SUMMARY_CAP),
        }
        if t.get("risk") is not None:
            row["risk"] = r.deep(t.get("risk"))
        out.append(row)
    return out


def _agent_turn(t: dict, r: _Redactor) -> dict:
    """A turn the agent ran for itself (#1438): the operator's agent key set, redacted, minus
    `reason` (withheld here as for every turn). Nothing that belongs to a submission."""
    return {
        "kind": "agent",
        "turn_id": t.get("turn_id"),
        "state": r(t.get("state")),
        "ts": t.get("ts"),
        "reply": r(
            t.get("reply"),
            start=bool(t.get("reply_truncated")),
            end=bool(t.get("reply_truncated")),
        ),
        "reply_ts": t.get("reply_ts"),
        "reply_truncated": bool(t.get("reply_truncated")),
        "tools": _tools(t.get("tools"), r),
        "tools_truncated": bool(t.get("tools_truncated")),
    }


def _turn(t: dict, authors: dict[str, dict], r: _Redactor, share_id: str) -> dict:
    if t.get("kind") == "agent":
        return _agent_turn(t, r)
    return {
        "kind": "operator",
        "turn_id": t.get("turn_id"),  # equals the operation id; no public input accepts one
        "state": r(t.get("state")),
        "delivery": r(t.get("delivery")),
        # A turn sent through THIS link is shown back as its visitor typed it: redacting it would
        # mark which of their own guesses equals a secret — thousands of whole-value guesses per
        # message (#1423 review). Its cut is still trimmed; everything else is redacted as usual.
        "text": r(
            t.get("text"),
            end=bool(t.get("text_truncated")),
            redact=(authors.get(t.get("operation_id")) or {}).get("share_id") != share_id,
        ),
        "text_truncated": bool(t.get("text_truncated")),
        # The native runtime keeps a reply's last 200k characters, and the facade then bounds it to
        # its first 20k (setting `reply_truncated`): any start cut implies the end cut and the flag.
        "reply": r(
            t.get("reply"),
            start=bool(t.get("reply_truncated")),
            end=bool(t.get("reply_truncated")),
        ),
        "reply_ts": t.get("reply_ts"),
        "reply_truncated": bool(t.get("reply_truncated")),
        "attachments": _attachments(t.get("attachments"), r, share_id),
        "tools": _tools(t.get("tools"), r),
        "tools_truncated": bool(t.get("tools_truncated")),
        "author": author_view(authors.get(t.get("operation_id"))),
        # Sent through THIS link — what Send now is checked against (per link, never by the
        # self-chosen name). Another link's turns are just `false`: no other link is named.
        "mine": (authors.get(t.get("operation_id")) or {}).get("share_id") == share_id,
    }


def _request(q: dict, r: _Redactor) -> dict:
    # Redact the WHOLE summary first, then take the line and cut it: cutting first could split a
    # secret and leave its start outside the redactor's reach (#1423 review).
    lines = r(q.get("summary") or "").strip().splitlines()
    first = lines[0] if lines else ""
    if len(first) > REQUEST_SUMMARY_MAX:
        first = first[: REQUEST_SUMMARY_MAX - 1] + "…"
    return {
        "request_id": q.get("request_id"),
        "turn_id": q.get("turn_id"),
        "kind": r(q.get("kind")),
        "summary": first,
        "risk": r.deep(q.get("risk")),
    }


def project(snapshot: dict, authors: dict[str, dict], r: _Redactor, share_id: str) -> dict:
    """The visitor's view of an operator snapshot. Allowlist only: ``cwd``, ``native``,
    ``context``, approval payloads/choices/grants, error ``reason``, the requested model and the
    permission mode never reach it. (A turn's ``turn_id`` equals its operation id in this
    runtime; it is shown because the viewer needs it, and no public input accepts an operation
    id — the server mints them.)"""
    return {
        "revision": snapshot.get("revision"),
        "event_cursor": snapshot.get("event_cursor"),
        "state": r(snapshot.get("state")),
        "active_turn": snapshot.get("active_turn"),
        "model": r(snapshot.get("model_effective")),
        "turns": [_turn(t, authors, r, share_id) for t in snapshot.get("turns") or []],
        "omitted_turns": snapshot.get("omitted_turns") or 0,
        "pending_requests": [_request(q, r) for q in snapshot.get("pending_requests") or []],
        "read_only": bool(snapshot.get("read_only")),
        "images": bool(snapshot.get("images")),
    }


def project_events(page: dict, link: str) -> dict:
    """The visitor's view of an events page: cursors and kinds only, never ``data`` (tool output,
    worker and connection ids live there). The viewer uses it to know WHEN to re-read the
    projected snapshot, exactly as the operator's pane does."""
    return {
        "link": link,
        "revision": page.get("revision"),
        "next_cursor": page.get("next_cursor"),
        "events": [
            {"cursor": e.get("cursor"), "kind": e.get("kind")} for e in page.get("events") or []
        ],
    }


def image_handles(snapshot: dict, share_id: str) -> dict[str, str]:
    """``{handle: stored}`` for the image uploads this session's (shown) turns reference — the
    only ones a visitor may read back, by handle."""
    out = {}
    for t in snapshot.get("turns") or []:
        for a in t.get("attachments") or []:
            if (a.get("mime") or "").startswith("image/") and a.get("stored"):
                out[image_handle(share_id, a["stored"])] = a["stored"]
    return out
