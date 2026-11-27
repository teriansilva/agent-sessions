"""HQ sets up project MCP servers (#2163 P2): `list_mcp_servers` and the `propose_mcp` cards, and a
project's own credential through `propose_secret`.

Pinned here: the list carries no credential value; staging refuses a credential value in any
argument; a create card registers exactly its bound draft through the route; an install card
shows the review's facts and is `stale` after a source change; a check card settles to words and
carries no credential; an enable card is `stale` when the server's revision moved, and a project
without a passing check is refused in words; no MCP or secret card is ever voice-confirmable; a
project's own secret is created project-scoped and its value appears in no Ask record, sidecar,
log or model request; server and project ids come from this turn's `list_mcp_servers`.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import dataclasses
import json
import logging
import time

import pytest

from agent_sessions import (
    ask_mcp,
    ask_operations,
    ask_store,
    mcp_check,
    mcp_install,
    mcp_registry,
    template_secrets,
    template_vars,
    variables,
    voice_calls,
)
from test_ask_operations import _project, _result, live  # noqa: F401
from test_ask_orchestrator import ai, answer, app, call, calls  # noqa: F401

CANARY = "zq9-mcp-card-Vh3kP0wLx7-2163"


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MCP_SERVERS", str(tmp_path / "mcp-servers.json"))
    monkeypatch.setattr(mcp_registry, "_ALLOW_LOOPBACK_HTTP", True)


def _ask(a, ai, *steps, cid: str | None = None, question: str = "set it up"):
    script = ai(*steps, answer("Proposed; confirm it on the card."))
    cid = cid or a.new()
    r = a.ask(cid, question)
    assert r.status_code == 200, r.text
    return cid, script


def _cards(a, cid: str) -> list[dict]:
    return [p for t in a.get(cid)["turns"] for p in t["proposals"]]


def _decide(a, pid: str, inputs: dict | None = None):
    body: dict = {"decision": "confirm"}
    if inputs is not None:
        body["inputs"] = inputs
    return a.c.post(f"/api/ask/proposals/{pid}/decide", json=body, headers=a.hdr)


def _settled(a, cid: str, pid: str, timeout: float = 10.0) -> dict:
    import time

    end = time.monotonic() + timeout
    while True:
        card = next(c for c in _cards(a, cid) if c["action_id"] == pid)
        if card["state"] != "deciding" or time.monotonic() > end:
            return card
        time.sleep(0.05)


def _remote(name="docs", url="https://mcp.example.test/mcp", **extra) -> dict:
    return mcp_registry.create({"name": name, "source": {"kind": "remote", "url": url}, **extra})


def _npm(name="nextcloud", version="0.14.2") -> dict:
    return mcp_registry.create(
        {
            "name": name,
            "source": {"kind": "npm", "package": "nextcloud-mcp-server", "version": version},
            "settings": [
                {"key": "NEXTCLOUD_HOST", "value": "https://cloud.example.test"},
                {"key": "NEXTCLOUD_APP_PASSWORD", "kind": "credential", "variable": "nc_pass"},
            ],
        }
    )


def _pass_check(sid: str, scope: str, tools=("search", "read")) -> dict:
    rec = mcp_registry.get(sid)
    started = (rec["rev"], mcp_registry.resolve(rec, scope).digest)
    return mcp_registry.record_check(
        sid, scope, started, "passed", steps=[{"step": "tools", "ok": True}], tools=list(tools)
    )


def _no_canary_anywhere(script=None, *texts: str) -> None:
    for path in ask_store.root().rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path
    if script is not None:
        assert CANARY not in json.dumps(script.bodies)
    for t in texts:
        assert CANARY not in t


# ---- the list -----------------------------------------------------------------------------------


def test_the_list_names_sources_and_state_never_a_credential_value(live, ai, mcp):
    atlas = _project(live, "Atlas")
    variables.create({"name": "nc_pass", "kind": "secret", "value": CANARY})
    sid = _npm()["id"]
    cid, script = _ask(live, ai, calls(("list_mcp_servers", {})))
    out = _result(script, 1)
    (row,) = out["servers"]
    assert row["id"] == sid and row["name"] == "nextcloud"
    assert row["source"] == "npm nextcloud-mcp-server 0.14.2" and row["installed"] is False
    assert {"key": "NEXTCLOUD_APP_PASSWORD", "kind": "credential", "variable": "nc_pass"} in (
        row["settings"]
    )
    assert {"id": atlas, "name": "Atlas"} in out["projects"]
    assert row["other_projects"] == {"missing": 1}  # Atlas binds nothing: missing, off
    _no_canary_anywhere(script)


# ---- create -------------------------------------------------------------------------------------


_SRC = {"kind": "remote", "url": "https://mcp.example.test/mcp"}


@pytest.mark.parametrize(
    "extra",
    [
        {
            "settings": [
                {"key": "Authorization", "kind": "credential", "variable": "k", "value": CANARY}
            ]
        },
        {"settings": [{"key": "X-Api-Key", "value": CANARY}]},
        {"value": CANARY},
        {"probe": {"tool": "write_all", "arguments": {}, "confirmed_write": True}},
    ],
)
def test_staging_refuses_a_credential_value_and_a_write_probe(live, ai, mcp, extra):
    cid, script = _ask(
        live, ai, call("propose_mcp", {"action": "create", "name": "docs", "source": _SRC, **extra})
    )
    assert _cards(live, cid) == []
    assert "refused" in json.dumps(_result(script, 1))
    (step,) = [r for r in ask_store.read(cid) if r.get("name") == "propose_mcp"]
    assert step["meta"]["error"] == "never"
    assert mcp_registry.list_servers() == []
    _no_canary_anywhere()


def test_a_create_card_registers_exactly_its_bound_draft_through_the_route(live, ai, mcp):
    settings = [
        {"key": "X-Tenant", "value": "acme"},
        {
            "key": "Authorization",
            "kind": "credential",
            "variable": "docs_token",
            "prefix": "Bearer ",
        },
    ]
    args = {"action": "create", "name": "docs", "source": _SRC, "settings": settings}
    cid, script = _ask(live, ai, call("propose_mcp", args))
    (card,) = _cards(live, cid)
    assert card["op"] == "mcp.create" and card["title"] == "docs"
    assert "enables and runs nothing yet" in card["text"] and "docs_token" in card["text"]
    shown = live.c.get(f"/api/ask/proposals/{card['action_id']}/content", headers=live.hdr)
    assert shown.status_code == 200
    server = json.loads(shown.json()["files"][0]["text"])
    assert server["name"] == "docs" and server["settings"][1]["variable"] == "docs_token"
    method, path, body = ask_operations.CATALOG["mcp.create"].route(
        "", {"_content": {"server": server}}, {}
    )
    assert (method, path, body) == ("POST", "/api/mcp/servers", server)

    assert _decide(live, card["action_id"]).status_code == 202
    done = _settled(live, cid, card["action_id"])
    assert done["state"] == "done", done
    (rec,) = mcp_registry.list_servers()
    assert rec["name"] == "docs" and rec["projects"] == [] and rec["probe"] is None
    assert [s.get("variable") for s in rec["settings"]] == [None, "docs_token"]


def test_a_taken_name_is_refused_at_staging(live, ai, mcp):
    _remote()
    cid, script = _ask(
        live, ai, call("propose_mcp", {"action": "create", "name": "docs", "source": _SRC})
    )
    assert _cards(live, cid) == [] and "exists" in json.dumps(_result(script, 1))


# ---- install ------------------------------------------------------------------------------------


@pytest.fixture
def reviews(monkeypatch):
    seen: dict = {"installed": []}

    def review(rec):
        seen["reviewed"] = rec["source"]
        return {
            "review_id": "a1b2c3d4e5f60718",
            "source": rec["source"],
            "integrity": "sha512-9fK2xQe7vB0mPz4LrT8uW3nYcJd1sHa6GkE5oVtqQw==",
            "dependencies": 23,
            "install_scripts_declared": ["esbuild", "sharp"],
            "runs": "node …/dist/index.js (stdio)",
        }

    def install(rec, review_id):
        seen["installed"].append(review_id)
        return {"source": rec["source"], "command": "/bin/true", "args": []}

    monkeypatch.setattr(mcp_install, "review", review)
    monkeypatch.setattr(mcp_install, "install", install)
    return seen


def _install_card(live, ai) -> tuple[str, dict, str]:
    sid = _npm()["id"]
    cid, _ = _ask(
        live,
        ai,
        calls(("list_mcp_servers", {})),
        call("propose_mcp", {"action": "install", "id": sid}, cid="p"),
    )
    (card,) = _cards(live, cid)
    return cid, card, sid


def test_an_install_card_shows_the_review_and_installs_exactly_it(live, ai, mcp, reviews):
    cid, card, sid = _install_card(live, ai)
    text = card["text"]
    assert card["op"] == "mcp.install" and card["risk"] == "external"
    for said in (
        "npm nextcloud-mcp-server 0.14.2 exactly",
        "sha512-9fK2xQe7vB0m…",
        "23 locked dependencies",
        "install scripts never run (declared by esbuild, sharp)",
        "node …/dist/index.js (stdio)",
        ask_mcp.THIRD_PARTY,
    ):
        assert said in text, said
    assert reviews["installed"] == []  # staging reviewed; nothing installed
    assert _decide(live, card["action_id"]).status_code == 202
    settled = _settled(live, cid, card["action_id"])
    assert settled["state"] == "done"
    # The tap STARTS the install (an operation): the card never says "installed" before it is.
    assert "Installed." not in json.dumps(settled) or reviews["installed"]
    deadline = time.monotonic() + 10
    while not (mcp_registry.get(sid).get("installed") or {}).get("source"):
        assert time.monotonic() < deadline, "the install never finished"
        time.sleep(0.05)
    assert reviews["installed"] == ["a1b2c3d4e5f60718"]
    assert mcp_registry.get(sid)["installed"]["source"]["version"] == "0.14.2"
    # One card is one operation: the request id is fixed by server + review.
    assert (
        ask_mcp.install_request_id(sid, "a1b2c3d4e5f60718")
        == ask_mcp.install_request_id(sid, "a1b2c3d4e5f60718")
        != ask_mcp.install_request_id(sid, "a1b2c3d4e5f60719")
    )


def test_an_install_card_is_stale_after_the_source_changed(live, ai, mcp, reviews):
    cid, card, sid = _install_card(live, ai)
    rec = mcp_registry.get(sid)
    source = {**rec["source"], "version": "0.15.0"}
    mcp_registry.update(sid, {"source": source}, rec["updated_at"])
    assert _decide(live, card["action_id"]).status_code == 202
    settled = _settled(live, cid, card["action_id"])
    assert settled["state"] == "stale" and "source changed" in settled["reason"]
    assert reviews["installed"] == []


def test_a_remote_server_has_no_install_card(live, ai, mcp, reviews):
    sid = _remote()["id"]
    cid, script = _ask(
        live,
        ai,
        calls(("list_mcp_servers", {})),
        call("propose_mcp", {"action": "install", "id": sid}, cid="p"),
    )
    assert _cards(live, cid) == [] and "installs nothing" in json.dumps(_result(script, 2))


# ---- check --------------------------------------------------------------------------------------


@pytest.fixture
def checks(monkeypatch):
    """The check route's runner, settling a scripted result through the real store."""
    state: dict = {"outcome": "failed", "steps": [], "tools": []}

    async def run_check(sid, scope):
        rec = mcp_registry.get(sid)
        started = (rec["rev"], mcp_registry.resolve(rec, scope).digest)
        return mcp_registry.record_check(
            sid, scope, started, state["outcome"], steps=state["steps"], tools=state["tools"]
        )

    monkeypatch.setattr(mcp_check, "run_check", run_check)
    return state


