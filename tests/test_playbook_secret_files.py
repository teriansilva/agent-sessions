"""Secret reference files (#1191, format 3): `{{secret_path:<name>}}` renders a PATH; apply writes
the bound value there at 0600 outside the project, and remove deletes only the file it wrote."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sessions import projects, template_vars
from agent_sessions.playbooks import (
    apply,
    lifecycle,
    loader,
    materials,
    remove,
    review,
    secret_files,
    store,
    verify,
)
from agent_sessions.playbooks.errors import PlaybookFormatError
from test_playbook_fleet import _write
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY, files
from test_playbook_review import setup as _review_setup

setup = _review_setup
SECRET = "forge-token-value-do-not-leak"
RULES = "Check {{endpoint}}. The token is in {{secret_path:token}}.\n"


def bundle(rules=RULES, version=3, **extra):
    contents = files(version)
    contents["template/RULES.md"] = rules
    contents.update(extra)
    return contents


@pytest.fixture
def deployed(setup):
    """A format-3 playbook whose RULES.md names the token file, bound and ready to apply."""
    folder, body = setup
    revision = _write(bundle())
    entity = projects.create("Secret files", folders=[str(folder)], default_folder=str(folder))
    inputs = {
        **body,
        "revision": revision,
        "project_id": entity.id,
        "bindings": [{"name": "token", "kind": "secret", "value": SECRET}],
    }
    return folder, entity, inputs


def bind_and_apply(entity, inputs, n=1):
    plan = review.build("review-demo", inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
    bound = lifecycle.bind(
        entity.id, "review-demo", inputs, receipt["receipt"], f"bind-op-{n}0000", key=KEY
    )
    return apply.apply(entity.id, f"apply-op-{n}0000", bound["receipt"], key=KEY)


def token_file(entity) -> Path:
    return Path(secret_files.path(entity.id, "token"))


def swap_in(path: Path, text: str) -> None:
    """Another file under the same name. Written beside it and renamed over it, so the old inode
    is still allocated and the new one cannot reuse its number (an unlink-then-create can)."""
    other = path.with_name("operator-copy")
    other.write_text(text)
    os.replace(other, path)


def plaintext_under(directory: Path) -> list[Path]:
    """Every non-empty regular file (retired names stay in `.reap/`, emptied, never unlinked)."""
    return [p for p in directory.rglob("*") if p.is_file() and p.stat().st_size]


def no_staging(entity) -> bool:
    return not [p for p in token_file(entity).parent.iterdir() if p.name.startswith(".staging-")]


# --- the format ----------------------------------------------------------------------------------


def _validate(contents):
    loader.validate_named(store.tree_from_files(contents), "review-demo")


def test_a_format_3_template_material_may_name_a_declared_secret_file():
    _validate(bundle())


@pytest.mark.parametrize(
    ("contents", "needle"),
    [
        (bundle(version=2), "format 3 or later"),
        (bundle(rules="{{secret_path:endpoint}}\n"), "must name a declared secret variable"),
        (bundle(rules="{{secret_path:nothing}}\n"), "must name a declared secret variable"),
        (bundle(rules="{{token}}\n"), "interpolates the secret variable"),
    ],
)
def test_a_secret_file_reference_is_refused_outside_its_one_place(contents, needle):
    with pytest.raises(PlaybookFormatError, match=needle):
        _validate(contents)


def test_a_verbatim_material_or_a_flow_may_not_name_a_secret_file():
    verbatim = bundle()
    verbatim["playbook.toml"] = verbatim["playbook.toml"].replace("template = true\n", "")
    with pytest.raises(PlaybookFormatError, match="only be referenced from a template material"):
        _validate(verbatim)
    flow = bundle(rules="Check {{endpoint}}.\n")
    flow["flows/check.toml"] = flow["flows/check.toml"].replace(
        'title = "Check"', 'title = "Check {{secret_path:token}}"', 1
    )
    with pytest.raises(PlaybookFormatError, match="only be referenced from a template material"):
        _validate(flow)


def test_rendering_is_one_pass_so_a_text_value_never_becomes_a_secret_path():
    pb = {
        "variables": [{"name": "note", "kind": "text"}, {"name": "token", "kind": "secret"}],
        "materials": [{"path": "A.md", "kind": "file", "disposition": "managed", "template": True}],
    }
    tree = SimpleNamespace(files={"template/A.md": b"{{note}} | {{secret_path:token}}"})
    [out] = materials.render(pb, tree, {"note": "{{secret_path:token}}"}, {"token": "/s/token"})
    assert out.data == b"{{secret_path:token}} | /s/token"
    with pytest.raises(materials.MaterialError, match="no secret reference file"):
        materials.render(pb, tree, {"note": "x"}, {})


# --- review --------------------------------------------------------------------------------------


def test_review_shows_the_path_and_never_the_value(deployed):
    folder, entity, inputs = deployed
    plan = review.build("review-demo", inputs, key=KEY)
    path = secret_files.path(entity.id, "token")
    assert plan.public["secret_files"] == [{"name": "token", "path": path}]
    rules = next(m for m in plan.public["materials"] if m["path"] == "RULES.md")
    assert rules["after"]["text"] == f"Check https://example.com/health. The token is in {path}.\n"
    assert SECRET not in json.dumps(plan.public)
    assert not os.path.lexists(path)  # review writes nothing


def test_a_pre_project_review_of_a_playbook_with_secret_files_is_refused(deployed):
    _, _, inputs = deployed
    preview = {k: v for k, v in inputs.items() if k != "project_id"}
    with pytest.raises(store.Conflict, match="create the project first"):
        review.build("review-demo", preview, key=KEY)


# --- apply ---------------------------------------------------------------------------------------


def test_apply_writes_the_value_at_0600_in_a_private_directory_outside_the_project(deployed):
    folder, entity, inputs = deployed
    assert bind_and_apply(entity, inputs)["state"] == "applied"
    path = token_file(entity)
    assert path.read_text() == SECRET
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert str(path) in (folder / "RULES.md").read_text()
    assert not str(path).startswith(str(folder))
    state = record(entity.id)
    assert state["secret_files"] == {"token": [path.stat().st_dev, path.stat().st_ino]}
    assert SECRET not in json.dumps(state)  # the record holds the inode, never the value
    assert no_staging(entity)


def test_a_rebound_value_replaces_the_file_this_deployment_wrote(deployed):
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    rebound = {
        **inputs,
        "bindings": [{"name": "token", "kind": "secret", "value": "rotated-token-value"}],
    }
    bind_and_apply(entity, rebound, n=2)
    path = token_file(entity)
    assert path.read_text() == "rotated-token-value"
    assert record(entity.id)["secret_files"]["token"][1] == path.stat().st_ino
    assert no_staging(entity)


def test_a_file_that_is_not_the_recorded_one_is_never_replaced(deployed):
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    path = token_file(entity)
    swap_in(path, "operator's own")  # same name, another inode
    rebound = {
        **inputs,
        "bindings": [{"name": "token", "kind": "secret", "value": "rotated-token-value"}],
    }
    with pytest.raises(store.Conflict, match="not the secret file this deployment wrote"):
        bind_and_apply(entity, rebound, n=2)
    assert path.read_text() == "operator's own"
    assert no_staging(entity)  # the staged plaintext is never left behind
    assert apply.status(entity.id)["state"] == "interrupted"


def test_a_file_already_there_before_the_first_apply_is_never_adopted(deployed):
    _, entity, inputs = deployed
    path = token_file(entity)
    path.parent.mkdir(mode=0o700, parents=True)
    os.chmod(path.parent.parent, 0o700)
    path.write_text("someone else's")
    with pytest.raises(store.Conflict, match="not the secret file"):
        bind_and_apply(entity, inputs)
    assert path.read_text() == "someone else's"


def test_an_update_that_drops_the_reference_deletes_the_file(deployed):
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    revision = _write(bundle(rules="Check {{endpoint}}.\n"))
    bind_and_apply(entity, {**inputs, "revision": revision}, n=2)
    assert not token_file(entity).exists()
    assert record(entity.id)["secret_files"] == {}


def bind_only(entity, inputs):
    plan = review.build("review-demo", inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
    return lifecycle.bind(
        entity.id, "review-demo", inputs, receipt["receipt"], "bind-op-10000", key=KEY
    )["receipt"]


def apply_once(entity, receipt):
    return apply.apply(entity.id, "apply-op-10000", receipt, key=KEY)


def crash_after_publication(monkeypatch):
    """The publication happened; the `done` checkpoint never became durable."""
    real = secret_files.write

    def write(top, pid, name, value, effect, recorded, progress, *rest):
        def lost(*a):
            if effect["phase"] == "done":
                raise OSError("simulated crash before the journal settled")
            progress()

        return real(top, pid, name, value, effect, recorded, lost, *rest)

    monkeypatch.setattr(secret_files, "write", write)


def test_a_crash_after_publication_settles_on_the_same_id_retry(deployed, monkeypatch):
    _, entity, inputs = deployed
    receipt = bind_only(entity, inputs)
    with monkeypatch.context() as patch:
        crash_after_publication(patch)
        with pytest.raises(store.StoreError):
            apply_once(entity, receipt)
    journal = record(entity.id)["apply_operation"]["secrets"]["token"]
    assert journal["phase"] == "staged"  # the published file is ours by its journaled inode
    assert apply_once(entity, receipt)["state"] == "applied"
    assert token_file(entity).read_text() == SECRET
    assert record(entity.id)["secret_files"]["token"] == journal["inode"]
    assert no_staging(entity)


def test_a_crash_after_publication_then_remove_deletes_the_published_file(deployed, monkeypatch):
    """Hermes 5749 #1: the journal says `staged` but the final name already holds its inode."""
    _, entity, inputs = deployed
    receipt = bind_only(entity, inputs)
    with monkeypatch.context() as patch:
        crash_after_publication(patch)
        with pytest.raises(store.StoreError):
            apply_once(entity, receipt)
    assert token_file(entity).read_text() == SECRET
    plan = remove.plan(entity.id, key=KEY)
    assert [f["name"] for f in plan["secret_files"]] == ["token"]
    result = remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert result["kept_secret_files"] == []
    assert not token_file(entity).exists() and no_staging(entity)


