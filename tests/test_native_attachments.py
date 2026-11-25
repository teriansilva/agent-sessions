"""Documents use the terminal's upload references; images keep native protocol input."""

import hashlib
import os

import pytest

from agent_sessions import native_attachments, native_ipc, native_protocol, native_worker

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def uploads(tmp_home):
    folder = tmp_home / ".agent-sessions" / "uploads"
    folder.mkdir(parents=True, mode=0o700)

    def put(name, data=b"document contents"):
        stored = "20261010-010000-" + name
        path = folder / stored
        path.write_bytes(data)
        return stored, path

    return put


@pytest.mark.parametrize(
    "name",
    [
        "brief.pdf",
        "draft.docx",
        "table.xlsx",
        "rows.csv",
        "notes.txt",
        "data.zip",
        "LICENSE",
        "drawing.svg",
    ],
)
def test_any_document_is_bound_and_delivered_by_server_owned_path(uploads, name):
    stored, path = uploads(name)
    admitted = native_attachments.admit([stored], images=True)
    assert admitted == [
        {
            "stored": stored,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mime": "application/octet-stream",
        }
    ]
    request = native_ipc.normalize_immutable_request(
        {"action": "submit", "params": {"text": "", "context": {}, "attachments": admitted}}
    )
    assert request["params"]["attachments"] == admitted
    text, images = native_attachments.prepare("", admitted)
    assert str(path) in text
    assert "document contents" not in text
    assert images == []


def test_mixed_images_and_documents_and_clients_without_native_images(uploads):
    picture, picture_path = uploads("shot.png", PNG)
    document, document_path = uploads("brief.pdf")
    admitted = native_attachments.admit([picture, document], images=True)
    text, images = native_attachments.prepare("Read both", admitted)
    assert text.startswith("Read both\n")
    assert str(document_path) in text and str(picture_path) not in text
    assert len(images) == 1 and images[0][0] == "image/png"
    files_only = native_attachments.admit([picture, document], images=False)
    text, images = native_attachments.prepare("Read both", files_only)
    assert str(picture_path) in text and str(document_path) in text
    assert images == []


@pytest.mark.parametrize(
    "names",
    [
        ["../etc/passwd"],
        ["/etc/passwd"],
        ["manual.pdf"],
        ["20261010-010000-missing.pdf"],
        "20261010-010000-brief.pdf",
        ["20261010-010000-brief.pdf"] * 2,
    ],
)
def test_invalid_names_and_duplicates_refuse(uploads, names):
    uploads("brief.pdf")
    with pytest.raises(native_attachments.AttachmentError):
        native_attachments.admit(names, images=True)


def test_document_count_size_empty_and_changed_file_refuse(uploads, monkeypatch):
    names = [uploads(f"{i}.pdf")[0] for i in range(5)]
    with pytest.raises(native_attachments.AttachmentError, match="at most"):
        native_attachments.admit(names, images=True)
    stored, path = uploads("brief.pdf")
    admitted = native_attachments.admit([stored], images=True)
    path.write_bytes(b"different contents")
    with pytest.raises(native_attachments.AttachmentError, match="changed"):
        native_attachments.prepare("x", admitted)
    monkeypatch.setattr(native_attachments, "MAX_FILE_BYTES", 8)
    with pytest.raises(native_attachments.AttachmentError, match="exceeds"):
        native_attachments.admit([stored], images=True)
    path.write_bytes(b"")
    with pytest.raises(native_attachments.AttachmentError, match="empty"):
        native_attachments.admit([stored], images=True)


def test_missing_file_is_refused_before_worker_builds_any_native_frame(uploads):
    stored, path = uploads("brief.pdf")
    admitted = native_attachments.admit([stored], images=True)
    path.unlink()
    worker = object.__new__(native_worker.Worker)
    worker.codec = native_protocol.CodexCodec()
    worker.codec.native_id = "thread-1"
    with pytest.raises(native_protocol.ProtocolError, match="no longer available"):
        worker.frame(
            "submit",
            {
                "text": "read",
                "operation_id": "00000000-0000-4000-8000-000000000001",
                "attachments": admitted,
            },
        )
    assert worker.codec.operation_id is None


def test_symlink_directory_and_replaced_upload_folder_refuse(uploads, tmp_path):
    stored, path = uploads("brief.pdf")
    admitted = native_attachments.admit([stored], images=True)
    outside = tmp_path / "other.pdf"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(native_attachments.AttachmentError):
        native_attachments.prepare("x", admitted)
    with pytest.raises(native_attachments.AttachmentError):
        native_attachments.admit([stored], images=True)
    path.unlink()
    path.mkdir()
    with pytest.raises(native_attachments.AttachmentError):
        native_attachments.admit([stored], images=True)
    folder = path.parent
    moved = folder.with_name("moved")
    folder.rename(moved)
    os.symlink(moved, folder)
    with pytest.raises(native_attachments.AttachmentError):
        native_attachments.admit([stored], images=True)


@pytest.mark.parametrize("kind", ["codex", "claude", "opencode"])
def test_worker_frame_delivers_documents_to_every_codec(uploads, kind):
    stored, path = uploads("brief.pdf")
    worker = object.__new__(native_worker.Worker)
    if kind == "codex":
        worker.codec = native_protocol.CodexCodec()
        worker.codec.native_id = "thread-1"
    elif kind == "claude":
        worker.codec = native_protocol.ClaudeCodec("11111111-1111-4111-8111-111111111111")
    else:
        worker.codec = native_protocol.OpencodeAcpCodec(agent="battlelab-api-0123456789abcdef")
        worker.codec.native_id = "ses_test1234567890"
        worker.codec._pinned = True
    frame = worker.frame(
        "submit",
        {
            "text": "",
            "operation_id": "00000000-0000-4000-8000-000000000001",
            "attachments": native_attachments.admit([stored], images=True),
        },
    )
    assert str(path) in str(frame)
    assert "document contents" not in str(frame)
