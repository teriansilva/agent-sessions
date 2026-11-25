"""Every agents cut that was ever published stays readable by current code (#2321).

A published cut is signed and can't change. A stricter manifest rule that refuses one makes the
catalog mirror red and, worse, makes every upgraded host's SAVED catalog unreadable ("Saved catalog
trust evidence is unavailable"), with nothing to recover it but a new hand-signed cut.

The fixtures are the real relay bytes and signatures, verified here with the INSTALLED signers
(see tests/fixtures/agents_cut_3.PROVENANCE.md). The clock is pinned inside the cut's and its
freshness statement's windows, never read from the wall clock. "Readable" means schema
compatibility only: expiry, freshness, trust, rollback and equivocation refusals all still hold
(the negative controls below).
"""

import asyncio
import hashlib
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agent_sessions import agent_catalog
from agent_sessions.plugins import (
    feed,
    feed_client,
    freshness,
    kinds,
    manifest,
    roster_state,
    storage,
)

FIXTURES = Path(__file__).parent / "fixtures"
CUT = FIXTURES / "agents_cut_3.plugin-feed.json"
CUT_DIGEST = "a41aace5d551496d41b965e222da33d2d9dbd21e8065c7d22b1c6ec2564d6352"
STATEMENT = FIXTURES / "agents_cut_3.freshness-statement.json"
POINTER = FIXTURES / "agents_cut_3.freshness-current.json"

DATA = CUT.read_bytes()
SIG = CUT.with_name(CUT.name + ".sig").read_bytes()
DOC = json.loads(DATA)
STMT = STATEMENT.read_bytes()
STMT_SIG = STATEMENT.with_name(STATEMENT.name + ".sig").read_bytes()
SELECTED = json.loads(POINTER.read_bytes())
#: Inside both the cut's window and its freshness statement's.
NOW = json.loads(STMT)["issued_at"] + 60


def _writes(layout, m):
    return layout in kinds.WRITABLE_STORE_LAYOUTS or "sqlite-vacuum" in m.maintenance


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A host's own plugin state, empty, and a clock inside the windows."""
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(feed.time, "time", lambda: NOW)
    return tmp_path


def _accepted_by_an_older_client(monkeypatch):
    """Save cut 3 exactly as a host running pre-#2301 code did: that code had no store.read_only
    cross-check. The record holds only the signed bytes, so this is the upgrade starting point."""
    with monkeypatch.context() as old:
        old.setattr(manifest, "_cross_check", lambda m: None)
        feed.accept(DATA, SIG, statement=(STMT, STMT_SIG, SELECTED), now=NOW)


# ---- the fixtures are the published cut ---------------------------------------------------------


def test_the_fixtures_are_the_published_cut_and_its_statement():
    assert hashlib.sha256(DATA).hexdigest() == CUT_DIGEST
    assert DOC["sequence"] == 3
    stmt = json.loads(STMT)
    assert (stmt["content_sequence"], stmt["content_digest"]) == (3, CUT_DIGEST)
    assert feed.verify(DATA, SIG) == feed.PRINCIPAL  # the installed release signers


# ---- the parser ---------------------------------------------------------------------------------


@pytest.mark.parametrize("historical", [False, True], ids=["fresh", "historical"])
def test_the_published_cut_parses(historical):
    snapshot = feed.parse(DATA, now=NOW, _historical=historical)
    assert (snapshot.sequence, snapshot.digest) == (3, CUT_DIGEST)
    assert len(snapshot.entries) == len(DOC["plugins"])


def test_a_signed_cut_serves_the_true_read_only_value():
    """#1182's point survives: a store BattleLab writes is served as writable even when the signed
    manifest predates the field (default true)."""
    snapshot = feed.parse(DATA, now=NOW)
    stores = {e.manifest.store.layout: e.manifest for e in snapshot.entries if e.manifest.store}
    for layout, m in stores.items():
        assert m.store.read_only is (not _writes(layout, m)), layout
    # The two that made #2301 refuse cut 3.
    assert stores["claude-projects"].store.read_only is False
    assert stores["opencode-sqlite"].store.read_only is False
    # A store BattleLab only reads keeps read_only = true.
    assert stores["codex-rollouts"].store.read_only is True


