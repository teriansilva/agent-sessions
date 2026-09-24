"""#853 P3 — adding or removing an agent is one manifest, across the whole server.

Two independent guarantees, each with a negative control that proves it can fail:

* **The ratchet** (static): no engine-id string literal in ``src/agent_sessions`` outside the
  places an engine is legitimately named — its manifest, the store KIND shaped by its store, the
  fixed ``RESERVED_IDS`` set, and lines marked ``# kind-data`` (a vendor's own record type that
  happens to spell an engine name). It is an accident guard, like the prompt ratchet, not a
  security boundary: it catches the ``engine == "<id>"`` / ``{"<id>": …}`` shape every roster
  regression so far has had.
* **Conformance** (behavioural): a fixture EIGHTH engine, loaded through the loader's test seam
  (never a runtime path), is picked up by every consumer through the kinds its manifest selects —
  and dropped by every one of them when its manifest is removed. A consumer that keeps its own list,
  or that is skipped, turns it red.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    agent_usage,
    discover,
    engines,
    handoff,
    headless_seed,
    menu_answer,
    opencode_admission,
    prefs,
    screen_menus,
    scrollback,
    transcript,
    transcript_owner,
)
from agent_sessions.engines import base, registry
from agent_sessions.main import create_app
from agent_sessions.plugins import FIRST_PARTY_DIR, kinds

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_sessions"

# --- the ratchet ---------------------------------------------------------------------------------

#: Where an engine id may be spelled as a string. Each entry is ENGINE-SHAPED code by design (a
#: store kind reads one vendor's store), or the one fixed set that must not follow the roster.
ALLOWED = {
    "plugins/kinds.py",  # RESERVED_IDS: deliberately fixed, so a missing manifest frees no id
    "engines/claude.py",
    "engines/opencode.py",
    "engines/codex.py",
    "engines/gemini.py",
    "engines/antigravity.py",
    "engines/kimi.py",
    "engines/shell.py",
    "scanner.py",  # the `claude-projects` store kind's reader
    "archive.py",  # the `claude-projects` store kind's archive move
}
PRAGMA = "# kind-data"


def engine_literals(source: str, ids: frozenset[str]) -> list[tuple[int, str]]:
    """``(line, text)`` for every string constant equal to an engine id that is not a docstring
    and not on a ``# kind-data`` line."""
    tree = ast.parse(source)
    lines = source.splitlines()
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in ids
            and id(node) not in docstrings
            and PRAGMA not in lines[node.lineno - 1]
        ):
            out.append((node.lineno, lines[node.lineno - 1].strip()))
    return out


def test_no_engine_id_literal_outside_the_manifests_and_kinds():
    ids = frozenset(kinds.RESERVED_IDS) | frozenset(engines.engine_ids())
    found = []
    for f in sorted(SRC.rglob("*.py")):
        rel = f.relative_to(SRC).as_posix()
        if rel in ALLOWED or rel.startswith("plugins/first_party/"):
            continue
        found += [f"{rel}:{n}: {t}" for n, t in engine_literals(f.read_text(), ids)]
    assert not found, (
        "an engine is named in code instead of asked of its manifest (#853 P3):\n"
        + "\n".join(found)
    )


def test_every_allowlisted_file_exists():
    """An allowlist entry for a file that no longer exists would silently allow its replacement."""
    assert [a for a in ALLOWED if not (SRC / a).is_file()] == []


@pytest.mark.parametrize(
    "planted",
    [
        'if prov.engine_id == "claude":\n    pass\n',
        'LABELS = {"kimi": "Kimi"}\n',
        'WIPES = frozenset({"codex"})\n',
        'register_adapter("opencode", f)\n',
    ],
)
def test_NEGATIVE_CONTROL_a_planted_literal_turns_the_ratchet_red(planted):
    assert engine_literals(planted, frozenset(kinds.RESERVED_IDS)), planted


