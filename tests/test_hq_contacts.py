"""HQ contacts (#1525): the closed set of contacts, their registered voices, and the pref.

What is pinned here (the turn itself — persona first, guarded prompt last, through the real
outbound guard — is in `tests/test_ask_orchestrator.py`):

* **the set** — the 13 ids and names, and the client's gallery (`contacts.ts`) names the same ids;
* **the voices are registry entries** — `hq_contacts.prompt_id` returns only a registered
  `hq_persona_*` id and raises for anything else (the registry checker trusts exactly that), and
  each default carries the tone-only and archetype-not-stereotype rules;
* **the pref** — `hq_contact` is a contact id | "random" | "off", default "random"; an unknown or
  removed id READS as the default; the write is strict (422, nothing persisted).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import hq_contacts, prefs, prompts
from agent_sessions.main import create_app

CLIENT_MANIFEST = (
    Path(__file__).resolve().parents[1] / "web" / "src" / "components" / "hq" / "contacts.ts"
)

NAMES = {
    "asia-woman": "General Hana Seo",
    "asia-man": "General Kenji Mori",
    "europe-woman": "General Claire Vidal",
    "europe-man": "General Lukas Brandt",
    "russia-woman": "General Irina Volkova",
    "russia-man": "General Dmitri Sokolov",
    "us-woman": "General Jessica Hale",
    "us-man": "General Jack Harlan",
    "africa-woman": "General Amara Okafor",
    "africa-man": "General Kwame Mensah",
    "southamerica-woman": "General Valentina Cruz",
    "southamerica-man": "General Mateo Ríos",
    "romania-woman": "General Ileana Lupu",
}


# ---- the set and its voices -------------------------------------------------------------------


def test_the_thirteen_contacts_and_their_names():
    assert list(hq_contacts.IDS) == list(NAMES)
    assert {c.id: c.name for c in hq_contacts.CONTACTS} == NAMES
    assert hq_contacts.PREF_VALUES == (*NAMES, "random", "off")


@pytest.mark.parametrize("contact", hq_contacts.IDS)
def test_each_voice_is_its_own_registered_prompt(contact):
    pid = hq_contacts.prompt_id(contact)
    assert pid == "hq_persona_" + contact.replace("-", "_")
    p = prompts.get(pid)
    assert not p.guarded and p.locked is None  # tone only; the guarded prompt follows it
    text = p.default
    assert text.startswith(f"You are {NAMES[contact]}, the Commander's HQ contact. ")
    assert "Address the operator as Commander." in text
    assert "never a stereotype" in text
    assert "no imitated accent or dialect spelling" in text
    assert "no jokes about anyone's ethnicity or nationality" in text
    assert "no real political slogans" in text
    assert text.endswith(
        "The voice is tone only: every fact, tool call, proposal and outcome follows the system "
        "instructions that follow exactly, and you never invent events, people or results to "
        "stay in character."
    )
    assert len(text) <= p.max_chars


def test_the_persona_prompts_are_exactly_the_contacts():
    personas = [pid for pid in prompts.IDS if pid.startswith(hq_contacts.PROMPT_PREFIX)]
    assert personas == list(hq_contacts.PROMPT_IDS)
    assert len(set(hq_contacts.PROMPT_IDS)) == 13


@pytest.mark.parametrize(
    "value",
    ["off", "random", "", "asia_woman", "hq_persona_asia_woman", "General Hana Seo", "ASIA-MAN"],
)
def test_prompt_id_refuses_anything_but_a_contact(value):
    """The registry checker accepts `prompts.effective(hq_contacts.prompt_id(…))` because this
    can only ever return a registered persona id: everything else raises, never falls back."""
    with pytest.raises(KeyError):
        hq_contacts.prompt_id(value)


@pytest.mark.parametrize(
    "value",
    ["off", "random", "removed-general", "", None, 3, ["asia-man"], {"id": "asia-man"}, "asia_man"],
    ids=["off", "random", "removed", "empty", "null", "number", "list", "object", "prompt-form"],
)
def test_resolve_makes_anything_unknown_off(value):
    assert hq_contacts.resolve(value) == "off"


@pytest.mark.parametrize("contact", hq_contacts.IDS)
def test_resolve_keeps_a_known_contact(contact):
    assert hq_contacts.resolve(contact) == contact


def _client_ids(src: str) -> list[str]:
    # Entries are written `contact("<id>", "<name>", …)` or as objects with `id: "<id>"`.
    return re.findall(r'(?:\bcontact\(\s*|\bid:\s*)"([a-z][a-z-]*)"', src)


def test_server_and_client_name_the_same_contacts():
    if not CLIENT_MANIFEST.exists():
        pytest.skip("web/src/components/hq/contacts.ts is not in this tree yet")
    ids = _client_ids(CLIENT_MANIFEST.read_text())
    assert ids, "no contact ids found in contacts.ts — has its entry shape changed?"
    assert len(ids) == len(set(ids))
    assert set(ids) == set(hq_contacts.IDS)


# ---- the pref ---------------------------------------------------------------------------------


def test_the_pref_defaults_to_random(tmp_path):
    assert prefs.get_hq_contact(tmp_path / "prefs.json") == "random"


@pytest.mark.parametrize("value", hq_contacts.PREF_VALUES)
def test_the_pref_round_trips_every_known_value(tmp_path, value):
    p = tmp_path / "prefs.json"
    assert prefs.set_hq_contact(value, p) == value
    assert prefs.get_hq_contact(p) == value


@pytest.mark.parametrize(
    "stored",
    ["removed-general", "asia_woman", "", None, 5, ["off"]],
    ids=["removed", "prompt-form", "empty", "null", "number", "list"],
)
def test_an_unknown_or_removed_stored_contact_reads_as_the_default(tmp_path, stored):
    p = tmp_path / "prefs.json"
    p.write_text(json.dumps({"hq_contact": stored, "theme": "light"}))
    assert prefs.get_hq_contact(p) == "random"
    assert prefs.get_theme(p) == "light"  # the rest of the file is untouched


def test_set_coerces_an_unknown_value_to_the_default(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_hq_contact("us-man", p)
    assert prefs.set_hq_contact("nobody", p) == "random"
    assert prefs.get_hq_contact(p) == "random"


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


def test_config_carries_the_pref_and_the_prefs_write_sets_it(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    cfg = c.get("/api/config").json()
    assert (cfg["hq_contact"], cfg["hq_contact_rev"]) == ("random", 0)
    r = c.post("/api/prefs", json={"hq_contact": "romania-woman"}, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json() == {"hq_contact": "romania-woman", "hq_contact_rev": 1}
    cfg = c.get("/api/config").json()
    assert (cfg["hq_contact"], cfg["hq_contact_rev"]) == ("romania-woman", 1)
    r = c.post("/api/prefs", json={"hq_contact": "off"}, headers=hdr)
    assert r.json()["hq_contact_rev"] == 2
    cfg = c.get("/api/config").json()
    assert (cfg["hq_contact"], cfg["hq_contact_rev"]) == ("off", 2)


def test_config_reads_the_contact_and_its_revision_as_one_snapshot(auth_cfg, tmp_home, monkeypatch):
    """Hermes on #1535: read separately, a write between the two reads paired the OLD contact with
    the NEW revision, which a client takes as authoritative. A write right after the first read of
    the prefs document must not leak into the same response."""
    from agent_sessions import prefs

    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    assert c.post("/api/prefs", json={"hq_contact": "us-man"}, headers=hdr).status_code == 200
    real_load, wrote = prefs._load, []

    def load_then_another_device_writes(path):
        doc = real_load(path)
        # Right after the read that serves the CONTACT (whichever getter makes it), once.
        if (
            sys._getframe(1).f_code.co_name in ("get_hq_contact", "get_hq_contact_state")
            and not wrote
        ):
            wrote.append(True)
            prefs.set_hq_contact_versioned("europe-man", path)
        return doc

    with monkeypatch.context() as m:
        m.setattr(prefs, "_load", load_then_another_device_writes)
        cfg = c.get("/api/config").json()
    assert wrote
    assert (cfg["hq_contact"], cfg["hq_contact_rev"]) in {("us-man", 1), ("europe-man", 2)}


def test_the_hq_contact_revision_orders_writes_and_survives_other_prefs(tmp_path):
    """Every write bumps it in the same locked write; other prefs leave it; garbage reads 0."""
    from agent_sessions import prefs

    p = tmp_path / "prefs.json"
    assert prefs.get_hq_contact_rev(p) == 0
    assert prefs.set_hq_contact_versioned("us-man", p) == ("us-man", 1)
    prefs.set_theme("light", p)
    assert prefs.set_hq_contact_versioned("us-man", p) == ("us-man", 2)  # same value, new write
    assert prefs.get_hq_contact_rev(p) == 2
    for junk in (True, -3, "7", None, 1.5):
        p.write_text(json.dumps({"hq_contact": "off", "hq_contact_rev": junk}))
        assert prefs.get_hq_contact_rev(p) == 0
        assert prefs.set_hq_contact_versioned("off", p)[1] == 1


@pytest.mark.parametrize(
    "value",
    ["nobody", "asia_woman", "", None, 1, ["off"]],
    ids=["unknown", "prompt-form", "empty", "null", "number", "list"],
)
def test_an_unknown_value_is_a_422_and_nothing_is_written(auth_cfg, tmp_home, value):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    prefs.set_hq_contact("us-woman")
    # Mixed with a valid key: the preflight refuses the WHOLE payload, so the theme stays too.
    r = c.post("/api/prefs", json={"theme": "light", "hq_contact": value}, headers=hdr)
    assert r.status_code == 422
    assert prefs.get_hq_contact() == "us-woman"
    assert prefs.get_theme() == "dark"


def test_the_write_needs_the_csrf_token(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/prefs", json={"hq_contact": "us-man"}, headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403
    assert prefs.get_hq_contact() == "random"
