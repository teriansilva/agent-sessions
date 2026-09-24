"""AI-suggested templates (#1090, Phase 3).

What this pins:

* **Only what the operator typed, inside the boundary, scrubbed.** User text turns only (never
  agent output), from in-scope, non-archived sessions active in the window; stored secrets and
  ad-hoc credential shapes are gone before anything reaches the endpoint.
* **Drafts, validated, never raw.** Suggestions pass the same rules a save applies; the model's
  raw reply is never stored; a secret variable suggestion carries no value.
* **Only on request, and a failure keeps the last result.** No endpoint is a 409 before any
  transcript is read; a refusal to redact is a 503 with nothing sent; dismissals stick.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    handoff,
    prefs,
    prompts,
    review,
    template_secrets,
    template_suggest,
    template_vars,
    templates,
    transcript,
)
from agent_sessions.main import create_app

STORED = "stored-db-password-9"  # noqa: S105 — a test fixture value


@dataclass
class Row:
    engine: str
    uuid: str
    cwd: str
    last_mtime: float
    archived: bool = False


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


def _dated(turns_by_id: dict, rows: list) -> object:
    """An ``adapter_for`` stand-in whose undated turns carry their session's last activity as the
    time they were typed — the collector sends only dated turns inside the window."""
    when = {r.uuid: r.last_mtime for r in rows}

    def adapter(nid, _home):
        return [
            t if t.ts is not None else dataclasses.replace(t, ts=when[nid])
            for t in turns_by_id[nid]
        ]

    return lambda _engine: adapter


@pytest.fixture(autouse=True)
def _fresh():
    template_secrets._reset_for_tests()
    template_suggest._running = False
    yield
    template_secrets._reset_for_tests()


@pytest.fixture
def sessions(tmp_home, monkeypatch):
    """Four sessions: two in scope (recent), one excluded folder, one archived, one too old."""
    work = tmp_home / "work"
    (work / "app").mkdir(parents=True)
    (tmp_home / "private").mkdir()
    now = time.time()
    rows = [
        Row("claude", "a" * 8, str(work / "app"), now - 60),
        Row("claude", "b" * 8, str(work / "app"), now - 120),
        Row("claude", "c" * 8, str(tmp_home / "private"), now - 60),
        Row("claude", "d" * 8, str(work / "app"), now - 60, archived=True),
        Row("claude", "e" * 8, str(work / "app"), now - 40 * 86400),
    ]
    turns = {
        "a" * 8: [
            transcript.Turn(
                "user", "Read the latest Hermes review on PR 12, fix every note, push."
            ),
            transcript.Turn("assistant", "AGENT OUTPUT that must never be sent"),
            transcript.Turn("user", f"connect with password={STORED} to staging.acme.test now"),
            transcript.Turn("user", "/usage"),
            transcript.Turn(
                "user", "deploy with token: ghp_A1b2C3d4E5f6G7h8I9j0KlMnOpQrStUv please"
            ),
        ],
        "b" * 8: [
            transcript.Turn(
                "user", "Read the latest Hermes review on PR 12, fix every note, push."
            ),
            transcript.Turn("user", "clone https://marcus:hunter2secret@git.example.com/x.git ok"),
        ],
        "c" * 8: [transcript.Turn("user", "EXCLUDED FOLDER message that must not be sent")],
        "d" * 8: [transcript.Turn("user", "ARCHIVED session message that must not be sent")],
        "e" * 8: [transcript.Turn("user", "TOO OLD message that must not be sent at all")],
    }
    monkeypatch.setattr(template_suggest.engines, "scan_all_cached", lambda: rows)
    monkeypatch.setattr(template_suggest.transcript, "adapter_for", _dated(turns, rows))
    prefs.set_folder_exclusions([str(tmp_home / "private")])
    return rows


def test_collect_sends_only_the_operators_in_scope_recent_messages_scrubbed(sessions):
    template_vars.create_variable({"name": "db_pass", "kind": "secret", "value": STORED})
    out = template_suggest.collect()
    texts = [m["text"] for m in out["messages"]]
    blob = json.dumps(out)
    for leak in (
        "AGENT OUTPUT",
        "EXCLUDED FOLDER",
        "ARCHIVED",
        "TOO OLD",
        "/usage",
        STORED,
        "ghp_A1b2C3d4E5f6G7h8I9j0KlMnOpQrStUv",
        "hunter2secret",
    ):
        assert leak not in blob, leak
    # Identical messages are sent ONCE with how often and where they were sent.
    [review_msg] = [m for m in out["messages"] if m["text"].startswith("Read the latest")]
    assert review_msg["count"] == 2 and review_msg["sessions"] == 2
    # A message that shows a credential trigger is not sent at all — not even redacted.
    assert not any("staging.acme.test" in t for t in texts)
    assert out["stats"]["withheld"] == 3
    assert out["stats"]["sessions"] == 2


@pytest.mark.parametrize(
    "text, gone",
    [
        ("export API_KEY=abc123def456", "abc123def456"),
        ("password: 'my pass word'", "my pass word"),
        ('"client_secret": "s3cr3t-v4lue"', "s3cr3t-v4lue"),
        ("https://user:pa55word@host.example/repo", "pa55word"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
        ("use bearer abcdef0123456789xyz", "abcdef0123456789xyz"),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\n-----END OPENSSH PRIVATE KEY-----",
            "AAAAB3Nza",
        ),
        ("key sk_" + "live_51H8xYzAbCdEfGh1234567890Qw", "sk_" + "live_51H8xYzAbCdEfGh1234567890Qw"),
    ],
)
def test_scrub_removes_each_credential_shape(text, gone):
    out = template_suggest.scrub(text)
    assert gone not in out and "[redacted]" in out


@pytest.mark.parametrize(
    "text",
    [
        "run uv run pytest -q tests/test_template_suggest.py",
        "open src/agent_sessions/routes/templates.py and fix the route",
        "deploy to staging.acme.test after the review-and-merge-checklist",
        "the author field and the passage about tokens in general",
    ],
)
def test_scrub_leaves_ordinary_text_alone(text):
    assert template_suggest.scrub(text) == text


def test_validation_drops_what_a_save_would_refuse_and_never_keeps_a_secret_value(tmp_home):
    templates.create_template({"name": "Existing", "body": "x"})
    template_vars.create_variable({"name": "host", "value": "h"})
    obj = {
        "suggestions": [
            {
                "kind": "template",
                "name": "Fix review notes",
                "reason": "sent 17x",
                "count": 17,
                "body": "Read the review on {{pr}}, fix it.",
                "fields": [{"name": "pr", "label": "PR"}],
            },
            {"kind": "template", "name": "Bad field", "body": "{{X}}", "fields": [{"name": "X"}]},
            {"kind": "template", "name": "existing", "body": "dup"},
            {"kind": "template", "name": "Esc", "body": "a\x1b[201~b"},
            {"kind": "variable", "name": "api_token", "secret": True, "value": "sk-live-LEAKED"},
            {"kind": "variable", "name": "stage", "value": "staging.acme.test", "count": 23},
            {"kind": "variable", "name": "host", "value": "dup"},
            {"kind": "variable", "name": "creds", "value": "password=hunter22x"},
            {"kind": "unknown"},
        ]
    }
    sugs, dropped = template_suggest.validate_suggestions(
        obj, taken_templates={"existing"}, taken_vars={"host"}
    )
    names = [s["name"] for s in sugs]
    assert names == ["Fix review notes", "api_token", "stage"]
    secret = next(s for s in sugs if s["name"] == "api_token")
    assert secret["value"] == "" and secret["secret"] is True
    assert "LEAKED" not in json.dumps(sugs)
    assert dropped == 6


# ---- the route --------------------------------------------------------------------------------


@pytest.fixture
def endpoint(tmp_home, monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    prefs.set_ai_review(
        {"base_url": "https://ai.test/v1", "api_key": "sk-test-key-1234", "model": "m"}
    )
    sent: list[dict] = []
    reply = {
        "suggestions": [
            {
                "kind": "template",
                "name": "Fix review notes",
                "reason": "you sent this twice",
                "count": 2,
                "body": "Read the latest Hermes review on {{pr}}, fix every note, push.",
                "fields": [{"name": "pr", "label": "PR"}],
            },
        ],
        "raw_marker": "MODEL-RAW-REPLY-MARKER",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]
            },
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    return sent


def test_analyse_on_request_persists_validated_drafts_only_and_dismissals_stick(
    auth_cfg, sessions, endpoint
):
    template_vars.create_variable({"name": "db_pass", "kind": "secret", "value": STORED})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    before = c.get("/api/templates/suggestions").json()
    assert before == {"result": None, "configured": True}

    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    [sug] = r.json()["suggestions"]
    assert sug["name"] == "Fix review notes" and sug["fields"][0]["name"] == "pr"
    assert r.json()["stats"]["sessions"] == 2

    # What reached the endpoint: the registry prompt, and none of the scrubbed material.
    [body] = endpoint
    assert body["messages"][0]["role"] == "system"
    wire = json.dumps(body)
    for leak in (STORED, "hunter2secret", "AGENT OUTPUT", "EXCLUDED FOLDER"):
        assert leak not in wire, leak

    stored = template_suggest.store_path()
    assert stored.stat().st_mode & 0o777 == 0o600
    assert "MODEL-RAW-REPLY-MARKER" not in stored.read_text(), "the raw reply is never stored"

    d = c.post(f"/api/templates/suggestions/{sug['id']}/dismiss", headers=_hdr(auth_cfg, csrf))
    assert d.status_code == 204
    assert c.get("/api/templates/suggestions").json()["result"]["suggestions"] == []
    # A later analysis proposing the same draft keeps it dismissed.
    again = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert again.json()["suggestions"] == []


def test_no_endpoint_is_refused_before_any_transcript_is_read(auth_cfg, tmp_home, monkeypatch):
    read = []
    monkeypatch.setattr(template_suggest, "collect", lambda *a, **k: read.append(1) or {})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.get("/api/templates/suggestions").json()["configured"] is False
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 409 and "No AI endpoint" in r.json()["detail"]
    assert read == []


def test_a_failed_analysis_keeps_the_previous_result(auth_cfg, sessions, endpoint, monkeypatch):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    first = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf)).json()

    def boom(_request):
        return httpx.Response(502, text="bad gateway")

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(boom))
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 502 and "Analysis failed" in r.json()["detail"]
    assert (
        c.get("/api/templates/suggestions").json()["result"]["suggestions"] == first["suggestions"]
    )


def test_a_store_that_cannot_be_redacted_sends_nothing(auth_cfg, sessions, endpoint, monkeypatch):
    template_vars.create_variable({"name": "db_pass", "kind": "secret", "value": STORED})

    def refuse():
        raise template_secrets.RedactionUnavailable("the secret store could not be read")

    monkeypatch.setattr(template_suggest.template_secrets, "redaction_values", refuse)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 503 and "nothing was sent" in r.json()["detail"]
    assert endpoint == []


def test_analyses_are_single_flight(tmp_home):
    template_suggest._running = True
    with pytest.raises(template_suggest.AlreadyRunning):
        asyncio.run(template_suggest.analyse())


def test_dismiss_refuses_a_malformed_id(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/suggestions/../../x/dismiss", headers=_hdr(auth_cfg, csrf))
    assert r.status_code in (404, 405)
    r = c.post("/api/templates/suggestions/NOTHEX/dismiss", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 404


# ---- carried from #1105: the Quick handoff says why it refuses -----------------------------


def test_quick_handoff_that_cannot_redact_is_an_explained_503(tmp_home, monkeypatch):
    def refuse():
        raise template_secrets.RedactionUnavailable("the secret store could not be read")

    monkeypatch.setattr(handoff.template_secrets, "redaction_values", refuse)
    monkeypatch.setattr(handoff, "_source_texts", lambda *_a: [("user", "hi there")])
    with pytest.raises(handoff.HandoffError) as exc:
        handoff.build_quick_seed("claude", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert exc.value.status == 503 and "could not be redacted" in exc.value.detail


# ---- independent review of #1110 ---------------------------------------------------------------


def test_agent_authored_user_turns_are_never_collected(tmp_home, monkeypatch):
    """A compaction summary and a handoff seed arrive as USER turns but were written by an agent."""
    work = tmp_home / "work"
    work.mkdir()
    rows = [Row("claude", "f" * 8, str(work), time.time() - 60)]
    turns = [
        transcript.Turn(
            "user",
            "This session is being continued from a previous conversation that ran out of "
            "context. The conversation is summarized below: AGENT SUMMARY TEXT",
        ),
        transcript.Turn("user", "# Handoff — continued from claude:abc\n\n[agent] AGENT LINE"),
        transcript.Turn("user", "notes:\n[user] fix it\n[agent] AGENT REPLY INSIDE A SEED"),
        transcript.Turn("user", "Please run the full test suite again and report."),
    ]
    monkeypatch.setattr(template_suggest.engines, "scan_all_cached", lambda: rows)
    monkeypatch.setattr(template_suggest.transcript, "adapter_for", _dated({"f" * 8: turns}, rows))
    blob = json.dumps(template_suggest.collect())
    assert "AGENT" not in blob
    assert "Please run the full test suite" in blob


def test_a_session_archived_in_battlelab_is_not_collected_whatever_its_engine_says(
    tmp_home, monkeypatch
):
    """opencode/codex archive by sidecar only; the raw scan row still says archived=False."""
    from agent_sessions import metadata

    work = tmp_home / "work"
    work.mkdir()
    oc = "ses_" + "a" * 26
    rows = [
        Row("opencode", oc, str(work), time.time() - 60, archived=False),
        Row("claude", "g" * 8, str(work), time.time() - 60),
    ]
    turns = {
        oc: [transcript.Turn("user", "SIDECAR-ARCHIVED session message never to be sent")],
        "g" * 8: [transcript.Turn("user", "An ordinary live message worth counting here")],
    }
    monkeypatch.setattr(template_suggest.engines, "scan_all_cached", lambda: rows)
    monkeypatch.setattr(template_suggest.transcript, "adapter_for", _dated(turns, rows))
    monkeypatch.setattr(template_suggest.engines, "physical_key", lambda k: k)
    metadata.patch(f"opencode:{oc}", archived=True)
    blob = json.dumps(template_suggest.collect())
    assert "SIDECAR-ARCHIVED" not in blob
    assert "ordinary live message" in blob


@pytest.mark.parametrize(
    "text, gone",
    [
        ("curl -u admin:S3cretPass! https://x.test", "S3cretPass!"),
        ("curl --user admin:S3cretPass! https://x.test", "S3cretPass!"),
        ("mysql -u root -p'Sup3r$ecret' db", "Sup3r$ecret"),
        ("mysql -u root -pSup3rsecret db", "Sup3rsecret"),
        ("sshpass -p 'MyPassw0rd' ssh host", "MyPassw0rd"),
        ("tool --password hunter2 --verbose", "hunter2"),
        ("tool --password=hunter2", "hunter2"),
        ("docker login -u me -p dckr_pat_abcdef registry", "dckr_pat_abcdef"),
        ("redis-cli -h host -a pa55 ping", "pa55"),
    ],
)
def test_scrub_catches_command_flag_and_prose_credentials(text, gone):
    out = template_suggest.scrub(text)
    assert gone not in out, out


@pytest.mark.parametrize(
    "text",
    ["mkdir -p src/app", "ssh -p 2222 host", "git log -p --stat", "curl -u admin https://x"],
)
def test_scrub_leaves_ordinary_flags_alone(text):
    assert template_suggest.scrub(text) == text


def test_validation_reuses_the_collection_snapshot_and_never_re_derives_after_the_call(
    tmp_home, monkeypatch
):
    def refuse():
        raise template_secrets.RedactionUnavailable("gone")

    monkeypatch.setattr(template_suggest.template_secrets, "redaction_values", refuse)
    sugs, dropped = template_suggest.validate_suggestions(
        {
            "suggestions": [
                {"kind": "variable", "name": "stage", "value": "staging.acme.test"},
                {"kind": "variable", "name": "leak", "value": "has-SECRETVALUE-in"},
            ]
        },
        taken_templates=set(),
        taken_vars=set(),
        secrets=["SECRETVALUE"],
    )
    assert [s["name"] for s in sugs] == ["stage"] and dropped == 1


def test_case_duplicates_and_since_saved_drafts_do_not_linger(tmp_home):
    sugs, dropped = template_suggest.validate_suggestions(
        {
            "suggestions": [
                {"kind": "template", "name": "Deploy", "body": "go"},
                {"kind": "template", "name": "deploy", "body": "go now"},
            ]
        },
        taken_templates=set(),
        taken_vars=set(),
    )
    assert [s["name"] for s in sugs] == ["Deploy"] and dropped == 1
    template_suggest._write(lambda d: d.update({"suggestions": sugs, "stats": {}, "dropped": 0}))
    assert len(template_suggest.current()["suggestions"]) == 1
    templates.create_template({"name": "DEPLOY", "body": "saved from the editor"})
    assert template_suggest.current()["suggestions"] == []


# ---- Hermes on #1110 (review 5149) -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, gone",
    [
        ('tool --password "correct horse battery staple" next', "horse battery staple"),
        ("tool --password 'correct horse battery staple' next", "horse battery staple"),
        ('{"password": "ab\\"cd efgh"}', "cd efgh"),
        ('{\\"password\\": \\"esc apes here\\"}', "apes here"),
        ('password="never closed at all', "closed at all"),
        ('curl -u "admin:correct horse" https://x.test', "horse"),
        ("curl -u 'admin:correct horse' https://x.test", "horse"),
        ('redis-cli -a "two words" ping', "words"),
        ("mysql -u root -p'two words' db", "words"),
    ],
)
def test_scrub_consumes_quoted_and_escaped_values_whole(text, gone):
    out = template_suggest.scrub(text)
    assert gone not in out and "[redacted]" in out, out
    assert template_suggest.scrub(out) == out, "the scrub is idempotent"


@pytest.mark.parametrize(
    "text",
    ["deploy --password {{db_pass}} now", "password={{db_pass}}", "curl -u admin:{{pw}} https://x"],
)
def test_scrub_leaves_a_placeholder_alone(text):
    assert template_suggest.scrub(text) == text


def _session_saying(tmp_home, monkeypatch, *turns, ago=60):
    work = tmp_home / "work"
    work.mkdir(exist_ok=True)
    rows = [Row("claude", "h" * 8, str(work), time.time() - ago)]
    monkeypatch.setattr(template_suggest.engines, "scan_all_cached", lambda: rows)
    monkeypatch.setattr(
        template_suggest.transcript, "adapter_for", _dated({"h" * 8: list(turns)}, rows)
    )


def test_no_credential_substring_reaches_the_wire_from_messages_or_template_metadata(
    auth_cfg, tmp_home, endpoint, monkeypatch
):
    """Through the REAL outbound transport: a message or template description that shows a
    credential trigger is withheld whole; the rest is sent."""
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", "Run the full test suite and fix what fails, then push."),
        transcript.Turn("user", 'run tool --password "correct horse battery staple" please'),
        transcript.Turn("user", 'send {"password": "ab\\"cd efgh-quoted"} to the api'),
        transcript.Turn("user", 'curl -u "admin:spaced pass phrase" https://x.test now'),
    )
    templates.create_template(
        {"name": "Deploy", "description": "uses password=MetaHunter2x on staging", "body": "go"}
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf)).status_code == 200
    [body] = endpoint
    wire = json.dumps(body)
    for leak in ("horse", "battery", "cd efgh", "spaced pass", "phrase", "MetaHunter2x"):
        assert leak not in wire, leak
    assert "Run the full test suite" in wire
    assert "Deploy" in wire and "on staging" not in wire, "the name is sent, the description not"


def test_a_draft_that_echoes_a_credential_is_dropped_never_stored_or_returned(
    auth_cfg, tmp_home, endpoint, monkeypatch
):
    """A draft field holding a stored secret, or anything the draft net would redact, is dropped —
    the net for a credential that reached the model without a trigger (and so was sent) coming back
    reshaped. Placeholders, secret-source reads and ordinary text survive it."""
    template_vars.create_variable({"name": "db_pass", "kind": "secret", "value": STORED})
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", 'connect with password="EchoPw1xyz" to staging please'),
        # A shape the outbound scrub does not know: it reaches the model as typed.
        transcript.Turn("user", "log in to the db as root with Missed9xyz, then migrate"),
    )
    templates.create_template(
        {"name": "Deploy", "description": "uses token=MetaPw9xyz on staging", "body": "go"}
    )
    reply = {
        "suggestions": [
            {"kind": "template", "name": "Echo body", "body": "login with password=EchoPw1xyz"},
            {
                "kind": "template",
                "name": "Echo reason",
                "body": "deploy",
                "reason": "token: MetaPw9xyz",
            },
            {
                "kind": "template",
                "name": "Echo default",
                "body": "use {{p}}",
                "fields": [{"name": "p", "default": STORED}],
            },
            {
                "kind": "template",
                "name": "Api call",
                "body": 'curl -H "X-API-Key: {{api_key}}" --token "$(cat ~/.token)" https://x',
            },
            {
                "kind": "template",
                "name": "Reshaped",
                "body": "mysql -u root --password Missed9xyz db",
            },
            {"kind": "template", "name": "Code", "body": "call refreshToken() before retrying"},
        ]
    }

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]
            },
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200, r.text
    assert [s["name"] for s in r.json()["suggestions"]] == ["Api call", "Code"]
    assert r.json()["dropped"] == 4, "the net catches a missed credential the model reshaped"
    for leak in ("EchoPw1xyz", "MetaPw9xyz", STORED, "Missed9xyz"):
        assert leak not in r.text and leak not in template_suggest.store_path().read_text()


def test_the_window_is_the_messages_own_time_and_newest_is_sent_first(tmp_home, monkeypatch):
    """A session resumed today still holds turns typed long ago: those are not sent."""
    now = time.time()
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", "TYPED IN 2020 and resumed recently", ts=1_600_000_000.0),
        transcript.Turn("user", "Older message inside the window here", ts=now - 5 * 86400),
        transcript.Turn("user", "UNDATED message the engine gave no time"),
        transcript.Turn("user", "Newest message typed just a minute ago", ts=now - 60),
    )
    # _dated stamps undated turns; this one must stay undated to prove it is refused.
    undated = transcript.Turn("user", "UNDATED message the engine gave no time")
    adapter = template_suggest.transcript.adapter_for("claude")
    monkeypatch.setattr(
        template_suggest.transcript,
        "adapter_for",
        lambda _e: lambda nid, h: [t for t in adapter(nid, h) if "UNDATED" not in t.text]
        + [undated],
    )
    texts = [m["text"] for m in template_suggest.collect(now)["messages"]]
    assert texts == [
        "Newest message typed just a minute ago",
        "Older message inside the window here",
    ]

    monkeypatch.setattr(template_suggest, "MESSAGES_MAX", 1)
    [kept] = template_suggest.collect(now)["messages"]
    assert kept["text"].startswith("Newest"), "the cap keeps the most recently typed"


def test_the_transcript_adapters_date_each_user_turn(tmp_path):
    """The window rests on the adapters carrying the engine's own record time."""
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2020-09-13T12:26:40.000Z",
                "message": {"role": "user", "content": "hello from 2020"},
            }
        )
        + "\n"
    )
    [t] = transcript.claude_turns_from_jsonl(p)
    assert t.ts == 1_600_000_000.0
    assert transcript.Turn("user", "x", ts=1.0) == transcript.Turn(
        "user", "x"
    ), "ts is not identity"
    assert transcript._when(1_600_000_000_000) == 1_600_000_000.0  # milliseconds
    assert transcript._when("not a time") is None and transcript._when(None) is None


