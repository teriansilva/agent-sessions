"""The roster as it was at the last reload — what lets a REMOVED engine retire gracefully (#853 P3).

When an engine's manifest disappears (a release deleted it, or — later — the operator removed a
plugin), its live `dtach` masters must stay attachable until they exit, and new work aimed at it
must be refused *with a reason* rather than as an unknown engine. Neither is possible without
remembering what the engine WAS, so every reload records:

* ``manifests`` — the bytes of every manifest that loaded, keyed by engine id. A removed engine's
  copy is what `parse_key` validates its session ids against while it retires.
* ``removed`` — engines whose copy has been dropped (no master left). An id stays here until a
  manifest with that id loads again, so "agent removed" survives the cleanup that ends retirement.

**This file is input, and it never grants an exec.** It lives in BattleLab's own state directory
(`plugin_state_home`, never beside a plugin), is written 0600 by this process only, and is read
through `provenance.open_verified` — the P1 ownership rule — so a copy another account could have
written is refused and reported, not trusted. A retiring provider built from it has no entrypoint
at all (attach runs `dtach -a`, never the agent), so the worst a forged copy could do is keep a
session id *shape* attachable — which the ATTACH scope check still gates.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..atomicjson import atomic_write_json
from . import plugin_state_home, provenance
from .manifest import MANIFEST_NAMES, Manifest, ManifestError, load_bytes

_MAX_DOC = 2 * 1024 * 1024
_ID_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


def state_path(env: Mapping[str, str] | None = None) -> Path:
    return plugin_state_home(env) / "roster.json"


@dataclass
class RosterState:
    #: engine id → (manifest file name, manifest bytes)
    manifests: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    #: why something in the file was refused — surfaced, never acted on
    problems: list[str] = field(default_factory=list)
    #: False when a roster file EXISTS but could not be read or trusted as a whole (unreadable,
    #: foreign-writable, malformed). Such a state must NEVER be saved over the file (Hermes on
    #: PR #1132): it would replace the only recovery copy with "nothing recorded". The file is left
    #: as it is, nothing in it is trusted, and the next reload tries again.
    intact: bool = True
    #: Records that were read but refused one by one (malformed, digest mismatch). Not trusted —
    #: but carried through a save VERBATIM, so one bad record is never erased by being skipped,
    #: and never freezes every later roster update either.
    carried: dict[str, dict] = field(default_factory=dict)


def _valid_id(eid: object) -> bool:
    return isinstance(eid, str) and 0 < len(eid) <= 24 and set(eid) <= _ID_OK


def load(env: Mapping[str, str] | None = None) -> RosterState:
    """The recorded roster, or an empty one. Never raises: an unreadable, foreign-owned or
    malformed file is a reported problem and reads as "nothing recorded"."""
    p = state_path(env)
    out = RosterState()
    # NOT `os.path.lexists`: it answers False for ANY lstat error, EIO included, and "absent"
    # would then be saved over a record that exists (Hermes on PR #1132). Only a genuine
    # ENOENT/ENOTDIR is absence; every other failure leaves the file alone.
    try:
        os.lstat(p)
    except (FileNotFoundError, NotADirectoryError):
        return out
    except OSError as e:
        out.problems.append(f"{p} could not be examined ({e.strerror}); left as it is")
        out.intact = False
        return out
    try:
        fd, _ = provenance.open_verified(str(p))
    except FileNotFoundError:
        return out
    except provenance.ProvenanceError as e:
        out.problems.append(f"{p} is not a file only the operator can write ({e}); ignored")
        out.intact = False
        return out
    except OSError as e:
        out.problems.append(f"{p} could not be opened ({e.strerror}); left as it is")
        out.intact = False
        return out
    try:
        data = os.read(fd, _MAX_DOC + 1)
    except OSError as e:
        out.problems.append(f"{p} could not be read ({e.strerror}); left as it is")
        out.intact = False
        return out
    finally:
        os.close(fd)
    try:
        doc = json.loads(data.decode("utf-8")) if len(data) <= _MAX_DOC else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        doc = None
    if not isinstance(doc, dict) or set(doc) - {"manifests", "removed"}:
        out.problems.append(f"{p} is malformed; ignored and left as it is")
        out.intact = False
        return out
    mans = doc.get("manifests") or {}
    removed = doc.get("removed") or []
    if not isinstance(mans, dict) or not isinstance(removed, list):
        out.problems.append(f"{p} is malformed; ignored and left as it is")
        out.intact = False
        return out
    for eid, rec in mans.items():
        if (
            not _valid_id(eid)
            or not isinstance(rec, dict)
            or rec.get("name") not in MANIFEST_NAMES
            or not isinstance(rec.get("text"), str)
            or not isinstance(rec.get("sha256"), str)
        ):
            out.problems.append(f"{p}: the recorded manifest for {eid!r} is malformed; ignored")
            if _valid_id(eid) and isinstance(rec, dict):
                out.carried[eid] = rec
            continue
        raw = rec["text"].encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != rec["sha256"]:
            out.problems.append(f"{p}: the recorded manifest for {eid!r} fails its digest; ignored")
            out.carried[eid] = rec
            continue
        out.manifests[eid] = (rec["name"], raw)
    out.removed = {e for e in removed if _valid_id(e)}
    return out


def parse_copy(eid: str, name: str, raw: bytes) -> Manifest:
    """A recorded manifest, parsed exactly as a live one is. Raises `ManifestError`."""
    m = load_bytes(raw, name=name, source=f"retired:{eid}")
    if m.id != eid:
        raise ManifestError("identity.id", f"recorded under {eid!r} but declares {m.id!r}")
    return m


def save(state: RosterState, env: Mapping[str, str] | None = None) -> None:
    """Persist ``state`` atomically, 0600, in app state. Refuses a state that was not read intact —
    that would overwrite the recovery copy with less than it holds."""
    if not state.intact:
        raise ValueError("refusing to save over a roster file that could not be read intact")
    p = state_path(env)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    records: dict[str, dict] = {
        eid: rec for eid, rec in state.carried.items() if eid not in state.manifests
    }
    for eid, (name, raw) in state.manifests.items():
        records[eid] = {
            "name": name,
            "text": raw.decode("utf-8"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    doc = {
        "manifests": dict(sorted(records.items())),
        "removed": sorted(state.removed),
    }
    atomic_write_json(p, doc, mode=0o600)


def manifest_bytes(m: Manifest) -> tuple[str, bytes] | None:
    """``(file name, bytes)`` of a LOADED manifest, re-read from where it was loaded, or None."""
    _trust, _, path = m.source.partition(":")
    if not path:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    return Path(path).name, raw
