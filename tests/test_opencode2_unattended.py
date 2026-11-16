"""opencode 2.x's unattended-launch capabilities (#2083: the #989 contract for `opencode2`).

As for 1.x (`test_opencode_unattended.py`), nearly every test is a REFUSAL that must happen: a start
check that says `found` when it should not types a brief into whatever screen is in front of it, and
one that says `absent` when it means "could not look" reports a running agent as never started.

The log lines are the real 2.2.7 shapes, measured on 2026-11-16 under a scratch XDG tree (paths
replaced). The binary's own value formatter writes a value bare when it matches `^[^\\s="\\\\]+$`
and as a JSON string otherwise; `_fmt` below is that rule.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from agent_sessions import engines, launch_binding, transcript
from agent_sessions import start_evidence as se
from agent_sessions.engines import base, opencode2
from agent_sessions.engines import opencode as oc

SCHEMA = Path(__file__).parent / "fixtures" / "opencode2" / "schema-2.2.7.sql"
ENGINE = "opencode2"
NONCE = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
OURS = "ses_e1ab595b0ffeWpFcQMj6DLSyzK"
OTHER = "ses_e1ab0f7e3ffe9RU6Oonpavh4IZ"
THIRD = "ses_e1aad54a1ffeh6u8hZdVYOqKHo"


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(tmp_path / "o2root"))


@pytest.fixture(autouse=True)
def master_alive(monkeypatch):
    """This launch's own dtach master as `start_evidence` probes it: alive unless a test says."""
    state = {"value": "alive"}
    monkeypatch.setattr(opencode2, "_launch_master_state", lambda launch: state["value"])
    return state


def prov():
    return engines.get(ENGINE)


