"""Playbook gallery + versioned local authoring (#1191 PR 1, #1192's agreed rules).

Pins: every write goes through the P1 validator; writes are descriptor-relative, staged, read back
and published by one rename; a revision is the content digest and a stale edit / delete /
set-default is a 409; `bundled` / `catalog` are read-only (403); duplicate remaps `after`,
`rework.to`, `distinct_from` (and step-output references) to fresh ids and never touches the
default; deleting a playbook projects run is refused listing them; saving leaves deployments
pinned; the routes bind nothing from the request but the path id; bodies are strict and bounded.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import fileedit
from agent_sessions.main import create_app
from agent_sessions.playbooks import deployments, loader, schema, store, tomlw
from agent_sessions.playbooks.tree import Tree, read_tree
from agent_sessions.routes import playbooks as routes

FIXTURES = Path(__file__).parent / "fixtures" / "playbooks"


@pytest.fixture(autouse=True)
def _bundled(monkeypatch):
    """The P1 fixtures play the `bundled` source; the local root is conftest's per-test tmp dir."""
    assert fileedit.install_lease_signal_handler()
    monkeypatch.setattr(loader, "BUNDLED_ROOT", FIXTURES)
    previous = deployments.set_registry(deployments._StoredDeployments())
    yield
    deployments.set_registry(previous)


def _files(fixture: str = "forge-workflow", new_id: str | None = None) -> dict:
    tree = read_tree(FIXTURES / fixture)
    out = {p: data.decode("utf-8") for p, data in tree.files.items()}
    if new_id is not None:
        out["playbook.toml"] = out["playbook.toml"].replace(
            f'id = "{fixture}"', f'id = "{new_id}"', 1
        )
    return out


def _root() -> Path:
    return store.local_root()


def _local_names() -> list[str]:
    return sorted(p.name for p in _root().iterdir()) if _root().exists() else []


class FakeRegistry:
    def __init__(self, running: dict[str, list[dict]]) -> None:
        self.running = running

    def projects_running(self, playbook_id: str) -> list[dict]:
        return [dict(r) for r in self.running.get(playbook_id, [])]


# ---- create / read -------------------------------------------------------------------------------


def test_create_writes_a_validated_owner_only_tree_and_lists_it_as_local(tmp_home):
    d = store.create_playbook(_files(new_id="my-flow"))
    assert d["id"] == "my-flow" and d["source"] == "local" and d["editable"] and d["ok"]
    on_disk = read_tree(_root() / "my-flow")
    assert d["revision"] == on_disk.digest()
    assert on_disk.files == {p: v.encode() for p, v in _files(new_id="my-flow").items()}
    for dirpath, _dirnames, filenames in os.walk(_root() / "my-flow"):
        assert os.stat(dirpath).st_mode & 0o777 == 0o700
        for f in filenames:
            assert os.stat(os.path.join(dirpath, f)).st_mode & 0o777 == 0o600
    cards = {c["id"]: c for c in store.list_playbooks()["playbooks"]}
    assert cards["my-flow"]["source"] == "local" and cards["forge-workflow"]["source"] == "bundled"
    assert cards["forge-workflow"]["editable"] is False
    assert _local_names() == [".lock", "my-flow"]  # no staging leftovers


def test_detail_carries_files_documents_readme_and_requires(tmp_home):
    d = store.get_playbook("forge-workflow")
    assert d["files"]["playbook.toml"].startswith("#")
    assert d["documents"]["flows/dev.toml"]["steps"][0]["id"] == "plan"
    assert d["readme"] and d["requires_present"] == {
        "binaries": {"git": d["requires_present"]["binaries"]["git"]}
    }
    assert d["flows"][0]["steps"][2]["actor"]["kind"] == "external"


@pytest.mark.parametrize("taken", ["forge-workflow", "local-dup", "empty-dir", "symlink"])
def test_create_never_replaces_an_existing_entry(tmp_home, taken, tmp_path):
    if taken == "local-dup":
        store.create_playbook(_files(new_id="local-dup"))
        before = read_tree(_root() / "local-dup").digest()
    elif taken in ("empty-dir", "symlink"):
        _root().mkdir(parents=True)
        if taken == "empty-dir":
            (_root() / taken).mkdir()
        else:
            target = tmp_path / "elsewhere"
            target.mkdir()
            (_root() / taken).symlink_to(target)
    with pytest.raises(store.Conflict, match="already exists"):
        store.create_playbook(_files(new_id=taken))
    if taken == "local-dup":
        assert read_tree(_root() / "local-dup").digest() == before
    if taken == "empty-dir":
        assert list((_root() / taken).iterdir()) == []
    if taken == "symlink":
        assert list((tmp_path / "elsewhere").iterdir()) == []
    assert not any(n.startswith(".staging-") for n in _local_names())


def test_the_noreplace_publish_is_what_refuses_a_name_claimed_after_the_check(
    tmp_home, monkeypatch
):
    # Remove the pre-check: publication itself must still refuse an entry that appeared.
    monkeypatch.setattr(store, "_ensure_free", lambda *a: None)
    _root().mkdir(parents=True)
    (_root() / "raced").mkdir()
    with pytest.raises(store.Conflict, match="already exists"):
        store.create_playbook(_files(new_id="raced"))
    assert list((_root() / "raced").iterdir()) == []


def test_an_invalid_bundle_is_refused_by_the_validator_and_nothing_is_written(tmp_home):
    files = _files(new_id="bad-one")
    files["template/CLAUDE.md"] += "\n{{forge_token}}\n"  # a secret rendered into a material
    with pytest.raises(store.StoreError) as e:
        store.create_playbook(files)
    assert e.value.status == 422 and "secret" in e.value.detail
    assert "bad-one" not in _local_names()
    assert not any(n.startswith(".staging-") for n in _local_names())


@pytest.mark.parametrize(
    "path",
    ["../x", "a//b", "/abs", ".git/config", "template/.GIT/x", "template/..", "a*b", "a\x01b"],
)
def test_a_file_path_is_refused_by_its_own_check_before_the_validator(tmp_home, path, monkeypatch):
    called = []
    monkeypatch.setattr(store, "_validate", lambda *a: called.append(a))
    with pytest.raises(store.StoreError) as e:
        store.tree_from_files({path: "a"})
    assert e.value.status == 422
    assert "not allowed" in e.value.detail or ".git" in e.value.detail
    assert called == []


@pytest.mark.parametrize(
    "files",
    [
        {},
        [],
        {"../x": "a"},
        {"a//b": "a"},
        {"/abs": "a"},
        {".git/config": "a"},
        {"template/.GIT/x": "a"},
        {"a*b": "a"},
        {"x": 5},
        {"x": None},
        {"x": ["a"]},
        {"x": {"base64": "!!notb64"}},
        {"x": {"base64": "aGk=", "extra": 1}},
        {"README.md": {"toml": {"a": 1}}},
        {"flows/a.toml": {"toml": {"x": 1.5}}},
        {"flows/a.toml": {"toml": {"x": None}}},
        {"a": "x", "a/b": "y"},
        {"x": "\ud800"},
        {"/".join(["d"] * 12) + "/f": "a"},
    ],
)
def test_the_files_map_is_parsed_strictly(tmp_home, files):
    with pytest.raises(store.StoreError) as e:
        store.create_playbook(files)
    assert e.value.status == 422
    assert not _root().exists() or not [n for n in _local_names() if not n.startswith(".")]


def test_a_file_may_be_sent_as_base64_or_as_a_toml_document(tmp_home):
    files = _files(new_id="spellings")
    files["template/docs/mission.md"] = {"base64": "IyBNaXNzaW9uCg=="}  # "# Mission\n"
    files["flows/dev.toml"] = {"toml": tomllib_loads(files["flows/dev.toml"])}
    d = store.create_playbook(files)
    on_disk = read_tree(_root() / "spellings")
    assert on_disk.files["template/docs/mission.md"] == b"# Mission\n"
    assert d["documents"]["flows/dev.toml"] == tomllib_loads(_files()["flows/dev.toml"])