def test_the_pragma_and_docstrings_are_the_only_exemptions():
    src = '"""claude is fine in a docstring."""\nx = "gemini"  # kind-data: a record type\n'
    assert engine_literals(src, frozenset(kinds.RESERVED_IDS)) == []


# --- conformance: a fixture eighth engine ---------------------------------------------------------

ZETA = "zeta"
ZETA_ID = "zt_0123abcd"
ZETA_MANIFEST = """\
contract = 1

[identity]
id = "zeta"
label = "Zeta Agent"
publisher = "test"
version = "1"

[binary]
name = "zeta"
env_var = "AGENT_SESSIONS_ZETA_BIN"
search_paths = ["~/.zeta/bin"]

[session_id]
pattern = "^zt_[0-9a-f]{8}$"
mint = "adopt"

[store]
root = "~/.zeta"
layout = "zeta-store"
[store.paths]
db = "zeta.db"

[launch]
resume = { kind = "flag", flag = "--resume" }
new = { kind = "bare" }
bypass = ["--yolo"]
admission = "sqlite-store-shared"

[capabilities]
resume = true
new = true
archive = true
seed_start = true
orchestrator_input = true
raw_tty = true
owns_transcript = true
# handoff_target deliberately NOT declared: a denied capability must be refused, not defaulted.

[transcript]
kind = "kimi-wire"

[usage]
source = "manual"

[terminal]
repaint = "wipe"
ready = "claude-first-paint"
menu = "claude-numbered"
menu_digit_submits = true

[display]
order = 70
name = "zeta"
badge = "zt"
accent = "teal"
"""


class ZetaKind:
    """A test-only store kind for the fixture engine: behaviour only, like every in-tree kind."""

    engine_id = ZETA
    id_pattern = None

    def scan(self):
        return []

    def archive(self, native_id):
        pass

    def unarchive(self, native_id):
        pass


def _consumers(eid: str, prefs_path: Path) -> dict[str, bool]:
    """Does each consumer SEE ``eid`` — through the kind its manifest selects, not by name?"""
    prov = registry._BY_ID.get(eid)
    key = f"{eid}:{ZETA_ID}"
    seen = {
        "registry": eid in engines.engine_ids(),
        "discover": eid in discover.engine_ids(),
        "usage_panel": eid in agent_usage.ENGINES,
        "usage_manual": eid in agent_usage.MANUAL_ONLY,
        "transcript": getattr(transcript.adapter_for(eid), "__wrapped__", None)
        is transcript._ADAPTERS["kimi-wire"],
        "repaint": scrollback._wipes_on_repaint(key),
        "ready_rule": headless_seed._rule(key) is headless_seed._PAINTED["claude-first-paint"],
        "menu_parser": screen_menus._parser_for(eid) is screen_menus._PARSERS["claude-numbered"],
        "menu_digit": menu_answer.digit_submits(eid),
        "admission": opencode_admission.admits(eid),
        "orchestrator": eid in registry.orchestrator_input_engines(),
        "transcript_owner": "zeta" in transcript_owner._owning_binaries(),
        "budgets": eid in prefs._budget_engine_ids(prefs_path),
        "seedable": prov is not None
        and handoff.seed_start_state(prov, present=True) == (True, None),
    }
    try:
        engines.parse_key(key)
        seen["parse_key"] = True
    except engines.EngineError:
        seen["parse_key"] = False
    return seen


@pytest.fixture
def fixture_roster(tmp_path, monkeypatch):
    """The in-tree manifests plus `zeta`, loaded through `reload(first_party_dir=…)` — the test
    seam. Restores the real roster afterwards, whatever the test did."""
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    (fp / ZETA).mkdir()
    (fp / ZETA / "plugin.toml").write_text(ZETA_MANIFEST)
    monkeypatch.setattr(kinds, "STORE_LAYOUTS", kinds.STORE_LAYOUTS | {"zeta-store"})
    monkeypatch.setitem(registry.STORE_KINDS, "zeta-store", ZetaKind)
    monkeypatch.setattr(ZetaKind, "id_pattern", None)
    registry.reload(first_party_dir=fp)
    yield fp
    registry.reload()