@pytest.fixture
def log() -> Path:
    p = Path(base._opencode2_log())
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def cwd(tmp_path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def _stamp(epoch: float) -> str:
    return (
        datetime.datetime.fromtimestamp(epoch, datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _fmt(value: str) -> str:
    """2.2.7's value formatter (`se()` in the bundle): bare when safe, else a JSON string."""
    import re

    return value if re.fullmatch(r'[^\s="\\]+', value) else json.dumps(value)


def _boot(epoch: float, directory: str, run: str = "5e8e90f6") -> str:
    """`location services booted`, verbatim 2.2.7 field order: `run` BEFORE `message`, and
    `directory` followed by more fields."""
    return (
        f"timestamp={_stamp(epoch)} level=INFO run={run} "
        f'message="location services booted" directory={_fmt(directory)} '
        "workspaceID=undefined durationMs=131 http.span=136 role=server"
    )


def _cli(epoch: float, run: str = "315ce987") -> str:
    return (
        f"timestamp={_stamp(epoch)} level=INFO run={run} "
        'message="cli starting" version=2.2.7 channel=latest local=false '
        'args="[\\"--standalone\\",\\".\\",\\"--auto\\"]" role=cli'
    )


def _event(epoch: float, directory: str) -> str:
    """A real record that carries the cwd under ANOTHER key — it must never count."""
    return (
        f"timestamp={_stamp(epoch)} level=INFO run=5e8e90f6 message=event "
        f"event.id=evt_1e54eef3f001fakB3TfcL4Dy45 event.type=provider.updated "
        f"event.location.directory={_fmt(directory)} event.location.workspaceID=undefined"
    )


def _watch(epoch: float, path: str) -> str:
    return (
        f"timestamp={_stamp(epoch)} level=INFO run=5e8e90f6 "
        f'message="watcher subscribe" path={_fmt(path)} type=directory ignores=6 role=server'
    )


def _write(log: Path, *lines: str) -> None:
    log.write_text("".join(x + "\n" for x in lines))


def _launch(cwd, *, at: float, snapshot=(), nonce: str = NONCE) -> base.LaunchContext:
    return base.LaunchContext(
        engine=ENGINE,
        key=f"{ENGINE}:new-11111111-2222-3333-4444-555555555555",
        native="new-11111111-2222-3333-4444-555555555555",
        cwd=str(cwd),
        nonce=nonce,
        snapshot=frozenset(snapshot),
        launched_at=at,
    )


def _evidence(cwd, at):
    return prov().start_evidence(_launch(cwd, at=at))


# --- the manifest -------------------------------------------------------------------------------


def test_the_manifest_declares_its_own_log_and_the_measured_capabilities():
    m = prov().manifest
    assert m.start_evidence == "opencode2-log"
    assert m.store.paths["log"] == "data/opencode/log/opencode.log"
    for cap in ("seed_start", "orchestrator_input", "handoff_target"):
        assert m.can(cap), cap
    # Store maintenance is NOT decided (#1986): the read-only invariant stands, nothing admitted.
    assert m.maintenance == () or not m.maintenance


def test_the_opencode2_log_kind_needs_a_log_path():
    from agent_sessions.plugins import kinds
    from agent_sessions.plugins.manifest import ManifestError, _cross_check

    assert "opencode2-log" in kinds.START_EVIDENCE_KINDS
    import dataclasses

    m = prov().manifest
    store = dataclasses.replace(m.store, paths={"db": m.store.paths["db"]})
    with pytest.raises(ManifestError, match="opencode2-log needs store.paths.log"):
        _cross_check(dataclasses.replace(m, store=store))


def test_opencode2_is_offerable_for_an_unattended_dispatch():
    p = prov()
    assert engines.unattended_start_state(p) == (True, None)
    for capability in engines.LATE_ID_CAPABILITIES:
        assert callable(getattr(p, capability, None)), capability


# --- the tokenizer ------------------------------------------------------------------------------


def test_fields_are_read_by_name_whatever_their_order():
    f = opencode2._log2_fields(_boot(1.0, "/a/b"))
    assert f["message"] == "location services booted" and f["directory"] == "/a/b"
    assert f["run"] == "5e8e90f6" and f["role"] == "server"
    # `event.location.directory` is a different key, never `directory`.
    assert "directory" not in opencode2._log2_fields(_event(1.0, "/a/b"))


@pytest.mark.parametrize(
    "line",
    [
        'timestamp=x message="unterminated',
        'timestamp=x message="bad \\q escape"',
        "timestamp=x  level=INFO",  # two spaces
        "timestamp=x level=INFO ",  # trailing space
        "timestamp=x level",  # no `=`
        "timestamp=x level=a=b",  # a bare value never holds `=`
        "timestamp=x level=INFO level=WARN",  # one key twice
        'timestamp=x message="a"b',
    ],
)
def test_a_line_that_is_not_exactly_key_value_pairs_is_refused(line):
    assert opencode2._log2_fields(line) is None


# --- start_evidence -----------------------------------------------------------------------------


def test_a_boot_in_our_cwd_after_our_launch_is_found(log, cwd):
    t = time.time()
    _write(log, _cli(t - 30), _cli(t + 0.4), _boot(t + 1.1, str(cwd)), _watch(t + 1.2, str(cwd)))
    state, why = _evidence(cwd, t)
    assert state == se.FOUND, why


def test_nothing_yet_is_absent(log, cwd):
    t = time.time()
    _write(log, _cli(t - 30), _cli(t + 0.4))
    assert _evidence(cwd, t)[0] == se.ABSENT


def test_a_missing_log_is_absent(log, cwd):
    assert not log.exists()
    assert _evidence(cwd, time.time())[0] == se.ABSENT


def test_the_cwd_under_another_key_is_not_a_boot(log, cwd):
    t = time.time()
    _write(log, _cli(t - 30), _event(t + 1, str(cwd)), _watch(t + 1, str(cwd)))
    assert _evidence(cwd, t)[0] == se.ABSENT


def test_a_boot_in_ANOTHER_directory_is_not_ours(log, cwd, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, str(other)))
    assert _evidence(cwd, t)[0] == se.ABSENT


def test_a_boot_BEFORE_the_launch_is_not_ours(log, cwd):
    t = time.time()
    _write(log, _cli(t - 600), _boot(t - 599, str(cwd)))
    assert _evidence(cwd, t)[0] == se.ABSENT


def test_the_preflights_own_boot_moments_before_the_launch_is_not_counted(log, cwd):
    """The preflight (`models --standalone`) boots a private server in the SAME directory and
    exits before `launched_at` is taken. 1.x's 2 s backward tolerance would count that record next
    to the agent's own and call the launch ambiguous for ever; the 2.x floor has none."""
    t = time.time()
    _write(log, _cli(t - 2.5), _boot(t - 0.3, str(cwd), run="95a1d178"), _boot(t + 1.1, str(cwd)))
    state, why = _evidence(cwd, t)
    assert state == se.FOUND, why


def test_two_boots_in_our_cwd_are_UNREADABLE(log, cwd):
    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, str(cwd)), _boot(t + 2, str(cwd), run="a8edde07"))
    state, why = _evidence(cwd, t)
    assert state == se.UNREADABLE and "2 opencode 2 servers" in why


