"""Installed signature authority, monotonic accepted index and retained signed evidence.

A cut longer than `contract.MAX_AGE` is usable only with a current freshness statement that names
it (#1730, `plugins/freshness.py`). The statement and its durable floor are stored beside the
index, under the index lock; `checked(record)` (not historical) applies the one usability rule.
"""

from __future__ import annotations

import contextlib
import re
import tempfile
from pathlib import Path

from ..plugins import feed, freshness, runner
from . import contract, storage

#: Statement records are `freshness-{content digest}.json`; the floor is `freshness-floor.json`.
STATEMENT = "freshness"


def _evidence(directory: Path):
    from ..plugins import freshness_store

    return freshness_store.Evidence(
        directory,
        prefix=STATEMENT,
        feed_name="gallery",
        read=storage.read,
        write=storage.write,
        error=contract.GalleryError,
    )


def verify(data: bytes, signature: bytes) -> None:
    if not data or len(data) > contract.MAX_BYTES or not 0 < len(signature) <= 16384:
        raise contract.GalleryError("Invalid gallery signature or size.")
    with tempfile.TemporaryDirectory(prefix="battlelab-gallery-") as temporary:
        path = Path(temporary) / "index.sig"
        path.write_bytes(signature)
        result = runner.run(
            [
                "/usr/bin/ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(feed.SIGNERS),
                "-I",
                feed.PRINCIPAL,
                "-n",
                contract.NAMESPACE,
                "-s",
                str(path),
            ],
            data=data,
            timeout=10,
            max_output=16384,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    if result.code:
        raise contract.GalleryError("Gallery signature is not from an installed trusted signer.")


def checked(record: dict, *, historical=False) -> contract.Gallery:
    try:
        if set(record) != {"data", "signature", "sequence", "digest"}:
            raise ValueError()
        data, signature = record["data"].encode("ascii"), record["signature"].encode("ascii")
        verify(data, signature)
        parsed = contract.parse(data, _historical=historical)
        if record["sequence"] != parsed.sequence or record["digest"] != parsed.digest:
            raise ValueError()
        if not historical:
            _require_fresh(parsed)
        return parsed
    except (KeyError, TypeError, AttributeError, UnicodeError):
        raise contract.GalleryError(
            "Gallery evidence is damaged; restore the retained record."
        ) from None


def _require_fresh(parsed: contract.Gallery, *, now: int | None = None) -> None:
    """A long cut is usable only while the stored, verified statement names exactly it, is
    unexpired, and sits at the durable statement floor (#1730)."""
    if not freshness.is_long(parsed.issued_at, parsed.expires_at):
        return
    if not _evidence(storage.root()).fresh(
        content_sequence=parsed.sequence,
        content_digest=parsed.digest,
        content_issued_at=parsed.issued_at,
        content_expires_at=parsed.expires_at,
        now=now,
    ):
        raise contract.GalleryError("The gallery has no current freshness statement.")


def _current(path: Path):
    record = storage.read(path)
    floor = storage.read(path.with_name("floor.json"))
    if record is None:
        if floor is not None:
            raise contract.GalleryError("Established gallery evidence is missing.")
        return None
    parsed = checked(record, historical=True)
    if floor != {"sequence": parsed.sequence, "digest": parsed.digest}:
        raise contract.GalleryError("Gallery evidence and its durable floor disagree.")
    return record


def current() -> dict | None:
    with storage.locked("index") as path:
        return _current(path)


def accept(
    data: bytes,
    signature: bytes,
    selected: dict,
    statement: tuple[bytes, bytes, dict] | None = None,
) -> dict:
    """Accept a release-signed cut, and with it (#1730) the freshness statement that keeps a long
    cut usable. A long cut is refused without a statement naming exactly it; a statement naming
    another cut is ignored for a short one. Nothing accepted earlier is replaced on a refusal."""
    verify(data, signature)
    parsed = contract.parse(data)
    cut = {"sequence": parsed.sequence, "digest": parsed.digest}
    if cut != selected:
        raise contract.GalleryError("The signed index differs from the selected publication.")
    long = freshness.is_long(parsed.issued_at, parsed.expires_at)
    vouched = None
    if statement is not None:
        stmt_data, stmt_sig, pointer = statement
        try:
            candidate = freshness.checked(
                stmt_data, stmt_sig, feed_name="gallery", selected=pointer
            )
            freshness.covers(
                candidate,
                content_sequence=parsed.sequence,
                content_digest=parsed.digest,
                content_issued_at=parsed.issued_at,
                content_expires_at=parsed.expires_at,
            )
            vouched = (candidate, stmt_data, stmt_sig)
        except freshness.FreshnessError as exc:
            if long:
                raise contract.GalleryError(
                    f"The long-lived gallery is not current: {exc}"
                ) from None
    if vouched is None and long:
        raise contract.GalleryError("The long-lived gallery has no current freshness statement.")
    record = {**cut, "data": data.decode("ascii"), "signature": signature.decode("ascii")}
    with storage.locked("index") as path:
        previous = storage.read(path)
        floor = storage.read(path.with_name("floor.json"))
        if previous is None and floor is not None:
            raise contract.GalleryError(
                "Established gallery evidence is missing; restore it first."
            )
        if previous is not None:
            old = checked(previous, historical=True)
            if (
                not isinstance(floor, dict)
                or set(floor) != {"sequence", "digest"}
                or type(floor["sequence"]) is not int
                or not isinstance(floor["digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", floor["digest"]) is None
            ):
                raise contract.GalleryError("Gallery floor is damaged.")
            if floor["sequence"] < old.sequence or (
                floor["sequence"] == old.sequence and floor["digest"] != old.digest
            ):
                raise contract.GalleryError("Gallery floor disagrees with accepted evidence.")
            for sequence, digest in (
                (old.sequence, old.digest),
                (floor["sequence"], floor["digest"]),
            ):
                if parsed.sequence < sequence or (
                    parsed.sequence == sequence and parsed.digest != digest
                ):
                    raise contract.GalleryError(
                        "Gallery rollback or changed bytes at an accepted sequence."
                    )
            accepted = old
            if floor["sequence"] > old.sequence:
                retained = storage.read(path.with_name("cut-" + floor["digest"] + ".json"))
                if retained is None:
                    raise contract.GalleryError(
                        "Retained gallery floor evidence is missing; restore it first."
                    )
                accepted = checked(retained, historical=True)
                if {"sequence": accepted.sequence, "digest": accepted.digest} != floor:
                    raise contract.GalleryError(
                        "Retained gallery evidence disagrees with its floor."
                    )
                contract.successor(old, accepted)
            # A floor-first write may have committed a newer cut than index.json. Its
            # authenticated history is already accepted and cannot be lost on recovery.
            contract.successor(accepted, parsed)
        # Keep immutable exact index/signature evidence for imported origins and crash repair.
        history = path.with_name("cut-" + parsed.digest + ".json")
        existing = storage.read(history)
        if existing is not None and existing != record:
            raise contract.GalleryError("Retained gallery evidence disagrees with its digest.")
        evidence = _evidence(path.parent)
        if vouched is not None:
            # Rollback, equivocation and damaged records refuse the whole admission of a long cut
            # before anything is written; a short cut needs no statement and ignores one it can't
            # use. Then the statement is stored FIRST, under the cut it names, so a later failed
            # write can't leave the previously usable cut without its own.
            try:
                evidence.admit(vouched[0])
            except (freshness.FreshnessError, contract.GalleryError):
                if long:
                    raise
                vouched = None
        if vouched is not None:
            evidence.stage(*vouched)
        if existing is None:
            storage.write(history, record)
        storage.write(path.with_name("floor.json"), cut)
        storage.write(path, record)
        if vouched is not None:
            # The statement floor is written last: until then the staged record sits above it.
            evidence.commit(vouched[0], keep=parsed.digest)
    return record


def selected(digest: str, *, fresh=True):
    record = current()
    if record is None:
        raise contract.GalleryError("Refresh the public gallery before importing content.")
    parsed = checked(record, historical=not fresh)
    entry = next((e for e in parsed.entries if e["digest"] == digest), None)
    if entry is None or (fresh and not entry["available"]):
        raise contract.GalleryError("This content is absent or withdrawn from the current gallery.")
    return record, entry


@contextlib.contextmanager
def admitted(digest):
    """Keep refresh/withdrawal fenced until a reviewed local publication has completed."""
    with storage.locked("index") as path:
        record = _current(path)
        if record is None:
            raise contract.GalleryError("Refresh the gallery before importing content.")
        parsed = checked(record)
        entry = next((e for e in parsed.entries if e["digest"] == digest and e["available"]), None)
        if entry is None:
            raise contract.GalleryError("Content is absent or withdrawn from the current gallery.")
        yield record, entry
