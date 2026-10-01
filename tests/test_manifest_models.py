"""#1189 — manifests say how to pick a model at launch and which instruction files an engine reads.

Pinned here, each with the regression the issue names:

* the manifest blocks (`launch.model`, `instructions`) and their closed vocabularies, with negative
  tests (unknown flag, path in a file name, bool-as-string, ambiguous alias, `default` reserved,
  chat runtime);
* the contract decision: additive fields stay on contract 1 — the new reader reads every old
  manifest, and a reader WITHOUT the new fields refuses a manifest that uses them (unknown field);
* THE resolver (`model_choice.select`) — argv injection refused before membership, aliases
  canonicalised, stale / unsupported / configured-elsewhere choices refused (never `default`),
  resume rules incl. a legacy session with nothing recorded;
* the provider accepting only a resolved `Selection`, and the pair landing as two argv elements;
* operator-added ids under `agent_defaults.models` (strict write, lenient read, capped);
* `/api/engines`, the sidecar `model_requested` (through placeholder adoption), the transcript-only
  `model_effective`, and the ws launch path (4422 before spawn; ATTACH never reads the model).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import json
import tomllib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_sessions import engines, metadata, model_choice, prefs, transcript
from agent_sessions.engine_errors import EngineError
from agent_sessions.main import create_app
from agent_sessions.plugins import FIRST_PARTY_DIR, kinds, manifest

# --- helpers -------------------------------------------------------------------------------------


def _doc(engine: str) -> dict:
    return tomllib.loads((FIRST_PARTY_DIR / engine / "plugin.toml").read_text())


def _parse(doc: dict) -> manifest.Manifest:
    return manifest.parse(copy.deepcopy(doc))


def _prov(engine: str):
    return engines.get(engine)


def _entry(monkeypatch, engine: str, path: str = "/opt/bin/agent"):
    prov = _prov(engine)
    monkeypatch.setattr(prov, "_entry_path", lambda: path)
    return prov


INJECTIONS = [
    "--model=x --dangerous",
    "--dangerously-skip-permissions",
    "-x",
    "claude-opus-5 --yolo",
    "claude opus",
    "claude-opus-5\x00--yolo",
    "claude-opus-5\n",
    "\N{FULLWIDTH LATIN SMALL LETTER C}laude-opus-5",  # a unicode lookalike
    "claude\N{HYPHEN}opus-5",  # a unicode hyphen
    "claude-opus-5=",
    "",
    "a" * 97,
]
NON_STRINGS = [123, 1.5, True, [], ["claude-opus-5"], {"id": "claude-opus-5"}]


# --- the manifest blocks -------------------------------------------------------------------------


def test_first_party_manifests_declare_models_and_instructions():
    by = {m.id: m for m in (p.manifest for p in engines.all_providers())}
    claude = by["claude"]
    assert claude.launch.model == manifest.ModelLaunch("flag", "--model", on_resume=True)
    assert claude.instructions == ("CLAUDE.md",)
    assert {m.id for m in claude.models} >= {"claude-opus-5", "claude-sonnet-5"}
    for eid in ("codex", "gemini"):
        assert by[eid].launch.model is not None and by[eid].launch.model.on_resume is False
        assert by[eid].models
    for eid in ("opencode", "kimi", "antigravity"):
        assert by[eid].models_configured_elsewhere and by[eid].launch.model is None
        assert by[eid].instructions
    assert by["apichat"].models_configured_elsewhere and by["apichat"].instructions == ()
    assert by["shell"].instructions == () and by["shell"].launch.model is None
    assert set(kinds.INSTRUCTION_FILES) == {"CLAUDE.md", "AGENTS.md", "GEMINI.md"}
    for m in by.values():
        for f in m.instructions:
            assert "/" not in f and f in kinds.INSTRUCTION_FILES


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda d: d["launch"].__setitem__("model", {"kind": "flag", "flag": "--mdl"}), "flag"),
        (
            lambda d: d["launch"].__setitem__("model", {"kind": "flag", "flag": "--model=x"}),
            "flag",
        ),
        (lambda d: d["launch"].__setitem__("model", {"kind": "arg", "flag": "--model"}), "kind"),
        (
            lambda d: d["launch"]["model"].__setitem__("on_resume", "true"),  # bool-as-string
            "on_resume",
        ),
        (lambda d: d["launch"]["model"].__setitem__("value", "claude-opus-5"), "value"),
        (lambda d: d.__setitem__("instructions", {"files": ["../CLAUDE.md"]}), "files"),
        (lambda d: d.__setitem__("instructions", {"files": ["docs/AGENTS.md"]}), "files"),
        (lambda d: d.__setitem__("instructions", {"files": ["README.md"]}), "files"),
        (lambda d: d.__setitem__("instructions", {"files": "CLAUDE.md"}), "files"),
        (lambda d: d["instructions"].__setitem__("paths", ["x"]), "paths"),
        (lambda d: d["models"]["list"][0].__setitem__("id", "-opus"), "id"),
        (lambda d: d["models"]["list"][0].__setitem__("id", "claude opus"), "id"),
        (lambda d: d["models"]["list"][0].__setitem__("id", "default"), "reserved"),
        (lambda d: d["models"]["list"][1].__setitem__("aliases", ["default"]), "reserved"),
        # an alias colliding with another model's id, and with another model's alias
        (lambda d: d["models"]["list"][1].__setitem__("aliases", ["claude-opus-5"]), "ambiguous"),
        (lambda d: d["models"]["list"][1].__setitem__("aliases", ["opus"]), "ambiguous"),
        (lambda d: d["models"].__setitem__("configured_elsewhere", True), "configured elsewhere"),
        (lambda d: d["models"].__setitem__("list", []), "non-empty"),
    ],
)
def test_the_model_and_instruction_blocks_are_closed(mutate, field):
    doc = _doc("claude")
    mutate(doc)
    with pytest.raises(manifest.ManifestError) as e:
        _parse(doc)
    assert field in str(e.value)


def test_a_chat_manifest_takes_no_models_and_no_instructions():
    ok = _parse(_doc("apichat"))
    assert ok.runtime == "chat" and ok.launch is None
    doc = _doc("apichat")
    doc["models"] = {"list": [{"id": "gpt-5"}]}
    with pytest.raises(manifest.ManifestError, match="endpoint config"):
        _parse(doc)
    doc = _doc("apichat")
    doc["instructions"] = {"files": ["AGENTS.md"]}
    with pytest.raises(manifest.ManifestError, match="forbidden for runtime 'chat'"):
        _parse(doc)
    doc = _doc("apichat")
    doc["launch"] = {"model": {"kind": "flag", "flag": "--model"}}
    with pytest.raises(manifest.ManifestError, match="forbidden for runtime 'chat'"):
        _parse(doc)


# --- the contract decision: additive fields stay on contract 1 -----------------------------------


def _strip_new(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc.pop("instructions", None)
    if "launch" in doc:
        doc["launch"].pop("model", None)
    return doc


@pytest.mark.parametrize(
    "engine", sorted(p.name for p in FIRST_PARTY_DIR.iterdir() if (p / "plugin.toml").exists())
)
def test_new_reader_reads_an_old_manifest(engine):
    # Every first-party manifest with the #1189 fields REMOVED is exactly a pre-#1189 contract-1
    # manifest. It must still load, on contract 1, as a default-only engine with no instructions.
    old = _strip_new(_doc(engine))
    assert old["contract"] == 1
    m = _parse(old)
    assert m.contract == kinds.CONTRACT_CURRENT == 1
    assert m.instructions == ()
    assert m.launch is None or m.launch.model is None
    assert not model_choice.takes_model(m)


def test_old_reader_refuses_a_new_manifest(monkeypatch):
    # A build that predates #1189 has no reader for `launch.model` / `instructions`. Simulate it by
    # making those readers consume nothing (exactly the old code's behaviour): the unknown-field
    # rule refuses the whole manifest — fail-closed, never half-read, and no contract bump needed.
    doc = _doc("claude")
    monkeypatch.setattr(manifest, "_launch_model", lambda r: None)
    with pytest.raises(manifest.ManifestError, match=r"launch\.model: unknown field"):
        _parse(doc)
    monkeypatch.undo()
    monkeypatch.setattr(manifest, "_instructions", lambda top: ())
    with pytest.raises(manifest.ManifestError, match=r"instructions: unknown field"):
        _parse(doc)


def test_a_future_key_inside_the_new_blocks_is_refused_too():
    # The new blocks are closed at every level, so the NEXT additive field is refused by this build.
    for mutate in (
        lambda d: d["launch"]["model"].__setitem__("allow_any", True),
        lambda d: d["instructions"].__setitem__("mode", "symlink"),
    ):
        doc = _doc("claude")
        mutate(doc)
        with pytest.raises(manifest.ManifestError, match="unknown field"):
            _parse(doc)


def test_a_higher_contract_still_needs_a_newer_build():
    doc = _doc("claude")
    doc["contract"] = 2
    with pytest.raises(manifest.ManifestError, match="needs a newer BattleLab"):
        _parse(doc)


# --- the resolver --------------------------------------------------------------------------------


def test_default_means_no_flag():
    p = _prov("claude")
    for req in (None, "default"):
        sel = model_choice.select(p, req)
        assert sel.model is None and sel.flag is None and sel.argv() == []


def test_an_alias_is_canonicalised_before_anything_is_launched():
    sel = model_choice.select(_prov("claude"), "opus", added=[])
    assert (sel.model, sel.flag) == ("claude-opus-5", "--model")
    assert sel.argv() == ["--model", "claude-opus-5"]


@pytest.mark.parametrize("bad", INJECTIONS + NON_STRINGS)
def test_argv_injection_is_refused_before_membership(bad, monkeypatch):
    # Even an operator list that "contains" the hostile value cannot admit it: shape is checked
    # first, so nothing hostile ever reaches a membership test, let alone argv.
    monkeypatch.setattr(
        model_choice, "operator_ids", lambda e: [bad] if isinstance(bad, str) else []
    )
    with pytest.raises(model_choice.ModelRefused) as e:
        model_choice.select(_prov("claude"), bad)
    assert e.value.code == "invalid"


def test_an_id_in_neither_list_is_refused_never_defaulted():
    with pytest.raises(model_choice.ModelRefused) as e:
        model_choice.select(_prov("claude"), "gpt-5", added=[])  # another engine's model
    assert e.value.code == "not_offered"


def test_an_operator_added_id_is_accepted_and_a_removed_one_is_refused():
    p = _prov("claude")
    assert model_choice.select(p, "claude-next-7", added=["claude-next-7"]).model == "claude-next-7"
    # The operator removed it after the form was filled in: refused, not `default`.
    with pytest.raises(model_choice.ModelRefused) as e:
        model_choice.select(p, "claude-next-7", added=[])
    assert e.value.code == "not_offered"
    # "not offered": the resolver cannot tell a removed id from one never listed.
    assert "is not offered" in e.value.detail


def test_a_non_default_request_to_a_configured_elsewhere_engine_is_refused():
    for eid in ("opencode", "kimi", "antigravity", "apichat"):
        with pytest.raises(model_choice.ModelRefused) as e:
            model_choice.select(_prov(eid), "anything")
        assert e.value.code == "configured_elsewhere", eid
        assert model_choice.select(_prov(eid), "default").flag is None
    # The shell has no agent, so it has no "configuration" to point at: it takes no model.
    with pytest.raises(model_choice.ModelRefused) as e:
        model_choice.select(_prov("shell"), "anything")
    assert e.value.code == "unsupported" and "configuration" not in e.value.detail


def test_resume_applies_the_flag_only_where_the_engine_honours_it():
    claude, codex = _prov("claude"), _prov("codex")
    assert model_choice.select(claude, "haiku", resume=True, added=[]).argv() == [
        "--model",
        "claude-haiku-4-5",
    ]
    # codex cannot override on resume: a DIFFERENT model is refused…
    with pytest.raises(model_choice.ModelRefused) as e:
        model_choice.select(codex, "gpt-5", resume=True, recorded="gpt-5-codex", added=[])
    assert e.value.code == "resume_override"
    # …the SAME model is accepted with no flag…
    same = model_choice.select(codex, "gpt-5", resume=True, recorded="gpt-5", added=[])
    assert same.model == "gpt-5" and same.flag is None
    # …and `default` is today's resume.
    assert model_choice.select(codex, "default", resume=True, recorded="").flag is None


@pytest.mark.parametrize("recorded", ["", None])
def test_a_legacy_session_with_no_recorded_model_matches_nothing(recorded):
    # Round-2 contract: a missing `model_requested` is never taken to match an explicit request.
    with pytest.raises(model_choice.ModelRefused) as e:
        model_choice.select(_prov("codex"), "gpt-5", resume=True, recorded=recorded, added=[])
    assert e.value.code == "resume_override"
    # …while an engine that honours the flag on resume applies it.
    sel = model_choice.select(_prov("claude"), "opus", resume=True, recorded=recorded, added=[])
    assert sel.flag == "--model"


# --- the provider: only a resolved Selection reaches argv ----------------------------------------


def test_the_flag_pair_is_two_argv_elements_before_bypass(monkeypatch):
    p = _entry(monkeypatch, "claude")
    sel = model_choice.select(p, "sonnet", added=[])
    argv = p.new_launch_argv(
        "33333333-3333-3333-3333-333333333333", cwd="/w", bypass=True, model=sel
    )
    i = argv.index("--model")
    assert argv[i + 1] == "claude-sonnet-5"
    assert argv.index("--dangerously-skip-permissions") > i + 1
    assert not any(a.startswith("--model=") for a in argv)
    # Resume assembles it the same way.
    argv = p.launch_argv("33333333-3333-3333-3333-333333333333", cwd="/w", bypass=False, model=sel)
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    # And default is today's argv, byte for byte.
    assert p.new_launch_argv(
        "33333333-3333-3333-3333-333333333333", cwd="/w", bypass=True
    ) == p.new_launch_argv(
        "33333333-3333-3333-3333-333333333333",
        cwd="/w",
        bypass=True,
        model=model_choice.select(p, "default"),
    )


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5",  # a raw string from a route / playbook / model reply
        model_choice.Selection("codex", "gpt-5", "--model"),  # another engine's selection
        model_choice.Selection("claude", "-x", "--model"),  # a forged selection
        model_choice.Selection("claude", "a b", "--model"),
        model_choice.Selection("claude", "claude-opus-5", "--yolo"),  # a flag not the manifest's
        model_choice.Selection("claude", "claude-opus-5", "-m"),
        # well-formed, right engine, right flag — but NOT offered: membership is re-checked here
        model_choice.Selection("claude", "claude-unlisted-9", "--model"),
    ],
)
def test_the_provider_refuses_anything_but_its_own_resolved_selection(monkeypatch, model):
    p = _entry(monkeypatch, "claude")
    with pytest.raises(EngineError):
        p.new_launch_argv(
            "33333333-3333-3333-3333-333333333333", cwd="/w", bypass=True, model=model
        )


def test_an_engine_without_a_model_flag_refuses_a_selection(monkeypatch):
    p = _entry(monkeypatch, "kimi")
    with pytest.raises(EngineError):
        p.new_launch_argv(
            "new-44444444-4444-4444-4444-444444444444",
            cwd="/w",
            bypass=True,
            model=model_choice.Selection("kimi", "k2", "--model"),
        )


# --- operator-added ids --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("patch", "why"),
    [
        ([], "must be an object"),
        ({"claude": "claude-x"}, "must be a list"),
        ({"claude": [["x"]]}, "invalid model id"),
        ({"claude": [7]}, "invalid model id"),
        ({"claude": ["--model=x"]}, "invalid model id"),
        ({"claude": ["a b"]}, "invalid model id"),
        ({"claude": ["Default"]}, "reserved"),
        ({"claude": ["x", "x"]}, "duplicate"),
        ({"claude": ["opus"]}, "already offered"),  # an alias of a manifest model
        ({"claude": ["claude-opus-5"]}, "already offered"),
        ({"claude": [f"m{i}" for i in range(prefs.AGENT_MODELS_MAX + 1)]}, "at most"),
        ({"opencode": ["x"]}, "does not take a model"),
        ({"shell": ["x"]}, "does not take a model"),
        ({"nosuch": ["x"]}, "does not take a model"),
        ({"Bad Id": ["x"]}, "engine ids"),
        ({f"e{i}": [] for i in range(40)}, "too many"),
    ],
)
def test_operator_model_ids_are_strict_on_write(patch, why):
    err = prefs.validate_agent_defaults_patch({"models": patch})
    assert err is not None and why in err


def test_operator_model_ids_replace_per_engine_and_read_leniently(tmp_path):
    assert prefs.validate_agent_defaults_patch({"models": {"claude": ["claude-next"]}}) is None
    prefs.set_agent_defaults({"models": {"claude": ["claude-next"], "codex": ["gpt-6"]}})
    prefs.set_agent_defaults({"models": {"codex": []}})  # clears codex, keeps claude
    assert prefs.get_agent_defaults()["models"] == {"claude": ["claude-next"]}
    assert model_choice.select(_prov("claude"), "claude-next").model == "claude-next"
    # A hand-edited file cannot strand the form or smuggle a value through the read path.
    path = prefs._default_path()
    doc = json.loads(path.read_text())
    doc["agent_defaults"]["models"] = {"claude": ["ok-1", "-bad", 5, "default", "ok-1"], 3: ["x"]}
    path.write_text(json.dumps(doc))
    assert prefs.get_agent_defaults()["models"] == {"claude": ["ok-1"]}


# --- routes --------------------------------------------------------------------------------------


def _client(cfg):
    c = TestClient(create_app(cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c


def test_api_engines_serves_the_offered_models_and_instructions(auth_cfg, engine_bin):
    engine_bin()
    prefs.set_agent_defaults({"models": {"claude": ["claude-next"]}})
    c = _client(auth_cfg)
    rows = {r["id"]: r for r in c.get("/api/engines").json()["engines"]}
    sel = rows["claude"]["model_select"]
    assert sel["supported"] and sel["on_resume"] and not sel["configured_elsewhere"]
    assert [m["id"] for m in sel["offered"]][-1] == "claude-next"
    assert sel["offered"][-1]["source"] == "operator"
    assert rows["claude"]["instructions"] == {"files": ["CLAUDE.md"]}
    assert rows["opencode"]["model_select"] == {
        "supported": False,
        "on_resume": False,
        "configured_elsewhere": True,
        "offered": [],
    }
    detail = c.get("/api/engines/claude").json()
    assert detail["model_select"] == sel and detail["instructions"] == {"files": ["CLAUDE.md"]}


@pytest.mark.parametrize(
    "models", [[], "claude", {"claude": [[]]}, {"claude": [{"a": 1}]}, {"claude": [None]}]
)
def test_the_prefs_route_answers_a_malformed_model_list_with_422(auth_cfg, models):
    c = _client(auth_cfg)
    csrf = c.get("/api/config").json()["csrf"]
    r = c.post(
        "/api/prefs",
        json={"agent_defaults": {"models": models}},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422, (r.status_code, r.text)
    # …and a well-formed one is written and echoed.
    r = c.post(
        "/api/prefs",
        json={"agent_defaults": {"models": {"claude": ["claude-next"]}}},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200 and r.json()["agent_defaults"]["models"] == {
        "claude": ["claude-next"]
    }


# --- sidecar + transcript ------------------------------------------------------------------------


def test_the_requested_model_survives_placeholder_adoption():
    ph = "codex:new-55555555-5555-5555-5555-555555555555"
    real = "codex:019e2ba1-1590-7003-8e4a-51ab62cec96e"
    model_choice.record(ph, model_choice.select(_prov("codex"), "gpt-5", added=[]))
    metadata.set_alias(ph, real)
    assert metadata.requested_model(real) == "gpt-5"  # via the alias
    # A logical entry that already exists gets a copy, so it cannot shadow the record.
    ph2 = "codex:new-66666666-6666-6666-6666-666666666666"
    real2 = "codex:019e2ba1-1590-7003-8e4a-51ab62cec97f"
    model_choice.record(ph2, model_choice.select(_prov("codex"), "gpt-5-codex", added=[]))
    metadata.patch(real2, title="renamed")
    metadata.set_alias(ph2, real2)
    assert metadata.load()[real2].model_requested == "gpt-5-codex"
    assert metadata.load()[real2].title == "renamed"
    # `default` records nothing, and nothing is taken to match anything.
    k = "claude:77777777-7777-7777-7777-777777777777"
    model_choice.record(k, model_choice.select(_prov("claude"), "default"))
    assert metadata.requested_model(k) == ""


def test_a_hand_edited_requested_model_reads_as_unrecorded():
    k = "claude:88888888-8888-8888-8888-888888888888"
    metadata.patch(k, model_requested="--yolo")
    assert metadata.load()[k].model_requested == ""


def test_the_effective_model_comes_only_from_the_transcript():
    recs = [
        {"type": "assistant", "message": {"model": "claude-sonnet-5"}},
        {"type": "user", "message": {"model": "not-this"}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},  # not a model id
    ]
    assert transcript.claude_model_from_records(recs) == "claude-sonnet-5"
    assert transcript.claude_model_from_records([]) is None
    codex = [
        {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        {"type": "response_item", "payload": {"model": "nope"}},
    ]
    assert transcript.codex_model_from_records(codex) == "gpt-5-codex"
    assert transcript.codex_model_from_records([{"type": "turn_context", "payload": 3}]) is None
    # An engine with no reader is unknown, never a guess (and never the requested model).
    assert transcript.effective_model("gemini", "x", FIRST_PARTY_DIR) is None


# --- the ws launch path --------------------------------------------------------------------------


def _ws_headers(c, cfg) -> dict:
    return {"Origin": cfg.origin, "Cookie": f"agent_sessions={c.cookies.get('agent_sessions')}"}


def _close_code(c, url, headers):
    try:
        with c.websocket_connect(url, headers=headers) as ws:
            msg = ws.receive()
            if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                return msg.get("code")
            return None
    except WebSocketDisconnect as e:
        return e.code


_NEW = "claude:12345678-1234-1234-1234-123456789abc"
_GOOD = "claude:11111111-1111-1111-1111-111111111111"


class _Capture:
    """Wrap a provider's argv builder: record what it built, then refuse (4500) so nothing runs."""

    def __init__(self, monkeypatch, prov, name):
        self.calls: list[tuple[list[str] | None, dict]] = []
        real = getattr(prov, name)

        def spy(*a, **kw):
            argv = None
            try:
                argv = real(*a, **kw)
            finally:
                self.calls.append((argv, kw))
            raise EngineError("captured")

        monkeypatch.setattr(prov, name, spy)


