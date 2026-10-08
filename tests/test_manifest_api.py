"""#1275: native protocol declarations never grant execution or borrow a stale source."""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import shutil
import tomllib
from types import SimpleNamespace

import pytest

from agent_sessions import engines
from agent_sessions.engines import registry
from agent_sessions.plugins import (
    FIRST_PARTY_DIR,
    PluginProvider,
    api_source,
    feed,
    kinds,
    manifest,
    provenance,
)


def _doc(engine):
    return tomllib.loads((FIRST_PARTY_DIR / engine / "plugin.toml").read_text())


def _api(*, name="native-api", source="codex", kind="codex-app-server"):
    doc = _doc("apichat")
    doc["identity"]["id"] = name
    doc["runtime"]["kind"] = "api"
    doc["api"] = {"kind": kind, "source": source}
    doc.pop("endpoint")
    doc.pop("models")
    return doc


def _provider(doc, tmp_path):
    m = manifest.parse(copy.deepcopy(doc))
    return PluginProvider(
        m,
        trust=provenance.FIRST_PARTY,
        root=tmp_path / m.id,
        home=tmp_path,
        env={},
        state_dir=tmp_path / "state",
    )


@pytest.mark.parametrize(
    "kind,source", [("codex-app-server", "codex"), ("claude-stream-json", "claude")]
)
def test_native_api_has_only_a_source_reference(kind, source, tmp_path):
    prov = _provider(_api(kind=kind, source=source), tmp_path)
    src = _provider(_doc(source), tmp_path)
    m = prov.manifest
    assert m.contract == kinds.CONTRACT_CURRENT == 1
    assert m.api == manifest.Api(kind, source)
    assert m.binary is m.launch is m.endpoint is m.install is None
    assert m.instructions == m.models == ()
    assert api_source.validate_provider(prov, {source: src}) is src
    assert prov.entrypoint() is None
    assert not registry.can_start(prov)
    with pytest.raises(engines.EngineError, match="nothing to launch"):
        prov.entrypoint_path()


@pytest.mark.parametrize(
    "block",
    [*kinds.PTY_ONLY_BLOCKS, "endpoint", "models", "maintenance"],
)
def test_native_api_cannot_carry_an_executable_setup_or_override(block):
    doc = _api()
    doc[block] = {}
    with pytest.raises(manifest.ManifestError, match=f"{block}: is forbidden"):
        manifest.parse(doc)


@pytest.mark.parametrize("capability", sorted(kinds.PTY_ONLY_CAPABILITIES))
def test_native_api_cannot_claim_console_capabilities(capability):
    doc = _api()
    doc["capabilities"][capability] = True
    with pytest.raises(manifest.ManifestError, match=f"capabilities.{capability}"):
        manifest.parse(doc)


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda d: d.pop("api"), "api"),
        (lambda d: d["api"].update(kind="arbitrary-command"), "api.kind"),
        (lambda d: d["api"].update(source="native-api"), "api.source"),
        (lambda d: d["api"].update(source="../../binary"), "api.source"),
        (lambda d: d["api"].update(source=True), "api.source"),
        (lambda d: d["api"].update(argv=["--unsafe"]), "api.argv"),
        (lambda d: d["identity"].update(kind="terminal"), "identity.kind"),
        (lambda d: d["session_id"].update(mint="adopt"), "session_id"),
        (lambda d: d["session_id"].update(legacy_bare_id=True), "session_id"),
        (lambda d: d["session_id"].update(pattern="^[a-z]{8}$"), "session_id.pattern"),
        (lambda d: d.pop("store"), "store"),
        (lambda d: d["store"].update(layout="codex-rollouts"), "store.layout"),
        (lambda d: d["store"].update(read_only=True), "store.read_only"),
        (lambda d: d["transcript"].update(kind="codex-rollout"), "transcript.kind"),
        (lambda d: d["usage"].update(kind="codex-app-server-probe"), "usage.kind"),
    ],
)
def test_native_api_invalid_shape_is_refused(mutate, field):
    doc = _api()
    mutate(doc)
    with pytest.raises(manifest.ManifestError) as exc:
        manifest.parse(doc)
    assert exc.value.field == field


@pytest.mark.parametrize("engine", ["apichat", "codex"])
def test_api_block_cannot_widen_an_existing_runtime(engine):
    doc = _doc(engine)
    doc["api"] = {"kind": "codex-app-server", "source": "codex"}
    with pytest.raises(manifest.ManifestError, match="only allowed for runtime 'api'"):
        manifest.parse(doc)


def test_old_reader_refuses_the_additive_api_runtime(monkeypatch):
    monkeypatch.setattr(kinds, "RUNTIME_KINDS", frozenset({"pty", "chat"}))
    with pytest.raises(manifest.ManifestError, match="needs a newer BattleLab"):
        manifest.parse(_api())


def test_every_existing_manifest_still_parses():
    for path in FIRST_PARTY_DIR.glob("*/plugin.toml"):
        m = manifest.load_file(path)
        # Only the native API clients (#1311) declare `[api]`; everything else is unchanged.
        assert (m.api is not None) is (m.runtime == "api")
        assert m.runtime in {"pty", "chat"} or m.id in {"codex-api", "claude-api", "opencode-api"}


