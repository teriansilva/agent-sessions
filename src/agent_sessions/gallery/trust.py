"""Installed signature authority, monotonic accepted index and retained signed evidence."""

from __future__ import annotations

import contextlib
import re
import tempfile
from pathlib import Path

from ..plugins import feed, runner
from . import contract, storage


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
        return parsed
    except (KeyError, TypeError, AttributeError, UnicodeError):
        raise contract.GalleryError(
            "Gallery evidence is damaged; restore the retained record."
        ) from None


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


def accept(data: bytes, signature: bytes, selected: dict) -> dict:
    verify(data, signature)
    parsed = contract.parse(data)
    cut = {"sequence": parsed.sequence, "digest": parsed.digest}
    if cut != selected:
        raise contract.GalleryError("The signed index differs from the selected publication.")
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
        if existing is None:
            storage.write(history, record)
        storage.write(path.with_name("floor.json"), cut)
        storage.write(path, record)
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
