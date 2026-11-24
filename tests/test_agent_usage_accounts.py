"""Every account's quota (#2331): each added account is asked on its own login, its figures are
kept under its own key, the engine row follows the account new sessions start on, and an alert
names whose quota it is.

No real CLI and no real login: the probes are fakes that answer by the login they run on, and the
account homes are temporary directories under the suite's pinned accounts dir.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sessions import agent_account_store, agent_accounts
from agent_sessions import agent_usage as au

FIXTURES = Path(__file__).parent / "fixtures"
BUDGETS = {"threshold_pct": 90, "notify": True, "engines": {}}
RESET = 1_900_000_000.0


@pytest.fixture(autouse=True)
def _own_accounts_dir(tmp_path, monkeypatch):
    """This file writes account stores (corrupt ones included). The suite's pinned accounts dir
    is shared by the whole run, so a store left there would refuse other files' launches: each
    test here gets its own."""
    monkeypatch.setenv(agent_accounts.ROOT_ENV, str(tmp_path / "accounts"))


RECIPE = agent_accounts.Recipe(
    kind="fixture-home",
    home_env="AGENT_HOME",
    default_home="~/.agent",
    private=("login.json",),
    shared_dirs=("sessions",),
    shared_files=(),
    clear_env=("AGENT_PLAIN_CLEARED",),
    login=("login",),
    status=("login", "status"),
)


def _pool(active: str = "default", ids: tuple[str, ...] = ("a-1", "a-2")) -> dict:
    return {
        "recipe": RECIPE,
        "active": active,
        "accounts": [{"id": i, "label": f"Label {i}"} for i in ids],
    }


def _plan(pct: float, *, email: str | None = None, label: str = "week") -> au.Report:
    return au.Report(
        engine="claude",
        source=au.SOURCE_PLAN,
        windows=[au.Window(label, pct, RESET)],
        at=time.time(),
        email=email,
    )


def _by_login() -> au.Report:
    """A reporter that answers by the login it runs on, as the real CLIs do."""
    acc = au._PROBE_ACCOUNT.get()
    if acc is None:
        return _plan(30.0, email="default@example.com")
    return _plan(70.0 if acc[1].name == "a-1" else 10.0, email=f"{acc[1].name}@example.com")


# --- the probe environment -----------------------------------------------------------------------


class _FakeProc:
    returncode = 0
    pid = 99999

    def __init__(self):
        import io

        self.stdout = io.BytesIO(b"")
        self.stdin = None

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _capture_env(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def fake_popen(argv, **kw):
        seen.append(dict(kw["env"]))
        return _FakeProc()

    monkeypatch.setattr(au.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(au.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(au.select, "select", lambda r, w, x, t: (r, [], []))
    monkeypatch.setattr(au, "_reap_group", lambda proc: None)
    return seen


def test_an_account_probe_runs_on_that_accounts_login(tmp_path, monkeypatch):
    """The environment every launch on the account gets: its home variable set, and a variable
    that would outrank its login removed (`agent_accounts.launch_env`)."""
    monkeypatch.setenv("AGENT_PLAIN_CLEARED", "would-outrank-the-login")
    seen = _capture_env(monkeypatch)
    home = tmp_path / "a-1"
    with au._account_scope(RECIPE, home):
        au._run(["/bin/true"])
    assert seen[0]["AGENT_HOME"] == str(home)
    assert "AGENT_PLAIN_CLEARED" not in seen[0]


def test_the_default_login_probe_environment_is_unchanged(monkeypatch):
    """No account scope: byte-identical to the environment before accounts existed."""
    monkeypatch.setenv("AGENT_PLAIN_CLEARED", "kept")
    monkeypatch.delenv("AGENT_HOME", raising=False)
    seen = _capture_env(monkeypatch)
    au._run(["/bin/true"])
    assert seen[0] == au.agent_env.scrubbed()
    assert "AGENT_HOME" not in seen[0]


def test_a_codex_account_probe_never_falls_back_to_the_shared_rollouts(tmp_path, monkeypatch):
    """Rollouts are in the store every account shares: the newest could be another login's."""
    monkeypatch.setattr(au, "_run", lambda argv, **kw: (1, "error: not logged in\n"))
    called = []
    monkeypatch.setattr(au, "read_codex_rate_limits", lambda *a, **k: called.append(1))
    with au._account_scope(RECIPE, tmp_path / "a-1"):
        rep = au.probe_codex(binary="/opt/codex", engine="codex")
    assert called == []
    assert rep.windows == []
    assert "not logged in" in rep.error