@pytest.mark.parametrize(
    "case", ["missing", "retiring", "terminal", "chat", "chain", "cycle", "incompatible"]
)
def test_source_references_fail_closed(case, tmp_path):
    target = _provider(_api(), tmp_path)
    source = _provider(_doc("codex"), tmp_path)
    by_id = {"codex": source}
    if case == "missing":
        by_id.clear()
    elif case == "retiring":
        source.retiring = True
    elif case == "terminal":
        source.manifest = dataclasses.replace(
            source.manifest, identity=dataclasses.replace(source.manifest.identity, kind="terminal")
        )
    elif case == "chat":
        source.manifest = dataclasses.replace(source.manifest, runtime="chat")
    elif case in {"chain", "cycle"}:
        source.manifest = dataclasses.replace(
            source.manifest,
            runtime="api",
            api=manifest.Api("codex-app-server", "native-api" if case == "cycle" else "third"),
        )
        by_id["native-api"] = target
    else:
        source.manifest = dataclasses.replace(source.manifest, transcript_kind="claude-jsonl")
    with pytest.raises(api_source.SourceError):
        api_source.validate_provider(target, by_id)


def test_source_protocol_is_selected_by_shape_not_engine_id(tmp_path):
    source_doc = _doc("codex")
    source_doc["identity"]["id"] = "eighth-console"
    source_doc["binary"]["aliases"] = [source_doc["binary"]["name"]]
    source = _provider(source_doc, tmp_path)
    target = _provider(_api(source=source.engine_id), tmp_path)
    assert api_source.validate_provider(target, {source.engine_id: source}) is source


def _roster(target, source, *, generation=7):
    return SimpleNamespace(
        generation=generation,
        by_id={p.engine_id: p for p in (target, source) if p is not None},
    )


def test_source_binding_checks_both_in_one_fresh_snapshot(monkeypatch, tmp_path):
    target = _provider(_api(), tmp_path)
    source = _provider(_doc("codex"), tmp_path)
    old = _roster(target, source)
    binding = api_source.resolve(target, roster=old)
    assert (binding.provider, binding.source, binding.generation) == (target, source, 7)
    calls = []
    live = old

    @contextlib.contextmanager
    def fresh(**kwargs):
        calls.append(kwargs)
        yield live

    monkeypatch.setattr(registry, "snapshot_scope", fresh)
    assert api_source.admits(binding)
    assert calls == [{"fresh": True, "require_current": True}]
    # An unrelated generation may rebuild equal wrappers; only changed bindings revoke.
    live = _roster(_provider(_api(), tmp_path), _provider(_doc("codex"), tmp_path), generation=8)
    assert api_source.admits(binding)
    for incomplete in (_roster(None, source), _roster(target, None)):
        live = incomplete
        assert not api_source.admits(binding)
    changed_source = _provider(_doc("codex"), tmp_path / "replacement")
    live = _roster(target, changed_source)
    assert not api_source.admits(binding)
    changed_target = _provider(_api(), tmp_path / "replacement")
    live = _roster(changed_target, source)
    assert not api_source.admits(binding)


@pytest.mark.parametrize("changed", ["api", "source"])
def test_source_binding_detects_same_provider_install_record_replacement(
    changed, monkeypatch, tmp_path
):
    target = _provider(_api(), tmp_path)
    source = _provider(_doc("codex"), tmp_path)
    roster = _roster(target, source)
    binding = api_source.resolve(target, roster=roster)
    prov = target if changed == "api" else source
    prov._record = provenance.Record(install_entrypoint="different")

    @contextlib.contextmanager
    def fresh(**kwargs):
        yield roster

    monkeypatch.setattr(registry, "snapshot_scope", fresh)
    assert not api_source.admits(binding)


def test_source_binding_refuses_stale_target_and_unreadable_reload(monkeypatch, tmp_path):
    target = _provider(_api(), tmp_path)
    source = _provider(_doc("codex"), tmp_path)
    with pytest.raises(api_source.SourceError, match="no longer active"):
        api_source.resolve(target, roster=_roster(None, source))
    binding = api_source.resolve(target, roster=_roster(target, source))

    @contextlib.contextmanager
    def broken(**kwargs):
        raise OSError("unreadable roster")
        yield  # pragma: no cover

    monkeypatch.setattr(registry, "snapshot_scope", broken)
    assert not api_source.admits(binding)


@pytest.mark.parametrize("source", ["codex", "missing-source", "apichat"])
def test_registry_validates_sources_after_all_store_kinds(monkeypatch, tmp_path, source):
    fp = tmp_path / "plugins"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    path = fp / "native-api"
    path.mkdir()
    (path / "plugin.json").write_text(json.dumps(_api(source=source)))
    monkeypatch.setattr(registry, "LOAD_PROBLEMS", {})
    roster = {p.engine_id: p for p in registry._build_roster(fp)}
    assert "codex" in roster and "apichat" in roster
    assert ("native-api" in roster) is (source == "codex")
    assert ("native-api" in registry.LOAD_PROBLEMS) is (source != "codex")


def test_local_manifest_cannot_install_a_native_api_client():
    # #1311: only the operator-signed catalog (or the in-tree roster) may add one.
    with pytest.raises(feed.FeedError, match="only be installed from the signed catalog"):
        feed.entry({"manifest": _api(), "recipe": {"artifacts": []}}, signed=False)
