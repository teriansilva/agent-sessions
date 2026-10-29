"""Secret files, agent access and line-wise redaction in the variables library (#1786 P1)."""

import json
import os
import stat

import pytest

from agent_sessions import template_secrets, variables, vault_files
from agent_sessions import template_vars as store

# A .p8-shaped key: a PEM armour around base64 body lines an agent would print one by one.
BODY_1 = "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQgCanaryLine1xyz"
BODY_2 = "oAoGCCqGSM49AwEHoUQDQgAECanaryLine2abcdefghijklmnopqrstuvwxyz0"
P8 = f"-----BEGIN PRIVATE KEY-----\n{BODY_1}\n{BODY_2}\n-----END PRIVATE KEY-----\n".encode()
BINARY = bytes(range(256)) * 4  # not UTF-8: a .p12-shaped blob


def define(name="asc_api_key"):
    return variables.create({"name": name, "kind": "secret_file"})


def upload(row, data=P8, name="AuthKey_ABC123.p8"):
    return variables.put_file(row["id"], data, name, row["updated_at"])


def document():
    return json.loads(store.store_path().read_text())


def blobs():
    top = vault_files.root()
    return sorted(p.name for p in top.iterdir() if p.name != vault_files.QUARANTINE)


def test_a_secret_file_is_stored_sealed_and_no_shape_carries_its_content(tmp_home):
    row = define()
    assert row["set"] is False and row["agent_access"] is False and "file" not in row
    stored = upload(row)
    assert stored["set"] is True and stored["needs_reentry"] is False
    assert stored["file"] == {"name": "AuthKey_ABC123.p8", "size": len(P8)}
    assert "value" not in stored and "secret" not in stored
    # On disk: the record carries metadata only; the blob is sealed, private, and not plaintext.
    rec = document()["variables"][0]
    assert rec["file"] == {"name": "AuthKey_ABC123.p8", "size": len(P8), "rev": 1}
    assert BODY_1 not in store.store_path().read_text()
    [name] = blobs()
    assert name == f"{row['id']}.1.bin"
    blob = vault_files.root() / name
    assert stat.S_IMODE(os.stat(blob).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(vault_files.root()).st_mode) == 0o700
    assert BODY_1.encode() not in blob.read_bytes()
    # No reader of the template library sees it: it is never a field, a value or a send secret.
    assert store.values() == {} and store.secret_state() == {} and store.secret_values() == {}
    assert store.list_variables() == []
    listed = json.dumps(variables.library())
    assert BODY_1 not in listed and BODY_2 not in listed


def test_a_blob_opens_only_under_its_own_identity_and_revision(tmp_home):
    row = upload(define())
    rec = next(r for r in store._read_strictly() if r["name"] == "asc_api_key")
    rec = {**rec, "id": row["id"]}
    assert store.file_bytes(rec) == P8
    good = vault_files.root() / f"{row['id']}.1.bin"
    # The same bytes under another revision, another id or another scope do not open.
    (vault_files.root() / f"{row['id']}.2.bin").write_bytes(good.read_bytes())
    assert vault_files.read(row["id"], 2, store.file_aad(rec, 2)) is None
    other = "0" * 32
    (vault_files.root() / f"{other}.1.bin").write_bytes(good.read_bytes())
    assert (
        vault_files.read(other, 1, vault_files.aad("global", None, rec["name"], other, 1)) is None
    )
    project = vault_files.aad("project", "atlas", rec["name"], row["id"], 1)
    assert vault_files.read(row["id"], 1, project) is None


def test_replacing_a_file_retires_the_old_text_and_destroys_its_blob(tmp_home, monkeypatch):
    first = upload(define())
    with monkeypatch.context() as m:
        # The commit's own cleanup destroys the old blob; the sweep is only the safety net.
        m.setattr(store, "_sweep_locked", lambda path: 0)
        second = upload(first, b"-----BEGIN KEY-----\nReplacementBodyLineAAAAAAAAAAAAAAAAAA\n")
    assert document()["variables"][0]["file"]["rev"] == 2
    assert blobs() == [f"{first['id']}.2.bin"]
    # The old content is still redacted (it may sit in transcripts it was printed into).
    values = template_secrets.redaction_values()
    assert P8.decode() in values and BODY_1 in values
    assert "ReplacementBodyLineAAAAAAAAAAAAAAAAAA" in values
    assert second["file"]["size"] == len(
        b"-----BEGIN KEY-----\nReplacementBodyLineAAAAAAAAAAAAAAAAAA\n"
    )


