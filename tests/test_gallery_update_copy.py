"""Gallery updates publish a fresh copy and never replace the compared source."""

import uuid

import pytest

from agent_sessions.gallery import library, origins, skills, storage
from agent_sessions.playbooks import store
from test_gallery_library import catalog as source_catalog
from test_gallery_library import imported, next_version


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    return source_catalog.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_update_creates_new_copy_with_original_and_lineage_unchanged(catalog, kind):
    imported(catalog, kind)
    before = library.local(kind, "my-copy")
    root = store.local_root() if kind == "playbook" else storage.skills_root()
    inode = (root / "my-copy").stat().st_ino
    incoming = next_version(catalog, kind)
    comparison = library.compare(kind, "my-copy", incoming["digest"])
    op = uuid.uuid4().hex
    args = (
        kind,
        "my-copy",
        incoming["digest"],
        comparison["comparison"],
        comparison["proposed"],
        op,
        True,
    )
    result = library.apply_update(*args, new_id="updated-copy")
    assert result["state"] == "complete"
    assert result["result"]["id"] == "updated-copy"
    assert library.local(kind, "my-copy") == before
    assert (root / "my-copy").stat().st_ino == inode
    after = library.local(kind, "updated-copy")
    assert after["files"]["README.md"] == "Upstream update"
    assert after["gallery_origin"]["previous"] == before["gallery_origin"]["pointer"]
    evidence = origins.evidence(after["gallery_origin"]["pointer"], kind, "updated-copy")
    assert evidence["original"] == before["gallery_origin"]["original"]
    assert library.apply_update(*args, new_id="updated-copy") == result
    with pytest.raises(store.Conflict):
        library.apply_update(*args, new_id="another-copy")
    assert not (root / "another-copy").exists()


@pytest.mark.parametrize("kind", ["playbook", "skill"])
@pytest.mark.parametrize("fault", ["content", "origin"])
@pytest.mark.parametrize("when", ["staged", "publishing"])
def test_source_edit_remains_live_and_is_never_exchanged(catalog, monkeypatch, kind, fault, when):
    import os

    from agent_sessions import renameat

    imported(catalog, kind)
    incoming = next_version(catalog, kind)
    view = library.compare(kind, "my-copy", incoming["digest"])
    before = library.local(kind, "my-copy")
    root = store.local_root() if kind == "playbook" else storage.skills_root()
    inode = (root / "my-copy").stat().st_ino
    name = "README.md" if kind == "playbook" else "SKILL.md"

    def edit():
        if fault == "content":
            (root / "my-copy" / name).write_text("Concurrent editor")
        else:
            os.removexattr(root / "my-copy", origins.XATTR)

    if when == "staged":
        module = store if kind == "playbook" else skills
        stage = module._stage

        def changed_stage(*args, **kwargs):
            result = stage(*args, **kwargs)
            edit()
            return result

        monkeypatch.setattr(module, "_stage", changed_stage)
    else:
        rename = renameat.renameat2

        def changed_publish(src_fd, src, dst_fd, dst, flags):
            if src.startswith(".staging-") and dst == "updated-copy":
                assert flags == renameat.RENAME_NOREPLACE
                edit()
            return rename(src_fd, src, dst_fd, dst, flags)

        monkeypatch.setattr(renameat, "renameat2", changed_publish)

    def apply():
        return library.apply_update(
            kind,
            "my-copy",
            incoming["digest"],
            view["comparison"],
            view["proposed"],
            uuid.uuid4().hex,
            True,
            new_id="updated-copy",
        )

    if when == "staged":
        with pytest.raises(store.Conflict, match="compare again"):
            apply()
        assert not (root / "updated-copy").exists()
    else:
        assert apply()["state"] == "complete"
    current = library.local(kind, "my-copy")
    assert (root / "my-copy").stat().st_ino == inode
    if fault == "content":
        assert current["files"][name] == "Concurrent editor"
        assert current["gallery_origin"] == before["gallery_origin"]
    else:
        assert current["files"] == before["files"]
        assert current["gallery_origin"]["state"] == "unavailable"