def test_ws_new_session_refuses_a_bad_model_before_anything_is_built(
    fake_jsonl, auth_cfg, monkeypatch
):
    prov = _entry(monkeypatch, "claude")
    cap = _Capture(monkeypatch, prov, "new_launch_argv")
    recorded: list = []
    monkeypatch.setattr(model_choice, "record", lambda *a: recorded.append(a))
    c = _client(auth_cfg)
    cwd = fake_jsonl / "proj"
    cwd.mkdir()
    for bad in ("--dangerously-skip-permissions", "gpt-5", "claude-next"):
        assert (
            _close_code(c, f"/ws/term/{_NEW}?new=1&cwd={cwd}&model={bad}", _ws_headers(c, auth_cfg))
            == 4422
        )
    assert cap.calls == [] and recorded == []


def test_ws_new_session_launches_the_canonical_model(fake_jsonl, auth_cfg, monkeypatch):
    prov = _entry(monkeypatch, "claude")
    cap = _Capture(monkeypatch, prov, "new_launch_argv")
    c = _client(auth_cfg)
    cwd = fake_jsonl / "proj"
    cwd.mkdir()
    assert (
        _close_code(c, f"/ws/term/{_NEW}?new=1&cwd={cwd}&model=opus", _ws_headers(c, auth_cfg))
        == 4500
    )
    argv, kw = cap.calls[-1]
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    # `default` passes no selection at all: exactly the launch before #1189.
    assert (
        _close_code(c, f"/ws/term/{_NEW}?new=1&cwd={cwd}&model=default", _ws_headers(c, auth_cfg))
        == 4500
    )
    argv, kw = cap.calls[-1]
    assert "model" not in kw and "--model" not in argv