def test_a_crash_before_publication_leaves_only_a_journaled_staging_file(deployed, monkeypatch):
    _, entity, inputs = deployed
    receipt = bind_only(entity, inputs)
    with monkeypatch.context() as patch:
        # Only this module's publication fails; apply's own directory creation is untouched.
        patch.setattr(
            secret_files,
            "renameat",
            SimpleNamespace(
                RENAME_NOREPLACE=1,
                RENAME_EXCHANGE=2,
                renameat2=lambda *a: (_ for _ in ()).throw(OSError("simulated crash")),
            ),
        )
        with pytest.raises(store.StoreError):
            apply_once(entity, receipt)
    assert not token_file(entity).exists()
    assert not no_staging(entity)
    # Removing the interrupted deployment reaps that staged inode as well.
    plan = remove.plan(entity.id, key=KEY)
    remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert no_staging(entity)


def test_a_crash_before_the_staging_link_leaves_no_name_at_all(deployed, monkeypatch):
    """The inode is journaled before any name refers to it; an unlinked O_TMPFILE simply goes."""
    _, entity, inputs = deployed
    receipt = bind_only(entity, inputs)
    with monkeypatch.context() as patch:
        patch.setattr(
            secret_files, "_link", lambda *a: (_ for _ in ()).throw(OSError("simulated crash"))
        )
        with pytest.raises(store.StoreError):
            apply_once(entity, receipt)
    assert sorted(p.name for p in token_file(entity).parent.iterdir()) == []
    assert apply_once(entity, receipt)["state"] == "applied"  # the retry stages afresh
    assert token_file(entity).read_text() == SECRET and no_staging(entity)


