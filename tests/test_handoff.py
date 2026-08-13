"""Cross-engine handoff (#597, Phases 1–2).

Pins the issue's acceptance contract: seed transport (never argv; PTY bracketed paste,
atomic single redemption), the prepare/commit lifecycle (side-effect-free prepare,
handle TTL + bind, double-commit refused), the capability matrix (shell excluded,
gemini/antigravity Phase 3, same-engine allowed, one capability source for UI + server),
source validation before any transcript read, and the provenance state machine
(aliveness gate; no dangling link on spawn failure; backlink only on the resolved real
id; reconcile fail-safe preserved).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import discover, engines, handoff, metadata
from agent_sessions.main import create_app

_SRC = "11111111-1111-1111-1111-111111111111"  # fake_jsonl session in /home/user/claude/repo-a


@pytest.fixture(autouse=True)
def _reset_handoff():
    handoff.reset_for_tests()
    yield
    handoff.reset_for_tests()


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def _hdr(csrf, cfg):
    return {"X-CSRF-Token": csrf, "Origin": cfg.origin}


def _present_all(monkeypatch):
    """Pretend every engine binary is installed (capability tests isolate the flag logic)."""
    monkeypatch.setattr(discover, "resolve", lambda e: f"/usr/bin/{e}")


# ---- seed builder ---------------------------------------------------------------------------


def test_quick_seed_is_engine_neutral_and_carries_the_tail(fake_jsonl):
    seed, meta = handoff.build_quick_seed(
        "claude", _SRC, title="first message on repo-a", cwd="/home/user/claude/repo-a"
    )
    assert "[user] first message on repo-a" in seed
    assert "claude session" in seed  # provenance labelled
    assert meta["mode"] == "quick" and meta["turns"] == 1
    assert meta["bytes"] == len(seed.encode()) and meta["cap"] == handoff.SEED_CAP_BYTES


def test_quick_seed_empty_transcript_raises_409(fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: []))
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.build_quick_seed("claude", _SRC)
    assert ei.value.status == 409


def test_quick_seed_caps_long_transcripts(fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    turns = [transcript.Turn(role="user", text="x" * 1024, kind="text") for _ in range(40)]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seed, meta = handoff.build_quick_seed("claude", _SRC)
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES
    assert meta["turns"] <= handoff.SEED_MAX_TURNS


def test_quick_seed_single_oversized_turn_is_truncated(fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    turns = [transcript.Turn(role="user", text="y" * (handoff.SEED_CAP_BYTES * 2), kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seed, meta = handoff.build_quick_seed("claude", _SRC)
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES + 8  # ellipsis slack
    assert meta["turns"] == 1


def test_quick_seed_strips_control_bytes_paste_breakout(fake_jsonl, monkeypatch):
    # An ESC in transcript content could terminate the bracketed paste early and smuggle
    # raw key input into the target agent — the builder strips every control byte.
    from agent_sessions import transcript

    evil = "before \x1b[201~\x1b[5;5H rm -rf \x07 after"
    turns = [transcript.Turn(role="user", text=evil, kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seed, _ = handoff.build_quick_seed("claude", _SRC)
    assert "\x1b" not in seed and "\x07" not in seed
    assert "before" in seed and "after" in seed


# ---- provenance must never evict the payload (#718) ------------------------------------------


def _nested_path(nbytes: int) -> str:
    """A filesystem-plausible path of about ``nbytes`` bytes — long, but nothing a real
    checkout under a deeply nested home could not produce."""
    seg = "/verylongdirectorysegmentname"
    return (seg * (nbytes // len(seg) + 1))[:nbytes]


def test_quick_keeps_the_newest_turn_when_both_paths_are_enormous(fake_jsonl, monkeypatch):
    """4089-byte `cwd` AND locator used to consume the whole 8192-byte cap: `_cap` keeps the
    PREFIX, so the seed arrived with no `## Recent turns` and no turn in it at all — while
    `meta.turns` still said 1."""
    from agent_sessions import transcript

    turns = [transcript.Turn(role="user", text="THE NEWEST TURN", kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    monkeypatch.setitem(transcript._LOCATORS, "claude", lambda native, home: _nested_path(4089))

    seed, meta = handoff.build_quick_seed(
        "claude", _SRC, cwd=_nested_path(4089), include_source_ref=True
    )
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES
    assert "## Recent turns" in seed
    assert "THE NEWEST TURN" in seed
    assert meta["turns"] == 1
    # Provenance is still THERE — bounded, not dropped — and visibly shortened.
    assert "- workdir:" in seed and "- transcript:" in seed
    assert handoff.PATH_TRUNC_MARKER in seed


def _head_value(seed: str, label: str) -> str:
    """The value on the `- <label>: ` line, or `""` when the line is absent."""
    for line in seed.splitlines():
        if line.startswith(f"- {label}: "):
            return line[len(f"- {label}: ") :]
    return ""


def test_a_long_workdir_cannot_starve_the_transcript_locator(fake_jsonl, monkeypatch):
    """Serving `workdir` greedily out of the shared budget left the locator 0 bytes, so the
    document carried a bare `- transcript: ` **and** the line telling the target agent the
    transcript is "at the location above" — defeating `include_source_ref` in exactly the
    long-path case this budget exists for (Hermes on #811)."""
    from agent_sessions import transcript

    monkeypatch.setattr(
        transcript,
        "adapter_for",
        lambda e: (lambda native, home: [transcript.Turn(role="user", text="hi", kind="text")]),
    )
    loc = _nested_path(6000) + "/the-real-transcript.jsonl"
    monkeypatch.setitem(transcript._LOCATORS, "claude", lambda native, home: loc)

    seed, _ = handoff.build_quick_seed(
        "claude", _SRC, cwd=_nested_path(6000), include_source_ref=True
    )
    value = _head_value(seed, "transcript")
    assert value, "the transcript line was emitted with no location"
    assert value != handoff.PATH_TRUNC_MARKER, "the location is only the truncation marker"
    # …and it is the INFORMATIVE end, which is the whole point of keeping the tail.
    assert value.endswith("/the-real-transcript.jsonl")
    # The workdir did not vanish either — both are bounded, neither is starved.
    assert _head_value(seed, "workdir")


def test_a_locator_that_cannot_be_rendered_drops_its_line_and_its_guidance(monkeypatch):
    """An empty label is worse than an absent one: the "read it only if you need more context"
    guidance would point at a location that is not there.

    Asserted on `_head_lines` rather than through a built seed on purpose — with the budget
    split in place, a cap small enough to starve the locator is also small enough for `_cap` to
    remove the line anyway, so a seed-level assertion would pass for the wrong reason.
    """
    monkeypatch.setattr(handoff, "_head_provenance_budget", lambda: 4)  # < marker + a tail
    head = "\n".join(
        handoff._head_lines(
            "claude", _SRC, "", _nested_path(6000), transcript_loc=_nested_path(6000)
        )
    )
    assert "- transcript:" not in head
    assert "location above" not in head
    # The same rule applies to workdir — neither is ever emitted as a bare label.
    assert "- workdir:" not in head


def test_no_provenance_line_is_ever_emitted_empty(monkeypatch):
    """Sweep the budget across the boundary where each value stops being renderable: at every
    size, a `- workdir:` / `- transcript:` line that EXISTS carries something after the colon."""
    for budget in range(0, 40):
        monkeypatch.setattr(handoff, "_head_provenance_budget", lambda b=budget: b)
        lines = handoff._head_lines(
            "claude", _SRC, "", _nested_path(500), transcript_loc=_nested_path(500)
        )
        for label in ("workdir", "transcript"):
            assert f"- {label}: " not in lines, f"empty {label} line at budget={budget}"


def test_a_long_path_keeps_its_informative_tail(fake_jsonl, monkeypatch):
    """Shortening keeps the END of a path: the repo and file are what identify it, the head is
    usually a shared prefix."""
    from agent_sessions import transcript

    monkeypatch.setattr(
        transcript,
        "adapter_for",
        lambda e: (lambda native, home: [transcript.Turn(role="user", text="hi", kind="text")]),
    )
    cwd = _nested_path(4000) + "/the-actual-repo"
    seed, _ = handoff.build_quick_seed("claude", _SRC, cwd=cwd)
    assert "/the-actual-repo" in seed


def test_the_header_can_never_take_more_than_its_share_of_the_cap(fake_jsonl, monkeypatch):
    """The bound is a FRACTION of the cap, so it holds at an operator-tuned cap too."""
    from agent_sessions import transcript

    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 1024)
    turns = [transcript.Turn(role="user", text="PAYLOAD", kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    monkeypatch.setitem(transcript._LOCATORS, "claude", lambda native, home: _nested_path(9000))

    seed, meta = handoff.build_quick_seed(
        "claude", _SRC, cwd=_nested_path(9000), include_source_ref=True
    )
    assert len(seed.encode()) <= 1024
    assert "PAYLOAD" in seed and meta["turns"] == 1


def test_meta_turns_counts_what_was_rendered_not_what_was_selected(fake_jsonl, monkeypatch):
    """`meta.turns` is the reader's only signal that a seed is short, so it must describe the
    document that exists — not the tail the builder started from.

    The trim loop stops at ONE turn however small the cap is, so at the ``MIN_CAP_BYTES`` floor
    the fixed header (261 bytes here) already exceeds the cap and `_cap` cuts the body away
    entirely. That used to be reported as `turns: 1` — a document claiming to carry a turn it
    does not contain.
    """
    from agent_sessions import transcript

    turns = [transcript.Turn(role="user", text="THE ONLY TURN", kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", handoff.MIN_CAP_BYTES)  # 256 < 261

    seed, meta = handoff.build_quick_seed("claude", _SRC)
    assert "THE ONLY TURN" not in seed  # there is genuinely no room for it
    assert meta["turns"] == 0, "meta claimed a turn the capped document does not carry"


# ---- capability matrix ------------------------------------------------------------------------


def test_seed_start_capability_matrix():
    cases = {
        "claude": (True, None),
        "codex": (True, None),
        "opencode": (True, None),
        "gemini": (False, "no seed-capable start yet"),
        "antigravity": (False, "no seed-capable start yet"),
        "shell": (False, "not an agent engine"),
    }
    for engine_id, expected in cases.items():
        prov = engines.get(engine_id)
        assert handoff.seed_start_state(prov, present=True) == expected, engine_id
    # An uninstalled but otherwise capable engine is refused with its own reason.
    assert handoff.seed_start_state(engines.get("claude"), present=False) == (
        False,
        "not installed",
    )


def test_api_engines_carries_the_capability_and_reason(auth_cfg, tmp_home, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = {r["id"]: r for r in c.get("/api/engines").json()["engines"]}
    assert rows["claude"]["supports_seed_start"] is True
    assert rows["claude"]["seed_reason"] is None
    # kimi is a seed-capable target since #720 Phase 3 (bracketed-paste readiness proven).
    assert rows["kimi"]["supports_seed_start"] is True
    assert rows["kimi"]["seed_reason"] is None
    assert rows["gemini"]["supports_seed_start"] is False
    assert rows["gemini"]["seed_reason"] == "no seed-capable start yet"
    assert rows["shell"]["supports_seed_start"] is False
    assert rows["shell"]["seed_reason"] == "not an agent engine"


# ---- transport: the seed never touches argv ----------------------------------------------------


def test_seed_never_appears_in_any_launch_argv(fake_jsonl):
    sentinel = "first message on repo-a"  # the seed body's distinctive content
    seed, _ = handoff.build_quick_seed("claude", _SRC, title=sentinel)
    assert sentinel in seed
    for engine_id in ("claude", "codex", "opencode"):
        prov = engines.get(engine_id)
        argv = prov.new_launch_argv(
            "new-99999999-9999-9999-9999-999999999999"
            if getattr(prov, "new_session_reconciles", False)
            else _SRC,
            cwd="/tmp",
            bypass=True,
        )
        joined = "\x00".join(argv)
        assert sentinel not in joined and "Handoff" not in joined


# ---- handle store lifecycle -------------------------------------------------------------------


def test_handle_commit_binds_and_double_commit_409():
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "seed body", cwd="/tmp")
    res = handoff.commit(h)
    assert res["engine"] == "claude" and res["cwd"] == "/tmp"
    assert res["id"] == f"claude:{res['native']}"
    assert not res["native"].startswith("new-")  # pinned-id engine
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.commit(h)
    assert ei.value.status == 409


def test_handle_commit_mints_placeholder_for_reconciling_engines():
    h = handoff.create_handle("claude:" + _SRC, "codex", "quick", "seed body", cwd="/tmp")
    res = handoff.commit(h)
    assert res["native"].startswith("new-")  # codex mints its own id → placeholder launch


def test_handle_commit_mints_placeholder_for_kimi_target():
    # kimi has no id-pinning flag (new_session_reconciles), so a handoff to it launches under a
    # ``new-<uuid>`` placeholder and reconciles — same as codex (#720 Phase 3).
    h = handoff.create_handle("claude:" + _SRC, "kimi", "quick", "seed body", cwd="/tmp")
    res = handoff.commit(h)
    assert res["engine"] == "kimi"
    assert res["native"].startswith("new-")


def test_seed_claim_ack_is_single_delivery():
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "the seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    assert handoff.has_pending_seed(key) is True
    # Claimants are serialized: while a claim is outstanding, a second viewer gets None.
    assert handoff.claim_seed(key) == "the seed"
    assert handoff.claim_seed(key) is None
    # A retry-ack releases the claim WITHOUT consuming — the seed survives for a retry.
    handoff.ack_seed(key, "retry")
    assert handoff.has_pending_seed(key) is True
    assert handoff.claim_seed(key) == "the seed"
    # A delivered-ack consumes it — the single-delivery guarantee.
    handoff.ack_seed(key, "delivered")
    assert handoff.claim_seed(key) is None
    assert handoff.has_pending_seed(key) is False


def test_seed_abort_ack_consumes_without_retry():
    # A partial PTY write polluted the target's input — the claim is settled as consumed
    # (never retried blindly), which is explicit and logged at the delivery layer.
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "partial", cwd="/tmp")
    key = handoff.commit(h)["id"]
    assert handoff.claim_seed(key) == "partial"
    handoff.ack_seed(key, "abort")
    assert handoff.has_pending_seed(key) is False
    assert handoff.claim_seed(key) is None


def test_handle_expires_on_ttl(monkeypatch):
    monkeypatch.setattr(handoff, "HANDLE_TTL_S", 0.05)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "seed", cwd="/tmp")
    time.sleep(0.1)
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.commit(h)
    assert ei.value.status == 404


def test_unknown_handle_404():
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.commit("nope")
    assert ei.value.status == 404


# ---- provenance state machine ------------------------------------------------------------------


def test_mark_spawned_pinned_id_writes_both_sides(tmp_home):
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    handoff.mark_spawned(key)
    handoff.mark_spawned(key)  # idempotent — a replay writes nothing twice
    assert metadata.get(key).handoff_from == src
    assert metadata.get(key).handoff_mode == "quick"
    assert metadata.get(key).handoff_at != ""
    assert metadata.get(src).handoff_to == key
    # PR #701 review P1: provenance publication must NOT evict the still-unredeemed seed —
    # the aliveness gate (8 s) can beat the injector's readiness wait (up to 45 s).
    assert handoff.has_pending_seed(key) is True
    assert handoff.claim_seed(key) == "seed"
    handoff.ack_seed(key, "delivered")
    # Only now — seed consumed AND provenance+backlink written — is the entry released.
    assert handoff.has_pending_seed(key) is False
    assert handoff.arm_watch(key) is False


def test_mark_spawned_placeholder_defers_backlink_until_reconcile(tmp_home):
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "codex", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]  # codex:new-<uuid>
    handoff.mark_spawned(key)
    assert metadata.get(key).handoff_from == src
    # Backlink absent until the REAL id exists — never a placeholder backlink.
    assert metadata.get(src).handoff_to == ""
    real = "codex:99999999-9999-9999-9999-999999999999"
    handoff.note_reconciled(key, real)
    assert metadata.get(src).handoff_to == real
    # P1: spawn + reconcile both done, but the seed is still unredeemed — it survives.
    assert handoff.claim_seed(key) == "seed"
    handoff.ack_seed(key, "delivered")
    assert handoff.has_pending_seed(key) is False


def test_reconcile_before_spawn_watch_still_backlinks_real_id(tmp_home):
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "codex", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    real = "codex:88888888-8888-8888-8888-888888888888"
    handoff.note_reconciled(key, real)  # reconcile wins the race
    assert metadata.get(src).handoff_to == ""  # still gated on aliveness
    handoff.mark_spawned(key)
    assert metadata.get(key).handoff_from == src
    assert metadata.get(src).handoff_to == real


def test_abort_spawn_leaves_no_dangling_link(tmp_home):
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    handoff.abort_spawn(key)
    assert metadata.get(key).handoff_from == ""
    assert metadata.get(src).handoff_to == ""
    assert handoff.has_pending_seed(key) is False
    handoff.mark_spawned(key)  # post-abort replay is a no-op, not a resurrection
    assert metadata.get(key).handoff_from == ""


def test_note_reconciled_without_handoff_is_a_noop(tmp_home):
    # The reconcile coroutine calls this for EVERY placeholder converge; a plain
    # picker-started session (no handoff) must never gain provenance.
    handoff.note_reconciled("codex:new-77777777-7777-7777-7777-777777777777", "codex:" + _SRC)
    assert metadata.get("codex:" + _SRC).handoff_to == ""


def test_arm_watch_is_single_shot():
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    assert handoff.arm_watch(key) is True
    assert handoff.arm_watch(key) is False  # a ws reconnect never arms a second watch
    assert handoff.arm_watch("claude:99999999-9999-9999-9999-999999999999") is False


# ---- routes: prepare ---------------------------------------------------------------------------


def _prepare(c, csrf, cfg, **over):
    body = {"source_id": f"claude:{_SRC}", "target_engine": "codex", "mode": "quick"}
    body.update(over)
    return c.post("/api/handoff/prepare", json=body, headers=_hdr(csrf, cfg))


def test_prepare_returns_handle_preview_meta(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["handle"]
    assert "[user] first message on repo-a" in body["preview"]
    assert body["meta"]["turns"] == 1 and body["meta"]["mode"] == "quick"


def test_prepare_and_commit_accept_kimi_target(auth_cfg, fake_jsonl, monkeypatch):
    # Hermes #4 (#720): the target-capability lockstep — /api/engines advertising kimi as
    # seed-capable must agree with the server actually accepting a kimi handoff target, so a tile
    # the modal shows can never be one the prepare/commit path refuses.
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rows = {r["id"]: r for r in c.get("/api/engines").json()["engines"]}
    assert rows["kimi"]["supports_seed_start"] is True  # advertised seed-capable …
    r = _prepare(c, csrf, auth_cfg, target_engine="kimi")  # … and the route accepts it
    assert r.status_code == 200, r.text
    handle = r.json()["handle"]
    commit = c.post("/api/handoff", json={"handle": handle}, headers=_hdr(csrf, auth_cfg))
    assert commit.status_code == 200, commit.text
    body = commit.json()
    assert body["engine"] == "kimi"
    assert body["native"].startswith("new-")  # reconciling engine → placeholder launch


def test_prepare_requires_csrf(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/handoff/prepare",
        json={"source_id": f"claude:{_SRC}", "target_engine": "codex"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_prepare_rejects_bad_source_id(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert _prepare(c, csrf, auth_cfg, source_id="claude:nope").status_code == 404
    assert _prepare(c, csrf, auth_cfg, source_id="martian:123").status_code == 404


def test_prepare_rejects_shell_source(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, source_id=f"shell:{_SRC}")
    assert r.status_code == 422
    assert "shell" in r.json()["detail"]


def test_prepare_rejects_unsupported_targets(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    for target in ("shell", "gemini", "antigravity"):
        r = _prepare(c, csrf, auth_cfg, target_engine=target)
        assert r.status_code == 422, target
        assert "target engine unavailable" in r.json()["detail"]
    assert _prepare(c, csrf, auth_cfg, target_engine="martian").status_code == 404


def test_prepare_rejects_uninstalled_target(auth_cfg, fake_jsonl, monkeypatch):
    monkeypatch.setattr(discover, "resolve", lambda e: None)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg)
    assert r.status_code == 422
    assert "not installed" in r.json()["detail"]


def test_prepare_rejects_an_unknown_mode(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, mode="telepathy")
    assert r.status_code == 422
    assert "unknown handoff mode" in r.json()["detail"]


def test_prepare_rejects_unscanned_source(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, source_id="claude:99999999-9999-9999-9999-999999999999")
    assert r.status_code == 404


def test_prepare_rejects_out_of_scope_source_before_transcript_read(
    auth_cfg, fake_jsonl, monkeypatch
):
    # Root scope (#465/#467): a scoped-out session is not readable through handoff either,
    # and the refusal happens before any transcript bytes are read.
    from agent_sessions import prefs, project_dirs, transcript

    _present_all(monkeypatch)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: ["/somewhere/else"])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])

    def _boom(engine_id):
        raise AssertionError("transcript read attempted for an out-of-scope source")

    monkeypatch.setattr(transcript, "adapter_for", _boom)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert _prepare(c, csrf, auth_cfg).status_code == 404


def test_prepare_empty_transcript_is_409(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    _present_all(monkeypatch)
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: []))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg)
    assert r.status_code == 409
    assert "empty" in r.json()["detail"]