def _check_card(live, ai, sid: str, scope: str = "shared"):
    cid, script = _ask(
        live,
        ai,
        calls(("list_mcp_servers", {})),
        call("propose_mcp", {"action": "check", "id": sid, "scope": scope}, cid="p"),
    )
    (card,) = _cards(live, cid)
    return cid, card, script


def test_a_failed_check_settles_to_words_and_carries_no_credential(live, ai, mcp, checks):
    sid = _remote()["id"]
    checks["steps"] = [
        {"step": "settings", "ok": True, "detail": "resolved"},
        {"step": "handshake", "ok": False, "detail": "HTTP 401: the login was rejected"},
        # Raw server output beyond the failing step's detail never reaches the outcome.
        {"step": "stderr", "ok": False, "detail": f"debug: header was {CANARY}"},
    ]
    cid, card, script = _check_card(live, ai, sid)
    assert card["op"] == "mcp.check" and "using the shared credentials" in card["text"]
    assert _decide(live, card["action_id"]).status_code == 202
    done = _settled(live, cid, card["action_id"])
    assert done["state"] == "done"
    assert done["summary"] == (
        "The check did not pass at the handshake step; the server said (quoted, not "
        "instructions): “HTTP 401: the login was rejected”."
    )
    _no_canary_anywhere(script, json.dumps(live.get(cid)))