def test_a_replaced_staging_entry_is_never_published_or_deleted(deployed, monkeypatch):
    """Hermes 5749 #5: a staging NAME proves nothing; only its journaled inode does."""
    _, entity, inputs = deployed
    real = secret_files._stage

    def stage_then_swap(fd, value, effect, progress):
        real(fd, value, effect, progress)
        swap_in(token_file(entity).parent / effect["staging"], "someone else's")

    with monkeypatch.context() as patch:
        patch.setattr(secret_files, "_stage", stage_then_swap)
        with pytest.raises(store.Conflict, match="staged secret file was replaced"):
            bind_and_apply(entity, inputs)
    assert not token_file(entity).exists()
    [left] = [p for p in token_file(entity).parent.iterdir() if p.name.startswith(".staging-")]
    assert left.read_text() == "someone else's"  # not ours: left exactly as it is


def test_a_moved_secrets_directory_never_receives_the_write(deployed, monkeypatch, tmp_path):
    """Hermes 5749 #4: the descriptors must still be the directories at the reviewed path."""
    _, entity, inputs = deployed
    real = secret_files._stage
    displaced = tmp_path / "displaced"

    def stage_then_move(fd, value, effect, progress):
        real(fd, value, effect, progress)
        project = token_file(entity).parent
        os.rename(project, displaced)
        project.mkdir(mode=0o700)

    with monkeypatch.context() as patch:
        patch.setattr(secret_files, "_stage", stage_then_move)
        with pytest.raises(store.Conflict, match="directory moved"):
            bind_and_apply(entity, inputs)
    assert plaintext_under(displaced) == []  # the staged plaintext was scrubbed through the fd


