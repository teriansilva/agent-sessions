"""opencode's unattended-launch capabilities (#1050, the #989 contract).

Every test here is about a REFUSAL that must happen. A capability that says `found` when it should
say `absent` types a mission brief into whatever screen is in front of it; one that says `absent`
when it means "I could not look" reports an agent as never started when it may be running
unattended with permission bypass. So the happy path gets one test and the rest guard the edges.

The evidence these are built on was measured against the real CLI on 2026-09-21 (opencode 1.18.31)
and is recorded on #1050: the SQLite store writes NO `session` row for 45 s with nothing typed
(#916's deadlock, for this engine), while the structured log gets its `creating instance` line
~1.8 s in.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_sessions import start_evidence as se
from agent_sessions.engines import base
from agent_sessions.engines import opencode as oc

# Captured at import, before the autouse stand-in below replaces it, so the one test about the
# real probe can restore it without `monkeypatch.undo()` (which also reverts conftest's pins).
_REAL_LAUNCH_MASTER_STATE = oc._launch_master_state


@pytest.fixture(autouse=True)
def bare_kind_binary(monkeypatch):
    """These tests drive the opencode store KIND directly, unattached to its manifest. #853 P3
    removed the kind's own PATH fallback — a kind never picks a binary; in the app its provider's
    provenance-checked entrypoint does — so a bare kind gets a fixed stand-in here. Nothing in
    this file is about binary resolution."""
    monkeypatch.setattr(oc.OpenCodeProvider, "_bin", lambda self: "/opt/test/bin/opencode")


@pytest.fixture(autouse=True)
def master_alive(monkeypatch):
    """This launch's own dtach master, as `start_evidence` probes it. Alive unless a test says
    otherwise; the tests that are about it set it explicitly."""
    state = {"value": "alive"}
    monkeypatch.setattr(oc, "_launch_master_state", lambda launch: state["value"])
    return state


@pytest.fixture
def log(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "log" / "opencode.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_LOG", str(p))
    return p


def _stamp(epoch: float) -> str:
    import datetime

    return (
        datetime.datetime.fromtimestamp(epoch, datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _instance_line(epoch: float, directory: str, run: str = "287c3988") -> str:
    """The real shape, verbatim from the operator's log."""
    return (
        f"timestamp={_stamp(epoch)} level=INFO run={run} "
        f'message="creating instance" directory={directory}'
    )


def _noise(epoch: float, directory: str) -> str:
    return (
        f"timestamp={_stamp(epoch)} level=INFO run=deadbeef "
        f'message="watcher backend" directory={directory} platform=linux backend=inotify'
    )


def _launch(cwd, *, at: float) -> base.LaunchContext:
    return base.LaunchContext(
        engine="opencode",
        key="opencode:new-11111111-2222-3333-4444-555555555555",
        native="",
        cwd=str(cwd),
        nonce="0" * 32,
        launched_at=at,
    )


# --- start_evidence -------------------------------------------------------------------------


def test_an_instance_created_in_our_cwd_after_our_launch_is_found(log, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    t = time.time()
    log.write_text(_instance_line(t + 1.8, str(cwd)) + "\n")
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))[0] == se.FOUND


def test_nothing_yet_is_absent_not_unreadable(log, tmp_path):
    """The caller POLLS on this. Reporting "could not look" would abort a dispatch that just
    needed another second."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    log.write_text("")
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=time.time()))[0] == se.ABSENT


def test_a_missing_log_is_absent_not_unreadable(log, tmp_path):
    """opencode has simply never run on this host. There is genuinely no instance line, and the
    file appears on the first launch."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    assert not log.exists()
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=time.time()))[0] == se.ABSENT


def test_an_instance_in_ANOTHER_directory_is_not_ours(log, tmp_path):
    """The log is one shared file written by every concurrent opencode on the host."""
    cwd, other = tmp_path / "work", tmp_path / "elsewhere"
    cwd.mkdir()
    other.mkdir()
    t = time.time()
    log.write_text(_instance_line(t + 1.0, str(other)) + "\n")
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))[0] == se.ABSENT