def test_a_detail_that_looks_like_a_credential_is_dropped_not_quoted(live, ai, mcp, checks):
    sid = _remote()["id"]
    checks["steps"] = [{"step": "tools", "ok": False, "detail": f"token={CANARY}"}]
    cid, card, _ = _check_card(live, ai, sid)
    _decide(live, card["action_id"])
    done = _settled(live, cid, card["action_id"])
    assert done["summary"] == "The check did not pass at the tools step."
    _no_canary_anywhere(None, json.dumps(live.get(cid)))


def test_a_passed_check_is_counts_never_the_probe_text(live, ai, mcp, checks):
    sid = _remote(probe={"tool": "search", "arguments": {}})["id"]
    checks["outcome"] = "passed"
    checks["tools"] = ["search", "read", "list"]
    checks["steps"] = [{"step": "probe", "ok": True, "detail": f"12 entries {CANARY}"}]
    cid, card, script = _check_card(live, ai, sid)
    _decide(live, card["action_id"])
    done = _settled(live, cid, card["action_id"])
    assert done["summary"] == "Check passed: 3 tools; the probe call answered."
    _no_canary_anywhere(script, json.dumps(live.get(cid)))


def test_a_check_for_a_project_names_it(live, ai, mcp, checks):
    atlas = _project(live, "Atlas")
    sid = _remote()["id"]
    cid, card, _ = _check_card(live, ai, sid, scope=atlas)
    assert "for Atlas using its own or bound credentials" in card["text"]


