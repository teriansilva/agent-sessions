"""Files shared with HQ as a SECRET, and moving one into the Library (#1786 P4b).

Pinned here: a Secret-shared file's contents (a canary that does NOT look like a credential, so
only the Secret flag can be what withholds it) never reach any model request — the upload turn,
a later turn's history replay — and are never served back, while the same file shared plainly
does reach the model; `store_attachment` moves it by custody transfer (commit → verify → release
→ Done) and refuses a file not shared as a secret, one over the vault limit and an index this
message does not have; and every fault in the move — before the commit, between commit and
verify, a verify mismatch, a release that cannot finish — keeps the chat copy until the Library
provably holds the same bytes, never duplicates the secret on retry, and says Done only once the
chat copy is released.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import json

import pytest

from agent_sessions import ask_attachments, ask_secrets, ask_store, template_vars, variables
from test_ask_operations import _confirm, _result, _settled, live  # noqa: F401
from test_ask_orchestrator import ai, answer, app, call, calls  # noqa: F401

CANARY = "plain words zq9 harbor lantern 1786 nothing credential shaped here"
KEY = (
    b"-----BEGIN PRIVATE KEY-----\n"
    b"MIGTAgEAMBMGByq1786canaryEHBHkwdwIBAQQg\n"
    b"-----END PRIVATE KEY-----\n"
)


def _upload(a, cid: str, name: str, data: bytes, secret: bool) -> dict:
    r = a.c.post(
        f"/api/ask/conversations/{cid}/attachments",
        files={"file": (name, data, "application/octet-stream")},
        data={"secret": "1" if secret else "0"},
        headers=a.hdr,
    )
    assert r.status_code == 201, r.text
    return r.json()["attachment"]


def _ask(a, ai, *steps, cid: str, files: list[str] | None = None, q: str = "do it"):
    script = ai(*steps, answer("Done thinking."))
    r = a.ask(cid, q, **({"attachments": files} if files else {}))
    assert r.status_code == 200, r.text
    return script


def _cards(a, cid: str) -> list[dict]:
    return [p for t in a.get(cid)["turns"] for p in t["proposals"]]


def _blob(a, cid: str, aid: str, qs: str = ""):
    return a.c.get(f"/api/ask/conversations/{cid}/attachments/{aid}{qs}")


def _ask_root_holds(needle: bytes) -> list[str]:
    return [str(p) for p in ask_store.root().rglob("*") if p.is_file() and needle in p.read_bytes()]


# ---- ingress ------------------------------------------------------------------------------------


def test_a_secret_shared_file_never_reaches_a_model_and_is_never_served(live, ai):
    cid = live.new()
    att = _upload(live, cid, "notes.txt", CANARY.encode(), secret=True)
    assert att["secret"] is True and att["kind"] == "text"
    first = _ask(live, ai, cid=cid, files=[att["id"]], q="keep this safe")
    later = _ask(live, ai, cid=cid, q="and now?")  # history replays the earlier turn
    for script in (first, later):
        sent = json.dumps(script.bodies)
        assert script.bodies and CANARY not in sent
        assert "notes.txt (secret file" in sent  # metadata only: name, kind, size
    assert _blob(live, cid, att["id"]).status_code == 403
    assert _blob(live, cid, att["id"], "?download=1").status_code == 403
    meta = _blob(live, cid, att["id"], "?meta=1").json()["attachment"]
    assert meta["secret"] is True and "notes.txt" == meta["name"]
    (turn,) = [t for t in live.get(cid)["turns"] if t.get("attachments")]
    assert turn["attachments"][0]["secret"] is True  # still Secret on replay


def test_the_same_file_shared_plainly_does_reach_the_model(live, ai):
    """The control: without the Secret flag this canary is ordinary text, so the test above
    proves the flag, not a credential guard."""
    cid = live.new()
    att = _upload(live, cid, "notes.txt", CANARY.encode(), secret=False)
    script = _ask(live, ai, cid=cid, files=[att["id"]])
    assert CANARY in json.dumps(script.bodies)
    assert _blob(live, cid, att["id"]).status_code == 200


def test_the_body_readers_and_the_excerpt_skip_a_secret_file(live):
    """Each layer on its own: the readers never load a Secret file's bytes for a model, and the
    excerpt builder never quotes them even if handed a body."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    cid = live.new()
    text = _upload(live, cid, "notes.txt", CANARY.encode(), secret=True)
    image = _upload(live, cid, "id.png", png, secret=True)
    plain = _upload(live, cid, "plain.txt", CANARY.encode(), secret=False)
    assert ask_attachments.read_text_bodies(cid, [text, plain]) == {plain["id"]: CANARY.encode()}
    assert ask_attachments.read_image_bodies(cid, [image]) == {}
    block = ask_attachments.prompt_block(text, CANARY.encode())
    assert CANARY not in block and "never shown to you" in block
    shown = ask_attachments.prompt_block(dict(image), png, image_shown=True)
    assert "attached below" not in shown