def test_an_instance_created_BEFORE_our_launch_is_not_ours(log, tmp_path):
    """A previous opencode in the same directory, possibly days ago. Matching on the directory
    alone would report every relaunch as started before it had."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    t = time.time()
    log.write_text(_instance_line(t - 600.0, str(cwd)) + "\n")
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))[0] == se.ABSENT


def test_a_symlinked_cwd_still_matches(log, tmp_path):
    """`/tmp` vs `/private/tmp`, and a symlinked checkout, are the same place — claude's rule."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    t = time.time()
    log.write_text(_instance_line(t + 1.0, str(real)) + "\n")
    assert oc.OpenCodeProvider().start_evidence(_launch(link, at=t))[0] == se.FOUND


def test_a_directory_containing_spaces_is_parsed_whole(log, tmp_path):
    """`directory=` is taken to END OF LINE, not as a space-delimited field. Splitting on spaces
    truncates the path and the match silently fails."""
    cwd = tmp_path / "my work dir"
    cwd.mkdir()
    t = time.time()
    log.write_text(_instance_line(t + 1.0, str(cwd)) + "\n")
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))[0] == se.FOUND


def test_two_instances_in_our_cwd_are_UNREADABLE_not_found(log, tmp_path):
    """Ambiguity is not a flavour of presence. The log carries a `run` id but no pid, so two
    instances created in our directory since our launch cannot be told apart — and this gate
    decides whether to type a brief."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    t = time.time()
    log.write_text(
        _instance_line(t + 1.0, str(cwd), run="aaaa1111")
        + "\n"
        + _instance_line(t + 1.2, str(cwd), run="bbbb2222")
        + "\n"
    )
    state, why = oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))
    assert state == se.UNREADABLE
    assert "pid" in why


def test_only_the_creating_instance_line_counts(log, tmp_path):
    """Other lines carry `directory=` too. Matching on the field alone would call a watcher
    starting up 'an agent started'."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    t = time.time()
    log.write_text(_noise(t + 1.0, str(cwd)) + "\n")
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))[0] == se.ABSENT


def test_a_log_that_cannot_be_read_back_to_the_launch_is_UNREADABLE(log, tmp_path, monkeypatch):
    """The window grew to its ceiling and every line in it is still newer than the launch, so a
    line we needed may have been cut off. Reporting `absent` there is indistinguishable from a
    genuine absence — the exact confusion #989 forbids."""
    monkeypatch.setattr(oc, "LOG_TAIL_BYTES", 256)
    monkeypatch.setattr(oc, "LOG_TAIL_MAX_BYTES", 512)
    cwd = tmp_path / "work"
    cwd.mkdir()
    t = time.time()
    # Every line is AFTER the floor, so the window can never reach back past it.
    log.write_text("".join(_noise(t + i, "/somewhere/else") + "\n" for i in range(200)))
    state, why = oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))
    assert state == se.UNREADABLE
    assert "far" in why or "KiB" in why


