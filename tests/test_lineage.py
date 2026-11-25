"""Session lineage (#2325): the ledger, the projection and process attribution."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import pytest

from agent_sessions import lineage, lineage_attribution

A, B, C, D = (f"claude:{c * 8}-{c * 4}-{c * 4}-{c * 4}-{c * 12}" for c in "abcd")


def row(key, **kw):
    base = {"id": key, "title": key[-4:], "created_at": 0}
    base.update(kw)
    return base


# --- ledger ------------------------------------------------------------------------------------


def test_link_and_precedence():
    assert lineage.link(B, A, "process") is True
    # An equal or higher source replaces a lower one; a lower one never replaces a higher one.
    assert lineage.link(B, C, "launch") is True
    assert lineage.links_snapshot()[B]["parent"] == C
    assert lineage.link(B, A, "process") is False
    assert lineage.links_snapshot()[B] == {**lineage.links_snapshot()[B], "parent": C}
    assert lineage.link(B, A, "operator") is True
    assert lineage.links_snapshot()[B]["source"] == "operator"


def test_self_link_and_cycle_are_refused():
    with pytest.raises(lineage.LineageError):
        lineage.link(A, A, "launch")
    lineage.link(B, A, "launch")
    lineage.link(C, B, "launch")
    with pytest.raises(lineage.LineageError):
        lineage.link(A, C, "launch")  # A → C → B → A
    assert A not in lineage.links_snapshot()


def test_unreadable_ledger_is_unknown_never_rewritten(tmp_path, monkeypatch):
    p = tmp_path / "lineage.json"
    p.write_text("{not json")
    monkeypatch.setenv("AGENT_SESSIONS_LINEAGE", str(p))
    with pytest.raises(lineage.LineageUnreadable):
        lineage.read_checked()
    with pytest.raises(lineage.LineageUnreadable):
        lineage.link(B, A, "launch")
    assert p.read_text() == "{not json"
    # Display readers: every row a main session, never "no sessions".
    proj = lineage.project([row(A), row(B)])
    assert proj.known is False and [r["id"] for r in proj.mains] == [A, B]
    assert lineage.child_keys_of({A, B}) == set()


def test_malformed_link_is_unreadable(tmp_path, monkeypatch):
    p = tmp_path / "lineage.json"
    p.write_text(json.dumps({"links": {B: {"parent": A, "source": "guess", "at": 1}}}))
    monkeypatch.setenv("AGENT_SESSIONS_LINEAGE", str(p))
    with pytest.raises(lineage.LineageUnreadable):
        lineage.read_checked()


def test_bindings_are_exact_names_only():
    unit_a = "as-claude-aaaaaaaa-0001beef.scope"
    unit_b = "as-claude-aaaaaaaa-0002beef.scope"  # same sid8 prefix, another session
    lineage.bind_scope(unit_a, A)
    lineage.bind_scope(unit_b, B)
    doc = lineage.read_checked()
    assert lineage.scope_key(unit_a, doc) == A
    assert lineage.scope_key(unit_b, doc) == B
    assert lineage.scope_key("as-claude-aaaaaaaa-0003beef.scope", doc) is None
    assert lineage.scope_key("as-claude-aaaaaaaa", doc) is None


def test_attribution_retry_schedule_then_settles():
    t = 1000.0
    states = []
    for _ in range(lineage.MAX_TRIES):
        assert lineage.attribution_due([A], now=t) == [A]
        states.append(lineage.record_attribution(A, "unknown", now=t))
        a = lineage.read_checked()["attribution"][A]
        if a["state"] == "unknown":
            assert lineage.attribution_due([A], now=t) == []  # not due before its backoff
            t = a["next_at"]
    assert states == ["unknown"] * (lineage.MAX_TRIES - 1) + ["none-final"]
    assert lineage.attribution_due([A], now=t + 10_000) == []


def test_attribution_linked_writes_a_process_link_and_final_states_stop():
    assert lineage.record_attribution(B, "linked", parent=A, now=5.0) == "linked"
    assert lineage.links_snapshot()[B] == {"parent": A, "source": "process", "at": 5.0}
    assert lineage.attribution_due([B, C], now=6.0) == [C]
    lineage.record_attribution(C, "none", now=6.0)
    assert lineage.attribution_due([C], now=1e12) == []


def test_attribution_linked_that_would_cycle_is_a_conflict():
    lineage.link(B, A, "launch")
    assert lineage.record_attribution(A, "linked", parent=B) == "conflict"
    assert A not in lineage.links_snapshot()
    assert lineage.record_attribution(C, "linked", parent=C) == "conflict"


def test_record_attributions_is_one_write(monkeypatch):
    writes = []
    real = lineage.atomic_write_json
    monkeypatch.setattr(
        lineage, "atomic_write_json", lambda *a, **k: (writes.append(1), real(*a, **k))
    )
    lineage.record_attributions([(A, "none", None), (B, "unknown", None), (C, "linked", A)])
    assert len(writes) == 1


def test_trim_bounds_the_document(monkeypatch):
    monkeypatch.setattr(lineage, "MAX_BINDINGS", 3)
    for i in range(5):
        lineage.bind_scope(f"as-x-{i}.scope", A, now=float(i))
    assert sorted(lineage.read_checked()["bindings"]) == [f"as-x-{i}.scope" for i in (2, 3, 4)]


# --- projection --------------------------------------------------------------------------------


def test_project_splits_mains_children_and_orphans():
    lineage.link(B, A, "launch", now=2)
    lineage.link(C, A, "process", now=1)
    lineage.link(D, "claude:gone", "launch")
    rows = [row(A), row(B), row(C), row(D)]
    proj = lineage.project(rows)
    assert [r["id"] for r in proj.mains] == [A, D]
    assert [r["id"] for r in proj.children[A]] == [C, B]  # spawn order
    assert rows[1]["parent_key"] == A and rows[1]["lineage_source"] == "launch"
    assert rows[3]["orphan"] is True and "parent_key" not in rows[3]
    assert rows[0]["child_count"] == 2 and rows[0]["descendant_count"] == 2


def test_rollup_and_attention_are_transitive():
    lineage.link(B, A, "launch")
    lineage.link(C, B, "launch")  # grandchild
    rows = [row(A), row(B, working=True), row(C, intervention_required=True)]
    proj = lineage.project(rows)
    main = rows[0]
    assert main["rollup"] == "attention"
    assert main["attention_count"] == 1 and main["attention"] == [C]
    assert rows[1]["rollup"] == "attention"  # the child's own subtree
    [(item, path)] = proj.attention(A)
    assert item["id"] == C and [p["id"] for p in path] == [A, B]


def test_review_excluded_child_is_not_attention():
    lineage.link(B, A, "launch")
    rows = [row(A), row(B, intervention_required=True, review_excluded=True, has_draft=True)]
    lineage.project(rows)
    assert rows[0]["rollup"] == "draft" and rows[0]["attention_count"] == 0


def test_attention_is_uncapped_beyond_the_preview():
    kids = [f"claude:{i:08d}-0000-0000-0000-000000000000" for i in range(12)]
    for i, k in enumerate(kids):
        lineage.link(k, A, "launch", now=float(i))
    rows = [row(A)] + [row(k, intervention_required=True) for k in kids]
    proj = lineage.project(rows)
    assert rows[0]["attention_count"] == 12
    assert rows[0]["attention"] == kids[: lineage.ATTENTION_PREVIEW]
    assert [r["id"] for r, _ in proj.attention(A)] == kids


def test_a_session_listed_twice_still_carries_every_aggregate():
    """A provider that lists one session twice (gemini's header-only resume files did) must not
    leave the second row without the aggregates: the filtered list reads `attention_count` on every
    row, and one missing key failed every sidebar search and HQ list with a 500."""
    lineage.link(B, A, "launch")
    rows = [row(A), row(B), row(B), row(C), row(C)]
    lineage.project(rows)
    for r in rows:
        assert r["attention_count"] == 0 and r["attention"] == []
        assert "child_count" in r and "descendant_count" in r and "rollup" in r


def test_a_hand_made_cycle_promotes_its_members_rather_than_hiding_them(tmp_path, monkeypatch):
    p = tmp_path / "lineage.json"
    p.write_text(
        json.dumps(
            {
                "links": {
                    A: {"parent": B, "source": "launch", "at": 1},
                    B: {"parent": A, "source": "launch", "at": 1},
                }
            }
        )
    )
    monkeypatch.setenv("AGENT_SESSIONS_LINEAGE", str(p))
    rows = [row(A), row(B), row(C)]
    proj = lineage.project(rows)
    shown = {r["id"] for r in proj.mains} | {
        c["id"] for kids in proj.children.values() for c in kids
    }
    assert shown == {A, B, C}


def test_child_keys_of_needs_the_parent_in_the_set():
    lineage.link(B, A, "launch")
    lineage.link(C, D, "launch")
    assert lineage.child_keys_of({A, B, C}) == {B}


def test_links_snapshot_is_cached_per_generation(monkeypatch):
    lineage.link(B, A, "launch")
    first = lineage.links_snapshot()
    reads = []
    real = lineage.read_checked
    monkeypatch.setattr(
        lineage, "read_checked", lambda *a, **k: (reads.append(1), real(*a, **k))[1]
    )
    assert lineage.links_snapshot() is first
    assert reads == []
    lineage.link(C, A, "launch")
    assert C in lineage.links_snapshot()


# --- attribution -------------------------------------------------------------------------------


@pytest.fixture
def procs(monkeypatch):
    """Fake ``/proc``: pid → (unit, BATTLELAB_SESSION | None) or an exception to raise."""
    table: dict[int, object] = {}

    def unit_of(pid):
        v = table[pid]
        if isinstance(v, BaseException):
            raise v
        return v[0]

    def env_value(pid):
        v = table[pid]
        if isinstance(v, BaseException):
            raise v
        return v[1]

    monkeypatch.setattr(lineage_attribution, "_unit_of", unit_of)
    monkeypatch.setattr(lineage_attribution, "_env_value", env_value)
    return table


def _eval(key, pids, known=(A, B, C)):
    return lineage_attribution.evaluate(key, pids, lineage.read_checked(), set(known), {})


def test_inherited_env_names_the_parent(procs):
    procs[10] = ("user@1000.service", A)
    assert _eval(B, [10]) == ("linked", A)


def test_own_env_is_not_evidence(procs):
    procs[10] = ("user@1000.service", B)
    assert _eval(B, [10]) == ("none", None)
    procs[11] = ("user@1000.service", None)
    assert _eval(B, [11]) == ("none", None)


def test_exact_scope_binding_names_the_parent_and_a_sid8_twin_does_not(procs):
    lineage.bind_scope("as-claude-aaaaaaaa-11111111.scope", A)
    procs[10] = ("as-claude-aaaaaaaa-11111111.scope", None)
    assert _eval(B, [10]) == ("linked", A)
    # Same engine and sid8 prefix, another nonce, never bound: no evidence at all.
    procs[11] = ("as-claude-aaaaaaaa-22222222.scope", None)
    assert _eval(B, [11]) == ("none", None)


def test_own_scope_is_not_evidence(procs):
    lineage.bind_scope("as-claude-bbbbbbbb-1.scope", B)
    procs[10] = ("as-claude-bbbbbbbb-1.scope", B)
    assert _eval(B, [10]) == ("none", None)


def test_cgroup_and_env_disagreeing_is_a_conflict(procs):
    lineage.bind_scope("as-claude-aaaaaaaa-1.scope", A)
    procs[10] = ("as-claude-aaaaaaaa-1.scope", C)
    assert _eval(B, [10]) == ("conflict", None)


def test_a_parent_nobody_lists_is_a_conflict(procs):
    procs[10] = ("x.service", D)
    assert _eval(B, [10], known=(A, B)) == ("conflict", None)


def test_unreadable_proc_is_unknown_and_vanished_pids_are_unknown(procs):
    procs[10] = PermissionError("no")
    assert _eval(B, [10]) == ("unknown", None)
    procs[11] = FileNotFoundError("gone")
    assert _eval(B, [11]) == ("unknown", None)


def test_native_worker_unit_maps_through_its_config(procs, monkeypatch):
    from agent_sessions import native_state

    worker = "0123456789abcdef0123456789abcdef"
    wid = f"{worker[:8]}-{worker[8:12]}-{worker[12:16]}-{worker[16:20]}-{worker[20:]}"
    monkeypatch.setattr(
        native_state, "read_config", lambda w: {"session_key": A} if w == wid else {}
    )
    procs[10] = (f"battlelab-native-{worker}.service", None)
    assert _eval(B, [10]) == ("linked", A)


def test_placeholder_parent_resolves_through_aliases(procs):
    placeholder = "codex:new-11111111-2222-3333-4444-555555555555"
    procs[10] = ("x.service", placeholder)
    out = lineage_attribution.evaluate(B, [10], lineage.read_checked(), {A, B}, {placeholder: A})
    assert out == ("linked", A)


@dataclass
class _S:
    engine: str
    uuid: str
    last_mtime: float
    archived: bool = False


class _Prov:
    def __init__(self, answer):
        self.answer = answer

    def session_pids(self, natives):
        return self.answer


def _walk(monkeypatch, sessions, provs):
    from agent_sessions import engines

    monkeypatch.setattr(engines, "scan_all_cached", lambda: sessions)
    monkeypatch.setattr(engines, "get_any", lambda e: provs.get(e))


def test_sweep_settles_stale_dead_sessions_and_retries_fresh_ones(monkeypatch, procs):
    now = 100_000.0
    old = _S("claude", A.split(":")[1], now - 10_000)
    fresh = _S("claude", B.split(":")[1], now - 5)
    _walk(monkeypatch, [old, fresh], {"claude": _Prov({})})
    assert lineage_attribution.sweep(now=now) == 2
    att = lineage.read_checked()["attribution"]
    assert att[A]["state"] == "none-final"
    assert att[B]["state"] == "unknown"


def test_sweep_links_through_a_live_pid(monkeypatch, procs):
    parent = _S("claude", A.split(":")[1], time.time())
    child = _S("claude", B.split(":")[1], time.time())
    procs[42] = ("user.slice", A)
    _walk(monkeypatch, [parent, child], {"claude": _Prov({child.uuid: [42]})})
    lineage_attribution.sweep()
    assert lineage.links_snapshot()[B]["parent"] == A
    assert lineage.read_checked()["attribution"][A]["state"] == "unknown"  # no pid yet, fresh


def test_sweep_without_the_hook_is_unavailable(monkeypatch):
    s = _S("other", "x1", time.time())
    _walk(monkeypatch, [s], {"other": object()})
    lineage_attribution.sweep()
    assert lineage.read_checked()["attribution"]["other:x1"]["state"] == "unavailable"


def test_sweep_uses_the_native_hook_first(monkeypatch):
    class Native(_Prov):
        def lineage(self, native):
            return A if native == B.split(":")[1] else None

    parent = _S("claude", A.split(":")[1], time.time())
    child = _S("claude", B.split(":")[1], time.time())
    _walk(monkeypatch, [parent, child], {"claude": Native({})})
    lineage_attribution.sweep()
    assert lineage.links_snapshot()[B] == {**lineage.links_snapshot()[B], "source": "native"}


def test_sweep_does_nothing_when_the_ledger_is_unreadable(tmp_path, monkeypatch):
    p = tmp_path / "lineage.json"
    p.write_text("[]")
    monkeypatch.setenv("AGENT_SESSIONS_LINEAGE", str(p))
    _walk(monkeypatch, [_S("claude", A.split(":")[1], time.time())], {"claude": _Prov({})})
    assert lineage_attribution.sweep() == 0
    assert p.read_text() == "[]"


def test_projection_of_a_deep_chain_is_linear(monkeypatch):
    """#2325 review: aggregates come from one bottom-up pass. A 3,000-deep chain used to visit
    ~4.5 M descendants (a per-row walk); now each row's state is read once."""
    n = 3000
    keys = [f"claude:{i:08d}-0000-0000-0000-000000000000" for i in range(n)]
    links = {
        keys[i]: {"parent": keys[i - 1], "source": "launch", "at": float(i)} for i in range(1, n)
    }
    calls = []
    real = lineage.row_state
    monkeypatch.setattr(lineage, "row_state", lambda r: (calls.append(1), real(r))[1])
    rows = [row(k) for k in keys]
    rows[-1]["intervention_required"] = True
    proj = lineage.project(rows, links=links)
    assert [r["id"] for r in proj.mains] == [keys[0]]
    head = rows[0]
    assert head["descendant_count"] == n - 1 and head["child_count"] == 1
    assert head["rollup"] == "attention"
    assert head["attention_count"] == 1 and head["attention"] == [keys[-1]]
    assert rows[n - 2]["attention"] == [keys[-1]] and rows[-1]["attention_count"] == 0
    assert len(calls) <= 2 * n  # linear, not quadratic
    # The lazy route still enumerates the one key it is asked about, with its full path.
    [(item, path)] = proj.attention(keys[0])
    assert item["id"] == keys[-1] and len(path) == n - 1


def test_preview_keeps_tree_order_across_branches():
    lineage.link(B, A, "launch", now=1)
    lineage.link(C, A, "launch", now=2)
    lineage.link(D, B, "launch", now=3)
    rows = [row(A), row(B, intervention_required=True), row(C, intervention_required=True)]
    rows.append(row(D, intervention_required=True))
    proj = lineage.project(rows)
    assert rows[0]["attention"] == [B, D, C]
    assert [r["id"] for r, _ in proj.attention(A)] == [B, D, C]