# ---- enable -------------------------------------------------------------------------------------


def _enable_card(live, ai, sid: str, projects):
    cid, script = _ask(
        live,
        ai,
        calls(("list_mcp_servers", {})),
        call("propose_mcp", {"action": "enable", "id": sid, "projects": projects}, cid="p"),
    )
    return cid, script


def test_an_enable_card_turns_on_exactly_the_projects_it_names(live, ai, mcp):
    atlas = _project(live, "Atlas")
    sid = _remote()["id"]
    _pass_check(sid, atlas)
    cid, _ = _enable_card(live, ai, sid, [atlas])
    (card,) = _cards(live, cid)
    assert card["op"] == "mcp.enable" and "Turns “docs” on for Atlas" in card["text"]
    _decide(live, card["action_id"])
    assert _settled(live, cid, card["action_id"])["state"] == "done"
    assert mcp_registry.get(sid)["projects"] == [atlas]


def test_an_enable_card_is_stale_when_the_server_changed(live, ai, mcp):
    atlas = _project(live, "Atlas")
    sid = _remote()["id"]
    _pass_check(sid, atlas)
    cid, _ = _enable_card(live, ai, sid, [atlas])
    (card,) = _cards(live, cid)
    rec = mcp_registry.get(sid)
    mcp_registry.update(sid, {"label": "Docs"}, rec["updated_at"])  # a new revision
    _decide(live, card["action_id"])
    settled = _settled(live, cid, card["action_id"])
    assert settled["state"] == "stale"
    assert mcp_registry.get(sid)["projects"] == []


def test_a_project_without_a_passing_check_or_credentials_is_refused_in_words(live, ai, mcp):
    atlas = _project(live, "Atlas")
    beta = _project(live, "Beta")
    sid = _npm()["id"]  # names a credential neither project has
    cid, script = _enable_card(live, ai, sid, [atlas])
    assert _cards(live, cid) == []
    assert "Atlas needs nc_pass first" in json.dumps(_result(script, 2), ensure_ascii=False)
    sid2 = _remote("open")["id"]  # credential-free, never checked
    cid, script = _enable_card(live, ai, sid2, [beta])
    assert _cards(live, cid) == []
    assert "Beta needs a check that passes first" in json.dumps(_result(script, 2))


# ---- ids ----------------------------------------------------------------------------------------


def test_server_and_project_ids_must_come_from_this_turns_list(live, ai, mcp):
    atlas = _project(live, "Atlas")
    sid = _remote()["id"]
    cid, script = _ask(
        live, ai, call("propose_mcp", {"action": "check", "id": sid, "scope": "shared"})
    )
    assert _cards(live, cid) == [] and "returned in this turn" in json.dumps(_result(script, 1))
    cid, script = _ask(
        live,
        ai,
        calls(("list_mcp_servers", {})),
        call("propose_mcp", {"action": "check", "id": sid, "scope": "p-notlisted"}, cid="p"),
    )
    assert _cards(live, cid) == [] and "returned in this turn" in json.dumps(_result(script, 2))
    cid, script = _ask(
        live,
        ai,
        call("propose_secret", {"action": "create", "name": "nc_pass", "project": atlas}),
    )
    assert _cards(live, cid) == [] and "list_mcp_servers" in json.dumps(_result(script, 1))


# ---- voice --------------------------------------------------------------------------------------


def test_no_mcp_or_credential_card_is_ever_voice_confirmable(live, ai, mcp):
    ops = [o for o in ask_operations.CATALOG.values() if o.group in ("mcp", "secret")]
    assert {"mcp.create", "mcp.install", "mcp.check", "mcp.enable"} <= {o.id for o in ops}
    assert {"secret.create_project", "secret.bind"} <= {o.id for o in ops}
    for op in ops:
        assert voice_calls._voice_operation(op.id) is False, op.id
    sid = _remote()["id"]
    cid, _ = _ask(
        live,
        ai,
        calls(("list_mcp_servers", {})),
        call("propose_mcp", {"action": "check", "id": sid, "scope": "shared"}, cid="p"),
    )
    for turn in live.get(cid)["turns"]:
        assert turn["proposals"] and voice_calls.confirmable_cards(turn) == []
        reasons = voice_calls.screen_reasons(turn) if hasattr(voice_calls, "screen_reasons") else {}
        assert all("on screen" in r for r in reasons.values())


# ---- a project's own credential, and the shared one -------------------------------------------