def _analysed(auth_cfg, c, csrf):
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("damage", ["malformed", "other-version", "unreadable"])
def test_an_unreadable_store_is_never_rewritten_from_empty(auth_cfg, sessions, endpoint, damage):
    import os

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    [sug] = _analysed(auth_cfg, c, csrf)["suggestions"]
    path = template_suggest.store_path()
    if damage == "malformed":
        path.write_text('{"version": 1, "suggestions": [')
    elif damage == "other-version":
        path.write_text(json.dumps({"version": 99, "suggestions": [], "dismissed": ["x"]}))
    else:
        if os.geteuid() == 0:
            pytest.skip("root reads a 000 file")
        path.chmod(0)
    before = (path.stat().st_ino, path.stat().st_mtime_ns)
    try:
        d = c.post(f"/api/templates/suggestions/{'f' * 16}/dismiss", headers=_hdr(auth_cfg, csrf))
        assert d.status_code == 503 and "left as it is" in d.json()["detail"]
        n = len(endpoint)
        a = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
        assert a.status_code == 503 and len(endpoint) == n, "refused before anything was sent"
        assert (path.stat().st_ino, path.stat().st_mtime_ns) == before, "the file was not replaced"
    finally:
        path.chmod(0o600)
    assert sug["id"]


# ---- Hermes review 5155 + the independent review of f216e2c -----------------------------------


