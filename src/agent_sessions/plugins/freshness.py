"""Freshness statements for the two signed public feeds (#1730).

A feed's CONTENT is approved by the release key (`feed.verify`, `gallery.trust.verify`), as
before. A content cut may now be long-lived (up to `MAX_CONTENT_AGE`). A long cut is usable only
with a short-lived FRESHNESS STATEMENT, signed daily by a separate low-authority key that the
publisher host holds (battlelab-cloud#84). The statement says "the cut with this digest is still
current". It can never add, change or withdraw content: it names a digest the release key
already approved, and it can't outlive that cut.

A cut whose own window is at most `feed.MAX_AGE` (7 days) needs no statement. That is the legacy
path, unchanged.

**The statement** is canonical JSON (the feed's own canonical form) with exactly these fields:
`{format: 1, feed, statement_sequence, content_sequence, content_digest, issued_at,
expires_at}`. It is signed with `ssh-keygen -Y sign -n battlelab-freshness`, principal
`PRINCIPAL`, and verified against `freshness-signers`, which is pinned in this package and
never supplied by the network.

**The pointer** `current.json` is `{sequence, digest}`: the statement's own sequence and the
sha256 of its exact bytes. The signed `statement_sequence` must equal the pointer's sequence, so
a generation number is authenticated, not just transported.

**The floor** (per feed, durable, stored by the feed's own module under its own lock) is the last
accepted `{sequence, digest}`:
* a greater sequence advances it;
* the same sequence with the identical digest is an idempotent reuse (a retry, a restart, a
  second admission check);
* a lower sequence, or the same sequence with a different digest, is refused (rollback,
  equivocation).
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import feed, runner

FORMAT = 1
NAMESPACE = "battlelab-freshness"
PRINCIPAL = "freshness@battlelab"
SIGNERS = Path(__file__).with_name("freshness-signers")
#: A content cut may be approved for at most this long; the statements keep it current.
MAX_CONTENT_AGE = 90 * 86400
#: A statement is valid for at most this long; the publisher re-signs daily.
MAX_STATEMENT_AGE = 7 * 86400
MAX_STATEMENT_BYTES = 1024
FEEDS = ("agents", "gallery")
_SHA = re.compile(r"[0-9a-f]{64}")
_MAX_INT = 9999999999999999
_FIELDS = {
    "format",
    "feed",
    "statement_sequence",
    "content_sequence",
    "content_digest",
    "issued_at",
    "expires_at",
}


class FreshnessError(ValueError):
    pass


@dataclass(frozen=True)
class Statement:
    feed: str
    sequence: int
    content_sequence: int
    content_digest: str
    issued_at: int
    expires_at: int
    digest: str  # sha256 of the exact statement bytes


def is_long(issued_at: int, expires_at: int) -> bool:
    """A cut whose own window exceeds the legacy 7 days needs a statement to be used."""
    return expires_at - issued_at > feed.MAX_AGE


def needs_statement(decoded: object) -> bool:
    """Whether a fetched cut (its decoded document) is long, so its statement is worth fetching.
    Anything malformed answers False: acceptance refuses it on its own terms."""
    if not isinstance(decoded, dict):
        return False
    issued, expires = decoded.get("issued_at"), decoded.get("expires_at")
    if type(issued) is not int or type(expires) is not int:
        return False
    return is_long(issued, expires)


def content_window_ok(issued_at: int, expires_at: int) -> bool:
    return issued_at < expires_at <= issued_at + MAX_CONTENT_AGE


def parse(data: bytes, *, now: int | None = None, historical: bool = False) -> Statement:
    if not data or len(data) > MAX_STATEMENT_BYTES:
        raise FreshnessError("invalid freshness statement size")
    try:
        doc = feed.decode(data)
    except feed.FeedError:
        raise FreshnessError("the freshness statement is not canonical JSON") from None
    if set(doc) != _FIELDS:
        raise FreshnessError("unexpected or missing freshness statement fields")
    if type(doc["format"]) is not int or doc["format"] != FORMAT:
        raise FreshnessError("unsupported freshness statement format")
    if doc["feed"] not in FEEDS:
        raise FreshnessError("unknown feed in freshness statement")
    for key in ("statement_sequence", "content_sequence", "issued_at", "expires_at"):
        if type(doc[key]) is not int or not 0 < doc[key] <= _MAX_INT:
            raise FreshnessError("invalid freshness statement number")
    if not isinstance(doc["content_digest"], str) or not _SHA.fullmatch(doc["content_digest"]):
        raise FreshnessError("invalid content digest in freshness statement")
    issued, expires = doc["issued_at"], doc["expires_at"]
    now = int(time.time()) if now is None else now
    if not issued < expires <= issued + MAX_STATEMENT_AGE or (
        not historical and (issued > now + feed.CLOCK_SKEW or expires <= now)
    ):
        raise FreshnessError("the freshness statement is expired or its window is invalid")
    return Statement(
        doc["feed"],
        doc["statement_sequence"],
        doc["content_sequence"],
        doc["content_digest"],
        issued,
        expires,
        hashlib.sha256(data).hexdigest(),
    )


def verify(data: bytes, signature: bytes) -> None:
    if not data or len(data) > MAX_STATEMENT_BYTES or not 0 < len(signature) <= 16384:
        raise FreshnessError("invalid freshness signature or statement size")
    with tempfile.TemporaryDirectory(prefix="battlelab-freshness-") as temporary:
        sig = Path(temporary) / "statement.sig"
        sig.write_bytes(signature)
        result = runner.run(
            [
                "/usr/bin/ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(SIGNERS),
                "-I",
                PRINCIPAL,
                "-n",
                NAMESPACE,
                "-s",
                str(sig),
            ],
            data=data,
            timeout=10,
            max_output=16384,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    if result.code != 0:
        raise FreshnessError("the freshness statement is not from a trusted freshness signer")


def pointer(value: object) -> dict:
    """A `current.json` pointer, checked: `{sequence, digest}` and nothing else."""
    if (
        not isinstance(value, dict)
        or set(value) != {"sequence", "digest"}
        or type(value["sequence"]) is not int
        or not 0 < value["sequence"] <= _MAX_INT
        or not isinstance(value["digest"], str)
        or not _SHA.fullmatch(value["digest"])
    ):
        raise FreshnessError("invalid freshness pointer")
    return value


def checked(
    data: bytes,
    signature: bytes,
    *,
    feed_name: str,
    selected: dict | None = None,
    now: int | None = None,
    historical: bool = False,
) -> Statement:
    """Verify, parse and bind a statement: right feed, and (when given) exactly the pointer that
    selected it — the signed sequence and the bytes' digest."""
    verify(data, signature)
    statement = parse(data, now=now, historical=historical)
    if statement.feed != feed_name:
        raise FreshnessError("the freshness statement is for another feed")
    if selected is not None:
        selected = pointer(selected)
        if selected["sequence"] != statement.sequence or selected["digest"] != statement.digest:
            raise FreshnessError("the freshness statement differs from the selected pointer")
    return statement