# ---- the real upgrade path ----------------------------------------------------------------------


def test_an_upgraded_host_reads_its_saved_cut(state, monkeypatch):
    _accepted_by_an_older_client(monkeypatch)
    saved = (storage.root() / "feed.json").read_bytes()
    floor = (storage.root() / "feed-floor.json").read_bytes()

    snapshot = feed.historical()  # what agent_catalog.current() re-reads on every request
    assert (snapshot.sequence, snapshot.digest) == (3, CUT_DIGEST)
    assert feed.fresh(snapshot)  # its stored freshness statement still makes it usable

    catalog = agent_catalog.current()
    assert catalog.source != "unavailable" and catalog.error is None, catalog.error
    assert (catalog.sequence, catalog.stale) == (3, False)
    offered = {c.entry.manifest.id: c for c in catalog.choices}
    remote_ids = {e.manifest.id for e in snapshot.entries}
    assert remote_ids <= set(offered)
    usable = [c for c in catalog.choices if c.source in ("signed", "bundled")]
    assert usable, "the upgraded host can install agents again"
    for c in catalog.choices:
        m = c.entry.manifest
        if m.store is not None:
            assert m.store.read_only is (not _writes(m.store.layout, m)), m.id

    # Reading changed nothing that was saved.
    assert (storage.root() / "feed.json").read_bytes() == saved
    assert (storage.root() / "feed-floor.json").read_bytes() == floor
    assert json.loads(saved)["data"].encode() == DATA


def test_a_host_refreshing_to_the_published_cut_accepts_it(state):
    snapshot = feed.accept(DATA, SIG, statement=(STMT, STMT_SIG, SELECTED), now=NOW)
    assert (snapshot.sequence, snapshot.digest) == (3, CUT_DIGEST)
    record = json.loads((storage.root() / "feed.json").read_bytes())
    assert record["data"].encode() == DATA and record["signature"].encode() == SIG


def _mirror():
    script = Path(__file__).resolve().parents[1] / ".forgejo" / "scripts" / "catalog_mirror.py"
    spec = importlib.util.spec_from_file_location("catalog_mirror_2321", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_mirror_reads_the_published_relay_cut(state, monkeypatch):
    """The hourly `agent-catalog-mirror` job's first step, against the relay's real bytes."""
    cm = _mirror()
    base = f"{cm.RELAY}/releases/3-{CUT_DIGEST}/plugin-feed.json"
    relay = {
        cm.RELAY + "/current.json": feed.canonical({"sequence": 3, "digest": CUT_DIGEST}),
        base: DATA,
        base + ".sig": SIG,
    }

    def respond(request):
        body = relay.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(respond))

    async def read():
        async with feed_client.open_client() as client:
            return await cm.relay_cut(client)

    cut = asyncio.run(read())
    assert (cut.sequence, cut.digest, cut.data, cut.signature) == (3, CUT_DIGEST, DATA, SIG)


# ---- negative controls: compatibility is not an exemption --------------------------------------


def test_the_published_cut_still_expires():
    with pytest.raises(feed.FeedError, match="expired"):
        feed.parse(DATA, now=DOC["expires_at"] + 1)


def test_the_long_cut_still_needs_its_freshness_statement(state):
    with pytest.raises(feed.FeedError, match="freshness statement"):
        feed.accept(DATA, SIG, now=NOW)
    assert not (storage.root() / "feed.json").exists()