@pytest.mark.parametrize(
    "text, gone",
    [
        ('curl -u admin:"correct horse battery" https://x.test', "horse"),
        ('curl -u "admin:ab\\"cd efgh" https://x.test', "cd efgh"),
        ('{\\"password\\": \\"ab\\\\\\"cd ef gh\\"}', "ef gh"),
        ("--password hunter2 at the start", "hunter2"),
        ("-u admin:hunter2 http://x.test", "hunter2"),
        ('-p"my pw" db', "pw"),
        ("password='abc'def'", "def"),
        ("password: “smart quotes pw”", "quotes"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n(cut off)", "b3Bl"),
    ],
)
def test_scrub_parses_the_whole_value_whatever_its_quoting(text, gone):
    out = template_suggest.scrub(text)
    assert gone not in out and "[redacted]" in out, out
    assert template_suggest.scrub(out) == out


@pytest.mark.parametrize(
    "text",
    [
        'curl -u "admin:{{pw}}" https://example.test',
        'curl -H "Authorization: Bearer {{token}}" https://api.test',
        "git clone https://{{user}}:{{token}}@host.test/r",
        "psql postgres://{{user}}:{{pass}}@{{host}}/db",
        "token: ${{ secrets.GITHUB_TOKEN }}",
        "export API_KEY=$API_KEY",
        "docker run -u 1000:1000 img",
        "mysql -h db -P3306 -u root",
        "secret: false",
        "auth: required",
        "src/agent_sessions/template_suggest_helpers_v2.py",
        "git push -u origin main",
        "if token == other: return",
        "deploy --password-stdin < pw.txt",
    ],
)
def test_scrub_leaves_placeholders_variables_and_ordinary_text_alone(text):
    assert template_suggest.scrub(text) == text


@pytest.mark.parametrize(
    "adversarial",
    [
        "token" + "-token" * 20_000,
        "a-" * 50_000,
        "x.auth.y-" * 11_000,
        '"' * 100_000,
        "\\" * 100_000,
        "password=" + '"\\' * 50_000,
        "-u " * 33_000,
        "{{" * 50_000,
        "mysql " + "x " * 50_000,
    ],
)
def test_scrub_is_linear_on_a_template_body_sized_input(adversarial):
    """A backtracking key pattern took minutes on a long dashed line; the scrub runs on model
    output up to a template body's 100k characters."""
    t0 = time.monotonic()
    template_suggest.scrub(adversarial)
    assert time.monotonic() - t0 < 2.0


def test_a_valid_quoted_placeholder_draft_is_kept_and_a_dropped_draft_reserves_no_name(tmp_home):
    obj = {
        "suggestions": [
            {"kind": "template", "name": "Fetch", "body": "curl -u admin:Hunter2echo https://x"},
            {
                "kind": "template",
                "name": "Fetch",
                "body": 'curl -u "admin:{{pw}}" https://example.test',
                "fields": [{"name": "pw"}],
            },
        ]
    }
    sugs, dropped = template_suggest.validate_suggestions(
        obj, taken_templates=set(), taken_vars=set(), secrets=["Hunter2echo"]
    )
    assert [s["body"] for s in sugs] == ['curl -u "admin:{{pw}}" https://example.test']
    assert dropped == 1


def test_validation_and_the_metadata_scrub_run_off_the_event_loop(
    auth_cfg, tmp_home, endpoint, monkeypatch
):
    import threading

    _session_saying(
        tmp_home, monkeypatch, transcript.Turn("user", "Please deploy staging again now.")
    )
    templates.create_template({"name": "Deploy", "description": "d", "body": "go"})
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = template_suggest.clean

    def spy(*a, **k):
        threads.append(threading.get_ident())
        return real(*a, **k)

    monkeypatch.setattr(template_suggest, "clean", spy)
    asyncio.run(template_suggest.analyse())
    assert threads and loop_thread not in threads


@pytest.mark.parametrize(
    "text, gone",
    [
        ("JWT_SECRET_KEY=hunter2x", "hunter2x"),
        ("secret_key: hunter2x", "hunter2x"),
        ("GITHUB_TOKEN_2=ghp_abc", "ghp_abc"),
        ("DB_PASSWORD_PROD=hunter2x", "hunter2x"),
        ("password1=hunter2x", "hunter2x"),
        ("password: ${X}hunter2x", "hunter2x"),
        ("password=hun}ter2x", "ter2x"),
        ("password: Pa;ss,w0rd", "w0rd"),
        ("password: $ecretPass1", "ecretPass1"),
        ("password: %hunter2%", "hunter2"),
        ('{"Authorization": "hunter2x"}', "hunter2x"),
        ('{"headers": {"Authorization": "Basic YWRtaW46aHVudGVy"}}', "YWRt"),
        ('Authorization: Bearer "abc def ghi"', "def"),
        ("'password' => 'hunter2x'", "hunter2x"),
        ("password:\n  hunter2x", "hunter2x"),
        ("password: `hunter 2x`", "2x"),
        ("postgres://u:p@ss@db/x", "ss@"),
        ("API key: sk-proj-Abc123XyZ", "sk-proj"),
        ("pw=hunter2x", "hunter2x"),
    ],
)
def test_scrub_catches_what_the_first_parser_let_through(text, gone):
    assert gone not in template_suggest.scrub(text)


@pytest.mark.parametrize(
    "text",
    [
        'curl -H "X-API-Key: {{api_key}}" https://api.example.com/v1',
        'curl -H "X-Api-Key: {{k}}" -H "Accept: json" https://x',
        'curl -H "X-Api-Key: $KEY" https://x',
    ],
)
def test_a_placeholder_inside_a_quoted_header_is_left_alone(text):
    assert template_suggest.scrub(text) == text


@pytest.mark.parametrize(
    "adversarial",
    [
        "token:" * 16_000,
        "-p“a " * 25_000,
        "-u “a " * 25_000,
        "Authorization:" * 7_000,
        "mysql -p“ " * 10_000,
        "--token “ " * 10_000,
        "password is “" * 8_000,
        "the password is " * 6_000,
    ],
)
def test_a_line_of_repeated_triggers_is_scanned_once(adversarial):
    t0 = time.monotonic()
    template_suggest.scrub(adversarial)
    assert time.monotonic() - t0 < 2.0


@pytest.mark.parametrize(
    "text, gone",
    [
        ("PGPASSWORD='Summer2024!' psql -h db -U app", "Summer"),
        ("SSHPASS=Summer2024! sshpass -e ssh host", "Summer"),
        ("export ACCESSTOKEN=correcthorse", "correct"),
        ("ROOTPW=correcthorse", "correct"),
        ("password: |\n  Summer2024!\n  more\nnext: 1", "Summer"),
        ("sshpass -pSummer2024! ssh host", "Summer"),
        ("curl -uadmin:Summer2024! https://x", "Summer"),
        ("password=hun&ter2secret", "ter2"),
        ('password="hunter2\nsecret"', "secret"),
    ],
)
def test_scrub_catches_run_together_names_block_values_and_glued_flags(text, gone):
    assert gone not in template_suggest.scrub(text)


@pytest.mark.parametrize(
    "text",
    [
        '--token "$(cat ~/.token)" https://x',
        "url?token={{t}}&next=1",
        "SECRET = process.env.SECRET",
        'curl -H "Authorization: Bearer {{token}}" -H "Accept: json" https://x',
        "call refreshToken() before retrying",
        "bypass the cache",
    ],
)
def test_the_draft_net_spares_substitutions_env_reads_and_prose(text):
    assert template_suggest.scrub(text) == text


# ---- Option A: withhold, never redact, on the way out (#1110) ----------------------------------

_ALL_LEAK_CASES = [
    "the password is hunter2, try it",
    'the password is "correct horse battery".',
    "password is: hunter2",
    "Password: correct horse battery staple",
    "API key: hunter2 secret",
    'password = os.getenv("DB_PASSWORD", "hunter2")',
    'PGPASSWORD="$(echo hunter2)" psql',
    'curl -H "Authorization: Basic $(echo -n admin:hunter2 | base64)" https://x',
    "PGPASSWORD='Summer2024!' psql -h db -U app",
    'curl -u admin:"correct horse battery" https://x.test',
    "export ACCESSTOKEN=correcthorse",
    "sshpass -pSummer2024! ssh host",
    "postgres://u:p@ss@db/x",
    "deploy with token: ghp_A1b2C3d4E5f6G7h8I9j0KlMnOpQrStUv please",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n",
    "use bearer abcdef0123456789xyz",
    "password: {{db_pass}}",  # a placeholder after a trigger is withheld too: it costs one message
]


@pytest.mark.parametrize("text", _ALL_LEAK_CASES)
def test_every_known_leak_shape_is_withheld_whole(text):
    assert template_suggest.credential_shaped(text)


@pytest.mark.parametrize(
    "text",
    [
        "Read the latest Hermes review on PR 12, fix every note, push.",
        "Make sure the session lookup happens after validation.",
        "git push -u origin main",
        "src/agent_sessions/template_suggest_helpers_v2.py",
        "docker compose up -d --build and tail the logs",
    ],
)
def test_ordinary_instructions_are_sent(text):
    assert not template_suggest.credential_shaped(text)


@pytest.mark.parametrize(
    "text",
    [
        "Review the auth middleware: make sure the token is validated before the session lookup.",
        "Rotate the secret: update the vault entry and redeploy {{service}}.",
        "Check that the API key is never logged in {{file}}.",
        "export OPENAI_API_KEY=$(cat ~/.config/openai/key)",
        "const token = process.env.API_TOKEN;",
    ],
)
def test_the_draft_net_keeps_ordinary_instructions_about_credentials(text):
    assert template_suggest.scrub(text) == text


@pytest.mark.parametrize(
    "text",
    [
        'password = os.getenv("DB_PASSWORD", "hunter2")',
        'PGPASSWORD="$(echo hunter2)" psql',
        "db = connect(password=os.environ.get('PGPASS', 'Sup3rS3cret'))",
    ],
)
def test_the_draft_net_does_not_take_a_fallback_or_an_echo_for_a_secret_read(text):
    assert template_suggest.scrub(text) != text


def test_an_ampersand_heavy_value_is_scanned_once():
    t0 = time.monotonic()
    template_suggest.scrub("token=a&" * 12_000)
    template_suggest.scrub("".join(f"&access_token=abc{i}" for i in range(3_000)))
    assert time.monotonic() - t0 < 2.0


# ---- "write me a template for …" ----------------------------------------------------------------


def _replying(monkeypatch, obj) -> list[dict]:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = obj if isinstance(obj, str) else json.dumps(obj)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    return sent


def test_write_drafts_one_template_from_the_request_alone_and_stores_nothing(
    auth_cfg, tmp_home, endpoint, sessions, monkeypatch
):
    template_vars.create_variable({"name": "staging_host", "value": "staging.acme.test"})
    template_vars.create_variable({"name": "db_pass", "kind": "secret", "value": STORED})
    sent = _replying(
        monkeypatch,
        {
            "name": "Migrate staging",
            "description": "Run the migrations on staging and report.",
            "body": "ssh {{staging_host}} and run the {{app}} migrations with {{db_pass}}.",
            "fields": [
                {"name": "staging_host", "label": "Host", "default": "ignored"},
                {"name": "app", "label": "App", "default": "api"},
                {"name": "db_pass", "label": "DB password"},
            ],
        },
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/templates/write",
        json={"request": "run the database migrations on staging"},
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 200, r.text
    t = r.json()["template"]
    assert t["name"] == "Migrate staging"
    fields = {f["name"]: f for f in t["fields"]}
    assert fields["staging_host"]["source"] == "library" and fields["staging_host"]["default"] == ""
    assert fields["db_pass"]["source"] == "library" and fields["db_pass"]["kind"] == "secret"
    assert fields["app"] == {
        "name": "app",
        "label": "App",
        "default": "api",
        "source": "template",
        "kind": "text",
    }
    [body] = sent
    assert body["messages"][0]["content"] == prompts.effective("template_write")
    wire = json.dumps(body)
    assert "run the database migrations on staging" in wire
    assert "staging_host" in wire and "db_pass" in wire, "library names, so the draft can use them"
    for leak in (STORED, "staging.acme.test", "Read the latest Hermes review"):
        assert leak not in wire, leak  # no values, no transcripts
    assert templates.list_templates() == [], "a draft is never saved"
    assert not template_suggest.store_path().exists()


def test_write_refuses_a_request_that_shows_a_credential_before_anything_is_sent(
    auth_cfg, tmp_home, endpoint, monkeypatch
):
    sent = _replying(monkeypatch, {})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    for req in ("deploy with password=hunter2x please", "", "x" * 2_001):
        r = c.post("/api/templates/write", json={"request": req}, headers=_hdr(auth_cfg, csrf))
        assert r.status_code == 422, r.text
    assert (
        "nothing was sent"
        in c.post(
            "/api/templates/write",
            json={"request": "log in with token: abc12345"},
            headers=_hdr(auth_cfg, csrf),
        ).json()["detail"]
    )
    assert sent == []


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        {"name": "", "body": "x"},
        {"name": "Leaky", "body": "login with --password Hunter2xyz now"},
        {
            "name": "Leaky default",
            "body": "use {{p}}",
            "fields": [{"name": "p", "default": "token=abc12345"}],
        },
    ],
)
def test_write_refuses_a_draft_it_would_not_save(auth_cfg, tmp_home, endpoint, monkeypatch, reply):
    _replying(monkeypatch, reply)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/templates/write",
        json={"request": "a code review template"},
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 502, r.text
    assert "Hunter2xyz" not in r.text and "abc12345" not in r.text