# ---- store_attachment ---------------------------------------------------------------------------


def _shared_key(live, ai, *, secret: bool = True, name: str = "asc_api_key", data: bytes = KEY):
    cid = live.new()
    att = _upload(live, cid, "AuthKey_7F3K2L9QXA.p8", data, secret=secret)
    script = _ask(
        live,
        ai,
        call("propose_secret", {"action": "store_attachment", "attachment_index": 0, "name": name}),
        cid=cid,
        files=[att["id"]],
        q="store this key",
    )
    return cid, att, script


def _record(name: str) -> dict | None:
    return next((v for v in variables.library()["variables"] if v["name"] == name), None)


def _vault_bytes(name: str) -> bytes | None:
    rec = next(r for r in template_vars._read_strictly_full() if r["name"] == name)
    return template_vars.file_bytes(rec, checked=True)


def test_a_secret_shared_key_moves_into_the_library_and_the_chat_copy_goes(live, ai):
    cid, att, script = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    assert card["op"] == "secret.store_attachment" and card["needs"] == []
    assert "AuthKey_7F3K2L9QXA.p8" in card["text"] and "never sees its contents" in card["text"]
    assert KEY not in json.dumps(script.bodies).encode()
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "done", settled
    assert "chat copy is deleted" in settled["summary"]
    rec = _record("asc_api_key")
    assert rec["kind"] == "secret_file" and rec["agent_access"] is False
    assert rec["file"]["name"] == "AuthKey_7F3K2L9QXA.p8"
    assert _vault_bytes("asc_api_key") == KEY
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 404  # released
    assert _ask_root_holds(b"1786canary") == []


def test_a_file_not_shared_as_a_secret_is_refused_and_nothing_is_staged(live, ai):
    cid, att, script = _shared_key(live, ai, secret=False)
    assert _cards(live, cid) == []
    assert "not shared as a secret" in json.dumps(_result(script, 1))
    assert _record("asc_api_key") is None


def test_an_index_this_message_does_not_have_is_refused(live, ai):
    cid = live.new()
    script = _ask(
        live,
        ai,
        call("propose_secret", {"action": "store_attachment", "attachment_index": 0, "name": "k"}),
        cid=cid,
    )
    assert _cards(live, cid) == []
    assert "attachment_index is the place of a file" in json.dumps(_result(script, 1))


def test_an_oversize_secret_file_is_refused_and_stays_secret_in_the_chat(live, ai):
    big = b"k" * (256 * 1024 + 1)
    cid, att, script = _shared_key(live, ai, data=big)
    assert _cards(live, cid) == []
    assert "at most 256 KB" in json.dumps(_result(script, 1))
    assert _record("asc_api_key") is None
    meta = _blob(live, cid, att["id"], "?meta=1").json()["attachment"]
    assert meta["secret"] is True  # the chat copy stays, still Secret
    assert _blob(live, cid, att["id"]).status_code == 403


def test_an_oversize_file_at_the_tap_writes_nothing(live, ai, monkeypatch):
    """Staged under the limit, over it by the tap (the vault's limit lowered): refused before
    anything is written — no record, no blob — and the chat copy stays Secret."""
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    from agent_sessions import vault_files

    monkeypatch.setattr(vault_files, "MAX_BYTES", len(KEY) - 1)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "failed" and "stays in the chat" in settled["summary"]
    assert _record("asc_api_key") is None
    assert not vault_files.root().exists() or list(vault_files.root().glob("*.bin")) == []
    assert _blob(live, cid, att["id"], "?meta=1").json()["attachment"]["secret"] is True


# ---- faults in the move -------------------------------------------------------------------------


