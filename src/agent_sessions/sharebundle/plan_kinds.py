"""The `mission-plan` share-bundle kind (#1924, #1613 P7a): a Library mission plan in a file.

**The item.** Kind `mission-plan`, id = the plan's id, one file `mission-plan.json`: exactly the
saved document (`mission_plan_library`, format 1). No origin, no saving mission, no version
history and no archive state leave the install. It has no dependencies: a plan carries its
procedures as snapshots and names its projects only as slots.

**Export** writes the current version of a live plan. The plan's own text — its ID, title,
summary and slot labels — passes the P3 credential scan (`template_kinds.scan`, fail closed). The
procedures travel as written, exactly as a playbook export does (the export banner says so).

**Import** validates the file with the Library's own validator (`mission_plan_library.validate`:
every procedure re-validated by the bundle loader's flow validator) and creates version 1 at the
planned ID, NOREPLACE. The new plan is inert: nothing is planned, dispatched or launched.
"""

from __future__ import annotations

import json

from .. import mission_plan_library as library
from .. import missions
from ..gallery import contract as gallery_contract
from ..playbooks import store
from . import template_kinds
from .template_kinds import Refused as ItemRefused
from .template_kinds import Terminal

KIND = "mission-plan"
PLAN_FILE = "mission-plan.json"


def _texts(pid: str, doc: dict) -> list[tuple[str, str]]:
    out = [("plan ID", pid), ("plan title", doc["title"]), ("plan summary", doc["summary"])]
    out.extend(("plan slot label", s["label"]) for s in doc["slots"])
    return out


def _file(doc: dict) -> dict[str, bytes]:
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return {PLAN_FILE: text.encode("utf-8")}


def _read(pid: str) -> dict:
    try:
        return library.get(pid)
    except library.NotFound:
        raise store.NotFound(pid) from None
    except missions.MissionError as exc:
        raise store.StoreError(str(exc), status=exc.status) from None


def export(pid: str, secrets: list[str]) -> dict:
    """The current version of a live plan, its own text scanned FIRST: a refusal never quotes
    what the scan would have withheld."""
    plan = _read(pid)
    doc = plan["document"]
    # The scan FIRST, before any refusal that names the plan (its ID may be what it withholds).
    template_kinds.scan(_texts(pid, doc), secrets)
    if plan["archived"]:
        raise ItemRefused("it is archived; unarchive it to export it")
    return {
        "title": doc["title"],
        "revision": f"v{plan['version']}",
        "files": _file(doc),
        "depends_on": [],
    }


def doc(files: dict[str, bytes]) -> dict:
    """An incoming plan's document, checked to the letter by the Library's own validator, or
    `ItemRefused`."""
    if set(files) != {PLAN_FILE}:
        raise ItemRefused(f"a mission plan is one file, {PLAN_FILE}")
    try:
        raw = json.loads(
            files[PLAN_FILE].decode("utf-8"),
            object_pairs_hook=gallery_contract._pairs,
            parse_float=gallery_contract._no_float,
            parse_constant=gallery_contract._no_float,
        )
    except (ValueError, UnicodeError, RecursionError, gallery_contract.GalleryError):
        raise ItemRefused(f"{PLAN_FILE} is not unambiguous JSON") from None
    fmt_no = raw.get("format") if isinstance(raw, dict) else None
    if isinstance(fmt_no, int) and not isinstance(fmt_no, bool) and fmt_no > library.FORMAT:
        raise ItemRefused("it was made by a newer BattleLab; update to import it")
    try:
        return library.validate(raw)
    except missions.MissionError as exc:
        raise ItemRefused(f"not a valid mission plan here: {exc}") from None


def validate(item_id: str, files: dict[str, bytes]) -> list[str]:
    if not library.ID_RE.fullmatch(item_id):
        raise ItemRefused(f"{item_id!r} is not a mission plan ID")
    doc(files)
    return []


def view(files: dict[str, bytes]) -> dict:
    """What the preview shows of a plan beyond its file: its counts (never a project)."""
    return library.derive(doc(files))["counts"]


def prepare(files: dict[str, bytes]) -> tuple[dict, str]:
    d = doc(files)
    return d, library.digest(d)


def publish(dest: str, payload: dict) -> str:
    """Version 1 at exactly `dest` (NOREPLACE): a taken ID is the store's 409, never a rename."""
    try:
        created = library.create(payload, origin={"kind": "file"}, pid=dest)
    except library.LibraryError as exc:
        if exc.status == 409 and "already exists" in str(exc):
            raise Terminal(f"{dest} was taken; nothing was overwritten") from None
        if exc.status == 409:  # the Library is full: plans are archived, never deleted
            raise Terminal(str(exc)) from None
        raise store.StoreError(str(exc), status=exc.status) from None
    except missions.MissionError as exc:  # the validator: the file can never be imported
        raise Terminal(str(exc)) from None
    return created["digest"]


def at(dest: str) -> str | None:
    try:
        return library.at(dest)
    except missions.MissionError as exc:
        raise store.StoreError(str(exc), status=exc.status) from None


def ids() -> dict[str, str | None]:
    """Only which ids are taken shapes an import's plan; nothing about a plan's content does."""
    try:
        return {pid: None for pid in library.ids()}
    except missions.MissionError as exc:
        raise store.StoreError(str(exc), status=exc.status) from None


def catalog() -> list[dict]:
    """Every live plan for the export picker. **One rule** (#1714 P3): a row shows the plan's own
    text only when this call's scan judged it clean; with no inventory every row is a
    placeholder."""
    try:
        secrets = template_kinds.secret_inventory()
        why_all = None
    except ItemRefused as exc:
        secrets, why_all = [], str(exc)
    try:
        plans = library.list_all()
    except missions.MissionError:
        plans = []
    out: list[dict] = []
    for plan in plans:
        row: dict = {
            "kind": KIND,
            "id": plan["id"],
            "title": plan["document"]["title"],
            "depends_on": [],
            "exportable": False,
            "reason": why_all,
            "source": "local",
            "note": None,
        }
        if why_all is not None:
            template_kinds._placeholder(row, out, "unavailable")
        else:
            try:
                template_kinds.scan(_texts(plan["id"], plan["document"]), secrets)
                row["exportable"] = True
            except ItemRefused as exc:
                template_kinds._refuse_row(row, exc, out)
        if str(row["id"]).startswith("~withheld-"):
            row["title"] = row["title"].replace("Mission-plan", "Mission plan", 1)
        out.append(row)
    return out