def test_prepare_allows_same_engine_handoff(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, target_engine="claude")
    assert r.status_code == 200
    r2 = c.post("/api/handoff", json={"handle": r.json()["handle"]}, headers=_hdr(csrf, auth_cfg))
    assert r2.status_code == 200
    assert r2.json()["engine"] == "claude"


# ---- routes: commit ----------------------------------------------------------------------------


def test_commit_returns_target_and_seed_becomes_redeemable(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg).json()["handle"]
    r = c.post("/api/handoff", json={"handle": handle}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "codex" and body["native"].startswith("new-")
    rows = {r["id"]: r for r in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert body["cwd"] == rows[f"claude:{_SRC}"]["cwd"]  # the source session's cwd
    assert handoff.has_pending_seed(body["id"]) is True


def test_commit_unknown_or_expired_handle_404(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/handoff", json={"handle": "bogus"}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 404


# ---- ws launch integration ---------------------------------------------------------------------


def test_ws_new_session_redeems_the_handoff_seed(auth_cfg, fake_jsonl, monkeypatch):
    """The committed handoff's ws launch passes the seed source to the PTY bridge (never
    argv) and arms exactly one spawn watch; the dead-master watch then aborts the handoff
    with no dangling sidecar link (the spawn-failure acceptance case)."""
    from agent_sessions import ptybridge, relaunch, sessions, webterm

    _present_all(monkeypatch)
    captured: dict = {}

    import contextlib

    from agent_sessions.routes import terminal as terminal_routes

    async def fake_run(ws, argv, **kwargs):
        captured["argv"] = argv
        captured["seed_key"] = kwargs.get("seed_key")
        # Await the spawn watch on ITS OWN loop, then tell the test — the client holds
        # the connection open until this frame, so the loop can't be torn down first.
        for t in list(terminal_routes._HANDOFF_WATCHES):
            with contextlib.suppress(Exception):
                await t
        await ws.send_text("WATCH-DONE")

    monkeypatch.setattr(webterm, "run", fake_run)
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (sessions.LAUNCH, None))
    monkeypatch.setattr(relaunch, "blocked", lambda key: False)
    monkeypatch.setattr(relaunch, "note_exit", lambda *a, **k: None)
    monkeypatch.setattr(relaunch, "_INSTANT_EXIT_S", 0.05)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)  # dies instantly
    monkeypatch.setattr(
        ptybridge, "launch_argv", lambda *, engine, session_id, launch_argv: ["/bin/true"]
    )
    monkeypatch.setattr(engines.base, "CLAUDE_BIN", "/bin/true")

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg, target_engine="claude").json()["handle"]
    res = c.post("/api/handoff", json={"handle": handle}, headers=_hdr(csrf, auth_cfg)).json()
    key, cwd = res["id"], res["cwd"]

    cookie = c.cookies.get("agent_sessions")
    headers = {"Origin": auth_cfg.origin, "Cookie": f"agent_sessions={cookie}"}
    with c.websocket_connect(f"/ws/term/{key}?new=1&cwd={cwd}", headers=headers) as ws:
        # Skip the route's control frames (role/seq/…) until the bridge's done signal.
        for _ in range(10):
            if ws.receive_text() == "WATCH-DONE":
                break
        else:
            raise AssertionError("WATCH-DONE never arrived")
    assert captured["seed_key"] == key  # the bridge got the seed source…
    assert all("Handoff" not in a and "repo-a" not in a for a in captured["argv"])  # …not argv
    assert handoff.arm_watch(key) is False  # the launch armed the single watch
    # The watch (instant-exit master) aborted the handoff: seed gone, NO provenance.
    assert handoff.has_pending_seed(key) is False
    assert metadata.get(key).handoff_from == ""
    assert metadata.get(f"claude:{_SRC}").handoff_to == ""