def test_a_failure_before_the_commit_keeps_the_chat_copy_and_stores_nothing(live, ai, monkeypatch):
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)

    def refuse(payload):
        raise template_vars.VariableError("the library is busy")

    monkeypatch.setattr(variables, "create", refuse)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "failed" and "stays in the chat" in settled["summary"]
    assert _record("asc_api_key") is None
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200


def test_a_verify_mismatch_keeps_the_chat_copy(live, ai, monkeypatch):
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    monkeypatch.setattr(ask_secrets, "_verify_and_release", lambda c, a, committed, d: "mismatch")
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "failed" and "chat copy was kept" in settled["summary"]
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200


@pytest.mark.parametrize("fault", ["verify_raises", "release_marker", "release_raises"])
def test_an_unfinished_move_is_unsettled_and_finishing_it_never_duplicates(
    live, ai, monkeypatch, fault
):
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    real_verify, real_release = ask_secrets._verify_and_release, ask_attachments.release_secret_held

    if fault == "verify_raises":

        def broken(c, a, committed, d):
            raise OSError("verify interrupted")

        monkeypatch.setattr(ask_secrets, "_verify_and_release", broken)
    elif fault == "release_marker":
        monkeypatch.setattr(ask_attachments, "release_secret_held", lambda c, a: "retry")
    else:

        def boom(c, a):
            raise OSError("disk")

        monkeypatch.setattr(ask_attachments, "release_secret_held", boom)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "unsettled", settled
    assert settled["can_confirm"] is True and settled["can_dismiss"] is False
    assert "Confirm again to finish" in settled["summary"]
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200  # not Done, copy kept
    assert _vault_bytes("asc_api_key") == KEY  # committed

    monkeypatch.setattr(ask_secrets, "_verify_and_release", real_verify)
    monkeypatch.setattr(ask_attachments, "release_secret_held", real_release)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "done", settled
    assert [v["name"] for v in variables.library()["variables"]].count("asc_api_key") == 1
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 404


# ---- custody under concurrency (Hermes on #1816) ------------------------------------------------


def test_a_library_delete_cannot_land_between_the_verify_and_the_release(live, ai, monkeypatch):
    """A delete of the destination started while the move verifies and releases waits for the
    store's write fences: it lands after the chat copy is released, never between."""
    import threading
    import time

    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    real_release = ask_attachments.release_secret_held
    seen: dict = {}

    def racing_release(c, a):
        rec = _record("asc_api_key")
        t = threading.Thread(
            target=lambda: seen.setdefault(
                "deleted", variables.delete(rec["id"], rec["updated_at"]) or True
            )
        )
        t.start()
        time.sleep(0.3)
        seen["blocked"] = t.is_alive()  # the delete waits on the fences the verify holds
        result = real_release(c, a)
        seen["thread"] = t
        return result

    monkeypatch.setattr(ask_attachments, "release_secret_held", racing_release)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    seen["thread"].join(10)
    assert seen["blocked"] is True
    assert settled["state"] == "done", settled


def test_a_destination_removed_before_the_verify_keeps_the_chat_copy(live, ai, monkeypatch):
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    real_rev = ask_secrets._committed_rev

    def then_delete(vid):
        rev = real_rev(vid)
        rec = _record("asc_api_key")
        variables.delete(rec["id"], rec["updated_at"])  # someone deletes it right after commit
        return rev

    monkeypatch.setattr(ask_secrets, "_committed_rev", then_delete)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "failed" and "chat copy was kept" in settled["summary"], settled
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200


def test_an_identical_record_made_elsewhere_is_never_adopted(live, ai):
    """Same name, same bytes, agent access ON: not this move's record, so nothing is moved and
    the access it carries is never reported as this card's doing."""
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    other = variables.create({"name": "asc_api_key", "kind": "secret_file"})
    other = variables.put_file(other["id"], KEY, "AuthKey_7F3K2L9QXA.p8", other["updated_at"])
    variables.update(other["id"], {"agent_access": True}, other["updated_at"])
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "failed" and "already used" in settled["summary"], settled
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200