@pytest.mark.parametrize("master", ["dead", "unknown"])
def test_our_boot_with_our_master_not_alive_is_UNREADABLE(log, cwd, master_alive, master):
    master_alive["value"] = master
    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, str(cwd)))
    assert _evidence(cwd, t)[0] == se.UNREADABLE


@pytest.mark.parametrize(
    "name", ["with space", 'quo"te', "back\\slash", "eq=sign x", "tab\tin", "ümlaut"]
)
def test_a_directory_is_parsed_whole_however_2x_quoted_it(log, tmp_path, name):
    """Measured: a space, quote, backslash, `=` or tab makes 2.2.7 JSON-quote the value."""
    d = tmp_path / name
    d.mkdir()
    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, str(d)))
    assert _evidence(d, t)[0] == se.FOUND
    # …and the same directory with its name cut at the space is not a match.
    if " " in name:
        _write(log, _cli(t - 30), _boot(t + 1, str(d).split(" ")[0]))
        assert _evidence(d, t)[0] == se.ABSENT


def test_a_symlinked_cwd_still_matches(log, tmp_path, cwd):
    link = tmp_path / "link"
    link.symlink_to(cwd)
    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, os.path.realpath(cwd)))
    assert _evidence(link, t)[0] == se.FOUND


def test_a_malformed_complete_line_is_UNREADABLE(log, cwd):
    t = time.time()
    _write(log, _cli(t - 30), 'timestamp=x message="half', _boot(t + 1, str(cwd)))
    state, why = _evidence(cwd, t)
    assert state == se.UNREADABLE and "parsed" in why


def test_a_line_still_being_written_is_waited_for_not_refused(log, cwd):
    t = time.time()
    log.write_text(_cli(t - 30) + "\n" + _boot(t + 1, str(cwd)) + "\n" + _boot(t + 2, "/x")[:40])
    assert _evidence(cwd, t)[0] == se.FOUND


def test_a_boot_record_without_a_directory_is_UNREADABLE(log, cwd):
    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, str(cwd)).replace(f"directory={_fmt(str(cwd))} ", ""))
    assert _evidence(cwd, t)[0] == se.UNREADABLE


def test_a_log_that_begins_after_the_launch_mid_stream_is_UNREADABLE(log, cwd):
    """Truncated or replaced: everything older than the launch is gone and the file starts on an
    arbitrary record, so this launch's line may have gone with it."""
    t = time.time()
    _write(log, _watch(t + 0.5, "/etc"), _boot(t + 1, str(cwd)))
    state, why = _evidence(cwd, t)
    assert state == se.UNREADABLE and "truncated or replaced" in why


def test_a_FRESH_log_that_begins_after_the_launch_is_read(log, cwd):
    """The first 2.x process ever started on this store writes the file's first record."""
    t = time.time()
    _write(log, _cli(t + 0.4), _boot(t + 1, str(cwd)))
    assert _evidence(cwd, t)[0] == se.FOUND