# ---- PTY injection ------------------------------------------------------------------------------

# A READY TUI: arms bracketed paste, paints a full screen (the first-paint evidence the
# injector waits for), goes quiet, then reads input — the shape of a booted codex/claude.
_CHILD = r"""
import os, sys, time, tty
tty.setraw(0)
os.write(1, b"\x1b[?2004h")            # 1. speaks the paste protocol
os.write(1, b"." * 4096)               # 2. first paint (> _SEED_FIRST_PAINT_BYTES)
buf = b""
end = time.time() + 8
while time.time() < end and b"\r" not in buf:
    try:
        buf += os.read(0, 65536)
    except OSError:
        break
os.write(1, b"GOT[" + buf + b"]")
"""

# A COLD TUI (#597 Phase 2, empirically observed from a fresh-install `codex`): arms
# bracketed paste in its terminal-init preamble ~immediately, then emits almost nothing
# for many seconds while it initialises — and DISCARDS anything written to stdin in that
# window. Gating on 2004 alone pasted the seed into the void and called it delivered.
_CHILD_PREAMBLE_ONLY = r"""
import os, time, tty
tty.setraw(0)
os.write(1, b"\x1b[?2004h")            # armed — but the input pipeline is NOT up
os.write(1, b"warning: still starting up\r\n")   # ~91-byte-scale preamble, no paint
time.sleep(6)                          # ...initialising; stdin is not being read
"""


def _fake_ws(collected):
    class FakeWS:
        async def receive(self):
            await asyncio.sleep(10)
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            collected.append(b)

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    return FakeWS()