def test_a_text_file_is_redacted_line_by_line_but_ordinary_lines_are_not(tmp_home):
    kubeconfig = (
        b"apiVersion: v1\nkind: Config\nusers:\n- name: ops\n  user:\n"
        b"    token: eyJhbGciOiJSUzI1NiJ9CanaryTokenRunOfCredentialChars\n"
    )
    upload(define("prod_kubeconfig"), kubeconfig, "kubeconfig.yaml")
    printed = "line two of the config: token: eyJhbGciOiJSUzI1NiJ9CanaryTokenRunOfCredentialChars"
    redacted = template_secrets.redact_text(printed, template_secrets.redaction_values())
    assert "CanaryTokenRunOfCredentialChars" not in redacted
    # Contract (Hermes 7394, 4): EVERY line of a secret of 8+ characters is redacted, even one that
    # looks ordinary — over-redacting text is the safe failure. Shorter lines are not entries.
    values = template_secrets.redaction_values()
    assert "apiVersion: v1" in values and "users:" not in values


def test_each_pem_line_of_a_key_is_redacted_when_printed_alone(tmp_home):
    upload(define())
    values = template_secrets.redaction_values()
    for line in (BODY_1, BODY_2):
        assert line not in template_secrets.redact_text(f"cat key | sed -n 2p\n{line}\n", values)
    # The armour is not secret and is not turned into a redaction entry.
    assert "-----BEGIN PRIVATE KEY-----" not in values


def test_a_multi_line_secret_variable_is_redacted_line_by_line(tmp_home):
    armour = "OPENSSH PRIVATE KEY"
    pem = f"-----BEGIN {armour}-----\n{BODY_1}\n{BODY_2}\n-----END {armour}-----"
    variables.create({"name": "deploy_key", "kind": "secret", "value": pem})
    values = template_secrets.redaction_values()
    assert BODY_2 not in template_secrets.redact_text(f"> {BODY_2}", values)


def test_the_documented_residual_a_binary_file_cannot_be_redacted(tmp_home):
    upload(define("client_cert"), BINARY, "client.p12")
    # Nothing a transcript could contain is derived from a binary file: no entry is added.
    assert template_secrets.redaction_values() == []


def test_redaction_refuses_when_a_current_blob_is_missing_or_tampered(tmp_home):
    row = upload(define())
    blob = vault_files.root() / f"{row['id']}.1.bin"
    sealed = blob.read_bytes()
    blob.write_bytes(sealed[:-1] + bytes([sealed[-1] ^ 1]))
    template_secrets._reset_for_tests()
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()
    assert variables.library()["variables"][0]["needs_reentry"] is True
    blob.unlink()
    template_secrets._reset_for_tests()
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()


def test_a_lost_key_reads_as_needs_reentry(tmp_home):
    upload(define())
    template_secrets.key_path().unlink()
    template_secrets._reset_for_tests()
    assert variables.library()["variables"][0]["needs_reentry"] is True


def test_an_orphan_new_blob_from_an_interrupted_write_is_retired_and_destroyed(tmp_home):
    """Interrupted between step 1 (publish) and step 2 (commit): an orphan rev+1 blob."""
    row = upload(define())
    rec = next(r for r in document()["variables"])
    orphan = b"-----BEGIN KEY-----\nOrphanBodyRunAAAAAAAAAAAAAAAAAAAAAA\n"
    vault_files.publish(
        row["id"], 2, vault_files.aad("global", None, rec["name"], row["id"], 2), orphan
    )
    assert store.sweep_secret_files() == 0
    assert blobs() == [f"{row['id']}.1.bin"]
    assert "OrphanBodyRunAAAAAAAAAAAAAAAAAAAAAA" in template_secrets.redaction_values()


def test_an_old_blob_left_after_the_commit_is_retired_and_destroyed(tmp_home, monkeypatch):
    """Interrupted between step 2 (commit) and step 3 (destroy the old blob)."""
    first = upload(define())

    def fail(*_a):
        raise OSError("disk went away")

    # A scoped patch: `monkeypatch.undo()` would also drop conftest's store-path pins.
    with monkeypatch.context() as m:
        m.setattr(vault_files, "remove", fail)
        m.setattr(store, "_sweep_locked", lambda path: 0)
        upload(first, b"second-file-content-long-enough")
    assert blobs() == [f"{first['id']}.1.bin", f"{first['id']}.2.bin"]
    template_secrets._reset_for_tests()  # the in-memory retire was lost with the process
    assert store.sweep_secret_files() == 0
    assert blobs() == [f"{first['id']}.2.bin"]
    assert BODY_1 in template_secrets.redaction_values()


def test_a_failed_commit_removes_the_blob_it_published(tmp_home, monkeypatch):
    row = define()

    def refuse(*_a, **_k):
        raise store.VariableError("refused late")

    monkeypatch.setattr(store, "atomic_write_json", refuse)  # the commit itself fails
    with pytest.raises(store.VariableError):
        upload(row)
    assert blobs() == []