def test_the_real_roster_has_no_eighth_engine(tmp_path):
    """The baseline: before the fixture loads, no consumer knows `zeta`."""
    assert not any(_consumers(ZETA, tmp_path / "prefs.json").values())


def test_an_EIGHTH_engine_reaches_every_consumer_through_its_kinds(fixture_roster, tmp_path):
    seen = _consumers(ZETA, tmp_path / "prefs.json")
    assert [c for c, ok in seen.items() if not ok] == [], seen


def test_the_eighth_engine_launches_by_its_KIND_and_is_refused_what_it_did_not_declare(
    fixture_roster, tmp_path, monkeypatch, engine_bin
):
    b = engine_bin(ZETA)
    prov = engines.get(ZETA)
    assert prov.launch_argv(ZETA_ID, cwd="/w", bypass=True) == [b, "--resume", ZETA_ID, "--yolo"]
    # handoff_target was not declared: seedable (missions may dispatch) but never a handoff target.
    assert handoff.handoff_target_state(prov, present=True) == (False, "no seed-capable start yet")
    # a terminal-only consumer accepts it (runtime pty) …
    engines.require_pty(prov)


def test_the_eighth_engine_is_in_api_engines(fixture_roster, auth_cfg, engine_bin):
    engine_bin(ZETA)
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    rows = {e["id"]: e for e in c.get("/api/engines").json()["engines"]}
    z = rows[ZETA]
    assert z["label"] == "Zeta Agent" and z["display"]["badge"] == "zt"
    assert z["session_id"] == {"mint": "adopt"} and z["runtime"] == "pty"
    assert z["capabilities"]["handoff_target"] is False and z["supports_seed_start"] is False
    assert list(rows) == engines.engine_ids()


def test_REMOVING_the_manifest_drops_it_from_every_consumer(fixture_roster, tmp_path):
    shutil.rmtree(fixture_roster / ZETA)
    registry.reload(first_party_dir=fixture_roster)
    seen = _consumers(ZETA, tmp_path / "prefs.json")
    assert [c for c, ok in seen.items() if ok] == [], seen


def test_NEGATIVE_CONTROL_a_consumer_that_keeps_its_OWN_list_is_caught(
    fixture_roster, tmp_path, monkeypatch
):
    """A deliberately hardcoded consumer — the pre-P3 transcript registry shape — turns the
    conformance check red for exactly that consumer."""
    frozen = {e: transcript.adapter_for(e) for e in kinds.RESERVED_IDS}
    monkeypatch.setattr(transcript, "adapter_for", lambda e: frozen.get(e))
    seen = _consumers(ZETA, tmp_path / "prefs.json")
    assert [c for c, ok in seen.items() if not ok] == ["transcript"]


def test_NEGATIVE_CONTROL_a_SKIPPED_consumer_is_caught(fixture_roster, tmp_path):
    """A consumer that never hears about the reload (its listener skipped) keeps the roster it
    was built with, and the conformance check names exactly that consumer."""
    saved = list(registry._RELOAD_LISTENERS)
    registry._RELOAD_LISTENERS.clear()
    try:
        registry.reload()  # back to the seven, and agent_usage is told …
        agent_usage._on_roster_reload()
        registry.reload(first_party_dir=fixture_roster)  # … zeta again, and it is NOT told
        seen = _consumers(ZETA, tmp_path / "prefs.json")
    finally:
        registry._RELOAD_LISTENERS[:] = saved
    assert sorted(c for c, ok in seen.items() if not ok) == ["usage_manual", "usage_panel"]


# --- the seven convert on an update, with nothing for the operator to do --------------------------