def test_webterm_injects_seed_as_bracketed_paste_after_readiness(tmp_path, monkeypatch):
    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_SETTLE_S", 0.05)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "SEED-BODY-42", cwd="/tmp")
    key = handoff.commit(h)["id"]
    out: list[bytes] = []
    asyncio.run(
        webterm.run(
            _fake_ws(out),
            [sys.executable, "-c", _CHILD],
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    blob = b"".join(out)
    got = blob[blob.find(b"GOT[") :]
    # The child received one bracketed paste with the exact seed, then the CR submit.
    assert b"\x1b[200~SEED-BODY-42\x1b[201~" in got
    assert b"\r" in got
    assert handoff.claim_seed(key) is None  # consumed exactly once


def test_webterm_seed_times_out_fail_safe_when_tui_never_arms_paste(tmp_path, monkeypatch):
    # A TUI that never arms bracketed paste gets NO injection (unseeded beats spraying raw
    # bytes into a half-booted TUI) — and the unconsumed seed survives for the next attach.
    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.02)
    monkeypatch.setattr(webterm, "_SEED_READY_TIMEOUT_S", 0.2)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "NEVER-SENT", cwd="/tmp")
    key = handoff.commit(h)["id"]
    out: list[bytes] = []
    asyncio.run(
        webterm.run(
            _fake_ws(out),
            [sys.executable, "-c", "import time; time.sleep(0.6)"],
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    assert b"NEVER-SENT" not in b"".join(out)
    assert handoff.has_pending_seed(key) is True  # not consumed — next attach can inject


def test_injector_cancelled_before_readiness_leaves_seed_pending(tmp_path, monkeypatch):
    # PR #701 review P2 (pre-redemption half): every await in the injector sits BEFORE
    # redemption, so a viewer that disconnects while the TUI is still booting cancels the
    # injector WITHOUT consuming the seed — the next attach injects it.
    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_READY_TIMEOUT_S", 30.0)  # far beyond the ws lifetime
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "STILL-PENDING", cwd="/tmp")
    key = handoff.commit(h)["id"]

    class DroppingWS:
        async def receive(self):
            await asyncio.sleep(0.2)  # viewer vanishes while the TUI is still booting
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            pass

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    asyncio.run(
        webterm.run(
            DroppingWS(),
            [sys.executable, "-c", "import time; time.sleep(5)"],  # never arms 2004
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    assert handoff.has_pending_seed(key) is True


def test_deliver_seed_full_delivery_acks_consumed(tmp_home):
    # Happy path of the claim/ack delivery: one full paste+CR write, seed consumed exactly
    # once, a second delivery attempt finds nothing to claim.
    from agent_sessions import webterm

    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "ATOMIC-SEED", cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    try:
        assert webterm._deliver_seed(w, key, None) is True
        assert os.read(r, 65536) == b"\x1b[200~ATOMIC-SEED\x1b[201~\r"
        assert webterm._deliver_seed(w, key, None) is False  # single delivery held
    finally:
        os.close(r)
        os.close(w)


def test_deliver_seed_zero_write_failure_leaves_seed_for_retry(tmp_home):
    # Round-2 P1b (clean-failure half): a delivery that wrote NOTHING acks "retry" — the
    # seed survives for the next attach instead of being consumed by a dead fd.
    from agent_sessions import webterm

    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "RETRYABLE", cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    os.close(r)
    os.close(w)  # both ends gone → the first os.write raises before any byte lands
    assert webterm._deliver_seed(w, key, None) is False
    assert handoff.has_pending_seed(key) is True
    assert handoff.claim_seed(key) == "RETRYABLE"  # claim was released for the retry


def test_deliver_seed_partial_write_aborts_the_claim_explicitly(tmp_home):
    # Round-2 P1b (partial half): a write that landed SOME bytes must not silently
    # half-lose the seed OR blindly retry into a polluted prompt — it acks "abort"
    # (consumed, logged), and no further claim is possible.
    import fcntl
    import threading

    from agent_sessions import webterm

    seed = "P" * 8000  # > the shrunken pipe capacity below → the write blocks mid-way
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", seed, cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)  # F_SETPIPE_SZ: capacity 4096 < len(paste)
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(webterm._deliver_seed(w, key, None)))
    t.start()
    time.sleep(0.3)  # the writer has filled the pipe and is blocked mid-payload
    os.close(r)  # reader vanishes → the blocked write raises EPIPE with bytes already sent
    t.join(timeout=10)
    os.close(w)
    assert result == [False]
    assert handoff.has_pending_seed(key) is False  # aborted — consumed, never blind-retried
    assert handoff.claim_seed(key) is None


def test_seed_delivery_backpressure_does_not_stall_the_event_loop(tmp_home):
    # Round-2 P1a: the blocking PTY write runs in a worker thread, so a target that stops
    # draining input stalls only that thread — the event loop keeps ticking. The pipe is
    # shrunk below the payload size so the write genuinely blocks until the reader drains.
    import fcntl

    from agent_sessions import webterm

    seed = "B" * 8000
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", seed, cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)  # F_SETPIPE_SZ

    async def main() -> None:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, webterm._deliver_seed, w, key, None)
        ticks = 0
        drained = b""
        while not fut.done():
            await asyncio.sleep(0.01)  # the loop is alive while the write is blocked
            ticks += 1
            if ticks > 5:  # let it sit blocked a few ticks first, then drain
                drained += os.read(r, 4096)
            assert ticks < 1000, "delivery never completed"
        assert await fut is True
        # Drain the remainder and check the payload ends with the submitting CR.
        while not drained.endswith(b"\x1b[201~\r"):
            drained += os.read(r, 65536)
        assert ticks > 5  # the loop demonstrably ran while the writer was blocked

    try:
        asyncio.run(main())
    finally:
        os.close(r)
        os.close(w)
    assert handoff.has_pending_seed(key) is False  # delivered → consumed


def test_wedged_delivery_times_out_and_settles_the_claim(tmp_home, monkeypatch):
    # Round-3 P1: a target that keeps the PTY open but never drains input can't pin the
    # delivery worker forever — the write deadline fires and the claim settles (abort,
    # since bytes already landed in the pipe).
    import fcntl

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_WRITE_TIMEOUT_S", 0.3)
    seed = "W" * 8000
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", seed, cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)  # F_SETPIPE_SZ: payload > capacity, and nobody ever drains
    try:
        start = time.monotonic()
        assert webterm._deliver_seed(w, key, None) is False
        assert time.monotonic() - start < 5  # bounded, not wedged
    finally:
        os.close(r)
        os.close(w)
    assert handoff.has_pending_seed(key) is False  # partial → aborted explicitly
    assert handoff.claim_seed(key) is None


def test_wedged_delivery_with_zero_bytes_leaves_seed_for_retry(tmp_home, monkeypatch):
    # Round-3 P1 (zero-byte half): the pipe is ALREADY full before the first chunk, so the
    # deadline fires with nothing written → retry-ack, seed survives for the next attach.
    import fcntl

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_WRITE_TIMEOUT_S", 0.3)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "UNWRITTEN", cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)
    os.write(w, b"j" * 4096)  # pre-fill to capacity — no room for even one chunk
    try:
        assert webterm._deliver_seed(w, key, None) is False
    finally:
        os.close(r)
        os.close(w)
    assert handoff.has_pending_seed(key) is True
    assert handoff.claim_seed(key) == "UNWRITTEN"


def test_delivery_stays_bounded_with_a_concurrent_writer(tmp_home, monkeypatch):
    # Round-4 P1: select-writability is only a snapshot — a concurrent writer (pump_in in
    # production) can consume the window before the delivery write. Every writer to the
    # PTY is therefore serialized through the bridge's write_lock, and the writability
    # re-check happens UNDER that lock — the deadline holds however aggressively the
    # (lock-respecting, like pump_in) competitor floods the fd, and the claim settles.
    import contextlib
    import fcntl
    import threading

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_WRITE_TIMEOUT_S", 0.5)
    seed = "C" * 8000
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", seed, cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)  # F_SETPIPE_SZ
    stop = threading.Event()
    write_lock = threading.Lock()  # shared with the competitor, exactly like pump_in

    def competitor() -> None:
        # Flood the same pipe under the shared lock, grabbing capacity whenever possible
        # (a non-blocking second fd so the competitor itself can never wedge the test).
        w2 = os.dup(w)
        os.set_blocking(w2, False)
        try:
            while not stop.is_set():
                with write_lock, contextlib.suppress(BlockingIOError, OSError):
                    os.write(w2, b"z" * 2048)
                time.sleep(0.001)
        finally:
            os.close(w2)

    t = threading.Thread(target=competitor)
    t.start()
    try:
        start = time.monotonic()
        webterm._deliver_seed(w, key, None, write_lock)
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        t.join(timeout=10)
        os.close(r)
        os.close(w)
    assert elapsed < 5  # the deadline held despite the competing writer
    # The claim is settled either way (delivered, retried-pending, or aborted) — never
    # stuck "claimed": a fresh claim attempt must not dead-lock on a stale claim.
    assert handoff.claim_seed(key) in (None, seed)


def test_saturated_pool_cancellation_reclaims_the_queued_fd(tmp_home):
    # Round-4 P2: a delivery cancelled while still QUEUED behind a saturated pool never
    # runs its finally-close — the wrapper must reclaim + close the dup'd fd, and the
    # never-claimed seed must survive for the next attach.
    import threading

    from agent_sessions import webterm

    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "QUEUED", cwd="/tmp")
    key = handoff.commit(h)["id"]
    gate = threading.Event()
    pool = webterm._seed_executor()
    blockers = [pool.submit(gate.wait, 30) for _ in range(webterm._SEED_MAX_DELIVERY_WORKERS)]
    r, w = os.pipe()
    fd = os.dup(w)

    async def main() -> None:
        task = asyncio.ensure_future(webterm._deliver_seed_via_pool(fd, key, None))
        await asyncio.sleep(0.2)  # the job sits queued behind the saturated pool
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(main())
    finally:
        gate.set()
        for b in blockers:
            b.result(timeout=10)
        os.close(r)
        os.close(w)
    with pytest.raises(OSError):
        os.fstat(fd)  # the dup was reclaimed and closed — no leak
    assert handoff.has_pending_seed(key) is True  # never claimed — next attach delivers


def test_seed_deliveries_run_on_a_dedicated_bounded_pool():
    # Round-3 P1: deliveries must never occupy the event loop's SHARED default executor
    # (pump_out's PTY reads live there) — they get their own small pool.
    import threading

    from agent_sessions import webterm

    pool = webterm._seed_executor()
    assert pool is webterm._seed_executor()  # one shared instance
    assert pool._max_workers == webterm._SEED_MAX_DELIVERY_WORKERS
    name = pool.submit(lambda: threading.current_thread().name).result(timeout=10)
    assert name.startswith("handoff-seed")


def test_spawn_watch_retries_failed_publication_in_production_path(tmp_home, monkeypatch):
    # Round-3 P2: the retry vehicle is the spawn watch ITSELF (armed once per target —
    # reconnects can't re-arm), so a transient sidecar-write failure heals without any
    # manual state-machine poke. Driven through the real watcher coroutine.
    from agent_sessions import ptybridge, relaunch
    from agent_sessions.routes import terminal as terminal_routes

    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    native = key.partition(":")[2]
    monkeypatch.setattr(relaunch, "_INSTANT_EXIT_S", 0.01)
    monkeypatch.setattr(terminal_routes, "_PUBLISH_RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: True)
    real_patch = metadata.patch
    calls = {"n": 0}

    def flaky(k, **fields):
        calls["n"] += 1
        if calls["n"] == 2:  # the backlink write fails once, transiently
            raise OSError("disk full")
        return real_patch(k, **fields)

    monkeypatch.setattr(metadata, "patch", flaky)
    asyncio.run(terminal_routes._handoff_spawn_watch("claude", native))
    assert metadata.get(key).handoff_from == src
    assert metadata.get(src).handoff_to == key  # the watch's retry healed the backlink
    assert calls["n"] == 3  # 1 target + 1 failed backlink + 1 retried backlink, no dupes


def test_provenance_publication_is_retryable_after_a_failed_sidecar_write(tmp_home, monkeypatch):
    # Round-2 P2: publication flags are set only after each sidecar patch succeeds — a
    # failed backlink write leaves retryable state, and a later mark_spawned performs
    # exactly the missing write (never a duplicate of the succeeded one).
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    real_patch = metadata.patch
    calls = {"n": 0}

    def flaky(k, **fields):
        calls["n"] += 1
        if calls["n"] == 2:  # the source-backlink write fails once
            raise OSError("disk full")
        return real_patch(k, **fields)

    monkeypatch.setattr(metadata, "patch", flaky)
    with pytest.raises(OSError):
        handoff.mark_spawned(key)
    assert metadata.get(key).handoff_from == src  # target side landed
    assert metadata.get(src).handoff_to == ""  # backlink didn't — but stays retryable
    handoff.mark_spawned(key)  # retry publishes ONLY the missing backlink
    assert metadata.get(src).handoff_to == key
    assert calls["n"] == 3  # 1 target + 1 failed backlink + 1 retried backlink


# ---- rows carry provenance ----------------------------------------------------------------------


def test_session_rows_carry_handoff_provenance(auth_cfg, fake_jsonl):
    src = f"claude:{_SRC}"
    tgt = "claude:22222222-2222-2222-2222-222222222222"
    metadata.patch(tgt, handoff_from=src, handoff_mode="quick", handoff_at="2026-07-16T00:00:00")
    metadata.patch(src, handoff_to=tgt)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = {r["id"]: r for r in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert rows[tgt]["handoff_from"] == src
    assert rows[src]["handoff_to"] == tgt
    assert rows[src]["handoff_from"] == ""  # unset stays an empty string, never null/missing


def test_stale_handoff_link_is_tolerated(auth_cfg, fake_jsonl):
    # A backlink whose peer no longer exists must not break the list (read-time tolerance).
    src = f"claude:{_SRC}"
    metadata.patch(src, handoff_to="codex:deadbeef-dead-dead-dead-deaddeadbeef")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/sessions?limit=50")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["sessions"]}
    assert rows[src]["handoff_to"].startswith("codex:")


# ---- Phase 2: AI mode ---------------------------------------------------------------------

_AI_OBJ = {
    "state": "Refactored the token-refresh path; single-flight lock added and tests pass.",
    "open_items": ["PR #482 awaiting review"],
    "next_steps": ["Address review comments", "Re-run the auth suite"],
}


def _fake_complete(obj):
    async def _c(messages, *, model=None):
        _fake_complete.seen = messages
        return obj

    return _c


def test_ai_seed_renders_the_guarded_brief(fake_jsonl, monkeypatch):
    from agent_sessions import review

    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    seed, meta = asyncio.run(
        handoff.build_ai_seed("claude", _SRC, title="t", cwd="/home/user/claude/repo-a")
    )
    assert meta["mode"] == "ai"
    assert "## State" in seed and "single-flight lock" in seed
    assert "- PR #482 awaiting review" in seed
    assert "- Re-run the auth suite" in seed
    assert "# Handoff — continued from a claude session" in seed  # shared header w/ Quick
    # The transcript tail is what we sent — and only that.
    sent = _fake_complete.seen[1]["content"]
    assert "[user] first message on repo-a" in sent


def test_ai_seed_shape_guard_caps_and_strips(fake_jsonl, monkeypatch):
    from agent_sessions import review

    evil = {
        "state": "ok \x1b[201~ escape " + "s" * 5000,
        "open_items": ["fine", 42, "", "x" * 900] + [f"extra{i}" for i in range(20)],
        "next_steps": "not a list",
    }
    monkeypatch.setattr(review, "complete_json", _fake_complete(evil))
    seed, _ = asyncio.run(handoff.build_ai_seed("claude", _SRC))
    assert "\x1b" not in seed  # paste-breakout guard holds on model output too
    body = seed.split("## State")[1]
    assert len(body) < handoff.AI_STATE_MAX + 400  # state capped
    assert seed.count("\n- ") <= handoff.AI_ITEMS_MAX  # item count capped
    assert "- 42" not in seed  # non-string items dropped
    assert "## Next steps" not in seed  # a non-list becomes no section


def test_ai_seed_missing_state_raises(fake_jsonl, monkeypatch):
    from agent_sessions import review

    monkeypatch.setattr(review, "complete_json", _fake_complete({"open_items": ["x"]}))
    with pytest.raises(handoff.HandoffError) as ei:
        asyncio.run(handoff.build_ai_seed("claude", _SRC))
    assert ei.value.status == 502


def test_ai_seed_empty_transcript_raises_409_like_quick(fake_jsonl, monkeypatch):
    from agent_sessions import review, transcript

    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: []))
    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    with pytest.raises(handoff.HandoffError) as ei:
        asyncio.run(handoff.build_ai_seed("claude", _SRC))
    assert ei.value.status == 409