def test_an_unreadable_log_file_is_UNREADABLE(log, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    log.write_text("x\n")
    log.chmod(0o000)
    try:
        state, _ = oc.OpenCodeProvider().start_evidence(_launch(cwd, at=time.time()))
    finally:
        log.chmod(0o644)
    assert state == se.UNREADABLE


# --- unattended_preflight -------------------------------------------------------------------


def _fake_probe(monkeypatch, completed: bool, out: str):
    from agent_sessions import engine_auth

    monkeypatch.setattr(engine_auth, "run_probe", lambda *a, **k: (completed, out))


def test_configured_credentials_open_the_gate(monkeypatch, tmp_path):
    _fake_probe(monkeypatch, True, "Credentials\n●  OpenAI oauth\n└  3 credentials\n")
    state, _ = oc.OpenCodeProvider().unattended_preflight(cwd=str(tmp_path))
    assert state == base.PREFLIGHT_OK


def test_zero_credentials_is_a_REFUSAL(monkeypatch, tmp_path):
    """A logged-out install would take the brief and be unable to act on it."""
    _fake_probe(monkeypatch, True, "└  0 credentials\n")
    state, why = oc.OpenCodeProvider().unattended_preflight(cwd=str(tmp_path))
    assert state == base.PREFLIGHT_REFUSED
    assert "credentials" in why


def test_a_probe_that_does_not_complete_is_UNKNOWN(monkeypatch, tmp_path):
    """`unknown` is not permission — `headless_dispatch` refuses it exactly as it refuses a no."""
    _fake_probe(monkeypatch, False, "")
    state, _ = oc.OpenCodeProvider().unattended_preflight(cwd=str(tmp_path))
    assert state == base.PREFLIGHT_UNKNOWN


def test_an_unrecognised_answer_is_UNKNOWN_never_a_refusal(monkeypatch, tmp_path):
    """opencode never said no. Reporting a refusal it did not make would tell the operator their
    install is logged out when it may not be."""
    _fake_probe(monkeypatch, True, "some future output nobody has parsed")
    state, _ = oc.OpenCodeProvider().unattended_preflight(cwd=str(tmp_path))
    assert state == base.PREFLIGHT_UNKNOWN


def test_the_preflight_detail_never_carries_probe_output(monkeypatch, tmp_path):
    """engine_auth's rule: a diagnostic must not be able to leak a token that appeared in an
    error message."""
    secret = "sk-live-THISMUSTNOTLEAK"
    _fake_probe(monkeypatch, True, f"error: bad token {secret}\n")
    _state, why = oc.OpenCodeProvider().unattended_preflight(cwd=str(tmp_path))
    assert secret not in why


def test_the_preflight_argv_is_literal_and_never_a_command_string(monkeypatch, tmp_path):
    """The shell-free guarantee in CLAUDE.md covers this path: it spawns a real vendor process."""
    seen = {}

    from agent_sessions import engine_auth

    def spy(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return True, "└  1 credentials"

    monkeypatch.setattr(engine_auth, "run_probe", spy)
    oc.OpenCodeProvider().unattended_preflight(cwd=str(tmp_path))
    assert isinstance(seen["argv"], list)
    assert all(isinstance(a, str) for a in seen["argv"])
    assert seen["argv"][1:] == ["auth", "list"]
    assert seen["kw"]["cwd"] == str(tmp_path)


# --- the gate as a whole --------------------------------------------------------------------


def test_opencode_is_now_offerable_for_an_unattended_dispatch():
    """The point of the whole file. Before #1050 `unattended_start_state` refused opencode with
    "missing: unattended_preflight, start_evidence, bind_session"."""
    from agent_sessions import engines

    prov = oc.OpenCodeProvider()
    supported, why = engines.unattended_start_state(prov)
    assert (supported, why) == (True, None)
    for capability in engines.LATE_ID_CAPABILITIES:
        assert callable(getattr(prov, capability, None)), capability


def test_codex_is_still_refused_and_the_reason_names_what_is_missing():
    """#1050 narrowed to opencode: codex's store is not a start gate either (measured), and no
    usable artifact was found, so it must stay refused rather than quietly inherit this."""
    from agent_sessions import engines

    prov = next(p for p in engines.all_providers() if p.engine_id == "codex")
    supported, why = engines.unattended_start_state(prov)
    assert supported is False
    assert "start_evidence" in (why or "")


# --- bind_session, DRIVEN end to end ----------------------------------------------------------
#
# The capability checks above assert the four names are *callable*. That is what
# `unattended_start_state` asks, and it is not enough: `bind_session` shipped calling
# `start_evidence.bind_by_nonce`, which does not exist — the shared implementation lives in
# `launch_binding`. Every call raised `AttributeError`, `headless_dispatch._await_binding` turned
# that into `BIND_UNREADABLE` and polled until the binding budget expired, and the dispatch failed
# only AFTER the brief had been typed into a live agent. A whole suite of green tests never
# noticed, because not one of them CALLED the method (Hermes, review 4983).
#
# So these drive the real provider against a real store. "Callable" is not "works".


def _seed_store(tmp_path, monkeypatch, sessions, messages=()):
    """A minimal `opencode.db`: `session` rows, plus `message`/`part` rows for the transcript."""
    import sqlite3

    db = tmp_path / "opencode.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT, "
        "time_created INTEGER, time_updated INTEGER, time_archived INTEGER)"
    )
    con.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
    con.execute("CREATE TABLE part (id TEXT, message_id TEXT, data TEXT)")
    con.executemany(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        [(sid, None, directory, "t", 1, 1, None) for sid, directory in sessions],
    )
    import json as _json

    for i, (sid, role, text) in enumerate(messages):
        mid = f"msg{i:04d}"
        con.execute("INSERT INTO message VALUES (?,?,?)", (mid, sid, _json.dumps({"role": role})))
        con.execute(
            "INSERT INTO part VALUES (?,?,?)",
            (f"prt{i:04d}", mid, _json.dumps({"type": "text", "text": text})),
        )
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    return db


_NONCE = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
_SID = "ses_11111111111111"
_OTHER = "ses_22222222222222"


def _bind_launch(cwd, snapshot):
    return base.LaunchContext(
        engine="opencode",
        key="opencode:new-11111111-2222-3333-4444-555555555555",
        native="",
        cwd=str(cwd),
        nonce=_NONCE,
        snapshot=frozenset(snapshot),
        launched_at=time.time(),
    )


def test_bind_session_actually_runs_and_binds_the_session_carrying_the_nonce(tmp_path, monkeypatch):
    """The regression test for the shipped `AttributeError`: this fails with a raise, not an
    assertion, if `bind_session` ever points at the wrong module again."""
    from agent_sessions import launch_binding

    cwd = tmp_path / "work"
    cwd.mkdir()
    brief = "do the thing\n\n" + launch_binding.nonce_line(_NONCE)
    _seed_store(
        tmp_path,
        monkeypatch,
        sessions=[(_SID, str(cwd))],
        messages=[(_SID, "user", brief)],
    )
    binding = oc.OpenCodeProvider().bind_session(_bind_launch(cwd, snapshot=[]))
    assert binding.state == base.BIND_BOUND
    assert binding.native == _SID
    assert binding.proof == base.PROOF_NONCE


def test_bind_session_does_not_bind_a_session_that_never_got_our_nonce(tmp_path, monkeypatch):
    """Correlation is not attribution — #989's whole point. A session given the SAME brief without
    this attempt's nonce is `pending`, never `bound`."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    _seed_store(
        tmp_path,
        monkeypatch,
        sessions=[(_SID, str(cwd))],
        messages=[(_SID, "user", "do the thing")],
    )
    binding = oc.OpenCodeProvider().bind_session(_bind_launch(cwd, snapshot=[]))
    assert binding.state == base.BIND_PENDING


def test_bind_session_ignores_a_session_that_predates_the_launch(tmp_path, monkeypatch):
    """The pre-launch snapshot is what makes a candidate a candidate."""
    from agent_sessions import launch_binding

    cwd = tmp_path / "work"
    cwd.mkdir()
    brief = "do the thing\n\n" + launch_binding.nonce_line(_NONCE)
    _seed_store(
        tmp_path,
        monkeypatch,
        sessions=[(_SID, str(cwd))],
        messages=[(_SID, "user", brief)],
    )
    # The session was already there before we launched, so it cannot be ours.
    binding = oc.OpenCodeProvider().bind_session(_bind_launch(cwd, snapshot=[_SID]))
    assert binding.state == base.BIND_PENDING


def test_bind_session_refuses_when_two_sessions_carry_the_nonce(tmp_path, monkeypatch):
    """`ambiguous`, never a pick. Two agents cannot both be the one we launched."""
    from agent_sessions import launch_binding

    cwd = tmp_path / "work"
    cwd.mkdir()
    brief = "do the thing\n\n" + launch_binding.nonce_line(_NONCE)
    _seed_store(
        tmp_path,
        monkeypatch,
        sessions=[(_SID, str(cwd)), (_OTHER, str(cwd))],
        messages=[(_SID, "user", brief), (_OTHER, "user", brief)],
    )
    binding = oc.OpenCodeProvider().bind_session(_bind_launch(cwd, snapshot=[]))
    assert binding.state == base.BIND_AMBIGUOUS


def test_every_declared_capability_is_actually_INVOKED_not_merely_callable(tmp_path, monkeypatch):
    """The gap that let the `AttributeError` ship.

    `unattended_start_state` checks that the four capability NAMES are callable, so a method whose
    body is broken passes it and passes the #989 ratchet too. This calls all four for real and
    asserts each returns its contract's shape rather than raising.
    """
    cwd = tmp_path / "work"
    cwd.mkdir()
    _seed_store(tmp_path, monkeypatch, sessions=[])
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_LOG", str(tmp_path / "nope.log"))
    prov = oc.OpenCodeProvider()
    launch = _bind_launch(cwd, snapshot=[])

    assert prov.snapshot_session_ids(str(cwd)) == set()

    state, _why = prov.start_evidence(launch)
    assert state in {se.FOUND, se.ABSENT, se.UNREADABLE}

    binding = prov.bind_session(launch)
    assert isinstance(binding, base.Binding)
    assert binding.state in {
        base.BIND_BOUND,
        base.BIND_PENDING,
        base.BIND_AMBIGUOUS,
        base.BIND_UNREADABLE,
    }

    from agent_sessions import engine_auth

    monkeypatch.setattr(engine_auth, "run_probe", lambda *a, **k: (True, "└  1 credentials"))
    pstate, _ = prov.unattended_preflight(cwd=str(cwd))
    assert pstate in {base.PREFLIGHT_OK, base.PREFLIGHT_REFUSED, base.PREFLIGHT_UNKNOWN}


# --- review comment 72377 ---------------------------------------------------------------------


def test_the_timestamp_tolerance_is_pinned_and_does_not_admit_an_earlier_instance(log, tmp_path):
    """Finding 6. A line 2.5 s before the launch predates it; widening the tolerance to admit it
    (it was once widenable to 500 s with every test green) turns a previous opencode in this
    folder into this launch's start."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    t = time.time()
    log.write_text(_instance_line(t - 2.5, str(cwd)) + "\n")
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))[0] == se.ABSENT
    # …while the rounding it exists for still counts.
    log.write_text(_instance_line(t - 1.0, str(cwd)) + "\n")
    assert oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))[0] == se.FOUND


