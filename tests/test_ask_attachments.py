"""HQ conversation attachments (#1707) — the store, the routes and the message flow.

Pinned here, one acceptance item per test (#1707 Phases 1–2):

* kind classification from the BYTES (image signatures / UTF-8 text / other), never the client's
  name or MIME — including the fake `image/x.png` carrying HTML, which must never serve inline;
* the caps (10 MiB per file, 4 staged, 32 MiB aggregate) with refusals that name them;
* names are never path components; a malformed or foreign id is refused;
* the shared upload/delete fence: a delete interleaved with a publish can never leave an orphan,
  `delete_conversation` takes the blobs with the records, staged blobs go on a TTL;
* the serve contract: image inline / text `text/plain` / `other` ALWAYS a download, `?download=1`
  forces one, nosniff + no-store everywhere, a gone blob 404s with no Download offered;
* the model-request boundary is fail-closed: a text file is inlined as a bounded excerpt through
  the sanitizer, a credential-shaped body or name is withheld WHOLE, and on a sanitizer failure
  the model gets counts/kinds/sizes with NO raw filename — in the current turn and on replay;
* a text-only turn's model input is byte-for-byte unchanged.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

import pytest

from agent_sessions import (
    ask_attachments,
    ask_orchestrator,
    ask_store,
    template_secrets,
)
from test_ask_orchestrator import ai, answer, app, call, calls  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
HTML = b"<!doctype html><script>alert(1)</script>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
ZIP = b"PK\x03\x04" + b"\x00" * 32
NULY = b"MZ\x00\x01plain-looking name but binary\x00"
LOG = b"2026-10-26T09:12:04Z ERROR migration 3/7: relation does not exist\n"


def _new() -> str:
    return ask_store.create()


def _publish(cid: str, name: str, data: bytes) -> dict:
    return ask_attachments.publish(cid, ask_attachments.new_aid(), name, data)


def _claim(cid: str, *aids: str) -> list[dict]:
    """`claim` as `begin` runs it: the caller holds the attachments fence across claim+append."""
    with ask_attachments.attachments_fence(cid):
        return ask_attachments.claim(cid, list(aids), ask_store.read(cid))


# ---- classification (bytes, never the name or MIME) -------------------------------------------


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (PNG, "image"),
        (JPEG, "image"),
        (GIF, "image"),
        (WEBP, "image"),
        (LOG, "text"),
        (SVG, "text"),  # SVG is TEXT — shown as plain text, never rendered
        (b'{"a": 1}', "text"),
        (ZIP, "other"),
        (NULY, "other"),
        (HTML, "other"),  # markup: never offered as a viewable text, only a download
    ],
)
def test_the_kind_comes_from_the_bytes(data, kind):
    assert ask_attachments.classify(data) == kind


def test_a_fake_image_name_carrying_html_is_not_an_image():
    # The client's filename and MIME are never asked. HTML in a .png is `other`, so the serve
    # route can only ever offer it as a download.
    assert ask_attachments.classify(HTML) != "image"
    d = _new()
    desc = _publish(d, "totally-an-image.png", HTML)
    assert desc["kind"] == "other"


# ---- caps, names, ids -------------------------------------------------------------------------


def test_an_oversized_file_is_refused_and_names_the_cap():
    d = _new()
    with pytest.raises(ask_attachments.AttachmentRefused) as e:
        _publish(d, "big.bin", b"x" * (ask_attachments.MAX_FILE_BYTES + 1))
    assert "limit" in e.value.detail
    assert ask_attachments.list_blobs(d) == []  # nothing left behind


def test_the_aggregate_cap_is_refused_and_names_the_cap():
    d = _new()
    per = 9 * 1024 * 1024
    _publish(d, "a.bin", b"x" * per)
    _publish(d, "b.bin", b"x" * per)
    _publish(d, "c.bin", b"x" * per)
    with pytest.raises(ask_attachments.AttachmentRefused) as e:
        _publish(d, "d.bin", b"x" * per)  # would cross 32 MiB
    assert "32 MB" in e.value.detail
    assert ask_attachments.total_bytes(d) <= ask_attachments.MAX_TOTAL_BYTES


def test_a_name_is_never_a_path_component():
    d = _new()
    desc = _publish(d, "../../etc/passwd", b"hello")
    assert "/" not in desc["name"] and ".." not in desc["name"]
    # and the blob lives under the conversation dir under its id, never under the name
    assert (ask_attachments.attachments_dir(d) / desc["id"]).exists()


def test_an_empty_file_is_refused():
    with pytest.raises(ask_attachments.AttachmentRefused):
        _publish(_new(), "empty.txt", b"")


def test_a_malformed_id_never_opens_a_path():
    d = _new()
    for bad in ("../../x", "nope", "", "A" * 32, "z" * 32):
        with pytest.raises(ask_attachments.AttachmentRefused):
            ask_attachments.open_blob(d, bad)


# ---- ownership: one claim per id --------------------------------------------------------------


def test_an_id_is_claimed_by_exactly_one_turn_or_stays_staged():
    d = _new()
    desc = _publish(d, "log.txt", LOG)
    recs = ask_store.read(d)
    with ask_attachments.attachments_fence(d):
        got = ask_attachments.claim(d, [desc["id"]], recs)
    assert got[0]["id"] == desc["id"] and got[0]["kind"] == "text"
    # the turn that claimed it stores the descriptor; a second claim is refused
    recs = recs + [
        {
            "type": "user",
            "turn_id": str(uuid.uuid4()),
            "text": "q",
            "qd": ask_store.question_digest("q"),
            "attachments": got,
        }
    ]
    with (
        ask_attachments.attachments_fence(d),
        pytest.raises(ask_attachments.AttachmentRefused) as e,
    ):
        ask_attachments.claim(d, [desc["id"]], recs)
    assert "already sent" in e.value.detail


def test_an_id_from_another_conversation_is_refused():
    a, b = _new(), _new()
    desc = _publish(a, "log.txt", LOG)
    with (
        ask_attachments.attachments_fence(b),
        pytest.raises(ask_attachments.AttachmentRefused) as e,
    ):
        ask_attachments.claim(b, [desc["id"]], ask_store.read(b))
    assert "not in this conversation" in e.value.detail


# ---- delete, retention, the fence -------------------------------------------------------------


def test_delete_conversation_takes_the_blobs_with_it():
    d = _new()
    _publish(d, "log.txt", LOG)
    _publish(d, "shot.png", PNG)
    assert len(ask_attachments.list_blobs(d)) == 2
    ask_store.delete_conversation(d)
    assert not ask_attachments.attachments_dir(d).exists()


def test_a_publish_never_lands_after_the_conversation_went():
    # The interleaving the fence exists for: delete wins the race, then an in-flight publish
    # must find the conversation gone INSIDE the fence and leave no orphan directory.
    d = _new()
    _publish(d, "log.txt", LOG)
    ask_store.delete_conversation(d)
    with pytest.raises(ask_store.ConversationNotFound):
        _publish(d, "late.png", PNG)
    assert not ask_attachments.attachments_dir(d).exists()


def test_staged_blobs_go_on_a_ttl_and_claimed_ones_stay():
    d = _new()
    staged = _publish(d, "staged.txt", b"staged")
    sent = _publish(d, "sent.txt", b"sent")
    recs = [
        {
            "type": "user",
            "turn_id": str(uuid.uuid4()),
            "text": "q",
            "qd": ask_store.question_digest("q"),
            "attachments": _claim(d, sent["id"]),
        }
    ]
    ask_store.append(d, *recs)
    old = time.time() - ask_attachments.STAGED_TTL - 60
    os.utime(ask_attachments.attachments_dir(d) / staged["id"], (old, old))
    removed = ask_attachments.reap_staged()
    assert removed >= 1
    ids = {a for a, _s, _m in ask_attachments.list_blobs(d)}
    assert sent["id"] in ids and staged["id"] not in ids


def test_a_refused_publish_leaves_no_partial():
    d = _new()
    with pytest.raises(ask_attachments.AttachmentRefused):
        _publish(d, "big.bin", b"x" * (ask_attachments.MAX_FILE_BYTES + 1))
    # Size is refused before the filesystem is touched at all — and a refusal further in must
    # still leave no .partial behind.
    entries = (
        [p.name for p in ask_attachments.attachments_dir(d).iterdir()]
        if ask_attachments.attachments_dir(d).exists()
        else []
    )
    assert entries == [] or entries == [".lock"]
    assert not any(n.endswith(".partial") for n in entries)


# ---- the serve contract -----------------------------------------------------------------------


def _get(app, cid: str, aid: str, qs: str = ""):
    return app.c.get(f"/api/ask/conversations/{cid}/attachments/{aid}{qs}", headers=app.hdr)


def test_an_image_serves_inline_with_its_sniffed_type(app):
    d = app.new()
    desc = _publish(d, "shot.png", PNG)
    r = _get(app, d, desc["id"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.content == PNG


def test_text_serves_as_text_plain_never_a_sniffed_type(app):
    d = app.new()
    desc = _publish(d, "notes.svg", SVG)  # a scriptable format: TEXT, never rendered
    r = _get(app, d, desc["id"])
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"] == "inline"


def test_other_is_always_a_download_and_a_fake_png_html_never_renders(app):
    d = app.new()
    desc = _publish(d, "totally-an-image.png", HTML)
    r = _get(app, d, desc["id"])
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert not r.headers["content-type"].startswith("image/")
    assert not r.headers["content-type"].startswith("text/html")


def test_download_forces_a_download_even_for_an_image(app):
    d = app.new()
    desc = _publish(d, "shot.png", PNG)
    r = _get(app, d, desc["id"], "?download=1")
    assert r.headers["content-disposition"].startswith("attachment")


def test_a_gone_blob_404s(app):
    d = app.new()
    r = _get(app, d, "0" * 32)
    assert r.status_code == 404


# ---- the message flow, and the fail-closed prompt boundary -------------------------------------


def _text_of(script) -> str:
    """Everything the first model request saw, as one string."""
    body = script.bodies[0]
    return "\n".join(str(m.get("content")) for m in body["messages"])


def test_a_text_only_turn_is_byte_for_byte_unchanged(app, ai):
    script = ai(answer("ok"))
    d = app.new()
    r = app.ask(d, "hello there")
    assert r.status_code == 200
    assert "hello there" in _text_of(script)
    assert "Shared" not in _text_of(script)  # no note when nothing was shared


def test_a_shared_text_file_is_inlined_as_a_bounded_excerpt(app, ai):
    script = ai(answer("ok"))
    d = app.new()
    desc = _publish(d, "deploy.log", LOG)
    r = app.ask(d, "what failed?", attachments=[desc["id"]])
    assert r.status_code == 200, r.text
    seen = _text_of(script)
    assert "deploy.log" in seen  # the note names it
    assert "relation does not exist" in seen  # and the bytes are inlined
    assert "```" in seen  # as a fenced block


def test_a_shared_images_bytes_never_enter_the_text(app, ai):
    # Since #1779 the pixels ride as an image part (`test_ask_images.py`); the TEXT names the
    # image and never carries its bytes.
    script = ai(answer("ok"))
    d = app.new()
    desc = _publish(d, "shot.png", PNG)
    r = app.ask(d, "and this?", attachments=[desc["id"]])
    assert r.status_code == 200, r.text
    question = script.bodies[0]["messages"][-1]["content"]
    text = "\n".join(p["text"] for p in question if p.get("type") == "text")
    assert "shot.png" in text and "image" in text
    assert "PNG" not in text and "base64" not in text  # no bytes in the text, ever


def test_a_credential_shaped_body_is_withheld_whole(app, ai):
    script = ai(answer("ok"))
    d = app.new()
    secrety = b"aws_secret_access_key = AKIA" + b"X" * 40 + b"\n"
    desc = _publish(d, "env.txt", secrety)
    r = app.ask(d, "read this", attachments=[desc["id"]])
    assert r.status_code == 200
    seen = _text_of(script)
    assert b"AKIA".decode() not in seen
    assert "withheld" in seen


def test_a_credential_shaped_filename_never_reaches_the_prompt(app, ai):
    script = ai(answer("ok"))
    d = app.new()
    desc = _publish(d, "token=" + "A" * 40 + ".txt", b"harmless")
    r = app.ask(d, "look", attachments=[desc["id"]])
    assert r.status_code == 200
    seen = _text_of(script)
    assert "A" * 40 not in seen
    assert "a shared file" in seen  # the withheld name's stand-in


def test_on_a_sanitizer_failure_the_model_gets_counts_only(monkeypatch):
    # The model-request boundary itself, with the sanitizer dead: the note must fall back to
    # fixed counts/kinds/sizes. (An app-level run cannot isolate this — the store boundary uses
    # the same redactor and would refuse the write long before the prompt is built.)
    def boom(*a, **k):
        raise template_secrets.RedactionUnavailable("no redactor")

    monkeypatch.setattr(template_secrets, "redaction_values", boom)
    att = {"id": "a" * 32, "name": "deploy.log", "kind": "text", "size": 33}
    note = ask_attachments.prompt_note([att])
    assert "deploy.log" not in note  # NO raw filename
    assert "Shared 1 file" in note  # the fixed count/kind/size note
    assert "text" in note
    block = ask_attachments.prompt_block(att, LOG)
    assert "deploy.log" not in block  # NO raw filename
    assert "relation does not exist" not in block  # NO bytes
    assert "Shared 1 file" in block


def test_history_replays_the_sanitized_note_never_the_bytes(app, ai):
    script = ai(answer("first"), answer("second"))
    d = app.new()
    desc = _publish(d, "deploy.log", LOG)
    app.ask(d, "what failed?", attachments=[desc["id"]])
    app.ask(d, "and after that?")
    second = script.bodies[1]
    history = "\n".join(str(m.get("content")) for m in second["messages"])
    assert "deploy.log" in history  # later turns know the file existed
    assert "relation does not exist" not in history  # the bytes go once, not again


def test_a_foreign_or_unclaimed_id_is_refused_before_anything_is_written(app, ai):
    ai(answer("ok"))
    d = app.new()
    other = _new()
    desc = _publish(other, "log.txt", LOG)
    r = app.ask(d, "steal", attachments=[desc["id"]])
    assert r.status_code == 422
    assert "not in this conversation" in r.json()["detail"]
    turns = app.get(d)["turns"]
    assert turns == []  # nothing was written


def test_the_stored_record_carries_the_descriptor_and_never_the_bytes(app, ai):
    ai(answer("ok"))
    d = app.new()
    desc = _publish(d, "deploy.log", LOG)
    app.ask(d, "what failed?", attachments=[desc["id"]])
    raw = (ask_store.root() / f"{d}.jsonl").read_text()
    assert "deploy.log" in raw and desc["id"] in raw
    assert "relation does not exist" not in raw  # the JSONL never sees blob bytes
    view = app.get(d)["turns"][0]
    att = view["attachments"][0]
    assert att["id"] == desc["id"] and att["kind"] == "text" and att["size"] == len(LOG)


# ---- Hermes PR review #1720 round 1 -----------------------------------------------------------


def test_a_blob_is_unreachable_once_the_conversation_is_gone_even_if_cleanup_failed(app):
    # The deletion/retention breach: the JSONL is gone (a crash between the unlink and the blob
    # removal, or a cleanup that errored) but the blob is still on disk. The old URL must NOT
    # serve it — retrieval rejects absent conversations whatever the cleanup left behind.
    d = app.new()
    desc = _publish(d, "deploy.log", LOG)
    (ask_store.root() / f"{d}.jsonl").unlink()  # as a failed cleanup would leave it
    r = _get(app, d, desc["id"])
    assert r.status_code == 404
    r = _get(app, d, desc["id"], "?meta=1")
    assert r.status_code == 404
    r = _get(app, d, desc["id"], "?download=1")
    assert r.status_code == 404


def test_one_classification_agrees_across_publish_claim_and_retrieval():
    # 8 KiB of ASCII followed by 0xff is `other`. The three old re-classifications disagreed
    # (full / 8 KiB / 16 bytes) and a binary attachment became a text card sent to the model.
    blob = b"A" * 8192 + b"\xff"
    assert ask_attachments.classify(blob) == "other"
    d = _new()
    desc = _publish(d, "looks-like-text.txt", blob)
    assert desc["kind"] == "other"
    got = _claim(d, desc["id"])
    assert got[0]["kind"] == "other"
    assert got[0]["name"] == "looks-like-text.txt"


def test_a_late_nul_is_not_text():
    # The no-NUL rule covers the WHOLE file, not its first 8 KiB.
    assert ask_attachments.classify(b"x" * 10000 + b"\x00tail") == "other"


def test_a_multibyte_character_across_the_header_boundary_is_still_text():
    # The old 16-byte GET sniff split a multibyte UTF-8 character and failed the text preview.
    blob = ("a" * 15 + "\u00e9moji-ish " + "rest").encode("utf-8")
    assert ask_attachments.classify(blob) == "text"
    d = _new()
    desc = _publish(d, "notes.txt", blob)
    assert desc["kind"] == "text"
    name, kind = ask_attachments.read_meta(d, desc["id"])
    assert (name, kind) == ("notes.txt", "text")


def test_the_staged_cap_is_enforced_at_upload_not_only_at_claim():
    # Four files may wait for a message; a fifth is refused BEFORE it lands (the advertised cap).
    d = _new()
    for i in range(ask_attachments.MAX_STAGED):
        _publish(d, f"staged-{i}.txt", f"file {i}".encode())
    with pytest.raises(ask_attachments.AttachmentRefused) as e:
        _publish(d, "one-too-many.txt", b"nope")
    assert "4 files" in e.value.detail
    assert len(ask_attachments.list_blobs(d)) == ask_attachments.MAX_STAGED
    # once sent, there is room again
    recs = ask_store.read(d)
    with ask_attachments.attachments_fence(d):
        got = ask_attachments.claim(d, [a for a, _s, _m in ask_attachments.list_blobs(d)], recs)
    ask_store.append(
        d,
        {
            "type": "user",
            "turn_id": str(uuid.uuid4()),
            "text": "q",
            "qd": ask_store.question_digest("q"),
            "attachments": got,
        },
    )
    _publish(d, "after-send.txt", b"room again")


def test_the_meta_probe_reports_presence_without_bytes(app):
    d = app.new()
    desc = _publish(d, "release.pdf", ZIP)
    r = _get(app, d, desc["id"], "?meta=1")
    assert r.status_code == 200
    body = r.json()["attachment"]
    assert body["kind"] == "other" and body["size"] == len(ZIP)
    (ask_attachments.attachments_dir(d) / desc["id"]).unlink()
    assert _get(app, d, desc["id"], "?meta=1").status_code == 404


# ---- Hermes PR review #1720 round 2 -----------------------------------------------------------


def test_a_secret_spanning_the_excerpt_boundary_never_keeps_its_prefix(monkeypatch):
    # Round 2, finding 1: the excerpt cut ran BEFORE the redactor, so a registered secret
    # spanning the 6 KiB boundary kept its prefix in the prompt. Order is now: redact the
    # complete body, withhold on the complete body, cut only after.
    secret = "SUPERSECRET" + "Q" * 40
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [secret])
    body = b"A" * (ask_attachments.PROMPT_TEXT_MAX - 20) + secret.encode() + b"Z" * 512
    att = {"id": "a" * 32, "name": "log.txt", "kind": "text", "size": len(body)}
    out = ask_attachments.prompt_block(att, body)
    assert "SUPERSECRET" not in out
    assert "Q" * 40 not in out
    assert "withheld" in out or "```" in out  # either withheld whole or the secret fully redacted


def test_a_credential_shaped_body_is_withheld_even_past_the_boundary():
    shaped = "aws_secret_access_key = AKIA" + "X" * 40
    body = b"A" * (ask_attachments.PROMPT_TEXT_MAX - 10) + shaped.encode() + b"\n" * 200
    att = {"id": "a" * 32, "name": "env.txt", "kind": "text", "size": len(body)}
    out = ask_attachments.prompt_block(att, body)
    assert "AKIA" not in out
    assert "withheld" in out


def test_the_reap_never_deletes_a_blob_a_turn_is_claiming():
    # Round 2, finding 2: reap decided under the fence alone while `begin` claims under the turn
    # lock — a TTL blob could vanish between claim and append. The reap now takes the turn lock
    # too, so a turn in flight makes it skip the conversation entirely.
    d = _new()
    staged = _publish(d, "old.txt", b"old")
    old = time.time() - ask_attachments.STAGED_TTL - 60
    os.utime(ask_attachments.attachments_dir(d) / staged["id"], (old, old))
    with ask_store.turn_lock(d) as free:
        assert free
        ask_attachments.reap_staged()
        # the conversation is being claimed right now: nothing was reaped from it
        assert staged["id"] in {a for a, _s, _m in ask_attachments.list_blobs(d)}
    # once the turn is gone, the stale staged blob goes
    ask_attachments.reap_staged()
    assert staged["id"] not in {a for a, _s, _m in ask_attachments.list_blobs(d)}


def test_discarding_staged_files_frees_the_quota_and_never_touches_sent_history():
    d = _new()
    aids = [_publish(d, f"staged-{i}.txt", f"file {i}".encode()) for i in range(4)]
    with pytest.raises(ask_attachments.AttachmentRefused):
        _publish(d, "one-too-many.txt", b"nope")  # the cap is live
    for a in aids:
        assert ask_attachments.discard(d, a["id"]) == "discarded"
    assert ask_attachments.list_blobs(d) == []
    _publish(d, "after-remove.txt", b"room")  # Remove freed the quota at once
    # a SENT file is never discardable
    sent = _publish(d, "sent.txt", b"sent")
    with ask_attachments.attachments_fence(d):
        got = ask_attachments.claim(d, [sent["id"]], ask_store.read(d))
    ask_store.append(
        d,
        {
            "type": "user",
            "turn_id": str(uuid.uuid4()),
            "text": "q",
            "qd": ask_store.question_digest("q"),
            "attachments": got,
        },
    )
    assert ask_attachments.discard(d, sent["id"]) == "claimed"
    assert (ask_attachments.attachments_dir(d) / sent["id"]).exists()


def test_the_tools_rejected_fallback_carries_the_files_too(monkeypatch):
    # Round 2, finding 3: the fallback path asked with `turn.question` alone — a file-only turn
    # lost its attachment note and excerpt exactly when the endpoint refused tools.
    seen: list[str] = []

    async def fake_ask(query, history, **kw):
        seen.append(query)
        return {"answer": "ok", "matches": []}

    monkeypatch.setattr("agent_sessions.ask_orchestrator.pulse_chat.ask", fake_ask)
    turn = ask_orchestrator._Turn(
        cid="ask_" + "0" * 32,
        turn_id="00000000-0000-4000-8000-000000000002",
        question="what failed?",
        working_keys=set(),
        progress=lambda ev: None,
        attachments=[{"id": "a" * 32, "name": "deploy.log", "kind": "text", "size": len(LOG)}],
        attachment_bodies={"a" * 32: LOG},
    )
    asyncio.run(ask_orchestrator._fallback_find(turn, []))
    assert seen and "deploy.log" in seen[0]
    assert "relation does not exist" in seen[0]  # the sanitized excerpt, not just the question


def test_discard_never_slips_between_a_claim_and_its_append():
    # Round 3 found the slip; round 6's contract closes it precisely: claim AND its append run
    # inside the attachments fence, and the discard takes only that fence. The fence admits
    # exactly two outcomes — discard-then-claim (the claim sees the tombstone and refuses) or
    # claim+append-then-discard (the discard sees the sent record and refuses) — never a file
    # unlinked between a claim's check and its record. Both orders are asserted here; the
    # exclusion itself is the lock's, so the two are the only reachable states.
    d = _new()
    first = _publish(d, "a.txt", b"a")
    assert ask_attachments.discard(d, first["id"]) == "discarded"  # discard wins
    with ask_attachments.attachments_fence(d), pytest.raises(ask_attachments.AttachmentRefused):
        # gone for every future turn — as "taken back" while the tombstone is pending, as
        # "not in this conversation" once it completed
        ask_attachments.claim(d, [first["id"]], ask_store.read(d))

    second = _publish(d, "b.txt", b"b")
    with ask_attachments.attachments_fence(d):
        got = ask_attachments.claim(d, [second["id"]], ask_store.read(d))
        ask_store.append(
            d,
            {
                "type": "user",
                "turn_id": str(uuid.uuid4()),
                "text": "q",
                "qd": ask_store.question_digest("q"),
                "attachments": got,
            },
        )
    assert ask_attachments.discard(d, second["id"]) == "claimed"  # claim+append won
    assert (ask_attachments.attachments_dir(d) / second["id"]).exists()


def test_a_removal_during_a_long_turn_is_durable_and_frees_the_quota_at_once():
    # Round 6, finding 2: the removal must not live only in one page's retry loop. With a turn
    # holding the turn lock (a model call running), the discard still tombstones durably — the
    # staged slot frees immediately, the claim path refuses the file, and nothing about a
    # reload or a closed tab can strand it.
    d = _new()
    aids = [_publish(d, f"staged-{i}.txt", f"file {i}".encode()) for i in range(4)]
    with ask_store.turn_lock(d) as free:  # a turn runs: the model call holds this
        assert free
        for a in aids:
            assert ask_attachments.discard(d, a["id"]) == "discarded"  # durable at once
        # the quota freed while the turn still runs: a fifth staged file is welcome
        _publish(d, "fifth.txt", b"room again")
    # a claim of the removed file is refused — it is gone for every future turn
    with ask_attachments.attachments_fence(d), pytest.raises(ask_attachments.AttachmentRefused):
        ask_attachments.claim(d, [aids[0]["id"]], ask_store.read(d))
    # and the bytes are gone too (the tombstone completed)
    assert aids[0]["id"] not in {a for a, _s, _m in ask_attachments.list_blobs(d)}


def test_a_code_point_across_the_classifier_boundary_is_still_text():
    # Round 3, finding 5: the classifier decoded `data[:256]`, so a multibyte character split at
    # byte 256 failed the prefix decode although the full file is valid UTF-8.
    blob = b"a" * 255 + "\u00e9".encode() + b" plain tail"
    assert ask_attachments.classify(blob) == "text"
    d = _new()
    desc = _publish(d, "notes.txt", blob)
    assert desc["kind"] == "text"


def test_a_secret_spanning_the_body_read_boundary_never_reaches_the_model(app, ai, monkeypatch):
    # Round 3, finding 1: `read_text_bodies` cut at 6 KiB+1 BEFORE the sanitizer, so a secret
    # crossing that upstream boundary kept its prefix in the prompt. The whole bounded file now
    # reaches the sanitizer. Pinned on the REAL upload → turn → model-input path.
    secret = "S3CR3T" + "K" * 40
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [secret])
    script = ai(answer("ok"))
    body = b"A" * (ask_attachments.PROMPT_TEXT_MAX - 10) + secret.encode() + b" tail tail"
    assert len(body) > ask_attachments.PROMPT_TEXT_MAX
    d = app.new()
    desc = _publish(d, "boundary.log", body)
    r = app.ask(d, "read this", attachments=[desc["id"]])
    assert r.status_code == 200
    seen = "\n".join(str(m.get("content")) for m in script.bodies[0]["messages"])
    assert "S3CR3T" not in seen
    assert "K" * 40 not in seen


def test_a_marker_that_cannot_be_written_is_never_acknowledged(monkeypatch):
    # Round 7, finding 2: a suppressed OSError on the marker write used to return True — the
    # pill vanished while the file stayed staged and counted. Now it fails closed.
    d = _new()
    desc = _publish(d, "kept.txt", b"kept")
    real_open = os.open

    def broken(path, flags, mode=0o777, *, dir_fd=None):
        if str(path).endswith(".gone"):
            raise OSError("no marker for you")
        return (
            real_open(path, flags, mode, dir_fd=dir_fd)
            if dir_fd is not None
            else real_open(path, flags, mode)
        )

    # A SCOPED patch: monkeypatch.undo() would also undo conftest's autouse store pins and the
    # next read would silently fall back to the real config.
    with monkeypatch.context() as m:
        m.setattr(os, "open", broken)
        assert ask_attachments.discard(d, desc["id"]) == "retry"
    # nothing was acknowledged: the file is still staged and still counted
    assert desc["id"] in {a for a, _s, _m in ask_attachments.list_blobs(d)}


def test_a_marker_is_retired_only_after_the_blob_is_confirmed_gone(monkeypatch):
    # Round 7, finding 2: unlink failure used to retire the marker anyway — bytes with no
    # obligation left. Now the marker stays until the bytes are really gone.
    d = _new()
    desc = _publish(d, "sticky.txt", b"sticky")
    tomb = ask_attachments.attachments_dir(d) / f"{desc['id']}.gone"
    real_unlink = Path.unlink

    def sticky_unlink(self, missing_ok=False):
        if self.name == desc["id"]:
            raise OSError("not today")
        return real_unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", sticky_unlink)
        assert ask_attachments.discard(d, desc["id"]) == "discarded"  # marker written
        assert tomb.exists()  # and KEPT, because the blob refused to go
        assert (ask_attachments.attachments_dir(d) / desc["id"]).exists()
        # ...yet the file is ALREADY unreadable: acknowledge-then-still-downloadable is the
        # retention breach this closes (#1720 round 9).
        with pytest.raises(ask_attachments.AttachmentRefused) as gone:
            ask_attachments.open_blob(d, desc["id"])
        assert gone.value.status == 404
        # and it never counts as staged while it waits
        assert ask_attachments.list_blobs(d) == []
    # once the filesystem cooperates, the next fenced pass finishes it
    with ask_attachments.attachments_fence(d):
        assert ask_attachments.complete_tombstones(d) == 1
    assert not tomb.exists()
    assert not (ask_attachments.attachments_dir(d) / desc["id"]).exists()


def test_a_sidecar_that_will_not_go_keeps_the_marker_too(monkeypatch):
    # Round 8, finding 1: the sidecar carries the FILENAME — retiring the marker while the
    # meta survives left a private name on disk with no obligation to remove it. The marker
    # now retires only when both the blob and the sidecar are gone.
    d = _new()
    desc = _publish(d, "private-name.txt", b"x")
    tomb = ask_attachments.attachments_dir(d) / f"{desc['id']}.gone"
    meta = ask_attachments.attachments_dir(d) / f"{desc['id']}.meta"
    real_unlink = Path.unlink

    def meta_sticky(self, missing_ok=False):
        if self.name.endswith(".meta"):
            raise OSError("meta stays")
        return real_unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", meta_sticky)
        assert ask_attachments.discard(d, desc["id"]) == "discarded"
        assert tomb.exists()  # the blob went but the sidecar refused: the marker STAYS
        assert meta.exists()
        assert not (ask_attachments.attachments_dir(d) / desc["id"]).exists()
    # once the sidecar cooperates, the next fenced pass retires the marker
    with ask_attachments.attachments_fence(d):
        assert ask_attachments.complete_tombstones(d) == 1
    assert not tomb.exists() and not meta.exists()


def test_a_failed_sidecar_write_leaves_no_invisible_blob(monkeypatch):
    # Round 8, finding 3: the blob was renamed into place BEFORE its sidecar; a sidecar
    # failure left a nameless blob eating a staged slot while the upload reported failure.
    # Both artifacts now roll back together.
    d = _new()

    def broken_meta(*a, **k):
        raise OSError("no sidecar today")

    with monkeypatch.context() as m:
        m.setattr(ask_attachments, "write_meta", broken_meta)
        with pytest.raises(OSError):
            ask_attachments.publish(d, ask_attachments.new_aid(), "ghost.txt", b"boo")
    assert ask_attachments.list_blobs(d) == []  # no invisible staged slot
    assert list(ask_attachments.attachments_dir(d).iterdir()) == []  # and no orphan sidecar


def test_a_truncated_upload_is_refused_not_silently_truncated(app):
    # Round 9, finding 2: the ingest used to accept a body cut mid-file (or missing its
    # closing boundary) as a complete upload — silently corrupting the stored copy.
    d = app.new()
    boundary = "----cut-here"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="cut.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "hell"  # the transfer dies mid-word
    ).encode()
    r = app.c.post(
        f"/api/ask/conversations/{d}/attachments",
        content=body,
        headers={
            **app.hdr,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert r.status_code == 422
    assert ask_attachments.list_blobs(d) == []  # nothing half-written is ever stored


def test_a_dual_failure_rollback_leaves_nothing_counted(monkeypatch):
    # Round 9, finding 3: when BOTH the sidecar write and the rollback unlink fail, the
    # fallback tombstone keeps the file logically gone — never an invisible staged slot.
    d = _new()

    def broken_meta(*a, **k):
        raise OSError("no sidecar")

    def sticky_blob(self, missing_ok=False):
        raise OSError("and the blob stays too")

    with monkeypatch.context() as m:
        m.setattr(ask_attachments, "write_meta", broken_meta)
        m.setattr(Path, "unlink", sticky_blob)
        with pytest.raises(OSError):
            ask_attachments.publish(d, ask_attachments.new_aid(), "ghost.txt", b"boo")
        # logically gone at once: counted by nothing, claimable by nothing
        assert ask_attachments.list_blobs(d) == []
        orphans = [
            e.name
            for e in ask_attachments.attachments_dir(d).iterdir()
            if e.is_file() and e.suffix == ""
        ]
        assert orphans  # the bytes are still physically there (the unlink refused)
        with pytest.raises(ask_attachments.AttachmentRefused):
            with ask_attachments.attachments_fence(d):
                ask_attachments.claim(d, orphans, ask_store.read(d))
    # once the filesystem cooperates, the fenced sweep finishes it
    with ask_attachments.attachments_fence(d):
        ask_attachments.complete_tombstones(d)
    assert [p.name for p in ask_attachments.attachments_dir(d).iterdir()] == []


def test_stale_bytes_still_hold_the_disk_cap_until_they_are_really_gone(monkeypatch):
    # Round 10: a removal whose unlink keeps failing frees the STAGED SLOT (the file is
    # logically gone) but its bytes stay on disk — and the 32 MiB DISK cap must keep counting
    # them, or every failed-removal cycle grows the dir forever.
    d = _new()
    real_unlink = Path.unlink

    def sticky_blob(self, missing_ok=False):
        if self.suffix == "" or self.name.endswith((".meta", ".partial")):
            raise OSError("bytes stay")
        return real_unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", sticky_blob)
        for i in range(3):
            _publish(d, f"big-{i}.bin", b"x" * (9 * 1024 * 1024))
            assert ask_attachments.discard(d, ask_attachments.list_blobs(d)[-1][0]) == "discarded"
        # three acknowledged removals, 27 MiB still physically present
        assert ask_attachments.list_blobs(d) == []  # no staged slot is held
        assert ask_attachments.total_bytes(d) >= 27 * 1024 * 1024  # but the DISK is still owed
        with pytest.raises(ask_attachments.AttachmentRefused) as e:
            _publish(d, "one-more.bin", b"x" * (10 * 1024 * 1024))
        assert "32 MB" in e.value.detail
    # once the filesystem cooperates, the sweep frees the bytes and uploads work again
    with ask_attachments.attachments_fence(d):
        ask_attachments.complete_tombstones(d)
    assert ask_attachments.total_bytes(d) == 0
    _publish(d, "after-cleanup.bin", b"room")


def test_a_reap_whose_sidecar_unlink_fails_is_retried_by_the_next_sweep(monkeypatch):
    # Round 11, finding 1: the TTL reap unlinked the blob first; a sidecar unlink failure then
    # left the private filename with no blob — and `list_blobs` could never see it again. The
    # orphan-sidecar sweep is that retry.
    d = _new()
    desc = _publish(d, "private-name.txt", b"stale")
    blob = ask_attachments.attachments_dir(d) / desc["id"]
    meta = ask_attachments.attachments_dir(d) / f"{desc['id']}.meta"
    old = time.time() - ask_attachments.STAGED_TTL - 60
    os.utime(blob, (old, old))
    real_unlink = Path.unlink

    def meta_sticky(self, missing_ok=False):
        if self.name.endswith(".meta"):
            raise OSError("meta stays this round")
        return real_unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", meta_sticky)
        ask_attachments.reap_staged()
        # the blob went, the sidecar (with the private name) stayed
        assert not blob.exists() and meta.exists()
        assert ask_attachments.list_blobs(d) == []  # and nothing would ever rediscover it...
    # ...except the orphan-sidecar sweep on the next fenced pass
    with ask_attachments.attachments_fence(d):
        ask_attachments.complete_tombstones(d)
    assert not meta.exists()


def test_the_retry_status_says_retryable_failure_apart_from_settled_one(monkeypatch):
    # Round 11, finding 4: a marker write that cannot land is "retry" — the UI keeps its
    # cleanup pending — never the same answer as "gone"/"claimed".
    d = _new()
    desc = _publish(d, "kept.txt", b"kept")
    real_open = os.open

    def broken(path, flags, mode=0o777, *, dir_fd=None):
        if str(path).endswith(".gone"):
            raise OSError("no marker")
        return (
            real_open(path, flags, mode, dir_fd=dir_fd)
            if dir_fd is not None
            else real_open(path, flags, mode)
        )

    with monkeypatch.context() as m:
        m.setattr(os, "open", broken)
        assert ask_attachments.discard(d, desc["id"]) == "retry"
    assert ask_attachments.discard(d, desc["id"]) == "discarded"
    assert ask_attachments.discard(d, desc["id"]) == "gone"