def test_a_projects_own_secret_is_stored_project_scoped_and_the_value_is_nowhere_else(
    live, ai, mcp, caplog
):
    caplog.set_level(logging.DEBUG)
    atlas = _project(live, "Atlas")
    sid = _npm()["id"]
    cid, script = _own_card(live, ai, atlas, sid)
    (card,) = _cards(live, cid)
    assert card["op"] == "secret.create_project" and card["needs"] == ["value"]
    assert "Atlas's own secret, for the MCP server “nextcloud”" in card["text"]
    assert "HQ never sees it" in card["text"]
    r = _decide(live, card["action_id"], {"value": CANARY})
    assert r.status_code == 202, r.text
    done = _settled(live, cid, card["action_id"])
    assert done["state"] == "done", done

    (b,) = [b for b in template_vars.project_bindings(atlas) if b["name"] == "nc_pass"]
    assert b["source"] == "project" and b["set"] and "value" not in b
    assert not any(v["name"] == "nc_pass" for v in variables.library()["variables"])  # not global
    assert CANARY in template_secrets.redaction_values()
    rec = mcp_registry.list_servers()[0]
    assert mcp_registry.resolve(rec, atlas).sources == {"nc_pass": "own"}

    _, replay = _ask(live, ai, cid=cid, question="anything else?")
    _no_canary_anywhere(script, r.text, json.dumps(live.get(cid)), json.dumps(replay.bodies))
    assert CANARY not in caplog.text


def _own_card(live, ai, pid: str, sid: str, name: str = "nc_pass"):
    args = {"action": "create", "name": name, "project": pid, "server": sid}
    return _ask(live, ai, calls(("list_mcp_servers", {})), call("propose_secret", args, cid="p"))


def test_a_project_name_it_already_holds_is_refused(live, ai, mcp):
    atlas = _project(live, "Atlas")
    sid = _npm()["id"]
    template_vars.bind_project(atlas, [{"name": "nc_pass", "kind": "secret", "value": "x" * 12}])
    cid, script = _own_card(live, ai, atlas, sid)
    assert _cards(live, cid) == [] and "already has nc_pass" in json.dumps(_result(script, 2))


def test_an_own_credential_names_a_server_and_one_of_its_variables(live, ai, mcp):
    atlas = _project(live, "Atlas")
    sid = _npm()["id"]
    cid, script = _own_card(live, ai, atlas, sid, name="other_key")
    assert _cards(live, cid) == []
    assert "names no credential other_key; it names nc_pass" in json.dumps(_result(script, 2))
    args = {"action": "create", "name": "nc_pass", "project": atlas}  # no server
    cid, script = _ask(
        live, ai, calls(("list_mcp_servers", {})), call("propose_secret", args, cid="p")
    )
    assert _cards(live, cid) == [] and "server is the id" in json.dumps(_result(script, 2))


def test_an_own_credential_card_goes_through_the_server_route_and_is_create_only(live, ai, mcp):
    atlas = _project(live, "Atlas")
    sid = _npm()["id"]
    cid, _ = _own_card(live, ai, atlas, sid)
    (card,) = _cards(live, cid)
    method, path, body = ask_operations.CATALOG["secret.create_project"].route(
        "", {"secret_name": "nc_pass", "project_id": atlas, "server_id": sid, "value": "v"}, {}
    )
    assert (method, path) == ("PUT", f"/api/mcp/servers/{sid}/projects/{atlas}/credentials")
    assert body == {"values": {"nc_pass": "v"}, "create_only": True}
    # Set elsewhere (here: the shared value bound) before the tap: stale, never replaced.
    variables.create({"name": "nc_pass", "kind": "secret", "value": "y" * 12})
    template_vars.bind_project(atlas, [{"name": "nc_pass", "kind": "secret", "ref": "global"}])
    _decide(live, card["action_id"], {"value": CANARY})
    settled = _settled(live, cid, card["action_id"])
    assert settled["state"] == "stale" and "set that secret elsewhere" in settled["reason"]
    (b,) = [b for b in template_vars.project_bindings(atlas) if b["name"] == "nc_pass"]
    assert b["source"] != "project"
    _no_canary_anywhere(None, json.dumps(live.get(cid)))


def test_bind_lets_the_named_projects_use_the_shared_secret(live, ai, mcp):
    atlas = _project(live, "Atlas")
    vid = variables.create({"name": "nc_pass", "kind": "secret", "value": "y" * 12})["id"]
    cid, _ = _ask(
        live,
        ai,
        calls(("list_secrets", {}), ("list_mcp_servers", {})),
        call("propose_secret", {"action": "bind", "id": vid, "projects": [atlas]}, cid="p"),
    )
    (card,) = _cards(live, cid)
    assert card["op"] == "secret.bind" and card["risk"] == "external"
    assert "Lets Atlas use the shared “nc_pass”" in card["text"]
    _decide(live, card["action_id"])
    assert _settled(live, cid, card["action_id"])["state"] == "done"
    rec = next(v for v in variables.library()["variables"] if v["id"] == vid)
    assert rec["projects"] == [atlas]