def test_an_untrusted_signer_is_still_refused(state, tmp_path, monkeypatch):
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    signers = tmp_path / "only-other"
    signers.write_text(feed.PRINCIPAL + " " + other.with_suffix(".pub").read_text())
    monkeypatch.setattr(feed, "SIGNERS", signers)
    with pytest.raises(feed.FeedError, match="not from a trusted"):
        feed.accept(DATA, SIG, statement=(STMT, STMT_SIG, SELECTED), now=NOW)


@pytest.fixture
def test_key(tmp_path, monkeypatch):
    """The installed release signers plus one test key, to sign controls built from cut 3's own
    entries (a short window, so they need no freshness statement)."""
    key = tmp_path / "control"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    signers = tmp_path / "signers"
    signers.write_text(
        feed.SIGNERS.read_text() + feed.PRINCIPAL + " " + key.with_suffix(".pub").read_text()
    )
    monkeypatch.setattr(feed, "SIGNERS", signers)

    def cut(sequence, **changes):
        doc = dict(DOC, sequence=sequence, issued_at=NOW - 60, expires_at=NOW + 86400, **changes)
        data = feed.canonical(doc)
        sig = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", feed.NAMESPACE],
            input=data,
            capture_output=True,
            check=True,
        ).stdout
        return data, sig

    return cut


def test_rollback_below_the_saved_cut_is_still_refused(state, monkeypatch, test_key):
    _accepted_by_an_older_client(monkeypatch)
    data, sig = test_key(2)
    with pytest.raises(feed.FeedError, match="backwards"):
        feed.accept(data, sig, now=NOW)
    assert feed.historical().sequence == 3


def test_equivocation_at_the_saved_sequence_is_still_refused(state, monkeypatch, test_key):
    _accepted_by_an_older_client(monkeypatch)
    data, sig = test_key(3)  # same sequence, different bytes
    with pytest.raises(feed.FeedError, match="different feed bytes"):
        feed.accept(data, sig, now=NOW)
    assert feed.historical().digest == CUT_DIGEST


# ---- the normalization is for signed sources only -----------------------------------------------


def _claude_manifest(read_only):
    raw = next(
        p["manifest"]
        for p in DOC["plugins"]
        if p["manifest"].get("store", {}).get("layout") == "claude-projects"
    )
    m = json.loads(json.dumps(raw))
    if read_only is None:
        m["store"].pop("read_only", None)
    else:
        m["store"]["read_only"] = read_only
    return json.dumps(m).encode()


@pytest.mark.parametrize("read_only", [None, True], ids=["omitted", "explicit-true"])
def test_a_signed_manifest_is_normalized(read_only):
    # The literal, not manifest.SIGNED_FEED, so this fails on behaviour where that name is missing.
    m = manifest.load_bytes(_claude_manifest(read_only), name="plugin.json", source="signed-feed")
    assert m.store.read_only is False


@pytest.mark.parametrize("read_only", [None, True], ids=["omitted", "explicit-true"])
def test_a_local_manifest_that_mislabels_a_writable_store_is_still_refused(read_only):
    with pytest.raises(manifest.ManifestError, match="store.read_only"):
        manifest.load_bytes(_claude_manifest(read_only), name="plugin.json", source="local")


@pytest.mark.parametrize("read_only", ["", "read_only = true\n"], ids=["omitted", "explicit-true"])
def test_a_first_party_plugin_toml_that_mislabels_a_writable_store_is_still_refused(
    tmp_path, read_only
):
    src = Path(manifest.__file__).with_name("first_party") / "claude" / "plugin.toml"
    text = src.read_text()
    assert "read_only = false" in text
    path = tmp_path / "plugin.toml"
    path.write_text(text.replace("read_only = false\n", read_only))
    with pytest.raises(manifest.ManifestError, match="store.read_only"):
        manifest.load_file(path)


def test_the_first_party_claude_manifest_loads_as_shipped():
    """Control for the test above: the in-tree file itself is valid."""
    src = Path(manifest.__file__).with_name("first_party") / "claude" / "plugin.toml"
    assert manifest.load_file(src).store.read_only is False