def test_prepare_ai_mode_returns_the_ai_seed(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions import review

    _present_all(monkeypatch)
    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, mode="ai")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meta"]["mode"] == "ai"
    assert not body["meta"].get("degraded")
    assert "## State" in body["preview"]


def test_prepare_ai_degrades_to_quick_when_unconfigured(auth_cfg, fake_jsonl, monkeypatch):
    # The documented Phase-2 contract: an unconfigured endpoint must NOT fail the handoff —
    # it falls back to the local quick tail and says so.
    from agent_sessions import review

    _present_all(monkeypatch)

    async def _boom(messages, *, model=None):
        raise review.NotConfiguredError("AI review endpoint is not configured")

    monkeypatch.setattr(review, "complete_json", _boom)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, mode="ai")
    assert r.status_code == 200
    meta = r.json()["meta"]
    assert meta["mode"] == "quick" and meta["requested_mode"] == "ai"
    assert meta["degraded"] is True
    assert "isn't configured" in meta["notice"]
    assert "[user] first message on repo-a" in r.json()["preview"]  # the quick tail


def test_prepare_ai_degrades_when_the_endpoint_fails(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions import review

    _present_all(monkeypatch)

    async def _boom(messages, *, model=None):
        raise review.ReviewError("endpoint returned HTTP 500")

    monkeypatch.setattr(review, "complete_json", _boom)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    meta = _prepare(c, csrf, auth_cfg, mode="ai").json()["meta"]
    assert meta["mode"] == "quick" and meta["degraded"] is True
    assert "AI summary failed" in meta["notice"]


def test_prepare_ai_degrades_on_an_unusable_answer(auth_cfg, fake_jsonl, monkeypatch):
    # A 502-shaped HandoffError (garbage model output) degrades; only the 409
    # empty-transcript case propagates (Quick can't do better).
    from agent_sessions import review

    _present_all(monkeypatch)
    monkeypatch.setattr(review, "complete_json", _fake_complete({"state": "   "}))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    meta = _prepare(c, csrf, auth_cfg, mode="ai").json()["meta"]
    assert meta["mode"] == "quick" and meta["degraded"] is True


def test_prepare_ai_empty_transcript_still_409(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions import review, transcript

    _present_all(monkeypatch)
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: []))
    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert _prepare(c, csrf, auth_cfg, mode="ai").status_code == 409


# ---- Phase 2: editable preview ------------------------------------------------------------


def test_sanitize_seed_strips_control_bytes():
    # Only the CONTROL BYTES go (ESC, BEL): that alone disarms the paste breakout — the
    # residual "[201~" is inert literal text once its ESC is gone.
    assert handoff.sanitize_seed("  hi \x1b[201~there\x07  ") == "hi [201~there"
    for bad in ("", "   ", "\x1b\x07"):
        with pytest.raises(handoff.HandoffError) as ei:
            handoff.sanitize_seed(bad)
        assert ei.value.status == 422


def test_sanitize_seed_rejects_over_cap_rather_than_truncating(monkeypatch):
    # #703 review: silently shortening USER-AUTHORED prose and reporting success would hand
    # the target a brief its author never wrote. Reject; the client knows the cap (meta.cap).
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 64)
    assert len(handoff.sanitize_seed("z" * 60).encode()) <= 64  # at the limit: fine
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.sanitize_seed("z" * 200)
    assert ei.value.status == 422
    assert "too large" in ei.value.detail


def test_commit_with_edited_seed_replaces_the_prepared_text(tmp_home):
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "ORIGINAL", cwd="/tmp")
    key = handoff.commit(h, "MY EDIT")["id"]
    assert handoff.claim_seed(key) == "MY EDIT"


def test_commit_sanitizes_an_edited_seed(tmp_home):
    # The edited seed is UNTRUSTED input — it gets the same control-strip as a built one,
    # so a hand-crafted ESC can't break out of the bracketed paste at delivery.
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "ORIGINAL", cwd="/tmp")
    key = handoff.commit(h, "safe \x1b[201~\x1b[5;5H rm -rf / \x07 tail")["id"]
    seed = handoff.claim_seed(key)
    assert "\x1b" not in seed and "\x07" not in seed
    assert "safe" in seed and "tail" in seed


def test_commit_rejects_an_empty_edited_seed(tmp_home):
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "ORIGINAL", cwd="/tmp")
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.commit(h, "   ")
    assert ei.value.status == 422
    # The handle is NOT consumed by a rejected edit — the modal can retry.
    assert handoff.commit(h, "second try")["id"]


def test_commit_route_passes_the_edited_seed(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg).json()["handle"]
    r = c.post(
        "/api/handoff",
        json={"handle": handle, "seed": "EDITED BY HAND"},
        headers=_hdr(csrf, auth_cfg),
    )
    assert r.status_code == 200
    assert handoff.claim_seed(r.json()["id"]) == "EDITED BY HAND"