@pytest.mark.parametrize("master", ["dead", "unknown"])
def test_a_line_in_our_folder_is_not_OUR_start_once_our_master_is_gone(
    log, tmp_path, master_alive, master
):
    """Finding 4. `dtach` exits with its child: a dead master means our opencode is gone, and the
    one line in our directory is someone else's instance. Neither that nor "could not probe" is a
    start."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    t = time.time()
    log.write_text(_instance_line(t + 1.0, str(cwd)) + "\n")
    master_alive["value"] = master
    state, why = oc.OpenCodeProvider().start_evidence(_launch(cwd, at=t))
    assert state == se.UNREADABLE
    assert "this launch's own process" in why


def test_the_master_probe_asks_the_socket_of_THIS_launch(monkeypatch):
    from agent_sessions import ptybridge

    # The real `_launch_master_state`, not the autouse stand-in.
    monkeypatch.setattr(oc, "_launch_master_state", _REAL_LAUNCH_MASTER_STATE)
    asked: list = []
    monkeypatch.setattr(ptybridge, "probe_master", lambda sock: asked.append(sock) or "dead")
    launch = base.LaunchContext(
        engine="opencode",
        key="opencode:new-11111111-2222-3333-4444-555555555555",
        native="new-11111111-2222-3333-4444-555555555555",
        cwd="/x",
        nonce="0" * 32,
    )
    assert oc._launch_master_state(launch) == "dead"
    assert asked == [ptybridge.socket_path("opencode", launch.native)]


def test_a_symlinked_cwd_BINDS(tmp_path, monkeypatch):
    """Finding 2. opencode records the real path; the dispatcher holds the operator's spelling.
    Start evidence already resolved both sides, so a raw comparison here said `found`, typed the
    brief, and then could never bind — tearing down a working agent at the binding deadline."""
    from agent_sessions import launch_binding

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    brief = "do the thing\n\n" + launch_binding.nonce_line(_NONCE)
    _seed_store(
        tmp_path, monkeypatch, sessions=[(_SID, str(real))], messages=[(_SID, "user", brief)]
    )
    prov = oc.OpenCodeProvider()
    assert prov.snapshot_session_ids(str(link)) == {_SID}
    assert prov.reconcile_new_session(str(link), set()) == _SID
    binding = prov.bind_session(_bind_launch(link, snapshot=[]))
    assert binding.state == base.BIND_BOUND
    assert binding.native == _SID


def test_an_UNREADABLE_candidate_is_unreadable_not_pending(tmp_path, monkeypatch):
    """Finding 3. The store lists the new id, but its transcript cannot be read. The fail-soft
    adapter answers `[]` — right for the sidebar, wrong here: `[]` reads as "no turn yet", and #989
    says unreadable is not absent."""
    import sqlite3

    from agent_sessions import transcript

    cwd = tmp_path / "work"
    cwd.mkdir()
    db = _seed_store(tmp_path, monkeypatch, sessions=[(_SID, str(cwd))])
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE message")
    con.execute("DROP TABLE part")
    con.commit()
    con.close()

    binding = oc.OpenCodeProvider().bind_session(_bind_launch(cwd, snapshot=[]))
    assert binding.state == base.BIND_UNREADABLE
    # …while every other reader of the same store stays fail-soft, so it never takes down a row.
    assert transcript.adapter_for("opencode")(_SID, tmp_path) == []


# --- `unattended_launch`: bypass=False is ENFORCED, not assumed (finding 1) ----------------------


def _launch_parts(env=None, bypass=False):
    return oc.OpenCodeProvider().unattended_launch(
        "new-x", cwd="/some/where", bypass=bypass, env=env or {}
    )


def _wildcard(value: str, pattern: str) -> bool:
    """opencode 1.18.32's `Wildcard.match`, ported verbatim from the binary: backslashes to `/`,
    escape ``.+^${}()|[]\\``, ``*`` → ``.*``, ``?`` → ``.``, a trailing `` .*`` made optional,
    anchored, dot-all."""
    import re

    value = value.replace("\\", "/")
    rx = re.sub(r"[.+^${}()|\[\]\\]", lambda m: "\\" + m.group(0), pattern.replace("\\", "/"))
    rx = rx.replace("*", ".*").replace("?", ".")
    if rx.endswith(" .*"):
        rx = rx[:-3] + "( .*)?"
    return re.fullmatch(rx, value, re.S) is not None


def _evaluate(permission: str, pattern: str, rules: list[dict]) -> str:
    """opencode's `evaluate`: the LAST rule whose permission and pattern both match; ``ask`` when
    none does."""
    for r in reversed(rules):
        if _wildcard(permission, r["permission"]) and _wildcard(pattern, r["pattern"]):
            return r["action"]
    return "ask"


def _block_rules(block: dict) -> list[dict]:
    """A config permission block as opencode flattens it: key order kept, a string is ``*``."""
    out = []
    for perm, v in block.items():
        for pat, action in v.items() if isinstance(v, dict) else [("*", v)]:
            out.append({"permission": perm, "pattern": pat, "action": action})
    return out


#: (permission, pattern, expected) — the coordinator's decision on #1055: claude parity. Reads in
#: the project go through; anything that writes, runs, reaches out, leaves the project, or reads a
#: `.env` asks; so does a tool nobody has heard of yet.
_POLICY_CASES = [
    ("read", "/p/src/main.py", "allow"),
    ("glob", "**/*.py", "allow"),
    # grep keys on its REGEX and runs `rg --hidden`, so `include: .env` would bypass the read guard.
    ("grep", "hello", "ask"),
    ("grep", "SECRET", "ask"),
    ("list", "/p", "allow"),
    ("todowrite", "*", "allow"),
    ("read", "/p/.env", "ask"),
    ("read", "/p/.env.local", "ask"),
    ("read", "/p/.env.example", "allow"),
    ("bash", "echo hi", "ask"),
    ("edit", "/p/src/main.py", "ask"),
    ("webfetch", "https://example.com", "ask"),
    ("websearch", "anything", "ask"),
    ("task", "general", "ask"),
    ("external_directory", "/elsewhere/*", "ask"),
    ("a_tool_from_the_future", "*", "ask"),
]


def test_without_bypass_reads_go_through_and_everything_else_asks():
    import json

    argv, env = _launch_parts()
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    name = cfg["default_agent"]
    assert oc._UNATTENDED_AGENT_RE.match(name)
    agent = cfg["agent"][name]
    assert agent["permission"] == oc.UNATTENDED_PERMISSION
    # ORDER IS THE POLICY: the catch-all comes first so only the read-only keys after it win.
    assert next(iter(agent["permission"].items())) == ("*", "ask")
    assert agent["mode"] == "primary"
    assert cfg["permission"] == {"*": "ask"}
    rules = _block_rules(agent["permission"])
    for perm, pattern, want in _POLICY_CASES:
        assert _evaluate(perm, pattern, rules) == want, (perm, pattern)


def test_the_launch_passes_the_PINNED_cwd_never_the_name():
    """Finding 5. The spawn happens in the descriptor the dispatcher opened; a path argument would
    make opencode resolve the operator's folder by name again, after every check that pinned it."""
    argv, _env = _launch_parts()
    assert argv[1:] == ["."]
    assert "/some/where" not in argv
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)