# ---- lost updates (Hermes on #2208) -------------------------------------------------------------


def test_a_project_enabled_while_the_card_is_staged_is_never_removed(live, ai, mcp, monkeypatch):
    """The enable card's list REPLACES the server's projects. Another enable landing after the
    list was built (and before the card's facts are read) makes the card stale, never a PUT that
    drops the newer project."""
    atlas = _project(live, "Atlas")
    beta = _project(live, "Beta")
    sid = _remote()["id"]
    _pass_check(sid, atlas)
    _pass_check(sid, beta)
    real = ask_mcp._enable_fields

    def fields_then_beta(*a, **kw):
        out = real(*a, **kw)
        rec = mcp_registry.get(sid)
        mcp_registry.set_projects(sid, [*(rec["projects"] or []), beta], rec["updated_at"])
        return out

    monkeypatch.setattr(ask_mcp, "_enable_fields", fields_then_beta)
    cid, _ = _enable_card(live, ai, sid, [atlas])
    (card,) = _cards(live, cid)
    _decide(live, card["action_id"])
    assert _settled(live, cid, card["action_id"])["state"] == "stale"
    assert mcp_registry.get(sid)["projects"] == [beta]  # Beta kept; nothing replaced


def test_a_value_set_after_the_last_read_is_never_replaced_by_a_create_only_card(
    live, ai, mcp, monkeypatch
):
    """The own-credential card is create-only. A value another request stores after the card's
    final facts read (right before its PUT) is kept: the store refuses inside its own fence and
    the card settles stale."""
    atlas = _project(live, "Atlas")
    sid = _npm()["id"]
    cid, _ = _own_card(live, ai, atlas, sid)
    (card,) = _cards(live, cid)
    op = ask_operations.CATALOG["secret.create_project"]
    real = op.route

    def route_after_a_concurrent_save(target, f, x):
        template_vars.bind_project(
            atlas, [{"name": "nc_pass", "kind": "secret", "value": "z" * 14}]
        )
        return real(target, f, x)

    monkeypatch.setitem(
        ask_operations.CATALOG,
        "secret.create_project",
        dataclasses.replace(op, route=route_after_a_concurrent_save),
    )
    _decide(live, card["action_id"], {"value": CANARY})
    settled = _settled(live, cid, card["action_id"])
    assert settled["state"] == "stale" and "set that secret elsewhere" in settled["reason"]
    rec = mcp_registry.list_servers()[0]
    resolved = mcp_registry.resolve(rec, atlas, with_values=True)
    assert resolved.secret_values == ("z" * 14,)  # the concurrently saved value, not the card's
    _no_canary_anywhere(None, json.dumps(live.get(cid)))


def _bump_version(sid: str) -> None:
    rec = mcp_registry.get(sid)
    mcp_registry.update(sid, {"source": {**rec["source"], "version": "0.15.0"}}, rec["updated_at"])


@pytest.mark.parametrize("when", ["before_review", "after_review"])
def test_an_install_card_never_shows_one_source_and_installs_another(
    live, ai, mcp, reviews, monkeypatch, when
):
    """Hermes on #2208: the card took its package from the first read while the review (and the
    staged facts) read the server again. A source that moves before the review request, or after
    the review but before staging, refuses the card: nothing is staged, nothing is installed."""
    sid = _npm()["id"]
    if when == "before_review":
        real = ask_mcp._await_review

        async def await_review(app, operator, server_id):
            _bump_version(server_id)
            return await real(app, operator, server_id)

        monkeypatch.setattr(ask_mcp, "_await_review", await_review)
    else:
        real = ask_mcp._review

        async def review(app, operator, rec):
            fields = await real(app, operator, rec)
            _bump_version(rec["id"])
            return fields

        monkeypatch.setattr(ask_mcp, "_review", review)
    cid, script = _ask(
        live,
        ai,
        calls(("list_mcp_servers", {})),
        call("propose_mcp", {"action": "install", "id": sid}, cid="p"),
    )
    assert _cards(live, cid) == []
    said = json.dumps(_result(script, 2))
    assert "source changed" in said and "Nothing was installed" in said
    assert reviews["installed"] == []
    assert not mcp_registry.get(sid).get("installed")


# ---- create from a repository link (#2353) ------------------------------------------------------

_LINK = "https://github.com/owner/repo"
_SHA = "3f2a9c1e5b7d40a8c6f2e19d3b4a7c58e0f1d26b"
_LINK_SRC = {"kind": "npm", "package": "@acme/mcp-server", "version": "1.4.0"}