def test_ws_resume_applies_or_refuses_per_on_resume(fake_jsonl, auth_cfg, monkeypatch):
    from agent_sessions import prefs as _prefs
    from agent_sessions import project_dirs

    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [])
    monkeypatch.setattr(_prefs, "get_folder_exclusions", lambda path=None: [])
    prov = _entry(monkeypatch, "claude")
    cap = _Capture(monkeypatch, prov, "launch_argv")
    c = _client(auth_cfg)
    h = _ws_headers(c, auth_cfg)
    assert _close_code(c, f"/ws/term/{_GOOD}?model=haiku", h) == 4500
    argv, _ = cap.calls[-1]
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    # The same engine WITHOUT on_resume: the session recorded nothing, so an explicit model is
    # refused before the argv is built (4422), and `default` is today's resume.
    m = prov.manifest
    no_resume = dataclasses.replace(
        m,
        launch=dataclasses.replace(
            m.launch, model=dataclasses.replace(m.launch.model, on_resume=False)
        ),
    )
    monkeypatch.setattr(prov, "manifest", no_resume)
    n = len(cap.calls)
    assert _close_code(c, f"/ws/term/{_GOOD}?model=haiku", h) == 4422
    assert len(cap.calls) == n
    assert _close_code(c, f"/ws/term/{_GOOD}?model=default", h) == 4500
    # …and the recorded model is accepted, with no flag.
    metadata.patch(_GOOD, model_requested="claude-haiku-4-5")
    assert _close_code(c, f"/ws/term/{_GOOD}?model=haiku", h) == 4500
    argv, kw = cap.calls[-1]
    assert "--model" not in argv


