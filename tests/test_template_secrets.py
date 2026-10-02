"""Secret template fields (#1090, Phase 2): encryption at rest, the server-side send, redaction.

What this pins:

* **A secret never comes back.** No response of the variables or templates routes — list,
  create, replace, a 409's ``current`` — carries the plaintext or the envelope, and the store
  file holds only ciphertext under a separate 0600 key file.
* **Undecryptable is a state.** A missing or different key file, or an envelope moved onto
  another name, reads as ``needs_reentry`` and a send that needs it is refused with nothing
  written.
* **One name space.** Text and secret variables are one list under one lock; a text field that
  names a secret (or the reverse) is a missing variable, never a value.
* **The send is the server's.** Three separate fenced writes — clear, paste, Enter — reach the
  PTY; the response and the sent history get the MASKED text; scope is enforced at entry; every
  seam outcome maps to what it actually means.
* **Redaction at the one transport.** ``review._post_chat`` replaces every stored or typed-once
  secret (raw, JSON-escaped, URL-encoded, through ANSI) in every non-system message; a wrapped
  secret is the documented residual.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import tty
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    prefs,
    review,
    session_input,
    template_secrets,
    template_send,
    template_vars,
    templates,
)
from agent_sessions.main import create_app

SECRET = "hunter22-staging-db"  # noqa: S105 — a test fixture value
TYPED = "typed-once-token-9"  # noqa: S105
KEY = "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def _hdr(cfg, csrf):
    return {"X-CSRF-Token": csrf, "Origin": cfg.origin}


def _secret(name="db_pass", value=SECRET):
    return template_vars.create_variable({"name": name, "kind": "secret", "value": value})


def _migration_template(**over):
    base = {
        "name": "Run staging migration",
        "body": "Connect to {{host}} with {{db_pass}} and token {{token}} for {{ticket}}.",
        "fields": [
            {"name": "host", "source": "library"},
            {"name": "db_pass", "source": "library", "kind": "secret"},
            {"name": "token", "kind": "secret", "required": True},
            {"name": "ticket", "required": True},
        ],
    }
    base.update(over)
    return templates.create_template(base)


@pytest.fixture(autouse=True)
def _seams():
    session_input.reset()
    template_secrets._reset_for_tests()
    yield
    session_input.reset()
    template_secrets._reset_for_tests()


# ---- at rest -----------------------------------------------------------------------------------


def test_a_secret_is_stored_as_ciphertext_under_a_separate_owner_only_key(tmp_home):
    rec = _secret()
    assert rec == {
        "name": "db_pass",
        "kind": "secret",
        "set": True,
        "needs_reentry": False,
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
    }
    raw = template_vars.store_path().read_text()
    assert SECRET not in raw
    doc = json.loads(raw)
    assert doc["version"] == template_vars.STORE_VERSION
    [stored] = doc["variables"]
    assert set(stored) == {
        "name",
        "kind",
        "scope",
        "project_id",
        "secret",
        "created_at",
        "updated_at",
    }
    # Store v3 (#1191): a new envelope is bound to its scope-qualified identity, and says so.
    assert stored["scope"] == "global" and stored["project_id"] is None
    assert stored["secret"]["aad"] == "scoped"
    key = template_secrets.key_path()
    assert key.stat().st_mode & 0o777 == 0o600
    assert len(key.read_bytes()) == 32
    assert template_vars.secret_values() == {"db_pass": SECRET}
    # A text-variable read never sees it: the name is not a text value.
    assert template_vars.values() == {}


def test_secrets_are_at_least_eight_characters(tmp_home):
    with pytest.raises(template_vars.VariableError, match="at least 8"):
        template_vars.create_variable({"name": "short", "kind": "secret", "value": "test"})


def test_a_name_is_unique_across_both_kinds(tmp_home):
    template_vars.create_variable({"name": "host", "value": "a.test"})
    with pytest.raises(template_vars.VariableError, match="already exists"):
        _secret(name="host")
    _secret(name="db_pass")
    with pytest.raises(template_vars.VariableError, match="already exists"):
        template_vars.create_variable({"name": "db_pass", "value": "plain"})


@pytest.mark.parametrize("change", ["missing", "replaced"])
def test_a_missing_or_replaced_key_reads_as_needs_reentry(tmp_home, change):
    _secret()
    key = template_secrets.key_path()
    if change == "missing":
        key.unlink()
    else:
        key.write_bytes(os.urandom(32))
    [row] = template_vars.list_variables()
    assert row["needs_reentry"] is True
    assert template_vars.secret_state() == {"db_pass": "reentry"}
    assert template_vars.secret_values() == {}


def test_an_envelope_moved_onto_another_name_does_not_decrypt(tmp_home):
    _secret(name="db_pass")
    _secret(name="other", value="another-secret-1")
    path = template_vars.store_path()
    doc = json.loads(path.read_text())
    by = {r["name"]: r for r in doc["variables"]}
    by["other"]["secret"] = by["db_pass"]["secret"]  # the name is the AAD
    path.write_text(json.dumps(doc))
    assert template_vars.secret_state() == {"db_pass": "ok", "other": "reentry"}


def test_a_bad_key_file_is_never_overwritten(tmp_home):
    key = template_secrets.key_path()
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(b"short")
    with pytest.raises(template_secrets.SecretKeyUnavailable):
        _secret()
    assert key.read_bytes() == b"short"


def test_no_route_ever_returns_the_plaintext_or_the_envelope(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    created = c.post(
        "/api/template-variables",
        json={"name": "db_pass", "kind": "secret", "value": SECRET},
        headers=_hdr(auth_cfg, csrf),
    )
    assert created.status_code == 201
    env = json.loads(template_vars.store_path().read_text())["variables"][0]["secret"]
    listed = c.get("/api/template-variables")
    replaced = c.patch(
        "/api/template-variables/db_pass",
        json={"value": "new-secret-value", "expected_updated_at": created.json()["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    stale = c.patch(
        "/api/template-variables/db_pass",
        json={"value": "third-secret-value", "expected_updated_at": created.json()["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert replaced.status_code == 200 and stale.status_code == 409
    assert template_vars.secret_values() == {"db_pass": "new-secret-value"}
    for r in (created, listed, replaced, stale):
        body = r.text
        for leak in (SECRET, "new-secret-value", env["ct"], env["nonce"], '"secret":'):
            assert leak not in body, (r.request.method, leak)
        assert r.headers["cache-control"] == "no-store"


def test_a_secret_field_never_carries_a_default(tmp_home):
    with pytest.raises(templates.TemplateError, match="no default"):
        templates.validate(
            {
                "name": "x",
                "body": "{{p}}",
                "fields": [{"name": "p", "kind": "secret", "default": SECRET}],
            }
        )


# ---- render ------------------------------------------------------------------------------------


def _render(t, values, **over):
    kw = {
        "library": template_vars.values(),
        "secrets": template_vars.secret_values(),
        "secret_state": template_vars.secret_state(),
    }
    kw.update(over)
    return template_send.render(t, values, **kw)


def test_render_fills_secrets_server_side_and_masks_them_for_the_browser(tmp_home):
    template_vars.create_variable({"name": "host", "value": "staging.acme.test"})
    _secret()
    t = _migration_template()
    out = _render(t, {"token": TYPED, "ticket": "ACME-7"})
    assert out.text == f"Connect to staging.acme.test with {SECRET} and token {TYPED} for ACME-7."
    assert out.masked == (
        "Connect to staging.acme.test with [secret: db_pass] and token [secret: token] for ACME-7."
    )
    assert out.typed_secrets == (TYPED,)


@pytest.mark.parametrize(
    "setup, values, status, message",
    [
        # kind mismatch: a TEXT library field naming a secret variable is missing
        ("secret-named-host", {"token": TYPED, "ticket": "t"}, 409, "no text variable named host"),
        # a library secret that does not exist
        ("no-secret", {"token": TYPED, "ticket": "t"}, 409, "no secret named db_pass"),
        # a stored secret cannot be overridden from the browser
        (
            "ok",
            {"db_pass": "override!!", "token": TYPED, "ticket": "t"},
            422,
            "cannot be overridden",
        ),
        # a typed-once secret is at least 8 characters
        ("ok", {"token": "short", "ticket": "t"}, 422, "at least 8"),
        # required template field
        ("ok", {"token": TYPED, "ticket": " "}, 422, "is required"),
        # control characters are refused (ESC could end the bracketed paste early)
        ("ok", {"token": TYPED, "ticket": "a\x1b[201~b"}, 422, "control character"),
        # values for undeclared fields are refused
        ("ok", {"token": TYPED, "ticket": "t", "extra": "x"}, 422, "does not declare"),
    ],
)
def test_render_refuses(tmp_home, setup, values, status, message):
    if setup == "secret-named-host":
        _secret(name="host", value="looks-like-a-host")
        _secret()
    elif setup == "no-secret":
        template_vars.create_variable({"name": "host", "value": "h"})
    else:
        template_vars.create_variable({"name": "host", "value": "h"})
        _secret()
    t = _migration_template()
    with pytest.raises((templates.TemplateError, template_send.SendRefused)) as exc:
        _render(t, values)
    got = exc.value.status if isinstance(exc.value, template_send.SendRefused) else 422
    assert got == status and message in str(exc.value)


def test_a_needs_reentry_secret_refuses_the_render(tmp_home):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    template_secrets.key_path().unlink()
    with pytest.raises(template_send.SendRefused, match="needs re-entry") as exc:
        _render(_migration_template(), {"token": TYPED, "ticket": "t"})
    assert exc.value.status == 409


def test_render_matches_the_client_substitution_fixture():
    """The Python twin of templateMessage.ts, driven by the table the Vitest suite also reads."""
    cases = json.loads(
        (Path(__file__).parent / "fixtures" / "template_render_cases.json").read_text()
    )
    for case in cases:
        fields = [{"name": n} for n in case["fields"]]
        got = template_send.assemble(
            template_send.substitute(case["body"], fields, case["values"]), case["paths"]
        )
        assert got == case["expected"], case["name"]


# ---- the send ------------------------------------------------------------------------------------


@pytest.fixture
def live_session(monkeypatch):
    """A registered writer on a real pty, and no roots/exclusions configured (the default)."""
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(KEY, master, threading.Lock(), "attached")
    monkeypatch.setattr(template_send.engines, "resolve_session", lambda *_a, **_k: None)
    yield slave
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def _drain(fd) -> bytes:
    os.set_blocking(fd, False)
    out = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return out
        if not chunk:
            return out
        out += chunk


def test_send_writes_clear_paste_enter_as_three_writes_and_answers_masked(
    auth_cfg, tmp_home, live_session, monkeypatch
):
    template_vars.create_variable({"name": "host", "value": "staging.acme.test"})
    _secret()
    t = _migration_template()
    writes: list[bytes] = []
    real = session_input.send_input

    def recording(key, payload, **kw):
        writes.append(payload)
        return real(key, payload, **kw)

    monkeypatch.setattr(template_send.session_input, "send_input", recording)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/templates/{t['id']}/send",
        json={
            "session": KEY,
            "values": {"token": TYPED, "ticket": "ACME-7"},
            "expected_updated_at": t["updated_at"],
        },
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 200, r.text
    text = f"Connect to staging.acme.test with {SECRET} and token {TYPED} for ACME-7."
    assert writes == [b"\x01\x0b", b"\x1b[200~" + text.encode() + b"\x1b[201~", b"\r"]
    assert _drain(live_session) == b"".join(writes)
    body = r.text
    assert SECRET not in body and TYPED not in body
    assert r.json()["masked"].endswith(
        "with [secret: db_pass] and token [secret: token] for ACME-7."
    )
    assert r.json()["template"]["used_count"] == 1
    assert r.headers["cache-control"] == "no-store"
    # The typed-once value joined the redaction set.
    assert TYPED in template_secrets.redaction_values()


def test_send_is_refused_at_entry_outside_the_project_folders(
    auth_cfg, tmp_home, live_session, monkeypatch
):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    # Real directories: effective_roots drops a root that does not exist.
    (tmp_home / "work").mkdir()
    (tmp_home / "elsewhere").mkdir()
    prefs.set_project_roots([str(tmp_home / "work")])

    class Row:
        cwd = str(tmp_home / "elsewhere")

    monkeypatch.setattr(template_send.engines, "resolve_session", lambda *_a, **_k: Row())
    calls = []
    monkeypatch.setattr(template_send.session_input, "send_input", lambda *a, **k: calls.append(a))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/templates/{t['id']}/send",
        json={
            "session": KEY,
            "values": {"token": TYPED, "ticket": "t"},
            "expected_updated_at": t["updated_at"],
        },
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 403 and "Outside your project folders" in r.json()["detail"]
    assert calls == []


def test_an_unreconciled_placeholder_cannot_be_sent_to(auth_cfg, tmp_home):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/templates/{t['id']}/send",
        json={
            "session": "opencode:new-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "values": {"token": TYPED, "ticket": "t"},
            "expected_updated_at": t["updated_at"],
        },
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 422


def test_a_stale_template_revision_is_refused_before_anything_is_written(
    auth_cfg, tmp_home, live_session
):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/templates/{t['id']}/send",
        json={
            "session": KEY,
            "values": {"token": TYPED, "ticket": "t"},
            "expected_updated_at": t["updated_at"] - 1,
        },
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 409 and "edited" in r.json()["detail"]
    assert _drain(live_session) == b""


def test_the_guard_refuses_a_template_edited_mid_send(tmp_home, live_session):
    """The fence is re-checked under the write lock (`final_guard`), not only at entry."""
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    rendered = _render(t, {"token": TYPED, "ticket": "t"})
    templates.update_template(
        t["id"],
        {k: t[k] for k in ("name", "description", "tags", "body", "fields", "images")},
        t["updated_at"],
    )
    with pytest.raises(template_send.SendRefused, match="template was edited") as exc:
        template_send.deliver(
            engines_phys(),
            rendered,
            cwd=None,
            template_id=t["id"],
            expected_updated_at=t["updated_at"],
            has_images=False,
        )
    assert exc.value.status == 409
    assert _drain(live_session) == b""


def engines_phys() -> str:
    return template_send.engines.physical_key(KEY)


@pytest.mark.parametrize(
    "state, detail, stage, status, copy",
    [
        ("not_live", "", "clear", 409, "isn't running"),
        ("refused", "session never went quiet", "clear", 409, "busy"),
        ("stale", "scope: outside", "clear", 409, "project folders changed"),
        ("stale", "template: edited", "paste", 409, "template was edited"),
        ("stale", "a viewer attached during authorization", "clear", 409, "Something changed"),
        ("failed", "EBADF", "clear", 502, "nothing reached the agent"),
        ("timeout", "", "clear", 502, "nothing reached the agent"),
        ("aborted", "partial", "paste", 502, "part of the message may have reached"),
    ],
)
def test_every_seam_outcome_maps_to_what_it_means(state, detail, stage, status, copy):
    err = template_send._map(session_input.Outcome(state, detail), stage=stage)
    assert err.status == status and copy in err.detail


def test_a_paste_whose_enter_fails_says_typed_not_submitted(tmp_home, monkeypatch):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    rendered = _render(t, {"token": TYPED, "ticket": "t"})
    outcomes = iter(
        [
            session_input.Outcome("delivered"),
            session_input.Outcome("delivered"),
            session_input.Outcome("failed", "EBADF"),
        ]
    )
    monkeypatch.setattr(template_send.session_input, "send_input", lambda *a, **k: next(outcomes))
    with pytest.raises(template_send.SendRefused, match="typed but not submitted"):
        template_send.deliver(
            KEY,
            rendered,
            cwd=None,
            template_id=t["id"],
            expected_updated_at=t["updated_at"],
            has_images=False,
        )


# ---- redaction at the transport ----------------------------------------------------------------


@pytest.fixture
def ai(tmp_home, monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    prefs.set_ai_review(
        {"base_url": "https://ai.test/v1", "api_key": "sk-test-key-1234", "model": "m"}
    )
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    return sent


def _user_content(sent) -> str:
    return "\n".join(m["content"] for m in sent[-1]["messages"] if m["role"] != "system")


def test_a_stored_secret_never_reaches_the_ai_endpoint_in_any_covered_form(ai, tmp_home):
    _secret(value="p@ss word/with:chars")
    ansi = "p@ss \x1b[1mword\x1b[0m/with:chars"
    content = "\n".join(
        [
            "raw: p@ss word/with:chars",
            "json: " + json.dumps("p@ss word/with:chars")[1:-1],
            "url: " + "p%40ss%20word%2Fwith%3Achars",
            "plus: " + "p%40ss+word%2Fwith%3Achars",
            "ansi: " + ansi,
        ]
    )
    asyncio.run(review.complete_json([{"role": "user", "content": content}]))
    out = _user_content(ai)
    assert "p@ss word" not in out and "p%40ss" not in out and "word/with" not in out
    assert out.count("[secret]") == 5


def test_a_typed_once_secret_is_redacted_until_its_ttl(ai, tmp_home, monkeypatch):
    template_secrets.register_typed([TYPED])
    asyncio.run(review.complete_json([{"role": "user", "content": f"echo {TYPED}"}]))
    assert TYPED not in _user_content(ai)
    later = time.time() + template_secrets.TYPED_TTL_S + 1
    monkeypatch.setattr(template_secrets.time, "time", lambda: later)
    asyncio.run(review.complete_json([{"role": "user", "content": f"echo {TYPED}"}]))
    assert TYPED in _user_content(ai), "after the TTL the value is no longer tracked (documented)"


def test_system_messages_are_never_rewritten(tmp_home):
    _secret()
    msgs = [{"role": "system", "content": f"x {SECRET}"}, {"role": "user", "content": SECRET}]
    out = template_secrets.redact_messages(msgs)
    assert out[0] == msgs[0] and out[1]["content"] == "[secret]"


def test_text_without_a_secret_is_returned_byte_for_byte(tmp_home):
    _secret()
    text = "a \x1b[31mred\x1b[0m line, nothing secret here"
    assert template_secrets.redact_text(text) == text


def test_the_documented_residual_a_secret_wrapped_across_lines_is_not_caught(tmp_home):
    """Pinned so the claim matches the code: a TUI that wraps the value mid-way defeats an
    exact-variant match. The docs say so beside the agent-may-repeat-it warning."""
    _secret()
    wrapped = SECRET[:9] + "\n" + SECRET[9:]
    assert template_secrets.redact_text(wrapped) == wrapped


# ---- independent review of #1105 -------------------------------------------------------------


@pytest.mark.parametrize("how", ["replace", "delete"])
def test_a_replaced_or_deleted_secret_is_still_redacted(tmp_home, how):
    """The old value is in every transcript it was pasted into; it must not stop being redacted
    the moment it stops being stored."""
    rec = _secret()
    if how == "replace":
        template_vars.update_variable("db_pass", {"value": "the-new-value-1"}, rec["updated_at"])
    else:
        template_vars.delete_variable("db_pass", rec["updated_at"])
    assert template_secrets.redact_text(f"old {SECRET}") == "old [secret]"


def test_a_secret_that_stops_decrypting_is_still_redacted_for_the_ttl(tmp_home):
    """Key lost after the set was computed: the value it held keeps being redacted."""
    _secret()
    assert template_secrets.redact_text(SECRET) == "[secret]"  # computes the set
    template_secrets.key_path().unlink()
    assert template_vars.secret_values() == {}
    assert template_secrets.redact_text(SECRET) == "[secret]"


def test_the_quick_handoff_seed_is_redacted(tmp_home, monkeypatch):
    """The Quick seed goes to the browser (preview) and into another agent without `_post_chat`."""
    from agent_sessions import handoff

    _secret()
    monkeypatch.setattr(
        handoff,
        "_source_texts",
        lambda *_a: [("user", f"connect with {SECRET} please"), ("agent", f"ok, {SECRET} works")],
    )
    seed, _meta = handoff.build_quick_seed("claude", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert SECRET not in seed
    assert seed.count("[secret]") == 2


def test_a_lone_surrogate_is_a_422_before_anything_is_written(tmp_home):
    with pytest.raises(template_vars.VariableError, match="invalid character"):
        template_vars.create_variable({"name": "bad", "kind": "secret", "value": "abc\ud800defgh"})
    assert not template_secrets.key_path().exists()
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    with pytest.raises(templates.TemplateError, match="invalid character"):
        _render(_migration_template(), {"token": "typed\ud800token", "ticket": "t"})


def test_a_failed_key_write_leaves_no_key_file_at_all(tmp_home, monkeypatch):
    """A 0-byte key file would refuse every later secret write; the key is published whole."""
    real_write = os.write

    def failing(fd, data):
        if len(data) == template_secrets.KEY_BYTES:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    with monkeypatch.context() as m:
        m.setattr(template_secrets.os, "write", failing)
        with pytest.raises(OSError):
            _secret()
    key = template_secrets.key_path()
    assert not key.exists()
    assert not [p for p in key.parent.iterdir() if p.name.endswith(".tmp")]
    _secret()  # and the next attempt simply works
    assert len(key.read_bytes()) == 32


# ---- Hermes on #1105 ------------------------------------------------------------------------


def _post(messages):
    return asyncio.run(review.complete_json(messages))


@pytest.mark.parametrize("fail", ["stat", "read_text"])
def test_a_warm_cache_does_not_vouch_for_a_store_it_cannot_read(ai, tmp_home, monkeypatch, fail):
    """Round 2: secret B is stored AFTER the cache was warmed with A; then the store becomes
    unreadable. Redacting only A would send B — so the transport refuses outright."""
    _secret(name="a_secret", value="first-secret-aaa")
    _post([{"role": "user", "content": "warm"}])  # warms the cache with A
    _secret(name="b_secret", value="second-secret-bbb")
    real = getattr(Path, fail)
    store = template_vars.store_path()

    def boom(self, *a, **k):
        if self == store:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    before = len(ai)
    with monkeypatch.context() as m:
        m.setattr(Path, fail, boom)
        with pytest.raises(review.ReviewError, match="nothing was sent"):
            _post([{"role": "user", "content": "a first-secret-aaa b second-secret-bbb"}])
    assert len(ai) == before, "nothing may reach the endpoint"
    _post([{"role": "user", "content": "a first-secret-aaa b second-secret-bbb"}])
    out = _user_content(ai)
    assert "first-secret-aaa" not in out and "second-secret-bbb" not in out


def test_a_secret_with_damaged_metadata_is_recovered_from_its_envelope(ai, tmp_home):
    """A bad timestamp must not make a decryptable secret invisible to redaction."""
    _secret()
    path = template_vars.store_path()
    doc = json.loads(path.read_text())
    doc["variables"][0]["updated_at"] = "not a number"
    path.write_text(json.dumps(doc))
    template_secrets._reset_for_tests()  # cold
    _post([{"role": "user", "content": f"x {SECRET}"}])
    assert SECRET not in _user_content(ai)


def test_an_unrecoverable_secret_record_refuses_rather_than_skips(ai, tmp_home):
    _secret()
    path = template_vars.store_path()
    doc = json.loads(path.read_text())
    doc["variables"][0]["secret"] = {"kid": "zz"}  # a secret we cannot read at all
    path.write_text(json.dumps(doc))
    template_secrets._reset_for_tests()
    before = len(ai)
    with pytest.raises(review.ReviewError, match="nothing was sent"):
        _post([{"role": "user", "content": "anything"}])
    assert len(ai) == before


def test_a_cold_unreadable_store_refuses_to_send_and_recovery_is_not_cached_empty(
    ai, tmp_home, monkeypatch
):
    _secret()
    template_secrets._reset_for_tests()  # cold
    store = template_vars.store_path()
    real_read = Path.read_text

    def boom(self, *a, **k):
        if self == store:
            raise PermissionError(13, "Permission denied")
        return real_read(self, *a, **k)

    before = len(ai)
    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", boom)
        with pytest.raises(review.ReviewError, match="nothing was sent"):
            _post([{"role": "user", "content": f"c {SECRET}"}])
    assert len(ai) == before, "nothing may reach the endpoint"
    # Recovered: the set is established for real — the failed read was never cached as empty.
    _post([{"role": "user", "content": f"d {SECRET}"}])
    assert SECRET not in _user_content(ai)


def test_a_stored_secret_that_was_SENT_stays_redacted_after_its_key_vanishes(
    auth_cfg, tmp_home, live_session
):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    template_secrets._reset_for_tests()  # nothing pre-warmed
    template_send.send(
        t["id"],
        {
            "session": KEY,
            "values": {"token": TYPED, "ticket": "t"},
            "expected_updated_at": t["updated_at"],
        },
    )
    template_secrets.key_path().unlink()
    template_secrets._stored_cache = None  # as if the process never computed the set
    assert template_secrets.redact_text(f"x {SECRET} y {TYPED}") == "x [secret] y [secret]"


def test_a_secret_with_edge_whitespace_is_refused_stored_or_typed(tmp_home):
    with pytest.raises(template_vars.VariableError, match="whitespace"):
        template_vars.create_variable({"name": "p", "kind": "secret", "value": f" {SECRET} "})
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    with pytest.raises(templates.TemplateError, match="whitespace"):
        _render(_migration_template(), {"token": f"{TYPED} ", "ticket": "t"})


def test_overlapping_sends_into_one_session_are_refused_not_interleaved(
    tmp_home, live_session, monkeypatch
):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    entered, release = threading.Event(), threading.Event()
    real = template_send.deliver

    def slow(*a, **k):
        entered.set()
        release.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(template_send, "deliver", slow)
    body = {
        "session": KEY,
        "values": {"token": TYPED, "ticket": "t"},
        "expected_updated_at": t["updated_at"],
    }
    first = threading.Thread(target=template_send.send, args=(t["id"], body))
    first.start()
    assert entered.wait(5)
    with pytest.raises(template_send.SendRefused, match="Another template") as exc:
        template_send.send(t["id"], body)
    assert exc.value.status == 409
    release.set()
    first.join(5)


def test_a_key_removed_after_resolution_refuses_before_any_write(
    tmp_home, live_session, monkeypatch
):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    real = template_send.render

    def then_lose_key(*a, **k):
        out = real(*a, **k)
        template_secrets.key_path().unlink()
        return out

    monkeypatch.setattr(template_send, "render", then_lose_key)
    with pytest.raises(template_send.SendRefused, match="secret key changed"):
        template_send.send(
            t["id"],
            {
                "session": KEY,
                "values": {"token": TYPED, "ticket": "t"},
                "expected_updated_at": t["updated_at"],
            },
        )
    assert _drain(live_session) == b""


def test_a_key_removed_between_clear_and_paste_refuses_the_paste(
    tmp_home, live_session, monkeypatch
):
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    real = session_input.send_input
    calls = []

    def wrapped(key, payload, **kw):
        calls.append(payload)
        out = real(key, payload, **kw)
        if len(calls) == 1:
            template_secrets.key_path().unlink()
        return out

    monkeypatch.setattr(template_send.session_input, "send_input", wrapped)
    with pytest.raises(template_send.SendRefused) as exc:
        template_send.send(
            t["id"],
            {
                "session": KEY,
                "values": {"token": TYPED, "ticket": "t"},
                "expected_updated_at": t["updated_at"],
            },
        )
    assert exc.value.status == 409
    assert _drain(live_session) == b"\x01\x0b", "only the clear reached the session"


def test_a_template_edit_committed_after_the_final_guard_is_refused_in_the_fence(
    tmp_home, live_session, monkeypatch
):
    """The interleaving the earlier test missed: the edit lands AFTER `final_guard` returns and
    before the byte. The in-fence fingerprint (template revision) refuses it."""
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    real = session_input.send_input
    stage = {"n": 0}

    def wrapped(key, payload, **kw):
        stage["n"] += 1
        if stage["n"] == 2:  # the PASTE
            guard = kw["final_guard"]

            def then_edit():
                verdict = guard()
                cur = templates.get_template(t["id"])
                templates.update_template(
                    t["id"],
                    {
                        k: cur[k]
                        for k in ("name", "description", "tags", "body", "fields", "images")
                    },
                    cur["updated_at"],
                )
                return verdict

            kw = {**kw, "final_guard": then_edit}
        return real(key, payload, **kw)

    monkeypatch.setattr(template_send.session_input, "send_input", wrapped)
    with pytest.raises(template_send.SendRefused):
        template_send.send(
            t["id"],
            {
                "session": KEY,
                "values": {"token": TYPED, "ticket": "t"},
                "expected_updated_at": t["updated_at"],
            },
        )
    assert SECRET.encode() not in _drain(live_session), "the stale paste never reached the pty"


def test_a_short_key_write_publishes_no_key(tmp_home, monkeypatch):
    real_write = os.write
    calls = {"n": 0}

    def once_short(fd, data):
        calls["n"] += 1
        if calls["n"] == 1 and len(data) == template_secrets.KEY_BYTES:
            return real_write(fd, bytes(data[:16]))  # a real short write
        if calls["n"] == 2:
            return 0  # and then no progress at all
        return real_write(fd, data)

    with monkeypatch.context() as m:
        m.setattr(template_secrets.os, "write", once_short)
        with pytest.raises(OSError):
            _secret()
    assert not template_secrets.key_path().exists()
    _secret()
    assert len(template_secrets.key_path().read_bytes()) == 32


def test_a_scope_change_is_ordered_after_the_byte_it_raced(tmp_home, live_session, monkeypatch):
    """Round 2: roots/exclusions are in the fingerprint, so their setters take the same fence.
    An exclusion attempted right after the in-fence re-read cannot commit until the paste's byte
    is out — and the Enter that follows then sees it and refuses."""
    work = tmp_home / "work"
    work.mkdir()

    class Row:
        cwd = str(work)

    monkeypatch.setattr(template_send.engines, "resolve_session", lambda *_a, **_k: Row())
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()
    real = session_input.send_input
    stage = {"n": 0}
    racer: dict = {}

    def wrapped(key, payload, **kw):
        stage["n"] += 1
        if stage["n"] == 2:  # the paste
            fp = kw["policy_fingerprint"]

            def fingerprint_then_race():
                value = fp()
                racer["calls"] = racer.get("calls", 0) + 1
                # The seam reads the fingerprint twice: at entry, and again INSIDE the fence
                # right before byte one. Race the second.
                if racer["calls"] == 2 and "thread" not in racer:
                    racer["thread"] = threading.Thread(
                        target=lambda: prefs.set_folder_exclusions([str(work)])
                    )
                    racer["thread"].start()
                    racer["thread"].join(0.3)
                    # Still blocked: the writer holds the fence from this re-read to the byte.
                    racer["blocked"] = racer["thread"].is_alive()
                return value

            kw = {**kw, "policy_fingerprint": fingerprint_then_race}
        return real(key, payload, **kw)

    monkeypatch.setattr(template_send.session_input, "send_input", wrapped)
    with pytest.raises(template_send.SendRefused, match="typed but not submitted"):
        template_send.send(
            t["id"],
            {
                "session": KEY,
                "values": {"token": TYPED, "ticket": "t"},
                "expected_updated_at": t["updated_at"],
            },
        )
    racer["thread"].join(5)
    assert racer["blocked"] is True, "the exclusion setter must wait on the write fence"
    assert prefs.get_folder_exclusions() == [str(work)]


def test_a_contended_fence_does_not_stall_the_event_loop(auth_cfg, tmp_home):
    """Round 2: store mutations wait on the write fence OFF the loop. An unrelated coroutine keeps
    ticking while a variables POST waits on a fence another thread holds."""
    from agent_sessions import authfence

    app = create_app(auth_cfg)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as c:
            r = await c.post(
                "/login",
                data={"username": "marcus", "password": "hunter2"},
                headers={"Origin": auth_cfg.origin},
            )
            assert r.status_code in (200, 303)
            csrf = (await c.get("/api/config")).json()["csrf"]
            held, release = threading.Event(), threading.Event()

            def hold():
                with authfence.hold(timeout=5):
                    held.set()
                    release.wait(5)

            holder = threading.Thread(target=hold)
            holder.start()
            assert held.wait(5)
            gaps: list[float] = []

            async def heartbeat():
                last = time.monotonic()
                for _ in range(25):
                    await asyncio.sleep(0.02)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            async def post():
                return await c.post(
                    "/api/template-variables",
                    json={"name": "host", "value": "h"},
                    headers=_hdr(auth_cfg, csrf),
                )

            asyncio.get_running_loop().call_later(0.5, release.set)
            resp, _ = await asyncio.gather(post(), heartbeat())
            holder.join(5)
            return resp, gaps

    resp, gaps = asyncio.run(scenario())
    assert resp.status_code == 201
    # #1151: the loop's freedom is proved by the GAP CEILING: a fence-blocked loop cannot run
    # the heartbeat while the fence is held, so some gap reaches the fence's own 0.5s release
    # (or past it). The old <0.25s ceiling was scheduler time on the shared runner (0.485s
    # measured with the mechanism intact — the post still waited for the 0.5s release, not
    # past it), so the belt sits at <0.5s: a gap that long means the loop missed the release
    # entirely. The tick count below is only a coarse companion signal — the heartbeat's
    # fixed 25 iterations complete even after a stall, so a low count means the coroutine was
    # starved, not (by itself) that the loop was held.
    assert max(gaps) < 0.5, f"the loop stalled for {max(gaps):.3f}s — past the fence release"
    assert len(gaps) >= 10, f"the heartbeat only ticked {len(gaps)}/25 — the loop was starved"


# ---- Hermes on #1105, round 3 -------------------------------------------------------------------


def test_one_failed_key_read_during_decryption_refuses_and_is_never_cached(
    ai, tmp_home, monkeypatch
):
    """The signature reads the key fine; the NEXT key open (inside decryption) fails once. That
    must refuse the call — not send the secret, not cache an empty inventory under the valid
    key id — and the following healthy call must redact."""
    _secret()
    template_secrets._reset_for_tests()  # cold
    key = str(template_secrets.key_path())
    real_open = os.open
    opens = {"n": 0}

    def flaky(path, *a, **k):
        if str(path) == key:
            opens["n"] += 1
            if opens["n"] == 2:
                raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **k)

    before = len(ai)
    with monkeypatch.context() as m:
        m.setattr(template_secrets.os, "open", flaky)
        with pytest.raises(review.ReviewError, match="nothing was sent"):
            _post([{"role": "user", "content": f"x {SECRET}"}])
    assert opens["n"] >= 2, "the failure must have hit the decryption read"
    assert len(ai) == before
    assert template_secrets._stored_cache is None, "an incomplete inventory is never cached"
    _post([{"role": "user", "content": f"y {SECRET}"}])
    assert SECRET not in _user_content(ai)


def test_a_failed_usage_counter_never_turns_a_delivered_send_into_a_retry(
    auth_cfg, tmp_home, live_session, monkeypatch
):
    """After Enter, bookkeeping cannot fail the send: a 503 'try again' would double-submit."""
    template_vars.create_variable({"name": "host", "value": "h"})
    _secret()
    t = _migration_template()

    def busy(_tid):
        raise session_input.AuthorityFenceBusy("fence busy")

    monkeypatch.setattr(template_send.tstore, "mark_used", busy)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/templates/{t['id']}/send",
        json={
            "session": KEY,
            "values": {"token": TYPED, "ticket": "t"},
            "expected_updated_at": t["updated_at"],
        },
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 200, r.text
    assert r.json()["counted"] is False
    assert "[secret: token]" in r.json()["masked"]
    assert _drain(live_session).endswith(b"\r"), "the message was submitted"