def test_write_needs_an_endpoint_and_a_session(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    assert c.post("/api/templates/write", json={"request": "x"}).status_code in (401, 403)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/write", json={"request": "a template"}, headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 409 and "No AI endpoint" in r.json()["detail"]


# ---- independent review of 77cef96 --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "redis://:hunter2@localhost:6379",
        "use password hunter2",
        "the db password hunter2 should work",
        "user admin pass hunter2",
        "password for staging is hunter2",
        "echo hunter2 | sudo -S apt install",
        "echo 'hunter2' | docker login -u me --password-stdin",
        "htpasswd -b file admin hunter2",
        "OPENAI_KEY=sk-abc",
    ],
)
def test_a_credential_word_next_to_a_value_is_withheld(text):
    assert template_suggest.credential_shaped(text)


@pytest.mark.parametrize(
    "text",
    [
        "resume session https://example.test/s/claude/376884c8-0362-464d-a249-c9c8b0aec500",
        "the session id is 376884c8-0362-464d-a249-c9c8b0aec500",
        "pass 3 tests then stop",
        "token counts look wrong in the usage panel",
    ],
)
def test_ids_and_ordinary_words_are_sent(text):
    assert not template_suggest.credential_shaped(text)


@pytest.mark.parametrize(
    "request_text",
    [
        "adding an API endpoint with bearer auth",
        "debugging a failing login where the password is rejected",
        "log into the db with --password {{pw}}",
    ],
)
def test_a_write_request_is_judged_by_value_not_by_trigger(
    auth_cfg, tmp_home, endpoint, monkeypatch, request_text
):
    sent = _replying(monkeypatch, {"name": "Draft", "body": "Do the thing."})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/write", json={"request": request_text}, headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200, r.text
    assert len(sent) == 1