def test_ws_attach_never_reads_the_model(fake_jsonl, auth_cfg, monkeypatch):
    from agent_sessions import ptybridge, sessions
    from agent_sessions.routes import terminal

    async def attach(engine, native):
        return sessions.ATTACH, None

    def refuse(**kw):
        raise ptybridge.PtyBridgeError("stop here")

    def boom(*a, **kw):
        raise AssertionError("ATTACH must never resolve a model")

    monkeypatch.setattr(terminal, "_open_action_offloop", attach)
    monkeypatch.setattr(ptybridge, "attach_argv", refuse)
    monkeypatch.setattr(model_choice, "select", boom)
    monkeypatch.setattr(model_choice, "select_resume", boom)
    c = _client(auth_cfg)
    # A junk model on an attach to a live master is ignored, not refused: the master's model is
    # never changed by attaching (attach-never-relaunch). 4500 is the stubbed attach refusal.
    assert _close_code(c, f"/ws/term/{_GOOD}?model=--yolo", _ws_headers(c, auth_cfg)) == 4500


# --- mission dispatch plumbing (#1194 is the first real caller) ---------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("model", [None, "opus"])
async def test_mission_dispatch_forwards_an_optional_model(monkeypatch, model):
    from agent_sessions import headless_dispatch, mission_dispatch

    seen: dict = {}

    async def dispatch(**kw):
        seen.update(kw)
        raise headless_dispatch.DispatchError("stop before anything runs")

    async def settle(*a, **kw):
        return None

    monkeypatch.setattr(headless_dispatch, "dispatch", dispatch)
    monkeypatch.setattr(mission_dispatch, "_settle", settle)
    out = await mission_dispatch.run(
        "m1",
        {"engine": "claude", "cwd": "/w", "brief": "go"},
        registry=object(),
        policy_epoch="e",
        model=model,
    )
    assert out["outcome"] == "refused"
    if model is None:
        assert "model" not in seen  # exactly the dispatch before #1189
    else:
        assert seen["model"] == "opus"