def test_an_unrecognized_blob_is_quarantined_never_decrypted_or_deleted(tmp_home):
    upload(define())
    stranger = vault_files.root() / f"{'f' * 32}.1.bin"
    stranger.write_bytes(b"BLSF1\nnot ours")
    stranger.chmod(0o600)
    (vault_files.root() / "notes.txt").write_text("hello")
    (vault_files.root() / ".tmp-deadbeef").write_bytes(b"half a write")
    assert store.sweep_secret_files() == 2
    assert vault_files.quarantined() == 2
    assert len(blobs()) == 1  # the current blob only; the temp file was destroyed


def test_agent_access_is_a_record_flag_on_secrets_only(tmp_home):
    row = upload(define())
    on = variables.update(row["id"], {"agent_access": True}, row["updated_at"])
    assert on["agent_access"] is True
    assert document()["variables"][0]["agent_access"] is True
    off = variables.update(row["id"], {"agent_access": False}, on["updated_at"])
    assert off["agent_access"] is False and "agent_access" not in document()["variables"][0]
    text = variables.create({"name": "host", "value": "one.test"})
    with pytest.raises(store.VariableError):
        variables.update(text["id"], {"agent_access": True}, text["updated_at"])
    with pytest.raises(store.VariableError):
        variables.update(row["id"], {"agent_access": "yes"}, off["updated_at"])


def test_a_project_ref_never_carries_its_own_agent_access(tmp_home):
    define()
    store.bind_project("atlas", [{"name": "asc_api_key", "kind": "secret_file", "ref": "global"}])
    raw = next(r for r in document()["variables"] if r.get("ref"))
    with pytest.raises(store.VariableError):
        store._coerce_record({**raw, "agent_access": True})


def test_a_secret_file_is_bound_by_ref_only_and_never_resolves_as_a_secret(tmp_home):
    upload(define())
    with pytest.raises(store.VariableError):
        store.bind_project(
            "atlas", [{"name": "asc_api_key", "kind": "secret_file", "value": "x" * 12}]
        )
    store.bind_project("atlas", [{"name": "asc_api_key", "kind": "secret_file", "ref": "global"}])
    with pytest.raises(store.BindingUnusable):
        store.resolver("atlas").secret("asc_api_key")


def test_the_name_based_api_never_creates_or_replaces_a_secret_file(tmp_home):
    with pytest.raises(store.VariableError):
        store.create_variable({"name": "f", "kind": "secret_file", "value": "x" * 12})
    row = upload(define())
    with pytest.raises(store.VariableError):
        store.update_variable("asc_api_key", {"value": "y" * 12}, row["updated_at"])
    with pytest.raises(store.VariableError):
        variables.update(row["id"], {"value": "y" * 12}, row["updated_at"])


def test_clearing_and_deleting_destroy_the_blob_and_retire_the_text(tmp_home):
    row = upload(define())
    cleared = variables.update(row["id"], {"clear_value": True}, row["updated_at"])
    assert cleared["set"] is False and "file" not in cleared and blobs() == []
    assert BODY_1 in template_secrets.redaction_values()
    again = upload(cleared)
    assert blobs() == [f"{row['id']}.1.bin"]
    variables.delete(row["id"], again["updated_at"])
    assert blobs() == []