def test_with_bypass_the_operators_own_policy_is_left_alone():
    _argv, env = _launch_parts(bypass=True)
    assert env == {}


def test_an_operators_own_inline_config_is_KEPT_under_the_override():
    """It may carry the provider the launch needs to authenticate."""
    import json

    existing = json.dumps(
        {
            "provider": {"local": {"npm": "x"}},
            "default_agent": "yolo",
            "agent": {"yolo": {"permission": {"*": "allow"}}},
            "permission": {"*": "allow"},
        }
    )
    _argv, env = _launch_parts(env={"OPENCODE_CONFIG_CONTENT": existing})
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["provider"] == {"local": {"npm": "x"}}
    name = cfg["default_agent"]
    assert oc._UNATTENDED_AGENT_RE.match(name)
    assert cfg["permission"] == {"*": "ask"}
    assert cfg["agent"][name]["permission"] == oc.UNATTENDED_PERMISSION
    assert cfg["agent"]["yolo"] == {"permission": {"*": "allow"}}  # kept, just not started in


def test_every_launch_gets_its_OWN_unguessable_agent_name():
    """Re-review at 89defb5: opencode merges the override key by key into an existing declaration
    of the same agent, so ANY fixed name can be pre-declared by a project to win. A name minted per
    launch cannot have been."""
    import json

    names = {
        json.loads(_launch_parts()[1]["OPENCODE_CONFIG_CONTENT"])["default_agent"]
        for _ in range(20)
    }
    assert len(names) == 20
    assert all(oc._UNATTENDED_AGENT_RE.match(n) for n in names)