def test_a_file_replaced_before_settlement_never_settles(deployed, monkeypatch):
    """Hermes 5749 #6: settlement re-reads every written name by path."""
    _, entity, inputs = deployed
    real = secret_files.settle

    def swap_then_settle(top, pid, owned, folders):
        swap_in(token_file(entity), SECRET)
        return real(top, pid, owned, folders)

    with monkeypatch.context() as patch:
        patch.setattr(secret_files, "settle", swap_then_settle)
        with pytest.raises(store.Conflict, match="changed before the apply record settled"):
            bind_and_apply(entity, inputs)
    assert apply.status(entity.id)["state"] == "interrupted"
    assert record(entity.id).get("secret_files", {}) == {}


def test_a_secret_rotated_after_review_is_never_written(deployed, monkeypatch):
    """Hermes 5749 #7: the value written must be the revision the accepted review saw."""
    _, entity, inputs = deployed
    receipt = bind_only(entity, inputs)
    real = template_vars.resolver

    def rotated(pid):
        r = real(pid)
        state = r.secret_state

        r.secret_state = lambda name: {**state(name), "revision": "rotated-since-review"}
        return r

    with monkeypatch.context() as patch:
        patch.setattr(apply.lifecycle.template_vars, "resolver", rotated)
        with pytest.raises(store.Conflict, match="secret changed since the review"):
            apply_once(entity, receipt)
    assert not token_file(entity).exists()


def test_a_changed_root_setting_never_strands_the_files(deployed, monkeypatch, tmp_path):
    """Hermes 5749 #3: the root a deployment wrote under is recorded; remove uses it."""
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    original = token_file(entity)
    monkeypatch.setenv(secret_files.ROOT_ENV, str(tmp_path / "elsewhere"))
    with pytest.raises(store.StoreError, match="secrets root is now"):
        review.build("review-demo", inputs, key=KEY)
    plan = remove.plan(entity.id, key=KEY)
    assert plan["secret_files"] == [{"name": "token", "path": str(original)}]
    remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert not original.exists()


def test_a_directory_that_is_not_private_refuses(deployed):
    _, entity, inputs = deployed
    root = Path(secret_files.root())
    root.mkdir(mode=0o755, parents=True)
    os.chmod(root, 0o755)
    with pytest.raises(store.Conflict, match="not private"):
        bind_and_apply(entity, inputs)
    assert not token_file(entity).exists()


# --- remove and verify ---------------------------------------------------------------------------


def test_remove_deletes_the_file_it_wrote(deployed):
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    plan = remove.plan(entity.id, key=KEY)
    assert plan["secret_files"] == [{"name": "token", "path": str(token_file(entity))}]
    result = remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert result["kept_secret_files"] == []
    assert not token_file(entity).exists()
    assert record(entity.id)["secret_files"] == {}


def test_remove_keeps_a_file_that_is_no_longer_the_one_it_wrote(deployed):
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    path = token_file(entity)
    swap_in(path, "operator's own")
    plan = remove.plan(entity.id, key=KEY)
    result = remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert result["kept_secret_files"] == [str(path)]
    assert path.read_text() == "operator's own"


def test_verify_reports_a_replaced_secret_file_as_drift_without_reading_it(deployed):
    _, entity, inputs = deployed
    contents = bundle()
    contents["playbook.toml"] = contents["playbook.toml"].replace(
        "format = 3\n", 'format = 3\nverify = ["materials"]\n', 1
    )
    inputs = {**inputs, "revision": _write(contents)}
    bind_and_apply(entity, inputs)
    assert verify.verify(entity.id)["checks"]["materials"]["ok"] is True
    path = token_file(entity)
    swap_in(path, SECRET)  # identical bytes, another inode: not the file apply wrote
    check = verify.verify(entity.id)["checks"]["materials"]
    assert check == {"ok": False, "drift": [str(path)]}


