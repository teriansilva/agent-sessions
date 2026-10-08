"""Image input for native API turns (#1332 Phase 3): admission, the journaled identity, the
worker's re-verification, and each protocol's image shape."""

from __future__ import annotations

import base64
import hashlib
import os

import pytest

from agent_sessions import native_images, native_ipc, native_protocol
from agent_sessions.plugins import kinds

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def uploads(tmp_home):
    d = tmp_home / ".agent-sessions" / "uploads"
    d.mkdir(parents=True, mode=0o700)

    def put(name: str, data: bytes) -> str:
        (d / name).write_bytes(data)
        return name

    put.dir = d
    return put


def test_every_api_kind_decides_whether_it_takes_images():
    # A new adapter cannot ship without saying (#1332 Phase 3).
    assert set(kinds.API_IMAGE_INPUT) == set(kinds.API_KINDS)
    assert all(type(v) is bool for v in kinds.API_IMAGE_INPUT.values())


def test_admission_binds_name_digest_and_sniffed_format(uploads):
    a = uploads("20261008-010000-shot.png", PNG)
    b = uploads("20261008-010001-photo.jpg", JPEG)
    assert native_images.admit([a, b]) == [
        {"stored": a, "sha256": hashlib.sha256(PNG).hexdigest(), "mime": "image/png"},
        {"stored": b, "sha256": hashlib.sha256(JPEG).hexdigest(), "mime": "image/jpeg"},
    ]
    assert native_images.admit(None) == []


@pytest.mark.parametrize(
    "names",
    [
        ["../etc/passwd"],
        ["/etc/hosts"],
        ["shot.png"],  # not the stored shape: a hand-placed file is not an upload
        ["20261008-010000-notes.txt"],  # not a picture suffix
        ["20261008-010000-missing.png"],
        "20261008-010000-shot.png",  # not a list
        ["20261008-010000-shot.png"] * 2,
    ],
)
def test_admission_refuses_anything_but_distinct_uploaded_pictures(uploads, names):
    uploads("20261008-010000-shot.png", PNG)
    uploads("20261008-010000-notes.txt", b"hello")
    with pytest.raises(native_images.ImageError):
        native_images.admit(names)


def test_admission_sniffs_content_not_the_suffix(uploads):
    # The upload route accepts any file; a script renamed .png is not a picture.
    name = uploads("20261008-010000-fake.png", b"#!/bin/sh\necho hi\n")
    with pytest.raises(native_images.ImageError, match="not the image"):
        native_images.admit([name])
    jpeg_as_png = uploads("20261008-010001-swap.png", JPEG)
    with pytest.raises(native_images.ImageError):
        native_images.admit([jpeg_as_png])


def test_admission_caps_count_and_size(uploads):
    names = [uploads(f"20261008-01000{i}-s.png", PNG) for i in range(native_images.MAX_IMAGES + 1)]
    with pytest.raises(native_images.ImageError, match="at most"):
        native_images.admit(names)
    big = uploads("20261008-020000-big.png", PNG + b"\x00" * native_images.MAX_IMAGE_BYTES)
    with pytest.raises(native_images.ImageError, match="exceeds"):
        native_images.admit([big])


def test_admission_never_follows_a_symlink(uploads, tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    os.symlink(outside, uploads.dir / "20261008-010000-link.png")
    with pytest.raises(native_images.ImageError):
        native_images.admit(["20261008-010000-link.png"])


def test_inlining_rereads_and_refuses_a_changed_picture(uploads):
    name = uploads("20261008-010000-shot.png", PNG)
    [admitted] = native_images.admit([name])
    assert native_images.inline([admitted]) == [
        ("image/png", base64.b64encode(PNG).decode("ascii"))
    ]
    (uploads.dir / name).write_bytes(PNG + b"tampered")
    with pytest.raises(native_images.ImageError, match="changed"):
        native_images.inline([admitted])


def _submit(params):
    return native_ipc.normalize_immutable_request({"action": "submit", "params": params})


def test_ipc_identity_carries_the_pictures_and_old_records_still_read():
    item = {"stored": "20261008-010000-shot.png", "sha256": "a" * 64, "mime": "image/png"}
    out = _submit({"text": "", "context": {}, "attachments": [item]})
    assert out["params"]["attachments"] == [item]
    # A text-only turn journaled before Phase 3 has no such key and reads exactly as written.
    assert _submit({"text": "hi", "context": {}}) == {
        "action": "submit",
        "params": {"text": "hi", "context": {}},
    }
    with pytest.raises(native_ipc.IPCError):
        _submit({"text": "", "context": {}})  # words OR pictures
    with pytest.raises(native_ipc.IPCError):
        _submit({"text": "x", "context": {}, "attachments": []})
    for bad in (
        {**item, "stored": "../x.png"},
        {**item, "sha256": "zz"},
        {**item, "mime": "text/html"},
        {**item, "extra": 1},
    ):
        with pytest.raises(native_ipc.IPCError):
            _submit({"text": "x", "context": {}, "attachments": [bad]})


def test_codex_sends_pictures_as_data_url_items_before_the_words():
    codec = native_protocol.CodexCodec()
    codec.native_id = "thread-1"
    frame = codec.submit("look", "00000000-0000-4000-8000-000000000001", [("image/png", "QUJD")])
    assert frame["params"]["input"] == [
        {"type": "image", "url": "data:image/png;base64,QUJD"},
        {"type": "text", "text": "look"},
    ]
    codec = native_protocol.CodexCodec()
    codec.native_id = "thread-1"
    only = codec.submit("", "00000000-0000-4000-8000-000000000002", [("image/png", "QUJD")])
    assert only["params"]["input"] == [{"type": "image", "url": "data:image/png;base64,QUJD"}]


def test_claude_sends_pictures_as_base64_blocks_and_text_alone_stays_a_string():
    codec = native_protocol.ClaudeCodec("11111111-1111-4111-8111-111111111111")
    frame = codec.submit("look", "00000000-0000-4000-8000-000000000001", [("image/jpeg", "QUJD")])
    assert frame["message"]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}},
        {"type": "text", "text": "look"},
    ]
    plain = native_protocol.ClaudeCodec("11111111-1111-4111-8111-111111111111")
    words = plain.submit("hello", "00000000-0000-4000-8000-000000000002")
    assert words["message"]["content"] == "hello"


def test_an_empty_turn_without_pictures_is_still_refused():
    codec = native_protocol.ClaudeCodec("11111111-1111-4111-8111-111111111111")
    with pytest.raises(native_protocol.ProtocolError):
        codec.submit("  ", "00000000-0000-4000-8000-000000000001")


def test_a_frame_with_four_full_size_pictures_fits_the_write_cap():
    data = base64.b64encode(b"\x00" * native_images.MAX_IMAGE_BYTES).decode("ascii")
    codec = native_protocol.ClaudeCodec("11111111-1111-4111-8111-111111111111")
    frame = codec.submit("x", "00000000-0000-4000-8000-000000000001", [("image/png", data)] * 4)
    assert native_protocol.encode(frame, native_protocol.MAX_WRITE_FRAME_BYTES)
    with pytest.raises(native_protocol.ProtocolError):
        native_protocol.encode(frame)  # what the agent may send US keeps the 1 MiB cap