def test_UPGRADE_pre_P3_state_keeps_its_meaning(tmp_path, monkeypatch):
    """What a pre-P3 install has on disk reads the same under the manifest-driven build: a bare
    legacy sidecar key still means claude, every engine-qualified key still parses to the same
    engine, a stored budget still validates, and the lock/socket name of a live master (keyed on
    `<engine>:<native>`) is unchanged."""
    from agent_sessions import metadata

    legacy = "0123abcd-0123-4567-89ab-0123456789ab"
    norm, changed = metadata._normalize_keys({legacy: {"title": "t"}, "codex:" + legacy: {}})
    assert changed and set(norm) == {f"claude:{legacy}", f"codex:{legacy}"}
    for key in (f"claude:{legacy}", f"codex:{legacy}", "opencode:ses_abc", f"shell:{legacy}"):
        prov, native = engines.parse_key(key)
        assert f"{prov.engine_id}:{native}" == key
        assert registry.physical_key(key) == key
    p = tmp_path / "prefs.json"
    p.write_text('{"agent_budgets": {"engines": {"claude": {"limit_tokens": 5}}}}')
    patch = {"engines": {"claude": {"limit_tokens": 9}}}
    assert prefs.validate_agent_budgets_patch(patch, p) is None


def test_a_budget_for_an_engine_nobody_has_is_refused_but_a_STORED_one_stays_editable(tmp_path):
    p = tmp_path / "prefs.json"
    assert "no such agent" in prefs.validate_agent_budgets_patch(
        {"engines": {"nope": {"limit_tokens": 1}}}, p
    )
    # Shell has no agent: no usage row, so no budget either.
    assert "no such agent" in prefs.validate_agent_budgets_patch(
        {"engines": {"shell": {"limit_tokens": 1}}}, p
    )
    # A removed engine's stored budget is kept inert and may still be edited or cleared.
    p.write_text('{"agent_budgets": {"engines": {"gone": {"limit_tokens": 7}}}}')
    assert prefs.validate_agent_budgets_patch({"engines": {"gone": {"limit_tokens": 0}}}, p) is None


# --- the moved tables are EQUIVALENT for the seven ------------------------------------------------


def test_every_moved_table_answers_what_the_old_one_did():
    """The pre-P3 tables, recorded verbatim, against what the manifests now answer."""
    seven = engines.engine_ids()
    assert set(seven) == set(kinds.RESERVED_IDS)
    old_wipe = {"codex", "kimi"}
    old_manual = {"kimi", "gemini"}
    old_reporters = {"claude", "antigravity", "codex", "opencode"}
    old_start_evidence = {"claude"}
    old_painted = {"claude"}
    old_menu = {"claude"}
    old_digit = {"claude"}
    old_admission = {"opencode"}
    old_owns = {"claude"}
    old_npm_global = {"codex", "gemini"}
    old_bin_name = {"antigravity": "agy", "shell": "bash"}
    old_dirs = {
        "claude": ["~/.local/bin"],
        "opencode": ["~/.opencode/bin", "~/.local/bin"],
        "codex": ["~/.codex/bin", "~/.local/bin"],
        "gemini": ["~/.local/bin"],
        "antigravity": ["~/.local/bin"],
        "kimi": ["~/.kimi-code/bin", "~/.local/bin"],
        "shell": ["/bin", "/usr/bin"],
    }
    for e in seven:
        k = f"{e}:x"
        assert scrollback._wipes_on_repaint(k) == (e in old_wipe), e
        assert (e in agent_usage.MANUAL_ONLY) == (e in old_manual), e
        assert (e in agent_usage.REPORTERS) == (e in old_reporters), e
        prov = engines.get(e)
        from agent_sessions import headless_dispatch

        assert (headless_dispatch._start_evidence_adapter(prov) is not None) == (
            e in old_start_evidence
        ), e
        assert (headless_seed._rule(k) is not headless_seed._BY_BYTES) == (e in old_painted), e
        assert (screen_menus._parser_for(e) is not None) == (e in old_menu), e
        assert menu_answer.digit_submits(e) == (e in old_digit), e
        assert opencode_admission.admits(e) == (e in old_admission), e
        assert engines.owns_transcript(e) == (e in old_owns), e
        assert discover._searches_npm_global(e) == (e in old_npm_global), e
        assert discover._bin_name(e) == old_bin_name.get(e, e), e
        assert discover._search_dirs(e) == old_dirs[e], e
        assert discover.envvar(e) == f"AGENT_SESSIONS_{old_bin_name.get(e, e).upper()}_BIN", e
    assert transcript_owner._owning_binaries() == {"claude"}
    assert opencode_admission.maintained_engine() == "opencode"
    # The usage panel lists every agent — the six it always listed — now in roster order.
    assert set(agent_usage.ENGINES) == set(seven) - {"shell"}


