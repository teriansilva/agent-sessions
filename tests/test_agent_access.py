"""Account access per agent (#1167): "no access" comes from the agent, never a guess.

Only a RECOGNISED refusal is denied; everything indeterminate is unknown and changes nothing, so a
network blip can never read as "no access". Fixtures are recorded from gemini-cli 0.60.0 and
kimi-code 2.0.2 on the host where both accounts were refused.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_sessions import agent_usage as au
from agent_sessions import engines

FIXTURES = Path(__file__).parent / "fixtures"
REFUSED = (FIXTURES / "gemini-acp-auth-refused.jsonl").read_text()
INIT_OK = REFUSED.splitlines()[0]
BUDGETS = {"threshold_pct": 90, "notify": True, "engines": {}}


# --- manifests ----------------------------------------------------------------------------------


def test_gemini_and_kimi_declare_their_access_check():
    assert engines.manifest_of("gemini").usage.access == "gemini-acp-auth"
    assert engines.manifest_of("kimi").usage.access == "kimi-wire-auth-error"
    assert set(au.ACCESS_CHECKS) == {"gemini", "kimi"}
    # Access is independent of the source: gemini is manual-only for quota, while kimi's quota is
    # asked through `kimi web` (#1239) and its access verdict is still the separate wire read.
    assert "gemini" not in au.REPORTERS and "kimi" in au.REPORTERS
    assert engines.manifest_of("kimi").usage.kind == "kimi-web-usage-probe"


# --- gemini: denied / ok / unknown --------------------------------------------------------------


def test_gemini_recorded_refusal_is_denied():
    a = au.parse_gemini_acp_auth(REFUSED, now=100.0)
    assert a.state == au.ACCESS_DENIED
    assert a.message.startswith("This client is no longer supported")
    assert a.observed_at == 100.0


def test_gemini_successful_authenticate_is_ok():
    a = au.parse_gemini_acp_auth(INIT_OK + '\n{"jsonrpc":"2.0","id":2,"result":{}}\n', now=5.0)
    assert (a.state, a.observed_at) == (au.ACCESS_OK, 5.0)


@pytest.mark.parametrize(
    "text",
    [
        "",  # no reply at all (timeout, missing binary)
        "not json\n",
        '{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"unsupported protocol"}}\n',
        INIT_OK + "\n",  # initialize answered, authenticate never
        INIT_OK + '\n{"jsonrpc":"2.0","id":2,"error":{"code":-32603,"message":"fetch failed"}}\n',
        INIT_OK + '\n{"jsonrpc":"2.0","id":2,"error":{"code":-32000}}\n',
    ],
)
def test_gemini_anything_but_a_recognised_refusal_is_unknown(text):
    a = au.parse_gemini_acp_auth(text)
    assert a.state is None
    assert a.error


def test_the_gemini_check_sends_only_initialize_and_authenticate(monkeypatch):
    """Never `session/new`, never a prompt — pinned on the exact bytes handed to the CLI."""
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return 0, REFUSED

    monkeypatch.setattr(au, "_run", fake_run)
    a = au.check_gemini_access(binary="/opt/gemini")
    assert seen["argv"] == ["/opt/gemini", "--acp"]
    msgs = [json.loads(line) for line in seen["send"].decode().splitlines()]
    assert [m["method"] for m in msgs] == ["initialize", "authenticate"]
    assert msgs[1]["params"] == {"methodId": "oauth-personal"}
    # `done` waits for BOTH replies: gemini can answer authenticate first (measured).
    auth_only = REFUSED.splitlines()[1].encode()
    assert not seen["done"](auth_only)
    assert seen["done"](REFUSED.encode())
    assert a.state == au.ACCESS_DENIED


def test_a_gemini_check_without_a_binary_is_unknown(monkeypatch):
    monkeypatch.setattr(au, "_probe_binary", lambda engine: None)
    monkeypatch.setattr(au, "_run", lambda *a, **k: pytest.fail("must not spawn"))
    assert au.check_gemini_access().state is None


# --- kimi: read its own record ------------------------------------------------------------------


def _wire(home: Path, name: str, *events: dict, mtime: float | None = None) -> Path:
    p = home / ".kimi-code" / "sessions" / "wd_x" / f"session_{name}" / "agents" / "main"
    p.mkdir(parents=True, exist_ok=True)
    f = p / "wire.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in events))
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def _ended(t_ms: int, reason: str = "completed", code: str | None = None) -> dict:
    ev: dict = {"type": "turn.ended", "agentId": "main", "turnId": 0, "reason": reason}
    if code:
        ev["error"] = {"code": code, "message": f"{code}: 403 no access to Kimi Code"}
    ev["time"] = t_ms
    return ev


def test_kimi_latest_auth_error_is_denied_with_its_own_time(tmp_path):
    _wire(
        tmp_path,
        "a",
        _ended(1_790_000_000_000),
        _ended(1_790_000_500_000, "failed", "provider.auth_error"),
    )
    a = au.check_kimi_access(tmp_path)
    assert a.state == au.ACCESS_DENIED
    assert "no access" in a.message
    assert a.observed_at == 1_790_000_500.0  # kimi's milliseconds, in seconds


def test_kimi_a_completed_turn_after_the_refusal_clears_it(tmp_path):
    _wire(
        tmp_path,
        "a",
        _ended(1_790_000_000_000, "failed", "provider.auth_error"),
        _ended(1_790_000_900_000),
    )
    assert au.check_kimi_access(tmp_path).state == au.ACCESS_OK


@pytest.mark.parametrize(
    "later",
    [
        _ended(1_790_000_900_000, "failed", "provider.network_error"),
        _ended(1_790_000_900_000, "cancelled"),
    ],
)
def test_kimi_a_later_non_auth_failure_is_not_a_recovery(tmp_path, later):
    _wire(tmp_path, "a", _ended(1_790_000_000_000, "failed", "provider.auth_error"), later)
    assert au.check_kimi_access(tmp_path).state is None


def test_kimi_orders_across_files_by_EVENT_time_not_file_order(tmp_path):
    # The file touched LAST holds the OLDER event; the refusal in the other file is newer.
    _wire(
        tmp_path,
        "new-refusal",
        _ended(1_790_000_900_000, "failed", "provider.auth_error"),
        mtime=1000,
    )
    _wire(tmp_path, "old-ok", _ended(1_790_000_000_000), mtime=2000)
    assert au.check_kimi_access(tmp_path).state == au.ACCESS_DENIED


def test_kimi_reads_a_bounded_tail_and_drops_the_partial_line(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "KIMI_TAIL_BYTES", 400)
    filler = {"type": "context.append_message", "text": "x" * 2000}
    f = _wire(
        tmp_path,
        "a",
        _ended(1_790_000_000_000, "failed", "provider.auth_error"),
        filler,
        _ended(1_790_000_900_000),
    )
    assert f.stat().st_size > 400
    # Only the tail is read: the refusal before the filler is out of range; the last turn decides.
    assert au.check_kimi_access(tmp_path).state == au.ACCESS_OK


def test_kimi_with_no_finished_turn_is_unknown(tmp_path):
    _wire(tmp_path, "a", {"type": "metadata"})
    assert au.check_kimi_access(tmp_path).state is None
    assert au.check_kimi_access(tmp_path / "nowhere").state is None


# --- refresh: persisted, merged, independent of quota -------------------------------------------


def _set_checks(monkeypatch, **states):
    for eng, acc in states.items():
        monkeypatch.setitem(au.ACCESS_CHECKS, eng, (lambda a=acc: a))


def _access_row(store: Path, engine: str) -> dict | None:
    doc = au.load(store)
    rows = au.build_rows(doc.get("reports") or {}, BUDGETS, time.time(), doc.get("access") or {})
    return next(r for r in rows if r["engine"] == engine)["access"]


def test_access_transitions_denied_unknown_ok_and_survive_a_restart(tmp_path, monkeypatch):
    store = tmp_path / "usage.json"
    monkeypatch.setattr(au, "REPORTERS", {})
    _set_checks(monkeypatch, gemini=au.Access(au.ACCESS_DENIED, "no longer supported", 10.0))
    au.refresh(path=store, engines=["gemini"], budgets=BUDGETS)
    assert _access_row(store, "gemini")["state"] == "denied"

    # A check that cannot tell keeps the denial (an access-only engine: no figures at all).
    _set_checks(monkeypatch, gemini=au.Access(None, error="timed out"))
    au.refresh(path=store, engines=["gemini"], budgets=BUDGETS)
    row = _access_row(store, "gemini")
    assert (row["state"], row["message"], row["observed_at"]) == (
        "denied",
        "no longer supported",
        10.0,
    )

    # Restart: a fresh read of the file serves the same verdict.
    assert (
        au.snapshot(path=store, budgets=BUDGETS)[
            [r["engine"] for r in au.snapshot(path=store, budgets=BUDGETS)].index("gemini")
        ]["access"]["state"]
        == "denied"
    )

    _set_checks(monkeypatch, gemini=au.Access(au.ACCESS_OK, observed_at=20.0))
    au.refresh(path=store, engines=["gemini"], budgets=BUDGETS)
    row = _access_row(store, "gemini")
    assert (row["state"], row["message"]) == ("ok", None)


def test_a_passing_access_check_clears_a_denial_even_when_the_quota_read_failed(
    tmp_path, monkeypatch
):
    store = tmp_path / "usage.json"
    monkeypatch.setattr(
        au,
        "REPORTERS",
        {"gemini": lambda: au.Report(engine="gemini", source=au.SOURCE_PLAN, at=1.0, error="boom")},
    )
    _set_checks(monkeypatch, gemini=au.Access(au.ACCESS_DENIED, "refused", 1.0))
    au.refresh(path=store, engines=["gemini"], budgets=BUDGETS)
    _set_checks(monkeypatch, gemini=au.Access(au.ACCESS_OK, observed_at=2.0))
    au.refresh(path=store, engines=["gemini"], budgets=BUDGETS)
    assert _access_row(store, "gemini")["state"] == "ok"


def test_an_access_check_that_raises_is_unknown_not_denied(tmp_path, monkeypatch):
    store = tmp_path / "usage.json"
    monkeypatch.setattr(au, "REPORTERS", {})

    def boom():
        raise RuntimeError("vendor exploded")

    monkeypatch.setitem(au.ACCESS_CHECKS, "kimi", boom)
    out = au.refresh(path=store, engines=["kimi"], budgets=BUDGETS)
    assert out["access"] == {"kimi": None}
    assert _access_row(store, "kimi") is None


def test_the_served_access_block_is_normalized():
    rows = au.build_rows(
        {},
        BUDGETS,
        time.time(),
        {"kimi": {"state": "denied", "message": "x" * 5000, "observed_at": "nope", "junk": 1}},
    )
    acc = next(r for r in rows if r["engine"] == "kimi")["access"]
    assert set(acc) == {"state", "message", "observed_at", "checked_at"}
    assert len(acc["message"]) == au.ACCESS_MESSAGE_MAX
    assert acc["message"].endswith("…")  # cut, and SAYS it was cut
    assert acc["observed_at"] is None
    assert next(r for r in rows if r["engine"] == "claude")["access"] is None


def test_a_refusal_longer_than_the_bound_is_an_excerpt_marked_as_cut():
    long = "This client is no longer supported. " + "Please migrate. " * 30
    a = au.parse_gemini_acp_auth(
        INIT_OK
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": long}})
        + "\n"
    )
    assert a.state == au.ACCESS_DENIED
    assert len(a.message) == au.ACCESS_MESSAGE_MAX and a.message.endswith("…")
    short = au.parse_gemini_acp_auth(REFUSED)
    assert not short.message.endswith("…")


def test_kimi_tail_read_stays_bounded_while_the_file_grows(tmp_path, monkeypatch):
    """Hermes on #1173: kimi appends to its wire files while we read them. Model an append that
    lands the moment the reader has the file open — whatever it measured before, it must not read
    more than ``cap`` bytes."""
    cap = 4096
    f = tmp_path / "wire.jsonl"
    f.write_bytes(b"old\n")
    real_open = Path.open

    def open_then_append(self, *a, **k):
        fh = real_open(self, *a, **k)
        if self == f:
            with real_open(f, "ab") as w:
                w.write((b"x" * 100 + b"\n") * (2 * cap // 101 + 1))
        return fh

    # `context()`, never `undo()`: undo would also drop conftest's store-path pins.
    with monkeypatch.context() as m:
        m.setattr(Path, "open", open_then_append)
        lines = au._tail_lines(f, cap)
    assert f.stat().st_size > 2 * cap  # the file really did grow past the bound
    # Line bytes plus the newlines BETWEEN them: what was actually read, at most `cap`.
    assert sum(len(line) for line in lines) + max(0, len(lines) - 1) <= cap
