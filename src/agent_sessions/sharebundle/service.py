"""Export, preview and confirm share bundles (#1714 P1). See `docs/invariants/share-bundles.md`.

* **Export** reads each chosen item and its dependency closure (a playbook's role skills that the
  skills library holds) and writes one format-1 bundle. Nothing secret is read.
* **Preview** verifies the whole file (`format.read`), validates every item as its kind,
  plans a destination id per item (the same id when free, else `<id>-2`, `-3`, …) and the
  rewritten content each will hold, and records that plan under a random token with a short
  TTL, bound to the bundle digest and to every id + revision the stores held.
* **Confirm** applies the selected items in dependency order, each as one NOREPLACE new copy.
  A durable journal keyed by the token records each item's intent (destination + content digest)
  before publication and its outcome after. A retry with the same token returns the journaled
  outcomes and never creates another copy; freshness and expiry gate only items not yet applied,
  against the bound revisions minus this journal's own writes. An intent without an outcome is
  reconciled by its destination: the journaled digest there means created, absent means
  pending, anything else fails that item without touching it.

Imported content is inert: no default, deployment, launch or activation happens here.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import logging
import math
import re
import secrets
import time

from ..gallery import contract as gallery_contract
from ..playbooks import store
from . import format as fmt
from . import kinds, state

log = logging.getLogger(__name__)

TTL_S = 15 * 60
JOURNAL_RETENTION_S = 30 * 86400
PREVIEW_TEXT_BYTES = 1024 * 1024
MAX_PROVENANCE = 2000
#: Staged bundle bytes are bounded across every preview (#1738 review): identical uploads share one
#: staged copy (named by digest), a copy lives one TTL after its latest preview, and a new bundle
#: that doesn't fit evicts the oldest copies no confirmed import still needs, or is refused.
MAX_STAGED_BYTES = 128 * 1024 * 1024
MAX_STAGED_BUNDLES = 16
#: Preview records (each with at most one journal) are bounded too (#1738 review 7218).
MAX_RECORDS = 64
#: A settled import that gives up its record keeps a compact receipt (its final per-item result)
#: for 30 days after it is written, so a lost response can still be replayed (#1738 review
#: 7240). Receipts are bounded too; when they are full, a settled record is not given up.
MAX_RECEIPTS = 1024
TOKEN_RE = re.compile(r"[0-9a-f]{32}")
MAX_RENAME = 999


class ShareError(store.StoreError):
    """A request the share routes refuse; `status` and `reason` reach the client."""


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(value: object) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise ShareError("expected a preview token", status=422)
    return value


def _label(kind: str) -> str:
    return kinds.LABELS.get(kind, kind).lower()


# ---- export ---------------------------------------------------------------------------------


def catalog() -> dict:
    try:
        known = imports()
    except state.StateError:
        known = {}  # the export picker never decides anything from the marks
    return {"items": kinds.catalog(), "imports": known}


def export(items: object, source: object = None) -> tuple[str, bytes]:
    if not isinstance(items, list) or not 1 <= len(items) <= fmt.MAX_ITEMS:
        raise ShareError(f"choose 1 to {fmt.MAX_ITEMS} items to export", status=422)
    if source is not None and (
        not isinstance(source, str)
        or len(source) > fmt.SOURCE_MAX
        or fmt._CONTROL.search(source)
        or not fmt._utf8(source)
    ):
        raise ShareError(f"a source label is at most {fmt.SOURCE_MAX} characters", status=422)
    wanted: list[tuple[str, str]] = []
    for raw in items:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"kind", "id"}
            or raw["kind"] not in kinds.KINDS
            or not isinstance(raw["id"], str)
            or not kinds.id_ok(raw["kind"], raw["id"])
        ):
            raise ShareError("each item is {kind, id} of a skill or playbook", status=422)
        if (raw["kind"], raw["id"]) not in wanted:
            wanted.append((raw["kind"], raw["id"]))
    # A skill the library can't read travels as a reference, never as a broken item.
    held = {sid for sid, rev in kinds.ids("skill").items() if rev is not None}
    read: dict[tuple[str, str], dict] = {}
    queue = list(wanted)
    while queue:
        kind, item_id = queue.pop(0)
        if (kind, item_id) in read:
            continue
        try:
            data = kinds.export(kind, item_id)
        except kinds.ItemRefused as exc:
            raise ShareError(f"{kinds.LABELS[kind]} {item_id}: {exc}", status=422) from None
        deps = [d for d in data["depends_on"] if d.split(":", 1)[1] in held]
        data["depends_on"] = deps
        read[(kind, item_id)] = data
        queue.extend(tuple(d.split(":", 1)) for d in deps)
        if len(read) > fmt.MAX_ITEMS:
            raise ShareError(f"a bundle holds at most {fmt.MAX_ITEMS} items", status=422)
    order = sorted(read, key=lambda k: (kinds.LEVEL[k[0]], list(read).index(k)))
    now = _now()
    try:
        data = fmt.write(
            [
                {
                    "kind": k,
                    "id": i,
                    **{f: read[(k, i)][f] for f in ("title", "files", "depends_on")},
                }
                for k, i in order
            ],
            created_at=_iso(now),
            app_version=_app_version(),
            source=source,
        )
    except fmt.Refused as exc:
        raise ShareError(exc.detail, status=422) from None
    stamp = dt.datetime.fromtimestamp(now, dt.UTC).strftime("%Y%m%d-%H%M%S")
    return f"battlelab-{stamp}{fmt.SUFFIX}", data


def _app_version() -> str:
    from ..version import get_version

    return get_version()[:64]


# ---- preview --------------------------------------------------------------------------------


def _order(bundle: fmt.Bundle) -> list[fmt.Item]:
    """Topological order: dependencies first; ties by kind level, then manifest position."""
    placed: list[fmt.Item] = []
    done: set[str] = set()
    pending = list(bundle.items)
    while pending:
        ready = [i for i in pending if all(d in done for d in i.depends_on)]
        ready.sort(key=lambda i: (kinds.LEVEL.get(i.kind, 9), i.index))
        nxt = ready[0]  # format.read refused cycles, so something is always ready
        placed.append(nxt)
        done.add(nxt.key)
        pending.remove(nxt)
    return placed


def _fresh_id(kind: str, item_id: str, taken: set[str]) -> str | None:
    if item_id not in taken:
        return item_id
    limit = 48 if kind == "playbook" else 64
    for n in range(2, MAX_RENAME + 1):
        suffix = f"-{n}"
        candidate = item_id[: limit - len(suffix)].rstrip("-_") + suffix
        if candidate not in taken and kinds.id_ok(kind, candidate):
            return candidate
    return None


def _bindings() -> dict:
    return {kind: kinds.ids(kind) for kind in kinds.KINDS}


def _plan(bundle: fmt.Bundle, local: dict) -> list[dict]:
    by_key = {item.key: item for item in bundle.items}
    plan: dict[str, dict] = {}
    taken = {kind: set(local.get(kind, {})) for kind in kinds.KINDS}
    # A playbook's role skill that the bundle does not carry keeps its own name here, so no
    # renamed copy may take it: a bundled `tdd` renamed to `tdd-2` must never become the skill
    # another playbook in this file already names as `tdd-2`.
    for item in bundle.items:
        try:
            requires = kinds.validate(item.kind, item.id, item.files)
        except kinds.ItemRefused:
            continue
        taken["skill"].update(s for s in requires if f"skill:{s}" not in by_key)
    for item in _order(bundle):
        entry = {
            "key": item.key,
            "kind": item.kind,
            "id": item.id,
            "title": item.title,
            "depends_on": list(item.depends_on),
            "dest": None,
            "status": "new",
            "reason": None,
            "digest": None,
            "uses": [],
        }
        plan[item.key] = entry
        try:
            requires = kinds.validate(item.kind, item.id, item.files)
            expected = sorted(f"skill:{s}" for s in requires if f"skill:{s}" in by_key)
            if sorted(item.depends_on) != expected:
                raise kinds.ItemRefused("its declared dependencies don't match its content")
            for dep in item.depends_on:
                if plan[dep]["status"] == "refused":
                    raise kinds.ItemRefused(
                        f"needs {_label(by_key[dep].kind)} {by_key[dep].id}, "
                        "which can't be imported"
                    )
            dest = _fresh_id(item.kind, item.id, taken[item.kind])
            if dest is None:
                raise kinds.ItemRefused("no free ID is left for a renamed copy")
            renames = {
                by_key[d].id: plan[d]["dest"] for d in item.depends_on if by_key[d].kind == "skill"
            }
            final = kinds.rewrite(item.kind, item.id, dest, item.files, renames)
        except kinds.ItemRefused as exc:
            entry.update(status="refused", reason=str(exc))
            continue
        taken[item.kind].add(dest)
        entry.update(
            dest=dest,
            status="new" if dest == item.id else "renamed",
            digest=fmt.tree_digest(final),
            uses=[{"from": a, "to": b} for a, b in sorted(renames.items()) if a != b],
        )
    return [plan[item.key] for item in bundle.items]


def _files_view(item: fmt.Item, budget: list[int]) -> list[dict]:
    out = []
    for path in sorted(item.files):
        data = item.files[path]
        row: dict = {"path": path, "size": len(data)}
        if len(data) <= budget[0]:
            try:
                row["text"] = data.decode("utf-8")
                budget[0] -= len(data)
            except UnicodeDecodeError:
                row["binary"] = True
        else:
            row["omitted"] = True
        out.append(row)
    return out


def _bundle_name(digest: str) -> str:
    return f"bundle-{digest}{fmt.SUFFIX}"


def _prune() -> None:
    """Remove expired state. Best effort, per file: one unreadable journal never blocks previews."""
    now = _now()
    entries = state.entries()
    names = {name for name, _m, _s in entries}
    for name, mtime, _size in entries:
        try:
            if name.startswith("journal-") and "preview-" + name[len("journal-") :] not in names:
                # A journal without its preview record can't be replayed; it goes once it holds
                # no copy's only provenance.
                if not _holds_unmarked(name):
                    state.remove_any(name)
                continue
            _prune_one(name, now - mtime)
        except (state.StateError, OSError):
            log.warning("share state: could not prune %s", name)


def _prune_one(name: str, age: float) -> None:
    if name.startswith("bundle-"):
        # Staged bytes are only ever read for an unexpired preview, and every preview of these
        # bytes refreshed the file: one TTL after the latest, nothing can need them.
        if age > TTL_S + 60:
            state.remove_any(name)
    elif name.startswith("preview-") and age > 2 * TTL_S:
        token = name.split("-", 1)[1].split(".", 1)[0]
        journal = state.read_json(f"journal-{token}.json") if TOKEN_RE.fullmatch(token) else None
        # A confirmed preview keeps its record while its journal lives (retries, P1 replay).
        if journal is None or age > JOURNAL_RETENTION_S:
            state.remove_any(name)
    elif name.startswith(("journal-", "receipt-")) and age > JOURNAL_RETENTION_S:
        if not _holds_unmarked(name):  # the only provenance of a copy is never pruned
            state.remove_any(name)
    elif name.startswith(".tmp-") and age > 2 * TTL_S:
        state.remove_any(name)


def _inventory(now: float) -> list[dict]:
    """Every preview record with what it still holds: its journal, whether it has expired, and
    whether a confirm left items that a retry may still apply (only those pin staged bytes)."""
    rows = []
    for name, mtime, _size in state.entries():
        if not (name.startswith("preview-") and name.endswith(".json")):
            continue
        token = name[len("preview-") : -len(".json")]
        if not TOKEN_RE.fullmatch(token):
            continue
        try:
            record = state.read_json(name)
            journal = state.read_json(f"journal-{token}.json")
            if record is not None:
                _check_shapes(record, journal, token)  # damaged state goes to the pinned fallback
            live = bool(record) and record["expires_at"] >= now
            pending = bool(record and journal and live) and any(
                s["status"] == "pending" for s in _status(record, journal).values()
            )
            # An intent without an outcome is a copy that may exist: the record stays until a
            # confirm settles it (a live one by digest; an expired one, with nothing written, as
            # failed), or its copy never gets its outcome or mark.
            open_intent = bool(journal) and any(
                "outcome" not in e for e in journal["entries"].values()
            )
            row = {
                "record": record,
                "confirmed": journal is not None,
                "live": live,
                "pending": pending or open_intent,
                "pins_bytes": pending,
                # Built here, inside the guard: a record that can't produce its receipt is
                # pinned (below), never dropped on a crash.
                "receipt": _receipt(token, record, journal) if journal is not None else None,
            }
        except Exception:  # noqa: BLE001 — unreadable or odd-shaped state is kept, never evicted
            # A record or journal that can't be read now (a mode or link count a backup changed,
            # a damaged shape) may hold a pending retry: pin it; the 30-day prune removes it.
            row = {
                "record": None,
                "confirmed": True,
                "live": True,
                "pending": True,
                "receipt": None,
            }
            row["pins_bytes"] = True
        rows.append({"token": token, "mtime": mtime, **row})
    return rows


def _result(token: str, record: dict, journal: dict, statuses: dict, stale: str | None) -> dict:
    plan = {e["key"]: e for e in record["items"]}
    return {
        "token": token,
        "bundle": record["bundle"],
        "repreview": stale,
        "items": [
            {
                "key": key,
                "kind": plan[key]["kind"],
                "id": plan[key]["id"],
                "title": plan[key]["title"],
                **statuses[key],
            }
            for key in (e["key"] for e in record["items"])
        ],
    }


def _receipt(token: str, record: dict, journal: dict) -> dict:
    """What a replay of this token answers once its record is gone: exactly what a confirm of
    the record answers now. A dropped record is never live with pending items (those pin it), so
    any pending item is one an expired preview can't apply: stale."""
    statuses = _status(record, journal)
    pending = [k for k, v in statuses.items() if v["status"] == "pending"]
    stale = "expired" if pending else None
    _mark_stale(statuses, pending, stale)
    return {
        "format": 1,
        "selection": journal["selection"],
        "keys": sorted(e["key"] for e in record["items"]),
        "result": _result(token, record, journal, statuses, stale),
    }


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _number(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int beyond float range
        return False


def _check_shapes(record: object, journal: object, token: str) -> None:
    """Raise `state.StateError` unless a preview record (and its journal, if any) has every field
    confirm and the inventory read, with the right types. Damaged state is a 503 and is kept."""
    item_fields = ("key", "kind", "id", "title", "status")
    ok = (
        isinstance(record, dict)
        and isinstance(record.get("bundle"), str)
        and record.get("token") == token
        and _number(record.get("expires_at"))
        and _number(record.get("created_at"))
        # A preview lives one TTL: anything else is damage (and keeps datetime/math calls sane).
        and 0 < record["created_at"] < record["expires_at"] <= record["created_at"] + TTL_S + 60
        # ... and was never created in the future (no far-future record lives or pins forever).
        # (one TTL of slack, so a clock stepped back by NTP never makes a live record damaged)
        and record["created_at"] <= _now() + TTL_S
        and isinstance(record.get("bindings"), dict)
        and isinstance(record.get("items"), list)
        and all(
            isinstance(i, dict)
            and all(isinstance(i.get(f), str) for f in item_fields)
            and _strings(i.get("depends_on"))
            for i in record["items"]
        )
    )
    if ok and journal is not None:
        ok = (
            isinstance(journal, dict)
            and journal.get("token") == token
            and journal.get("bundle") == record["bundle"]
            and _strings(journal.get("selection"))
            and journal["selection"] == sorted(journal["selection"])
            and isinstance(journal.get("entries"), dict)
            and all(
                isinstance(e, dict) and isinstance(e.get("dest"), str)
                for e in journal["entries"].values()
            )
        )
    if not ok:
        raise state.StateError("share state is damaged; it was kept untouched")


def _drop_record(row: dict) -> None:
    """Give up a record. A settled import first leaves its receipt (built and checked when the
    row was read), the final result a replay of its token answers."""
    token = row["token"]
    if row["confirmed"]:
        state.write_json(f"receipt-{token}.json", row["receipt"])
    state.remove_any(f"preview-{token}.json")
    state.remove_any(f"journal-{token}.json")


def _record_room(rows: list[dict]) -> list[dict]:
    """The records to drop so one more fits within `MAX_RECORDS`: oldest unconfirmed first, then
    settled imports while receipts have room; a record a retry still needs is never dropped.
    Refuses (dropping nothing) when it can't fit."""
    if len(rows) < MAX_RECORDS:
        return []
    need = len(rows) - MAX_RECORDS + 1
    spare = [r for r in rows if not r["pending"]]
    unconfirmed = sorted((r for r in spare if not r["confirmed"]), key=lambda r: r["mtime"])
    settled = sorted((r for r in spare if r["confirmed"]), key=lambda r: r["mtime"])
    receipts = sum(1 for n, _m, _s in state.entries() if n.startswith("receipt-"))
    candidates = unconfirmed + settled[: max(0, MAX_RECEIPTS - receipts)]
    if len(candidates) < need:
        if receipts >= MAX_RECEIPTS and settled:
            raise ShareError(
                "BattleLab keeps a receipt of each finished import for "
                f"{JOURNAL_RETENTION_S // 86400} days so a lost answer can be replayed, and it "
                f"holds {MAX_RECEIPTS} now. New "
                "previews resume as the oldest receipts expire.",
                status=503,
                reason="staging_full",
            )
        raise _full()
    return candidates[:need]


def _full() -> ShareError:
    return ShareError(
        "Too many imports are waiting to finish. Confirm or close them, or try again in "
        f"{TTL_S // 60} minutes.",
        status=503,
        reason="staging_full",
    )


def _stage(data: bytes, digest: str, rows: list[dict]) -> None:
    """Stage the verified bytes once per digest, within the aggregate bound (under the lock).

    Only a confirmed import with pending items pins its bytes. Copies are evicted oldest first,
    and only once it is certain the new bundle then fits: a refusal evicts nothing."""
    name = _bundle_name(digest)
    staged = {n: (m, size) for n, m, size in state.entries() if n.startswith("bundle-")}
    if name in staged:
        state.touch(name)
        return
    needed = {_bundle_name((r["record"] or {}).get("bundle", "")) for r in rows if r["pins_bytes"]}
    if any(r["pins_bytes"] and not r["record"] for r in rows):
        # An unreadable record may pin any staged copy: evict none while one exists.
        needed |= set(staged)
    spare = sorted((n for n in staged if n not in needed), key=lambda n: staged[n][0])
    total = sum(size for _m, size in staged.values())
    pinned = total - sum(staged[n][1] for n in spare)
    if pinned + len(data) > MAX_STAGED_BYTES or len(staged) - len(spare) + 1 > MAX_STAGED_BUNDLES:
        raise _full()
    for old in spare:
        if total + len(data) <= MAX_STAGED_BYTES and len(staged) < MAX_STAGED_BUNDLES:
            break
        state.remove_any(old)
        total -= staged.pop(old)[1]
    state.write_bytes(name, data)


def _same_plan(row: dict, digest: str, bindings: dict, items: list[dict]) -> bool:
    record = row["record"] or {}
    if not row["record"]:
        return False
    return (
        row["live"]
        and not row["confirmed"]
        and record.get("bundle") == digest
        and record.get("bindings") == bindings
        and record.get("items") == items
    )


def preview(data: bytes) -> dict:
    bundle = fmt.read(data)
    local = _bindings()
    items = _plan(bundle, local)
    bindings = {kind: sorted([k, v] for k, v in ids.items()) for kind, ids in local.items()}
    try:
        head = _preview_locked(data, bundle, items, bindings)
    except fmt.Refused:
        raise
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
        # Damaged share state met while recording or reusing a preview: a 503, never a 500.
        log.warning("share preview: damaged state (%s)", type(exc).__name__, exc_info=True)
        raise state.StateError("share state is damaged; preview the file again later") from None
    # Built from the verified bundle, outside the damaged-state boundary: a real bug here stays
    # a visible 500 rather than reading as damaged state.
    budget = [PREVIEW_TEXT_BYTES]
    return {
        "token": head["token"],
        "expires_at": head["expires_at"],
        "bundle": {
            "digest": bundle.digest,
            "size": bundle.size,
            "format": fmt.FORMAT,
            "created_at": bundle.created_at,
            "app_version": bundle.app_version,
            "source": bundle.source,
            "items": len(bundle.items),
            "files": sum(len(i.files) for i in bundle.items),
        },
        "items": [
            {**entry, "files": _files_view(bundle.item(entry["key"]), budget)}
            for entry in head["items"]
        ],
    }


def _preview_locked(data: bytes, bundle: fmt.Bundle, items: list, bindings: dict) -> dict:
    now = _now()
    with state.lock():
        _sync_marks()
        _prune()
        rows = _inventory(now)
        # The same file previewed again against the same library is the same plan: answer the
        # existing token instead of recording another (a repeated upload costs no state).
        reuse = _reusable(rows, bundle.digest, bindings, items)
        if reuse is not None:
            record = reuse["record"]
        else:
            drop = _record_room(rows)  # both checks refuse before anything is removed
            _stage(data, bundle.digest, rows)
            for old in drop:
                _drop_record(old)
            token = secrets.token_hex(16)
            record = {
                "format": 1,
                "token": token,
                "bundle": bundle.digest,
                "created_at": now,
                "expires_at": now + TTL_S,
                "bindings": bindings,
                "items": items,
            }
            state.write_json(f"preview-{token}.json", record)
        # Everything read from the stored record is taken here, inside the boundary.
        return {
            "token": record["token"],
            "expires_at": _iso(record["expires_at"]),
            "items": [_public(entry) for entry in record["items"]],
        }


def _reusable(rows: list[dict], digest: str, bindings: dict, items: list[dict]) -> dict | None:
    staged = {n for n, _m, _s in state.entries() if n.startswith("bundle-")}
    for row in rows:
        if _same_plan(row, digest, bindings, items) and _bundle_name(digest) in staged:
            return row
    return None


def _public(entry: dict) -> dict:
    return {
        k: entry[k]
        for k in ("key", "kind", "id", "title", "dest", "status", "reason", "depends_on", "uses")
    }


# ---- confirm --------------------------------------------------------------------------------


def _fresh(record: dict, journal: dict) -> str | None:
    """Why pending items can't be applied now, or None. Own journaled writes are excluded."""
    if _now() > record["expires_at"]:
        return "expired"
    own: dict[str, set[str]] = {}
    for key, entry in journal["entries"].items():
        own.setdefault(key.split(":", 1)[0], set()).add(entry["dest"])
    for kind in kinds.KINDS:
        bound = {k: v for k, v in record["bindings"].get(kind, [])}
        current = {k: v for k, v in kinds.ids(kind).items() if k not in own.get(kind, set())}
        if current != bound:
            return "changed"
    return None


_STALE = {
    "expired": "This preview expired. Preview the file again to import this item.",
    "changed": (
        "Your library changed since this preview. Preview the file again to import this item."
    ),
    "evicted": (
        "This preview's file was cleared to make room for other imports. Preview the file again "
        "to import this item."
    ),
}


def _staged_bytes(record: dict) -> bytes | None:
    data = state.read_bytes(_bundle_name(record["bundle"]), fmt.MAX_BUNDLE_BYTES)
    if data is None or hashlib.sha256(data).hexdigest() != record["bundle"]:
        return None
    return data


def _record_outcome(journal: dict, key: str, **outcome) -> None:
    journal["entries"][key].update(outcome, at=_now())
    state.write_json(f"journal-{journal['token']}.json", journal)


def _reconcile(journal: dict, plan: dict, *, expired: bool = False) -> None:
    """Settle intents whose outcome was never recorded (a crash between publish and record).

    Absent while the preview is live: still pending (the item is applied again). Absent once it
    has expired: nothing was written and nothing can be any more, so it settles as failed."""
    for key, entry in journal["entries"].items():
        if "outcome" in entry:
            continue
        try:
            current = kinds.at(plan[key]["kind"], entry["dest"])
        except (OSError, store.StoreError, gallery_contract.GalleryError, ValueError):
            continue  # unreadable now: stays pending, decided on a later attempt
        if current == entry["digest"]:
            _record_outcome(journal, key, outcome="created", revision=current, recovered=True)
            _remember(plan[key], entry["dest"], current, journal["bundle"])
        elif current is not None:
            _record_outcome(
                journal,
                key,
                outcome="failed",
                retry=False,
                reason=f"A different copy now holds {entry['dest']}; nothing was overwritten.",
            )
        elif expired:
            _record_outcome(
                journal,
                key,
                outcome="failed",
                retry=False,
                reason="The preview expired before this item was written; nothing was written. "
                "Preview the file again to import it.",
            )


def _needs(entry: dict, tail: str) -> str:
    return f"needs {_label(entry['kind'])} {entry['id']}, {tail}"


def _status(record: dict, journal: dict) -> dict[str, dict]:
    """Each item's settled status before applying: selection, refusals, recorded outcomes."""
    plan = {e["key"]: e for e in record["items"]}
    chosen = set(journal["selection"])
    out: dict[str, dict] = {}
    for key in _topo(record):
        entry = plan[key]
        recorded = journal["entries"].get(key, {})
        if recorded.get("outcome") == "created":
            out[key] = {"status": "created", "dest": recorded["dest"], "reason": None}
        elif recorded.get("outcome") == "failed" and not recorded.get("retry"):
            out[key] = {"status": "failed", "dest": recorded["dest"], "reason": recorded["reason"]}
        elif key not in chosen:
            out[key] = {"status": "not_selected", "dest": None, "reason": None}
        elif entry["status"] == "refused":
            out[key] = {"status": "refused", "dest": None, "reason": entry["reason"]}
        else:
            reason = None
            status = "refused"
            for dep in entry["depends_on"]:
                dep_entry = plan[dep]
                if out[dep]["status"] == "not_selected":
                    reason = _needs(dep_entry, "which isn't being imported")
                elif out[dep]["status"] == "refused":
                    reason = _needs(dep_entry, "which can't be imported")
                elif out[dep]["status"] in ("failed", "skipped"):
                    # A dependency that failed for good: this item can never be applied under
                    # this preview, so it is settled (and pins nothing), not pending.
                    status = "skipped"
                    reason = f"Not attempted: it {_needs(dep_entry, 'which failed')}."
                if reason:
                    break
            out[key] = (
                {"status": status, "dest": None, "reason": reason}
                if reason
                else {"status": "pending", "dest": entry["dest"], "reason": None}
            )
    return out


def _topo(record: dict) -> list[str]:
    items = {e["key"]: e for e in record["items"]}
    index = {e["key"]: n for n, e in enumerate(record["items"])}
    done: list[str] = []
    pending = list(items)
    while pending:
        ready = [k for k in pending if all(d in done for d in items[k]["depends_on"])]
        if not ready:
            raise state.StateError("share state is damaged: its dependencies can't be ordered")
        ready.sort(key=lambda k: (kinds.LEVEL.get(items[k]["kind"], 9), index[k]))
        done.append(ready[0])
        pending.remove(ready[0])
    return done


def _remember(entry: dict, dest: str, revision: str, bundle: str) -> None:
    """Mark the new copy *imported from a file* with the bundle digest. Best effort: the copy and
    its journaled outcome already exist, so an unreadable or unwritable mark never fails (or
    misreports) the import; the mark is simply missing."""
    try:
        _write_mark(entry, dest, revision, bundle)
    except (state.StateError, OSError, ValueError, TypeError, AttributeError, ArithmeticError):
        log.warning("share import: could not record the imported-from-a-file mark for %s", dest)


def _marks() -> dict:
    """The stored marks (`imported.json`): `{}` when absent; `StateError` when it can't be read
    or has an odd shape (never treated as empty, and never overwritten)."""
    try:
        record = state.read_json("imported.json")
    except OSError:
        raise state.StateError("the import record can't be read") from None
    if record is None:
        return {}
    items = record.get("items")
    if not isinstance(items, dict) or not all(isinstance(v, dict) for v in items.values()):
        raise state.StateError("the import record is damaged")
    return items


def _evidence() -> dict:
    """Copies this install's journals and receipts say an import created, as marks. They are the
    durable record behind `imported.json`: a mark write that failed (it is best effort) never
    loses a copy's provenance, because the journal or receipt that recorded the outcome stays
    until its marks are stored (`_holds_unmarked`)."""
    out: dict = {}
    for name, _mtime, _size in state.entries():
        try:
            if name.startswith("journal-"):
                doc = state.read_json(name)
                if doc is None:
                    # Dropped since the listing: its receipt is written before it goes.
                    name = "receipt-" + name[len("journal-") :]
                else:
                    for key, entry in (doc.get("entries") or {}).items():
                        if isinstance(entry, dict) and entry.get("outcome") == "created":
                            out[f"{key.split(':', 1)[0]}:{entry['dest']}"] = {
                                "bundle": doc["bundle"],
                                "revision": entry.get("revision"),
                            }
                    continue
            if name.startswith("receipt-"):
                result = (state.read_json(name) or {}).get("result") or {}
                for item in result.get("items") or []:
                    if isinstance(item, dict) and item.get("status") == "created":
                        out[f"{item['kind']}:{item['dest']}"] = {
                            "bundle": result["bundle"],
                            "revision": None,
                        }
        except (state.StateError, KeyError, TypeError, AttributeError):
            # A journal or receipt that can't be read may hold a copy's only provenance: the
            # whole record is unknown (a 503), never "no imports".
            raise state.StateError("an import record can't be read") from None
    return out


def _holds_unmarked(name: str) -> bool:
    """Whether a journal or receipt records a created copy whose mark isn't stored yet (or the
    marks can't be read): it is then the only provenance, and is kept."""
    try:
        marks = _marks()
        doc = state.read_json(name) or {}
        if name.startswith("journal-"):
            created = [
                f"{k.split(':', 1)[0]}:{e.get('dest')}"
                for k, e in (doc.get("entries") or {}).items()
                if isinstance(e, dict) and e.get("outcome") == "created"
            ]
        else:
            created = [
                f"{i.get('kind')}:{i.get('dest')}"
                for i in ((doc.get("result") or {}).get("items") or [])
                if isinstance(i, dict) and i.get("status") == "created"
            ]
        return any(key not in marks for key in created)
    except (state.StateError, KeyError, TypeError, AttributeError):
        return True  # can't tell: keep it (fail closed), never a 500 out of pruning


def _sync_marks() -> None:
    """Store the marks that only journals or receipts hold yet (a mark write that failed earlier).
    Best effort, under the share lock; a damaged record is never overwritten."""
    try:
        marks = _marks()
        missing = {k: v for k, v in _evidence().items() if k not in marks}
        if missing:
            now = _now()
            merged = {**{k: {**v, "at": now} for k, v in missing.items()}, **marks}
            state.write_json("imported.json", {"items": _bounded(merged)})
    except (state.StateError, OSError, ValueError, TypeError, AttributeError, ArithmeticError):
        log.warning("share state: could not store pending imported-from-a-file marks")


def _bounded(items: dict) -> dict:
    """Over `MAX_PROVENANCE`, only marks of copies that no longer exist are dropped, oldest first.
    The mark of a copy that still exists is its provenance and is never evicted (the libraries
    themselves are bounded, so the marks of present copies are too); when the library can't be
    read, nothing is dropped."""
    if len(items) <= MAX_PROVENANCE:
        return items
    try:
        present = {f"{k}:{i}" for k in kinds.KINDS for i in kinds.ids(k)}
    except (OSError, store.StoreError, gallery_contract.GalleryError, ValueError):
        return items

    def when(key: str) -> int | float:
        # No conversion: an int too large for a float is still comparable as it is.
        at = items[key].get("at")
        if isinstance(at, bool) or not isinstance(at, int | float):
            return 0
        return 0 if isinstance(at, float) and math.isnan(at) else at

    gone = sorted((k for k in items if k not in present), key=when)
    for old in gone[: max(0, len(items) - MAX_PROVENANCE)]:
        del items[old]
    return items


def _write_mark(entry: dict, dest: str, revision: str, bundle: str) -> None:
    items = dict(_marks())  # a damaged record raises: it is never replaced by a partial one
    items[f"{entry['kind']}:{dest}"] = {"bundle": bundle, "revision": revision, "at": _now()}
    state.write_json("imported.json", {"items": _bounded(items)})


def imports() -> dict:
    """`{"<kind>:<id>": {bundle, revision}}` of copies created from a file: the stored marks plus
    any the journals and receipts hold that a failed mark write didn't store. An absent record
    with no such evidence is authoritatively empty; a record that can't be read or has an odd
    shape raises `StateError` (a 503), never `{}`: the import dialog treats "unknown" as
    "copies may exist" and pre-selects nothing."""
    # Evidence first: a concurrent preview may store a mark and then prune its journal, so the
    # mark read afterwards still sees it.
    evidence = _evidence()
    marks = _marks()
    found = {**evidence, **marks}

    def text(value: object) -> str | None:  # only strings reach the response
        return value if isinstance(value, str) else None

    return {
        k: {"bundle": text(v.get("bundle")), "revision": text(v.get("revision"))}
        for k, v in found.items()
    }


def _apply(record: dict, journal: dict, statuses: dict, bundle: fmt.Bundle) -> None:
    plan = {e["key"]: e for e in record["items"]}
    for key in _topo(record):
        if statuses[key]["status"] != "pending":
            continue
        entry = plan[key]
        blocked = next((d for d in entry["depends_on"] if statuses[d]["status"] != "created"), None)
        if blocked is not None:
            dep = plan[blocked]
            word = "failed" if statuses[blocked]["status"] == "failed" else "was not imported"
            statuses[key] = {
                "status": "skipped",
                "dest": entry["dest"],
                "reason": f"Not attempted: it {_needs(dep, 'which ' + word)}.",
            }
            continue
        moved = _moved_dependency(journal, plan, entry)
        if moved is not None:
            statuses[key] = {
                "status": "stale",
                "dest": None,
                "reason": (
                    f"Not attempted: the imported copy {moved} it needs was changed or removed "
                    "since. Preview the file again to import this item."
                ),
            }
            continue
        item = bundle.item(key)
        renames = {
            plan[d]["id"]: statuses[d]["dest"]
            for d in entry["depends_on"]
            if plan[d]["kind"] == "skill"
        }
        try:
            final = kinds.rewrite(item.kind, item.id, entry["dest"], item.files, renames)
        except kinds.ItemRefused as exc:
            statuses[key] = {"status": "failed", "dest": entry["dest"], "reason": str(exc)}
            continue
        digest = fmt.tree_digest(final)
        if digest != entry["digest"]:
            statuses[key] = {
                "status": "failed",
                "dest": entry["dest"],
                "reason": "The planned content changed; preview the file again.",
            }
            continue
        try:
            occupant = kinds.at(item.kind, entry["dest"])
        except (OSError, store.StoreError, gallery_contract.GalleryError, ValueError) as exc:
            statuses[key] = {
                "status": "failed",
                "dest": entry["dest"],
                "reason": f"Nothing was written: {_failure_text(exc)}. Confirm again to retry.",
            }
            continue
        recorded = journal["entries"].get(key, {})
        if occupant == digest and "intent_at" in recorded and "outcome" not in recorded:
            # Our own earlier attempt published it but its outcome was never recorded (the
            # reconcile rule, applied now that the destination could be read).
            _record_outcome(journal, key, outcome="created", revision=occupant, recovered=True)
            _remember(entry, entry["dest"], occupant, journal["bundle"])
            statuses[key] = {"status": "created", "dest": entry["dest"], "reason": None}
            continue
        if occupant is not None:
            # Something else (another import, a manual copy) holds the planned ID: never adopt it.
            reason = (
                f"{entry['dest']} was taken since the preview; nothing was overwritten. "
                "Preview the file again to import this item."
            )
            journal["entries"][key] = {"dest": entry["dest"], "digest": digest}
            _record_outcome(journal, key, outcome="failed", retry=False, reason=reason)
            statuses[key] = {"status": "failed", "dest": entry["dest"], "reason": reason}
            continue
        journal["entries"][key] = {"dest": entry["dest"], "digest": digest, "intent_at": _now()}
        state.write_json(f"journal-{journal['token']}.json", journal)
        try:
            revision = kinds.publish(item.kind, entry["dest"], final)
        except (OSError, store.StoreError, gallery_contract.GalleryError, ValueError) as exc:
            statuses[key] = _after_failure(journal, entry, key, digest, exc)
            continue
        _record_outcome(journal, key, outcome="created", revision=revision)
        _remember(entry, entry["dest"], revision, journal["bundle"])
        statuses[key] = {"status": "created", "dest": entry["dest"], "reason": None}


def _failure_text(exc: Exception) -> str:
    """Words for a failed publication. An OSError's text can name host paths: only its code."""
    if isinstance(exc, OSError):
        code = errno.errorcode.get(exc.errno or 0, "unknown")
        return f"a storage error ({code})"
    return getattr(exc, "detail", None) or str(exc) or type(exc).__name__


def _moved_dependency(journal: dict, plan: dict, entry: dict) -> str | None:
    """The destination of a created dependency that no longer holds its journaled revision.

    A retry applies a dependent only against the exact copies this import made: a copy deleted,
    edited or replaced since then (even by another skill now at that ID) blocks the dependent."""
    for dep in entry["depends_on"]:
        recorded = journal["entries"].get(dep, {})
        if recorded.get("outcome") != "created":
            continue
        try:
            current = kinds.at(plan[dep]["kind"], recorded["dest"])
        except (OSError, store.StoreError, gallery_contract.GalleryError, ValueError):
            current = None
        if current != recorded.get("revision"):
            return recorded["dest"]
    return None


def _after_failure(journal: dict, entry: dict, key: str, digest: str, exc: Exception) -> dict:
    """A publication refused or failed: decide by what the destination holds now."""
    try:
        current = kinds.at(entry["kind"], entry["dest"])
    except (OSError, store.StoreError, gallery_contract.GalleryError, ValueError):
        # Can't tell whether it was published: the intent stays without an outcome, and the next
        # confirm reconciles it by destination and digest. Never a permanent "taken".
        return {
            "status": "failed",
            "dest": entry["dest"],
            "reason": f"Nothing was written: {_failure_text(exc)}. Confirm again to retry.",
        }
    if current == digest:
        _record_outcome(journal, key, outcome="created", revision=current, recovered=True)
        _remember(entry, entry["dest"], current, journal["bundle"])
        return {"status": "created", "dest": entry["dest"], "reason": None}
    if current is None:
        detail = _failure_text(exc)
        reason = f"Nothing was written: {detail}. Confirm again to retry this item."
        _record_outcome(journal, key, outcome="failed", retry=True, reason=reason)
        return {"status": "failed", "dest": entry["dest"], "reason": reason}
    reason = (
        f"{entry['dest']} was taken since the preview; nothing was overwritten. "
        "Preview the file again to import this item."
    )
    _record_outcome(journal, key, outcome="failed", retry=False, reason=reason)
    return {"status": "failed", "dest": entry["dest"], "reason": reason}


def confirm(token: object, selection: object) -> dict:
    token = _token(token)
    if (
        not isinstance(selection, list)
        or not all(isinstance(k, str) for k in selection)
        or len(set(selection)) != len(selection)
        or len(selection) > fmt.MAX_ITEMS
    ):
        raise ShareError("selection is a list of distinct item keys", status=422)
    if not selection:
        raise ShareError("choose at least one item to import", status=422)
    with state.lock():
        try:
            return _confirm_locked(token, selection)
        except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
            # Damaged share state (the app's own private files): a 503, kept untouched. Nothing
            # can be duplicated by trying again (NOREPLACE + the digest checks), so the storage
            # answer's "confirming again never makes a second copy" stays true.
            log.warning("share import %s: damaged state (%s)", token, type(exc).__name__)
            raise state.StateError("share state is damaged; it was kept untouched") from None


def _confirm_locked(token: str, selection: list[str]) -> dict:
    record = state.read_json(f"preview-{token}.json")
    if record is None:
        # Unreadable (mode, link count) is a 503 like an unreadable record: it may be read
        # again, and "preview again" would import the created items a second time. Only a
        # receipt whose shape is damaged, which never heals, answers "gone".
        receipt = state.read_json(f"receipt-{token}.json")
        if receipt is not None and not (
            _strings(receipt.get("selection"))
            and _strings(receipt.get("keys"))
            and isinstance(receipt.get("result"), dict)
            and isinstance(receipt["result"].get("items"), list)
        ):
            receipt = None  # a damaged receipt replays nothing: the preview is gone
        if receipt is not None:
            # A settled import whose record gave way: its replay answers the recorded result.
            unknown = sorted(set(selection) - set(receipt["keys"]))
            if unknown:
                raise ShareError(f"{fmt.shown(unknown[0])} is not in this preview", status=422)
            if receipt.get("selection") != sorted(selection):
                raise ShareError(
                    "This preview was already confirmed with a different selection. "
                    "Preview the file again to import other items.",
                    status=409,
                    reason="selection",
                )
            return receipt["result"]
        raise ShareError(
            "This preview is gone. Preview the file again.", status=404, reason="missing"
        )
    journal = state.read_json(f"journal-{token}.json")
    _check_shapes(record, journal, token)  # damaged state is a 503 (kept), never a 500
    keys = {e["key"] for e in record["items"]}
    unknown = sorted(set(selection) - keys)
    if unknown:
        raise ShareError(f"{fmt.shown(unknown[0])} is not in this preview", status=422)
    if journal is not None and journal["selection"] != sorted(selection):
        raise ShareError(
            "This preview was already confirmed with a different selection. "
            "Preview the file again to import other items.",
            status=409,
            reason="selection",
        )
    if journal is None:
        why = _fresh(record, {"entries": {}})
        if why is None and _staged_bytes(record) is None:
            why = "evicted"  # checked before any journal exists: nothing pins this preview
        if why:
            raise ShareError(_STALE[why], status=409, reason=why)
        journal = {
            "format": 1,
            "token": token,
            "bundle": record["bundle"],
            "selection": sorted(selection),
            "created_at": _now(),
            "entries": {},
        }
        state.write_json(f"journal-{token}.json", journal)
    plan = {e["key"]: e for e in record["items"]}
    _reconcile(journal, plan, expired=_now() > record["expires_at"])
    statuses = _status(record, journal)
    pending = [k for k, s in statuses.items() if s["status"] == "pending"]
    stale = None
    if pending:
        stale = _fresh(record, journal)
        bundle = None
        if stale is None:
            data = _staged_bytes(record)
            if data is None:
                stale = "evicted"
            else:
                bundle = fmt.read(data)
        if stale is None and bundle is not None:
            _apply(record, journal, statuses, bundle)
            if any(statuses[k]["status"] == "stale" for k in pending):
                stale = "changed"
        else:
            _mark_stale(statuses, pending, stale)
    return _result(token, record, journal, statuses, stale)


def _mark_stale(statuses: dict, pending: list[str], stale: str | None) -> None:
    """Pending items a confirm can't apply now are reported stale, with the reason (shared by
    `confirm` and the receipt a dropped record leaves, so a replay answers exactly the same)."""
    if stale is None:
        return
    for key in pending:
        statuses[key] = {"status": "stale", "dest": None, "reason": _STALE[stale]}