def test_a_secret_file_record_exists_only_in_a_version_4_store():
    raw = {
        "name": "f",
        "kind": "secret_file",
        "scope": "global",
        "project_id": None,
        "file": {"name": "a.p8", "size": 3, "rev": 1},
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    with pytest.raises(store.VariableError):
        store._coerce_record(raw, 3)


@pytest.mark.parametrize("name", ["", "a/b.p8", "..", "tab\there", "x" * 256])
def test_a_file_name_is_display_text_never_a_path(tmp_home, name):
    with pytest.raises(store.VariableError):
        upload(define(), name=name)
    assert not vault_files.root().exists() or blobs() == []


def test_a_file_name_carrying_a_stored_secret_or_a_credential_shape_is_refused(tmp_home):
    variables.create({"name": "token", "kind": "secret", "value": "s3cr3t-token-value-1"})
    row = define()
    for name in ("s3cr3t-token-value-1.p8", "ghp_abcdefghijklmnopqrstuvwxyz0123456789.txt"):
        with pytest.raises(store.VariableError):
            upload(row, name=name)
    assert not vault_files.root().exists() or blobs() == []


def _client(auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    login = client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert login.status_code == 303
    csrf = client.get("/api/config").json()["csrf"]
    return client, {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}


def test_the_upload_route_is_authenticated_bounded_and_never_echoes_content(auth_cfg, tmp_home):
    client, headers = _client(auth_cfg)
    row = client.post(
        "/api/variables", json={"name": "asc_api_key", "kind": "secret_file"}, headers=headers
    ).json()
    url = f"/api/variables/{row['id']}/file?expected_updated_at={row['updated_at']}"
    files = {"file": ("AuthKey_ABC123.p8", P8, "application/octet-stream")}
    no_csrf = client.put(url, files=files, headers={"Origin": auth_cfg.origin})
    assert no_csrf.status_code == 403 and not vault_files.root().exists()
    too_big = client.put(
        url, files={"file": ("big.bin", b"x" * (vault_files.MAX_BYTES + 1))}, headers=headers
    )
    assert too_big.status_code == 413 and not vault_files.root().exists()
    assert client.put(url, content=P8, headers=headers).status_code == 415
    empty = client.put(url, files={"file": ("empty.p8", b"")}, headers=headers)
    assert empty.status_code == 422
    stored = client.put(url, files=files, headers=headers)
    assert stored.status_code == 200, stored.text
    assert BODY_1 not in stored.text and "no-store" in stored.headers["cache-control"]
    assert stored.json()["file"] == {"name": "AuthKey_ABC123.p8", "size": len(P8)}
    stale = client.put(url, files=files, headers=headers)
    assert stale.status_code == 409 and BODY_1 not in stale.text
    listing = client.get("/api/variables")
    assert BODY_1 not in listing.text and BODY_2 not in listing.text
    legacy = client.get("/api/template-variables")
    assert "asc_api_key" not in legacy.text
    patched = client.patch(
        f"/api/variables/{row['id']}",
        json={"expected_updated_at": stored.json()["updated_at"], "agent_access": True},
        headers=headers,
    )
    assert patched.status_code == 200 and patched.json()["agent_access"] is True


def test_the_redaction_prefilter_answers_exactly_what_the_full_scan_answers():
    """`_candidates` only skips variants that cannot occur; pinned against the naive algorithm."""
    import random

    rng = random.Random(1786)
    alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/=_-. :"\\n'
    secrets = sorted(
        {"".join(rng.choice(alphabet) for _ in range(rng.randint(8, 40))) for _ in range(300)},
        key=len,
        reverse=True,
    )

    def naive(text: str) -> str:
        variants = [v for s in secrets for v in template_secrets._variants(s)]

        def replace(t: str) -> str:
            for v in variants:
                if v in t:
                    t = t.replace(v, template_secrets.REDACTED)
            return t

        text = replace(text)
        if "\x1b" in text:
            stripped = template_secrets._ANSI_RE.sub("", text)
            if any(v in stripped for v in variants):
                text = replace(stripped)
        return text

    for _ in range(200):
        parts = []
        for _ in range(rng.randint(1, 8)):
            pick = rng.random()
            s = rng.choice(secrets)
            if pick < 0.3:
                parts.append(s)
            elif pick < 0.4:
                parts.append(template_secrets._variants(s)[-1])  # an encoded form
            elif pick < 0.5:
                cut = rng.randint(1, len(s) - 1)
                parts.append(s[:cut] + "\x1b[31m" + s[cut:])  # an ANSI-interleaved echo
            else:
                parts.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30))))
        text = " ".join(parts)
        assert template_secrets.redact_text(text, secrets) == naive(text)


def test_every_short_line_of_a_multi_line_secret_is_redacted(tmp_home):
    variables.create(
        {"name": "creds", "kind": "secret", "value": "header-line\nCanary7!\nfooter-line"}
    )
    out = template_secrets.redact_text("got: Canary7!", template_secrets.redaction_values())
    assert "Canary7!" not in out


def test_a_failed_directory_sync_after_the_replace_never_removes_the_committed_blob(
    tmp_home, monkeypatch
):
    """The document became visible, then its durability step failed (Hermes 7394, 1)."""
    from agent_sessions import atomicjson

    row = define()
    real = atomicjson.fsync_dir
    calls = {"n": 0}

    def flaky(d):
        calls["n"] += 1
        raise OSError("EIO on the directory sync")

    with monkeypatch.context() as m:
        m.setattr(atomicjson, "fsync_dir", flaky)
        with pytest.raises(OSError):
            upload(row)
    assert calls["n"] >= 1 and atomicjson.fsync_dir is real
    rec = document()["variables"][0]
    assert rec["file"]["rev"] == 1  # the record is visible…
    assert blobs() == [f"{row['id']}.1.bin"]  # …and its blob was kept
    assert BODY_1 in template_secrets.redaction_values()