def test_freshness_module_is_the_one_acceptance_uses():
    """Guards the fixture: the statement is checked by the same module and signers as hosts."""
    stmt = freshness.checked(STMT, STMT_SIG, feed_name="agents", selected=SELECTED, now=NOW)
    assert stmt is not None


# ---- a retiring engine's recorded copy (independent review on #2322) ----------------------------


@pytest.mark.parametrize("index", range(len(DOC["plugins"])))
def test_a_retiring_engine_installed_from_the_cut_still_parses_its_recorded_copy(index):
    """A managed agent installed from cut 3 records the signed manifest bytes as its copy
    (`manager.py`: `manifest_copy = ("plugin.json", entry.manifest_bytes)`). When it retires,
    `roster_state.parse_copy` re-reads them; refusing them would mark the engine "agent removed"
    and strand its live sessions (the attach-until-exit rule)."""
    raw = feed.canonical(DOC["plugins"][index]["manifest"])
    eid = DOC["plugins"][index]["manifest"]["identity"]["id"]
    m = roster_state.parse_copy(eid, "plugin.json", raw)
    if m.store is not None:
        assert m.store.read_only is (not _writes(m.store.layout, m))


def _build_catalog_module(root):
    script = Path(__file__).resolve().parents[1] / "scripts" / "build-agent-catalog"
    loader = importlib.machinery.SourceFileLoader("build_agent_catalog_2321", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.ROOT = root
    return module


def test_the_catalog_builder_still_refuses_a_mislabelled_recipe(tmp_path):
    """A recipe entering a NEW cut is validated strictly, so the leniency for published cuts can
    never bake a mislabelled store into the next one."""
    import shutil

    repo = Path(__file__).resolve().parents[1]
    for part in ("release/recipes/linux-x64", "src/agent_sessions/plugins/first_party"):
        shutil.copytree(repo / part, tmp_path / part)
    builder = _build_catalog_module(tmp_path)
    builder.build()  # the shipped recipes build
    recipe = tmp_path / "release/recipes/linux-x64/claude.json"
    doc = json.loads(recipe.read_text())
    doc["manifest"]["store"]["read_only"] = True
    recipe.write_text(json.dumps(doc))
    with pytest.raises(manifest.ManifestError, match="store.read_only"):
        builder.build()


def _build_cut(tmp_path, plugins):
    directory = tmp_path / "entries"
    directory.mkdir()
    for i, p in enumerate(plugins):
        (directory / f"{i:02d}.json").write_text(json.dumps(p))
    script = Path(__file__).resolve().parents[1] / "scripts" / "build-plugin-feed"
    argv = [
        sys.executable,
        str(script),
        str(directory),
        str(tmp_path / "cut"),
        "--sequence",
        "4",
        "--issued-at",
        str(NOW - 60),
        "--valid-for",
        "86460",
    ]
    return subprocess.run(argv, capture_output=True, text=True)


def test_the_signed_cut_builder_refuses_a_new_cut_with_a_mislabelled_store(tmp_path):
    """Cut 3's own entries are read leniently as a PUBLISHED cut, but building a NEW cut from them
    is refused until the writable stores declare `read_only = false` (#2321)."""
    result = _build_cut(tmp_path, DOC["plugins"])
    assert result.returncode != 0
    assert "store.read_only" in result.stderr
    assert not (tmp_path / "cut" / "plugin-feed.json").exists()


def test_the_signed_cut_builder_accepts_the_same_entries_once_declared_correctly(tmp_path):
    fixed = json.loads(json.dumps(DOC["plugins"]))
    for p in fixed:
        store = p["manifest"].get("store")
        if store and store["layout"] in ("claude-projects", "opencode-sqlite"):
            store["read_only"] = False
    result = _build_cut(tmp_path, fixed)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cut" / "plugin-feed.json").is_file()
