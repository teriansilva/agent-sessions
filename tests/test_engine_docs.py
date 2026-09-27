"""The engine lists in the docs are generated from the first-party manifests (#853 P8).

`scripts/gen-engine-docs` rewrites every `<!-- BEGIN generated:<name> -->` block from the roster
and the kinds. These tests hold the committed docs to it, so adding or removing a manifest (or a
kind) without regenerating turns the build red instead of leaving a page that lies — the hand-kept
tables it replaced had each drifted on their own.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
from pathlib import Path

import pytest

from agent_sessions.plugins import FIRST_PARTY_DIR, kinds, load_first_party

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "scripts" / "gen-engine-docs"


def _load_gen():
    loader = importlib.machinery.SourceFileLoader("gen_engine_docs", str(_SCRIPT))
    spec = importlib.util.spec_from_loader("gen_engine_docs", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def gen():
    return _load_gen()


def _copy_targets(gen, dst: Path) -> None:
    for rel in gen.TARGETS:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dst / rel)


def _snapshot(gen, root: Path) -> dict[str, str]:
    return {rel: (root / rel).read_text(encoding="utf-8") for rel in gen.TARGETS}


def _roster_from(gen, monkeypatch, first_party: Path) -> None:
    real = load_first_party
    monkeypatch.setattr(
        gen, "load_first_party", lambda **kw: real(first_party_dir=first_party, **kw)
    )


def test_committed_docs_match_the_manifests(gen):
    assert gen.stale() == [], "run: python3 scripts/gen-engine-docs"


def test_regenerating_twice_changes_nothing(gen, tmp_path, monkeypatch):
    _copy_targets(gen, tmp_path)
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert gen.main([]) == 0
    first = _snapshot(gen, tmp_path)
    assert gen.main([]) == 0
    assert _snapshot(gen, tmp_path) == first


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(
            lambda t: t.replace("<!-- BEGIN generated:engine-table -->\n", "", 1).replace(
                "<!-- END generated:engine-table -->", "", 1
            ),
            id="deleted-block",
        ),
        pytest.param(
            lambda t: t.replace("<!-- END generated:engine-table -->", "", 1), id="unpaired"
        ),
        pytest.param(
            lambda t: t.replace("generated:engine-table -->", "generated:engine-tabel -->"),
            id="renamed",
        ),
        pytest.param(
            lambda t: t
            + "\n<!-- BEGIN generated:engine-table -->\n<!-- END generated:engine-table -->\n",
            id="duplicated",
        ),
    ],
)
def test_a_missing_or_malformed_block_fails_closed(gen, tmp_path, monkeypatch, capsys, mangle):
    # A file that lost its markers would otherwise compare equal to itself and pass as fresh.
    _copy_targets(gen, tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(mangle(readme.read_text(encoding="utf-8")), encoding="utf-8")
    # Make every OTHER target stale too, so "writes nothing" is observable across the inventory.
    engines = tmp_path / "docs/site/guide/engines.md"
    engines.write_text(
        engines.read_text(encoding="utf-8").replace("`codex resume <id>`", "`x`"), encoding="utf-8"
    )
    before = _snapshot(gen, tmp_path)
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert gen.main(["--check"]) == 1
    assert "README.md" in capsys.readouterr().err
    assert gen.main([]) == 1  # regenerate refuses too, and writes nothing anywhere
    assert _snapshot(gen, tmp_path) == before
    with pytest.raises(SystemExit):
        gen.stale(tmp_path)


def test_a_broken_manifest_stops_generation(gen, tmp_path, monkeypatch):
    # The loader is fail-soft; regenerating from the six that loaded would bless a truncated table.
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    p = fp / "codex" / "plugin.toml"
    p.write_text(p.read_text(encoding="utf-8").replace("contract = 1", "contract = 99"))
    _roster_from(gen, monkeypatch, fp)
    with pytest.raises(SystemExit, match="did not load"):
        gen.engine_table()
    _copy_targets(gen, tmp_path)
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    before = _snapshot(gen, tmp_path)
    assert gen.main([]) == 1
    assert _snapshot(gen, tmp_path) == before


def test_an_empty_roster_stops_generation(gen, tmp_path, monkeypatch):
    (tmp_path / "first_party").mkdir()
    _roster_from(gen, monkeypatch, tmp_path / "first_party")
    with pytest.raises(SystemExit, match="no first-party manifests"):
        gen.engine_table()


def test_a_new_manifest_makes_the_docs_stale(gen, tmp_path, monkeypatch):
    # Negative control: prove the check is reached by the roster, not only by edited text.
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    codex = (fp / "codex" / "plugin.toml").read_text(encoding="utf-8")
    (fp / "zeta").mkdir()
    (fp / "zeta" / "plugin.toml").write_text(
        codex.replace('id = "codex"', 'id = "zeta"')
        .replace('label = "Codex"', 'label = "Zeta Agent"')
        .replace('name = "codex"', 'name = "zeta"')
        .replace("AGENT_SESSIONS_CODEX_BIN", "AGENT_SESSIONS_ZETA_BIN")
        .replace("AGENT_SESSIONS_CODEX_SESSIONS_DIR", "AGENT_SESSIONS_ZETA_DIR")
        .replace('badge = "cx"', 'badge = "zt"')
        .replace("order = 30", "order = 90"),
        encoding="utf-8",
    )
    _roster_from(gen, monkeypatch, fp)
    table = gen.engine_table()
    assert "**Zeta Agent** (`zeta`)" in table
    assert "`zeta resume <id>`" in table  # assembled by its launch kind, not copied
    assert "`AGENT_SESSIONS_ZETA_BIN`" in gen.engine_env()
    assert "Zeta Agent" in gen.engine_stores()

    _copy_targets(gen, tmp_path)
    stale = gen.stale(tmp_path)
    assert {"README.md", "docs/site/guide/engines.md", "docs/site/start/uninstall.md"} <= set(stale)


def test_a_removed_manifest_makes_the_docs_stale(gen, tmp_path, monkeypatch):
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    shutil.rmtree(fp / "kimi")
    _roster_from(gen, monkeypatch, fp)
    assert "`kimi`" not in gen.engine_table()
    _copy_targets(gen, tmp_path)
    assert "README.md" in gen.stale(tmp_path)


def test_check_mode_fails_on_a_stale_block(gen, tmp_path, monkeypatch, capsys):
    _copy_targets(gen, tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("`codex resume <id>`", "`codex --old <id>`"),
        encoding="utf-8",
    )
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert gen.main(["--check"]) == 1
    assert "README.md" in capsys.readouterr().err
    assert gen.main([]) == 0  # regenerate
    assert gen.main(["--check"]) == 0
    assert "`codex resume <id>`" in readme.read_text(encoding="utf-8")


def test_an_unknown_block_name_is_refused(gen):
    # render() alone, below the inventory check: a block with no renderer is never passed through.
    with pytest.raises(SystemExit):
        gen.render("<!-- BEGIN generated:nope -->\n<!-- END generated:nope -->", {})


def test_argv_matches_the_launcher(gen):
    # The documented command is the launcher's own assembly with the path swapped for the name.
    result = load_first_party(env={}, home=Path("/nonexistent-home"))
    for prov in result.providers.values():
        m = prov.manifest
        if m.launch.resume.kind == "fresh":
            continue
        prov._entry_path = lambda m=m: m.binary.name
        argv = prov._assemble(m.launch.resume, "<id>", cwd="/__dir__", bypass=False, new=False)
        documented = " ".join("<dir>" if a == "/__dir__" else a for a in argv)
        assert f"`{documented}`" in gen.engine_table(), m.id


_NOT_MANIFEST_VALUES = {
    # Guard lists and internal subsets, not values a manifest chooses.
    "FORBIDDEN_ENTRYPOINTS",
    "RESERVED_IDS",
    "AGENT_ONLY_CAPABILITIES",
    "_CLI_PROBE_KINDS",
}


def test_the_vocab_table_lists_every_kind(gen):
    # A new closed set (or a new member) in kinds.py must reach the "Allowed values" table.
    vocab = gen.manifest_vocab()
    checked = 0
    for name in dir(kinds):
        if name in _NOT_MANIFEST_VALUES:
            continue
        value = getattr(kinds, name)
        members: set[str] = set()
        if isinstance(value, frozenset):
            members = {v for v in value if isinstance(v, str)}
        elif isinstance(value, tuple) and value and all(isinstance(v, str) for v in value):
            members = set(value)
        elif isinstance(value, dict) and name.isupper():
            members = set(value)
            for v in value.values():
                members |= set(v)
        if not members or name.startswith("FORBIDDEN_ENTRYPOINT"):
            continue
        for v in members:
            assert f"`{v}`" in vocab, f"kinds.{name} member {v!r} missing from the vocab table"
        checked += 1
    assert checked >= 20