@pytest.mark.parametrize("kind", ["playbook", "skill"])
@pytest.mark.parametrize("collision", ["same-id", "existing", "race"])
def test_copy_destination_never_replaces_an_existing_name(catalog, monkeypatch, kind, collision):
    imported(catalog, kind)
    before = library.local(kind, "my-copy")
    incoming = next_version(catalog, kind)
    view = library.compare(kind, "my-copy", incoming["digest"])
    root = store.local_root() if kind == "playbook" else storage.skills_root()
    target = "my-copy" if collision == "same-id" else "updated-copy"
    if collision == "existing":
        (root / target).mkdir()
        (root / target / "owner.txt").write_text("Other owner")
    elif collision == "race":
        publish = store._publish_new

        def collide(fd, staging, pid):
            (root / pid).mkdir()
            (root / pid / "owner.txt").write_text("Other owner")
            return publish(fd, staging, pid)

        monkeypatch.setattr(store, "_publish_new", collide)
    with pytest.raises(store.Conflict):
        library.apply_update(
            kind,
            "my-copy",
            incoming["digest"],
            view["comparison"],
            view["proposed"],
            uuid.uuid4().hex,
            True,
            new_id=target,
        )
    assert library.local(kind, "my-copy") == before
    if collision != "same-id":
        assert (root / target / "owner.txt").read_text() == "Other owner"


@pytest.mark.parametrize("kind", ["playbook", "skill"])
@pytest.mark.parametrize("fault", ["before", "after", "changed"])
def test_copy_interruption_never_replays_and_recovers_only_exact_destination(
    catalog, monkeypatch, kind, fault
):
    imported(catalog, kind)
    before = library.local(kind, "my-copy")
    incoming = next_version(catalog, kind)
    view = library.compare(kind, "my-copy", incoming["digest"])
    op = uuid.uuid4().hex
    args = (kind, "my-copy", incoming["digest"], view["comparison"], view["proposed"], op, True)
    root = store.local_root() if kind == "playbook" else storage.skills_root()
    publish = store._publish_new

    def interrupt(fd, staging, pid):
        if fault != "before":
            publish(fd, staging, pid)
            if fault == "changed":
                (root / pid / "README.md").write_text("Another editor")
        raise OSError("interrupted response")

    with monkeypatch.context() as m:
        m.setattr(store, "_publish_new", interrupt)
        with pytest.raises(OSError):
            library.apply_update(*args, new_id="updated-copy")
    outcome = library.operation(op)
    assert outcome["state"] == ("complete" if fault == "after" else "uncertain")
    assert library.apply_update(*args, new_id="updated-copy") == outcome
    assert library.local(kind, "my-copy") == before
    if fault == "before":
        assert not (root / "updated-copy").exists()
    if fault == "changed":
        assert (root / "updated-copy" / "README.md").read_text() == "Another editor"


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_multiple_copy_generations_keep_bases_under_their_own_ids(catalog, kind):
    imported(catalog, kind)
    incoming = next_version(catalog, kind)
    original = library.local(kind, "my-copy")
    for source, target in [("my-copy", "second-copy"), ("second-copy", "third-copy")]:
        before = library.local(kind, source)
        view = library.compare(kind, source, incoming["digest"])
        library.apply_update(
            kind,
            source,
            incoming["digest"],
            view["comparison"],
            view["proposed"],
            uuid.uuid4().hex,
            True,
            new_id=target,
        )
        assert library.local(kind, source) == before
        new = library.local(kind, target)
        evidence = origins.evidence(new["gallery_origin"]["pointer"], kind, target)
        assert evidence["original"] == original["gallery_origin"]["original"]
        assert evidence["previous"] == before["gallery_origin"]["pointer"]
        if kind == "playbook":
            from agent_sessions.gallery import contract

            assert (
                contract.tomllib.loads(evidence["base_files"]["playbook.toml"])["identity"]["id"]
                == target
            )
            assert new["files"]["flows/triage.toml"] == original["files"]["flows/triage.toml"]


def test_copy_keeps_default_and_existing_deployment_record(catalog):
    from agent_sessions.playbooks import deployment_state

    imported(catalog)
    source = store.get_playbook("my-copy")
    store.set_default("my-copy", source["revision"], None)
    record = {
        "version": 1,
        "project_id": "p-example",
        "playbook_id": "my-copy",
        "state": "bound",
        "revision": source["revision"],
    }
    with deployment_state.locked("p-example", create=True) as locked:
        locked.write(record)
    path = store.local_root() / ".deployments" / "p-example" / "state.json"
    saved = path.read_bytes()
    incoming = next_version(catalog)
    view = library.compare("playbook", "my-copy", incoming["digest"])
    library.apply_update(
        "playbook",
        "my-copy",
        incoming["digest"],
        view["comparison"],
        view["proposed"],
        uuid.uuid4().hex,
        True,
        new_id="updated-copy",
    )
    assert store.get_playbook("my-copy")["default"]
    assert not store.get_playbook("updated-copy")["default"]
    assert path.read_bytes() == saved
    assert deployment_state.Registry().projects_running("my-copy") == [{"project_id": "p-example"}]
    assert deployment_state.Registry().projects_running("updated-copy") == []