@pytest.mark.parametrize("how", ["truncated", "replaced"])
def test_a_log_truncated_or_replaced_while_it_is_read_is_UNREADABLE(log, cwd, monkeypatch, how):
    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, str(cwd)))
    real_open = Path.open
    fired = []

    def racing_open(self, *a, **k):
        if self == log and not fired:
            fired.append(1)  # once: the race lands between the reader's stat and its open
            if how == "truncated":
                os.truncate(log, 0)
            else:
                fresh = log.with_name("fresh.log")
                _write(fresh, _cli(t - 30), _boot(t + 1, str(cwd)))
                os.replace(fresh, log)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", racing_open)
    state, why = _evidence(cwd, t)
    assert state == se.UNREADABLE and "truncated or replaced" in why


class _HookedFile:
    """A file proxy whose `read` runs a hook right after the bytes are taken (C-level
    `BufferedReader.read` can't be patched), to land a change DURING the reader's read."""

    def __init__(self, fh, hook):
        self._fh, self._hook = fh, hook

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def fileno(self):
        return self._fh.fileno()

    def seek(self, *a):
        return self._fh.seek(*a)

    def read(self, *a):
        data = self._fh.read(*a)
        self._hook()
        return data


def _open_with_hook_after_read(monkeypatch, log, hook):
    real_open = Path.open

    def hooked(self, *a, **k):
        fh = real_open(self, *a, **k)
        return _HookedFile(fh, hook) if self == log else fh

    monkeypatch.setattr(Path, "open", hooked)


@pytest.mark.parametrize("how", ["truncated", "replaced", "removed"])
@pytest.mark.parametrize("when", ["after_check", "during_read"])
def test_a_log_changed_after_the_pre_read_check_is_UNREADABLE(log, cwd, monkeypatch, how, when):
    """#2089 review: the change lands AFTER the reader's own descriptor check (or while it reads),
    not between stat and open. A truncation must not read as `absent`, and a replaced pathname must
    never let the old, unlinked descriptor answer `found`."""
    from agent_sessions.engines import opencode as oc1

    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, str(cwd)))
    fired = []

    def mutate():
        if fired:
            return
        fired.append(1)
        if how == "truncated":
            os.truncate(log, 0)
        elif how == "replaced":
            fresh = log.with_name("fresh.log")
            fresh.write_text("")
            os.replace(fresh, log)
        else:
            os.unlink(log)

    if when == "after_check":
        real_fstat = os.fstat
        calls = []

        def fstat_then_mutate(fd):
            result = real_fstat(fd)
            calls.append(1)
            if len(calls) == 1:  # the reader's pre-read check has just sampled the descriptor
                mutate()
            return result

        monkeypatch.setattr(oc1.os, "fstat", fstat_then_mutate)
    else:
        _open_with_hook_after_read(monkeypatch, log, mutate)
    state, why = _evidence(cwd, t)
    assert state == se.UNREADABLE and "truncated or replaced" in why, (state, why)


def test_a_log_that_only_GROWS_while_it_is_read_is_still_read(log, cwd, monkeypatch):
    """Append-only growth is the normal case and stays allowed."""
    t = time.time()
    _write(log, _cli(t - 30), _boot(t + 1, str(cwd)))
    grown = []

    def append():
        if not grown:
            grown.append(1)
            with open(log, "a") as fh:
                fh.write(_watch(t + 3, "/etc") + "\n")

    _open_with_hook_after_read(monkeypatch, log, append)
    assert _evidence(cwd, t)[0] == se.FOUND


def test_a_tail_that_cannot_reach_back_to_the_launch_is_UNREADABLE(log, cwd, monkeypatch):
    monkeypatch.setattr(oc, "LOG_TAIL_BYTES", 256)
    monkeypatch.setattr(oc, "LOG_TAIL_MAX_BYTES", 512)
    t = time.time()
    _write(log, *[_watch(t + 1 + i / 100, "/elsewhere") for i in range(200)])
    state, why = _evidence(cwd, t)
    assert state == se.UNREADABLE and "KiB" in why


def test_an_unreadable_log_file_is_UNREADABLE(log, cwd):
    _write(log, _cli(time.time() - 30))
    log.chmod(0o000)
    try:
        state, _ = _evidence(cwd, time.time())
    finally:
        log.chmod(0o644)
    assert state == se.UNREADABLE


# --- bind_session: by the nonce proof, never by "one new row" ----------------------------------