@pytest.mark.parametrize(
    "name",
    ["battlelab-mission", "build", "battlelab-mission-XYZ", "battlelab-mission-0123456789abcdef\n"],
)
def test_a_malformed_agent_name_is_refused(name):
    with pytest.raises(ValueError):
        oc._ask_config_content(None, name)


@pytest.mark.parametrize("existing", ["{not json", "[1, 2]", '"ask"'])
def test_an_inline_config_that_cannot_be_merged_REFUSES_the_launch(existing):
    with pytest.raises(ValueError):
        _launch_parts(env={"OPENCODE_CONFIG_CONTENT": existing})


_REAL_OPENCODE = Path.home() / ".opencode" / "bin" / "opencode"


@pytest.mark.skipif(not _REAL_OPENCODE.exists(), reason="needs the real opencode binary")
def test_the_REAL_opencode_resolves_the_launch_to_ask_over_a_hostile_config(tmp_path):
    """Against the real binary, in a throwaway HOME, with the worst configuration measured: a global
    ``"*": "allow"``, an operator ``default_agent`` whose agent allows everything, a PROJECT
    ``build`` agent that begins with ``"*": "allow"`` and allows ``websearch`` after it, and
    hostile agents declared under the old fixed name and a same-prefix name — in the project's
    agent directory, the project's ``opencode.json`` and the global agent directory — each
    ``"*": allow`` then ``bash: allow``, the shape that won against a fixed name (re-review at
    89defb5). The agent the launch starts in, evaluated the way opencode does it (last matching
    rule, its own wildcard), must give: (a) read/glob allow, (b) grep/bash/edit/webfetch ask,
    (c) `.env` and outside-the-project ask, (d) the operator's and the project's CONFIG allows
    do not win. What this does not cover, by design: hostile project CODE (a plugin)."""
    import json
    import subprocess

    home, proj = tmp_path / "home", tmp_path / "proj"
    (home / ".config" / "opencode" / "agent").mkdir(parents=True)
    (proj / ".opencode" / "agent").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text(
        json.dumps({"permission": {"*": "allow", "bash": "allow"}, "default_agent": "yolo"})
    )
    (home / ".config" / "opencode" / "agent" / "yolo.md").write_text(
        '---\ndescription: yolo\nmode: primary\npermission:\n  "*": allow\n---\nyolo\n'
    )
    (proj / ".opencode" / "agent" / "build.md").write_text(
        '---\ndescription: b\nmode: primary\npermission:\n  "*": allow\n  websearch: allow\n'
        "---\nb\n"
    )
    hostile = (
        '---\ndescription: h\nmode: primary\npermission:\n  "*": allow\n  bash: allow\n---\nh\n'
    )
    for name in ("battlelab-mission", "battlelab-mission-0000000000000000"):
        (proj / ".opencode" / "agent" / f"{name}.md").write_text(hostile)
        (home / ".config" / "opencode" / "agent" / f"{name}.md").write_text(hostile)
    (proj / "opencode.json").write_text(
        json.dumps(
            {
                "agent": {
                    n: {"permission": {"*": "allow", "bash": "allow"}}
                    for n in ("battlelab-mission", "battlelab-mission-0000000000000000")
                }
            }
        )
    )
    _argv, override = oc.OpenCodeProvider().unattended_launch(
        "new-x", cwd=str(proj), bypass=False, env={}
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        # Hermetic: no model-catalogue fetch, no self-update, from a test.
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        **override,
    }

    def resolved(*args):
        out = subprocess.run(  # noqa: S603 — literal argv
            [str(_REAL_OPENCODE), "debug", *args],
            cwd=proj,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout
        return json.loads(out[out.index("{") :])

    name = json.loads(override["OPENCODE_CONFIG_CONTENT"])["default_agent"]
    assert resolved("config")["default_agent"] == name
    rules = resolved("agent", name)["permission"]
    # Patterns in the shapes opencode 1.18.32 actually evaluates, read off its own log in a live
    # run under this same hostile config: `read` gets the path without its leading `/`, `grep` its
    # search term, and a read OUTSIDE the project raises `external_directory` for `<dir>/*`.
    inside = str(proj / "notes.txt").lstrip("/")
    cases = [
        ("read", inside, "allow"),  # (a)
        ("glob", "**/*.py", "allow"),  # (a)
        ("grep", "hello", "ask"),  # (b): keyed on the regex, runs `rg --hidden`
        ("bash", "echo hi", "ask"),  # (b)
        ("edit", inside, "ask"),  # (b)
        ("webfetch", "https://example.com", "ask"),  # (b)
        ("read", str(proj / ".env").lstrip("/"), "ask"),  # (c)
        ("external_directory", f"{tmp_path / 'elsewhere'}/*", "ask"),  # (c)
        ("websearch", "anything", "ask"),  # (d): the project's build.md allows it
        ("a_tool_from_the_future", "*", "ask"),  # (d): the operator's global `"*": allow`
    ]
    for perm, pattern, want in cases:
        assert _evaluate(perm, pattern, rules) == want, (perm, pattern, rules[-8:])