def test_a_malformed_field_name_is_a_502_not_a_crash(auth_cfg, tmp_home, endpoint, monkeypatch):
    _replying(monkeypatch, {"name": "x", "body": "b", "fields": [{"name": ["a"]}]})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/write", json={"request": "a template"}, headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 502
    assert not template_suggest._writing


# ---- Hermes review 5187: separators, command context, the write gate ---------------------------

_REVIEW_5187 = [
    "curl -u  admin:shortpass https://x.test",
    "curl -u\tadmin:shortpass https://x.test",
    "mysql " + "--host db " * 40 + "-pshortpass database",
    "docker login " + "--username me " * 30 + "-p shortpass registry",
    "redis-cli " + "-h host " * 60 + "-a shortpass ping",
]


@pytest.mark.parametrize("text", _REVIEW_5187)
def test_separator_runs_and_long_commands_never_reach_the_endpoint_or_a_draft(
    auth_cfg, tmp_home, endpoint, monkeypatch, text
):
    """Through the real routes and transport: the message is withheld, the same text as a write
    request is refused unsent, and the model echoing it back is dropped."""
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", "Run the full test suite and fix what fails, then push."),
        transcript.Turn("user", text),
    )
    sent = _replying(
        monkeypatch,
        {"suggestions": [{"kind": "template", "name": "Echo", "body": text}]},
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200, r.text
    assert [len(sent), r.json()["suggestions"], r.json()["dropped"]] == [1, [], 1]
    assert "shortpass" not in json.dumps(sent) and "shortpass" not in r.text
    assert "shortpass" not in template_suggest.store_path().read_text()
    w = c.post("/api/templates/write", json={"request": text}, headers=_hdr(auth_cfg, csrf))
    assert w.status_code == 422 and len(sent) == 1


@pytest.mark.parametrize("request_text", ["password = correcthorse", "the password is hunter2"])
def test_a_write_request_that_declares_a_password_is_refused_unsent(
    auth_cfg, tmp_home, endpoint, monkeypatch, request_text
):
    sent = _replying(monkeypatch, {"name": "Draft", "body": "Do the thing."})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/write", json={"request": request_text}, headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422 and sent == []


def test_the_command_scan_has_no_distance_cutoff_and_stays_linear():
    near = "mysql --host db -pshortpass db"
    far = "mysql " + "--host db " * 500 + "-pshortpass db"
    assert template_suggest.credential_shaped(near) and template_suggest.credential_shaped(far)
    assert "shortpass" not in template_suggest.scrub(far)
    t0 = time.monotonic()
    for adv in ("mysql -px " * 10_000, "docker login " * 8_000 + "-p x", "redis-cli -a " * 8_000):
        template_suggest.credential_shaped(adv)
        template_suggest.scrub(adv)
    assert time.monotonic() - t0 < 3.0


# ---- Hermes review 5191: continuations, declarations, a quoted -u ------------------------------

_CONTINUED = [
    "mysql --host db \\\n  -pshortpass database",
    "docker login -u me \\\n  -p shortpass registry",
    "redis-cli -h host \\\n  -a shortpass ping",
]


@pytest.mark.parametrize("text", _CONTINUED)
def test_a_continued_command_keeps_its_context_on_every_route(
    auth_cfg, tmp_home, endpoint, monkeypatch, text
):
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", "Run the full test suite and fix what fails, then push."),
        transcript.Turn("user", text),
    )
    sent = _replying(
        monkeypatch, {"suggestions": [{"kind": "template", "name": "Echo", "body": text}]}
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200 and r.json()["suggestions"] == []
    assert "shortpass" not in json.dumps(sent)
    assert "shortpass" not in template_suggest.store_path().read_text()
    w = c.post("/api/templates/write", json={"request": text}, headers=_hdr(auth_cfg, csrf))
    assert w.status_code == 422 and len(sent) == 1


def test_a_quoted_user_argument_whose_password_starts_with_a_space_is_withheld(
    auth_cfg, tmp_home, endpoint, monkeypatch
):
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", "Run the full test suite and fix what fails, then push."),
        transcript.Turn("user", 'curl -u "admin: shortpass" https://x.test'),
    )
    sent = _replying(monkeypatch, {"suggestions": []})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf)).status_code == 200
    assert len(sent) == 1 and "shortpass" not in json.dumps(sent)


