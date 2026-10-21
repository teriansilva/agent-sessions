"""#853 P4 — the SPA's test roster is generated from the manifests, and must not drift from them.

`web/src/test/roster.fixture.json` is what every Vitest renders agents with. If it were written by
hand it would be a third copy of the roster — the #454 drift shape this whole epic retires — so
this compares every manifest-derived field with what `/api/engines` actually serves. Host state
(`present`, `bin`) is fixed in the fixture and not compared.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from agent_sessions.main import create_app
from agent_sessions.plugins.kinds import ACCENT_TOKENS

WEB = Path(__file__).resolve().parents[1] / "web" / "src"
FIXTURE = WEB / "test" / "roster.fixture.json"
MANIFEST_FIELDS = (
    "id",
    "label",
    "kind",
    "runtime",
    "display",
    "capabilities",
    "session_id",
    "models",
    "model_select",
    "instructions",
    "usage",
    "terminal",
    "status",
    "status_reason",
)


def test_the_web_roster_fixture_is_exactly_what_the_manifests_serve(auth_cfg, engine_bin):
    engine_bin()
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert (
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        ).status_code
        == 303
    )
    served = c.get("/api/engines").json()["engines"]
    fixture = json.loads(FIXTURE.read_text())["engines"]
    pick = lambda rows: [{k: r[k] for k in MANIFEST_FIELDS} for r in rows]  # noqa: E731
    assert pick(fixture) == pick(
        served
    ), "web/src/test/roster.fixture.json drifted from the manifests — regenerate it"
    # The capability-derived host answers agree too (every fixture engine is present).
    for f, s in zip(fixture, served, strict=True):
        assert (f["supports_new"], f["supports_seed_start"]) == (
            s["supports_new"],
            s["supports_seed_start"],
        ), f["id"]


def test_every_manifest_accent_has_a_web_token_and_a_contrast_check():
    """A manifest accent the SPA has no entry for silently renders slate. The three copies of the
    vocabulary — `kinds.ACCENT_TOKENS`, `engineRoster.ts`'s `ACCENT_VAR`, the contrast test's
    `ENGINE_ACCENTS` — and the `--engine-*` tokens must name the same set."""
    roster = (WEB / "app" / "engineRoster.ts").read_text()
    block = re.search(r"const ACCENT_VAR[^{]*\{(.*?)\};", roster, re.S)
    assert block, "ACCENT_VAR not found in engineRoster.ts"
    var_map = dict(re.findall(r'(\w+):\s*"var\(--engine-(\w+)\)"', block.group(1)))
    assert set(var_map) == ACCENT_TOKENS
    assert all(k == v for k, v in var_map.items()), var_map
    contrast = (WEB / "theme" / "contrast.test.ts").read_text()
    listed = re.search(r"const ENGINE_ACCENTS = \[(.*?)\];", contrast, re.S)
    assert listed, "ENGINE_ACCENTS not found in contrast.test.ts"
    assert set(re.findall(r'"(\w+)"', listed.group(1))) == ACCENT_TOKENS
    tokens = set(re.findall(r"--engine-(\w+):", (WEB / "tokens.css").read_text()))
    assert tokens == ACCENT_TOKENS