def test_list_and_lookup_rows_share_one_shape_and_only_the_lookup_reads_the_model(
    auth_cfg, fake_jsonl
):
    # #867's invariant: the pane's lookup row IS the list's row shape. `model_effective` is on
    # both — null on the list (never read there), filled on the lookup from the transcript.
    jsonl = (
        fake_jsonl
        / ".claude"
        / "projects"
        / "-home-user-claude-repo-a"
        / "11111111-1111-1111-1111-111111111111.jsonl"
    )
    with jsonl.open("a") as fh:
        fh.write('{"type":"assistant","message":{"model":"claude-sonnet-5","content":"hi"}}\n')
    c = _client(auth_cfg)
    listed = next(
        s
        for s in c.get("/api/sessions?limit=100").json()["sessions"]
        if s["uuid"] == "11111111-1111-1111-1111-111111111111"
    )
    looked = c.get(f"/api/sessions/{_GOOD}").json()
    assert set(listed) == set(looked)
    assert listed["model_effective"] is None
    assert looked["model_effective"] == "claude-sonnet-5"
    assert {k: v for k, v in looked.items() if k != "model_effective"} == {
        k: v for k, v in listed.items() if k != "model_effective"
    }
    assert listed["model_requested"] is None and looked["model_requested"] is None


def test_a_renamed_late_id_session_keeps_its_requested_model_on_every_row(
    auth_cfg, fake_jsonl, monkeypatch, tmp_path
):
    # A late-id session records its model under the placeholder; adoption aliases it to the real
    # id; a rename then CREATES the real id's entry without `model_requested`. The rows must read
    # the request through the one precedence a resume uses, not off that entry as a whole.
    root = tmp_path / "codex-sessions"
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(root))
    native = "019e2ba1-1590-7003-8e4a-51ab62cec9cc"
    ph = "codex:new-cccccccc-cccc-cccc-cccc-cccccccccccc"
    real = f"codex:{native}"
    d = root / "2026" / "05" / "15"
    d.mkdir(parents=True)
    (d / f"rollout-2026-05-15T15-33-57-{native}.jsonl").write_text(
        "\n".join(
            json.dumps(x)
            for x in (
                {"type": "session_meta", "payload": {"id": native, "cwd": str(fake_jsonl)}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello codex"}],
                    },
                },
            )
        )
        + "\n"
    )
    engines.invalidate_scan_cache()
    prov = _entry(monkeypatch, "codex")
    model_choice.record(ph, model_choice.select(prov, "gpt-5", added=[]))
    metadata.set_alias(ph, real)
    c = _client(auth_cfg)
    csrf = c.get("/api/config").json()["csrf"]
    r = c.post(
        f"/api/sessions/{real}/rename",
        json={"title": "renamed"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    # The trap: the real id's entry now exists, carries the title, and has no request on it.
    assert metadata.load()[real].title == "renamed"
    assert metadata.load()[real].model_requested == ""
    assert metadata.requested_model(real) == "gpt-5"
    listed = next(s for s in c.get("/api/sessions?limit=100").json()["sessions"] if s["id"] == real)
    looked = c.get(f"/api/sessions/{real}").json()
    assert listed["title"] == looked["title"] == "renamed"
    assert listed["model_requested"] == looked["model_requested"] == "gpt-5"
    # …and a resume applies the same request where the engine takes the flag on resume.
    _model_on_resume(monkeypatch, prov, True)
    sel = model_choice.select_resume(prov, None, real)
    assert (sel.model, sel.flag) == ("gpt-5", "--model")


def test_a_resume_record_on_an_adopted_late_id_session_keeps_its_placeholder_metadata(
    auth_cfg, fake_jsonl, monkeypatch, tmp_path
):
    # The title and sticky flag were set while the session was still under its placeholder, so
    # they live ONLY on the placeholder entry. A resume record (an engine that honours the flag on
    # resume) must not create a sparse logical entry: the rows read the logical entry first, so
    # one would hide the title and sticky. The record lands on the entry `resolve_key` names.
    root = tmp_path / "codex-sessions"
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(root))
    native = "019e2ba1-1590-7003-8e4a-51ab62cec9dd"
    ph = "codex:new-dddddddd-dddd-dddd-dddd-dddddddddddd"
    real = f"codex:{native}"
    d = root / "2026" / "05" / "15"
    d.mkdir(parents=True)
    (d / f"rollout-2026-05-15T15-33-57-{native}.jsonl").write_text(
        "\n".join(
            json.dumps(x)
            for x in (
                {"type": "session_meta", "payload": {"id": native, "cwd": str(fake_jsonl)}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello codex"}],
                    },
                },
            )
        )
        + "\n"
    )
    engines.invalidate_scan_cache()
    prov = _entry(monkeypatch, "codex")
    metadata.patch(ph, title="my title", sticky=True)
    metadata.set_alias(ph, real)
    _model_on_resume(monkeypatch, prov, True)
    model_choice.record(real, model_choice.select_resume(prov, "gpt-5", real))
    # No sparse logical entry: the record sits beside the title and sticky.
    assert real not in metadata.load()
    assert metadata.get(ph).title == "my title" and metadata.get(ph).sticky
    assert metadata.requested_model(real) == "gpt-5"
    c = _client(auth_cfg)
    listed = next(s for s in c.get("/api/sessions?limit=100").json()["sessions"] if s["id"] == real)
    looked = c.get(f"/api/sessions/{real}").json()
    for row in (listed, looked):
        assert row["title"] == "my title"
        assert row["sticky"] is True
        assert row["model_requested"] == "gpt-5"
    # …and a later model-less resume applies the recorded model.
    sel = model_choice.select_resume(prov, None, real)
    assert (sel.model, sel.flag) == ("gpt-5", "--model")


def test_the_default_marker_reads_as_null_on_every_row(auth_cfg, fake_jsonl):
    metadata.patch(_GOOD, model_requested=manifest.MODEL_DEFAULT)
    c = _client(auth_cfg)
    listed = next(
        s for s in c.get("/api/sessions?limit=100").json()["sessions"] if s["id"] == _GOOD
    )
    looked = c.get(f"/api/sessions/{_GOOD}").json()
    assert listed["model_requested"] is None and looked["model_requested"] is None