def test_the_secret_value_never_reaches_a_response(deployed, auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    c = TestClient(create_app(auth_cfg), base_url=auth_cfg.origin)
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    for route in ("", "/verify"):
        response = c.get(f"/api/projects/{entity.id}/playbook{route}")
        assert response.status_code == 200, response.text
        assert SECRET not in response.text
    assert template_vars.resolver(entity.id).secret("token") == SECRET


@pytest.mark.parametrize("replacement", [True, False])
def test_removal_refuses_while_the_secrets_directory_is_displaced(deployed, tmp_path, replacement):
    """Hermes 5751 #1: a name absent from a replacement (or missing) directory proves nothing."""
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    project = token_file(entity).parent
    displaced = tmp_path / "displaced"
    os.rename(project, displaced)
    if replacement:
        project.mkdir(mode=0o700)
    plan = remove.plan(entity.id, key=KEY)
    with pytest.raises(store.Conflict, match="not the one this deployment wrote"):
        remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert record(entity.id)["secret_files"]  # nothing forgotten
    assert (displaced / "token").read_text() == SECRET
    # Recoverable: put the original back and the same removal settles.
    if replacement:
        project.rmdir()
    os.rename(displaced, project)
    remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert not token_file(entity).exists()


def test_an_update_refuses_a_replaced_secrets_directory(deployed):
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    project = token_file(entity).parent
    os.rename(project, project.with_name("displaced"))
    project.mkdir(mode=0o700)
    rebound = {
        **inputs,
        "bindings": [{"name": "token", "kind": "secret", "value": "rotated-token-value"}],
    }
    with pytest.raises(store.Conflict, match="not the one this deployment wrote"):
        bind_and_apply(entity, rebound, n=2)
    assert list(project.iterdir()) == []


@pytest.mark.parametrize("via_symlink", [False, True])
def test_a_secrets_root_inside_a_project_folder_is_refused(
    deployed, monkeypatch, tmp_path, via_symlink
):
    """Hermes 5751 #2: the value must live outside every workspace."""
    folder, entity, inputs = deployed
    if via_symlink:
        (tmp_path / "alias").symlink_to(folder)
        inside = tmp_path / "alias" / "secrets"
    else:
        inside = folder / "secrets"
    monkeypatch.setenv(secret_files.ROOT_ENV, str(inside))
    with pytest.raises(store.StoreError, match="inside the project folder"):
        review.build("review-demo", inputs, key=KEY)
    assert not inside.exists()


def test_apply_rechecks_the_root_against_every_project_folder(deployed, monkeypatch):
    folder, entity, inputs = deployed
    receipt = bind_only(entity, inputs)
    with monkeypatch.context() as patch:
        # The review passed; a project folder that now contains the root is caught at the write.
        patch.setattr(secret_files, "root", lambda: str(folder / "secrets"))
        patch.setattr(secret_files, "check_root", lambda record: None)
        with pytest.raises(store.Conflict, match="inside the project folder"):
            apply_once(entity, receipt)
    assert not (folder / "secrets").exists()


def test_a_root_above_a_project_folder_named_like_the_project_is_refused(
    deployed, monkeypatch, tmp_path
):
    """Hermes 5756 #1: what must be outside every project is `<root>/<project id>`, not the root."""
    _, entity, inputs = deployed
    parent = tmp_path / "workspace-parent"
    (parent / entity.id).mkdir(parents=True)
    projects.create("Named like the id", folders=[str(parent / entity.id)])
    monkeypatch.setenv(secret_files.ROOT_ENV, str(parent))
    with pytest.raises(store.StoreError, match="inside the project folder"):
        review.build("review-demo", inputs, key=KEY)
    assert list((parent / entity.id).iterdir()) == []


def _swap_on_quarantine(monkeypatch, target):
    """Install an operator file under `target()` in the last moment: after the inode check,
    immediately before the name is moved into `.reap/`."""
    real = secret_files.renameat

    def renameat2(src_fd, src, dst_fd, dst, flags):
        if dst.startswith("r-"):
            name = target()
            if name is not None and name.name == src:
                swap_in(name, "operator's own")
        return real.renameat2(src_fd, src, dst_fd, dst, flags)

    monkeypatch.setattr(
        secret_files,
        "renameat",
        SimpleNamespace(
            RENAME_NOREPLACE=real.RENAME_NOREPLACE,
            RENAME_EXCHANGE=real.RENAME_EXCHANGE,
            renameat2=renameat2,
        ),
    )


def test_a_file_swapped_in_at_the_last_moment_is_never_deleted(deployed, monkeypatch):
    """Hermes 5756 #2: the name is moved, the moved file judged by descriptor, a foreign one
    given back; nothing is ever unlinked."""
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    plan = remove.plan(entity.id, key=KEY)
    with monkeypatch.context() as patch:
        armed = [token_file(entity)]
        _swap_on_quarantine(patch, lambda: armed.pop() if armed else None)
        result = remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert result["kept_secret_files"] == [str(token_file(entity))]
    assert token_file(entity).read_text() == "operator's own"


def test_a_staging_entry_swapped_in_at_the_last_moment_is_never_deleted(deployed, monkeypatch):
    _, entity, inputs = deployed
    receipt = bind_only(entity, inputs)
    with monkeypatch.context() as patch:
        patch.setattr(
            secret_files,
            "renameat",
            SimpleNamespace(
                RENAME_NOREPLACE=1,
                RENAME_EXCHANGE=2,
                renameat2=lambda *a: (_ for _ in ()).throw(OSError("simulated crash")),
            ),
        )
        with pytest.raises(store.StoreError):
            apply_once(entity, receipt)
    staging = record(entity.id)["apply_operation"]["secrets"]["token"]["staging"]
    plan = remove.plan(entity.id, key=KEY)
    with monkeypatch.context() as patch:
        armed = [token_file(entity).parent / staging]
        _swap_on_quarantine(patch, lambda: armed.pop() if armed else None)
        remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert (token_file(entity).parent / staging).read_text() == "operator's own"


def test_a_crash_between_retiring_a_name_and_scrubbing_it_is_found_by_inode(deployed, monkeypatch):
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    plan = remove.plan(entity.id, key=KEY)
    real = secret_files._scrub

    def crash_on_retired(fd, name, proven):
        if name.startswith("r-"):
            raise OSError("simulated crash after the move")
        return real(fd, name, proven)

    with monkeypatch.context() as patch:
        patch.setattr(secret_files, "_scrub", crash_on_retired)
        with pytest.raises(store.StoreError):
            remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    project = token_file(entity).parent
    assert [p.read_text() for p in plaintext_under(project)] == [SECRET]  # in .reap/, proven
    assert record(entity.id)["secret_files"]  # nothing forgotten
    remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)  # same-id retry
    assert plaintext_under(project) == []