def _db(sessions=(), first_user=None, *, broken=False) -> Path:
    """A 2.2.7-schema store with ``sessions`` [(id, directory)] and each one's first user text."""
    db = Path(base._opencode2_db())
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.read_text())
    now = int(time.time() * 1000)
    for i, (sid, directory) in enumerate(sessions):
        con.execute(
            "INSERT INTO session_v2 (id, project_id, slug, directory, version, time_created,"
            " time_updated) VALUES (?, 'prj', 's', ?, '2.2.7', ?, ?)",
            (sid, directory, now + i, now + i),
        )
        text = (first_user or {}).get(sid)
        if text is not None:
            con.execute(
                "INSERT INTO session_message (id, session_id, type, seq, time_created,"
                " time_updated, data) VALUES (?, ?, 'user', 1, ?, ?, ?)",
                (f"msg_{i}", sid, now, now, json.dumps({"text": text, "files": []})),
            )
    if broken:
        con.execute("DROP TABLE session_part")
    con.commit()
    con.close()
    return db


def _envelope(brief="review the open bugs", nonce=NONCE) -> str:
    # Stored as 2.2.7 stores a pasted envelope: verbatim, plus one trailing space (measured).
    return launch_binding.envelope(brief, nonce) + " "


def _bind(cwd, snapshot=(), nonce=NONCE):
    return prov().bind_session(_launch(cwd, at=time.time(), snapshot=snapshot, nonce=nonce))


def test_binds_the_session_carrying_this_attempts_nonce(cwd):
    _db([(OURS, str(cwd))], {OURS: _envelope()})
    b = _bind(cwd)
    assert (b.state, b.native, b.proof) == (base.BIND_BOUND, OURS, base.PROOF_NONCE)


def test_a_competing_same_cwd_session_that_appears_FIRST_is_never_bound(cwd):
    """An operator's session in the same folder writes its row before ours: exactly one new
    candidate, and it is somebody else's. Pending until ours appears, then ours."""
    _db([(OTHER, str(cwd))], {OTHER: "review the open bugs"})
    assert _bind(cwd).state == base.BIND_PENDING
    Path(base._opencode2_db()).unlink()
    _db([(OTHER, str(cwd)), (OURS, str(cwd))], {OTHER: "review the open bugs", OURS: _envelope()})
    b = _bind(cwd)
    assert (b.state, b.native) == (base.BIND_BOUND, OURS)


def test_one_new_row_without_the_nonce_is_never_bound(cwd):
    _db([(OURS, str(cwd))], {OURS: None})  # a row with no turn yet
    assert _bind(cwd).state == base.BIND_PENDING


def test_a_MISMATCHED_nonce_is_not_a_proof(cwd):
    _db([(OURS, str(cwd))], {OURS: _envelope(nonce="f" * 32)})
    assert _bind(cwd).state == base.BIND_PENDING


def test_a_launch_with_NO_valid_nonce_binds_nothing(cwd):
    _db([(OURS, str(cwd))], {OURS: _envelope()})
    assert _bind(cwd, nonce="").state == base.BIND_UNREADABLE


def test_a_session_that_predates_the_launch_is_not_a_candidate(cwd):
    _db([(OURS, str(cwd))], {OURS: _envelope()})
    assert _bind(cwd, snapshot=[OURS]).state == base.BIND_PENDING


def test_TWO_sessions_carrying_the_nonce_is_AMBIGUOUS_and_refused(cwd):
    """The shared binder's vocabulary (#2083, Hermes): duplicate proofs are `ambiguous`; the
    dispatcher refuses it exactly as it refuses `unreadable`, and nothing is picked."""
    _db([(OURS, str(cwd)), (OTHER, str(cwd))], {OURS: _envelope(), OTHER: _envelope()})
    b = _bind(cwd)
    assert b.state == base.BIND_AMBIGUOUS and b.native in (None, "")