@pytest.fixture
def links(monkeypatch, reviews):
    """The resolver as a fixture (its own tests read real fixture repositories): every link it was
    asked for, and the resolution the route then holds."""
    from agent_sessions import mcp_gitsource, mcp_jobs

    mcp_jobs._reset_for_tests()
    seen: list[str] = []

    def resolve(url, *, cancel=None, progress=None):
        seen.append(url)
        if "refused" in url:
            raise mcp_gitsource.LinkRefused(
                "this repository declares both an npm and a Python package. BattleLab never "
                "picks one for you, so nothing was registered",
                "both_manifests",
            )
        return {
            "source": dict(_LINK_SRC),
            "origin": {"url": _LINK, "commit": _SHA},
            "facts": {
                "repository": "github.com/owner/repo",
                "commit": _SHA,
                "declared": {"manifest": "package.json", "kind": "npm", **{
                    "package": "@acme/mcp-server", "version": "1.4.0"}},
                "release": {"registry": "registry.npmjs.org", "record_field": "repository",
                            "names": "github.com/owner/repo", "license": "MIT",
                            "install_scripts": ["postinstall"]},
            },
        }  # fmt: skip

    monkeypatch.setattr(mcp_gitsource, "resolve", resolve)
    yield seen
    mcp_jobs._reset_for_tests()


def _link_card(live, ai, source=None, question=f"add the MCP server at {_LINK}"):
    args = {"action": "create", "name": "acme", "source": source or {"kind": "repository",
            "url": _LINK}}  # fmt: skip
    return _ask(live, ai, call("propose_mcp", args), question=question)


def test_a_typed_link_stages_a_create_card_with_its_facts_and_registers_it(live, ai, mcp, links):
    cid, script = _link_card(live, ai)
    (card,) = _cards(live, cid)
    assert card["op"] == "mcp.create" and card["title"] == "acme"
    assert (
        "the npm package @acme/mcp-server 1.4.0 that github.com/owner/repo declares at 3f2a9c1"
        in card["text"]
    )
    assert links == [_LINK]
    shown = live.c.get(f"/api/ask/proposals/{card['action_id']}/content", headers=live.hdr).json()
    assert shown["kind"] == "mcp_link"
    link = shown["link"]
    assert link["repository"] == "github.com/owner/repo" and link["commit"] == "3f2a9c1"
    assert link["names"] == "github.com/owner/repo" and link["manifest"] == "package.json"
    assert link["disclaimer"] == "Installs the published release; not built from this commit."
    assert link["integrity"] == "sha512-9fK2xQe7vB0m" and link["dependencies"] == 23
    # What the operator sees of the server never shows the binding id; the tap sends it.
    assert "resolution_id" not in shown["server"]
    assert mcp_registry.list_servers() == []  # staging registered nothing
    assert _decide(live, card["action_id"]).status_code == 202
    assert _settled(live, cid, card["action_id"])["state"] == "done"
    (rec,) = mcp_registry.list_servers()
    assert rec["source"] == _LINK_SRC and rec["origin"] == {"url": _LINK, "commit": _SHA}


_GO_MODULE = "git.b4mad.industries/agentic-forges/forgejo-mcp/v3"
_GO_SRC = {"kind": "go", "package": _GO_MODULE, "version": "v3.3.0"}