class _Bridge:
    """A ws LAUNCH path that gets past the argv build to a stand-in bridge (#1189).

    `spawns=True`: the bridge brings the dtach master up — `ptybridge.session_exists` answers yes
    for that socket, the success signal the record watch polls for. `spawns=False`: the spawn
    fails (the real bridge's 4502 path) and no master ever appears. `raises`: the bridge itself
    errors. Every connect is a LAUNCH (never an attach).

    The connections share ONE event loop (`client`), as the app's do in production. A record watch
    is an independent task that outlives the connection that started it, so the test's loop must
    too: with a loop per connection the watch died with the socket. That was the #1254 CI flake —
    the client hung up on the `role` frame the route sends BEFORE the bridge runs, the test client
    cancelled the app, and the watch was cancelled (or never ran) unless its record thread had
    already started. `connect` returns only once the route has finished AND every watch started so
    far has concluded (recorded, lapsed, or cancelled), so each assertion after it reads a settled
    store — no wall-clock settle. `recorded` lists every `model_choice.record` call; `outcomes`
    says how each watch ended (`done` = it ran to the end, `cancelled` = the route stopped it).
    """

    def __init__(self, monkeypatch, *, spawns: bool = True, raises: bool = False):
        import threading

        from agent_sessions import ptybridge, relaunch, scopedspawn, sessions, webterm
        from agent_sessions.routes import terminal

        self.alive: set[str] = set()
        self.spawns = spawns
        self.raises = raises
        self.recorded: list[tuple[str, str | None]] = []
        self.outcomes: list[str] = []
        # Set to an asyncio.Event to hold every new watch before its first liveness check;
        # `release()` lets them go.
        self.hold: asyncio.Event | None = None
        self._cond = threading.Condition()
        self._started = 0
        self._portal_cm = None
        self._portal = None

        async def launch(engine, native):
            return sessions.LAUNCH, None

        async def run(ws, argv, **kw):
            await self._spawn(kw["buf_key"].partition(":")[2], ws)

        async def takeover(ws, **kw):
            await self._spawn(kw["phys_native"], ws)

        real_watch = terminal._record_model_once_live

        async def watched(*a):
            outcome = "cancelled"
            try:
                if self.hold is not None:
                    await self.hold.wait()
                await real_watch(*a)
                outcome = "done"
            finally:
                with self._cond:
                    self.outcomes.append(outcome)
                    self._cond.notify_all()

        def watch(*a):
            # Counted when the route CALLS it (synchronously, at create_task), not when the task
            # first runs, so `settle` can never pass before a watch has even started.
            with self._cond:
                self._started += 1
            return watched(*a)

        real_record = model_choice.record

        def record(key, sel):
            self.recorded.append((key, sel.model))
            real_record(key, sel)

        monkeypatch.setattr(terminal, "_open_action_offloop", launch)
        # These launches "exit" instantly by design; keep them out of the relaunch backstop's
        # per-key count, which would otherwise block later tests' launches of the same key.
        monkeypatch.setattr(relaunch, "note_exit", lambda *a, **kw: None)
        monkeypatch.setattr(terminal, "_record_model_once_live", watch)
        monkeypatch.setattr(terminal, "_MODEL_RECORD_POLL_S", 0.02)
        monkeypatch.setattr(model_choice, "record", record)
        monkeypatch.setattr(ptybridge, "launch_argv", lambda **kw: ["/bin/true"])
        monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: n in self.alive)
        monkeypatch.setattr(scopedspawn, "wrap", lambda argv, **kw: (argv, None))
        monkeypatch.setattr(webterm, "run", run)
        monkeypatch.setattr(terminal, "_serve_takeover", takeover)

    async def _spawn(self, native: str, ws) -> None:
        if self.spawns and not self.raises:
            self.alive.add(native)
        # The bridge ends the connection itself, so the client never hangs up mid-route.
        with contextlib.suppress(Exception):
            await ws.close(code=1011 if self.raises else 1000 if self.spawns else 4502)
        if self.raises:
            raise RuntimeError("bridge error")

    def client(self, cfg):
        """A logged-in client whose connections all run on one loop that outlives each of them."""
        import anyio.from_thread

        c = _client(cfg)
        self._portal_cm = anyio.from_thread.start_blocking_portal(**c.async_backend)
        self._portal = self._portal_cm.__enter__()
        c.portal = self._portal
        return c

    def close(self) -> None:
        if self._portal_cm is not None:
            self._portal_cm.__exit__(None, None, None)
            self._portal_cm = None

    def connect(self, c, url, headers, *, settle: bool = True) -> int | None:
        """One connection, start to finish; the server's close code. With `settle` (the default),
        also wait until every record watch started so far has concluded."""
        code = None
        try:
            with c.websocket_connect(url, headers=headers) as ws:
                while True:
                    msg = ws.receive()
                    if msg.get("type") == "websocket.close":
                        code = msg.get("code")
                        break
        except WebSocketDisconnect as e:
            code = e.code
        except RuntimeError:
            pass  # the `raises` bridge's error, re-raised by the test client on the way out
        if settle:
            self.settle()
        return code

    def settle(self) -> None:
        with self._cond:
            # A hang guard only: every watch here concludes in milliseconds.
            assert self._cond.wait_for(lambda: len(self.outcomes) == self._started, 30.0)

    def release(self) -> None:
        assert self.hold is not None and self._portal is not None
        self._portal.call(self.hold.set)

    def exit_master(self) -> None:
        """The agent quit: the next connect is a fresh LAUNCH of a session with no live master."""
        self.alive.clear()


@pytest.fixture
def bridge(monkeypatch):
    made: list[_Bridge] = []

    def make(**kw) -> _Bridge:
        made.append(_Bridge(monkeypatch, **kw))
        return made[-1]

    yield make
    for b in made:
        b.close()


def _refuse_dtach_argv(monkeypatch):
    """The dtach argv build refuses (a PtyBridgeError BEFORE anything is spawned)."""
    from agent_sessions import ptybridge

    def refuse(**kw):
        raise ptybridge.PtyBridgeError("refused")

    monkeypatch.setattr(ptybridge, "launch_argv", refuse)


def _resume_env(monkeypatch):
    from agent_sessions import project_dirs

    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])


def _spy_launch_argv(monkeypatch, prov) -> list[list[str]]:
    built: list[list[str]] = []
    real = prov.launch_argv

    def spy(*a, **kw):
        argv = real(*a, **kw)
        built.append(argv)
        return argv

    monkeypatch.setattr(prov, "launch_argv", spy)
    return built


def _model_on_resume(monkeypatch, prov, on: bool) -> None:
    """Whether the engine takes `--model` on resume too: claude's shape (True) or new sessions
    only, codex's and gemini's (False)."""
    m = prov.manifest
    monkeypatch.setattr(
        prov,
        "manifest",
        dataclasses.replace(
            m,
            launch=dataclasses.replace(
                m.launch, model=dataclasses.replace(m.launch.model, on_resume=on)
            ),
        ),
    )


def _run_watch(monkeypatch, answers, *, window_s: float) -> tuple[list[str], list]:
    """Drive `_record_model_once_live` alone: `session_exists` gives `answers` in turn, then
    False forever. Returns (each liveness check, each record)."""
    from agent_sessions import ptybridge
    from agent_sessions.routes import terminal

    checks: list[str] = []
    recorded: list = []
    it = iter(answers)

    def exists(engine, native):
        checks.append(native)
        return next(it, False)

    monkeypatch.setattr(ptybridge, "session_exists", exists)
    monkeypatch.setattr(model_choice, "record", lambda key, sel: recorded.append((key, sel.model)))
    monkeypatch.setattr(terminal, "_MODEL_RECORD_WAIT_S", window_s)
    monkeypatch.setattr(terminal, "_MODEL_RECORD_POLL_S", 0.0)
    sel = model_choice.select(_prov("claude"), "opus")
    asyncio.run(terminal._record_model_once_live("claude", _GOOD.partition(":")[2], _GOOD, sel))
    return checks, recorded


def test_record_watch_records_once_when_the_master_comes_up_after_the_first_check(
    fake_jsonl, monkeypatch
):
    # Production order: the watch starts BEFORE webterm.run spawns, so its first checks find no
    # master; the polling loop is what records — once, when the master appears.
    checks, recorded = _run_watch(monkeypatch, [False, False, False, True], window_s=30.0)
    assert len(checks) == 4
    assert recorded == [(_GOOD, "claude-opus-5")]