def test_commit_route_without_a_seed_keeps_the_prepared_text(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    prep = _prepare(c, csrf, auth_cfg).json()
    r = c.post("/api/handoff", json={"handle": prep["handle"]}, headers=_hdr(csrf, auth_cfg))
    assert handoff.claim_seed(r.json()["id"]) == prep["preview"]


def test_commit_route_rejects_an_empty_edited_seed(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg).json()["handle"]
    r = c.post("/api/handoff", json={"handle": handle, "seed": " "}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 422


def test_abandoned_preview_spawns_nothing_and_expires(auth_cfg, fake_jsonl, monkeypatch):
    # "Cancellation after preview leaves no session, no sidecar entry, no temp file" —
    # prepare is side-effect-free, so cancelling is literally doing nothing.
    _present_all(monkeypatch)
    monkeypatch.setattr(handoff, "HANDLE_TTL_S", 0.05)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg).json()["handle"]
    assert metadata.load() == {}  # nothing persisted at prepare
    time.sleep(0.1)
    r = c.post("/api/handoff", json={"handle": handle}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 404  # the abandoned handle simply expired
    assert metadata.load() == {}


def test_seed_is_not_injected_into_a_tui_that_armed_paste_but_never_painted(tmp_path, monkeypatch):
    """#597 Phase 2 regression — the bug the empirical handoff caught.

    A fresh-install `codex` arms DECSET 2004 in its terminal-init preamble (~0.3 s), then
    initialises for many seconds while DISCARDING stdin. Phase 1 treated 2004 alone as
    readiness, so it pasted the seed into the void, acked it "delivered", and silently
    produced an unseeded session. Readiness now also requires first-paint evidence + a
    quiet window, so this TUI never opens the gate: no injection, seed still pending for
    the next attach, and a warning explains why.
    """
    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_READY_TIMEOUT_S", 1.5)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "NOT-INTO-THE-VOID", cwd="/tmp")
    key = handoff.commit(h)["id"]
    out: list[bytes] = []
    asyncio.run(
        webterm.run(
            _fake_ws(out),
            [sys.executable, "-c", _CHILD_PREAMBLE_ONLY],
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    blob = b"".join(out)
    assert b"\x1b[?2004h" in blob  # it DID arm paste — the old gate would have fired
    assert b"NOT-INTO-THE-VOID" not in blob  # …but we did not paste into a booting TUI
    assert handoff.has_pending_seed(key) is True  # unconsumed — the next attach can deliver


def test_seed_waits_for_quiet_before_pasting(tmp_path, monkeypatch):
    # Readiness also needs the paint to SETTLE: a TUI still streaming output is mid-boot.
    # This child paints continuously for ~1s, then goes quiet — delivery must land after
    # the quiet window, never during the noisy stretch.
    from agent_sessions import webterm

    noisy = (
        "import os, time, tty\n"
        "tty.setraw(0)\n"
        'os.write(1, b"\\x1b[?2004h")\n'
        "end = time.time() + 1.0\n"
        "while time.time() < end:\n"
        '    os.write(1, b"x" * 512); time.sleep(0.05)\n'
        "buf = b''\n"
        "t = time.time() + 8\n"
        "while time.time() < t and b'\\r' not in buf:\n"
        "    try: buf += os.read(0, 65536)\n"
        "    except OSError: break\n"
        'os.write(1, b"GOT[" + buf + b"]")\n'
    )
    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_QUIET_S", 0.4)
    monkeypatch.setattr(webterm, "_SEED_SETTLE_S", 0.05)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "AFTER-QUIET", cwd="/tmp")
    key = handoff.commit(h)["id"]
    out: list[bytes] = []
    asyncio.run(
        webterm.run(
            _fake_ws(out),
            [sys.executable, "-c", noisy],
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    blob = b"".join(out)
    got = blob[blob.find(b"GOT[") :]
    assert b"\x1b[200~AFTER-QUIET\x1b[201~" in got  # delivered once the paint settled
    assert handoff.has_pending_seed(key) is False


# ---- #703 review: boundary fixes ------------------------------------------------------


def test_ai_input_budget_holds_for_a_single_oversized_turn(monkeypatch):
    # #703 review: the overflow branch only broke when `rows` was non-empty, so a single
    # newest turn larger than the whole budget sailed through whole.
    monkeypatch.setattr(handoff, "AI_INPUT_CHARS", 40)
    out = handoff._ai_input([("user", "x" * 500)])
    assert len(out) <= 40
    # …and the budget still holds across many turns (oldest dropped first).
    out = handoff._ai_input([("user", "a" * 30), ("agent", "b" * 30), ("user", "c" * 30)])
    assert len(out) <= 40
    assert "c" in out  # the NEWEST turn is the one kept


def test_ai_request_content_respects_the_cap(fake_jsonl, monkeypatch):
    # The cap is enforced on what actually leaves the process, not just on a local count.
    from agent_sessions import review, transcript

    monkeypatch.setattr(handoff, "AI_INPUT_CHARS", 120)
    turns = [transcript.Turn(role="user", text="q" * 4000, kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seen = {}

    async def _c(messages, *, model=None):
        seen["user"] = messages[1]["content"]
        return _AI_OBJ

    monkeypatch.setattr(review, "complete_json", _c)
    asyncio.run(handoff.build_ai_seed("claude", _SRC))
    assert len(seen["user"]) <= 120


def test_cap_never_exceeds_the_byte_limit(monkeypatch):
    # #703 review: `_cap` sliced to the FULL cap and then appended the "…" marker, so the
    # documented hard cap was overshot by the marker's bytes.
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 32)
    out = handoff._cap("y" * 200)
    assert len(out.encode()) <= 32
    assert out.endswith("…\n")  # still marked as truncated
    # A multibyte char split by the cut is dropped, never mojibaked.
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 20)
    out = handoff._cap("é" * 50)
    assert len(out.encode()) <= 20
    out.encode().decode("utf-8")  # round-trips → no broken sequence


def test_builders_still_truncate_their_own_output(fake_jsonl, monkeypatch):
    # The builders truncate (there is no author to ask); only USER text is rejected.
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 400)
    from agent_sessions import review

    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    seed, meta = asyncio.run(handoff.build_ai_seed("claude", _SRC, title="t", cwd="/c"))
    assert len(seed.encode()) <= 400
    assert meta["bytes"] <= 400


def test_commit_route_rejects_an_over_cap_edited_seed(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 128)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg).json()["handle"]
    r = c.post(
        "/api/handoff",
        json={"handle": handle, "seed": "z" * 500},
        headers=_hdr(csrf, auth_cfg),
    )
    assert r.status_code == 422
    assert "too large" in r.json()["detail"]
    # The rejected edit did not consume the handle — the user can trim and retry.
    r2 = c.post(
        "/api/handoff", json={"handle": handle, "seed": "trimmed"}, headers=_hdr(csrf, auth_cfg)
    )
    assert r2.status_code == 200


def test_prepare_meta_carries_the_cap_the_server_enforces(auth_cfg, fake_jsonl, monkeypatch):
    # The modal gates its CTA on meta.cap, so it must be the same number commit enforces.
    _present_all(monkeypatch)
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 256)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert _prepare(c, csrf, auth_cfg).json()["meta"]["cap"] == 256


# ---- #703 review round 2: the cap is exact, for every path ----------------------------


def test_quick_seed_cap_holds_for_an_oversized_single_turn(fake_jsonl, monkeypatch):
    # Round 2: the bespoke truncation overshot by the marker+arithmetic — 8196 bytes at the
    # 8192 default. Every generated doc exits through `_cap` now.
    from agent_sessions import transcript

    turns = [transcript.Turn(role="user", text="q" * 50_000, kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seed, meta = handoff.build_quick_seed("claude", _SRC)
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES
    assert meta["bytes"] == len(seed.encode()) <= meta["cap"]


def test_quick_seed_cap_holds_for_an_oversized_title(fake_jsonl):
    # Round 2: an oversized first_user_message went into the header unbounded → a 20 KB
    # "capped" doc. The title is bounded AND the doc is capped.
    seed, meta = handoff.build_quick_seed("claude", _SRC, title="T" * 40_000, cwd="/c")
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES
    assert meta["bytes"] <= meta["cap"]
    assert "## Recent turns" in seed  # the title didn't crowd out the actual handoff
    assert len("T" * 40_000) > handoff.HEAD_TITLE_MAX  # …because the title is bounded


def test_ai_seed_cap_holds_for_an_oversized_title(fake_jsonl, monkeypatch):
    from agent_sessions import review

    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    seed, meta = asyncio.run(handoff.build_ai_seed("claude", _SRC, title="T" * 40_000))
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES
    assert meta["bytes"] <= meta["cap"]


def test_ai_seed_keeps_its_state_when_both_paths_are_enormous(fake_jsonl, monkeypatch):
    """The AI half of #718: the state IS the brief, and a 4089-byte `cwd` + locator pushed it
    out of an 8192-byte document that still reported `bytes=8192, turns=1`."""
    from agent_sessions import review, transcript

    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    monkeypatch.setitem(transcript._LOCATORS, "claude", lambda native, home: _nested_path(4089))

    seed, meta = asyncio.run(
        handoff.build_ai_seed("claude", _SRC, cwd=_nested_path(4089), include_source_ref=True)
    )
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES
    assert "## State" in seed
    assert "single-flight lock" in seed
    assert meta["mode"] == "ai"


def test_ai_locator_resolution_does_not_block_the_event_loop(fake_jsonl, monkeypatch):
    """Transcript extraction was already offloaded; the locator was not — and production
    locators do recursive globbing plus a 0.5 s SQLite probe. A blocking one stalls every other
    request on the loop, not just this one."""
    from agent_sessions import review, transcript

    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))

    def _slow_locator(native, home):
        time.sleep(0.25)  # stands in for the glob + sqlite probe
        return "/store/x.jsonl"

    monkeypatch.setitem(transcript._LOCATORS, "claude", _slow_locator)

    async def _drive():
        beats: list[float] = []

        async def heartbeat():
            while True:
                t0 = time.monotonic()
                await asyncio.sleep(0.01)
                beats.append(time.monotonic() - t0)

        hb = asyncio.create_task(heartbeat())
        try:
            await handoff.build_ai_seed("claude", _SRC, include_source_ref=True)
        finally:
            hb.cancel()
        return beats

    beats = asyncio.run(_drive())
    assert beats, "the heartbeat never ran"
    # The locator sleeps 250 ms. On the loop, one beat absorbs all of it; off the loop the
    # worst beat stays in the low tens of ms even on a busy host.
    assert max(beats) < 0.15, f"the event loop stalled for {max(beats):.3f}s during the locator"


def test_generated_seeds_never_exceed_the_cap_at_any_size(fake_jsonl, monkeypatch):
    # Sweep the boundary: whatever the turn sizes, the advertised cap is the real one.
    from agent_sessions import transcript

    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 512)
    for n in (1, 100, 400, 511, 512, 513, 900, 5000):
        turns = [transcript.Turn(role="user", text="z" * n, kind="text")]
        monkeypatch.setattr(transcript, "adapter_for", lambda e, t=turns: (lambda native, home: t))
        seed, meta = handoff.build_quick_seed("claude", _SRC, title="t", cwd="/c")
        assert len(seed.encode()) <= 512, f"n={n} produced {len(seed.encode())} bytes"
        assert meta["bytes"] == len(seed.encode())


def test_edited_seed_at_exactly_the_cap_is_accepted(tmp_home, monkeypatch):
    # Round 2: the server appended "\n" before validating, so a brief whose visible size
    # equalled meta.cap was rejected as cap+1 — while the modal (counting the same visible
    # bytes) had enabled the button. The stored seed is now exactly what was validated.
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 64)
    exact = "e" * 64
    assert handoff.sanitize_seed(exact) == exact
    assert len(handoff.sanitize_seed(exact).encode()) == 64
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "orig", cwd="/tmp")
    key = handoff.commit(h, exact)["id"]
    assert handoff.claim_seed(key) == exact
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.sanitize_seed("e" * 65)
    assert ei.value.status == 422


def test_client_visible_bytes_never_undercount_the_server(monkeypatch):
    # The modal gates on the RAW textarea bytes vs meta.cap; the server only ever strips or
    # trims (both shrink), so a client-accepted brief is always server-accepted. Pin that
    # direction — it is what makes the two enforcement points agree at the boundary.
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", 64)
    for raw in ("x" * 64, "  " + "x" * 62 + "  ", "\x1b" * 10 + "y" * 54, "é" * 32):
        if len(raw.encode()) > 64:
            continue  # the modal would have blocked it
        assert len(handoff.sanitize_seed(raw).encode()) <= 64


def test_cap_holds_for_caps_smaller_than_the_truncation_marker(monkeypatch):
    # #703 review round 3: `_cap` always appended the 5-byte "\n…\n" marker, so a cap below
    # 5 produced 5 bytes. The marker is a courtesy — never a reason to exceed the cap.
    for cap in (0, 1, 2, 3, 4, 5, 6, 10):
        monkeypatch.setattr(handoff, "SEED_CAP_BYTES", cap)
        out = handoff._cap("x" * 200)
        assert len(out.encode()) <= cap, f"cap={cap} produced {len(out.encode())} bytes"
    monkeypatch.setattr(handoff, "SEED_CAP_BYTES", -5)  # defensive: never negative-slice
    assert handoff._cap("x" * 50) == ""