def test_the_label_check_and_a_concurrent_secret_write_are_serialized(tmp_home, monkeypatch):
    """The file-name check runs inside the fenced transaction: a secret stored meanwhile waits
    for the upload to commit, never slips between the check and the commit (Hermes 7394, 2)."""
    import threading

    row = define()
    started = {}

    def store_secret():
        try:
            variables.create({"name": "late", "kind": "secret", "value": "late-secret-value-1"})
        except store.VariableError as exc:
            started["refused"] = str(exc)

    real_sweep = store._sweep_locked

    def barrier(path):
        started["t"] = threading.Thread(target=store_secret)
        started["t"].start()
        started["t"].join(timeout=0.5)
        assert started["t"].is_alive()  # blocked behind this transaction's fences
        return real_sweep(path)

    monkeypatch.setattr(store, "_sweep_locked", barrier)
    upload(row, name="late-secret-value-1.p8")
    started["t"].join(timeout=10)
    assert not started["t"].is_alive()
    # It then sees the committed file name and is refused (Hermes 7398, 2): the value never
    # becomes a stored secret that a public label already shows.
    assert "file name" in started["refused"]
    assert "late-secret-value-1" not in started["refused"]
    assert all(v["name"] != "late" for v in variables.library()["variables"])


def test_every_agent_visible_label_passes_the_withholding_check(tmp_home):
    variables.create({"name": "token", "kind": "secret", "value": "s3cr3t-token-value-1"})
    with pytest.raises(store.VariableError):
        variables.create(
            {
                "name": "f",
                "kind": "secret_file",
                "definition": {"help": "https://ops:hunter22x@host"},
            }
        )
    with pytest.raises(store.VariableError):
        variables.create(
            {"name": "g", "kind": "secret_file", "definition": {"label": "s3cr3t-token-value-1"}}
        )
    row = define()
    with pytest.raises(store.VariableError):
        variables.update(
            row["id"],
            {
                "definition": {
                    "value_type": "text",
                    "help": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                }
            },
            row["updated_at"],
        )


@pytest.mark.parametrize(
    "help_text",
    [
        "App Store Connect API key (.p8) for fastlane",
        "API token: create it under GitHub Settings",
        "Bearer token sent as the Authorization header",
        "Use --password to pass it to the CLI",
        "kubeconfig for the ops cluster (token: from vault)",
    ],
)
def test_ordinary_help_text_about_credentials_is_accepted(tmp_home, help_text):
    """A label is refused for a stored secret or a credential VALUE, never for describing one
    (independent review of #1788): this feature's own examples must be savable."""
    row = variables.create(
        {"name": "asc_api_key", "kind": "secret_file", "definition": {"help": help_text}}
    )
    assert row["definition"]["help"] == help_text
    stored = upload(row, name="gcp-sa-prod-1234567890abcdef.json")
    assert stored["file"]["name"] == "gcp-sa-prod-1234567890abcdef.json"


def test_a_truncated_upload_is_refused_and_nothing_is_stored(auth_cfg, tmp_home):
    client, headers = _client(auth_cfg)
    row = client.post(
        "/api/variables", json={"name": "asc_api_key", "kind": "secret_file"}, headers=headers
    ).json()
    url = f"/api/variables/{row['id']}/file?expected_updated_at={row['updated_at']}"
    body = (
        b"--XyZ\r\n"
        b'Content-Disposition: form-data; name="file"; filename="AuthKey.p8"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n" + P8[:40]
    )  # cut off: no closing boundary
    cut = client.put(
        url,
        content=body,
        headers={**headers, "Content-Type": "multipart/form-data; boundary=XyZ"},
    )
    assert cut.status_code == 422 and "ended early" in cut.json()["detail"]
    assert not vault_files.root().exists() or blobs() == []


def test_a_warm_cache_never_vouches_for_a_missing_tampered_or_resealed_blob(tmp_home):
    row = upload(define())
    rec = document()["variables"][0]
    assert BODY_1 in template_secrets.redaction_values()  # warm
    blob = vault_files.root() / f"{row['id']}.1.bin"
    sealed = blob.read_bytes()
    # Re-sealed under the same identity with other bytes: the new plaintext must be redacted.
    aad = vault_files.aad("global", None, rec["name"], row["id"], 1)
    blob.write_bytes(template_secrets.seal_bytes(aad, b"Resealed-Different-Content-Line-0001\nx"))
    assert "Resealed-Different-Content-Line-0001" in template_secrets.redaction_values()
    blob.write_bytes(sealed[:-1] + bytes([sealed[-1] ^ 1]))  # tampered
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()
    blob.write_bytes(sealed)
    assert BODY_1 in template_secrets.redaction_values()
    blob.unlink()  # missing, cache warm
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()