# --- identity ------------------------------------------------------------------------------------


def test_claude_auth_status_gives_the_email_and_the_plan():
    text = (FIXTURES / "claude-auth-status.json").read_text()
    assert au.parse_claude_auth_status(text) == ("op@example.com", "max_20x")


def test_a_logged_out_home_has_no_email():
    text = json.dumps({"loggedIn": False, "authMethod": "none"})
    assert au.parse_claude_auth_status(text) == (None, None)


@pytest.mark.parametrize(
    "value",
    [
        "not an address",
        "a@b c",
        "x" * 300 + "@example.com",
        "sk-ant-oat01-abcdef",
        ["op@example.com"],
        None,
    ],
)
def test_anything_not_address_shaped_is_dropped(value):
    assert au.clean_email(value) is None


def test_the_claude_probe_reports_who_answered(monkeypatch):
    def fake_run(argv, **kw):
        if "/usage" in argv:
            return 0, "Current week (all models): 12% used\n"
        return 0, (FIXTURES / "claude-auth-status.json").read_text()

    monkeypatch.setattr(au, "_run", fake_run)
    rep = au.probe_claude(binary="/opt/claude", engine="claude")
    assert rep.email == "op@example.com"
    assert rep.plan == "max_20x"
    assert [w.used_pct for w in rep.windows] == [12.0]


# --- the sweep -----------------------------------------------------------------------------------


@pytest.fixture
def two_accounts(monkeypatch):
    """claude holds a-1 (sound) and a-2 (a broken home), and reports by login."""
    monkeypatch.setitem(au.REPORTERS, "claude", _by_login)
    monkeypatch.setattr(au, "account_pools", lambda engines=None: {"claude": _pool()})
    monkeypatch.setattr(
        agent_accounts,
        "problems",
        lambda rec, home, shared: ["login.json: the login entry is not a regular file"]
        if home.name == "a-2"
        else [],
    )


def test_each_account_is_asked_on_its_own_login_and_kept_under_its_own_key(tmp_path, two_accounts):
    store = tmp_path / "usage.json"
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    reports = au.load(store)["reports"]
    assert reports["claude"]["windows"][0]["used_pct"] == 30.0
    assert reports["claude"]["email"] == "default@example.com"
    assert reports["claude@a-1"]["windows"][0]["used_pct"] == 70.0
    assert reports["claude@a-1"]["email"] == "a-1@example.com"
    # A home `problems()` rejects is never spawned in; its row says which entry, nothing more.
    assert reports["claude@a-2"]["windows"] == []
    assert reports["claude@a-2"]["error"].startswith("login.json")


def test_a_removed_accounts_figures_are_pruned_and_an_unread_pool_keeps_its_own(
    tmp_path, monkeypatch
):
    store = tmp_path / "usage.json"
    au.atomic_write_json(
        store,
        {
            "reports": {
                "claude@gone": _plan(50.0).as_dict(),
                "codex@kept": _plan(50.0).as_dict(),
            },
            "history": {"claude@gone": [{"t": time.time(), "w": []}]},
        },
    )
    monkeypatch.setitem(au.REPORTERS, "claude", _by_login)
    # codex's pool could not be read this time: absent from the pools, so nothing of it is dropped.
    monkeypatch.setattr(au, "account_pools", lambda engines=None: {"claude": _pool(ids=())})
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    doc = au.load(store)
    assert "claude@gone" not in doc["reports"]
    assert "claude@gone" not in doc.get("history", {})
    assert "codex@kept" in doc["reports"]