def test_a_symlinked_ancestor_retargeted_after_the_check_never_carries_the_file_inside(
    deployed, monkeypatch, tmp_path
):
    """Hermes 5759 #1: containment is judged on the directory actually held, not the path."""
    folder, entity, inputs = deployed
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside)
    monkeypatch.setenv(secret_files.ROOT_ENV, str(alias / "secrets"))
    receipt = bind_only(entity, inputs)
    real = secret_files.directory

    def retarget_then_create(top, pid):
        # After apply's last path-based containment check, before the directory is created.
        alias.unlink()
        alias.symlink_to(folder)  # now <alias>/secrets/<pid> lies inside the project
        return real(top, pid)

    with monkeypatch.context() as patch:
        patch.setattr(secret_files, "directory", retarget_then_create)
        with pytest.raises(store.Conflict, match="inside the project folder"):
            apply_once(entity, receipt)
    assert not (folder / "secrets").exists() or plaintext_under(folder / "secrets") == []


@pytest.mark.parametrize("mode", [0o400, 0o000])
def test_a_read_only_owned_file_is_still_scrubbed_on_removal(deployed, mode):
    """Hermes 5759 #2: a proven inode is made writable and truncated through its descriptor."""
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    os.chmod(token_file(entity), mode)
    plan = remove.plan(entity.id, key=KEY)
    result = remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert result["kept_secret_files"] == []
    assert not token_file(entity).exists()
    assert plaintext_under(token_file(entity).parent) == []


def test_a_proven_file_that_cannot_be_scrubbed_refuses_and_keeps_its_proof(deployed, monkeypatch):
    _, entity, inputs = deployed
    bind_and_apply(entity, inputs)
    plan = remove.plan(entity.id, key=KEY)
    with monkeypatch.context() as patch:
        patch.setattr(
            secret_files, "_writable", lambda b: (_ for _ in ()).throw(PermissionError("immutable"))
        )
        with pytest.raises(store.StoreError):
            remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)
    assert record(entity.id)["secret_files"]  # never forgotten while plaintext remains
    assert [p.read_text() for p in plaintext_under(token_file(entity).parent)] == [SECRET]
    remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)  # same-id recovery
    assert plaintext_under(token_file(entity).parent) == []