def test_store_locations_come_from_the_manifest_with_every_old_default_and_override(
    tmp_path, monkeypatch
):
    home = tmp_path
    for var in (
        "AGENT_SESSIONS_GEMINI_TMP_DIR",
        "AGENT_SESSIONS_ANTIGRAVITY_DIR",
        "AGENT_SESSIONS_KIMI_DIR",
        "AGENT_SESSIONS_CODEX_SESSIONS_DIR",
        "AGENT_SESSIONS_OPENCODE_DB",
        "AGENT_SESSIONS_OPENCODE_LOG",
        "AGENT_SESSIONS_SHELL_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    assert base._gemini_tmp_dir(home) == home / ".gemini" / "tmp"
    assert base._antigravity_dir(home) == home / ".gemini" / "antigravity-cli"
    assert base._kimi_dir(home) == home / ".kimi-code"
    assert base._codex_sessions_dir(home) == home / ".codex" / "sessions"
    assert base._opencode_db(home) == str(home / ".local" / "share" / "opencode" / "opencode.db")
    assert base._opencode_log(home) == home / ".local/share/opencode/log/opencode.log"
    assert base._shell_dir(home) == home / ".claude" / "shell-sessions"
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", "/elsewhere/o.db")
    monkeypatch.setenv("AGENT_SESSIONS_KIMI_DIR", "/elsewhere/kimi")
    assert base._opencode_db(home) == "/elsewhere/o.db"
    assert base._kimi_dir(home) == Path("/elsewhere/kimi")


# --- a SHARED kind reads the REQUESTING engine's store (Hermes on PR #1127) ---------------------
#
# Comparing adapter identities proves dispatch, not data: a reader kind that resolved its store by
# layout would pass it while reading the codex store for another engine. So `zetab` selects the
# codex usage and transcript kinds and the sqlite maintenance kind, with its OWN store — and each
# test reads distinguishable fixtures from the two stores.

ZETAB = "zetab"
ZETAB_MANIFEST = """\
contract = 1
maintenance = ["sqlite-vacuum"]

[identity]
id = "zetab"
label = "Zeta B"
publisher = "test"
version = "1"

[binary]
name = "zetab"
env_var = "AGENT_SESSIONS_ZETAB_BIN"
search_paths = ["~/.zetab/bin"]

[session_id]
pattern = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
mint = "adopt"

[store]
root = "~/.zetab"
layout = "zetab-store"
[store.paths]
db = "zetab.db"

[launch]
resume = { kind = "subcommand", subcommand = "resume" }
admission = "sqlite-store-shared"

[capabilities]
resume = true

[transcript]
kind = "codex-rollout"

[usage]
source = "plan"
kind = "codex-rollout-field"

[display]
order = 80
name = "zetab"
badge = "zb"
accent = "slate"
"""
SID = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"
NOW = 1_800_000_000


class ZetabKind(ZetaKind):
    engine_id = ZETAB


@pytest.fixture
def two_stores(tmp_path, monkeypatch):
    """codex and zetab, each with its own store under one isolated HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("AGENT_SESSIONS_CODEX_SESSIONS_DIR", "AGENT_SESSIONS_OPENCODE_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(home / "opencode.db"))
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    (fp / ZETAB).mkdir()
    (fp / ZETAB / "plugin.toml").write_text(ZETAB_MANIFEST)
    monkeypatch.setattr(kinds, "STORE_LAYOUTS", kinds.STORE_LAYOUTS | {"zetab-store"})
    monkeypatch.setitem(registry.STORE_KINDS, "zetab-store", ZetabKind)
    registry.reload(first_party_dir=fp)
    assert engines.get(ZETAB) is not None, "zetab did not load"
    yield home
    registry.reload()


def _rollout(root: Path, pct: float, *, native: str = SID) -> Path:
    import json

    p = root / "2026" / "09" / "24" / f"rollout-2026-09-24T00-00-00-{native}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    limits = {"primary": {"used_percent": pct, "window_minutes": 300, "resets_at": NOW + 3600}}
    rec = {
        "timestamp": "2026-09-24T00:00:00Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": limits},
    }
    p.write_text(json.dumps(rec) + "\n")
    return p


def test_a_shared_USAGE_kind_reads_each_engines_OWN_store(two_stores):
    _rollout(two_stores / ".codex" / "sessions", 17.0)
    _rollout(two_stores / ".zetab", 83.0)
    mine = agent_usage.REPORTERS[ZETAB](home=two_stores, now=NOW)
    theirs = agent_usage.REPORTERS["codex"](home=two_stores, now=NOW)
    assert (mine.engine, [w.used_pct for w in mine.windows]) == (ZETAB, [83.0])
    assert (theirs.engine, [w.used_pct for w in theirs.windows]) == ("codex", [17.0])


def test_a_shared_TRANSCRIPT_kind_locates_each_engines_OWN_file(two_stores):
    theirs = _rollout(two_stores / ".codex" / "sessions", 17.0)
    assert transcript.source_location("codex", SID, two_stores) == str(theirs)
    # The session exists only in codex's store: zetab must find NOTHING, never codex's file.
    assert transcript.source_location(ZETAB, SID, two_stores) is None
    assert transcript.growth_mark(ZETAB, SID, two_stores) is None
    mine = _rollout(two_stores / ".zetab", 83.0)
    assert transcript.source_location(ZETAB, SID, two_stores) == str(mine)
    assert transcript.source_location("codex", SID, two_stores) == str(theirs)


def _db_with_free_pages(path: Path) -> None:
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("create table t(x)")
    con.executemany("insert into t values (?)", [("x" * 1000,)] * 500)
    con.commit()
    con.execute("delete from t")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


def _free_pages(path: Path) -> int:
    import sqlite3

    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        con.close()


def test_EVERY_maintenance_target_is_reachable_with_its_own_database_and_lock(
    two_stores, monkeypatch
):
    from agent_sessions import opencode_compact

    theirs, mine = two_stores / "opencode.db", two_stores / ".zetab" / "zetab.db"
    _db_with_free_pages(theirs)
    _db_with_free_pages(mine)
    assert opencode_compact.targets() == ["opencode", ZETAB]
    assert opencode_compact.database_path(ZETAB) == mine.resolve()
    assert opencode_compact.database_path("opencode") == theirs.resolve()
    with pytest.raises(FileNotFoundError):
        opencode_compact.database_path("claude")  # not a target: never another engine's store
    # Separate admission locks: holding zetab's exclusive lock does not block opencode's.
    held = opencode_admission.acquire(engine=ZETAB, exclusive=True)
    try:
        assert opencode_admission.acquire(engine=ZETAB, exclusive=True) is None
        other = opencode_admission.acquire(engine="opencode", exclusive=True)
        assert other is not None
        other.release()
    finally:
        held.release()
    # And a compaction of zetab compacts zetab's database — and only that one.
    monkeypatch.setattr(
        opencode_compact, "holders", lambda _p, _e=None: {"pids": [], "unknown": False}
    )
    before = _free_pages(theirs)
    assert _free_pages(mine) > 0 and before > 0
    result = opencode_compact.Worker(ZETAB).run(lambda _ok: None, lambda _phase: None)
    assert result["state"] == "done" and result["vacuum"] == "done", result
    assert _free_pages(mine) == 0
    assert _free_pages(theirs) == before, "another engine's database was touched"
