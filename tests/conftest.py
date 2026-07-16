"""Shared pytest fixtures.

Tests don't touch the real Claude Code session history or the real sidecar —
every fixture sets up isolated paths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_sessions import auth
from agent_sessions.auth import AuthConfig, hash_password

# The production work factor, captured at conftest import — i.e. before ANY fixture (in
# particular _fast_pbkdf2 below) can patch the module constant. The dedicated guard test
# (test_password.py::test_production_kdf_iteration_count_is_not_silently_downgraded) pins
# this against the shipped 600k.
_PROD_PBKDF2_ITERS = auth._PBKDF2_ITERS


@pytest.fixture(scope="session")
def prod_pbkdf2_iters() -> int:
    return _PROD_PBKDF2_ITERS


@pytest.fixture(scope="session", autouse=True)
def _fast_pbkdf2():
    """Shrink the PBKDF2 work factor to 1,000 iterations for the test session (#699).

    At the production 600k (``auth._PBKDF2_ITERS``) every ``hash_password``/``verify_password``
    costs ~0.4–0.6 s — and the auth/2FA tests hash and verify *in-test* constantly (a single 2FA
    enrollment mints a full recovery-code set; recovery login scans every stored hash), which
    dominated the suite's wall clock. The patch is safe because the encoded hash is
    self-describing (``pbkdf2_sha256$<iters>$<salt>$<key>``) and ``verify_password`` derives with
    the iteration count parsed FROM the string, never the module constant — so test-minted
    ``$1000$`` hashes verify at 1,000 rounds while parsing, scheme rejection, the real PBKDF2
    derivation, and the constant-time compare all still execute unchanged. Production is
    structurally out of reach: only ``hash_password`` reads the constant at mint time, every real
    mint site (install.sh, change-password, recovery codes) runs in the server process, and
    ``tests/`` is never packaged into the wheel.

    The assertion below fails the WHOLE suite the moment the production work factor is
    weakened at the source; the dedicated #395 guard test in test_password.py additionally
    pins the exact value and round-trips one real 600k hash per suite."""
    assert auth._PBKDF2_ITERS >= 600_000, (
        f"production PBKDF2 work factor weakened: auth._PBKDF2_ITERS "
        f"is {auth._PBKDF2_ITERS}, expected >= 600_000"
    )
    orig = auth._PBKDF2_ITERS
    auth._PBKDF2_ITERS = 1_000
    yield
    auth._PBKDF2_ITERS = orig


@pytest.fixture(autouse=True)
def _isolate_scrollback(tmp_path, monkeypatch) -> None:
    """Point the persisted-scrollback cache (#206) at a per-test tmp dir and reset the
    in-memory ring state, so ``_buffer_append`` never writes to the real ``$HOME`` and no
    cache bytes leak between tests (``_SCROLLBACK_DIR`` is captured at import from
    ``Path.home()``, so setting ``$HOME`` later is not enough)."""
    from agent_sessions import webterm

    monkeypatch.setattr(webterm.scrollback, "_SCROLLBACK_DIR", tmp_path / "scrollback-cache")
    for d in (
        webterm._BUFFERS,
        webterm._TOTALS,
        webterm._LAST_OUTPUT_AT,
        webterm._SUPPRESS_OUTPUT_UNTIL,
        webterm.scrollback._MODES,
        webterm.scrollback._MODE_CARRY,
        webterm.scrollback._SANITIZE_CARRY,
    ):
        d.clear()
    webterm._LOADED_FROM_DISK.clear()


@pytest.fixture(autouse=True)
def _isolate_prefs(tmp_path, monkeypatch) -> None:
    """Point operator prefs (#465) at a per-test tmp file so a LIVE config on the machine running
    the suite can't leak in. ``prefs.get_project_roots()`` — read FIRST by
    ``project_dirs.effective_roots`` — otherwise returns the operator's real project roots (e.g. a
    runner whose owner has set ``/home/<user>`` as a root), and ``test_project_dirs``'s
    'no roots' / env-only cases fail against the live roots instead of the test's. ``_default_path``
    reads ``AGENT_SESSIONS_PREFS`` per call, so the env override is enough — no file is created, so
    ``_load`` sees an empty prefs and roots fall back to ``AGENT_SESSIONS_PROJECT_ROOTS``.

    Points at the CANONICAL ``~/.config/agent-sessions/prefs.json`` sub-path under the tmp dir so it
    coincides with what ``tmp_home`` already uses for METADATA/PROJECTS (and with the real default
    when ``$HOME`` is the tmp dir) — so a test that writes a legacy prefs file via the default path
    and reads it back through ``create_app`` (e.g. the migration test) still lines up."""
    monkeypatch.setenv(
        "AGENT_SESSIONS_PREFS", str(tmp_path / ".config" / "agent-sessions" / "prefs.json")
    )


@pytest.fixture(autouse=True)
def _isolate_scan_cache() -> None:
    """Disable + reset the ``/api/sessions`` scan snapshot cache (#561) for every test.

    The cache memoises ``engines.scan_all()`` for a short TTL keyed on ``Path.home()``. With it
    live, a test that mutates the tree (archive moves a JSONL) and re-queries within the TTL would
    read the pre-mutation snapshot — and many tests monkeypatch ``engines.scan_all`` then call the
    route twice expecting each call to re-run the patch. Setting the TTL to 0 makes every request
    re-walk (identical to pre-#561 behaviour); the dedicated cache tests opt back in with an
    explicit ``set_scan_cache_ttl``. Cleared on the way in and out so no snapshot leaks across
    tests (distinct ``$HOME``s already isolate the key, but a shared 0-key entry could otherwise
    survive a test that raised the TTL)."""
    from agent_sessions import engines

    engines.set_scan_cache_ttl(0.0)
    engines.invalidate_scan_cache()
    yield
    engines.set_scan_cache_ttl(0.0)
    engines.invalidate_scan_cache()


@pytest.fixture
def tmp_home(tmp_path, monkeypatch) -> Path:
    """Pretend the user's ``$HOME`` is an empty tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_SESSIONS_METADATA",
        str(tmp_path / ".config" / "agent-sessions" / "metadata.json"),
    )
    monkeypatch.setenv(
        "AGENT_SESSIONS_PROJECTS",
        str(tmp_path / ".config" / "agent-sessions" / "projects.json"),
    )
    return tmp_path


@pytest.fixture
def fake_jsonl(tmp_home) -> Path:
    """Lay down a couple of Claude Code-shaped JSONLs under tmp_home/.claude/projects/."""
    projects = tmp_home / ".claude" / "projects"
    proj1 = projects / "-home-user-claude-repo-a"
    proj2 = projects / "-tmp-other"
    proj1.mkdir(parents=True)
    proj2.mkdir(parents=True)
    (proj1 / "11111111-1111-1111-1111-111111111111.jsonl").write_text(
        '{"type":"user","message":{"content":"first message on repo-a"}}\n'
    )
    (proj1 / "22222222-2222-2222-2222-222222222222.jsonl").write_text(
        '{"type":"user","message":{"content":[{"type":"text","text":"second"}]}}\n'
    )
    (proj2 / "33333333-3333-3333-3333-333333333333.jsonl").write_text(
        '{"type":"user","message":{"content":"hello tmp"}}\n'
    )
    # A dotted-path project: the dir name is lossy (demoapp.io and
    # demoapp/io both encode to ...-demoapp-io), but the JSONL carries the
    # real cwd. The scanner must prefer the JSONL cwd over the decode.
    proj3 = projects / "-home-user-claude-demoapp-io"
    proj3.mkdir(parents=True)
    (proj3 / "55555555-5555-5555-5555-555555555555.jsonl").write_text(
        '{"type":"user","cwd":"/home/user/claude/demoapp.io",'
        '"message":{"content":"dotted path session"}}\n'
    )
    # An archived one — same shape, different root.
    archive = tmp_home / ".claude" / "projects-archive" / "-home-user-claude-old"
    archive.mkdir(parents=True)
    (archive / "44444444-4444-4444-4444-444444444444.jsonl").write_text(
        '{"type":"user","message":{"content":"archived session"}}\n'
    )
    return tmp_home


@pytest.fixture(scope="session")
def password_hash() -> str:
    """The encoded ``hunter2`` hash, computed ONCE per test session (#395). ``hash_password``
    runs production-strength PBKDF2 (600k iterations — ~0.6 s on a free core, multiple seconds
    under load); the function-scoped ``auth_cfg`` re-ran it on every construction across ~16
    modules, so the suite paid that KDF cost dozens of times and page-thrashed on a loaded CI
    runner (#393). Fixtures only need a *valid* hash, not a fresh one — the KDF scheme itself is
    covered by the dedicated auth/password unit tests. Byte-format-identical, zero API change."""
    return hash_password("hunter2")


@pytest.fixture
def auth_cfg(tmp_path, monkeypatch, password_hash) -> AuthConfig:
    monkeypatch.setenv("AGENT_SESSIONS_USERNAME", "marcus")
    monkeypatch.setenv("AGENT_SESSIONS_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    # Isolate the 2FA store (#116) to a tmp path — otherwise twofactor.default_path()
    # resolves under the real HOME and the auth/login tests read the operator's real,
    # possibly-enabled 2fa.json, making the suite outcome host-dependent (Hermes #140).
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    return AuthConfig.from_env()


# opencode session ids in the fixture (≥1 top-level, 1 archived, 1 fork to skip,
# 1 ephemeral CI session to filter).
OC_TOP = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"
OC_ARCHIVED = "ses_bbbbbbbbbbbbbbbbbbbbbbbb"
OC_FORK = "ses_ffffffffffffffffffffffff"
OC_ACT = "ses_cccccccccccccccccccccccc"
# An ephemeral CI workdir (nektos/act), recorded under a HOME that differs from
# the test's tmp_home — proving the ``.cache``/``act`` component match catches it
# regardless of the runtime cache env (#452).
OC_ACT_DIR = "/home/ci-runner/.cache/act/deadbeef0001/hostexecutor"


@pytest.fixture
def opencode_db(tmp_home, monkeypatch) -> Path:
    """A minimal opencode SQLite DB (only the columns OpenCodeProvider reads).

    Two top-level sessions (one archived) + one fork (``parent_id`` set, must be
    skipped) + one ephemeral CI session (``OC_ACT``, an ``~/.cache/act`` workdir
    that must be filtered, #452). ``time_updated`` is epoch **milliseconds**, like
    the real DB.
    """
    db = tmp_home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT, "
        "time_created INTEGER, time_updated INTEGER, time_archived INTEGER)"
    )
    con.executemany(
        "INSERT INTO session "
        "(id, parent_id, directory, title, time_created, time_updated, time_archived) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            # time_created < time_updated (ms), like the real DB (#506).
            (OC_TOP, None, "/home/user/claude", "OC top one", 1777400000000, 1777460564154, None),
            (
                OC_ARCHIVED,
                None,
                "/tmp/other",
                "OC archived",
                1777200000000,
                1777300000000,
                1777400000000,
            ),
            (
                OC_FORK,
                OC_TOP,
                "/home/user/claude",
                "OC fork skip",
                1777460564000,
                1777460564999,
                None,
            ),
            (OC_ACT, None, OC_ACT_DIR, "OC ephemeral CI", 1777460565000, 1777460565000, None),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    return db