def test_a_resumed_move_whose_destination_was_replaced_never_says_done(live, ai, monkeypatch):
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    real_release = ask_attachments.release_secret_held
    monkeypatch.setattr(ask_attachments, "release_secret_held", lambda c, a: "retry")
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "unsettled"
    # Meanwhile: the chat copy goes and the Library record is replaced with unrelated bytes.
    monkeypatch.setattr(ask_attachments, "release_secret_held", real_release)
    ask_attachments.release_secret(cid, att["id"])
    rec = _record("asc_api_key")
    variables.put_file(rec["id"], b"unrelated replacement bytes", "Other.p8", rec["updated_at"])
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "failed", settled
    assert "changed, removed or cannot be read" in settled["summary"]


def test_a_different_secret_of_the_same_name_is_never_overwritten(live, ai):
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    other = variables.create({"name": "asc_api_key", "kind": "secret_file"})
    variables.put_file(other["id"], b"someone else's key bytes", "Other.p8", other["updated_at"])
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "failed" and "already used in the Library" in settled["summary"]
    assert _vault_bytes("asc_api_key") == b"someone else's key bytes"
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200


# ---- the create card's file mode ----------------------------------------------------------------


def _file_card(live, ai) -> tuple[str, dict]:
    cid = live.new()
    _ask(
        live,
        ai,
        call("propose_secret", {"action": "create", "name": "asc_api_key", "kind": "file"}),
        cid=cid,
    )
    (card,) = _cards(live, cid)
    return cid, card


def _decide_file(live, pid: str, name: str, data: bytes, qs: str = "?decision=confirm"):
    return live.c.post(
        f"/api/ask/proposals/{pid}/decide{qs}",
        files={"file": (name, data, "application/octet-stream")},
        headers=live.hdr,
    )


def test_a_file_chosen_on_the_card_is_stored_and_held_nowhere_else(live, ai):
    cid, card = _file_card(live, ai)
    assert card["op"] == "secret.create_file" and card["needs"] == ["file"]
    r = _decide_file(live, card["action_id"], "AuthKey_7F3K2L9QXA.p8", KEY)
    assert r.status_code == 202, r.text
    settled = _settled(live, cid)
    assert settled["state"] == "done", settled
    rec = _record("asc_api_key")
    assert rec["kind"] == "secret_file" and rec["agent_access"] is False
    assert _vault_bytes("asc_api_key") == KEY
    assert b"1786canary" not in r.content and _ask_root_holds(b"1786canary") == []


def test_a_file_decide_needs_a_confirm_and_the_vault_limit(live, ai):
    cid, card = _file_card(live, ai)
    assert _decide_file(live, card["action_id"], "k.p8", KEY, qs="").status_code == 422
    big = _decide_file(live, card["action_id"], "k.p8", b"k" * (256 * 1024 + 1))
    assert big.status_code == 413
    assert _cards(live, cid)[0]["state"] == "pending"
    assert _record("asc_api_key") is None


def test_an_upload_the_library_refuses_takes_the_empty_record_back(live, ai):
    """A file name holding a stored secret is refused by the upload route; the record the card
    just created goes with it, so nothing is left behind."""
    variables.create({"name": "other_token", "kind": "secret", "value": "stored-token-1786-zz"})
    cid, card = _file_card(live, ai)
    r = _decide_file(live, card["action_id"], "stored-token-1786-zz.p8", KEY)
    assert r.status_code == 202
    settled = _settled(live, cid)
    assert settled["state"] == "failed" and "Nothing changed" in settled["summary"], settled
    assert _record("asc_api_key") is None


def test_an_empty_record_of_that_name_is_never_filled_by_the_move(live, ai):
    """Someone made "asc_api_key" in the Library (empty, agent access ON) after the card was
    staged: the move must not upload into it — that would hand agents the file with no access
    card (independent review of P4b)."""
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    other = variables.create({"name": "asc_api_key", "kind": "secret_file"})
    variables.update(other["id"], {"agent_access": True}, other["updated_at"])
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert (
        settled["state"] == "failed" and "already used in the Library" in settled["summary"]
    ), settled
    rec = _record("asc_api_key")
    assert rec["set"] is False and rec["agent_access"] is True  # untouched
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200


def test_a_refused_upload_takes_back_the_record_it_created(live, ai, monkeypatch):
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)

    def refuse(*args, **kwargs):
        raise template_vars.VariableError("the file name looks like a credential")

    monkeypatch.setattr(variables, "put_file", refuse)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "failed" and "stays in the chat" in settled["summary"], settled
    assert "empty" not in settled["summary"]
    assert _record("asc_api_key") is None
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200