def tomllib_loads(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


def test_only_directories_implied_by_files_are_created(tmp_home):
    """#1248 note 3: a stray empty directory is never mirrored — the client cannot even say one."""
    d = store.create_playbook(_files(new_id="dirs"))
    on_disk = read_tree(_root() / "dirs")
    implied = {
        "/".join(p.split("/")[:i]) for p in on_disk.files for i in range(1, p.count("/") + 1)
    }
    assert on_disk.dirs == implied and d["ok"]


# ---- edit ----------------------------------------------------------------------------------------


def test_edit_is_fenced_by_the_revision_the_operator_loaded(tmp_home):
    d = store.create_playbook(_files(new_id="edit-me"))
    files = d["files"]
    files["README.md"] = "second"
    u = store.update_playbook("edit-me", d["revision"], files)
    assert u["revision"] != d["revision"]
    assert (_root() / "edit-me" / "README.md").read_text() == "second"
    files["README.md"] = "third"
    with pytest.raises(store.Conflict) as e:
        store.update_playbook("edit-me", d["revision"], files)  # stale
    assert (
        e.value.extra["revision"] == u["revision"] and e.value.extra["current"]["id"] == "edit-me"
    )
    assert (_root() / "edit-me" / "README.md").read_text() == "second"
    assert [n for n in _local_names() if n.startswith(".")] == [".lock", ".recovery"]
    # The save RETAINED the tree it displaced (displace, never drop).
    [kept] = store.recovery_entries()
    assert kept["playbook_id"] == "edit-me"
    assert read_tree(_root() / ".recovery" / kept["name"]).digest() == d["revision"]


def test_a_hand_edit_on_disk_moves_the_revision_too(tmp_home):
    d = store.create_playbook(_files(new_id="hand"))
    (_root() / "hand" / "README.md").write_text("edited in vim")
    files = d["files"]
    files["README.md"] = "from the editor"
    with pytest.raises(store.Conflict):
        store.update_playbook("hand", d["revision"], files)
    assert (_root() / "hand" / "README.md").read_text() == "edited in vim"


def test_an_edit_cannot_rename_the_playbook(tmp_home):
    d = store.create_playbook(_files(new_id="keep-id"))
    with pytest.raises(store.StoreError) as e:
        store.update_playbook("keep-id", d["revision"], _files(new_id="other-id"))
    assert e.value.status == 422 and "must equal" in e.value.detail


@pytest.mark.parametrize("op", ["edit", "delete"])
def test_bundled_and_catalog_playbooks_are_read_only(tmp_home, monkeypatch, tmp_path, op):
    cat = tmp_path / "catalog"
    cat.mkdir()
    import shutil

    shutil.copytree(FIXTURES / "inbox-triage", cat / "inbox-triage")
    monkeypatch.setattr(store, "catalog_root", lambda: cat)
    cards = {(c["id"], c["source"]): c for c in store.list_playbooks()["playbooks"]}
    assert cards[("inbox-triage", "bundled")]["ok"]  # bundled owns the id first
    assert "already used by a bundled" in cards[("inbox-triage", "catalog")]["error"]
    assert store.get_playbook("inbox-triage")["source"] == "bundled"
    monkeypatch.setattr(loader, "BUNDLED_ROOT", tmp_path / "no-bundled")
    cards = {c["id"]: c for c in store.list_playbooks()["playbooks"]}
    assert cards["inbox-triage"]["source"] == "catalog"
    for pid, fixture in (("inbox-triage", "inbox-triage"),):
        rev = cards[pid]["revision"]
        with pytest.raises(store.ReadOnly):
            if op == "edit":
                store.update_playbook(pid, rev, _files(fixture))
            else:
                store.delete_playbook(pid, rev)
    monkeypatch.setattr(loader, "BUNDLED_ROOT", FIXTURES)
    rev = store.get_playbook("forge-workflow")["revision"]
    with pytest.raises(store.ReadOnly):
        if op == "edit":
            store.update_playbook("forge-workflow", rev, {"junk": "not even a bundle"})
        else:
            store.delete_playbook("forge-workflow", rev)
    assert read_tree(FIXTURES / "forge-workflow").digest() == rev


def test_an_edit_that_changes_nothing_keeps_the_revision(tmp_home):
    d = store.create_playbook(_files(new_id="same"))
    assert store.update_playbook("same", d["revision"], d["files"])["revision"] == d["revision"]


def test_saving_leaves_deployed_projects_pinned(tmp_home):
    d = store.create_playbook(_files(new_id="deployed"))
    pinned = [{"project_id": "p-aaaa0001", "name": "Alpha", "revision": d["revision"]}]
    reg = FakeRegistry({"deployed": pinned})
    deployments.set_registry(reg)
    files = d["files"]
    files["README.md"] = "v2"
    u = store.update_playbook("deployed", d["revision"], files)  # a save is never refused
    assert reg.running["deployed"] == pinned  # nothing about the deployment moved
    assert u["revision"] != pinned[0]["revision"]  # → the project sees "update available"


def test_an_exchange_leaves_the_old_tree_as_a_leftover_the_next_write_sweeps(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="crashy"))
    old = read_tree(_root() / "crashy").digest()
    with monkeypatch.context() as m:  # NOT monkeypatch.undo(): that would undo conftest's env pins
        m.setattr(store, "_retain", lambda *a: None)  # "crash" before the old tree was retained
        files = d["files"]
        files["README.md"] = "new"
        store.update_playbook("crashy", d["revision"], files)
    leftovers = [n for n in _local_names() if n.startswith(".staging-")]
    assert len(leftovers) == 1 and read_tree(_root() / leftovers[0]).digest() == old
    assert (_root() / "crashy" / "README.md").read_text() == "new"
    assert "crashy" in {c["id"] for c in store.list_playbooks()["playbooks"]}
    store.create_playbook(_files(new_id="next-write"))
    assert "next-write" in _local_names()
    assert not any(n.startswith(".staging-") for n in _local_names())
    # …and the next write RETAINED the leftover (it may be an old tree), never deleted it.
    assert any(
        read_tree(_root() / ".recovery" / r["name"]).digest() == old
        for r in store.recovery_entries()
    )


def test_the_staged_tree_is_read_back_before_publication(tmp_home, monkeypatch):
    real = store.read_tree_at

    def tampered(fd, name):
        t = real(fd, name)
        if name.startswith(".staging-"):
            t.files["README.md"] = b"something else"
        return t

    monkeypatch.setattr(store, "read_tree_at", tampered)
    with pytest.raises(store.Conflict, match="changed while it was being written"):
        store.create_playbook(_files(new_id="verified"))
    assert "verified" not in _local_names()
    assert not any(n.startswith(".staging-") for n in _local_names())