def test_a_failed_account_refresh_keeps_its_last_figures(tmp_path, two_accounts, monkeypatch):
    store = tmp_path / "usage.json"
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)

    def failing():
        if au._PROBE_ACCOUNT.get() is None:
            return _plan(31.0)
        return au.Report(engine="claude", source=au.SOURCE_PLAN, at=time.time() + 5, error="boom")

    monkeypatch.setitem(au.REPORTERS, "claude", failing)
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    a1 = au.load(store)["reports"]["claude@a-1"]
    assert a1["windows"][0]["used_pct"] == 70.0
    assert a1["error"] == "boom"


# --- rows ----------------------------------------------------------------------------------------


def _reports() -> dict:
    return {
        "claude": _plan(30.0, email="default@example.com").as_dict(),
        "claude@a-1": _plan(95.0, email="a-1@example.com", label="week (all models)").as_dict(),
    }


def test_the_engine_row_is_the_account_new_sessions_start_on():
    now = time.time()
    pools = {"claude": _pool(active="a-1")}
    rows = au.build_rows(_reports(), BUDGETS, now, pools=pools)
    row = next(r for r in rows if r["engine"] == "claude")
    assert row["account"] == {"id": "a-1", "label": "Label a-1", "email": "a-1@example.com"}
    assert row["used_pct"] == 95.0
    assert row["windows"][0]["label"] == "week (all models)"


def test_with_the_default_picked_the_engine_row_is_what_it_always_was():
    now = time.time()
    reports = _reports()
    with_pool = au.build_rows(reports, BUDGETS, now, pools={"claude": _pool()})
    without = au.build_rows(reports, BUDGETS, now)
    a = next(r for r in with_pool if r["engine"] == "claude")
    b = next(r for r in without if r["engine"] == "claude")
    assert a == b
    assert a["account"] == {
        "id": "default",
        "label": "Default login",
        "email": "default@example.com",
    }


def test_a_pick_change_switches_the_row_without_asking_anyone(monkeypatch):
    def nope():
        raise AssertionError("a read must never probe")

    monkeypatch.setitem(au.REPORTERS, "claude", nope)
    now = time.time()
    before = au.build_rows(_reports(), BUDGETS, now, pools={"claude": _pool()})
    after = au.build_rows(_reports(), BUDGETS, now, pools={"claude": _pool(active="a-1")})
    pct = lambda rows: next(r for r in rows if r["engine"] == "claude")["used_pct"]  # noqa: E731
    assert (pct(before), pct(after)) == (30.0, 95.0)


def test_every_account_has_a_row_and_one_is_active():
    now = time.time()
    out = au.build_account_rows(_reports(), BUDGETS, now, pools={"claude": _pool(active="a-1")})
    rows = out["claude"]
    assert [r["account"]["id"] for r in rows] == ["default", "a-1", "a-2"]
    assert [r["active"] for r in rows] == [False, True, False]
    # Never asked yet: "not measured", never 0 % and never the default login's counter.
    assert rows[2]["source"] == au.SOURCE_NONE
    assert rows[2]["used_pct"] is None


def test_an_account_not_asked_yet_never_borrows_the_default_logins_manual_count():
    budgets = {**BUDGETS, "engines": {"claude": {"limit_tokens": 1000, "manual_used": 900}}}
    rows = au.build_account_rows({}, budgets, time.time(), pools={"claude": _pool()})["claude"]
    assert rows[0]["source"] == au.SOURCE_MANUAL
    assert rows[1]["source"] == au.SOURCE_NONE


# --- alerts --------------------------------------------------------------------------------------


