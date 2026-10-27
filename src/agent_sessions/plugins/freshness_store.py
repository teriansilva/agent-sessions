"""Where a feed keeps its freshness evidence, and in what order it is written (#1730).

Shared by both admission paths (`plugins/feed.py`, `gallery/trust.py`); the caller holds its own
store lock around every call. The statement format itself is `freshness.py`'s shared contract.

**One record per content cut.** A statement record is stored under the digest of the cut it
names (`{prefix}-{digest}.json`), so writing the next cut's statement can never disturb the cut
in use now. A refresh therefore writes the new statement FIRST, before any content evidence:
whatever write fails afterwards, the cut that was usable stays usable or (where the content
protocol has already moved on) the new cut finds its statement waiting.

**The floor ranks by content first.** `{prefix}-floor.json` is the newest statement this store
committed, ranked by `(content_sequence, statement_sequence)`. A new release-approved cut (which
only the release key can make) therefore always outranks every statement about an older cut.
So a stolen freshness key, or a publisher whose statement counter restarted, can at most stall
statements for the cut in use: the next release cut recovers (independent review of 473a7880).
Within one cut, a higher statement sequence advances; the same rank with the same bytes is an
idempotent reuse; the same rank with other bytes, or a lower rank, is refused.

**Write order.** The FIRST admission ever writes the floor before its record (no long cut is in
use yet that it could cost); every later one writes its record first and the floor last, so a
record above the floor is an interrupted write. Our own writes therefore never leave a record
without a floor: a missing floor beside any retained record is damage (an incomplete restore).
It is never fresh, and every admission is refused until the trust records are restored, or a lost
floor plus an older restored record would roll the high-water mark back (Hermes 7180/7185/7202).

**At admission** the effective floor is the highest of the floor and every retained record that
verifies. A record that is malformed, names another feed, or is filed under another cut's digest
is damage and refuses. A well-formed record whose signer is no longer pinned (the freshness key
was rotated) is not damage and not authority: it is skipped, and the floor bounds what follows.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from pathlib import Path

from . import freshness
from .provenance import ProvenanceError

_DIGEST = re.compile(r"[0-9a-f]{64}")
_FLOOR_FIELDS = {"content_sequence", "sequence", "digest"}


def _rank(mark: dict) -> tuple[int, int]:
    return (mark["content_sequence"], mark["sequence"])


def floor_mark(statement: freshness.Statement) -> dict:
    return {
        "content_sequence": statement.content_sequence,
        "sequence": statement.sequence,
        "digest": statement.digest,
    }


class Evidence:
    def __init__(
        self,
        directory: Path,
        *,
        prefix: str,
        feed_name: str,
        read: Callable[[Path], dict | None],
        write: Callable[[Path, dict], None],
        error: type[Exception],
    ):
        self.directory = directory
        self.prefix = prefix
        self.feed_name = feed_name
        self._raw_read = read
        self._write = write
        self._error = error

    def _read(self, path: Path) -> dict | None:
        """Storage refusals that aren't ValueError/OSError (an unsafe file: wrong mode, a link, a
        FIFO) become this store's own error, so callers degrade instead of answering 500."""
        try:
            return self._raw_read(path)
        except ProvenanceError:
            raise self._error(
                f"a {self.feed_name} freshness file is unsafe; restore its trust records"
            ) from None

    @property
    def floor_path(self) -> Path:
        return self.directory / f"{self.prefix}-floor.json"

    def record_path(self, content_digest: str) -> Path:
        if not _DIGEST.fullmatch(content_digest):
            raise self._error("invalid content digest")
        return self.directory / f"{self.prefix}-{content_digest}.json"

    def _records(self) -> list[Path]:
        pattern = re.compile(re.escape(self.prefix) + r"-[0-9a-f]{64}\.json")
        return sorted(p for p in self.directory.iterdir() if pattern.fullmatch(p.name))

    def floor(self) -> dict | None:
        value = self._read(self.floor_path)
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or set(value) != _FLOOR_FIELDS
            or any(
                type(value[k]) is not int or value[k] <= 0 for k in ("content_sequence", "sequence")
            )
            or not isinstance(value["digest"], str)
            or not _DIGEST.fullmatch(value["digest"])
        ):
            raise self._error(
                f"the {self.feed_name} freshness floor is damaged; restore its trust records"
            )
        return value

    def _parsed(self, value: object) -> tuple[freshness.Statement, bytes, bytes]:
        """A record's shape, format and pointer binding, without the signature. Raises
        FreshnessError on any of them."""
        if not isinstance(value, dict) or set(value) != {"sequence", "digest", "data", "signature"}:
            raise freshness.FreshnessError("malformed record")
        try:
            data = value["data"].encode("ascii")
            signature = value["signature"].encode("ascii")
        except (AttributeError, UnicodeError):
            raise freshness.FreshnessError("malformed record") from None
        statement = freshness.parse(data, historical=True)
        if statement.feed != self.feed_name:
            raise freshness.FreshnessError("record for another feed")
        if (statement.sequence, statement.digest) != (value["sequence"], value["digest"]):
            raise freshness.FreshnessError("record disagrees with its statement")
        return statement, data, signature

    def admission_floor(self) -> dict | None:
        """The highest committed or retained statement mark. Refuses on any damaged record, and
        on a missing floor beside any retained record; skips one whose signer was rotated out."""
        marks = []
        floor = self.floor()
        records = self._records()
        if floor is None and records:
            raise self._error(
                f"the {self.feed_name} freshness floor is missing; restore its trust records"
            )
        if floor is not None:
            marks.append(floor)
        damaged = self._error(
            f"a retained {self.feed_name} freshness record is damaged; restore its trust records"
        )
        for path in records:
            try:
                statement, data, signature = self._parsed(self._read(path))
            except (freshness.FreshnessError, ValueError, OSError):
                raise damaged from None
            if path != self.record_path(statement.content_digest):
                raise damaged
            try:
                freshness.verify(data, signature)
            except freshness.FreshnessError:
                continue  # signed by a key that is no longer pinned: not authority, not damage
            marks.append(floor_mark(statement))
        if not marks:
            return None
        top = max(marks, key=_rank)
        if any(_rank(m) == _rank(top) and m["digest"] != top["digest"] for m in marks):
            raise self._error(f"the {self.feed_name} freshness records disagree at one rank")
        return top

    def admit(self, statement: freshness.Statement) -> None:
        """Refuse rollback or equivocation against the effective floor. Raises before any write."""
        floor = self.admission_floor()
        if floor is None or _rank(floor_mark(statement)) > _rank(floor):
            return
        if _rank(floor_mark(statement)) == _rank(floor):
            if statement.digest == floor["digest"]:
                return  # the identical statement again: an idempotent reuse
            raise freshness.FreshnessError(
                "this freshness sequence already identifies a different statement"
            )
        raise freshness.FreshnessError("the freshness statement sequence would go backwards")

    def stage(self, statement: freshness.Statement, data: bytes, signature: bytes) -> None:
        """Write the statement's record under the cut it names. Call before any content write.
        The first admission ever writes the floor first, so no record ever lacks a floor."""
        if self.floor() is None:
            self._write(self.floor_path, floor_mark(statement))
        self._write(
            self.record_path(statement.content_digest),
            {
                **freshness.mark(statement),
                "data": data.decode("ascii"),
                "signature": signature.decode("ascii"),
            },
        )

    def commit(self, statement: freshness.Statement, *, keep: str) -> None:
        """Advance the floor (last), then drop records for other cuts; pruning is best effort."""
        floor = self.floor()
        if floor is None or _rank(floor_mark(statement)) > _rank(floor):
            self._write(self.floor_path, floor_mark(statement))
        for path in self._records():
            if path != self.record_path(keep):
                with contextlib.suppress(OSError):
                    path.unlink()

    def fresh(
        self,
        *,
        content_sequence: int,
        content_digest: str,
        content_issued_at: int,
        content_expires_at: int,
        now: int | None = None,
    ) -> bool:
        """The record for exactly this cut verifies now (against the pinned key), ranks at or
        above the floor, and covers the cut. A missing floor is damage here (our writes never
        leave a record without one): not fresh. A malformed or unsafe floor raises: the caller
        reports unavailable trust evidence."""
        floor = self.floor()
        if floor is None:
            return False
        try:
            value = self._read(self.record_path(content_digest))
            if value is None:
                return False
            statement, data, signature = self._parsed(value)
            freshness.checked(
                data,
                signature,
                feed_name=self.feed_name,
                selected=freshness.mark(statement),
                now=now,
            )
            rank, top = _rank(floor_mark(statement)), _rank(floor)
            if rank < top or (rank == top and statement.digest != floor["digest"]):
                return False
            freshness.covers(
                statement,
                content_sequence=content_sequence,
                content_digest=content_digest,
                content_issued_at=content_issued_at,
                content_expires_at=content_expires_at,
            )
        except (freshness.FreshnessError, ValueError, OSError):
            return False
        return True