def above(statement: Statement, floor: dict | None) -> bool:
    """Floor rule. True when the statement ADVANCES the floor, False for an idempotent reuse of
    the identical statement; raises on rollback or equivocation."""
    if floor is None:
        return True
    floor = pointer(floor)
    if statement.sequence > floor["sequence"]:
        return True
    if statement.sequence == floor["sequence"] and statement.digest == floor["digest"]:
        return False
    if statement.sequence == floor["sequence"]:
        raise FreshnessError("this freshness sequence already identifies a different statement")
    raise FreshnessError("the freshness statement sequence would go backwards")


def covers(
    statement: Statement,
    *,
    content_sequence: int,
    content_digest: str,
    content_issued_at: int,
    content_expires_at: int,
) -> None:
    """The statement vouches for exactly this content cut, and never beyond its own window."""
    if statement.content_sequence != content_sequence or statement.content_digest != content_digest:
        raise FreshnessError("the freshness statement names another content cut")
    if statement.expires_at > content_expires_at:
        raise FreshnessError("the freshness statement outlives the content it vouches for")
    if statement.issued_at < content_issued_at:
        raise FreshnessError("the freshness statement predates the content it vouches for")


def mark(statement: Statement) -> dict:
    return {"sequence": statement.sequence, "digest": statement.digest}