def test_the_default_logins_alert_key_is_unchanged():
    """Keys held before accounts existed must still match, or a deploy re-announces them."""
    row = {
        "engine": "claude",
        "account": {"id": "default", "label": "Default login", "email": None},
        "windows": [{"label": "week", "used_pct": 95.0, "resets_at": RESET}],
    }
    assert au.alert_key("claude", row, 90.0) == f"claude:week:{RESET}:90"
    legacy = {k: v for k, v in row.items() if k != "account"}
    assert au.alert_key("claude", legacy, 90.0) == f"claude:week:{RESET}:90"


def test_an_accounts_crossing_is_its_own_episode():
    now = time.time()
    rows = au.build_rows(_reports(), BUDGETS, now, pools={"claude": _pool(active="a-1")})
    alerts, _ = au.evaluate_alerts(rows, BUDGETS, [f"claude:week (all models):{RESET}:90"])
    keys = [a["key"] for a in alerts if a["engine"] == "claude"]
    # The default login's held key does not silence a-1: it is another account's quota.
    assert keys == [f"claude@a-1:week (all models):{RESET}:90"]


# --- the pool view -------------------------------------------------------------------------------


def test_an_agent_without_an_account_kind_never_reads_the_store(tmp_path, monkeypatch):
    agent_accounts.ensure_root()
    (agent_accounts.root() / "state.json").write_text("{ not json")  # would refuse if read
    plain = SimpleNamespace(engine_id="plain", manifest=SimpleNamespace(accounts_kind="none"))
    assert agent_account_store.usage_view(plain) is None


def test_an_unreadable_store_is_the_default_login_only(monkeypatch):
    agent_accounts.ensure_root()
    (agent_accounts.root() / "state.json").write_text("{ not json")
    prov = SimpleNamespace(
        engine_id="claude", manifest=SimpleNamespace(accounts_kind="claude-config-dir")
    )
    assert agent_account_store.usage_view(prov) is None


def test_a_pick_of_a_removed_account_reads_as_the_default(monkeypatch):
    agent_accounts.ensure_root()
    (agent_accounts.root() / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "engines": {"claude": {"accounts": [], "new_sessions": "a-gone"}},
                "assignments": {},
            }
        )
    )
    prov = SimpleNamespace(
        engine_id="claude", manifest=SimpleNamespace(accounts_kind="claude-config-dir")
    )
    assert agent_account_store.usage_view(prov)["active"] == "default"


# --- HQ ------------------------------------------------------------------------------------------


def test_no_account_email_reaches_hq(monkeypatch):
    """HQ sees labels only (#1820): its rows are built from named fields, and the email is not
    one of them."""
    from agent_sessions import ask_orchestrator

    now = time.time()
    rows = au.build_rows(_reports(), BUDGETS, now, pools={"claude": _pool(active="a-1")})
    monkeypatch.setattr(au, "snapshot", lambda **kw: rows)
    out = json.dumps(ask_orchestrator._usage_rows())
    assert "example.com" not in out
    assert "Label a-1" not in out


# --- the bell ------------------------------------------------------------------------------------


def test_the_notification_names_the_agent_the_quota_and_the_account(monkeypatch):
    from agent_sessions import usage_loop

    monkeypatch.setattr(usage_loop, "_agent_label", lambda engine: "Claude Code")
    row = {
        "engine": "claude",
        "account": {"id": "a-1", "label": "Personal", "email": "op@example.com"},
        "windows": [
            {"label": "session", "used_pct": 12.0, "resets_at": RESET},
            {"label": "week (all models)", "used_pct": 91.0, "resets_at": RESET},
            {"label": "week (Fable)", "used_pct": 42.0, "resets_at": RESET},
        ],
    }
    alert = {"engine": "claude", "used_pct": 91.0, "level": 90.0, "row": row}
    title, reason = usage_loop.alert_text(alert, BUDGETS)
    assert title == "Claude Code: week (all models) at 91%"
    assert reason == "op@example.com · past your 90% threshold · session 12%, week (Fable) 42%"

    used_up = {**alert, "used_pct": 100.0, "level": 100.0}
    row["windows"][1]["used_pct"] = 100.0
    title, reason = usage_loop.alert_text(used_up, BUDGETS)
    assert title == "Claude Code: week (all models) used up (100%)"
    assert reason.startswith("op@example.com · back after the reset")