def test_a_release_whose_marker_landed_finishes_as_done_when_resumed(live, ai, monkeypatch):
    """The tombstone was written, then the unlink step failed: the card is unsettled; Finish
    finds the chat copy already released and the Library's copy present — Done, not stale."""
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)

    def broken(c):
        raise OSError("unlink failed")

    monkeypatch.setattr(ask_attachments, "complete_tombstones", broken)
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "unsettled"
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "done", settled
    assert _vault_bytes("asc_api_key") == KEY
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 404


def test_a_move_and_a_message_in_the_same_conversation_never_deadlock(live, ai):
    """Lock order (independent review of #1816): a message holds the conversation's attachments
    fence and then takes the registration fence (`begin` → append); the move's verify-and-release
    must take them in the SAME order, or the two wait on each other forever."""
    import threading
    import time

    from agent_sessions import template_secrets

    cid = live.new()
    att = _upload(live, cid, "AuthKey.p8", KEY, secret=True)
    live.ask(cid, "keep it", attachments=[att["id"]])
    rec = variables.create({"name": "asc_api_key", "kind": "secret_file"})
    rec = variables.put_file(rec["id"], KEY, "AuthKey.p8", rec["updated_at"])
    committed = {"id": rec["id"], "rev": ask_secrets._committed_rev(rec["id"])}
    done: dict = {}

    def message_side():
        with ask_attachments.attachments_fence(cid):
            time.sleep(0.3)  # the move starts meanwhile
            with template_secrets.registration_fence():
                done["message"] = True

    def move_side():
        time.sleep(0.1)
        done["move"] = ask_secrets._verify_and_release(cid, att["id"], committed, KEY)

    threads = [threading.Thread(target=f, daemon=True) for f in (message_side, move_side)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not any(t.is_alive() for t in threads), "the move and the message deadlocked"
    assert done == {"message": True, "move": "released"}


def _stop_and_recover(live, cid, card, monkeypatch, patch):
    """Confirm with ``patch`` in place (it stops the move), then do what startup does."""
    import time

    from agent_sessions import ask_operations, ask_proposals

    patch()
    _confirm(live, card["action_id"])
    for _ in range(200):
        if not ask_operations.running(card["action_id"]):
            break
        time.sleep(0.05)
    ask_proposals.recover(cid)
    return _cards(live, cid)[0]


@pytest.mark.parametrize("where", ["after_the_note_before_create", "after_create_before_upload"])
def test_a_stop_on_either_side_of_creating_the_record_is_finished(live, ai, monkeypatch, where):
    """Hermes on #1816: the record's id is noted BEFORE the record exists, so a stop at either
    side of the create is finished from the note — the card comes back `unsettled`, Finish
    creates and/or uploads, verifies and releases: one record, the chat copy gone."""
    import asyncio

    from agent_sessions import asgi_call

    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    real_note, real_call = ask_secrets._note, asgi_call.call

    async def stop_after_note(pid, c, committed):
        await real_note(pid, c, committed)
        raise asyncio.CancelledError  # the process stops here

    async def stop_after_create(app, operator, method, path, body, **kw):
        r = await real_call(app, operator, method, path, body, **kw)
        if method == "POST" and path == "/api/variables":
            raise asyncio.CancelledError
        return r

    def patch():
        if where == "after_the_note_before_create":
            monkeypatch.setattr(ask_secrets, "_note", stop_after_note)
        else:
            monkeypatch.setattr(asgi_call, "call", stop_after_create)

    resumed = _stop_and_recover(live, cid, card, monkeypatch, patch)
    monkeypatch.setattr(ask_secrets, "_note", real_note)
    monkeypatch.setattr(asgi_call, "call", real_call)
    if where == "after_the_note_before_create":
        assert _record("asc_api_key") is None  # nothing was created yet
    else:
        assert _record("asc_api_key")["set"] is False  # created, never filled
    assert resumed["state"] == "unsettled" and resumed["can_confirm"] is True, resumed
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "done", settled
    assert [v["name"] for v in variables.library()["variables"]].count("asc_api_key") == 1
    assert _vault_bytes("asc_api_key") == KEY
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 404


def test_finish_with_the_chat_copy_gone_needs_a_readable_library_copy(live, ai, monkeypatch):
    """The release's marker landed and the card is unsettled; the Library's sealed copy is then
    damaged. Finish must not say Done on metadata alone (Hermes on #1816)."""
    from agent_sessions import vault_files

    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)

    def broken(c):
        raise OSError("unlink failed")

    monkeypatch.setattr(ask_attachments, "complete_tombstones", broken)
    _confirm(live, card["action_id"])
    assert _settled(live, cid)["state"] == "unsettled"
    committed = next(
        r["committed"] for r in ask_store.read_proposals(cid) if r["id"] == card["action_id"]
    )
    assert ask_secrets._intact(committed) is True
    for blob in vault_files.root().glob("*.bin"):
        blob.write_bytes(b"\x00" * blob.stat().st_size)  # damaged at rest
    assert ask_secrets._intact(committed) is False  # metadata alone proves nothing
    _confirm(live, card["action_id"])  # a damaged store fails closed, before or in the move
    settled = _settled(live, cid)
    assert settled["state"] != "done", settled


