"""Artifact ids travel where the credential guard reads them (#1932).

An artifact's link reaches HQ's stored answers and a mission's shelf, and both withhold anything
credential-shaped (`ask_store.guard_text`). A random alphanumeric id is read as a token, so ids are
digit groups; this pins that a link built from a fresh id passes the guard every time, and that a
read never creates the store (an HQ draft's read leaves every store byte-identical).
"""

from __future__ import annotations

from agent_sessions import artifact_store, ask_store


def test_a_fresh_id_has_its_shape_and_a_link_to_it_is_never_withheld():
    for _ in range(500):
        aid = artifact_store.new_id()
        assert artifact_store.ID_RE.fullmatch(aid), aid
        text = f"here: https://box.example/artifacts/{aid}/ (pinned: /artifacts/{aid}@3/)"
        assert ask_store.guard_text(text, len(text)) == text


def test_reads_never_create_the_store():
    assert not artifact_store.db_path().exists()
    assert artifact_store.list_artifacts()["total"] == 0
    assert artifact_store.get(artifact_store.new_id()) is None
    assert artifact_store.get_activation("session", "claude:x") is None
    assert artifact_store.usage()["artifacts"] == 0
    assert not artifact_store.db_path().exists()
