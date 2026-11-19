"""#956 — settings that no longer did anything are gone, and the doors that remain are honest.

Two removals, each pinned from both sides:

* the ``medium`` scan depth (and the banner prompt it existed for) — and since #2179 the whole
  depth, with the scan: a stored value is not read and stays in the file; a WRITE is refused.
* prompt writes through ``/api/prefs``: the three prompts that predate the registry still store in
  their feature blocks, but only ``PATCH /api/prompts/{id}`` may write them, and the public
  config views no longer carry copies nobody read.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_sessions import prefs, prompts
from agent_sessions.main import create_app

LEGACY_PROMPT_BLOCKS = [
    ("ai_review", "tail_review"),
    ("auto_sort", "auto_sort"),
    ("orchestrator", "orchestrator_pass"),
]


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg) -> dict:
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def _write_raw_prefs(doc: dict) -> None:
    path = prefs._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


# ---- the scan depth (medium #956; the whole depth, with the scan, #2179) ----------------


def test_a_stored_depth_is_no_longer_read_and_stays_in_the_file():
    _write_raw_prefs({"pulse": {"scan_depth": "medium", "auto_enabled": True, "window_days": 2}})
    assert prefs.get_pulse() == {"window_days": 2}
    assert json.loads(prefs._default_path().read_text())["pulse"]["scan_depth"] == "medium"


def test_writing_a_depth_is_refused_not_coerced(auth_cfg, fake_jsonl):  # noqa: ARG001
    assert prefs.validate_pulse_patch({"scan_depth": "medium"}) is not None
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    for depth in ("medium", "fast", "slow"):
        r = c.post("/api/prefs", json={"pulse": {"scan_depth": depth}}, headers=hdr)
        assert r.status_code == 422, depth
    assert "scan_depth" not in c.get("/api/config").json()["pulse"]


def test_a_stored_override_for_the_retired_session_line_prompt_is_ignored_and_left_alone():
    """#2179: `pulse_session_line` went with the scan; an override an operator saved for it is
    operator data — never read again, never deleted."""
    _write_raw_prefs(
        {"ai_prompts": {"pulse_session_line": "OLD LINE TEXT", "session_recap": "Write three."}}
    )
    assert "pulse_session_line" not in [e["id"] for e in prompts.catalog()]
    assert "OLD LINE TEXT" not in prompts.effective_set()
    with pytest.raises(prompts.UnknownPromptError):
        prompts.get("pulse_session_line")
    assert prompts.editable("session_recap") == "Write three."
    stored = json.loads(prefs._default_path().read_text())
    assert stored["ai_prompts"]["pulse_session_line"] == "OLD LINE TEXT"


def test_the_orchestrator_prompt_still_resolves_from_its_block():
    """#2179 removed `POST /api/prefs {orchestrator}`, not the stored field it shared: the prompt
    registry still reads `orchestrator.prompt` (and appends the guard, the prompt is guarded)."""
    _write_raw_prefs({"orchestrator": {"prompt": "Operator-written pass prompt."}})
    assert prompts.editable("orchestrator_pass") == "Operator-written pass prompt."
    assert prompts.effective("orchestrator_pass").startswith("Operator-written pass prompt.")
    assert prompts.guard_suffix("orchestrator_pass") is not None


def test_a_stored_override_for_the_removed_banner_prompt_is_ignored_and_left_alone():
    _write_raw_prefs(
        {"ai_prompts": {"pulse_banner": "OLD BANNER TEXT", "session_recap": "Write three lines."}}
    )
    assert "pulse_banner" not in [e["id"] for e in prompts.catalog()]
    assert "OLD BANNER TEXT" not in prompts.effective_set()
    with pytest.raises(prompts.UnknownPromptError):
        prompts.get("pulse_banner")
    # Its neighbours in the same block still read, and the orphan is not deleted (operator data).
    assert prompts.editable("session_recap") == "Write three lines."
    stored = json.loads(prefs._default_path().read_text())
    assert stored["ai_prompts"]["pulse_banner"] == "OLD BANNER TEXT"


# ---- prompt writes through /api/prefs --------------------------------------------------


@pytest.mark.parametrize(("block", "pid"), LEGACY_PROMPT_BLOCKS)
def test_prefs_refuses_a_prompt_field_and_names_the_route_that_writes_it(
    auth_cfg, fake_jsonl, block, pid
):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    before = prompts.editable(pid)
    r = c.post("/api/prefs", json={block: {"prompt": "INJECTED THROUGH PREFS"}}, headers=hdr)
    assert r.status_code == 422
    assert f"/api/prompts/{pid}" in r.json()["detail"]
    assert prompts.editable(pid) == before

    # The one real door still writes the same stored field.
    ok = c.patch(
        f"/api/prompts/{pid}", json={"value": "Operator text via the catalog."}, headers=hdr
    )
    assert ok.status_code == 200
    assert prompts.editable(pid).startswith("Operator text via the catalog.")
    assert prefs._load(prefs._default_path())[block]["prompt"].startswith(
        "Operator text via the catalog."
    )


@pytest.mark.parametrize(("block", "_pid"), LEGACY_PROMPT_BLOCKS)
def test_public_config_views_carry_no_prompt_copies(auth_cfg, fake_jsonl, block, _pid):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    view = c.get("/api/config").json()[block]
    assert "prompt" not in view
    assert "default_prompt" not in view


def test_a_prompt_field_rides_nowhere_in_a_prefs_echo(auth_cfg, fake_jsonl):
    """The POST echo is the same public view, so it must not reintroduce the copies either."""
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"ai_review": {"interval_minutes": 9}, "auto_sort": {"max_per_pass": 4}},
        headers=hdr,
    )
    assert r.status_code == 200
    for block in ("ai_review", "auto_sort"):
        assert "prompt" not in r.json()[block] and "default_prompt" not in r.json()[block]