def test_configured_cap_is_floored_to_a_usable_minimum(monkeypatch):
    # The env knob is an operator dial; 0 / negative / absurd would configure a handoff that
    # can carry no handoff. The floor is applied at load.
    import importlib

    for raw in ("0", "-1", "3", "not-a-number"):
        monkeypatch.setenv("AGENT_SESSIONS_HANDOFF_CAP_BYTES", raw)
        mod = importlib.reload(handoff)
        assert mod.SEED_CAP_BYTES >= mod.MIN_CAP_BYTES
    monkeypatch.setenv("AGENT_SESSIONS_HANDOFF_CAP_BYTES", "4096")
    mod = importlib.reload(handoff)
    assert mod.SEED_CAP_BYTES == 4096  # a sane operator value is honoured
    monkeypatch.delenv("AGENT_SESSIONS_HANDOFF_CAP_BYTES", raising=False)
    importlib.reload(handoff)  # restore the module for the rest of the session


# ---- #703 review round 4: readiness survives a reconnect ------------------------------


def test_first_paint_flag_survives_scrollback_state_loss(tmp_path, monkeypatch):
    # SCOPE: this pins the FIRST-PAINT READINESS FLAG's durability only (the `.ready`
    # sidecar, re-hydrated after the in-memory scrollback caches are dropped) — which is
    # what the round-4 reconnect fix needs. It deliberately does NOT claim the SEED itself
    # survives a broker restart: the handoff store is in-memory and its durable-outbox work
    # is tracked in #709 (a fresh process after commit() would still find no pending seed).
    from agent_sessions import scrollback

    monkeypatch.setattr(scrollback, "_SCROLLBACK_DIR", tmp_path / "sb")
    key = "codex:new-abc"
    assert scrollback.first_paint_seen(key) is False
    scrollback.note_first_paint(key)
    assert scrollback.first_paint_seen(key) is True
    # Simulate the process losing its in-memory state (eviction / restart): the flag is
    # re-hydrated from the durable sidecar on the next touch.
    scrollback._drop_buffer(key)
    scrollback._READY.discard(key)
    scrollback._LOADED_FROM_DISK.discard(key)
    assert scrollback.first_paint_seen(key) is True  # from the .ready sidecar
    # clear_scrollback removes it (a wiped session isn't "ready" anymore).
    scrollback.clear_scrollback([key])
    scrollback._READY.discard(key)
    scrollback._LOADED_FROM_DISK.discard(key)
    assert scrollback.first_paint_seen(key) is False


def test_pending_seed_is_delivered_on_the_next_attach_to_an_idle_tui(tmp_path, monkeypatch):
    """#703 review round 4 — the documented "delivered on the next attach" path.

    Attach 1: the TUI arms 2004 and paints a full screen but the viewer disconnects before
    the quiet window, so the seed stays pending. Attach 2: an ALREADY-painted, now-idle TUI
    produces almost no live output — under the old gate `painted` (from this run's
    `out_bytes`) is false forever and the seed is never delivered. With durable first-paint
    it is delivered on attach 2.
    """
    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_SETTLE_S", 0.05)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "DELIVER-LATER", cwd="/tmp")
    key = handoff.commit(h)["id"]

    # --- Attach 1: paints, but the viewer leaves before the quiet window ---
    class DropAfterPaint:
        def __init__(self):
            self.seen = 0

        async def receive(self):
            await asyncio.sleep(10)
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            self.seen += len(b)

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    painter = (
        "import os, time, tty\n"
        "tty.setraw(0)\n"
        'os.write(1, b"\\x1b[?2004h")\n'
        'os.write(1, b"." * 4096)\n'  # a full first paint
        "time.sleep(30)\n"  # stays 'alive'; the ws is torn down before quiet
    )
    monkeypatch.setattr(webterm, "_SEED_QUIET_S", 10.0)  # quiet never satisfies in attach 1
    monkeypatch.setattr(webterm, "_SEED_READY_TIMEOUT_S", 1.0)

    async def _attach1() -> None:
        await asyncio.wait_for(
            webterm.run(
                DropAfterPaint(),
                [sys.executable, "-c", painter],
                cwd=str(tmp_path),
                buf_key=key,
                seed_key=key,
            ),
            timeout=3,
        )

    with contextlib.suppress(TimeoutError, Exception):
        asyncio.run(_attach1())
    assert webterm.scrollback.first_paint_seen(key) is True  # attach 1 recorded the paint
    assert handoff.has_pending_seed(key) is True  # …but didn't deliver (dropped pre-quiet)

    # --- Attach 2: an idle, already-painted TUI — barely any live output ---
    idle = (
        "import os, time, tty\n"
        "tty.setraw(0)\n"
        'os.write(1, b"\\x1b[?2004h")\n'
        'os.write(1, b"x")\n'  # < first-paint threshold: only the persisted flag can help
        "buf = b''\n"
        "end = time.time() + 8\n"
        "while time.time() < end and b'\\r' not in buf:\n"
        "    try: buf += os.read(0, 65536)\n"
        "    except OSError: break\n"
        'os.write(1, b"GOT[" + buf + b"]")\n'
    )
    monkeypatch.setattr(webterm, "_SEED_QUIET_S", 0.3)
    monkeypatch.setattr(webterm, "_SEED_READY_TIMEOUT_S", 6.0)
    out: list[bytes] = []
    asyncio.run(
        webterm.run(
            _fake_ws(out),
            [sys.executable, "-c", idle],
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    blob = b"".join(out)
    got = blob[blob.find(b"GOT[") :]
    assert b"\x1b[200~DELIVER-LATER\x1b[201~" in got  # delivered on the SECOND attach
    assert handoff.has_pending_seed(key) is False


# ---- #703 review follow-up: owner input is held until the seed lands ------------------

# A child that arms paste, paints, then accumulates ALL stdin for a fixed window and echoes
# it — so a test can assert the ORDER bytes arrived in (unlike _CHILD, which stops at CR).
_CHILD_RECORD = r"""
import os, sys, time, tty
tty.setraw(0)
os.write(1, b"\x1b[?2004h")
os.write(1, b"." * 4096)
buf = b""
end = time.time() + 2.5
os.set_blocking(0, False)
while time.time() < end:
    try:
        d = os.read(0, 65536)
        if d: buf += d
    except (BlockingIOError, OSError):
        pass
    time.sleep(0.02)
os.write(1, b"REC[" + buf + b"]")
"""


def _ws_sends_then_waits(collected, frames):
    """A fake WS that yields the given input frames (in order) on successive receive()
    calls, then blocks until the run ends. Records all output bytes into `collected`."""

    class FakeWS:
        def __init__(self):
            self._pending = list(frames)

        async def receive(self):
            if self._pending:
                await asyncio.sleep(0.05)
                return self._pending.pop(0)
            await asyncio.sleep(10)
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            collected.append(b)

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    return FakeWS()


def test_owner_input_is_queued_until_the_seed_lands_and_never_splits_it(tmp_path, monkeypatch):
    # #703 review follow-up: a keystroke typed while the seed is still pending must not beat
    # the seed to the prompt, nor land mid-paste. It is queued and flushed AFTER the seed.
    import json as _json

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_QUIET_S", 0.3)
    monkeypatch.setattr(webterm, "_SEED_SETTLE_S", 0.05)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "THE-SEED", cwd="/tmp")
    key = handoff.commit(h)["id"]
    out: list[bytes] = []
    # The user types "USERKEY" ~immediately, while the TUI is still booting (seed pending).
    frames = [{"text": _json.dumps({"t": "i", "d": "USERKEY"})}]
    asyncio.run(
        webterm.run(
            _ws_sends_then_waits(out, frames),
            [sys.executable, "-c", _CHILD_RECORD],
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    blob = b"".join(out)
    rec = blob[blob.find(b"REC[") :]
    paste = rec.find(b"\x1b[200~THE-SEED\x1b[201~")
    userkey = rec.find(b"USERKEY")
    assert paste != -1, "the seed paste never reached the child"
    assert userkey != -1, "the queued keystroke was lost"
    assert paste < userkey, "owner input must come AFTER the seed, never before/into it"
    # The bracketed-paste frame is intact (USERKEY didn't split it).
    assert b"\x1b[200~THE-SEED\x1b[201~\r" in rec


def test_owner_input_is_discarded_when_the_seed_is_not_delivered(tmp_path, monkeypatch):
    # If the TUI never becomes ready (seed stays pending for the next attach), queued owner
    # input is dropped rather than written ahead of the still-pending seed.
    import json as _json

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_READY_TIMEOUT_S", 0.4)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "PENDING", cwd="/tmp")
    key = handoff.commit(h)["id"]
    out: list[bytes] = []
    frames = [{"text": _json.dumps({"t": "i", "d": "TYPED-WHILE-BOOTING"})}]
    asyncio.run(
        webterm.run(
            _ws_sends_then_waits(out, frames),
            [sys.executable, "-c", _CHILD_PREAMBLE_ONLY],  # never arms paste → never ready
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    blob = b"".join(out)
    assert b"TYPED-WHILE-BOOTING" not in blob  # not written ahead of the pending seed
    assert handoff.has_pending_seed(key) is True  # seed still pending for the next attach


def test_spawn_watch_waits_for_a_slow_master_instead_of_aborting_it(tmp_home, monkeypatch):
    # #703 review follow-up: the watch is armed at connection-accept time, but spawning the
    # dtach master can take up to webterm.SPAWN_TIMEOUT_S. The instant-exit window must start
    # only once the master APPEARS — a fixed timer from arm-time would abort a valid 8-15 s
    # launch and delete its committed seed.
    from agent_sessions import ptybridge, relaunch, webterm
    from agent_sessions.routes import terminal as terminal_routes

    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "slow-seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    native = key.partition(":")[2]
    monkeypatch.setattr(relaunch, "_INSTANT_EXIT_S", 0.05)
    monkeypatch.setattr(webterm, "SPAWN_TIMEOUT_S", 2.0)
    monkeypatch.setattr(terminal_routes, "_SPAWN_APPEAR_POLL_S", 0.02)
    # The master is NOT up for the first ~0.5 s (a slow launch), then appears and stays.
    t0 = time.monotonic()
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: (time.monotonic() - t0) > 0.5)
    asyncio.run(terminal_routes._handoff_spawn_watch("claude", native))
    # The slow launch was NOT aborted — provenance was published once it came up.
    assert metadata.get(key).handoff_from == src  # marked spawned, not aborted
    assert handoff.has_pending_seed(key) is True  # seed intact (never abort_spawn'd)


def test_spawn_watch_aborts_a_master_that_never_appears(tmp_home, monkeypatch):
    # …but a master that never comes up within the spawn window IS a genuine failure → abort
    # (no dangling provenance, seed dropped).
    from agent_sessions import ptybridge, relaunch, webterm
    from agent_sessions.routes import terminal as terminal_routes

    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "never", cwd="/tmp")
    key = handoff.commit(h)["id"]
    native = key.partition(":")[2]
    monkeypatch.setattr(relaunch, "_INSTANT_EXIT_S", 0.05)
    monkeypatch.setattr(webterm, "SPAWN_TIMEOUT_S", 0.2)
    monkeypatch.setattr(terminal_routes, "_SPAWN_APPEAR_MARGIN_S", 0.1)
    monkeypatch.setattr(terminal_routes, "_SPAWN_APPEAR_POLL_S", 0.02)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)  # never comes up
    asyncio.run(terminal_routes._handoff_spawn_watch("claude", native))
    assert metadata.get(key).handoff_from == ""  # no provenance
    assert handoff.has_pending_seed(key) is False  # aborted


def test_spawn_watch_aborts_a_master_that_dies_in_the_instant_exit_window(tmp_home, monkeypatch):
    # The instant-exit check still applies AFTER the master appears: a master that comes up
    # then exits instantly (misconfigured launch) is aborted, not marked spawned.
    from agent_sessions import ptybridge, relaunch, webterm
    from agent_sessions.routes import terminal as terminal_routes

    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "flash", cwd="/tmp")
    key = handoff.commit(h)["id"]
    native = key.partition(":")[2]
    monkeypatch.setattr(webterm, "SPAWN_TIMEOUT_S", 1.0)
    monkeypatch.setattr(relaunch, "_INSTANT_EXIT_S", 0.3)
    monkeypatch.setattr(terminal_routes, "_SPAWN_APPEAR_POLL_S", 0.02)
    # Alive briefly (so it "appears"), then gone before the instant-exit window closes.
    t0 = time.monotonic()
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: (time.monotonic() - t0) < 0.15)
    asyncio.run(terminal_routes._handoff_spawn_watch("claude", native))
    assert metadata.get(key).handoff_from == ""  # came up then died → aborted
    assert handoff.has_pending_seed(key) is False