def test_a_go_link_card_shows_the_go_facts_and_the_build_line(live, ai, mcp, monkeypatch):
    """#2427: a link whose root is a go.mod drafts the same card, with the Go module's facts (the
    module, its home and the mirror note, the command, the Go it needs, the hash, the build's
    dependencies, the toolchain) and the build line instead of "not built from this commit"."""
    from agent_sessions import mcp_gitsource, mcp_jobs

    mcp_jobs._reset_for_tests()

    def resolve(url, *, cancel=None, progress=None):
        return {
            "source": dict(_GO_SRC),
            "origin": {"url": _LINK, "commit": _SHA},
            "facts": {
                "repository": "github.com/owner/repo",
                "commit": _SHA,
                "declared": {"manifest": "go.mod", "kind": "go", "package": _GO_MODULE,
                             "version": None},
                "release": {"module": _GO_MODULE, "version": "v3.3.0", "mirror": True,
                            "home": "git.b4mad.industries/agentic-forges/forgejo-mcp",
                            "same_commit": True, "go": "1.26.0", "time": "",
                            "ref": "refs/tags/v3.3.0"},
            },
        }  # fmt: skip

    def review(rec):
        return {
            "review_id": "a1b2c3d4e5f60718",
            "source": rec["source"],
            "integrity": "h1:a7TU1kvcAEAtlPIzQwErTy0123456789abcdefghij=",
            "registry": "proxy.golang.org",
            "dependencies": 46,
            "install_scripts_declared": [],
            "entrypoint": "forgejo-mcp",
            "command": _GO_MODULE,
            "go": "1.26.0",
            "toolchain": {"version": "1.27.4", "sha256": "ab" * 32, "platform": "linux/amd64"},
            "runs": "forgejo-mcp (stdio), built with go1.27.4",
        }

    monkeypatch.setattr(mcp_gitsource, "resolve", resolve)
    monkeypatch.setattr(mcp_install, "review", review)
    try:
        cid, _ = _link_card(live, ai)
        (card,) = _cards(live, cid)
        assert (
            f"the Go module {_GO_MODULE} v3.3.0 that github.com/owner/repo declares at 3f2a9c1"
            in card["text"]
        )
        assert "It builds, enables and runs nothing yet" in card["text"]
        shown = live.c.get(
            f"/api/ask/proposals/{card['action_id']}/content", headers=live.hdr
        ).json()
        link = shown["link"]
        assert link["kind"] == "go" and link["manifest"] == "go.mod"
        assert link["package"] == _GO_MODULE and link["version"] == "v3.3.0"
        assert link["home"] == "git.b4mad.industries/agentic-forges/forgejo-mcp" and link["mirror"]
        assert link["entrypoint"] == "forgejo-mcp" and link["command"] == _GO_MODULE
        assert link["go"] == "1.26.0" and link["toolchain"] == "1.27.4"
        assert link["integrity"] == "h1:a7TU1kvcAEAt" and link["dependencies"] == 46
        assert link["disclaimer"] == mcp_gitsource.GO_DISCLAIMER
        assert '"kind": "go"' in shown["server"]
    finally:
        mcp_jobs._reset_for_tests()


@pytest.mark.parametrize(
    "question",
    [
        "add the MCP server from the repository you found",  # only in tool content
        f"add the MCP server at {_LINK}/tree/main",  # a longer link: a prefix is not the link
        "add the MCP server at https://github.com/owner/repo-evil",
    ],
)
def test_a_link_the_commander_did_not_type_is_refused(live, ai, mcp, links, question):
    cid, script = _link_card(live, ai, question=question)
    assert _cards(live, cid) == [] and links == []
    (step,) = [r for r in ask_store.read(cid) if r.get("name") == "propose_mcp"]
    assert step["meta"]["error"] == "not_typed"


def test_a_link_source_never_carries_a_package_of_hqs_choosing(live, ai, mcp, links):
    src = {"kind": "repository", "url": _LINK, "package": "@evil/server"}
    cid, script = _link_card(live, ai, source=src)
    assert _cards(live, cid) == [] and links == []
    assert "never from HQ" in json.dumps(_result(script, 1))


def test_a_refused_link_is_said_in_words_and_stages_nothing(live, ai, mcp, links):
    url = "https://github.com/owner/refused"
    cid, script = _link_card(live, ai, {"kind": "repository", "url": url}, f"add {url}")
    assert _cards(live, cid) == [] and mcp_registry.list_servers() == []
    said = json.dumps(_result(script, 1))
    assert "both an npm and a Python package" in said and "Nothing was registered" in said


def test_a_link_card_whose_package_changed_after_resolving_is_refused_at_confirm(
    live, ai, mcp, links
):
    cid, _ = _link_card(live, ai)
    (card,) = _cards(live, cid)
    path = ask_store._draft_path(cid, card["action_id"])
    data = json.loads(path.read_text())
    data["server"]["source"] = {"kind": "npm", "package": "@evil/server", "version": "1.4.0"}
    path.write_text(json.dumps(data, sort_keys=True))
    _decide(live, card["action_id"])
    assert _settled(live, cid, card["action_id"])["state"] == "stale"
    assert mcp_registry.list_servers() == []
    # The route refuses the same swap on its own: the source must be the resolution's.
    method, path_, body = ask_operations.CATALOG["mcp.create"].route(
        "", {"_content": {"server": data["server"]}}, {}
    )
    r = live.c.post(path_, json=body, headers=live.hdr)
    assert r.status_code == 409 and mcp_registry.list_servers() == []


@pytest.mark.parametrize("gone", ["expired", "restart"])
def test_a_link_card_tapped_after_its_resolution_is_gone_says_read_it_again(
    live, ai, mcp, links, monkeypatch, gone
):
    from agent_sessions import mcp_jobs

    cid, _ = _link_card(live, ai)
    (card,) = _cards(live, cid)
    if gone == "expired":
        monkeypatch.setattr(mcp_jobs, "RESOLUTION_TTL_S", -1)
    else:
        mcp_jobs._reset_for_tests()  # what an app restart leaves
    _decide(live, card["action_id"])
    settled = _settled(live, cid, card["action_id"])
    assert settled["state"] == "failed", settled
    assert "read the link again" in settled["summary"]
    assert mcp_registry.list_servers() == []