@pytest.mark.parametrize("kind", ["playbook", "skill"])
def test_copy_route_requires_new_id_and_preserves_source(auth_cfg, catalog, kind):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    imported(catalog, kind)
    source = library.local(kind, "my-copy")
    incoming = next_version(catalog, kind)
    view = library.compare(kind, "my-copy", incoming["digest"])
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    login = client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert login.status_code == 303
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    payload = {
        "kind": kind,
        "id": "my-copy",
        "digest": incoming["digest"],
        "comparison": view["comparison"],
        "files": view["proposed"],
        "operation": uuid.uuid4().hex,
        "acknowledge": True,
    }
    assert client.post("/api/gallery/update", json=payload, headers=headers).status_code == 422
    payload["new_id"] = "updated-copy"
    assert client.post("/api/gallery/update", json=payload).status_code == 403
    assert (
        client.post(
            "/api/gallery/update",
            json=payload,
            headers={**headers, "Origin": "https://invalid.example"},
        ).status_code
        == 403
    )
    result = client.post("/api/gallery/update", json=payload, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["result"]["id"] == "updated-copy"
    assert result.headers["cache-control"] == "no-store"
    assert library.local(kind, "my-copy") == source
    assert client.post("/api/gallery/update", json=payload, headers=headers).json() == result.json()


@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_skill_edit_copy_retains_source_and_base_without_fresh_gallery(
    catalog, monkeypatch, marked, interrupted
):
    from agent_sessions.gallery import contract
    from test_gallery_library import NOW

    if marked:
        imported(catalog, "skill")
    else:
        skills.save("my-copy", {"SKILL.md": "Local only"})
    source = skills.get("my-copy")
    source_inode = (storage.skills_root() / "my-copy").stat().st_ino
    files = {**source["files"], "SKILL.md": "Edited copy"}
    op = uuid.uuid4().hex
    pointer = source["gallery_origin"]["pointer"] if marked else None
    args = ("my-copy", "edited-copy", source["revision"], pointer, files, op)
    monkeypatch.setattr(contract.time, "time", lambda: NOW + 86400)
    publish = store._publish_new
    if interrupted:

        def crash(*args):
            publish(*args)
            raise OSError("interrupted after creation")

        with monkeypatch.context() as m:
            m.setattr(store, "_publish_new", crash)
            with pytest.raises(OSError):
                library.edit_skill_copy(*args)
        result = library.operation(op)
        assert result["state"] == ("complete" if marked else "uncertain")
    else:
        result = library.edit_skill_copy(*args)
        assert result["state"] == "complete"
    assert library.edit_skill_copy(*args) == result
    assert skills.get("my-copy") == source
    assert (storage.skills_root() / "my-copy").stat().st_ino == source_inode
    edited = skills.get("edited-copy")
    assert edited["files"] == files
    if marked:
        base = origins.evidence(edited["gallery_origin"]["pointer"], "skill", "edited-copy")
        old = origins.evidence(pointer, "skill", "my-copy")
        assert base["base_files"] == old["base_files"]
        assert base["entry_digest"] == old["entry_digest"]
        assert base["previous"] == pointer
    else:
        assert edited["gallery_origin"] is None


@pytest.mark.parametrize("fault", ["content", "origin", "destination"])
def test_skill_edit_copy_refuses_races_without_replacing_source(catalog, monkeypatch, fault):
    import os

    imported(catalog, "skill")
    source = skills.get("my-copy")
    root = storage.skills_root()
    inode = (root / "my-copy").stat().st_ino
    stage = skills._stage

    def race(*args):
        staging = stage(*args)
        if fault == "content":
            (root / "my-copy" / "SKILL.md").write_text("Concurrent edit")
        elif fault == "origin":
            os.removexattr(root / "my-copy", origins.XATTR)
        else:
            (root / "edited-copy").mkdir()
            (root / "edited-copy" / "SKILL.md").write_text("Other owner")
        return staging

    monkeypatch.setattr(skills, "_stage", race)
    with pytest.raises(store.Conflict):
        library.edit_skill_copy(
            "my-copy",
            "edited-copy",
            source["revision"],
            source["gallery_origin"]["pointer"],
            {"SKILL.md": "Submitted"},
            uuid.uuid4().hex,
        )
    assert (root / "my-copy").stat().st_ino == inode
    current = skills.get("my-copy")
    assert current["files"]["SKILL.md"] == (
        "Concurrent edit" if fault == "content" else source["files"]["SKILL.md"]
    )
    if fault == "destination":
        assert (root / "edited-copy" / "SKILL.md").read_text() == "Other owner"
    else:
        assert not (root / "edited-copy").exists()