# --- source reference: session id + transcript location (#716) ------------------------------
# The seed is byte-capped, so a taking-over agent can't reach anything the cap dropped. The
# session id is ALWAYS named (free provenance); the transcript LOCATION is opt-in, because
# following it is what spends tokens. A location is emitted only when it resolves *this*
# session — never a guess, never a same-prefix neighbour, never a store that lacks the rows.


def test_claude_locator_resolves_the_exact_session_jsonl(fake_jsonl, tmp_home):
    from pathlib import Path

    from agent_sessions import transcript

    loc = transcript.source_location("claude", _SRC, tmp_home)
    assert loc is not None
    assert loc.endswith(f"{_SRC}.jsonl")
    assert Path(loc).is_file()


def test_locator_is_none_for_an_unresolvable_session_or_engine(fake_jsonl, tmp_home):
    from agent_sessions import transcript

    assert (
        transcript.source_location("claude", "99999999-9999-9999-9999-999999999999", tmp_home)
        is None
    )
    assert transcript.source_location("nosuchengine", _SRC, tmp_home) is None


def test_claude_locator_never_matches_a_prefix_neighbour(tmp_home):
    """A short id that PREFIXES a real filename must not resolve to that file: it is a
    different session, and naming it would point the target agent at someone else's history."""
    from agent_sessions import transcript

    proj = tmp_home / ".claude" / "projects" / "-p"
    proj.mkdir(parents=True)
    (proj / "abcdefff-1111-1111-1111-111111111111.jsonl").write_text("{}\n")
    assert transcript.source_location("claude", "abcdefff", tmp_home) is None


def test_opencode_locator_requires_rows_for_this_session(tmp_home, monkeypatch):
    """opencode has no per-session file — the DB existing says nothing about THIS session,
    so the locator resolves only when the session's rows are actually there, and it reads as
    a query rather than a file to cat."""
    import sqlite3

    from agent_sessions import transcript
    from agent_sessions.engines import base

    db = tmp_home / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
    conn.execute("INSERT INTO message VALUES ('m1','ses_real','{}')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(base, "_opencode_db", lambda home: str(db))

    loc = transcript.source_location("opencode", "ses_real", tmp_home)
    assert loc is not None
    assert "ses_real" in loc and "no per-session file" in loc
    # Same DB, a session with no rows: must NOT resolve.
    assert transcript.source_location("opencode", "ses_absent", tmp_home) is None


def test_source_location_is_fail_soft_when_a_locator_raises(fake_jsonl, tmp_home, monkeypatch):
    from agent_sessions import transcript

    def boom(native, home):
        raise OSError("store on fire")

    monkeypatch.setitem(transcript._LOCATORS, "claude", boom)
    assert transcript.source_location("claude", _SRC, tmp_home) is None


def test_seed_always_names_the_source_session_and_omits_the_location_by_default(fake_jsonl):
    seed, _ = handoff.build_quick_seed("claude", _SRC)
    assert f"- session: claude:{_SRC}" in seed
    assert "- transcript:" not in seed


def test_seed_carries_the_transcript_location_when_enabled(fake_jsonl):
    seed, _ = handoff.build_quick_seed("claude", _SRC, include_source_ref=True)
    assert f"- session: claude:{_SRC}" in seed
    assert "- transcript:" in seed and f"{_SRC}.jsonl" in seed
    assert "read it only if you need more context" in seed


def test_seed_omits_the_location_when_it_cannot_resolve(fake_jsonl, monkeypatch):
    """Option on but the session doesn't resolve ⇒ the line is dropped entirely; the id
    (which needs no lookup) still stands."""
    from agent_sessions import transcript

    monkeypatch.setitem(transcript._LOCATORS, "claude", lambda n, h: None)
    seed, _ = handoff.build_quick_seed("claude", _SRC, include_source_ref=True)
    assert f"- session: claude:{_SRC}" in seed
    assert "- transcript:" not in seed


def test_seed_stays_within_the_cap_with_the_source_reference_on(fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    turns = [transcript.Turn("user", "x" * 5000, "text") for _ in range(20)]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seed, meta = handoff.build_quick_seed("claude", _SRC, include_source_ref=True)
    assert len(seed.encode("utf-8")) <= handoff.SEED_CAP_BYTES
    assert meta["bytes"] <= handoff.SEED_CAP_BYTES


def test_ai_seed_carries_the_source_reference_too(fake_jsonl, monkeypatch):
    from agent_sessions import review

    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    seed, _ = asyncio.run(handoff.build_ai_seed("claude", _SRC, include_source_ref=True))
    assert f"- session: claude:{_SRC}" in seed
    assert "- transcript:" in seed and f"{_SRC}.jsonl" in seed


def test_ai_review_payload_never_carries_the_transcript_location(fake_jsonl, monkeypatch):
    """The locator is for the TARGET engine only. The AI-review endpoint summarizes the
    transcript tail and must never be told where that transcript lives — which is why the
    source header is composed only AFTER the endpoint call returns."""
    from agent_sessions import review, transcript

    loc = transcript.source_location("claude", _SRC, __import__("pathlib").Path.home())
    assert loc  # precondition: it really does resolve, so absence below is meaningful

    monkeypatch.setattr(review, "complete_json", _fake_complete(_AI_OBJ))
    seed, _ = asyncio.run(handoff.build_ai_seed("claude", _SRC, include_source_ref=True))
    assert loc in seed  # the target does get it
    sent = "".join(m["content"] for m in _fake_complete.seen)
    assert loc not in sent
    assert "- transcript:" not in sent


def test_prepare_round_trips_the_source_ref_flag(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)

    off = _prepare(c, csrf, auth_cfg)
    assert off.status_code == 200, off.text
    assert f"- session: claude:{_SRC}" in off.json()["preview"]
    assert "- transcript:" not in off.json()["preview"]  # default off

    on = _prepare(c, csrf, auth_cfg, include_source_ref=True)
    assert on.status_code == 200, on.text
    assert "- transcript:" in on.json()["preview"]
    assert f"{_SRC}.jsonl" in on.json()["preview"]


def test_source_reference_uses_the_same_logical_id_the_transcript_was_read_from(
    auth_cfg, fake_jsonl, monkeypatch
):
    """The id in the header and the locator must be derived from the SAME alias-resolved
    logical key the transcript read uses (#611/#716).

    `parse_key` rejects a raw `new-<uuid>` placeholder, so a placeholder never reaches this
    route as a source — but the route still resolves `logical_key` before reading history, and
    the danger is *divergence*: building `- session:`/`- transcript:` from the raw `source_id`
    while the turns come from the logical one would hand the target agent a pointer to a
    different session than the brief it just read. Forcing the two apart proves they agree."""
    from agent_sessions import transcript

    real = "77777777-7777-7777-7777-777777777777"
    monkeypatch.setattr(engines, "logical_key", lambda key, aliases=None: f"claude:{real}")
    monkeypatch.setattr(
        transcript,
        "adapter_for",
        lambda e: (
            lambda native, home: (
                [transcript.Turn("user", "real upstream history", "text")] if native == real else []
            )
        ),
    )
    monkeypatch.setitem(
        transcript._LOCATORS,
        "claude",
        lambda native, home: f"/store/{native}.jsonl" if native == real else None,
    )

    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, include_source_ref=True)
    assert r.status_code == 200, r.text
    preview = r.json()["preview"]
    # Brief, id and locator all describe the SAME session — the logical one.
    assert "real upstream history" in preview
    assert f"- session: claude:{real}" in preview
    assert f"/store/{real}.jsonl" in preview
    assert _SRC not in preview


def test_prepare_rejects_a_non_boolean_source_ref_instead_of_coercing_it(
    auth_cfg, fake_jsonl, monkeypatch
):
    """The flag gates a privacy disclosure, so it is validated, never coerced: `bool("false")`
    is True, and a client sending the STRING "false" must not silently opt in and embed a local
    path in the seed (Hermes on #717)."""
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    for bad in ("false", "0", 1, [], {}):
        r = _prepare(c, csrf, auth_cfg, include_source_ref=bad)
        assert r.status_code == 422, f"{bad!r} should be rejected, got {r.status_code}"
        assert "boolean" in r.json()["detail"]
    # A real boolean still works, both ways.
    assert (
        "- transcript:"
        not in _prepare(c, csrf, auth_cfg, include_source_ref=False).json()["preview"]
    )
    assert "- transcript:" in _prepare(c, csrf, auth_cfg, include_source_ref=True).json()["preview"]