def test_record_watch_records_nothing_when_no_master_appears_before_the_deadline(
    fake_jsonl, monkeypatch
):
    checks, recorded = _run_watch(monkeypatch, [], window_s=0.2)
    assert len(checks) > 1  # it kept looking for the whole window …
    assert recorded == []  # … and gave up without a record


def test_ws_new_session_records_the_requested_model_once_the_master_is_live(
    fake_jsonl, auth_cfg, monkeypatch, bridge
):
    _entry(monkeypatch, "claude")
    b = bridge()
    c = b.client(auth_cfg)
    cwd = fake_jsonl / "proj"
    cwd.mkdir()
    b.connect(c, f"/ws/term/{_NEW}?new=1&cwd={cwd}&model=opus", _ws_headers(c, auth_cfg))
    assert "12345678-1234-1234-1234-123456789abc" in b.alive
    # canonical, never the alias — and only once the master it asked for exists
    assert metadata.requested_model(_NEW) == "claude-opus-5"
    # A default launch starts no watch and records nothing.
    other = "claude:12345678-1234-1234-1234-123456789abd"
    b.connect(c, f"/ws/term/{other}?new=1&cwd={cwd}", _ws_headers(c, auth_cfg))
    assert b.outcomes == ["done"]
    assert b.recorded == [(_NEW, "claude-opus-5")]
    assert metadata.requested_model(other) == ""


@pytest.mark.parametrize("failure", ["spawn", "bridge_error", "argv_refused"])
def test_ws_new_session_that_never_launches_records_no_model(
    fake_jsonl, auth_cfg, monkeypatch, bridge, failure
):
    _entry(monkeypatch, "claude")
    b = bridge(spawns=False, raises=failure == "bridge_error")
    if failure == "argv_refused":
        _refuse_dtach_argv(monkeypatch)
    c = b.client(auth_cfg)
    cwd = fake_jsonl / "proj"
    cwd.mkdir()
    code = b.connect(c, f"/ws/term/{_NEW}?new=1&cwd={cwd}&model=opus", _ws_headers(c, auth_cfg))
    if failure == "argv_refused":
        assert code == 4500
        assert b.outcomes == []  # a refused argv never starts the watch
    else:
        # The route stopped the watch on its way out: no master was left behind.
        assert b.outcomes == ["cancelled"]
    assert not b.alive
    assert b.recorded == []
    assert metadata.get(_NEW).model_requested == ""


@pytest.mark.parametrize("failure", ["spawn", "bridge_error", "argv_refused"])
def test_ws_resume_that_never_launches_keeps_the_prior_record(
    fake_jsonl, auth_cfg, monkeypatch, bridge, failure
):
    _resume_env(monkeypatch)
    _entry(monkeypatch, "claude")
    b = bridge(spawns=False, raises=failure == "bridge_error")
    if failure == "argv_refused":
        _refuse_dtach_argv(monkeypatch)
    metadata.patch(_GOOD, model_requested="claude-opus-5")
    c = b.client(auth_cfg)
    h = _ws_headers(c, auth_cfg)
    # A different model, and an explicit default: neither ran, so neither replaces the record.
    for q in ("?model=sonnet", "?model=default"):
        b.connect(c, f"/ws/term/{_GOOD}{q}", h)
    assert not b.alive
    assert b.outcomes == ([] if failure == "argv_refused" else ["cancelled", "cancelled"])
    assert b.recorded == []
    assert metadata.requested_model(_GOOD) == "claude-opus-5"


def test_ws_resume_records_a_new_model_once_the_master_is_live(
    fake_jsonl, auth_cfg, monkeypatch, bridge
):
    _resume_env(monkeypatch)
    _entry(monkeypatch, "claude")
    b = bridge()
    metadata.patch(_GOOD, model_requested="claude-opus-5")
    c = b.client(auth_cfg)
    b.connect(c, f"/ws/term/{_GOOD}?model=sonnet", _ws_headers(c, auth_cfg))
    assert b.recorded == [(_GOOD, "claude-sonnet-5")]
    assert metadata.requested_model(_GOOD) == "claude-sonnet-5"


def test_ws_record_watch_outlives_a_client_that_leaves_once_the_master_is_up(
    fake_jsonl, auth_cfg, monkeypatch, bridge
):
    # The master outlives the browser: a client gone before the watch's first check must not
    # cost the record. The watch is held until the connection has fully torn down.
    _resume_env(monkeypatch)
    _entry(monkeypatch, "claude")
    b = bridge()
    b.hold = asyncio.Event()
    metadata.patch(_GOOD, model_requested="claude-opus-5")
    c = b.client(auth_cfg)
    b.connect(c, f"/ws/term/{_GOOD}?model=haiku", _ws_headers(c, auth_cfg), settle=False)
    assert b.outcomes == [] and b.recorded == []  # the route is done; its watch is still waiting
    b.release()
    b.settle()
    assert b.outcomes == ["done"]
    assert b.recorded == [(_GOOD, "claude-haiku-4-5")]
    assert metadata.requested_model(_GOOD) == "claude-haiku-4-5"


def test_ws_failed_launch_watch_never_records_for_a_later_launchs_master(
    fake_jsonl, auth_cfg, monkeypatch, bridge
):
    # Connection 1 asks for opus and its spawn fails; connection 2 then launches the same session
    # on haiku, well inside connection 1's record window (the production one). The master on the
    # socket is connection 2's: only haiku may be recorded, never opus.
    from agent_sessions.routes import terminal

    _resume_env(monkeypatch)
    _entry(monkeypatch, "claude")
    b = bridge(spawns=False)
    assert terminal._MODEL_RECORD_WAIT_S > 10  # the window is still open for connection 2
    metadata.patch(_GOOD, model_requested="claude-sonnet-5")
    c = b.client(auth_cfg)
    h = _ws_headers(c, auth_cfg)
    assert b.connect(c, f"/ws/term/{_GOOD}?model=opus", h, settle=False) == 4502
    b.spawns = True
    b.connect(c, f"/ws/term/{_GOOD}?model=haiku", h)
    assert sorted(b.outcomes) == ["cancelled", "done"]
    assert b.recorded == [(_GOOD, "claude-haiku-4-5")]
    assert metadata.requested_model(_GOOD) == "claude-haiku-4-5"


def test_ws_explicit_default_resume_replaces_the_record_and_the_next_resume_sends_no_flag(
    fake_jsonl, auth_cfg, monkeypatch, bridge
):
    _resume_env(monkeypatch)
    prov = _entry(monkeypatch, "claude")
    b = bridge()
    built = _spy_launch_argv(monkeypatch, prov)
    metadata.patch(_GOOD, model_requested="claude-opus-5")
    c = b.client(auth_cfg)
    h = _ws_headers(c, auth_cfg)

    def row_model():
        return c.get(f"/api/sessions/{_GOOD}").json()["model_requested"]

    assert row_model() == "claude-opus-5"
    # 1. An explicit default resume: no flag, and once it is live the record says default.
    b.connect(c, f"/ws/term/{_GOOD}?model=default", h)
    assert "--model" not in built[-1]
    assert metadata.requested_model(_GOOD) == ""
    assert row_model() is None  # the row shows default (no tag), not the old model
    # 2. The agent quit; an ordinary resume (no model) re-applies nothing.
    b.exit_master()
    b.connect(c, f"/ws/term/{_GOOD}", h)
    assert len(built) == 2 and "--model" not in built[-1]
    assert row_model() is None
    # 3. A later explicit pick records again, over the default marker.
    b.exit_master()
    b.connect(c, f"/ws/term/{_GOOD}?model=haiku", h)
    assert b.outcomes == ["done", "done"]
    assert b.recorded == [(_GOOD, None), (_GOOD, "claude-haiku-4-5")]
    assert metadata.requested_model(_GOOD) == "claude-haiku-4-5"