@pytest.mark.parametrize(
    "request_text", ["the password is correcthorse", "the password is CorrectHorse"]
)
def test_a_declared_password_is_refused_whatever_its_complexity(
    auth_cfg, tmp_home, endpoint, monkeypatch, request_text
):
    sent = _replying(monkeypatch, {"name": "Draft", "body": request_text})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/write", json={"request": request_text}, headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422 and sent == []
    # …and the same declaration coming back in a draft is dropped by the net.
    with pytest.raises(template_suggest.DraftInvalid):
        template_suggest.validate_draft(
            {"name": "Draft", "body": request_text}, library={}, secrets=[]
        )


@pytest.mark.parametrize(
    "request_text",
    [
        "debugging a failing login where the password is rejected",
        "the token was expired when the job retried",
        "make sure the secret is stored hashed",
        "the password is {{pw}}",
    ],
)
def test_instructional_prose_about_passwords_is_still_written(
    auth_cfg, tmp_home, endpoint, monkeypatch, request_text
):
    sent = _replying(monkeypatch, {"name": "Draft", "body": "Do the thing."})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/write", json={"request": request_text}, headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200, r.text
    assert len(sent) == 1


# ---- Hermes review 5193: quoted declarations, a continuation inside the command -----------------


@pytest.mark.parametrize(
    "request_text",
    [
        'the password is "copperseed"',
        'the password is "orchardking"',
        'the password is "correct horse battery staple"',
        "the password is correct horse battery staple",
    ],
)
def test_a_quoted_or_multiword_declaration_is_refused_and_never_kept(
    auth_cfg, tmp_home, endpoint, monkeypatch, request_text
):
    sent = _replying(monkeypatch, {"name": "Draft", "body": request_text})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/write", json={"request": request_text}, headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422 and sent == []
    with pytest.raises(template_suggest.DraftInvalid):
        template_suggest.validate_draft(
            {"name": "Draft", "body": request_text}, library={}, secrets=[]
        )