def test_the_default_logins_notification_says_so(monkeypatch):
    from agent_sessions import usage_loop

    monkeypatch.setattr(usage_loop, "_agent_label", lambda engine: "Codex")
    row = {
        "engine": "codex",
        "account": {"id": "default", "label": "Default login", "email": None},
        "windows": [{"label": "week", "used_pct": 95.0, "resets_at": RESET}],
    }
    title, reason = usage_loop.alert_text(
        {"engine": "codex", "used_pct": 95.0, "level": 90.0, "row": row}, BUDGETS
    )
    assert title == "Codex: week at 95%"
    assert reason == "Default login · past your 90% threshold"
    row["account"]["email"] = "op@example.com"
    _, reason = usage_loop.alert_text(
        {"engine": "codex", "used_pct": 95.0, "level": 90.0, "row": row}, BUDGETS
    )
    assert reason.startswith("Default login (op@example.com)")


@pytest.mark.parametrize("hot", ["a-1", "default"])
def test_switching_the_pick_away_and_back_does_not_re_announce(tmp_path, monkeypatch, hot):
    """Hermes on #2340: choosing an account is not a reset. An episode delivered while its account
    was active stays held while another account is picked, so picking it again announces nothing —
    even with its bell row gone. Covers an added account and the default login's own keys."""
    other = "default" if hot == "a-1" else "a-1"
    state = {"active": hot}

    def report():
        acc = au._PROBE_ACCOUNT.get()
        who = "default" if acc is None else acc[1].name
        return _plan(95.0 if who == hot else 10.0)

    monkeypatch.setitem(au.REPORTERS, "claude", report)
    monkeypatch.setattr(
        au,
        "account_pools",
        lambda engines=None: {"claude": _pool(active=state["active"], ids=("a-1",))},
    )
    monkeypatch.setattr(agent_accounts, "problems", lambda rec, home, shared: [])
    store = tmp_path / "usage.json"

    first = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    keys = [a["key"] for a in first["alerts"] if a["engine"] == "claude"]
    assert len(keys) == 1
    assert keys[0].startswith("claude@a-1:" if hot == "a-1" else "claude:")
    au.mark_announced(keys, path=store)

    state["active"] = other
    away = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert [a for a in away["alerts"] if a["engine"] == "claude"] == []
    assert keys[0] in au.load(store)["alerted"]  # still held while not picked

    state["active"] = hot
    back = au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert [a for a in back["alerts"] if a["engine"] == "claude"] == []


def test_an_idle_accounts_held_episode_ends_when_its_own_quota_retreats(tmp_path, monkeypatch):
    """Held, not frozen: an idle account's key re-arms when ITS figures fall past the band."""
    pct = {"a-1": 95.0}

    def report():
        acc = au._PROBE_ACCOUNT.get()
        return _plan(10.0 if acc is None else pct["a-1"])

    state = {"active": "a-1"}
    monkeypatch.setitem(au.REPORTERS, "claude", report)
    monkeypatch.setattr(
        au,
        "account_pools",
        lambda engines=None: {"claude": _pool(active=state["active"], ids=("a-1",))},
    )
    monkeypatch.setattr(agent_accounts, "problems", lambda rec, home, shared: [])
    store = tmp_path / "usage.json"
    keys = [a["key"] for a in au.refresh(path=store, engines=["claude"], budgets=BUDGETS)["alerts"]]
    au.mark_announced(keys, path=store)
    state["active"] = "default"
    pct["a-1"] = 50.0
    au.refresh(path=store, engines=["claude"], budgets=BUDGETS)
    assert not [k for k in au.load(store)["alerted"] if k.startswith("claude@a-1:")]