@pytest.mark.parametrize(
    "label",
    [
        {"name": "k_late_secret_value_9"},
        {"definition": {"value_type": "text", "label": "late_secret_value_9 key"}},
        {"definition": {"value_type": "text", "help": "uses late_secret_value_9"}},
        {"file": "late_secret_value_9.p8"},
    ],
)
def test_a_secret_already_shown_in_any_label_is_refused_at_its_write(tmp_home, label):
    value = "late_secret_value_9"
    name = label.get("name", "holder")
    row = variables.create(
        {
            "name": name,
            "kind": "secret_file",
            **({"definition": label["definition"]} if "definition" in label else {}),
        }
    )
    if "file" in label:
        upload(row, name=label["file"])
    # Every secret write path refuses a value a label already shows.
    with pytest.raises(store.VariableError, match="appears in a variable's"):
        variables.create({"name": "late", "kind": "secret", "value": value})
    with pytest.raises(store.VariableError, match="appears in a variable's"):
        store.create_variable({"name": "late2", "kind": "secret", "value": value})
    other = variables.create({"name": "other", "kind": "secret", "value": "unrelated-value-1"})
    with pytest.raises(store.VariableError, match="appears in a variable's"):
        variables.update(other["id"], {"value": value}, other["updated_at"])
    with pytest.raises(store.VariableError, match="appears in a variable's"):
        store.bind_project("atlas", [{"name": "late3", "kind": "secret", "value": value}])
    target = define("file_two")
    with pytest.raises(store.VariableError, match="appears in a variable's"):
        upload(target, data=f"header-line\n{value}\n".encode(), name="other.txt")
    # None of the refused writes stored the value as a secret.
    assert all(value not in v for v in store.secret_values_checked())


@pytest.mark.parametrize("kind", ["text", "secret"])
def test_a_project_binding_name_holding_a_stored_secret_is_refused(tmp_home, kind):
    variables.create({"name": "token", "kind": "secret", "value": "canarysecretvalue987"})
    value = {"value": "plain-text" if kind == "text" else "another-secret-1"}
    with pytest.raises(store.VariableError, match="name"):
        store.bind_project("atlas", [{"name": "canarysecretvalue987", "kind": kind, **value}])
    assert store.project_bindings("atlas") == []


def test_every_writer_of_the_store_is_held_to_the_label_rules(tmp_home):
    """The backstop in the one transaction (Hermes 7402): a writer that adds records directly —
    as a playbook's apply and restore do — cannot add a secret-bearing name, nor a secret a label
    already shows."""
    from agent_sessions import template_secrets

    variables.create({"name": "token", "kind": "secret", "value": "canarysecretvalue987"})
    variables.create(
        {"name": "holder", "kind": "secret_file", "definition": {"help": "for zz-shown-value-42"}}
    )

    def add(rec):
        def fn(records):
            records.append(
                {
                    "scope": "project",
                    "project_id": "atlas",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                    **rec,
                }
            )

        return fn

    with pytest.raises(store.VariableError, match="name"):
        store._mutate(add({"name": "canarysecretvalue987", "kind": "text", "value": "x"}))
    envelope = template_secrets.encrypt_scoped("project", "atlas", "pb_secret", "zz-shown-value-42")
    with pytest.raises(store.VariableError, match="description"):
        store._mutate(add({"name": "pb_secret", "kind": "secret", "secret": envelope}))
    # An unchanged record is never re-judged: an unrelated write still succeeds.
    store._mutate(add({"name": "pb_plain", "kind": "text", "value": "ok"}))
    assert [b["name"] for b in store.project_bindings("atlas")] == ["pb_plain"]


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_an_unreadable_file_is_repaired_by_uploading_it_again(tmp_home, damage):
    """Recovery the docs promise (Hermes 7406, 1): re-upload, not only delete. Sends stay fail
    closed until then."""
    row = upload(define())
    blob = vault_files.root() / f"{row['id']}.1.bin"
    if damage == "missing":
        blob.unlink()
    else:
        sealed = blob.read_bytes()
        blob.write_bytes(sealed[:-1] + bytes([sealed[-1] ^ 1]))
    template_secrets._reset_for_tests()
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()  # the transport refuses meanwhile
    current = variables.library()["variables"][0]
    assert current["needs_reentry"] is True
    repaired = upload(current, b"-----BEGIN KEY-----\nRepairedKeyBodyLineAAAAAAAAAAAAAAAAA\n")
    assert repaired["needs_reentry"] is False
    assert "RepairedKeyBodyLineAAAAAAAAAAAAAAAAA" in template_secrets.redaction_values()
    assert blobs() == [f"{row['id']}.2.bin"]