def test_an_error_after_the_note_leaves_the_move_finishable(live, ai, monkeypatch):
    """The Library cannot be read back after the record was created and noted: the card is
    `unsettled` (Finish resumes), never a dead `failed` with a record behind it."""
    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    real_rev = ask_secrets._committed_rev

    def flaky(vid):
        raise OSError("store briefly unreadable")

    monkeypatch.setattr(ask_secrets, "_committed_rev", flaky)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "unsettled" and settled["can_confirm"] is True, settled
    monkeypatch.setattr(ask_secrets, "_committed_rev", real_rev)
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "done", settled
    assert [v["name"] for v in variables.library()["variables"]].count("asc_api_key") == 1
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 404


def test_a_new_variable_may_bring_its_own_fresh_well_formed_id(tmp_home):
    chosen = "ab" * 16
    row = variables.create({"name": "k1", "kind": "secret_file", "id": chosen})
    assert row["id"] == chosen
    with pytest.raises(template_vars.VariableError, match="already used"):
        variables.create({"name": "k2", "kind": "secret_file", "id": chosen})
    with pytest.raises(template_vars.VariableError, match="32 lowercase hex"):
        variables.create({"name": "k3", "kind": "secret_file", "id": "NOT-AN-ID"})
    assert len(variables.create({"name": "k4", "kind": "secret_file"})["id"]) == 32
    # A deleted record's id is never reused, even for the same name and kind.
    gone = variables.create({"name": "k5", "kind": "secret_file"})
    variables.delete(gone["id"], gone["updated_at"])
    with pytest.raises(template_vars.VariableError, match="already used"):
        variables.create({"name": "k5", "kind": "secret_file", "id": gone["id"]})


def test_finish_never_fills_an_empty_record_whose_access_was_switched_on(live, ai, monkeypatch):
    """Hermes on #1816: stopped after the create, someone turns agent access on for the still-
    empty record; Finish must not fill it (that would grant agents the file on this card's
    strength). It stays unsettled, the chat copy kept; with access off again Finish completes."""
    import asyncio

    from agent_sessions import asgi_call

    cid, att, _ = _shared_key(live, ai)
    (card,) = _cards(live, cid)
    real_call = asgi_call.call

    async def stop_after_create(app, operator, method, path, body, **kw):
        r = await real_call(app, operator, method, path, body, **kw)
        if method == "POST" and path == "/api/variables":
            raise asyncio.CancelledError
        return r

    _stop_and_recover(
        live,
        cid,
        card,
        monkeypatch,
        lambda: monkeypatch.setattr(asgi_call, "call", stop_after_create),
    )
    monkeypatch.setattr(asgi_call, "call", real_call)
    rec = _record("asc_api_key")
    rec = variables.update(rec["id"], {"agent_access": True}, rec["updated_at"])
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "unsettled", settled
    assert "Agent access was switched on" in settled["summary"]
    assert _record("asc_api_key")["set"] is False  # never filled
    assert _blob(live, cid, att["id"], "?meta=1").status_code == 200  # chat copy kept

    variables.update(rec["id"], {"agent_access": False}, rec["updated_at"])
    _confirm(live, card["action_id"])
    settled = _settled(live, cid)
    assert settled["state"] == "done" and "Agent access is off" in settled["summary"], settled