def test_a_continuation_inside_the_command_name_keeps_its_context(
    auth_cfg, tmp_home, endpoint, monkeypatch
):
    text = "docker \\\n login -u me -p shortpass registry"
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", "Run the full test suite and fix what fails, then push."),
        transcript.Turn("user", text),
    )
    sent = _replying(
        monkeypatch, {"suggestions": [{"kind": "template", "name": "Echo", "body": text}]}
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200 and r.json()["suggestions"] == []
    assert "shortpass" not in json.dumps(sent)
    assert "shortpass" not in template_suggest.store_path().read_text()
    w = c.post("/api/templates/write", json={"request": text}, headers=_hdr(auth_cfg, csrf))
    assert w.status_code == 422 and len(sent) == 1


# ---- Hermes review 5197: shell-correct continuations; known secrets on the original bytes -------

_INTERIOR = [
    "doc\\\nker login -u me -p shortpass registry",
    "docker lo\\\ngin -u me -p shortpass registry",
    "mysql -\\\npshortpass database",
]


@pytest.mark.parametrize("text", _INTERIOR)
def test_a_continuation_inside_a_word_or_flag_is_read_as_the_shell_reads_it(
    auth_cfg, tmp_home, endpoint, monkeypatch, text
):
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", "Run the full test suite and fix what fails, then push."),
        transcript.Turn("user", text),
    )
    sent = _replying(
        monkeypatch, {"suggestions": [{"kind": "template", "name": "Echo", "body": text}]}
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200 and r.json()["suggestions"] == []
    assert "shortpass" not in json.dumps(sent)
    assert "shortpass" not in template_suggest.store_path().read_text()
    w = c.post("/api/templates/write", json={"request": text}, headers=_hdr(auth_cfg, csrf))
    assert w.status_code == 422 and len(sent) == 1


def test_a_known_secret_containing_a_continuation_is_still_caught_in_a_draft(
    auth_cfg, tmp_home, endpoint, monkeypatch
):
    secret = "multi\\\nline-Secret-42"
    template_secrets.register_typed([secret])
    assert secret in template_secrets.redaction_values()
    _session_saying(
        tmp_home,
        monkeypatch,
        transcript.Turn("user", "Run the full test suite and fix what fails, then push."),
    )
    _replying(
        monkeypatch,
        {"suggestions": [{"kind": "template", "name": "Echo", "body": f"use {secret} now"}]},
    )
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates/suggest", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200 and r.json()["suggestions"] == []
    assert "line-Secret-42" not in template_suggest.store_path().read_text()
    _replying(monkeypatch, {"name": "Echo", "body": f"use {secret} now"})
    w = c.post(
        "/api/templates/write", json={"request": "a deploy template"}, headers=_hdr(auth_cfg, csrf)
    )
    assert w.status_code == 502 and "line-Secret-42" not in w.text