def test_a_damaged_store_is_never_swept_and_its_blob_survives_until_repaired(tmp_home):
    """A record whose kind was corrupted must not make its still-referenced blob an orphan
    (Hermes 7406, 2): the sweep needs a store that reads completely, else it moves nothing."""
    row = upload(define())
    path = store.store_path()
    good = path.read_text()
    doc = json.loads(good)
    doc["variables"][0]["kind"] = "text"
    path.write_text(json.dumps(doc))
    assert store.sweep_secret_files() == 0
    assert blobs() == [f"{row['id']}.1.bin"] and vault_files.quarantined() == 0
    path.write_text(good)  # the operator repairs the metadata
    rec = next(r for r in store._read(path)[0] if r["kind"] == "secret_file")
    assert store.file_bytes(rec) == P8


def test_several_unreadable_files_are_each_repairable_and_block_nothing_else(tmp_home):
    """A lost key pauses model sends (fail closed, Hermes 7416) but blocks no repair and no
    unrelated write: each file is repaired by uploading it again, or cleared, one by one
    (independent review of #1788, blocking 1)."""
    a = upload(define("file_a"))
    b = upload(define("file_b"), b"second-file-body-AAAAAAAAAAAAAAAA\n")
    template_secrets.key_path().unlink()
    template_secrets._reset_for_tests()  # a cold restart after the key was lost
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()  # fail closed on key loss: model sends pause
    variables.create({"name": "host", "value": "one.test"})  # unrelated writes still work
    rows = {v["name"]: v for v in variables.library()["variables"]}
    assert rows["file_a"]["needs_reentry"] and rows["file_b"]["needs_reentry"]
    upload(rows["file_a"], b"repaired-a-body-BBBBBBBBBBBBBBBBBBBB\n")
    cleared = variables.update(
        rows["file_b"]["id"], {"clear_value": True}, rows["file_b"]["updated_at"]
    )
    assert cleared["set"] is False
    assert "repaired-a-body-BBBBBBBBBBBBBBBBBBBB" in template_secrets.redaction_values()
    assert a["id"] != b["id"]


def test_two_tampered_files_are_repaired_one_at_a_time_while_sends_stay_closed(tmp_home):
    a = upload(define("file_a"))
    b = upload(define("file_b"), b"second-file-body-AAAAAAAAAAAAAAAA\n")
    for row in (a, b):
        blob = vault_files.root() / f"{row['id']}.1.bin"
        sealed = blob.read_bytes()
        blob.write_bytes(sealed[:-1] + bytes([sealed[-1] ^ 1]))
    template_secrets._reset_for_tests()
    rows = {v["name"]: v for v in variables.library()["variables"]}
    upload(rows["file_a"], b"repaired-a-body-BBBBBBBBBBBBBBBBBBBB\n")
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()  # file_b is still damaged under the current key
    upload(rows["file_b"], b"repaired-b-body-CCCCCCCCCCCCCCCCCCCC\n")
    assert "repaired-b-body-CCCCCCCCCCCCCCCCCCCC" in template_secrets.redaction_values()


def test_a_blob_vanishing_between_listing_and_stat_is_not_damage(tmp_home, monkeypatch):
    upload(define())
    real = os.listdir
    with monkeypatch.context() as m:
        m.setattr(vault_files.os, "listdir", lambda fd: [*real(fd), f"{'a' * 32}.9.bin"])
        assert len(vault_files.signature()) == 1