def test_a_symlinked_local_root_is_refused(tmp_home, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    _root().parent.mkdir(parents=True, exist_ok=True)
    _root().symlink_to(target)
    with pytest.raises(store.StoreError) as e:
        store.create_playbook(_files(new_id="nope"))
    assert e.value.status == 409
    assert list(target.iterdir()) == []


# ---- delete / default ----------------------------------------------------------------------------


def test_delete_is_fenced_clears_the_default_and_leaves_nothing(tmp_home):
    d = store.create_playbook(_files(new_id="gone"))
    store.set_default("gone", d["revision"], None)
    assert store.list_playbooks()["default"] == "gone"
    with pytest.raises(store.Conflict):
        store.delete_playbook("gone", "0" * 64)
    store.delete_playbook("gone", d["revision"])
    listing = store.list_playbooks()
    assert listing["default"] is None and "gone" not in {c["id"] for c in listing["playbooks"]}
    assert json.loads((_root() / ".state.json").read_text())["default"] is None
    assert [n for n in _local_names() if not n.startswith(".retained-state-")] == [
        ".lock",
        ".recovery",
        ".state.json",
    ]
    [kept] = store.recovery_entries()  # a deleted playbook is retained, not removed
    assert read_tree(_root() / ".recovery" / kept["name"]).digest() == d["revision"]


def test_deleting_a_playbook_projects_still_run_is_refused_listing_them(tmp_home):
    d = store.create_playbook(_files(new_id="in-use"))
    deployments.set_registry(
        FakeRegistry(
            {"in-use": [{"project_id": "p-bbbb0002", "name": "Beta"}, {"project_id": "p-aaaa0001"}]}
        )
    )
    with pytest.raises(store.InUse) as e:
        store.delete_playbook("in-use", d["revision"])
    assert e.value.status == 409
    assert e.value.extra["projects"] == [
        {"project_id": "p-aaaa0001", "name": "p-aaaa0001"},
        {"project_id": "p-bbbb0002", "name": "Beta"},
    ]
    assert "in-use" in _local_names()


@pytest.mark.parametrize("answer", ["raises", "not-a-list", "bad-row"])
def test_a_registry_that_cannot_answer_refuses_the_delete(tmp_home, answer):
    d = store.create_playbook(_files(new_id="unknown"))

    class Broken:
        def projects_running(self, pid):
            if answer == "raises":
                raise OSError("records unreadable")
            return {"x": 1} if answer == "not-a-list" else [{"name": "no id"}]

    deployments.set_registry(Broken())
    with pytest.raises(store.StoreError) as e:
        store.delete_playbook("unknown", d["revision"])
    assert e.value.status == 503 and "nothing was deleted" in e.value.detail
    assert "unknown" in _local_names()


def test_the_registry_is_asked_inside_the_exclusive_lock(tmp_home):
    d = store.create_playbook(_files(new_id="locked"))
    seen = {}

    class Probe:
        def projects_running(self, pid):
            try:
                with store.root_lock(exclusive=False, wait=0):
                    seen["shared"] = "acquired"
            except store.Busy:
                seen["shared"] = "busy"
            return []

    deployments.set_registry(Probe())
    store.delete_playbook("locked", d["revision"])
    assert seen == {"shared": "busy"}


def test_set_default_is_fenced_by_revision_and_by_the_default_seen(tmp_home):
    a = store.create_playbook(_files(new_id="first"))
    b = store.create_playbook(_files(new_id="second"))
    with pytest.raises(store.Conflict):
        store.set_default("first", "0" * 64, None)  # stale revision
    assert store.set_default("first", a["revision"], None) == {
        "default": "first",
        "state_durable": True,
    }
    with pytest.raises(store.Conflict) as e:
        store.set_default("second", b["revision"], None)  # the operator saw "no default"
    assert e.value.extra["default"] == "first"
    store.set_default("second", b["revision"], "first")
    assert store.list_playbooks()["default"] == "second"
    # A bundled playbook may be the default too: choosing it does not mutate it.
    fw = store.get_playbook("forge-workflow")
    store.set_default("forge-workflow", fw["revision"], "second")
    with pytest.raises(store.Conflict):
        store.clear_default("second")
    assert store.clear_default("forge-workflow") == {"default": None, "state_durable": True}


# ---- duplicate -----------------------------------------------------------------------------------


def _steps(d: dict) -> list[dict]:
    doc = d["documents"][f"flows/{d['default_flow']}.toml"]
    return doc["steps"]


def test_duplicate_gets_fresh_ids_remaps_every_reference_and_keeps_the_default(tmp_home):
    fw = store.get_playbook("forge-workflow")
    store.set_default("forge-workflow", fw["revision"], None)
    dup = store.duplicate_playbook("forge-workflow", {"revision": fw["revision"]})
    assert dup["source"] == "local" and dup["id"] != "forge-workflow" and dup["ok"]
    assert dup["name"] == "Forge workflow (copy)"
    assert store.list_playbooks()["default"] == "forge-workflow"  # never changed
    assert dup["default_flow"] != fw["default_flow"]
    assert f"flows/{fw['default_flow']}.toml" not in dup["files"]
    old, new = _steps(fw), _steps(dup)
    mapping = {o["id"]: n["id"] for o, n in zip(old, new, strict=True)}
    assert not set(mapping) & set(mapping.values())  # every step id is fresh
    for o, n in zip(old, new, strict=True):
        assert n.get("after", []) == [mapping[a] for a in o.get("after", [])]
        if "rework" in o:
            assert n["rework"]["to"] == mapping[o["rework"]["to"]]
            assert n["rework"]["when"] == o["rework"]["when"]
        assert [d["step"] for d in n.get("distinct_from", [])] == [
            mapping[d["step"]] for d in o.get("distinct_from", [])
        ]
        for oi, ni in zip(o.get("checklist", []), n.get("checklist", []), strict=True):
            for k, v in oi.get("probe_args", {}).items():
                nv = ni["probe_args"][k]
                if v.startswith("{{steps."):
                    sid = v.split(".")[1]
                    assert nv == v.replace(f"steps.{sid}.", f"steps.{mapping[sid]}.")
                else:
                    assert nv == v
    # Each remapped kind was actually exercised by the fixture.
    assert any(o.get("rework") for o in old) and any(o.get("distinct_from") for o in old)
    # Everything else is byte for byte.
    for path, text in fw["files"].items():
        if path != "playbook.toml" and not path.startswith("flows/"):
            assert dup["files"][path] == text


def test_duplicate_takes_an_id_and_a_name_and_refuses_a_stale_revision(tmp_home):
    dup = store.duplicate_playbook("inbox-triage", {"id": "my-inbox", "name": "Mine"})
    assert dup["id"] == "my-inbox" and dup["name"] == "Mine"
    with pytest.raises(store.Conflict):
        store.duplicate_playbook("inbox-triage", {"id": "my-inbox"})  # taken
    with pytest.raises(store.Conflict):
        store.duplicate_playbook("my-inbox", {"revision": "0" * 64})
    with pytest.raises(store.StoreError):
        store.duplicate_playbook("my-inbox", {"parent": "x"})
    with pytest.raises(store.StoreError):
        store.duplicate_playbook("my-inbox", {"name": ["x"]})


def test_a_local_duplicate_of_a_duplicate_stays_valid(tmp_home):
    a = store.duplicate_playbook("forge-workflow", {})
    b = store.duplicate_playbook(a["id"], {"revision": a["revision"]})
    assert b["ok"] and len(b["id"]) <= 48 and b["id"] != a["id"]


# ---- listing is fail-soft ------------------------------------------------------------------------


def test_one_broken_local_bundle_disables_only_itself(tmp_home):
    store.create_playbook(_files(new_id="good-one"))
    (_root() / "broken").mkdir()
    (_root() / "broken" / "playbook.toml").write_text("format = [")
    linked = _root() / "linked"
    linked.mkdir()
    (linked / "playbook.toml").symlink_to(_root() / "good-one" / "playbook.toml")
    (_root() / "Not_An_Id").mkdir()
    shadow = store.duplicate_playbook("inbox-triage", {"id": "shadow-src"})
    os.rename(_root() / "shadow-src", _root() / "inbox-triage")  # a local copy of a bundled id
    assert shadow["ok"]
    cards = {(c["id"], c["source"]): c for c in store.list_playbooks()["playbooks"]}
    assert cards[("good-one", "local")]["ok"]
    assert not cards[("broken", "local")]["ok"] and "parse" in cards[("broken", "local")]["error"]
    assert "symbolic link" in cards[("linked", "local")]["error"]
    assert not cards[("Not_An_Id", "local")]["ok"]
    shadowed = cards[("inbox-triage", "local")]
    assert not shadowed["ok"] and "already used by a bundled" in shadowed["error"]
    assert not shadowed["editable"]
    assert store.get_playbook("inbox-triage")["source"] == "bundled"
    # Dot-named store internals are never cards.
    assert not any(pid.startswith(".") for pid, _ in cards)


# ---- the lock ------------------------------------------------------------------------------------


def test_a_write_waits_a_bounded_time_then_is_busy(tmp_home, monkeypatch):
    monkeypatch.setattr(store, "LOCK_WAIT_S", 0.1)
    held, release = threading.Event(), threading.Event()

    def holder():
        with store.root_lock(exclusive=True):
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(10)
    try:
        with pytest.raises(store.Busy):
            store.create_playbook(_files(new_id="waits"))
    finally:
        release.set()
        t.join(10)
    store.create_playbook(_files(new_id="waits"))


def test_a_cancelled_request_never_strands_the_lock(tmp_home, monkeypatch):
    inside, release = threading.Event(), threading.Event()
    real = store._stage

    def slow(fd, tree, *a):
        inside.set()
        assert release.wait(10)
        return real(fd, tree, *a)

    monkeypatch.setattr(store, "_stage", slow)

    async def scenario():
        task = asyncio.create_task(asyncio.to_thread(store.create_playbook, _files(new_id="cxl")))
        assert await asyncio.to_thread(inside.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(scenario())
    # The worker thread finished its write and released the lock in its own `finally`.
    with store.root_lock(exclusive=True, wait=5):
        pass
    assert "cxl" in _local_names()


def test_the_lock_file_is_not_inherited(tmp_home):
    with store.root_lock(exclusive=True) as root_fd:
        fds = []
        for n in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{n}").endswith("/.lock"):
                    fds.append(int(n))
            except OSError:
                pass
        assert fds and all(not os.get_inheritable(fd) for fd in fds)
        assert not os.get_inheritable(root_fd)


# ---- TOML writer and digest ----------------------------------------------------------------------


def test_every_fixture_document_round_trips_through_the_writer():
    import tomllib

    for bundle in ("forge-workflow", "inbox-triage"):
        tree = read_tree(FIXTURES / bundle)
        for path, data in tree.files.items():
            if path.endswith(".toml"):
                doc = tomllib.loads(data.decode())
                assert tomllib.loads(tomlw.dumps(doc)) == doc


@pytest.mark.parametrize(
    "doc",
    [
        {"x": 1.5},
        {"x": None},
        {"x": 2**64},
        {"x": "\ud800"},
        {"x": __import__("datetime").date(2026, 1, 1)},
        [],
    ],
)
def test_the_writer_refuses_what_it_cannot_write_exactly(doc):
    with pytest.raises(tomlw.TomlWriteError):
        tomlw.dumps(doc)


def test_the_writer_is_type_strict_and_escapes_everything():
    import tomllib

    doc = {"a b": 'q"\\\n\t\x01\x7f', "t": True, "n": 1, "l": [True, 1, "x", {"k": [1]}], "e": {}}
    assert tomllib.loads(tomlw.dumps(doc)) == doc
    assert not tomlw.strict_equal({"a": True}, {"a": 1})
    deep: dict = {}
    cur = deep
    for _ in range(200):
        cur["x"] = {}
        cur = cur["x"]
    with pytest.raises(tomlw.TomlWriteError):
        tomlw.dumps(deep)


def test_the_round_trip_proof_refuses_a_writer_that_emits_something_else(monkeypatch):
    real = tomlw._string
    monkeypatch.setattr(tomlw, "_string", lambda v: real(v[:-1]) if len(v) > 1 else real(v))
    with pytest.raises(tomlw.TomlWriteError, match="without changing it"):
        tomlw.dumps({"a": "abc"})


def test_the_digest_moves_with_any_byte_directory_or_name():
    base = Tree(files={"a/b": b"1"}, dirs={"a"})
    variants = [
        Tree(files={"a/b": b"2"}, dirs={"a"}),
        Tree(files={"a/b": b"1"}, dirs={"a", "c"}),
        Tree(files={"a/c": b"1"}, dirs={"a"}),
        Tree(files={"a/b": b"1", "a/c": b""}, dirs={"a"}),
    ]
    digests = {base.digest(), *(v.digest() for v in variants)}
    assert len(digests) == 5
    assert Tree(files={"a/b": b"1"}, dirs={"a"}).digest() == base.digest()


# ---- routes --------------------------------------------------------------------------------------


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg) -> dict:
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def test_no_playbook_route_exposes_a_parameter_as_request_input(auth_cfg):
    from fastapi.dependencies.utils import get_flat_dependant

    app = create_app(auth_cfg)
    seen = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not (path == routes.PREFIX or path.startswith(routes.PREFIX + "/")):
            continue
        seen.append((sorted(route.methods), path))
        flat = get_flat_dependant(route.dependant)
        exposed = [
            p.name
            for p in (
                *flat.query_params,
                *flat.body_params,
                *flat.header_params,
                *flat.cookie_params,
            )
        ]
        assert exposed == [], (path, exposed)
        assert {p.name for p in flat.path_params} <= {"pid", "name"}, path
    assert len(seen) == 10, seen  # authoring + review/confirm; recovery has no purge endpoint


def test_the_routes_need_a_session_csrf_and_the_origin_and_are_never_cached(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    r = c.get(routes.PREFIX)
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store"
    hdr = _login(c, auth_cfg)
    body = {"files": _files(new_id="routed")}
    assert c.post(routes.PREFIX, json=body).status_code == 403  # no CSRF token
    assert (
        c.post(
            routes.PREFIX, json=body, headers={**hdr, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert "routed" not in _local_names()
    r = c.post(routes.PREFIX, json=body, headers=hdr)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    rev = r.json()["revision"]
    for resp in (
        c.get(routes.PREFIX),
        c.get(routes.PREFIX + "/routed"),
        c.get(routes.PREFIX + "/nope-nope"),
        c.put(
            routes.PREFIX + "/routed",
            json={"revision": "0" * 64, "files": body["files"]},
            headers=hdr,
        ),
    ):
        assert resp.headers["cache-control"] == "no-store"
    for method, path, kwargs in (
        ("put", "/routed", {"json": {"revision": rev, "files": body["files"]}}),
        ("delete", f"/routed?revision={rev}", {}),
        ("post", "/routed/duplicate", {"json": {}}),
        ("put", "/routed/default", {"json": {"revision": rev, "expect_default": None}}),
        ("delete", "/routed/default", {}),
    ):
        r = getattr(c, method)(routes.PREFIX + path, **kwargs)  # no CSRF header
        assert r.status_code == 403, (method, path)
    assert read_tree(_root() / "routed").digest() == rev


def test_the_route_lifecycle_and_its_status_codes(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    files = _files(new_id="life")
    r = c.post(routes.PREFIX, json={"files": files}, headers=hdr)
    assert r.status_code == 201
    rev = r.json()["revision"]
    assert c.post(routes.PREFIX, json={"files": files}, headers=hdr).status_code == 409
    files["README.md"] = "v2"
    r = c.put(routes.PREFIX + "/life", json={"revision": rev, "files": files}, headers=hdr)
    assert r.status_code == 200
    rev2 = r.json()["revision"]
    stale = c.put(routes.PREFIX + "/life", json={"revision": rev, "files": files}, headers=hdr)
    assert stale.status_code == 409 and stale.json()["revision"] == rev2
    fw = c.get(routes.PREFIX + "/forge-workflow").json()
    ro = c.put(
        routes.PREFIX + "/forge-workflow",
        json={"revision": fw["revision"], "files": _files()},
        headers=hdr,
    )
    assert ro.status_code == 403
    assert (
        c.delete(
            routes.PREFIX + f"/forge-workflow?revision={fw['revision']}", headers=hdr
        ).status_code
        == 403
    )
    dup = c.post(routes.PREFIX + "/forge-workflow/duplicate", json={}, headers=hdr)
    assert dup.status_code == 201 and dup.json()["source"] == "local"
    r = c.put(
        routes.PREFIX + "/life/default",
        json={"revision": rev2, "expect_default": None},
        headers=hdr,
    )
    assert r.status_code == 200 and c.get(routes.PREFIX).json()["default"] == "life"
    assert c.delete(routes.PREFIX + f"/life?revision={rev}", headers=hdr).status_code == 409
    gone = c.delete(routes.PREFIX + f"/life?revision={rev2}", headers=hdr).json()
    assert gone["deleted"] == "life" and gone["state_durable"] is True
    assert gone["retained"].startswith("life-")
    assert c.get(routes.PREFIX).json()["default"] is None
    assert c.get(routes.PREFIX + "/life").status_code == 404
    deployments.set_registry(FakeRegistry({dup.json()["id"]: [{"project_id": "p-aaaa0001"}]}))
    r = c.delete(
        routes.PREFIX + f"/{dup.json()['id']}?revision={dup.json()['revision']}", headers=hdr
    )
    assert r.status_code == 409 and r.json()["projects"] == [
        {"project_id": "p-aaaa0001", "name": "p-aaaa0001"}
    ]


@pytest.mark.parametrize(
    "path",
    ["/UPPER", "/a", "/%C2%B2abc", "/ab%2F..", "/" + "a" * 49],
)
def test_a_bad_id_is_refused_before_the_filesystem(auth_cfg, tmp_home, path, monkeypatch):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    touched = []
    monkeypatch.setattr(store, "_open_root", lambda **k: touched.append(k))
    r = c.get(routes.PREFIX + path)
    assert r.status_code in (404, 422), r.text
    r = c.delete(routes.PREFIX + path + "?revision=" + "0" * 64, headers=hdr)
    assert r.status_code in (404, 405, 422)
    assert touched == []


@pytest.mark.parametrize(
    "revision",
    ["", "0" * 63, "0" * 65, "G" * 64, "²" * 64, "0" * 63 + "\n"],
)
def test_a_revision_query_value_is_strict(auth_cfg, tmp_home, revision):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    d = store.create_playbook(_files(new_id="strict"))
    r = c.delete(routes.PREFIX + "/strict", params={"revision": revision}, headers=hdr)
    assert r.status_code == 422
    assert read_tree(_root() / "strict").digest() == d["revision"]


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]",
        b"[" * 100_000 + b"]" * 100_000,
        json.dumps({"files": {}, "extra": 1}).encode(),
        json.dumps({"files": []}).encode(),
        json.dumps({}).encode(),
    ],
)
def test_a_create_body_is_strict(auth_cfg, tmp_home, raw):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(routes.PREFIX, content=raw, headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    "body",
    [
        {"revision": ["x"], "files": {}},
        {"revision": 7, "files": {}},
        {"files": {}},
        {"revision": "0" * 64},
    ],
)
def test_a_save_body_is_type_checked_before_anything_else(auth_cfg, tmp_home, body):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    store.create_playbook(_files(new_id="typed"))
    r = c.put(routes.PREFIX + "/typed", json=body, headers=hdr)
    assert r.status_code == 422, r.text


def test_set_default_body_is_strict(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    d = store.create_playbook(_files(new_id="dflt"))
    for body in (
        {"revision": d["revision"]},  # expect_default is required, even when null
        {"revision": d["revision"], "expect_default": 5},
        {"revision": d["revision"], "expect_default": None, "force": True},
    ):
        r = c.put(routes.PREFIX + "/dflt/default", json=body, headers=hdr)
        assert r.status_code == 422, (body, r.text)
    assert store.list_playbooks()["default"] is None


def test_an_oversized_body_is_refused_while_it_streams(auth_cfg, tmp_home, monkeypatch):
    monkeypatch.setattr(routes, "BODY_MAX", 1024)
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        routes.PREFIX,
        content=b"x" * 2048,
        headers={**hdr, "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    sent = []

    def chunks():
        for _ in range(64):
            sent.append(1)
            yield b" " * 512

    r = c.post(routes.PREFIX, content=chunks(), headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_the_limits_the_routes_rely_on():
    assert routes.BODY_MAX >= 2 * schema.MAX_TOTAL_BYTES
    assert store.REVISION_RE.pattern == "[0-9a-f]{64}"


# ---- review of PR #1253: the write path ----------------------------------------------------------


@pytest.mark.parametrize("op", ["update", "delete"])
def test_an_out_of_band_edit_during_a_change_is_put_back_never_dropped(tmp_home, monkeypatch, op):
    """Review 1 (and Hermes on #1253): an edit landing on disk after the revision check and
    before publication was swapped out by the exchange (or renamed aside by a delete) and
    discarded unchecked.

    The store verifies the DISPLACED tree after the swap, which is what closes the check-to-
    exchange interval: an edit made before the exchange is inside the displaced tree and is
    detected here. On a mismatch the exchange is reversed — the operator's edit is back byte for
    byte — and the staged tree is removed only because it is still exactly what was staged (the
    two-edit case below covers the one that is not)."""
    d = store.create_playbook(_files(new_id="raced-edit"))
    live = _root() / "raced-edit" / "README.md"
    if op == "update":
        real = store._stage

        def stage_then_edit(root_fd, tree, *a):
            name = real(root_fd, tree, *a)
            live.write_text("the operator's hand edit\n")  # lands after the check, before the swap
            return name

        monkeypatch.setattr(store, "_stage", stage_then_edit)
        files = d["files"]
        files["README.md"] = "from the editor"
        with pytest.raises(store.Conflict) as e:
            store.update_playbook("raced-edit", d["revision"], files)
    else:
        real_find = store._local_for_write

        def check_then_edit(entries, pid, expect):
            out = real_find(entries, pid, expect)
            live.write_text("the operator's hand edit\n")
            return out

        monkeypatch.setattr(store, "_local_for_write", check_then_edit)
        with pytest.raises(store.Conflict) as e:
            store.delete_playbook("raced-edit", d["revision"])
    assert live.read_bytes() == b"the operator's hand edit\n"  # byte for byte
    after = read_tree(_root() / "raced-edit")
    before = {p: v.encode() for p, v in d["files"].items()} if op == "update" else None
    if before is not None:
        before["README.md"] = b"the operator's hand edit\n"
        assert after.files == before  # the whole tree is the edited one, not a mix
    assert e.value.extra["revision"] == after.digest()
    assert e.value.extra["revision"] != d["revision"]
    assert not any(n.startswith((".staging-", ".trash-")) for n in _local_names())


def test_a_tree_that_cannot_be_put_back_is_kept_and_named(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="kept"))
    live = _root() / "kept" / "README.md"
    real_find = store._local_for_write

    def check_then_edit(entries, pid, expect):
        out = real_find(entries, pid, expect)
        live.write_text("hand edit\n")
        return out

    monkeypatch.setattr(store, "_local_for_write", check_then_edit)
    real_rename = store.renameat.renameat2

    def rename(src_fd, src, dst_fd, dst, flags):
        if dst == "kept" and src.startswith(".trash-"):
            (_root() / "kept").mkdir()  # the name was claimed meanwhile
            return real_rename(src_fd, src, dst_fd, dst, flags)
        return real_rename(src_fd, src, dst_fd, dst, flags)

    monkeypatch.setattr(store.renameat, "renameat2", rename)
    with pytest.raises(store.Conflict, match=r"kept as recovery entry kept-") as e:
        store.delete_playbook("kept", d["revision"])
    [kept] = e.value.extra["kept"]
    assert (_root() / ".recovery" / kept / "README.md").read_text() == "hand edit\n"
    store.create_playbook(_files(new_id="sweeper"))  # a later write never removes it
    assert kept in {r["name"] for r in store.recovery_entries()}


@pytest.mark.parametrize("name", [".state.json", ".lock"])
def test_a_fifo_in_the_store_never_hangs_a_reader(tmp_home, name):
    """Review 2: a FIFO at `.state.json` blocked the open under the root lock."""
    store.create_playbook(_files(new_id="fifo"))
    target = _root() / name
    target.unlink(missing_ok=True)
    os.mkfifo(target)
    result: dict = {}

    def run():
        try:
            result["out"] = store.list_playbooks()
        except store.StoreError as e:
            result["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "a read hung on the FIFO"
    if name == ".state.json":
        assert result["out"]["default"] is None and "fifo" in {
            c["id"] for c in result["out"]["playbooks"]
        }
        with store.root_lock(exclusive=True, wait=1):  # released
            pass
    else:
        assert result["err"].status == 503


def test_every_store_open_of_an_existing_name_is_nonblocking():
    """The audit behind review 2, pinned: each `os.open` in the store either creates its name
    (`O_EXCL`), is a directory open, or is `O_NONBLOCK`."""
    import ast
    import inspect

    src = inspect.getsource(store)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.open":
            flags = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
            assert any(f in flags for f in ("O_EXCL", "O_NONBLOCK", "_DIR_FLAGS")), flags
    assert store._DIR_FLAGS & os.O_NONBLOCK


@pytest.mark.parametrize("raw", [{}, "x", {"revision": "0" * 64, "files": {}}])
@pytest.mark.parametrize("source", ["bundled", "catalog"])
def test_a_read_only_playbook_is_a_403_whatever_the_body(
    auth_cfg, tmp_home, monkeypatch, tmp_path, raw, source
):
    """Review 3: `{}` for a bundled playbook answered 422 — the body was parsed first."""
    if source == "catalog":
        import shutil

        cat = tmp_path / "catalog"
        cat.mkdir()
        shutil.copytree(FIXTURES / "forge-workflow", cat / "forge-workflow")
        monkeypatch.setattr(store, "catalog_root", lambda: cat)
        monkeypatch.setattr(loader, "BUNDLED_ROOT", tmp_path / "none")
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    content = json.dumps(raw).encode() if not isinstance(raw, str) else raw.encode()
    r = c.put(
        routes.PREFIX + "/forge-workflow",
        content=content,
        headers={**hdr, "Content-Type": "application/json"},
    )
    assert r.status_code == 403, r.text
    for q in ("", "?revision=x", "?revision=" + "0" * 64):
        assert c.delete(routes.PREFIX + "/forge-workflow" + q, headers=hdr).status_code == 403
    with pytest.raises(store.ReadOnly):
        store.update_playbook("forge-workflow", "x", {})
    with pytest.raises(store.ReadOnly):
        store.delete_playbook("forge-workflow", None)


def test_duplicate_never_drops_a_flow_whose_id_a_minted_one_would_take(tmp_home, monkeypatch):
    """Review 4: flows `a` and `a-0000` with the hex seeded to `0000` lost a flow."""
    files = _files(new_id="two-flows")
    dev = files.pop("flows/dev.toml")
    files["flows/a.toml"] = dev
    files["flows/a-0000.toml"] = dev.replace('title = "Issue to live"', 'title = "Second"')
    files["playbook.toml"] = files["playbook.toml"].replace('default = "dev"', 'default = "a"')
    store.create_playbook(files)
    hexes = iter(["0000", "0000", "1111", "2222", *[f"{i:04x}" for i in range(3, 4000)]])
    monkeypatch.setattr(store.secrets, "token_hex", lambda n=None: next(hexes))
    dup = store.duplicate_playbook("two-flows", {"id": "two-flows-copy"})
    titles = sorted(f["title"] for f in dup["flows"])
    assert titles == ["Issue to live", "Second"]
    assert len([p for p in dup["files"] if p.startswith("flows/")]) == 2
    assert not {"a", "a-0000"} & {f["id"] for f in dup["flows"]}


def test_a_failed_delete_leaves_the_default(tmp_home, monkeypatch):
    """Review 5: the default was cleared before the rename that then failed."""
    import errno

    d = store.create_playbook(_files(new_id="still-default"))
    store.set_default("still-default", d["revision"], None)

    def einval(*a):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(store.renameat, "renameat2", einval)
    with pytest.raises(store.StoreError) as e:
        store.delete_playbook("still-default", d["revision"])
    assert e.value.status == 503
    assert store.list_playbooks()["default"] == "still-default"
    assert "still-default" in _local_names()


def test_a_state_file_that_is_a_directory_is_a_clean_503(tmp_home):
    """Review 7: an unmapped `OSError` (a 500) from the rename."""
    d = store.create_playbook(_files(new_id="state-dir"))
    (_root() / ".state.json").mkdir()
    with pytest.raises(store.StoreError) as e:
        store.set_default("state-dir", d["revision"], None)
    assert e.value.status == 503 and ".state.json" in e.value.detail
    assert not any(n.startswith(".state-") for n in _local_names())


def test_the_state_directory_answers_503_on_the_route(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    d = store.create_playbook(_files(new_id="state-route"))
    (_root() / ".state.json").mkdir()
    r = c.put(
        routes.PREFIX + "/state-route/default",
        json={"revision": d["revision"], "expect_default": None},
        headers=hdr,
    )
    assert r.status_code == 503 and "state.json" in r.json()["detail"]


def test_a_delete_interrupted_before_its_state_commit_is_restored_not_lost(tmp_home, monkeypatch):
    """Hermes 5511 on #1253: the tree moved to trash, the state write failed, and the next write's
    sweep deleted the trash — a failed delete erased the playbook. A deletion is COMMITTED only by
    the published state; an uncommitted trash entry goes back under its id on the next write."""
    d = store.create_playbook(_files(new_id="crashed-delete"))
    store.set_default("crashed-delete", d["revision"], None)

    class Killed(BaseException):
        pass

    def die(*a, **k):
        raise Killed

    with monkeypatch.context() as m:
        m.setattr(store, "_write_state", die)
        with pytest.raises(Killed):
            store.delete_playbook("crashed-delete", d["revision"])
    assert store.list_playbooks()["default"] is None  # reads show what is under the id: nothing
    store.create_playbook(_files(new_id="next-write"))  # the next write settles it
    listing = store.list_playbooks()
    assert "crashed-delete" in {c["id"] for c in listing["playbooks"]}
    assert read_tree(_root() / "crashed-delete").digest() == d["revision"]
    assert listing["default"] == "crashed-delete"  # default and directory agree again
    assert not any(n.startswith(".trash-") for n in _local_names())


def test_a_failed_state_commit_renames_the_playbook_back(tmp_home, monkeypatch):
    """Hermes 5511, the reviewer's fault injection: the state rename fails → 503 → an unrelated
    create → the playbook is still present."""
    d = store.create_playbook(_files(new_id="kept-on-503"))
    real_rename = store.renameat.renameat2

    def failing(src_fd, src, dst_fd, dst, *a, **k):
        if dst == store.STATE_NAME:
            raise OSError(5, "Input/output error")
        return real_rename(src_fd, src, dst_fd, dst, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(store.renameat, "renameat2", failing)
        with pytest.raises(store.StoreError) as e:
            store.delete_playbook("kept-on-503", d["revision"])
    assert e.value.status == 503
    assert read_tree(_root() / "kept-on-503").digest() == d["revision"]
    store.create_playbook(_files(new_id="unrelated"))
    assert read_tree(_root() / "kept-on-503").digest() == d["revision"]
    assert "kept-on-503" in {c["id"] for c in store.list_playbooks()["playbooks"]}


def test_a_committed_deletion_left_in_trash_is_retained_never_deleted(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="committed-trash"))
    with monkeypatch.context() as m:
        m.setattr(store, "_retain", lambda *a: None)  # crash after the commit, before retention
        store.delete_playbook("committed-trash", d["revision"])
    assert any(n.startswith(".trash-committed-trash-") for n in _local_names())
    store.create_playbook(_files(new_id="next-write-2"))
    assert "committed-trash" not in _local_names()  # it was deleted — and stays deleted
    [kept] = [r for r in store.recovery_entries() if r["playbook_id"] == "committed-trash"]
    assert read_tree(_root() / ".recovery" / kept["name"]).digest() == d["revision"]
    assert json.loads((_root() / ".state.json").read_text())["committed"] == []


def test_a_write_through_a_held_descriptor_after_the_check_is_recoverable(tmp_home, monkeypatch):
    """Hermes 5511: an editor already holding the old README open writes AFTER the displaced tree's
    digest check. The old tree is retained, so those bytes are in the recovery copy."""
    d = store.create_playbook(_files(new_id="held-fd"))
    fd = os.open(_root() / "held-fd" / "README.md", os.O_WRONLY)
    real = store._digest_at

    def check_then_write(root_fd, name):
        out = real(root_fd, name)
        if name.startswith(".staging-"):
            os.write(fd, b"the editor's late bytes\n")  # through the descriptor, after the check
        return out

    monkeypatch.setattr(store, "_digest_at", check_then_write)
    files = d["files"]
    files["README.md"] = "from the app"
    try:
        store.update_playbook("held-fd", d["revision"], files)
    finally:
        os.close(fd)
    [kept] = store.recovery_entries()
    readme = (_root() / ".recovery" / kept["name"] / "README.md").read_bytes()
    assert readme.startswith(b"the editor's late bytes\n")
    assert store.get_playbook("held-fd")["recovery"] == [kept]


def test_healing_never_clears_a_default_some_source_still_holds(tmp_home):
    fw = store.get_playbook("forge-workflow")
    store.set_default("forge-workflow", fw["revision"], None)
    store.create_playbook(_files(new_id="unrelated"))
    assert json.loads((_root() / ".state.json").read_text())["default"] == "forge-workflow"


def test_an_edit_landing_between_the_exchange_and_the_exchange_back_is_kept(tmp_home, monkeypatch):
    """Delta review of #1253: after the first exchange the id names the NEW tree, so a path-based
    edit made before the exchange-back lands in it. The exchange-back must not delete that tree
    unchecked: edit 1 is back in place, edit 2 is in a recovery copy named in the 409."""
    d = store.create_playbook(_files(new_id="two-edits"))
    live = _root() / "two-edits"
    real_stage, real_digest = store._stage, store._digest_at
    calls = {"n": 0}

    def stage_then_edit(root_fd, tree, *a):
        name = real_stage(root_fd, tree, *a)
        (live / "README.md").write_text("edit 1\n")  # before the exchange: into the old tree
        return name

    def digest_then_edit(root_fd, name):
        calls["n"] += 1
        out = real_digest(root_fd, name)
        if calls["n"] == 1:  # the first check, right after the exchange: the id names OUR tree
            with open(live / "playbook.toml", "a") as f:
                f.write("# edit 2\n")
        return out

    monkeypatch.setattr(store, "_stage", stage_then_edit)
    monkeypatch.setattr(store, "_digest_at", digest_then_edit)
    files = d["files"]
    files["README.md"] = "from the editor"
    with pytest.raises(store.Conflict) as e:
        store.update_playbook("two-edits", d["revision"], files)
    assert (live / "README.md").read_text() == "edit 1\n"  # edit 1, in place
    [kept] = e.value.extra["kept"]
    assert kept.startswith("two-edits-") and kept in e.value.detail
    kept_tree = _root() / ".recovery" / kept
    assert (kept_tree / "playbook.toml").read_text().endswith("# edit 2\n")  # edit 2, kept
    store.create_playbook(_files(new_id="later-write"))  # nothing prunes the recovery area
    assert kept in {r["name"] for r in store.recovery_entries()}


def test_an_exchange_back_of_an_untouched_staged_tree_leaves_nothing_behind(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="one-edit"))
    real_stage = store._stage

    def stage_then_edit(root_fd, tree, *a):
        name = real_stage(root_fd, tree, *a)
        (_root() / "one-edit" / "README.md").write_text("edit\n")
        return name

    monkeypatch.setattr(store, "_stage", stage_then_edit)
    files = d["files"]
    files["README.md"] = "from the editor"
    with pytest.raises(store.Conflict) as e:
        store.update_playbook("one-edit", d["revision"], files)
    assert e.value.extra["kept"] == []
    assert not any(n.startswith((".kept-", ".staging-")) for n in _local_names())


def test_an_unreadable_source_never_clears_a_default(tmp_home, monkeypatch, tmp_path):
    """Healing clears a stale default only on a COMPLETE listing of every source."""
    fw = store.get_playbook("forge-workflow")
    store.set_default("forge-workflow", fw["revision"], None)
    real_open = store.os.open

    def flaky(path, flags, *a, **k):
        if str(path) == str(FIXTURES):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(store.os, "open", flaky)
        # The write's healing pass runs first and must not clear the default; the write itself
        # is then refused, because ownership cannot be established without that source.
        with pytest.raises(store.StoreError) as e:
            store.create_playbook(_files(new_id="while-unreadable"))
        assert e.value.status == 503
    assert json.loads((_root() / ".state.json").read_text())["default"] == "forge-workflow"
    assert store.list_playbooks()["default"] == "forge-workflow"


# ---- Hermes 5512 on #1253 -----------------------------------------------------------------------


def _fail_after_state_rename(monkeypatch):
    """EIO from the directory fsync that FOLLOWS the state's rename: the state IS published."""
    real_rename, real_fsync = store.renameat.renameat2, store._fsync_dir
    armed = {"on": False}

    def rename(src_fd, src, dst_fd, dst, *a, **k):
        out = real_rename(src_fd, src, dst_fd, dst, *a, **k)
        if dst == store.STATE_NAME:
            armed["on"] = True
        return out

    def fsync(fd):
        if armed["on"]:
            armed["on"] = False
            raise OSError(5, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(store.renameat, "renameat2", rename)
    monkeypatch.setattr(store, "_fsync_dir", fsync)


def test_a_delete_whose_state_is_published_but_not_flushed_is_committed(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="published"))
    store.set_default("published", d["revision"], None)
    with monkeypatch.context() as m:
        _fail_after_state_rename(m)
        out = store.delete_playbook("published", d["revision"])
    assert out["state_durable"] is False and "flushed" in out["state_reason"]
    listing = store.list_playbooks()
    assert "published" not in {c["id"] for c in listing["playbooks"]}
    assert [r["playbook_id"] for r in listing["recovery"]] == ["published"]
    state = json.loads((_root() / ".state.json").read_text())
    assert listing["default"] is None and state["default"] is None  # gallery == state
    assert not any(n.startswith(".trash-") for n in _local_names())


def test_a_state_rename_that_fails_changes_nothing_including_the_default(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="untouched"))
    store.set_default("untouched", d["revision"], None)
    before = (_root() / ".state.json").read_bytes()
    real = store.renameat.renameat2

    def eio(src_fd, src, dst_fd, dst, *a, **k):
        if dst == store.STATE_NAME:
            raise OSError(5, "Input/output error")
        return real(src_fd, src, dst_fd, dst, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(store.renameat, "renameat2", eio)
        with pytest.raises(store.StoreError) as e:
            store.delete_playbook("untouched", d["revision"])
        assert e.value.status == 503
        with pytest.raises(store.StoreError) as e:
            store.clear_default("untouched")
        assert e.value.status == 503
    assert (_root() / ".state.json").read_bytes() == before
    assert store.list_playbooks()["default"] == "untouched"
    assert read_tree(_root() / "untouched").digest() == d["revision"]


def test_set_default_reports_a_published_but_unflushed_state(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="dflt-durable"))
    with monkeypatch.context() as m:
        _fail_after_state_rename(m)
        out = store.set_default("dflt-durable", d["revision"], None)
    assert out["default"] == "dflt-durable" and out["state_durable"] is False
    assert store.list_playbooks()["default"] == "dflt-durable"


def test_the_delete_route_reports_state_durability(auth_cfg, tmp_home, monkeypatch):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    d = store.create_playbook(_files(new_id="route-durable"))
    with monkeypatch.context() as m:
        _fail_after_state_rename(m)
        r = c.delete(routes.PREFIX + f"/route-durable?revision={d['revision']}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["deleted"] == "route-durable" and r.json()["state_durable"] is False


def _shadow_with_unreadable_bundled(monkeypatch, how: str):
    """A LOCAL copy named like the bundled `forge-workflow`, with the bundled source unreadable."""
    import shutil

    _root().mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES / "forge-workflow", _root() / "forge-workflow")
    local_rev = read_tree(_root() / "forge-workflow").digest()
    if how == "root":
        real_open = store.os.open

        def flaky(path, flags, *a, **k):
            if str(path) == str(FIXTURES):
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *a, **k)

        monkeypatch.setattr(store.os, "open", flaky)
    else:
        real_read = store.read_tree_at

        def unreadable(fd, name):
            if name == "forge-workflow" and os.readlink(f"/proc/self/fd/{fd}") == str(FIXTURES):
                raise PermissionError(13, "Permission denied")
            return real_read(fd, name)

        monkeypatch.setattr(store, "read_tree_at", unreadable)
    return local_rev


@pytest.mark.parametrize("how", ["root", "bundle"])
def test_an_unreadable_higher_source_refuses_every_ownership_dependent_write(
    tmp_home, monkeypatch, how
):
    """Hermes 5512: with the bundled root unreadable the gallery read it as EMPTY, so the shadowed
    local copy looked like the owner and could be deleted, edited or made the default."""
    with monkeypatch.context() as m:
        local_rev = _shadow_with_unreadable_bundled(m, how)
        for call in (
            lambda: store.delete_playbook("forge-workflow", local_rev),
            lambda: store.update_playbook("forge-workflow", local_rev, _files()),
            lambda: store.set_default("forge-workflow", local_rev, None),
            lambda: store.clear_default("forge-workflow"),
            lambda: store.require_writable("forge-workflow"),
            lambda: store.create_playbook(_files(new_id="any-new")),
            lambda: store.duplicate_playbook("inbox-triage", {"id": "dup-x"}),
        ):
            with pytest.raises(store.StoreError) as e:
                call()
            assert e.value.status == 503 and "could not be read" in e.value.detail
        assert isinstance(store.list_playbooks()["playbooks"], list)  # the gallery stays fail-soft
    assert read_tree(_root() / "forge-workflow").digest() == local_rev  # the local copy survives
    assert "any-new" not in _local_names()


# ---- the independent review of dd23048 ----------------------------------------------------------


def test_a_retained_copy_whose_flush_fails_is_reported_never_raised(tmp_home, monkeypatch):
    """Review item 2: `_retain`'s fsyncs after its rename were unguarded, so EIO turned a save
    that had already happened into a 500. Now the save succeeds and says the copy is not durable."""
    d = store.create_playbook(_files(new_id="flush"))
    real = store._fsync_dir

    def eio_after_retain(fd):
        try:
            name = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            name = ""
        if name.endswith("/.recovery"):
            raise OSError(5, "Input/output error")
        return real(fd)

    files = d["files"]
    files["README.md"] = "v2"
    with monkeypatch.context() as m:
        m.setattr(store, "_fsync_dir", eio_after_retain)
        out = store.update_playbook("flush", d["revision"], files)
    assert out["recovery_durable"] is False and "flushed" in out["recovery_reason"]
    assert (_root() / "flush" / "README.md").read_text() == "v2"
    [kept] = store.recovery_entries()
    assert read_tree(_root() / ".recovery" / kept["name"]).digest() == d["revision"]


def test_a_failing_retention_flush_inside_the_sweep_never_fails_a_write(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="sweep-flush"))
    with monkeypatch.context() as m:  # a crashed save: the old tree left as staging
        m.setattr(store, "_retain", lambda *a: None)
        files = d["files"]
        files["README.md"] = "new"
        store.update_playbook("sweep-flush", d["revision"], files)
    assert any(n.startswith(".staging-sweep-flush-") for n in _local_names())  # carries the id
    real = store._fsync_dir

    def eio(fd):
        try:
            if os.readlink(f"/proc/self/fd/{fd}").endswith("/.recovery"):
                raise OSError(5, "Input/output error")
        except FileNotFoundError:
            pass
        return real(fd)

    with monkeypatch.context() as m:
        m.setattr(store, "_fsync_dir", eio)
        store.create_playbook(_files(new_id="after-sweep"))  # the sweep runs; the write succeeds
    assert "after-sweep" in _local_names()
    # Item 5: the leftover was retained under ITS playbook's id and shows in its detail.
    detail = store.get_playbook("sweep-flush")
    assert [r["playbook_id"] for r in detail["recovery"]] == ["sweep-flush"]


def test_a_damaged_state_never_resurrects_a_committed_delete(tmp_home, monkeypatch):
    """Review item 3: the sweep read the state leniently, so a damaged `.state.json` read as
    "nothing committed" and RESTORED a deleted playbook. Now the trash is left untouched."""
    d = store.create_playbook(_files(new_id="stays-deleted"))
    with monkeypatch.context() as m:
        m.setattr(store, "_retain", lambda *a: None)  # crash after the commit, before retention
        store.delete_playbook("stays-deleted", d["revision"])
    [trash] = [n for n in _local_names() if n.startswith(".trash-stays-deleted-")]
    (_root() / ".state.json").write_text("{damaged")
    store.create_playbook(_files(new_id="next"))
    assert "stays-deleted" not in _local_names()  # not resurrected
    assert trash in _local_names()  # and not moved either: commitment is unknown
    assert read_tree(_root() / trash).digest() == d["revision"]


def test_a_crashed_save_is_retained_under_its_id_and_unparseable_ones_as_unsaved(tmp_home):
    _root().mkdir(parents=True, exist_ok=True)
    (_root() / ".staging-oddname").mkdir()  # an old-style or foreign leftover name
    (_root() / ".staging-oddname" / "f").write_text("x")
    store.create_playbook(_files(new_id="any"))
    assert [r["playbook_id"] for r in store.recovery_entries()] == ["unsaved"]


def test_the_recovery_listing_is_capped_newest_first_with_a_total(tmp_home, monkeypatch):
    monkeypatch.setattr(store, "RECOVERY_LIST_MAX", 3)
    _root().mkdir(parents=True, exist_ok=True)
    rec = _root() / ".recovery"
    rec.mkdir()
    for i in range(5):
        (rec / f"capped-2026010{i}T000000-0000000{i}").mkdir()
    (rec / "other-20260109T000000-00000009").mkdir()
    listing = store.list_playbooks()
    assert listing["recovery_total"] == 6
    assert [r["at"] for r in listing["recovery"]] == [
        "20260109T000000",
        "20260104T000000",
        "20260103T000000",
    ]
    store.create_playbook(_files(new_id="capped"))
    detail = store.get_playbook("capped")
    assert detail["recovery_total"] == 5 and len(detail["recovery"]) == 3
    assert {r["playbook_id"] for r in detail["recovery"]} == {"capped"}  # filtered by id


def test_the_sweep_restore_never_replaces_an_empty_directory_at_the_id(tmp_home, monkeypatch):
    """Mutant: a plain rename would replace an empty directory now holding the id."""
    d = store.create_playbook(_files(new_id="restore-noreplace"))

    class Killed(BaseException):
        pass

    def die(*a, **k):
        raise Killed

    with monkeypatch.context() as m:
        m.setattr(store, "_write_state", die)
        with pytest.raises(Killed):
            store.delete_playbook("restore-noreplace", d["revision"])
    (_root() / "restore-noreplace").mkdir()  # someone claimed the id meanwhile
    store.create_playbook(_files(new_id="settle"))
    assert list((_root() / "restore-noreplace").iterdir()) == []  # untouched
    [kept] = [r for r in store.recovery_entries() if r["playbook_id"] == "restore-noreplace"]
    assert read_tree(_root() / ".recovery" / kept["name"]).digest() == d["revision"]


def test_committed_entries_must_be_trash_names(tmp_home):
    _root().mkdir(parents=True, exist_ok=True)
    (_root() / ".state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "default": None,
                "committed": ["../evil", ".trash-x", ".trash-ok-0123456789abcdef", 5],
            }
        )
    )
    fd = os.open(_root(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert store._read_state(fd)["committed"] == [".trash-ok-0123456789abcdef"]
    finally:
        os.close(fd)


def test_a_state_write_over_a_damaged_state_is_refused_so_no_delete_comes_back(
    tmp_home, monkeypatch
):
    """The reviewer's two-step repro: a committed delete left in trash, a damaged state, then
    set-default — which used to publish `committed: []` built from a lenient read — and then any
    write, whose sweep restored the deleted playbook."""
    d = store.create_playbook(_files(new_id="gone-for-good"))
    other = store.create_playbook(_files(new_id="other-one"))
    with monkeypatch.context() as m:
        m.setattr(store, "_retain", lambda *a: None)
        store.delete_playbook("gone-for-good", d["revision"])
    (_root() / ".state.json").write_text("{not json")
    for call in (
        lambda: store.set_default("other-one", other["revision"], None),
        lambda: store.clear_default("other-one"),
        lambda: store.delete_playbook("other-one", other["revision"]),
    ):
        with pytest.raises(store.StoreError) as e:
            call()
        assert e.value.status == 503 and "damaged" in e.value.detail
    assert (_root() / ".state.json").read_text() == "{not json"  # nothing was written
    store.create_playbook(_files(new_id="any-write"))
    assert "gone-for-good" not in _local_names()
    assert "gone-for-good" not in {c["id"] for c in store.list_playbooks()["playbooks"]}


def test_a_failed_flush_after_the_exchange_still_answers_the_409(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="flush-409"))
    real_stage, real_fsync = store._stage, store._fsync_dir
    armed = {"on": False}

    def stage_then_edit(root_fd, tree, *a):
        name = real_stage(root_fd, tree, *a)
        (_root() / "flush-409" / "README.md").write_text("hand edit\n")
        armed["on"] = True  # every directory flush from here on fails
        return name

    def eio(fd):
        if armed["on"]:
            raise OSError(5, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(store, "_stage", stage_then_edit)
    monkeypatch.setattr(store, "_fsync_dir", eio)
    files = d["files"]
    files["README.md"] = "from the app"
    with pytest.raises(store.Conflict):
        store.update_playbook("flush-409", d["revision"], files)
    assert (_root() / "flush-409" / "README.md").read_text() == "hand edit\n"


def test_a_published_save_or_create_whose_flush_fails_reports_it(tmp_home, monkeypatch):
    real = store._fsync_dir
    armed = {"on": False}
    real_rename = store.renameat.renameat2

    def rename(*a):
        out = real_rename(*a)
        armed["on"] = True
        return out

    def eio(fd):
        if armed["on"]:
            armed["on"] = False
            raise OSError(5, "Input/output error")
        return real(fd)

    with monkeypatch.context() as m:
        m.setattr(store.renameat, "renameat2", rename)
        m.setattr(store, "_fsync_dir", eio)
        out = store.create_playbook(_files(new_id="flush-create"))
    assert out["durable"] is False and "flushed" in out["durable_reason"]
    assert "flush-create" in _local_names()


def test_default_healing_never_rewrites_a_damaged_state(tmp_home):
    """A state the strict read refuses (here: a malformed `committed`) is never rewritten by the
    healing pass, even when its default names a playbook no source holds."""
    _root().mkdir(parents=True, exist_ok=True)
    damaged = '{"version": 1, "default": "ghost-playbook", "committed": "not-a-list"}'
    (_root() / ".state.json").write_text(damaged)
    store.create_playbook(_files(new_id="a-write"))  # runs the sweep and the healing pass
    assert (_root() / ".state.json").read_text() == damaged


# ---- the independent re-review of 9bcc03d -------------------------------------------------------


def test_a_delete_refused_for_a_damaged_state_leaves_the_playbook_in_place(tmp_home):
    d = store.create_playbook(_files(new_id="victim"))
    (_root() / ".state.json").write_text("{not json")
    with pytest.raises(store.StoreError) as e:
        store.delete_playbook("victim", d["revision"])
    assert e.value.status == 503 and "damaged" in e.value.detail
    assert read_tree(_root() / "victim").digest() == d["revision"]
    assert not any(n.startswith(".trash-") for n in _local_names())
    assert "victim" in {c["id"] for c in store.list_playbooks()["playbooks"]}


def _eio_on_next_root_flush_after_a_rename(m):
    real_fsync, real_rename = store._fsync_dir, store.renameat.renameat2
    armed = {"on": False, "fired": False}

    def rename(*a):
        out = real_rename(*a)
        armed["on"] = not armed["fired"]
        return out

    def eio(fd):
        if armed["on"]:
            armed["on"] = False
            armed["fired"] = True
            raise OSError(5, "Input/output error")
        return real_fsync(fd)

    m.setattr(store.renameat, "renameat2", rename)
    m.setattr(store, "_fsync_dir", eio)


@pytest.mark.parametrize("op", ["update", "duplicate"])
def test_a_published_update_or_duplicate_whose_flush_fails_reports_it(tmp_home, monkeypatch, op):
    d = store.create_playbook(_files(new_id=f"flush-{op}"))
    with monkeypatch.context() as m:
        _eio_on_next_root_flush_after_a_rename(m)
        if op == "update":
            files = d["files"]
            files["README.md"] = "v2"
            out = store.update_playbook(f"flush-{op}", d["revision"], files)
        else:
            out = store.duplicate_playbook(f"flush-{op}", {"id": "flush-dup-copy"})
    assert out["durable"] is False and "flushed" in out["durable_reason"]


def test_a_failed_flush_after_deletes_rename_aside_does_not_raise(tmp_home, monkeypatch):
    d = store.create_playbook(_files(new_id="flush-del"))
    with monkeypatch.context() as m:
        _eio_on_next_root_flush_after_a_rename(m)  # the first rename is delete's rename-aside
        out = store.delete_playbook("flush-del", d["revision"])
    assert out["state_durable"] is True and "flush-del" not in _local_names()


@pytest.mark.parametrize("op", ["update", "delete"])
def test_a_tree_the_recovery_area_could_not_take_yet_is_reported(tmp_home, monkeypatch, op):
    d = store.create_playbook(_files(new_id=f"rec-{op}"))

    def eio_creating_recovery(fd):
        raise OSError(5, "Input/output error")

    def recovery_fd(root_fd):  # creating `.recovery` fails at its flush
        os.mkdir(store.RECOVERY_DIR, store.DIR_MODE, dir_fd=root_fd)
        eio_creating_recovery(root_fd)

    with monkeypatch.context() as m:
        m.setattr(store, "_recovery_fd", recovery_fd)
        if op == "update":
            files = d["files"]
            files["README.md"] = "v2"
            out = store.update_playbook(f"rec-{op}", d["revision"], files)
        else:
            out = store.delete_playbook(f"rec-{op}", d["revision"])
    assert out["recovery_durable"] is False and "next write" in out["recovery_reason"]
    store.create_playbook(_files(new_id="settles"))  # the next write retains it
    assert [r["playbook_id"] for r in store.recovery_entries()] == [f"rec-{op}"]


# ---- Hermes 5527 on #1253 ------------------------------------------------------------------


def test_the_strict_state_read_parses_the_bytes_it_validated(tmp_home, monkeypatch):
    """Hermes 5527: the strict read validated one open and then RE-OPENED leniently, so a swap in
    between was accepted. Now a single read is parsed; a second open never happens."""
    store.create_playbook(_files(new_id="any"))
    good = json.dumps({"version": 1, "default": None, "committed": [".trash-x0-0123456789abcdef"]})
    (_root() / ".state.json").write_text(good)
    real_read = store.os.read
    reads = {"n": 0}

    def swapping_read(fd, n):
        out = real_read(fd, n)
        reads["n"] += 1
        (_root() / ".state.json").write_text("{damaged")  # swapped right after the first read
        return out

    fd = os.open(_root(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with monkeypatch.context() as m:
            m.setattr(store.os, "read", swapping_read)
            state = store._read_state_strict(fd)
        assert reads["n"] == 1
        assert state is not None and state["committed"] == [".trash-x0-0123456789abcdef"]
        # …and the damaged version that is there now is refused by the strict read.
        assert store._read_state_strict(fd) is None
    finally:
        os.close(fd)


def _one_recovery_entry() -> dict:
    d = store.create_playbook(_files(new_id="fenced"))
    files = d["files"]
    files["README.md"] = "v2"
    store.update_playbook("fenced", d["revision"], files)
    [kept] = store.recovery_entries()
    assert store.REVISION_RE.fullmatch(kept["revision"])
    return kept


@pytest.mark.parametrize(
    "damage",
    ["{damaged", '{"version":1,"default":null,"committed":[".trash-ab-0123456789abcdef"]}'],
)
def test_state_changed_during_delete_restores_the_live_tree(tmp_home, monkeypatch, damage):
    d = store.create_playbook(_files(new_id="state-race"))
    store.set_default("state-race", d["revision"], None)
    real = store.renameat.renameat2

    def change_state(*args):
        result = real(*args)
        if args[1] == "state-race" and args[3].startswith(".trash-"):
            (_root() / ".state.json").write_text(damage)
        return result

    monkeypatch.setattr(store.renameat, "renameat2", change_state)
    with pytest.raises(store.StoreError) as e:
        store.delete_playbook("state-race", d["revision"])
    assert e.value.status == 503
    assert store.get_playbook("state-race")["revision"] == d["revision"]
    assert (_root() / ".state.json").read_text() == damage
    assert not any(n.startswith(".trash-") for n in _local_names())


def test_recovery_revisions_bind_content_and_directory_identity(tmp_home, tmp_path):
    import shutil

    kept = _one_recovery_entry()
    path = _root() / ".recovery" / kept["name"]
    assert store.list_playbooks()["recovery"] == [kept]
    assert store.get_playbook("fenced")["recovery"] == [kept]
    (path / "README.md").write_text("edited after listing")
    [edited] = store.recovery_entries()
    assert edited["revision"] != kept["revision"]
    path.rename(tmp_path / "original")
    shutil.copytree(tmp_path / "original", path)
    [replaced] = store.recovery_entries()
    assert replaced["revision"] != edited["revision"]
    assert read_tree(path).digest() == read_tree(tmp_path / "original").digest()


def test_online_operations_cannot_purge_recovery_edits(auth_cfg, tmp_home):
    kept = _one_recovery_entry()
    path = _root() / ".recovery" / kept["name"]
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    with (path / "README.md").open("w") as editor:
        # Even a previously valid name/revision cannot authorize a destructive online purge.
        r = c.delete(
            "/api/playbook-recovery/" + kept["name"],
            params={"revision": kept["revision"]},
            headers=hdr,
        )
        assert r.status_code in (404, 405)
        editor.write("late edit through an open descriptor")
    store.create_playbook(_files(new_id="sweep-after-edit"))
    assert (path / "README.md").read_text() == "late edit through an open descriptor"
    assert [r["name"] for r in store.recovery_entries()] == [kept["name"]]
    assert not hasattr(store, "delete_recovery")


# ---- Hermes 5618: state publication and staging ownership ---------------------------------------


def test_sweep_does_not_overwrite_a_new_deletion_commitment(tmp_home, monkeypatch):
    store.create_playbook(_files(new_id="first-gone"))
    second = store.create_playbook(_files(new_id="second-gone"))
    first_trash = ".trash-first-gone-0123456789abcdef"
    second_trash = ".trash-second-gone-fedcba9876543210"
    (_root() / "first-gone").rename(_root() / first_trash)
    state = {"version": 1, "default": None, "committed": [first_trash]}
    (_root() / store.STATE_NAME).write_text(json.dumps(state))
    real = store._retain

    def retain_then_commit(root_fd, name, pid):
        kept = real(root_fd, name, pid)
        if name == first_trash:
            (_root() / "second-gone").rename(_root() / second_trash)
            (_root() / store.STATE_NAME).write_text(
                json.dumps({**state, "committed": [first_trash, second_trash]})
            )
        return kept

    with monkeypatch.context() as m:
        m.setattr(store, "_retain", retain_then_commit)
        store.create_playbook(_files(new_id="runs-sweep"))
    assert second_trash in json.loads((_root() / store.STATE_NAME).read_text())["committed"]
    store.create_playbook(_files(new_id="sweeps-again"))
    assert "second-gone" not in _local_names()
    [kept] = [r for r in store.recovery_entries() if r["playbook_id"] == "second-gone"]
    assert read_tree(_root() / ".recovery" / kept["name"]).digest() == second["revision"]


@pytest.mark.parametrize("interval", ["before_write", "at_exchange"])
def test_delete_state_publication_refuses_an_intervening_state(tmp_home, monkeypatch, interval):
    detail = store.create_playbook(_files(new_id="state-fence"))
    store.set_default("state-fence", detail["revision"], None)
    changed = json.dumps(
        {
            "version": 1,
            "default": None,
            "committed": [
                ".trash-other-gone-0123456789abcdef",
            ],
        }
    )

    def replace_state():
        candidate = _root() / ".external-state"
        candidate.write_text(changed)
        candidate.replace(_root() / store.STATE_NAME)

    if interval == "before_write":
        real_write = store._write_state

        def write(root_fd, state, **kwargs):
            replace_state()
            return real_write(root_fd, state, **kwargs)

        monkeypatch.setattr(store, "_write_state", write)
    else:
        real_rename = store.renameat.renameat2
        fired = False

        def rename(src_fd, src, dst_fd, dst, flags):
            nonlocal fired
            if dst == store.STATE_NAME and flags == store.renameat.RENAME_EXCHANGE and not fired:
                fired = True
                replace_state()
            return real_rename(src_fd, src, dst_fd, dst, flags)

        monkeypatch.setattr(store.renameat, "renameat2", rename)
    with pytest.raises(store.StoreError, match="state changed"):
        store.delete_playbook("state-fence", detail["revision"])
    assert (_root() / store.STATE_NAME).read_text() == changed
    assert read_tree(_root() / "state-fence").digest() == detail["revision"]


def test_state_publication_refuses_an_existing_open_writer(tmp_home):
    detail = store.create_playbook(_files(new_id="state-writer"))
    store.set_default("state-writer", detail["revision"], None)
    before = (_root() / store.STATE_NAME).read_bytes()
    with (_root() / store.STATE_NAME).open("r+") as writer:
        with pytest.raises(store.StoreError, match="cannot be saved"):
            store.delete_playbook("state-writer", detail["revision"])
        assert writer.read().encode() == before
    assert read_tree(_root() / "state-writer").digest() == detail["revision"]


def test_failed_staging_never_removes_a_substituted_directory(tmp_home, monkeypatch):
    real = store.read_tree_at

    def substitute(root_fd, name):
        if name.startswith(store.STAGING_PREFIX):
            (_root() / name).rename(_root() / "editor-kept-original")
            (_root() / name).mkdir()
            (_root() / name / "precious.txt").write_text("operator work")
        return real(root_fd, name)

    monkeypatch.setattr(store, "read_tree_at", substitute)
    with pytest.raises(store.Conflict):
        store.create_playbook(_files(new_id="stage-substitute"))
    assert (_root() / "editor-kept-original" / "playbook.toml").is_file()
    assert [p.read_text() for p in (_root() / ".recovery").glob("*/precious.txt")] == [
        "operator work"
    ]
    assert "stage-substitute" not in _local_names()


def test_a_crash_after_state_exchange_blocks_automatic_trash_restoration(tmp_home, monkeypatch):
    detail = store.create_playbook(_files(new_id="state-crash"))
    store.set_default("state-crash", detail["revision"], None)
    real = store.renameat.renameat2

    class Killed(BaseException):
        pass

    def crash(src_fd, src, dst_fd, dst, flags):
        result = real(src_fd, src, dst_fd, dst, flags)
        if dst == store.STATE_NAME and flags == store.renameat.RENAME_EXCHANGE:
            raise Killed
        return result

    with monkeypatch.context() as m:
        m.setattr(store.renameat, "renameat2", crash)
        with pytest.raises(Killed):
            store.delete_playbook("state-crash", detail["revision"])
    assert (_root() / store.STATE_PENDING).is_file()
    [trash] = list(_root().glob(".trash-state-crash-*"))
    assert read_tree(trash).digest() == detail["revision"]
    store.create_playbook(_files(new_id="after-state-crash"))
    assert trash.is_dir() and not (_root() / "state-crash").exists()
    with pytest.raises(store.StoreError, match="unsettled"):
        store.set_default("forge-workflow", store.get_playbook("forge-workflow")["revision"], None)


@pytest.mark.parametrize("failure", ["io", "replacement"])
def test_failed_state_rollback_retains_evidence_and_never_restores_a_deleted_tree(
    tmp_home, monkeypatch, failure
):
    detail = store.create_playbook(_files(new_id="state-rollback"))
    store.set_default("state-rollback", detail["revision"], None)
    real = store.renameat.renameat2
    exchanges = 0
    changed = json.dumps(
        {
            "version": 1,
            "default": None,
            "committed": [
                ".trash-other-gone-0123456789abcdef",
            ],
        }
    )

    def rename(src_fd, src, dst_fd, dst, flags):
        nonlocal exchanges
        if dst == store.STATE_NAME and flags == store.renameat.RENAME_EXCHANGE:
            exchanges += 1
            if exchanges == 2:
                if failure == "io":
                    raise OSError(5, "rollback failed")
                candidate = _root() / ".second-external-state"
                candidate.write_text(changed.replace("other-gone", "later-gone"))
                candidate.replace(_root() / store.STATE_NAME)
                (_root() / ".trash-later-gone-0123456789abcdef").mkdir()
            else:
                candidate = _root() / ".external-state"
                candidate.write_text(changed)
                candidate.replace(_root() / store.STATE_NAME)
        return real(src_fd, src, dst_fd, dst, flags)

    with monkeypatch.context() as m:
        m.setattr(store.renameat, "renameat2", rename)
        with pytest.raises(store.StoreError) as error:
            store.delete_playbook("state-rollback", detail["revision"])
    assert error.value.extra["state_unsettled"] is True
    assert (_root() / store.STATE_PENDING).is_file()
    retained = [p.read_text() for p in _root().glob(".retained-state-*")]
    assert changed in [*retained, (_root() / store.STATE_NAME).read_text()]
    if failure == "replacement":
        assert changed.replace("other-gone", "later-gone") in retained
    store.create_playbook(_files(new_id="after-failed-rollback"))
    if failure == "replacement":
        assert (_root() / ".trash-later-gone-0123456789abcdef").is_dir()
        assert not (_root() / "later-gone").exists()
    assert not (_root() / "state-rollback").exists()
    [kept] = [r for r in store.recovery_entries() if r["playbook_id"] == "state-rollback"]
    assert read_tree(_root() / ".recovery" / kept["name"]).digest() == detail["revision"]


def test_a_commit_added_during_sweep_restoration_prevents_resurrection(tmp_home, monkeypatch):
    detail = store.create_playbook(_files(new_id="late-commit"))
    trash = ".trash-late-commit-0123456789abcdef"
    (_root() / "late-commit").rename(_root() / trash)
    real = store.renameat.renameat2

    def commit(src_fd, src, dst_fd, dst, flags):
        result = real(src_fd, src, dst_fd, dst, flags)
        if src == trash and dst == "late-commit":
            (_root() / store.STATE_NAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "default": None,
                        "committed": [trash],
                    }
                )
            )
        return result

    with monkeypatch.context() as m:
        m.setattr(store.renameat, "renameat2", commit)
        with pytest.raises(store.StoreError, match="changed during recovery"):
            store.create_playbook(_files(new_id="triggers-restore"))
    assert not (_root() / "late-commit").exists()
    assert read_tree(_root() / trash).digest() == detail["revision"]
    store.create_playbook(_files(new_id="settles-late-commit"))
    assert not (_root() / "late-commit").exists()
    [kept] = [r for r in store.recovery_entries() if r["playbook_id"] == "late-commit"]
    assert read_tree(_root() / ".recovery" / kept["name"]).digest() == detail["revision"]


def test_state_marker_retention_failure_reports_saved_but_requires_recovery(tmp_home, monkeypatch):
    detail = store.create_playbook(_files(new_id="marker-settlement"))
    real = store.renameat.renameat2

    def refuse_marker_move(src_fd, src, dst_fd, dst, flags):
        if src == store.STATE_PENDING:
            raise OSError(5, "marker retention failed")
        return real(src_fd, src, dst_fd, dst, flags)

    with monkeypatch.context() as m:
        m.setattr(store.renameat, "renameat2", refuse_marker_move)
        result = store.set_default("marker-settlement", detail["revision"], None)
    assert result["default"] == "marker-settlement" and result["state_durable"] is False
    assert "completion marker" in result["state_reason"]
    assert json.loads((_root() / store.STATE_NAME).read_text())["default"] == "marker-settlement"
    assert (_root() / store.STATE_PENDING).is_file()
    with pytest.raises(store.StoreError, match="unsettled"):
        store.set_default("marker-settlement", detail["revision"], None)


@pytest.mark.parametrize(
    ("interval", "edited"),
    [
        ("after_restore", True),
        ("after_restore", False),
        ("at_withdrawal", True),
        ("at_put_back", True),
    ],
)
def test_sweep_rollback_preserves_an_editors_replacement(
    tmp_home, tmp_path, monkeypatch, interval, edited
):
    """Hermes 5623: a restored name is not ownership of whatever later occupies that name."""
    import shutil

    pid = "sweep-owner"
    detail = store.create_playbook(_files(new_id=pid))
    live = _root() / pid
    trash = f".trash-{pid}-0123456789abcdef"
    live.rename(_root() / trash)
    original = tmp_path / "editor-kept-original"
    real = store.renameat.renameat2
    restored = replaced = put_back_raced = False
    editor_inode = None

    def replace_live():
        nonlocal replaced, editor_inode
        live.rename(original)
        shutil.copytree(original, live)
        if edited:
            (live / "README.md").write_text("the editor's replacement")
        editor_inode = live.stat().st_ino
        replaced = True

    def race(src_fd, src, dst_fd, dst, flags):
        nonlocal restored, put_back_raced, editor_inode
        if restored and src == pid and not replaced:
            replace_live()  # between the identity check and the withdrawal syscall
        if interval == "at_put_back" and replaced and dst == pid:
            shutil.copytree(original, live)
            (live / "README.md").write_text("a still newer live replacement")
            editor_inode = live.stat().st_ino
            put_back_raced = True
        result = real(src_fd, src, dst_fd, dst, flags)
        if src == trash and dst == pid and not restored:
            restored = True
            (_root() / store.STATE_NAME).write_text(
                json.dumps({"version": 1, "default": None, "committed": [trash]})
            )
            if interval == "after_restore":
                replace_live()
        return result

    with monkeypatch.context() as m:
        m.setattr(store.renameat, "renameat2", race)
        with pytest.raises(store.StoreError, match="changed during recovery") as error:
            store.create_playbook(_files(new_id="triggers-owner-restore"))
    assert replaced and live.is_dir()
    assert live.stat().st_ino == editor_inode
    expected = "a still newer live replacement" if put_back_raced else "the editor's replacement"
    if not edited:
        expected = (original / "README.md").read_text()
    assert (live / "README.md").read_text() == expected
    assert read_tree(original).digest() == detail["revision"]
    if interval == "at_put_back":
        assert put_back_raced
        [kept] = error.value.extra["kept"]
        assert (
            _root() / ".recovery" / kept / "README.md"
        ).read_text() == "the editor's replacement"
    # A later sweep cannot classify the editor's replacement as this deletion's committed trash.
    store.create_playbook(_files(new_id="sweeps-after-owner-race"))
    assert live.stat().st_ino == editor_inode
    assert (live / "README.md").read_text() == expected
