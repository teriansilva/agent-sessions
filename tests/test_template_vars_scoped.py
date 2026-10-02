"""The variables store v3: scoped identities, legacy envelopes, project bindings (#1191 PR 1).

Pins the round-1 contract of #1191 and #1096 §3: a v1/v2 store reads as global with ``legacy``
(bare-name AAD) envelopes that still decrypt; the envelope's own marker decides its AAD and nothing
is guessed; a legacy envelope is re-encrypted on its next write; resolution is project → global →
default for text, and a secret never crosses scopes without a recorded ``ref``; a binding that
cannot be used refuses instead of falling back; one fence decides a bind racing a global delete; and
removing one project's bindings leaves the other project and the global entry untouched.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from agent_sessions import template_secrets, template_vars
from agent_sessions import templates as tstore
from agent_sessions.main import create_app

SECRET = "hunter22-staging-db"  # noqa: S105 — test fixture values
SECRET_A = "project-a-token-111"  # noqa: S105
SECRET_B = "project-b-token-222"  # noqa: S105
SECRET_G = "global-token-33333"  # noqa: S105


@pytest.fixture(autouse=True)
def _fresh_redaction():
    template_secrets._reset_for_tests()
    yield
    template_secrets._reset_for_tests()


def _doc() -> dict:
    return json.loads(template_vars.store_path().read_text())


def _write(doc: dict) -> None:
    path = template_vars.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def _v2_store() -> dict:
    """A store exactly as the v2 build wrote it: bare-name AAD, no scope, no marker."""
    env = template_secrets.encrypt("db_pass", SECRET)
    assert set(env) == {"kid", "nonce", "ct"}
    doc = {
        "version": 2,
        "variables": [
            {"name": "db_pass", "kind": "secret", "secret": env, "created_at": 1, "updated_at": 1},
            {"name": "host", "kind": "text", "value": "a.test", "created_at": 1, "updated_at": 1},
        ],
    }
    _write(doc)
    return env


def _by(doc: dict, name: str, pid: str | None = None) -> dict:
    [rec] = [r for r in doc["variables"] if r["name"] == name and r.get("project_id") == pid]
    return rec


# ---- migration ---------------------------------------------------------------------------------


def test_existing_encrypted_globals_decrypt_after_upgrade(tmp_home):
    env = _v2_store()
    assert template_vars.secret_values() == {"db_pass": SECRET}
    assert template_vars.secret_state() == {"db_pass": "ok"}
    assert template_vars.values() == {"host": "a.test"}
    # The first accepted write publishes v3: every record global, the old envelope KEPT byte for
    # byte and marked legacy — and it still decrypts.
    template_vars.create_variable({"name": "other", "value": "x"})
    doc = _doc()
    assert doc["version"] == 3
    rec = _by(doc, "db_pass")
    assert rec["scope"] == "global" and rec["project_id"] is None
    assert rec["secret"] == {**env, "aad": "legacy"}
    assert template_vars.secret_values() == {"db_pass": SECRET}
    assert SECRET in template_secrets.redaction_values()


def test_a_legacy_envelope_is_re_encrypted_under_its_scoped_aad_on_its_next_write(tmp_home):
    env = _v2_store()
    [row] = [r for r in template_vars.list_variables() if r["name"] == "db_pass"]
    template_vars.update_variable("db_pass", {"value": "rotated-secret-1"}, row["updated_at"])
    rec = _by(_doc(), "db_pass")
    assert rec["secret"]["aad"] == "scoped" and rec["secret"]["ct"] != env["ct"]
    assert template_vars.secret_values() == {"db_pass": "rotated-secret-1"}


@pytest.mark.parametrize("flip", ["scoped-marked-legacy", "legacy-marked-scoped"])
def test_the_marker_decides_the_aad_and_nothing_is_guessed(tmp_home, flip):
    if flip == "scoped-marked-legacy":
        template_vars.create_variable({"name": "db_pass", "kind": "secret", "value": SECRET})
        doc = _doc()
        _by(doc, "db_pass")["secret"]["aad"] = "legacy"
    else:
        _v2_store()
        template_vars.create_variable({"name": "other", "value": "x"})  # publish v3 (legacy kept)
        doc = _doc()
        _by(doc, "db_pass")["secret"]["aad"] = "scoped"
    _write(doc)
    # Either envelope WOULD decrypt under the other AAD; the wrong marker must not be "tried".
    assert template_vars.secret_state() == {"db_pass": "reentry"}
    assert template_vars.secret_values() == {}


def test_a_v3_envelope_without_a_marker_or_a_record_without_a_scope_is_damaged(tmp_home):
    template_vars.create_variable({"name": "db_pass", "kind": "secret", "value": SECRET})
    template_vars.create_variable({"name": "host", "value": "a.test"})
    doc = _doc()
    del _by(doc, "db_pass")["secret"]["aad"]
    del _by(doc, "host")["scope"]
    _write(doc)
    assert template_vars.list_variables() == []
    with pytest.raises(template_vars.ResolutionUnavailable):
        template_vars.resolver("p-aaaa0001")


def test_decryption_never_uses_a_legacy_aad_outside_the_global_scope(tmp_home):
    """Defence in depth under the store's own record check: `decrypt_record` itself refuses a
    legacy envelope for a project identity, though the bare-name AAD would authenticate it."""
    env = {**template_secrets.encrypt("tok", SECRET), "aad": "legacy"}
    assert template_secrets.decrypt_record("global", None, "tok", env) == SECRET
    assert template_secrets.decrypt_record("project", "p-aaaa0001", "tok", env) is None
    assert template_secrets.decrypt_record_checked("project", "p-aaaa0001", "tok", env) is None


def test_a_global_secret_blocks_the_text_default_of_the_same_name(tmp_home):
    template_vars.create_variable({"name": "host", "kind": "secret", "value": SECRET_G})
    with pytest.raises(template_vars.BindingUnusable):
        template_vars.resolver("p-aaaa0001").text("host", "fallback.test")


def test_an_older_build_refuses_a_v3_file_and_writes_nothing(tmp_home, monkeypatch):
    template_vars.create_variable({"name": "host", "value": "a.test"})
    before = template_vars.store_path().read_bytes()
    monkeypatch.setattr(template_vars, "STORE_VERSION", 2)  # what the v2 build does with it
    assert template_vars.list_variables() == []
    with pytest.raises(template_vars.VariableStoreUnsupported):
        template_vars.create_variable({"name": "other", "value": "x"})
    with pytest.raises(template_vars.VariableStoreUnsupported):
        template_vars.secret_values_checked()  # redaction refuses: fail closed
    assert template_vars.store_path().read_bytes() == before


def test_a_legacy_envelope_on_a_project_record_never_decrypts(tmp_home):
    env = _v2_store()
    template_vars.bind_project("p-aaaa0001", [{"name": "tok", "kind": "secret", "value": SECRET_A}])
    doc = _doc()
    rec = _by(doc, "tok", "p-aaaa0001")
    # A legacy envelope moved onto a project record: it would decrypt under its bare name.
    rec["name"] = "db_pass"
    rec["secret"] = {**env, "aad": "legacy"}
    _write(doc)
    assert template_vars.project_bindings("p-aaaa0001") == []  # refused as a damaged record
    with pytest.raises(template_vars.ResolutionUnavailable):
        template_vars.resolver("p-aaaa0001").secret("db_pass")


@pytest.mark.parametrize("move", ["project-to-project", "project-to-global", "global-to-project"])
def test_an_envelope_moved_across_scopes_does_not_decrypt(tmp_home, move):
    template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    template_vars.bind_project("p-aaaa0001", [{"name": "tok", "kind": "secret", "value": SECRET_A}])
    template_vars.bind_project("p-bbbb0002", [{"name": "tok", "kind": "secret", "value": SECRET_B}])
    doc = _doc()
    a, b, g = _by(doc, "tok", "p-aaaa0001"), _by(doc, "tok", "p-bbbb0002"), _by(doc, "tok")
    src, dst = {
        "project-to-project": (a, b),
        "project-to-global": (a, g),
        "global-to-project": (g, a),
    }[move]
    dst["secret"] = dict(src["secret"])
    _write(doc)
    if dst is g:
        assert template_vars.secret_state() == {"tok": "reentry"}
    else:
        pid = dst["project_id"]
        assert template_vars.resolver(pid).secret_state("tok")["state"] == "reentry"
        with pytest.raises(template_vars.BindingUnusable):
            template_vars.resolver(pid).secret("tok")


# ---- resolution --------------------------------------------------------------------------------


def test_text_resolves_project_then_global_then_default(tmp_home):
    template_vars.create_variable({"name": "host", "value": "global.test"})
    template_vars.bind_project("p-aaaa0001", [{"name": "host", "kind": "text", "value": "a.test"}])
    a = template_vars.resolver("p-aaaa0001")
    assert a.text("host") == {
        "value": "a.test",
        "source": "project",
        "revision": a.text("host")["revision"],
    }
    other = template_vars.resolver("p-bbbb0002")
    assert other.text("host")["value"] == "global.test" and other.text("host")["source"] == "global"
    assert other.text("port", "22") == {"value": "22", "source": "default", "revision": None}
    with pytest.raises(template_vars.BindingMissing):
        other.text("port")


def test_a_secret_never_falls_back_to_the_global_one_without_a_recorded_ref(tmp_home):
    template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    with pytest.raises(template_vars.BindingMissing):
        template_vars.resolver("p-aaaa0001").secret("tok")
    template_vars.bind_project("p-aaaa0001", [{"name": "tok", "kind": "secret", "ref": "global"}])
    r = template_vars.resolver("p-aaaa0001")
    assert r.secret("tok") == SECRET_G
    assert r.secret_state("tok")["source"] == "global"


def test_a_binding_that_needs_re_entry_refuses_and_never_falls_back(tmp_home):
    template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    template_vars.create_variable({"name": "host", "value": "global.test"})
    template_vars.bind_project(
        "p-aaaa0001",
        [
            {"name": "tok", "kind": "secret", "value": SECRET_A},
            {"name": "host", "kind": "secret", "value": SECRET_B},
        ],
    )
    doc = _doc()
    rec = _by(doc, "tok", "p-aaaa0001")
    rec["secret"]["ct"] = _by(doc, "tok")["secret"]["ct"]  # tampered: fails authentication
    _write(doc)
    r = template_vars.resolver("p-aaaa0001")
    assert r.secret_state("tok")["state"] == "reentry"
    with pytest.raises(template_vars.BindingUnusable) as e:
        r.secret("tok")
    assert e.value.name == "tok" and SECRET_G not in str(e.value)
    # A project binding of the OTHER kind blocks the global text value too.
    with pytest.raises(template_vars.BindingUnusable):
        r.text("host")
    # The global itself is untouched and still resolves for a project that did not bind it.
    assert template_vars.secret_values() == {"tok": SECRET_G}


def test_a_ref_to_a_global_that_needs_re_entry_refuses(tmp_home):
    template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    template_vars.bind_project("p-aaaa0001", [{"name": "tok", "kind": "secret", "ref": "global"}])
    doc = _doc()
    _by(doc, "tok")["secret"]["kid"] = "00000000"
    _write(doc)
    with pytest.raises(template_vars.BindingUnusable):
        template_vars.resolver("p-aaaa0001").secret("tok")


def test_the_two_project_regression(tmp_home):
    """#1096 §3's named regression: two projects, same names, different values (text + secret);
    remove one project's bindings; the other project's values and the global entries survive and
    still resolve."""
    template_vars.create_variable({"name": "host", "value": "global.test"})
    template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    for pid, host, tok in (("p-aaaa0001", "a.test", SECRET_A), ("p-bbbb0002", "b.test", SECRET_B)):
        template_vars.bind_project(
            pid,
            [
                {"name": "host", "kind": "text", "value": host},
                {"name": "tok", "kind": "secret", "value": tok},
            ],
        )
    a, b = template_vars.resolver("p-aaaa0001"), template_vars.resolver("p-bbbb0002")
    assert (a.text("host")["value"], a.secret("tok")) == ("a.test", SECRET_A)
    assert (b.text("host")["value"], b.secret("tok")) == ("b.test", SECRET_B)

    assert template_vars.remove_project_bindings("p-aaaa0001") == ["host", "tok"]

    b = template_vars.resolver("p-bbbb0002")
    assert (b.text("host")["value"], b.secret("tok")) == ("b.test", SECRET_B)
    assert template_vars.values() == {"host": "global.test"}
    assert template_vars.secret_values() == {"tok": SECRET_G}
    a = template_vars.resolver("p-aaaa0001")
    assert a.text("host") == {
        "value": "global.test",
        "source": "global",
        "revision": a.text("host")["revision"],
    }
    with pytest.raises(template_vars.BindingMissing):
        a.secret("tok")  # its own secret is gone, and the global one is not silently used
    # The removed secret stays redacted: it is still in the transcripts it was pasted into.
    assert SECRET_A in template_secrets.redaction_values()


def test_removing_named_bindings_touches_only_those(tmp_home):
    template_vars.bind_project(
        "p-aaaa0001",
        [
            {"name": "host", "kind": "text", "value": "a"},
            {"name": "port", "kind": "text", "value": "1"},
        ],
    )
    assert template_vars.remove_project_bindings("p-aaaa0001", ["port"]) == ["port"]
    assert [b["name"] for b in template_vars.project_bindings("p-aaaa0001")] == ["host"]


# ---- dependency-aware deletion under one fence --------------------------------------------------


def test_deleting_a_global_that_a_project_refers_to_is_refused_listing_it(tmp_home):
    rec = template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    template_vars.bind_project("p-aaaa0001", [{"name": "tok", "kind": "secret", "ref": "global"}])
    with pytest.raises(template_vars.VariableInUse) as e:
        template_vars.delete_variable("tok", rec["updated_at"])
    assert e.value.projects == ["p-aaaa0001"] and e.value.dependants == []
    template_vars.remove_project_bindings("p-aaaa0001")
    template_vars.delete_variable("tok", rec["updated_at"])
    assert template_vars.secret_values() == {}


def test_a_ref_needs_the_global_of_that_name_and_kind(tmp_home):
    template_vars.create_variable({"name": "host", "value": "g"})
    for bad in (
        {"name": "tok", "kind": "secret", "ref": "global"},  # no such global
        {"name": "host", "kind": "secret", "ref": "global"},  # the global is text
    ):
        with pytest.raises(template_vars.VariableError, match="no global"):
            template_vars.bind_project("p-aaaa0001", [bad])
    assert template_vars.project_bindings("p-aaaa0001") == []


def _race(first: str, monkeypatch):
    """Run a delete of global ``tok`` and a ``ref`` bind to it, with ``first`` holding the fence
    (paused INSIDE the locked section) while the other one starts."""
    rec = template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    inside, release = threading.Event(), threading.Event()
    results: dict[str, object] = {}

    def pause():
        inside.set()
        assert release.wait(10)

    if first == "delete":
        real = tstore.library_references_checked

        def slow(name):
            pause()
            return real(name)

        monkeypatch.setattr(tstore, "library_references_checked", slow)
        bind = [{"name": "tok", "kind": "secret", "ref": "global"}]
    else:
        real_enc = template_secrets.encrypt_scoped

        def slow_enc(*a):
            pause()
            return real_enc(*a)

        monkeypatch.setattr(template_secrets, "encrypt_scoped", slow_enc)
        # The ref is checked, then the second binding's encryption pauses inside the fence.
        bind = [
            {"name": "tok", "kind": "secret", "ref": "global"},
            {"name": "own", "kind": "secret", "value": SECRET_A},
        ]

    def do_delete():
        try:
            template_vars.delete_variable("tok", rec["updated_at"])
            results["delete"] = "ok"
        except Exception as e:  # noqa: BLE001
            results["delete"] = e

    def do_bind():
        try:
            template_vars.bind_project("p-aaaa0001", bind)
            results["bind"] = "ok"
        except Exception as e:  # noqa: BLE001
            results["bind"] = e

    t1 = threading.Thread(target=do_delete if first == "delete" else do_bind)
    t2 = threading.Thread(target=do_bind if first == "delete" else do_delete)
    t1.start()
    assert inside.wait(10)
    t2.start()
    t2.join(0.3)
    assert t2.is_alive(), "the second writer must wait on the fence the first one holds"
    release.set()
    t1.join(10)
    t2.join(10)
    return results


def test_a_bind_racing_a_global_delete_loses_when_the_delete_holds_the_fence(tmp_home, monkeypatch):
    r = _race("delete", monkeypatch)
    assert r["delete"] == "ok"
    assert isinstance(r["bind"], template_vars.VariableError) and "no global" in str(r["bind"])
    assert template_vars.project_bindings("p-aaaa0001") == []


def test_a_global_delete_racing_a_bind_loses_when_the_bind_holds_the_fence(tmp_home, monkeypatch):
    r = _race("bind", monkeypatch)
    assert r["bind"] == "ok"
    assert isinstance(r["delete"], template_vars.VariableInUse)
    assert r["delete"].projects == ["p-aaaa0001"]
    assert template_vars.resolver("p-aaaa0001").secret("tok") == SECRET_G


def test_the_delete_route_names_the_projects(tmp_home, auth_cfg):
    rec = template_vars.create_variable({"name": "host", "value": "g"})
    template_vars.bind_project("p-aaaa0001", [{"name": "host", "kind": "text", "ref": "global"}])
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert (
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        ).status_code
        == 303
    )
    csrf = c.get("/api/config").json()["csrf"]
    r = c.delete(
        f"/api/template-variables/host?expected_updated_at={rec['updated_at']}",
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 409
    assert r.json()["projects"] == ["p-aaaa0001"] and "1 project" in r.json()["detail"]


# ---- scopes stay out of the global views; input is strict --------------------------------------


def test_project_bindings_never_appear_in_the_global_library(tmp_home):
    template_vars.bind_project(
        "p-aaaa0001",
        [
            {"name": "host", "kind": "text", "value": "a.test"},
            {"name": "tok", "kind": "secret", "value": SECRET_A},
        ],
    )
    assert template_vars.list_variables() == []
    assert template_vars.values() == {} and template_vars.secret_state() == {}
    assert template_vars.secret_values() == {} and template_vars.revisions() == {}
    # …but redaction covers every scope.
    assert SECRET_A in template_secrets.redaction_values()
    # A global of the same name is independent of the project's.
    template_vars.create_variable({"name": "host", "value": "global.test"})
    assert template_vars.values() == {"host": "global.test"}


@pytest.mark.parametrize(
    "pid", ["", "P-1", "a:b", "__default__", "../x", "p/1", "p-²", "p\n", "x" * 65, None, 7]
)
def test_a_project_id_is_validated_before_anything_is_stored(tmp_home, pid):
    with pytest.raises(template_vars.VariableError):
        template_vars.bind_project(pid, [{"name": "host", "kind": "text", "value": "a"}])
    assert not template_vars.store_path().exists()


@pytest.mark.parametrize(
    "binding",
    [
        {"name": "host", "kind": "text", "value": []},
        {"name": "host", "kind": "text", "value": {"x": 1}},
        {"name": "host", "kind": "text", "value": "a", "ref": "global"},
        {"name": "host", "kind": "text"},
        {"name": "host", "kind": "text", "ref": "project"},
        {"name": "host", "kind": "text", "value": "a", "scope": "global"},
        {"name": "Host", "kind": "text", "value": "a"},
        {"name": ["host"], "kind": "text", "value": "a"},
        {"name": "tok", "kind": "secret", "value": "short"},
        {"name": "host", "kind": "code", "value": "a"},
        "host",
    ],
)
def test_a_binding_is_parsed_strictly(tmp_home, binding):
    with pytest.raises(template_vars.VariableError):
        template_vars.bind_project("p-aaaa0001", [binding])
    assert not template_vars.store_path().exists()


def test_a_binding_list_names_each_variable_once_and_is_all_or_nothing(tmp_home):
    with pytest.raises(template_vars.VariableError, match="once"):
        template_vars.bind_project(
            "p-aaaa0001",
            [
                {"name": "a", "kind": "text", "value": "1"},
                {"name": "a", "kind": "text", "value": "2"},
            ],
        )
    with pytest.raises(template_vars.VariableError):
        template_vars.bind_project(
            "p-aaaa0001",
            [
                {"name": "a", "kind": "text", "value": "1"},
                {"name": "b", "kind": "secret", "ref": "global"},  # no such global: all refused
            ],
        )
    assert template_vars.project_bindings("p-aaaa0001") == []


def test_no_secret_value_reaches_a_response_a_file_an_error_or_a_log(tmp_home, auth_cfg, caplog):
    caplog.set_level(logging.DEBUG)
    template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    bound = template_vars.bind_project(
        "p-aaaa0001", [{"name": "tok", "kind": "secret", "value": SECRET_A}]
    )
    template_vars.bind_project("p-bbbb0002", [{"name": "tok", "kind": "secret", "ref": "global"}])
    texts = [
        json.dumps(bound),
        json.dumps(template_vars.project_bindings("p-aaaa0001")),
        json.dumps(template_vars.project_bindings("p-bbbb0002")),
        json.dumps(template_vars.list_variables()),
        json.dumps(template_vars.resolver("p-aaaa0001").secret_state("tok")),
        template_vars.store_path().read_text(),
    ]
    rec = next(r for r in template_vars.list_variables() if r["name"] == "tok")
    try:
        template_vars.delete_variable("tok", rec["updated_at"])
    except template_vars.VariableInUse as e:
        texts.append(str(e))
    doc = _doc()
    _by(doc, "tok", "p-aaaa0001")["secret"]["kid"] = "00000000"
    _write(doc)
    try:
        template_vars.resolver("p-aaaa0001").secret("tok")
    except template_vars.BindingUnusable as e:
        texts.append(str(e))
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    csrf = c.get("/api/config").json()["csrf"]
    texts.append(c.get("/api/template-variables").text)
    texts.append(
        c.delete(
            f"/api/template-variables/tok?expected_updated_at={rec['updated_at']}",
            headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
        ).text
    )
    texts.append(caplog.text)
    blob = "\n".join(texts)
    for leak in (SECRET_A, SECRET_G):
        assert leak not in blob
    assert len(texts) == 11  # every surface above was actually produced


def test_a_projects_own_value_of_the_same_name_does_not_block_deleting_the_global(tmp_home):
    """Review 6 (mutant M9): only a recorded `ref: "global"` is a dependant, never a project's own
    value that happens to share the name."""
    rec = template_vars.create_variable({"name": "x", "value": "global"})
    template_vars.bind_project("p-aaaa0001", [{"name": "x", "kind": "text", "value": "own"}])
    template_vars.delete_variable("x", rec["updated_at"])
    assert template_vars.values() == {}
    assert template_vars.resolver("p-aaaa0001").text("x")["value"] == "own"


@pytest.mark.parametrize("marker", [[], {}, 1, None, True, ["scoped"]])
def test_a_malformed_aad_marker_is_a_damaged_record_never_a_crash(tmp_home, marker):
    """Hermes 5511: `"aad": []` raised TypeError (unhashable) before the record-level handler."""
    template_vars.create_variable({"name": "tok", "kind": "secret", "value": SECRET_G})
    template_vars.create_variable({"name": "host", "value": "a.test"})
    doc = _doc()
    _by(doc, "tok")["secret"]["aad"] = marker
    _write(doc)
    env = _by(doc, "tok")["secret"]
    # direct
    assert template_secrets.valid_store_envelope(env) is False
    assert template_secrets.decrypt_record("global", None, "tok", env) is None
    # list: the damaged record is skipped, the good one still listed
    assert [v["name"] for v in template_vars.list_variables()] == ["host"]
    # resolver: a damaged store never resolves
    with pytest.raises(template_vars.ResolutionUnavailable):
        template_vars.resolver("p-aaaa0001")
    # redaction: a secret record it cannot read refuses (fail closed), as a VariableError
    with pytest.raises(template_vars.VariableError):
        template_vars.secret_values_checked()
    # write: the damaged file is quarantined and the next write succeeds
    template_vars.create_variable({"name": "other", "value": "x"})
    assert {v["name"] for v in template_vars.list_variables()} == {"host", "other"}
    kept = list(template_vars.store_path().parent.glob("template-variables.json.*"))
    assert kept, "the damaged file was not quarantined"


def test_duplicate_identities_are_all_redacted(tmp_home):
    """Hermes 5512: plaintexts keyed by identity let a duplicate record hide the other one's value
    from redaction. Every decryptable record is redacted; the resolver still refuses to use them."""
    vals = [
        "global-dup-value-1",
        "global-dup-value-2",
        "project-dup-value-1",
        "project-dup-value-2",
    ]
    recs = [
        {
            "name": "tok",
            "kind": "secret",
            "scope": scope,
            "project_id": pid,
            "created_at": 1,
            "updated_at": 1,
            "secret": template_secrets.encrypt_scoped(scope, pid, "tok", v),
        }
        for (scope, pid), v in zip(
            [
                ("global", None),
                ("global", None),
                ("project", "p-aaaa0001"),
                ("project", "p-aaaa0001"),
            ],
            vals,
            strict=True,
        )
    ]
    _write({"version": 3, "variables": recs})
    assert sorted(template_vars.secret_values_checked()) == sorted(vals)
    text = " | ".join(vals)
    assert template_secrets.redact_text(text) == " | ".join(["[secret]"] * 4)
    out = template_secrets.redact_messages([{"role": "user", "content": text}])
    assert all(v not in out[0]["content"] for v in vals)
    with pytest.raises(template_vars.ResolutionUnavailable):
        template_vars.resolver("p-aaaa0001")


def test_malformed_global_reference_blocks_delete_and_recreation(tmp_home):
    global_ = template_vars.create_variable({"name": "host", "value": "example.test"})
    template_vars.bind_project("p-aaaa0001", [{"name": "host", "kind": "text", "ref": "global"}])
    doc = _doc()
    _by(doc, "host", "p-aaaa0001")["kind"] = "secret"
    _write(doc)
    before = template_vars.store_path().read_bytes()
    with pytest.raises(template_vars.ResolutionUnavailable):
        template_vars.resolver("p-aaaa0001")
    with pytest.raises(template_vars.VariableInUse):
        template_vars.delete_variable("host", global_["updated_at"])
    assert template_vars.store_path().read_bytes() == before
    # Even an out-of-band removal cannot make creating a new secret silently activate the
    # damaged binding. The operator must explicitly remove/rebind that project first.
    doc["variables"] = [r for r in doc["variables"] if r["scope"] != "global"]
    _write(doc)
    before = template_vars.store_path().read_bytes()
    with pytest.raises(template_vars.VariableInUse):
        template_vars.create_variable({"name": "host", "kind": "secret", "value": SECRET})
    assert template_vars.store_path().read_bytes() == before
    with pytest.raises(template_vars.ResolutionUnavailable):
        template_vars.resolver("p-aaaa0001")