@pytest.mark.parametrize("engine", ["codex", "gemini"])
def test_explicit_default_resume_replaces_no_record_where_the_engine_ignores_the_flag_on_resume(
    fake_jsonl, engine
):
    prov = _prov(engine)
    assert not prov.manifest.launch.model.on_resume
    sel = model_choice.select_resume(prov, "default", f"{engine}:x")
    assert sel.model is None and sel.flag is None and not sel.replaces_record


def test_ws_explicit_default_resume_keeps_the_record_where_the_engine_ignores_the_flag_on_resume(
    fake_jsonl, auth_cfg, monkeypatch, bridge
):
    # The engine keeps the model it started with, so the record — which names it — stays: no
    # `default` marker, no watch at all.
    _resume_env(monkeypatch)
    prov = _entry(monkeypatch, "claude")
    _model_on_resume(monkeypatch, prov, False)
    b = bridge()
    metadata.patch(_GOOD, model_requested="claude-opus-5")
    c = b.client(auth_cfg)
    b.connect(c, f"/ws/term/{_GOOD}?model=default", _ws_headers(c, auth_cfg))
    assert _GOOD.partition(":")[2] in b.alive  # it did launch
    assert b.outcomes == [] and b.recorded == []
    assert metadata.get(_GOOD).model_requested == "claude-opus-5"
    assert metadata.requested_model(_GOOD) == "claude-opus-5"


def test_default_marker_shadows_a_placeholder_record(fake_jsonl):
    # A late-id session recorded its model under the placeholder; an explicit default resume
    # writes the marker on the entry the session's metadata lives on (here the placeholder's, as
    # no logical entry exists), and the placeholder's model must not resurface.
    ph = "codex:new-88888888-8888-8888-8888-888888888888"
    real = "codex:019e2ba1-1590-7003-8e4a-51ab62cec9aa"
    model_choice.record(ph, model_choice.select(_prov("codex"), "gpt-5", added=[]))
    metadata.set_alias(ph, real)
    assert metadata.requested_model(real) == "gpt-5"
    model_choice.record(real, model_choice.Selection("codex", None, None, replaces_record=True))
    assert metadata.requested_model(real) == ""


def test_default_marker_on_the_placeholder_entry_reads_as_default(fake_jsonl):
    # The explicit default resume ran while the session was still under its placeholder: the
    # marker sits on the PHYSICAL entry and the logical one has nothing. Followed through the
    # alias, it still reads as default — never as the literal marker.
    ph = "codex:new-99999999-9999-9999-9999-999999999999"
    real = "codex:019e2ba1-1590-7003-8e4a-51ab62cec9bb"
    model_choice.record(ph, model_choice.Selection("codex", None, None, replaces_record=True))
    metadata.set_alias(ph, real)
    assert metadata.get(real).model_requested == ""
    assert metadata.get(ph).model_requested == manifest.MODEL_DEFAULT
    assert metadata.requested_model(real) == ""
    assert metadata.requested_model(ph) == ""


def test_still_offered_accepts_only_canonical_ids(monkeypatch):
    prov = _prov("claude")
    m = prov.manifest
    assert model_choice.still_offered(m, "claude-opus-5")
    assert not model_choice.still_offered(m, "opus")  # an alias is never canonical
    # …even when the operator's (hand-edited) list also names the alias.
    monkeypatch.setattr(model_choice, "operator_ids", lambda e: ["opus", "claude-next"])
    assert not model_choice.still_offered(m, "opus")
    assert model_choice.still_offered(m, "claude-next")
    assert not model_choice.still_offered(m, "claude-gone")


def test_recorded_not_offered_refusal_puts_the_id_last_and_names_the_way_back(
    fake_jsonl, monkeypatch
):
    prov = _prov("claude")
    long_id = "claude-" + "x" * 89  # the longest shape an id may have (96)
    for rid in ("claude-next", long_id):
        monkeypatch.setattr(model_choice, "recorded_for", lambda key, rid=rid: rid)
        with pytest.raises(model_choice.ModelRefused) as e:
            model_choice.select_resume(prov, None, _GOOD)
        detail = e.value.detail
        assert e.value.code == "recorded_not_offered"
        assert detail.endswith(rid)
        assert "no longer offered" in detail and "Settings → Agents" in detail
        # The ws close-reason cap cuts only the id, never the instruction before it.
        cut = detail.encode("utf-8")[:120].decode("utf-8", "ignore")
        assert cut.startswith(detail[: -len(rid)])


def test_ws_default_resume_reapplies_the_recorded_model_where_the_engine_honours_it(
    fake_jsonl, auth_cfg, monkeypatch
):
    from agent_sessions import project_dirs

    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])
    prov = _entry(monkeypatch, "claude")
    cap = _Capture(monkeypatch, prov, "launch_argv")
    c = _client(auth_cfg)
    h = _ws_headers(c, auth_cfg)
    # Nothing recorded: today's resume.
    assert _close_code(c, f"/ws/term/{_GOOD}", h) == 4500
    assert "--model" not in cap.calls[-1][0]
    # Started on an operator-added model: a resume with NO model re-applies it (re-validated).
    prefs.set_agent_defaults({"models": {"claude": ["claude-next"]}})
    metadata.patch(_GOOD, model_requested="claude-next")
    assert _close_code(c, f"/ws/term/{_GOOD}", h) == 4500
    argv = cap.calls[-1][0]
    assert argv[argv.index("--model") + 1] == "claude-next"
    # An explicit `default` is a deliberate "no flag".
    assert _close_code(c, f"/ws/term/{_GOOD}?model=default", h) == 4500
    assert "--model" not in cap.calls[-1][0]
    # The recorded model is no longer offered: refused before the argv is built, never defaulted.
    prefs.set_agent_defaults({"models": {"claude": []}})
    n = len(cap.calls)
    assert _close_code(c, f"/ws/term/{_GOOD}", h) == 4422
    assert len(cap.calls) == n
    with pytest.raises(model_choice.ModelRefused) as e:
        model_choice.select_resume(prov, None, _GOOD)
    assert e.value.code == "recorded_not_offered" and "no longer offered" in e.value.detail
    # An engine that cannot take the flag on resume resumes as today, recorded model or not.
    m = prov.manifest
    no_resume = dataclasses.replace(
        m,
        launch=dataclasses.replace(
            m.launch, model=dataclasses.replace(m.launch.model, on_resume=False)
        ),
    )
    monkeypatch.setattr(prov, "manifest", no_resume)
    assert _close_code(c, f"/ws/term/{_GOOD}", h) == 4500
    assert "--model" not in cap.calls[-1][0]
