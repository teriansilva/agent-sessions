"""Share bundles (#1714 P1): hostile files, round trips, renames, dependencies, freshness, order,
partial failure, lost responses and crash reconciliation — against the real skill library and
playbook store in a private tmp install."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import unicodedata
from pathlib import Path

import pytest

from agent_sessions import fileedit
from agent_sessions.gallery import library, skills
from agent_sessions.gallery import storage as gallery_storage
from agent_sessions.playbooks import loader, store
from agent_sessions.sharebundle import format as fmt
from agent_sessions.sharebundle import kinds, service, state

BUNDLED = Path(loader.__file__).resolve().parent / "bundled" / "research-brief"
ROLE_SKILLS = ("research-with-citations", "research-review")


def _install(root: Path, monkeypatch) -> Path:
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(root / "app"))
    monkeypatch.setenv("AGENT_SESSIONS_PLAYBOOKS_DIR", str(root / "playbooks"))
    empty = root / "bundled"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(loader, "BUNDLED_ROOT", empty)
    return root


@pytest.fixture
def install(tmp_path, monkeypatch):
    assert fileedit.install_lease_signal_handler()
    return _install(tmp_path / "one", monkeypatch)


def _skill(sid: str, body: str = "") -> None:
    library.create_skill(sid, {"SKILL.md": body or f"# {sid}\n\nDo {sid} well.\n"})


def _playbook_files(pid: str = "brief-kit") -> dict[str, str]:
    out = {}
    for path in BUNDLED.rglob("*"):
        if path.is_file():
            out[str(path.relative_to(BUNDLED))] = path.read_text()
    out["playbook.toml"] = out["playbook.toml"].replace('id = "research-brief"', f'id = "{pid}"', 1)
    return out


def _seed() -> None:
    for sid in ROLE_SKILLS:
        _skill(sid)
    _skill("tdd")
    store.create_playbook(_playbook_files())


def _export(*items: tuple[str, str]) -> bytes:
    _, data = service.export([{"kind": k, "id": i} for k, i in items])
    return data


def _keys(preview: dict) -> list[str]:
    return [i["key"] for i in preview["items"] if i["status"] != "refused"]


def _state_files() -> list[str]:
    root = state.root()
    return sorted(os.listdir(root)) if root.exists() else []


def _library_snapshot() -> tuple:
    skill_root = gallery_storage.skills_root()
    playbook_root = store.local_root()
    return (
        sorted(os.listdir(skill_root)) if skill_root.exists() else [],
        sorted(os.listdir(playbook_root)) if playbook_root.exists() else [],
    )


def _skill_dirs() -> list[str]:
    return sorted(n for n in os.listdir(gallery_storage.skills_root()) if not n.startswith("."))


def _playbook_role_skills(pid: str) -> list[str]:
    detail = store.get_playbook(pid)
    return [
        s
        for f in detail["flows"]
        for st in f["steps"]
        for s in st.get("role", {}).get("skills", [])
    ]


# ---- hostile bundles --------------------------------------------------------------------------


def _member(name: bytes, data: bytes, *, typeflag: bytes = b"0", linkname: bytes = b"") -> bytes:
    block = bytearray(512)
    block[0 : len(name)] = name
    block[100:108] = b"0000644\0"
    block[108:116] = b"0000000\0"
    block[116:124] = b"0000000\0"
    block[124:136] = f"{len(data):011o}".encode() + b"\0"
    block[136:148] = b"00000000000\0"
    block[148:156] = b" " * 8
    block[156:157] = typeflag
    block[157 : 157 + len(linkname)] = linkname
    block[257:263] = b"ustar\0"
    block[263:265] = b"00"
    block[148:156] = f"{sum(block):06o}".encode() + b"\0 "
    return bytes(block) + data + b"\0" * ((-len(data)) % 512)


def _archive(
    members: list[tuple[bytes, bytes]], *, manifest: dict | None, extra: bytes = b""
) -> bytes:
    out = b""
    if manifest is not None:
        out += _member(b"manifest.json", json.dumps(manifest).encode())
    for name, data in members:
        out += _member(name, data)
    return out + extra + b"\0" * 1024


def _manifest(items: list[dict], **over) -> dict:
    return {
        "format": 1,
        "created_at": "2026-10-27T00:00:00Z",
        "app_version": "test",
        "items": items,
        **over,
    }


def _skill_item(sid: str, files: dict[str, bytes], **over) -> dict:
    return {
        "kind": "skill",
        "id": sid,
        "title": sid,
        "revision": fmt.tree_digest(files),
        "digest": fmt.item_digest("skill", sid, files),
        "depends_on": [],
        "files": [
            {"path": p, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
            for p, d in sorted(files.items())
        ],
        **over,
    }


def _valid(sid: str = "tdd", files: dict[str, bytes] | None = None) -> tuple[dict, list]:
    files = files or {"SKILL.md": b"# tdd\n"}
    item = _skill_item(sid, files)
    return item, [(f"skill/{sid}/{p}".encode(), d) for p, d in sorted(files.items())]


def _with_name(name: str, data: bytes = b"# x\n") -> bytes:
    """A bundle whose single declared file has the raw path `name` (manifest and member agree)."""
    path = name.split("/", 2)[2]
    item = {
        "kind": "skill",
        "id": "tdd",
        "title": "tdd",
        "revision": "0" * 64,
        "digest": "0" * 64,
        "depends_on": [],
        "files": [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
    }
    return _archive([(name.encode("utf-8"), data)], manifest=_manifest([item]))


def _tar(add) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        item, members = _valid()
        raw = json.dumps(_manifest([item])).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        for name, data in members:
            info = tarfile.TarInfo(name.decode())
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        add(tar)
    return buf.getvalue()


def _special(kind: bytes, name: str = "skill/tdd/extra") -> bytes:
    def add(tar):
        info = tarfile.TarInfo(name)
        info.type = kind
        if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else "skill/tdd/SKILL.md"
        tar.addfile(info)

    return _tar(add)


def _pax() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        item, members = _valid()
        raw = json.dumps(_manifest([item])).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        info.pax_headers = {"comment": "x"}
        tar.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def _digest_mismatch() -> bytes:
    item, members = _valid()
    return _archive([(members[0][0], b"# changed\n")], manifest=_manifest([item]))


def _duplicate_item() -> bytes:
    item, members = _valid()
    return _archive(members, manifest=_manifest([item, item]))


def _duplicate_member() -> bytes:
    item, members = _valid()
    return _archive(members + members, manifest=_manifest([item]))


def _undeclared() -> bytes:
    item, members = _valid()
    return _archive(members + [(b"skill/tdd/notes.md", b"x")], manifest=_manifest([item]))


def _missing() -> bytes:
    item, members = _valid(files={"SKILL.md": b"# tdd\n", "notes.md": b"x"})
    return _archive(members[:1], manifest=_manifest([item]))


def _case_collision() -> bytes:
    files = {"SKILL.md": b"# tdd\n", "skill.md": b"x"}
    item, members = _valid(files=files)
    return _archive(members, manifest=_manifest([item]))


def _non_utf8() -> bytes:
    item, _ = _valid()
    return _archive([(b"skill/tdd/\xff.md", b"x")], manifest=_manifest([item]))


def _newer() -> bytes:
    item, members = _valid()
    return _archive(members, manifest=_manifest([item], format=2))


def _unknown_key() -> bytes:
    item, members = _valid()
    return _archive(members, manifest=_manifest([item], signed=True))


def _dangling_dep() -> bytes:
    item, members = _valid()
    item["depends_on"] = ["skill:missing"]
    return _archive(members, manifest=_manifest([item]))


def _cycle() -> bytes:
    a, ma = _valid("a-skill")
    b, mb = _valid("b-skill")
    a["depends_on"], b["depends_on"] = ["skill:b-skill"], ["skill:a-skill"]
    return _archive(ma + mb, manifest=_manifest([a, b]))


def _manifest_not_first() -> bytes:
    item, members = _valid()
    out = b"".join(_member(n, d) for n, d in members)
    return out + _member(b"manifest.json", json.dumps(_manifest([item])).encode()) + b"\0" * 1024


def _trailing_garbage() -> bytes:
    item, members = _valid()
    return _archive(members, manifest=_manifest([item])) + b"\x01" + b"\0" * 511


def _gnu_magic() -> bytes:
    data = bytearray(_archive(_valid()[1], manifest=_manifest([_valid()[0]])))
    data[257:265] = b"ustar  \0"
    data[148:156] = b" " * 8
    data[148:156] = f"{sum(data[:512]):06o}".encode() + b"\0 "
    return bytes(data)


HOSTILE = {
    "traversal": (lambda: _with_name("skill/tdd/../../.bashrc"), ". or .. segment"),
    "absolute": (
        lambda: _archive([(b"/etc/passwd", b"x")], manifest=_manifest([_valid()[0]])),
        "absolute",
    ),
    "symlink": (lambda: _special(tarfile.SYMTYPE), "symbolic link"),
    "hardlink": (lambda: _special(tarfile.LNKTYPE), "hard link"),
    "chardev": (lambda: _special(tarfile.CHRTYPE), "character device"),
    "blockdev": (lambda: _special(tarfile.BLKTYPE), "block device"),
    "fifo": (lambda: _special(tarfile.FIFOTYPE), "FIFO"),
    "directory": (lambda: _special(tarfile.DIRTYPE, "skill/tdd/sub"), "a directory"),
    "pax": (_pax, "PAX"),
    "gnu": (_gnu_magic, "ustar"),
    "digest": (_digest_mismatch, "does not match its digest"),
    "newer": (_newer, "format 2"),
    "unknown_key": (_unknown_key, "unknown keys"),
    "duplicate_item": (_duplicate_item, "twice"),
    "duplicate_member": (_duplicate_member, "repeats the member"),
    "undeclared": (_undeclared, "does not declare"),
    "missing": (_missing, "lacks"),
    "dot_slash": (lambda: _with_name("skill/tdd/./SKILL.md"), ". or .. segment"),
    "double_slash": (lambda: _with_name("skill/tdd//SKILL.md"), "empty path segment"),
    "trailing_slash": (lambda: _with_name("skill/tdd/SKILL.md/"), "ends with a slash"),
    "backslash": (lambda: _with_name("skill/tdd/a\\b.md"), "backslash"),
    "non_nfc": (
        lambda: _with_name("skill/tdd/" + unicodedata.normalize("NFD", "é.md")),
        "normal form C",
    ),
    "case_collision": (_case_collision, "differ only by case"),
    "non_utf8": (_non_utf8, "not valid UTF-8"),
    "control": (lambda: _with_name("skill/tdd/a\x01.md"), "control character"),
    "dangling_dep": (_dangling_dep, "not in the bundle"),
    "cycle": (_cycle, "cycle"),
    "manifest_not_first": (_manifest_not_first, "must come first"),
    "trailing_garbage": (_trailing_garbage, "after its end-of-archive"),
    "truncated": (
        lambda: _archive(_valid()[1], manifest=_manifest([_valid()[0]]))[:-1024],
        "truncated",
    ),
    "not_tar": (lambda: b"PK\x03\x04" + b"\0" * 508, "not a BattleLab bundle"),
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_hostile_bundles_are_refused_before_anything_is_written(install, case):
    build, words = HOSTILE[case]
    before = (_library_snapshot(), _state_files())
    with pytest.raises(fmt.Refused) as exc:
        service.preview(build())
    assert words in exc.value.detail
    assert (_library_snapshot(), _state_files()) == before


def test_bounds_refuse_oversized_archives_files_and_counts(install, monkeypatch):
    item, members = _valid()
    data = _archive(members, manifest=_manifest([item]))
    monkeypatch.setattr(fmt, "MAX_BUNDLE_BYTES", len(data) - 1)
    with pytest.raises(fmt.Refused, match="larger than"):
        fmt.read(data)
    monkeypatch.setattr(fmt, "MAX_BUNDLE_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(fmt, "MAX_FILE_BYTES", 3)
    with pytest.raises(fmt.Refused, match="larger than 3 bytes"):
        fmt.read(data)
    monkeypatch.setattr(fmt, "MAX_FILE_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(fmt, "MAX_MEMBERS", 1)
    with pytest.raises(fmt.Refused, match="more than 1 files"):
        fmt.read(data)


def test_a_newer_format_has_its_own_reason(install):
    with pytest.raises(fmt.Refused) as exc:
        fmt.read(_newer())
    assert exc.value.reason == "newer_format"
    assert "Update BattleLab" in exc.value.detail


# ---- round trips ------------------------------------------------------------------------------


def test_export_includes_the_dependency_closure_and_nothing_else(install):
    _seed()
    bundle = fmt.read(_export(("playbook", "brief-kit")))
    assert [i.key for i in bundle.items] == [
        "skill:research-with-citations",
        "skill:research-review",
        "playbook:brief-kit",
    ]
    assert bundle.item("playbook:brief-kit").depends_on == (
        "skill:research-with-citations",
        "skill:research-review",
    )


def test_round_trip_on_a_clean_install_gives_the_same_content(tmp_path, monkeypatch, install):
    _seed()
    source = {sid: skills.get(sid)["files"] for sid in ROLE_SKILLS}
    playbook = store.get_playbook("brief-kit")
    data = _export(("playbook", "brief-kit"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    assert {i["status"] for i in preview["items"]} == {"new"}
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}
    for sid in ROLE_SKILLS:
        assert skills.get(sid)["files"] == source[sid]
    copy = store.get_playbook("brief-kit")
    assert copy["revision"] == playbook["revision"]
    assert copy["source"] == "local"
    assert store.list_playbooks()["default"] is None  # inert: never made the default
    assert service.imports()["playbook:brief-kit"]["bundle"] == preview["bundle"]["digest"]


def test_conflicts_are_renamed_and_every_reference_is_rewritten(install):
    _seed()
    data = _export(("playbook", "brief-kit"))
    before = {sid: skills.get(sid)["revision"] for sid in ROLE_SKILLS}
    playbook_before = store.get_playbook("brief-kit")["revision"]
    preview = service.preview(data)
    plan = {i["key"]: i for i in preview["items"]}
    assert plan["skill:research-review"]["dest"] == "research-review-2"
    assert plan["skill:research-review"]["status"] == "renamed"
    assert plan["playbook:brief-kit"]["dest"] == "brief-kit-2"
    assert {u["to"] for u in plan["playbook:brief-kit"]["uses"]} == {
        "research-with-citations-2",
        "research-review-2",
    }
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}
    assert sorted(_playbook_role_skills("brief-kit-2")) == sorted(f"{s}-2" for s in ROLE_SKILLS)
    assert _playbook_role_skills("brief-kit") == list(ROLE_SKILLS)
    # Nothing existing was touched.
    assert {sid: skills.get(sid)["revision"] for sid in ROLE_SKILLS} == before
    assert store.get_playbook("brief-kit")["revision"] == playbook_before


# ---- dependencies -----------------------------------------------------------------------------


def test_deselecting_a_dependency_refuses_its_dependent_and_creates_nothing_dangling(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("playbook", "brief-kit"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    chosen = [k for k in _keys(preview) if k != "skill:research-review"]
    result = {i["key"]: i for i in service.confirm(preview["token"], chosen)["items"]}
    assert result["skill:research-review"]["status"] == "not_selected"
    assert result["playbook:brief-kit"]["status"] == "refused"
    assert (
        "needs skill research-review, which isn't being imported"
        in result["playbook:brief-kit"]["reason"]
    )
    assert result["skill:research-with-citations"]["status"] == "created"
    with pytest.raises(store.NotFound):
        store.get_playbook("brief-kit")


def test_a_refused_dependency_refuses_its_dependents_in_the_preview(tmp_path, monkeypatch, install):
    files = {"SKILL.md": b"   \n"}  # an empty SKILL.md is not a valid skill
    skill = _skill_item("research-review", files)
    good = {"SKILL.md": b"# ok\n"}
    other = _skill_item("research-with-citations", good)
    pfiles = {p: t.encode() for p, t in _playbook_files().items()}
    playbook = {
        **_skill_item("brief-kit", pfiles),
        "kind": "playbook",
        "digest": fmt.item_digest("playbook", "brief-kit", pfiles),
        "depends_on": ["skill:research-with-citations", "skill:research-review"],
    }
    members = (
        [(f"skill/research-review/{p}".encode(), d) for p, d in files.items()]
        + [(f"skill/research-with-citations/{p}".encode(), d) for p, d in good.items()]
        + [(f"playbook/brief-kit/{p}".encode(), d) for p, d in sorted(pfiles.items())]
    )
    preview = service.preview(_archive(members, manifest=_manifest([skill, other, playbook])))
    plan = {i["key"]: i for i in preview["items"]}
    assert plan["skill:research-review"]["status"] == "refused"
    assert plan["playbook:brief-kit"]["status"] == "refused"
    assert "which can't be imported" in plan["playbook:brief-kit"]["reason"]
    result = {
        i["key"]: i
        for i in service.confirm(preview["token"], [k["key"] for k in preview["items"]])["items"]
    }
    assert result["playbook:brief-kit"]["status"] == "refused"
    with pytest.raises(store.NotFound):
        store.get_playbook("brief-kit")


def test_declared_dependencies_must_match_the_content(tmp_path, monkeypatch, install):
    _seed()
    bundle = fmt.read(_export(("playbook", "brief-kit")))
    items = []
    for it in bundle.items:
        items.append(
            {
                "kind": it.kind,
                "id": it.id,
                "title": it.title,
                "files": it.files,
                "depends_on": [] if it.kind == "playbook" else list(it.depends_on),
            }
        )
    data = fmt.write(items, created_at="x", app_version="t")
    _install(tmp_path / "two", monkeypatch)
    plan = {i["key"]: i for i in service.preview(data)["items"]}
    assert plan["playbook:brief-kit"]["status"] == "refused"
    assert "don't match its content" in plan["playbook:brief-kit"]["reason"]


# ---- freshness --------------------------------------------------------------------------------


def test_a_local_change_after_the_preview_forces_a_repreview(install):
    _seed()
    data = _export(("skill", "tdd"))
    preview = service.preview(data)
    assert preview["items"][0]["dest"] == "tdd-2"
    _skill("tdd-2", "# someone else\n")  # the planned rename target is taken meanwhile
    before = _library_snapshot()
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], _keys(preview))
    assert exc.value.status == 409 and exc.value.extra["reason"] == "changed"
    assert _library_snapshot() == before
    assert skills.get("tdd-2")["files"]["SKILL.md"] == "# someone else\n"


def test_an_expired_preview_forces_a_repreview(install, monkeypatch):
    _seed()
    preview = service.preview(_export(("skill", "tdd")))
    now = service._now()
    monkeypatch.setattr(service, "_now", lambda: now + service.TTL_S + 1)
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], _keys(preview))
    assert exc.value.extra["reason"] == "expired"
    with pytest.raises(store.NotFound):
        skills.get("tdd-2")


# ---- order, partial failure, retry --------------------------------------------------------------


def test_dependencies_apply_first_and_a_failure_stops_only_its_dependents(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("skill", "tdd"), ("playbook", "brief-kit"))
    _install(tmp_path / "two", monkeypatch)
    calls = []
    real = kinds.publish
    fail = {"research-review"}

    def publish(kind, dest, files):
        calls.append((kind, dest))
        if dest in fail:
            raise store.Busy("the skill library was busy")
        return real(kind, dest, files)

    monkeypatch.setattr(kinds, "publish", publish)
    preview = service.preview(data)
    first = {i["key"]: i for i in service.confirm(preview["token"], _keys(preview))["items"]}
    assert [k for k, _ in calls] == ["skill", "skill", "skill"]  # the playbook never ran
    assert first["skill:tdd"]["status"] == "created"
    assert first["skill:research-with-citations"]["status"] == "created"
    assert first["skill:research-review"]["status"] == "failed"
    assert first["playbook:brief-kit"]["status"] == "skipped"
    assert "research-review" in first["playbook:brief-kit"]["reason"]
    assert ("playbook", "brief-kit") not in calls
    # Retry the same preview: completed copies are returned as-is, the failed item and its
    # dependent are attempted once more.
    fail.clear()
    calls.clear()
    second = {i["key"]: i for i in service.confirm(preview["token"], _keys(preview))["items"]}
    assert {v["status"] for v in second.values()} == {"created"}
    assert calls == [("skill", "research-review"), ("playbook", "brief-kit")]
    assert _skill_dirs() == sorted([*ROLE_SKILLS, "tdd"])


def test_a_lost_response_retry_returns_the_recorded_outcomes_and_makes_no_second_copy(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("playbook", "brief-kit"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    first = service.confirm(preview["token"], _keys(preview))
    now = service._now()
    monkeypatch.setattr(service, "_now", lambda: now + service.TTL_S + 3600)
    monkeypatch.setattr(kinds, "publish", lambda *a: pytest.fail("published twice"))
    again = service.confirm(preview["token"], _keys(preview))
    assert again["items"] == first["items"]
    assert again["repreview"] is None
    assert _skill_dirs() == sorted(ROLE_SKILLS)
    assert [p for p in os.listdir(store.local_root()) if not p.startswith(".")] == ["brief-kit"]
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], [preview["items"][0]["key"]])
    assert exc.value.extra["reason"] == "selection"


class _Crash(BaseException):
    pass


def test_a_crash_between_publication_and_record_reconciles_to_created(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("skill", "tdd"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    real = service._record_outcome
    monkeypatch.setattr(service, "_record_outcome", lambda *a, **k: (_ for _ in ()).throw(_Crash()))
    with pytest.raises(_Crash):
        service.confirm(preview["token"], _keys(preview))
    assert skills.get("tdd")  # published before the crash
    monkeypatch.setattr(service, "_record_outcome", real)
    monkeypatch.setattr(kinds, "publish", lambda *a: pytest.fail("published twice"))
    result = service.confirm(preview["token"], _keys(preview))
    assert result["items"][0]["status"] == "created"
    assert _skill_dirs() == ["tdd"]


def test_a_destination_holding_other_content_fails_without_being_overwritten(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("skill", "tdd"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)

    def other_then_crash(kind, dest, files):
        _skill(dest, "# not the bundle's\n")
        raise _Crash()

    monkeypatch.setattr(kinds, "publish", other_then_crash)
    with pytest.raises(_Crash):
        service.confirm(preview["token"], _keys(preview))
    monkeypatch.setattr(kinds, "publish", lambda *a: pytest.fail("overwrote the destination"))
    result = service.confirm(preview["token"], _keys(preview))
    assert result["items"][0]["status"] == "failed"
    assert "nothing was overwritten" in result["items"][0]["reason"]
    assert skills.get("tdd")["files"]["SKILL.md"] == "# not the bundle's\n"


def test_the_journal_holds_ids_and_digests_only(tmp_path, monkeypatch, install):
    _seed()
    data = _export(("skill", "tdd"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    service.confirm(preview["token"], _keys(preview))
    raw = (state.root() / f"journal-{preview['token']}.json").read_bytes()
    assert b"Do tdd well" not in raw
    st = os.stat(state.root() / f"journal-{preview['token']}.json")
    assert st.st_mode & 0o777 == 0o600
    assert os.stat(state.root()).st_mode & 0o777 == 0o700


def test_export_refuses_secret_values_and_unknown_items(install):
    _seed()
    with pytest.raises(service.ShareError):
        service.export([{"kind": "variable", "id": "token"}])
    with pytest.raises(service.ShareError):
        service.export([{"kind": "skill", "id": "nope"}])


# ---- routes -----------------------------------------------------------------------------------


def test_routes_are_authenticated_csrf_guarded_bounded_and_no_store(auth_cfg, install, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    _seed()
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    for path in ("/api/share/catalog", "/api/share/imports"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    assert (
        client.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        ).status_code
        == 303
    )
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    body = {"items": [{"kind": "playbook", "id": "brief-kit"}]}
    assert client.post("/api/share/export", json=body).status_code == 403
    assert (
        client.post(
            "/api/share/export", json=body, headers={**headers, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    secret = client.post(
        "/api/share/export", json={**body, "include_secrets": True}, headers=headers
    )
    assert secret.status_code == 422
    assert (
        client.post(
            "/api/share/export", json={**body, "passphrase": "x"}, headers=headers
        ).status_code
        == 422
    )
    catalog = client.get("/api/share/catalog").json()
    row = next(i for i in catalog["items"] if (i["kind"], i["id"]) == ("playbook", "brief-kit"))
    assert sorted(row["depends_on"]) == sorted(f"skill:{s}" for s in ROLE_SKILLS)
    exported = client.post("/api/share/export", json=body, headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.headers["cache-control"] == "no-store"
    assert exported.headers["content-disposition"].endswith('.battlelab"')
    assert client.post("/api/share/import/preview", content=exported.content).status_code == 403
    monkeypatch.setattr(fmt, "MAX_BUNDLE_BYTES", len(exported.content) - 1)
    big = client.post("/api/share/import/preview", content=exported.content, headers=headers)
    assert big.status_code == 413
    monkeypatch.setattr(fmt, "MAX_BUNDLE_BYTES", 32 * 1024 * 1024)
    bad = client.post("/api/share/import/preview", content=_newer(), headers=headers)
    assert bad.status_code == 422 and bad.json()["reason"] == "newer_format"
    preview = client.post("/api/share/import/preview", content=exported.content, headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.headers["cache-control"] == "no-store"
    token = preview.json()["token"]
    keys = [i["key"] for i in preview.json()["items"]]
    confirm = {"token": token, "selection": keys}
    assert client.post("/api/share/import", json=confirm).status_code == 403
    assert (
        client.post(
            "/api/share/import", json={**confirm, "passphrase": "x"}, headers=headers
        ).status_code
        == 422
    )
    done = client.post("/api/share/import", json=confirm, headers=headers)
    assert done.status_code == 200, done.text
    assert {i["status"] for i in done.json()["items"]} == {"created"}
    assert client.get("/api/share/imports").json()["imports"]
    gone = client.post(
        "/api/share/import", json={"token": "0" * 32, "selection": ["skill:x"]}, headers=headers
    )
    assert gone.status_code == 404 and gone.json()["reason"] == "missing"


# ---- review round 1 (independent review of #1738) -----------------------------------------------


def test_a_retry_never_applies_a_dependent_whose_imported_dependency_was_removed(
    tmp_path, monkeypatch, install
):
    import shutil

    _seed()
    data = _export(("playbook", "brief-kit"))
    preview = service.preview(data)  # same install: every item is renamed to <id>-2
    real = kinds.publish

    def publish(kind, dest, files):
        if kind == "playbook":
            raise OSError(5, "I/O error", "/home/someone/secret/path")
        return real(kind, dest, files)

    monkeypatch.setattr(kinds, "publish", publish)
    first = {i["key"]: i for i in service.confirm(preview["token"], _keys(preview))["items"]}
    assert first["skill:research-review"]["status"] == "created"
    assert first["playbook:brief-kit"]["status"] == "failed"
    # The response names the failure, never a host path.
    assert "/home/someone" not in first["playbook:brief-kit"]["reason"]
    assert "EIO" in first["playbook:brief-kit"]["reason"]
    # The operator removes the imported skill copy, and something else takes its ID.
    shutil.rmtree(gallery_storage.skills_root() / "research-review-2")
    _skill("research-review-2", "# foreign content\n")
    monkeypatch.setattr(kinds, "publish", real)
    second = service.confirm(preview["token"], _keys(preview))
    result = {i["key"]: i for i in second["items"]}
    assert result["playbook:brief-kit"]["status"] == "stale"
    assert "research-review-2" in result["playbook:brief-kit"]["reason"]
    assert second["repreview"] == "changed"
    with pytest.raises(store.NotFound):
        store.get_playbook("brief-kit-2")


def test_a_rename_never_takes_a_skill_id_a_bundled_playbook_names(tmp_path, monkeypatch, install):
    pfiles = {p: t.encode() for p, t in _playbook_files().items()}
    flow = "flows/research.toml"
    pfiles[flow] = pfiles[flow].replace(b'"research-review"', b'"research-review-2"')
    data = fmt.write(
        [
            {
                "kind": "skill",
                "id": "research-review",
                "title": "r",
                "files": {"SKILL.md": b"# r\n"},
            },
            {"kind": "playbook", "id": "brief-kit", "title": "b", "files": pfiles},
        ],
        created_at="x",
        app_version="t",
    )
    _skill("research-review")  # local: forces the bundled skill to be renamed
    plan = {i["key"]: i for i in service.preview(data)["items"]}
    assert plan["skill:research-review"]["dest"] == "research-review-3"
    assert plan["playbook:brief-kit"]["status"] == "new"


def test_an_unwritable_imported_mark_never_fails_or_misreports_the_import(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("skill", "tdd"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    state.write_bytes("imported.json", b"{not json")
    result = service.confirm(preview["token"], _keys(preview))
    assert result["items"][0]["status"] == "created"
    assert _skill_dirs() == ["tdd"]
    again = service.confirm(preview["token"], _keys(preview))
    assert again["items"] == result["items"] and _skill_dirs() == ["tdd"]


def test_one_unreadable_journal_never_blocks_previews(install, monkeypatch):
    _seed()
    data = _export(("skill", "tdd"))
    old = service.preview(data)["token"]
    state.write_bytes(f"journal-{old}.json", b"{broken")
    now = service._now()
    monkeypatch.setattr(service, "_now", lambda: now + service.TTL_S * 3)
    os.utime(state.root() / f"preview-{old}.json", (now - 3600, now - 3600))
    assert service.preview(data)["token"] != old


def test_a_playbook_that_cannot_be_rewritten_is_refused_not_a_500(install, monkeypatch):
    _seed()
    data = _export(("playbook", "brief-kit"))

    def boom(*a, **k):
        raise service.kinds.tomlw.TomlWriteError("a float is not supported")

    monkeypatch.setattr(kinds.tomlw, "dumps", boom)
    plan = {i["key"]: i for i in service.preview(data)["items"]}
    assert plan["playbook:brief-kit"]["status"] == "refused"
    assert "can't be renamed" in plan["playbook:brief-kit"]["reason"]


def test_a_storage_failure_never_claims_nothing_changed(auth_cfg, install, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}

    def fail(*a):
        raise state.StateError("journal unwritable")

    monkeypatch.setattr(service, "confirm", fail)
    response = client.post(
        "/api/share/import", json={"token": "a" * 32, "selection": []}, headers=headers
    )
    assert response.status_code == 503
    assert "nothing was changed" not in response.json()["detail"].lower()
    assert "never makes a second copy" in response.json()["detail"]


# ---- review round 2 (Hermes on #1738): staged bytes are bounded --------------------------------


def _staged() -> list[str]:
    return sorted(n for n in os.listdir(state.root()) if n.startswith("bundle-"))


def test_identical_uploads_share_one_staged_copy(tmp_path, monkeypatch, install):
    _seed()
    data = _export(("skill", "tdd"))
    _install(tmp_path / "two", monkeypatch)
    first = service.preview(data)
    second = service.preview(data)
    # The same file against the same library is the same plan: one token, one record, one copy.
    assert first["token"] == second["token"]
    assert len(_staged()) == 1
    assert len([n for n in os.listdir(state.root()) if n.startswith("preview-")]) == 1
    result = service.confirm(second["token"], _keys(second))
    assert result["items"][0]["status"] == "created"


def test_a_new_bundle_evicts_unconfirmed_staging_and_the_evicted_preview_asks_again(
    tmp_path, monkeypatch, install
):
    _seed()
    a = _export(("skill", "tdd"))
    b = _export(("skill", "research-review"))
    _install(tmp_path / "two", monkeypatch)
    monkeypatch.setattr(service, "MAX_STAGED_BUNDLES", 1)
    pa = service.preview(a)
    pb = service.preview(b)
    assert _staged() == [f"bundle-{pb['bundle']['digest']}.battlelab"]
    with pytest.raises(service.ShareError) as exc:
        service.confirm(pa["token"], _keys(pa))
    assert exc.value.status == 409 and exc.value.extra["reason"] == "evicted"
    # Refused before a journal existed: the evicted preview pins no record slot.
    assert not (state.root() / f"journal-{pa['token']}.json").exists()
    assert service.confirm(pb["token"], _keys(pb))["items"][0]["status"] == "created"
    with pytest.raises(store.NotFound):
        skills.get("tdd")


def test_staging_a_confirmed_import_still_needs_is_never_evicted_the_new_upload_is_refused(
    tmp_path, monkeypatch, install
):
    _seed()
    a = _export(("skill", "tdd"))
    b = _export(("skill", "research-review"))
    _install(tmp_path / "two", monkeypatch)
    monkeypatch.setattr(service, "MAX_STAGED_BUNDLES", 1)
    pa = service.preview(a)
    real = kinds.publish
    monkeypatch.setattr(kinds, "publish", lambda *a: (_ for _ in ()).throw(store.Busy("busy")))
    assert service.confirm(pa["token"], _keys(pa))["items"][0]["status"] == "failed"
    with pytest.raises(service.ShareError) as exc:
        service.preview(b)
    assert exc.value.status == 503 and exc.value.extra["reason"] == "staging_full"
    monkeypatch.setattr(kinds, "publish", real)
    assert service.confirm(pa["token"], _keys(pa))["items"][0]["status"] == "created"


def test_staged_bytes_are_removed_one_ttl_after_their_latest_preview(
    tmp_path, monkeypatch, install
):
    _seed()
    a = _export(("skill", "tdd"))
    b = _export(("skill", "research-review"))
    _install(tmp_path / "two", monkeypatch)
    service.preview(a)
    now = service._now()
    old = now - service.TTL_S - 120
    for name in _staged():
        os.utime(state.root() / name, (old, old))
    pb = service.preview(b)
    assert _staged() == [f"bundle-{pb['bundle']['digest']}.battlelab"]


# ---- review round 3: Hermes 7218 + independent review of 01a22aa3 ------------------------------


def _distinct(n: int) -> list[bytes]:
    out = []
    for i in range(n):
        sid = f"skill-{i}"
        files = {"SKILL.md": f"# {sid}\n".encode()}
        out.append(
            fmt.write(
                [{"kind": "skill", "id": sid, "title": sid, "files": files}],
                created_at="x",
                app_version="t",
            )
        )
    return out


def test_a_settled_import_never_pins_staging(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_STAGED_BUNDLES", 1)
    a, b = _distinct(2)
    pa = service.preview(a)
    assert service.confirm(pa["token"], _keys(pa))["items"][0]["status"] == "created"
    pb = service.preview(b)  # A is settled: its staged copy gives way
    assert service.confirm(pb["token"], _keys(pb))["items"][0]["status"] == "created"


def test_preview_records_are_bounded_and_a_needed_record_is_never_dropped(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 2)
    bundles = _distinct(4)
    tokens = [service.preview(data)["token"] for data in bundles[:3]]
    records = sorted(n for n in os.listdir(state.root()) if n.startswith("preview-"))
    assert len(records) == 2 and f"preview-{tokens[0]}.json" not in records
    # Two imports with a pending retry hold both records: a new preview is refused, nothing dropped.
    monkeypatch.setattr(kinds, "publish", lambda *a: (_ for _ in ()).throw(store.Busy("busy")))
    for token in tokens[1:]:
        assert (
            service.confirm(token, [f"skill:skill-{tokens.index(token)}"])["items"][0]["status"]
            == "failed"
        )
    before = sorted(os.listdir(state.root()))
    with pytest.raises(service.ShareError) as exc:
        service.preview(bundles[3])
    assert exc.value.extra["reason"] == "staging_full"
    assert sorted(os.listdir(state.root())) == before


def test_an_empty_selection_is_refused(install):
    _seed()
    preview = service.preview(_export(("skill", "tdd")))
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], [])
    assert exc.value.status == 422
    assert not (state.root() / f"journal-{preview['token']}.json").exists()


def test_eviction_happens_only_when_the_new_bundle_then_fits(install, monkeypatch):
    a, b, c = _distinct(3)
    pa = service.preview(a)
    pb = service.preview(b)
    monkeypatch.setattr(kinds, "publish", lambda *x: (_ for _ in ()).throw(store.Busy("busy")))
    service.confirm(pb["token"], _keys(pb))  # B is pending: pinned
    sizes = {n: os.stat(state.root() / n).st_size for n in _staged()}
    monkeypatch.setattr(
        service, "MAX_STAGED_BYTES", max(sizes.values()) + 10
    )  # C can't fit beside B
    staged = _staged()
    with pytest.raises(service.ShareError):
        service.preview(c)
    assert _staged() == staged  # A was not evicted for nothing
    assert pa["token"]


def test_an_unreadable_skill_makes_export_refuse_or_travel_as_a_reference(install):
    _seed()
    (gallery_storage.skills_root() / "research-review" / "extra.md").write_bytes(b"\xff\xfe")
    with pytest.raises(service.ShareError) as exc:
        service.export([{"kind": "skill", "id": "research-review"}])
    assert exc.value.status == 422
    bundle = fmt.read(_export(("playbook", "brief-kit")))
    assert [i.key for i in bundle.items] == ["skill:research-with-citations", "playbook:brief-kit"]
    row = next(i for i in kinds.catalog() if i["id"] == "brief-kit")
    assert row["depends_on"] == ["skill:research-with-citations"]


def test_a_retry_never_adopts_a_copy_another_import_made(tmp_path, monkeypatch, install):
    _seed()
    data = _export(("playbook", "brief-kit"))
    _install(tmp_path / "two", monkeypatch)
    t1 = service.preview(data)
    real = kinds.publish
    monkeypatch.setattr(kinds, "publish", lambda *a: (_ for _ in ()).throw(store.Busy("busy")))
    first = {i["key"]: i["status"] for i in service.confirm(t1["token"], _keys(t1))["items"]}
    assert first["playbook:brief-kit"] == "skipped"
    monkeypatch.setattr(kinds, "publish", real)
    t2 = service.preview(data)
    assert t2["token"] != t1["token"]
    skills_only = [k for k in _keys(t2) if k.startswith("skill:")]
    assert {
        i["status"]
        for i in service.confirm(t2["token"], skills_only)["items"]
        if i["key"] in skills_only
    } == {"created"}
    retry = {i["key"]: i for i in service.confirm(t1["token"], _keys(t1))["items"]}
    assert retry["skill:research-review"]["status"] == "failed"
    assert "was taken" in retry["skill:research-review"]["reason"]
    assert retry["playbook:brief-kit"]["status"] == "skipped"
    with pytest.raises(store.NotFound):
        store.get_playbook("brief-kit")


def test_an_unreadable_destination_after_a_failure_is_left_for_reconciliation(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("skill", "tdd"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    real_at, real_publish, calls = kinds.at, kinds.publish, []

    def at(kind, dest):
        calls.append(dest)
        if len(calls) == 2:  # the lookup after the failed publish
            raise store.Busy("busy")
        return real_at(kind, dest)

    monkeypatch.setattr(kinds, "at", at)
    monkeypatch.setattr(kinds, "publish", lambda *a: (_ for _ in ()).throw(store.Busy("busy")))
    first = service.confirm(preview["token"], _keys(preview))["items"][0]
    assert first["status"] == "failed" and "taken" not in first["reason"]
    journal = state.read_json(f"journal-{preview['token']}.json")
    assert "outcome" not in journal["entries"]["skill:tdd"]
    monkeypatch.setattr(kinds, "at", real_at)
    monkeypatch.setattr(kinds, "publish", real_publish)
    assert service.confirm(preview["token"], _keys(preview))["items"][0]["status"] == "created"


def test_share_refusals_carry_the_apps_security_headers(auth_cfg, install):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    refused = client.post("/api/share/import/preview", content=b"not a bundle", headers=headers)
    assert refused.status_code == 422
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert refused.headers["cache-control"] == "no-store"


# ---- review round 4: independent re-review of 30fa5ad8 -----------------------------------------


def test_a_lone_surrogate_in_a_bundle_text_is_refused_not_a_500(install):
    files = {"SKILL.md": b"# x\n"}
    item = _skill_item("tdd", files, title="bad \ud800 title")
    raw = json.dumps(_manifest([item])).encode()  # json escapes it as \ud800
    data = (
        _member(b"manifest.json", raw)
        + _member(b"skill/tdd/SKILL.md", files["SKILL.md"])
        + b"\0" * 1024
    )
    before = (_library_snapshot(), _state_files())
    with pytest.raises(fmt.Refused, match="not valid Unicode"):
        service.preview(data)
    assert (_library_snapshot(), _state_files()) == before
    _seed()
    with pytest.raises(service.ShareError, match="source label"):
        service.export([{"kind": "skill", "id": "tdd"}], "src \ud800")


def test_an_open_intent_of_our_own_is_settled_as_created_never_taken(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("skill", "tdd"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    real_at, real_publish, calls = kinds.at, kinds.publish, []

    def publish_then_fail(kind, dest, files):
        real_publish(kind, dest, files)
        raise OSError(5, "I/O error")

    def at(kind, dest):
        calls.append(dest)
        if len(calls) in (2, 3):  # after the failed publish, and in the next reconcile
            raise store.Busy("busy")
        return real_at(kind, dest)

    monkeypatch.setattr(kinds, "at", at)
    monkeypatch.setattr(kinds, "publish", publish_then_fail)
    assert service.confirm(preview["token"], _keys(preview))["items"][0]["status"] == "failed"
    monkeypatch.setattr(kinds, "publish", lambda *a: pytest.fail("published twice"))
    second = service.confirm(preview["token"], _keys(preview))["items"][0]
    assert second["status"] == "created", second
    assert "skill:tdd" in service.imports()


def test_a_dependent_of_a_permanently_failed_dependency_pins_nothing(
    tmp_path, monkeypatch, install
):
    _seed()
    data = _export(("playbook", "brief-kit"))
    _install(tmp_path / "two", monkeypatch)
    preview = service.preview(data)
    _skill("research-review", "# someone else's\n")  # taken after the preview: permanent failure
    monkeypatch.setattr(service, "_fresh", lambda record, journal: None)
    result = {i["key"]: i for i in service.confirm(preview["token"], _keys(preview))["items"]}
    assert result["skill:research-review"]["status"] == "failed"
    assert result["playbook:brief-kit"]["status"] == "skipped"
    row = next(r for r in service._inventory(service._now()) if r["token"] == preview["token"])
    assert row["pending"] is False and row["pins_bytes"] is False


def test_an_expired_record_with_an_open_intent_is_never_dropped(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 1)
    _seed()
    preview = service.preview(_export(("skill", "tdd")))
    journal = {
        "format": 1,
        "token": preview["token"],
        "bundle": preview["bundle"]["digest"],
        "selection": ["skill:tdd"],
        "created_at": 0,
        "entries": {"skill:tdd": {"dest": "tdd-2", "digest": "0" * 64, "intent_at": 0}},
    }
    state.write_json(f"journal-{preview['token']}.json", journal)
    now = service._now()
    monkeypatch.setattr(service, "_now", lambda: now + service.TTL_S + 60)
    with pytest.raises(service.ShareError):
        service.preview(_export(("skill", "research-review")))
    assert (state.root() / f"preview-{preview['token']}.json").exists()


# ---- review round 5: independent re-check of 964707de ------------------------------------------


def _pending_import(monkeypatch, data: bytes) -> dict:
    preview = service.preview(data)
    with monkeypatch.context() as m:
        m.setattr(kinds, "publish", lambda *a: (_ for _ in ()).throw(store.Busy("busy")))
        assert service.confirm(preview["token"], _keys(preview))["items"][0]["status"] == "failed"
    return preview


def test_an_unreadable_journal_keeps_its_pending_import(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 2)
    a, b, c = _distinct(3)
    pa = _pending_import(monkeypatch, a)
    pb_token = service.preview(b)["token"]
    os.chmod(state.root() / f"journal-{pa['token']}.json", 0o640)
    service.preview(c)  # room is made from B; A can't be read and may hold a retry: kept
    assert (state.root() / f"preview-{pa['token']}.json").exists()
    assert (state.root() / f"journal-{pa['token']}.json").exists()
    assert not (state.root() / f"preview-{pb_token}.json").exists()


@pytest.mark.parametrize("entries", [[], {"skill:skill-0": []}, "x"])
def test_an_odd_shaped_journal_never_breaks_previews(install, monkeypatch, entries):
    a, b = _distinct(2)
    pa = _pending_import(monkeypatch, a)
    journal = state.read_json(f"journal-{pa['token']}.json")
    journal["entries"] = entries
    state.write_json(f"journal-{pa['token']}.json", journal)
    assert service.preview(b)["token"]
    assert (state.root() / f"preview-{pa['token']}.json").exists()


def test_an_expired_open_intent_with_nothing_written_settles_as_failed(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 1)
    a, b = _distinct(2)
    pa = service.preview(a)

    class _Crash(BaseException):
        pass

    with monkeypatch.context() as m:
        m.setattr(kinds, "publish", lambda *x: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            service.confirm(pa["token"], _keys(pa))  # intent written, crash before publish
    now = service._now()
    monkeypatch.setattr(service, "_now", lambda: now + service.TTL_S + 120)
    result = service.confirm(pa["token"], _keys(pa))["items"][0]
    assert result["status"] == "failed" and "expired" in result["reason"]
    assert service.preview(b)["token"]  # the settled record no longer pins the only slot


# ---- review round 6 (Hermes 7240): a settled import's replay survives eviction -----------------


def test_a_settled_import_replays_its_result_after_its_record_gives_way(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 1)
    a, b = _distinct(2)
    pa = service.preview(a)
    first = service.confirm(pa["token"], _keys(pa))
    assert first["items"][0]["status"] == "created"
    service.preview(b)  # A's record gives way at the quota boundary
    assert not (state.root() / f"preview-{pa['token']}.json").exists()
    monkeypatch.setattr(kinds, "publish", lambda *x: pytest.fail("published twice"))
    assert service.confirm(pa["token"], _keys(pa)) == first  # the lost response, replayed
    receipt = state.read_json(f"receipt-{pa['token']}.json")
    receipt["selection"] = ["skill:skill-0", "skill:skill-1"]  # confirmed with another selection
    receipt["keys"] = ["skill:skill-0", "skill:skill-1"]
    state.write_json(f"receipt-{pa['token']}.json", receipt)
    with pytest.raises(service.ShareError) as exc:
        service.confirm(pa["token"], _keys(pa))
    assert exc.value.extra["reason"] == "selection"


def test_full_receipts_keep_settled_records_and_refuse_new_previews(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 1)
    monkeypatch.setattr(service, "MAX_RECEIPTS", 0)
    a, b = _distinct(2)
    pa = service.preview(a)
    service.confirm(pa["token"], _keys(pa))
    with pytest.raises(service.ShareError) as exc:
        service.preview(b)
    assert exc.value.extra["reason"] == "staging_full"
    assert service.confirm(pa["token"], _keys(pa))["items"][0]["status"] == "created"


# ---- review round 7: a receipt answers exactly what the live record did ------------------------


def test_a_receipt_replays_the_expired_answer_not_a_raw_pending_status(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 1)
    a, b = _distinct(2)
    pa = service.preview(a)
    with monkeypatch.context() as m:
        m.setattr(kinds, "publish", lambda *x: (_ for _ in ()).throw(OSError(5, "I/O error")))
        assert service.confirm(pa["token"], _keys(pa))["items"][0]["status"] == "failed"
    now = service._now()
    monkeypatch.setattr(service, "_now", lambda: now + service.TTL_S + 120)
    live = service.confirm(pa["token"], _keys(pa))
    assert live["repreview"] == "expired" and live["items"][0]["status"] == "stale"
    service.preview(b)  # A's record gives way, leaving its receipt
    assert not (state.root() / f"preview-{pa['token']}.json").exists()
    assert service.confirm(pa["token"], _keys(pa)) == live


@pytest.mark.parametrize(
    "receipt", [{"format": 1}, {"selection": "x", "result": {}}, {"result": []}]
)
def test_a_damaged_receipt_answers_gone_not_a_500(install, receipt):
    token = "b" * 32
    state.write_json(f"receipt-{token}.json", receipt)
    with pytest.raises(service.ShareError) as exc:
        service.confirm(token, ["skill:x"])
    assert exc.value.status == 404


# ---- review round 8: a damaged settled record is kept, never a crash ---------------------------


def _settled(monkeypatch, data: bytes) -> dict:
    preview = service.preview(data)
    assert service.confirm(preview["token"], _keys(preview))["items"][0]["status"] == "created"
    return preview


@pytest.mark.parametrize("damage", ["title", "selection", "record"])
def test_a_damaged_settled_record_is_kept_and_never_crashes_a_preview(install, monkeypatch, damage):
    monkeypatch.setattr(service, "MAX_RECORDS", 2)
    a, b, c = _distinct(3)
    pa = _settled(monkeypatch, a)
    name, jname = f"preview-{pa['token']}.json", f"journal-{pa['token']}.json"
    if damage == "title":
        record = state.read_json(name)
        del record["items"][0]["title"]
        state.write_json(name, record)
    elif damage == "selection":
        journal = state.read_json(jname)
        del journal["selection"]
        state.write_json(jname, journal)
        now = service._now()
        monkeypatch.setattr(service, "_now", lambda: now + service.TTL_S + 120)
    else:
        state.write_json(name, {})
    pb = _settled(monkeypatch, b)["token"]  # settled too, and newer than A
    service.preview(c)  # at capacity: room is made from B, never from the damaged (older) A
    assert (state.root() / name).exists()
    assert not (state.root() / f"preview-{pb}.json").exists()


def test_a_receipt_replay_refuses_unknown_keys_like_a_live_confirm(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 1)
    a, b = _distinct(2)
    pa = _settled(monkeypatch, a)
    service.preview(b)
    with pytest.raises(service.ShareError) as exc:
        service.confirm(pa["token"], ["skill:nope"])
    assert exc.value.status == 422


def test_an_unreadable_receipt_is_a_503_never_preview_again(install, monkeypatch):
    # An unreadable receipt may be read again: answering "gone" would send the operator to
    # re-preview and import the created items a second time.
    monkeypatch.setattr(service, "MAX_RECORDS", 1)
    a, b = _distinct(2)
    pa = _settled(monkeypatch, a)
    service.preview(b)
    os.chmod(state.root() / f"receipt-{pa['token']}.json", 0o644)
    with pytest.raises(state.StateError):
        service.confirm(pa["token"], _keys(pa))


# ---- review round 9: damaged state is a 503 and kept, never a 500 or a drop --------------------


def test_a_settled_record_without_expires_at_is_kept_not_dropped(install, monkeypatch):
    monkeypatch.setattr(service, "MAX_RECORDS", 2)
    a, b, c = _distinct(3)
    pa = _settled(monkeypatch, a)
    record = state.read_json(f"preview-{pa['token']}.json")
    del record["expires_at"]
    state.write_json(f"preview-{pa['token']}.json", record)
    _settled(monkeypatch, b)
    service.preview(c)
    assert (state.root() / f"preview-{pa['token']}.json").exists()
    with pytest.raises(state.StateError):
        service.confirm(pa["token"], _keys(pa))


@pytest.mark.parametrize("keys", [[["x"]], None, "skill:x"])
def test_a_receipt_with_damaged_keys_answers_gone(install, keys):
    token = "d" * 32
    receipt = {"format": 1, "selection": ["skill:x"], "keys": keys, "result": {"items": []}}
    state.write_json(f"receipt-{token}.json", receipt)
    with pytest.raises(service.ShareError) as exc:
        service.confirm(token, ["skill:x"])
    assert exc.value.status == 404


@pytest.mark.parametrize("damage", ["journal-empty", "record-empty", "no-entries"])
def test_confirm_on_damaged_live_state_is_a_503_not_a_500(install, monkeypatch, damage):
    (a,) = _distinct(1)
    preview = service.preview(a)
    token = preview["token"]
    if damage == "record-empty":
        state.write_json(f"preview-{token}.json", {})
    else:
        with monkeypatch.context() as m:
            m.setattr(kinds, "publish", lambda *x: (_ for _ in ()).throw(store.Busy("busy")))
            service.confirm(token, _keys(preview))
        journal = {} if damage == "journal-empty" else {**state.read_json(f"journal-{token}.json")}
        journal.pop("entries", None)
        state.write_json(f"journal-{token}.json", journal)
    with pytest.raises(state.StateError):
        service.confirm(token, _keys(preview))


# ---- review round 10: the whole damaged-state class is a 503, never a 500 or a foreign write ----


DAMAGE = [
    "journal-no-token",
    "failed-no-reason",
    "refused-no-reason",
    "intent-no-digest",
    "entry-not-in-plan",
    "depends-on-missing",
    "bindings-not-pairs",
    "foreign-token",
    "expires-nan",
]


@pytest.mark.parametrize("damage", DAMAGE)
def test_hand_damaged_share_state_is_a_503_and_never_writes_another_import(
    install, monkeypatch, damage
):
    (a,) = _distinct(1)
    preview = service.preview(a)
    token = preview["token"]
    with monkeypatch.context() as m:
        m.setattr(kinds, "publish", lambda *x: (_ for _ in ()).throw(store.Busy("busy")))
        service.confirm(token, _keys(preview))  # leaves a retriable failure
    rname, jname = f"preview-{token}.json", f"journal-{token}.json"
    record, journal = state.read_json(rname), state.read_json(jname)
    key = "skill:skill-0"
    other = "e" * 32
    if damage == "journal-no-token":
        del journal["token"]
    elif damage == "failed-no-reason":
        journal["entries"][key] = {"dest": "skill-0", "outcome": "failed", "retry": False}
    elif damage == "refused-no-reason":
        record["items"][0]["status"] = "refused"
        record["items"][0].pop("reason", None)
    elif damage == "intent-no-digest":
        journal["entries"][key] = {"dest": "skill-0", "intent_at": 0}
    elif damage == "entry-not-in-plan":
        journal["entries"]["skill:zzz"] = {"dest": "zzz", "digest": "0" * 64, "intent_at": 0}
    elif damage == "depends-on-missing":
        record["items"][0]["depends_on"] = ["skill:missing"]
    elif damage == "bindings-not-pairs":
        record["bindings"] = {"skill": [1], "playbook": []}
    elif damage == "foreign-token":
        journal["token"] = other
    elif damage == "expires-nan":
        record["expires_at"] = float("nan")
    state.write_json(rname, record)
    state.write_json(jname, journal)
    with pytest.raises(state.StateError):
        service.confirm(token, _keys(preview))
    assert not (state.root() / f"journal-{other}.json").exists()
    assert not (state.root() / f"preview-{other}.json").exists()