def test_an_UNREADABLE_candidate_is_unreadable_not_pending(cwd):
    """The strict 2.x reader raises where the sidebar's degrades to `[]` — which would read as
    "no turn yet" and turn a candidate we could not read into a pending one."""
    _db([(OURS, str(cwd)), (OTHER, str(cwd))], {OURS: _envelope()}, broken=True)
    assert _bind(cwd).state == base.BIND_UNREADABLE
    assert transcript.adapter_for(ENGINE)(OURS, Path.home()) == []


def test_a_candidate_in_ANOTHER_folder_is_not_considered(cwd, tmp_path):
    _db([(OURS, str(tmp_path))], {OURS: _envelope()})
    assert _bind(cwd).state == base.BIND_PENDING


# --- unattended_launch --------------------------------------------------------------------------


def _parts(engine_bin, env=None, bypass=False):
    engine_bin(ENGINE)
    return prov().unattended_launch(
        "new-x", cwd="/some/where", bypass=bypass, env=env if env is not None else {}
    )


def _evaluate(action: str, resource: str, rules: list[dict]) -> str:
    """2.2.7's `p2`: the LAST rule whose action and resource both match (`*` a wildcard), else
    `ask` — ported from the bundle for these simple patterns."""
    import fnmatch

    for r in reversed(rules):
        if fnmatch.fnmatchcase(action, r["action"]) and fnmatch.fnmatchcase(
            resource, r["resource"]
        ):
            return r["effect"]
    return "ask"


def test_the_argv_is_a_private_server_in_the_PINNED_cwd(engine_bin):
    argv, _ = _parts(engine_bin)
    assert argv[1:] == ["--standalone", "."]
    argv, env = _parts(engine_bin, bypass=True)
    assert argv[1:] == ["--standalone", ".", "--auto"] and env == {}