def test_a_store_read_racing_a_committed_replace_is_read_again(tmp_home, monkeypatch):
    upload(define())
    calls = {"n": 0}
    real = store._secret_values_checked

    def first_sees_a_destroyed_blob(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            path = store.store_path()
            os.utime(path, ns=(1, 1))  # the store moved on meanwhile
            raise store._FileUnreadable
        return real(**kw)

    with monkeypatch.context() as m:
        m.setattr(store, "_secret_values_checked", first_sees_a_destroyed_blob)
        assert any(BODY_1 in v for v in store.secret_values_checked())
    assert calls["n"] == 2


def test_stray_entries_never_break_uploads_or_the_sweep(tmp_home):
    row = upload(define())
    (vault_files.root() / ".tmp-strange").mkdir()
    empty = vault_files.root() / f"{row['id']}.7.bin"
    empty.write_bytes(b"")
    empty.chmod(0o600)
    assert store.sweep_secret_files() == 1  # the directory is moved aside, never opened
    assert not empty.exists()  # an emptied orphan is simply destroyed
    current = variables.library()["variables"][0]
    upload(current, b"next-revision-body-DDDDDDDDDDDDDDDDDD\n")


def test_the_prefilter_keeps_a_variant_completed_by_an_earlier_replacement():
    """`[secret]` inserted by one replacement can complete another secret: the bracket clause of
    `_candidates` keeps such a variant (independent review of #1788, 7)."""
    fillers = [f"filler-secret-{i:03d}-zzzz" for i in range(70)]
    secrets = sorted(
        ["abcdefghij", f"{template_secrets.REDACTED}xyz", *fillers], key=len, reverse=True
    )
    secrets = sorted(set(secrets), key=lambda v: (v != "abcdefghij", -len(v)))
    text = "before abcdefghijxyz after"
    variants = [v for s_ in secrets for v in template_secrets._variants(s_)]
    naive = text
    for v in variants:
        if v in naive:
            naive = naive.replace(v, template_secrets.REDACTED)
    assert template_secrets.redact_text(text, secrets) == naive


def test_a_malformed_upload_is_a_client_error(auth_cfg, tmp_home):
    client, headers = _client(auth_cfg)
    row = client.post(
        "/api/variables", json={"name": "asc_api_key", "kind": "secret_file"}, headers=headers
    ).json()
    url = f"/api/variables/{row['id']}/file?expected_updated_at={row['updated_at']}"
    body = b"--XyZ\r\nno colon header line\r\n\r\nabc\r\n--XyZ--\r\n"
    bad = client.put(
        url, content=body, headers={**headers, "Content-Type": "multipart/form-data; boundary=XyZ"}
    )
    assert bad.status_code == 422


def test_the_legacy_routes_answer_503_when_the_inventory_is_unreadable(
    auth_cfg, tmp_home, monkeypatch
):
    client, headers = _client(auth_cfg)

    def unavailable():
        raise template_secrets.RedactionUnavailable("x")

    with monkeypatch.context() as m:
        m.setattr(template_secrets, "redaction_values_for_labels", unavailable)
        r = client.post(
            "/api/template-variables", json={"name": "host", "value": "a.test"}, headers=headers
        )
    assert r.status_code == 503


def test_a_forged_key_id_in_a_blob_header_never_skips_a_readable_file(tmp_home):
    """The header's key id is not authenticated (independent review of #1788, B1): a blob whose id
    was rewritten still decrypts and is still redacted; one that does not open refuses."""
    row = upload(define())
    blob = vault_files.root() / f"{row['id']}.1.bin"
    sealed = bytearray(blob.read_bytes())
    at = len(template_secrets.BLOB_MAGIC)
    sealed[at : at + 8] = b"deadbeef"
    blob.write_bytes(bytes(sealed))
    template_secrets._reset_for_tests()
    out = template_secrets.redact_text("x " + BODY_1, template_secrets.redaction_values())
    assert BODY_1 not in out
    sealed[-1] ^= 1  # and now it does not open either: refused, never skipped
    blob.write_bytes(bytes(sealed))
    template_secrets._reset_for_tests()
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()


def test_an_unchanged_label_is_never_re_judged_by_the_backstop(tmp_home):
    """Only a label field that CHANGED is checked: an old help text that a later secret now
    matches (written past every check) must not block an unrelated edit of the same record."""
    from agent_sessions import variable_document

    row = variables.create(
        {"name": "holder", "kind": "secret_file", "definition": {"help": "uses zz-help-value-9"}}
    )
    path = store.store_path()
    doc = json.loads(path.read_text())
    rec = {
        "name": "later",
        "kind": "secret",
        "scope": "global",
        "project_id": None,
        "secret": template_secrets.encrypt_scoped("global", None, "later", "zz-help-value-9"),
        "id": variable_document.new_id(),
        "definition": {"value_type": "text"},
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    doc["variables"].append(rec)
    doc["identities"].append(variable_document.identity(rec))
    path.write_text(json.dumps(doc))
    template_secrets._reset_for_tests()
    # The label changes, the help text does not: only the label is judged.
    updated = variables.update(
        row["id"],
        {"definition": {"value_type": "text", "label": "Holder", "help": "uses zz-help-value-9"}},
        row["updated_at"],
    )
    assert updated["definition"]["label"] == "Holder"


def test_a_stripe_key_is_a_value_shape(tmp_home):
    with pytest.raises(store.VariableError):
        variables.create(
            {
                "name": "f",
                "kind": "secret_file",
                "definition": {"help": "sk_live_" + "Ab3dE5gH7jK9mN1pQ3sT"},
            }
        )


def test_an_emptied_current_blob_is_kept_for_the_operator(tmp_home):
    row = upload(define())
    blob = vault_files.root() / f"{row['id']}.1.bin"
    blob.write_bytes(b"")
    store.sweep_secret_files()
    assert blob.exists()
    assert variables.library()["variables"][0]["needs_reentry"] is True