@pytest.mark.parametrize(
    ("action", "resource", "want"),
    [
        ("read", "/p/src/main.py", "allow"),
        ("glob", "**/*.py", "allow"),
        ("list", "/p", "allow"),
        ("todowrite", "*", "allow"),
        ("read", "/p/.env", "ask"),
        ("read", "/p/.env.local", "ask"),
        ("read", "/p/.env.example", "allow"),
        ("grep", "SECRET", "ask"),
        ("shell", "touch marker", "ask"),
        ("edit", "/p/a.py", "ask"),
        ("webfetch", "https://example.com", "ask"),
        ("subagent", "general", "ask"),
        ("external_directory", "/elsewhere/*", "ask"),
        ("a_tool_from_the_future", "*", "ask"),
    ],
)
def test_without_bypass_reads_go_through_and_everything_else_asks(
    engine_bin, action, resource, want
):
    _, env = _parts(engine_bin)
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    agent = cfg["default_agent"]
    assert agent.startswith("battlelab-mission-") and cfg["agents"][agent]["mode"] == "primary"
    assert cfg["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    assert _evaluate(action, resource, cfg["agents"][agent]["permissions"]) == want


def test_every_guarded_launch_gets_its_OWN_unguessable_agent(engine_bin):
    names = {json.loads(_parts(engine_bin)[1]["OPENCODE_CONFIG_CONTENT"])["default_agent"]}
    names.add(json.loads(_parts(engine_bin)[1]["OPENCODE_CONFIG_CONTENT"])["default_agent"])
    assert len(names) == 2


def test_an_operators_inline_config_is_kept_under_the_policy(engine_bin):
    mine = {
        "provider": {"x": {"options": {"baseURL": "http://127.0.0.1:1"}}},
        "permission": {"*": "allow"},
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        "agents": {"mine": {"mode": "primary"}},
    }
    _, env = _parts(engine_bin, env={"OPENCODE_CONFIG_CONTENT": json.dumps(mine)})
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["provider"] == mine["provider"] and "mine" in cfg["agents"]
    assert "permission" not in cfg  # the 1.x-shaped config-level allow is dropped
    assert cfg["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]


@pytest.mark.parametrize("existing", ["not json", "[1, 2]", '"a string"'])
def test_an_inline_config_that_cannot_be_merged_REFUSES_the_launch(engine_bin, existing):
    with pytest.raises(ValueError, match="cannot be applied"):
        _parts(engine_bin, env={"OPENCODE_CONFIG_CONTENT": existing})


def test_with_bypass_the_operators_policy_is_left_alone(engine_bin):
    _, env = _parts(engine_bin, env={"OPENCODE_CONFIG_CONTENT": "{}"}, bypass=True)
    assert env == {}


# --- unattended_preflight -----------------------------------------------------------------------


def _probe(monkeypatch, completed: bool, out: str) -> dict:
    from agent_sessions import engine_auth

    seen: dict = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return completed, out

    monkeypatch.setattr(engine_auth, "run_probe", run)
    return seen


MODELS = "opencode/beam-free\nopencode/big-pickle\nopencode/deepseek-v4.1-flash-free\n"


def test_listed_models_open_the_gate_even_with_zero_credentials(engine_bin, monkeypatch, cwd):
    """2.x's free default model runs unauthenticated, so the model list — not a credential
    count — is what says it can act (measured: 13 `opencode/*` lines, 0 credentials)."""
    exe = engine_bin(ENGINE)
    seen = _probe(monkeypatch, True, MODELS + "\n")
    state, why = prov().unattended_preflight(cwd=str(cwd))
    assert state == base.PREFLIGHT_OK and "3 model" in why
    # The measured argv: a private server, never `auth list` (which starts `serve --service`).
    assert seen["argv"] == [exe, "models", "--standalone"]
    assert seen["cwd"] == str(cwd)
    # The forced XDG tree rides on the probe, over the app's own environment.
    for key, value in opencode2.isolation_env(
        Path(os.environ["AGENT_SESSIONS_OPENCODE2_DIR"]),
        Path(base._opencode2_db()),
        Path.home(),
    ).items():
        assert seen["env"][key] == value


def test_no_model_at_all_is_a_REFUSAL(engine_bin, monkeypatch, cwd):
    engine_bin(ENGINE)
    _probe(monkeypatch, True, "\n")
    assert prov().unattended_preflight(cwd=str(cwd))[0] == base.PREFLIGHT_REFUSED


def test_a_probe_that_does_not_complete_is_UNKNOWN(engine_bin, monkeypatch, cwd):
    engine_bin(ENGINE)
    _probe(monkeypatch, False, "")
    assert prov().unattended_preflight(cwd=str(cwd))[0] == base.PREFLIGHT_UNKNOWN


def test_output_that_is_not_a_model_list_is_UNKNOWN_and_never_quoted(engine_bin, monkeypatch, cwd):
    engine_bin(ENGINE)
    secret = "sk-live-THISMUSTNOTLEAK"
    _probe(monkeypatch, True, MODELS + f"Error: bad token {secret}\n")
    state, why = prov().unattended_preflight(cwd=str(cwd))
    assert state == base.PREFLIGHT_UNKNOWN and secret not in why


def test_a_tree_that_reaches_1x_refuses_the_preflight_and_spawns_nothing(
    engine_bin, monkeypatch, cwd
):
    engine_bin(ENGINE)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE2_DIR", str(Path.home() / ".local/share"))
    seen = _probe(monkeypatch, True, MODELS)
    state, why = prov().unattended_preflight(cwd=str(cwd))
    assert state == base.PREFLIGHT_REFUSED and "overlaps" in why
    assert seen == {}


def test_every_capability_is_INVOKED_not_merely_callable(engine_bin, monkeypatch, cwd):
    engine_bin(ENGINE)
    p = prov()
    _db([])
    launch = _launch(cwd, at=time.time())
    assert p.snapshot_session_ids(str(cwd)) == set()
    assert p.start_evidence(launch)[0] in {se.FOUND, se.ABSENT, se.UNREADABLE}
    assert p.bind_session(launch).state in {
        base.BIND_BOUND,
        base.BIND_PENDING,
        base.BIND_AMBIGUOUS,
        base.BIND_UNREADABLE,
    }
    _probe(monkeypatch, True, MODELS)
    assert p.unattended_preflight(cwd=str(cwd))[0] == base.PREFLIGHT_OK
    argv, _env = p.unattended_launch("new-x", cwd=str(cwd), bypass=False, env={})
    assert argv[1:] == ["--standalone", "."]
