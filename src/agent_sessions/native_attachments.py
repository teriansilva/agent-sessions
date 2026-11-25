"""Native turn attachments: inline pictures and terminal-style file references (#1396).

Only upload basenames cross the client/IPC boundary. The journal binds name, digest and kind
(image MIME or generic file MIME), never bytes or a client path. Files are hashed through the
upload store's bound no-follow descriptors at admission and again immediately before native
handoff. A reference is built only after every attachment passes. Subsequent agent tool reads
use normal native permissions and, like terminal attachments, observe the file at read time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from . import native_images
from .routes.upload import IMAGE_TYPES, MAX_UPLOAD_BYTES, STORED_RE, open_upload, uploads_dir

MAX_ATTACHMENTS = native_images.MAX_IMAGES
MAX_FILE_BYTES = MAX_UPLOAD_BYTES
FILE_MIME = "application/octet-stream"
AttachmentError = native_images.ImageError
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _name(stored) -> str:
    if not isinstance(stored, str) or STORED_RE.fullmatch(stored) is None:
        raise AttachmentError("an attachment must name an upload")
    return stored


def _digest(stored: str) -> str:
    try:
        fd, st = open_upload(stored)
    except OSError:
        raise AttachmentError("an attached file is no longer available") from None
    digest, size = hashlib.sha256(), 0
    with os.fdopen(fd, "rb") as fh:
        if st.st_size > MAX_FILE_BYTES:
            raise AttachmentError("an attached file exceeds the upload size limit")
        while chunk := fh.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise AttachmentError("an attached file exceeds the upload size limit")
            digest.update(chunk)
    if not size:
        raise AttachmentError("an attached file is empty")
    return digest.hexdigest()


def admit(names, *, images: bool) -> list[dict]:
    """Admit a mixed list; native pictures retain their existing format and size validation."""
    if names is None:
        return []
    if not isinstance(names, list) or len(names) > MAX_ATTACHMENTS:
        raise AttachmentError(f"a turn carries at most {MAX_ATTACHMENTS} attachments")
    out = []
    for name in names:
        stored = _name(name)
        if any(item["stored"] == stored for item in out):
            raise AttachmentError("the same file is attached twice")
        if images and os.path.splitext(stored)[1].lower() in IMAGE_TYPES:
            out.extend(native_images.admit([stored]))
        else:
            out.append({"stored": stored, "sha256": _digest(stored), "mime": FILE_MIME})
    return out


def normalize(value) -> list[dict]:
    """Closed journal/IPC identity; legacy image records normalize byte-for-byte as before."""
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise AttachmentError("invalid attachments")
    out = []
    for item in value:
        if type(item) is not dict or item.keys() != {"stored", "sha256", "mime"}:
            raise AttachmentError("invalid attachments")
        if not isinstance(item["mime"], str):
            raise AttachmentError("invalid attachments")
        if item["mime"] != FILE_MIME:
            out.extend(native_images.normalize([item]))
            continue
        stored, digest = _name(item["stored"]), item["sha256"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise AttachmentError("invalid attachments")
        out.append({"stored": stored, "sha256": digest, "mime": FILE_MIME})
    if len({a["stored"] for a in out}) != len(out):
        raise AttachmentError("invalid attachments")
    return out


def public(item: dict) -> dict:
    """Safe bounded display name, retaining collision prefixes and original numeric names."""
    stored = _name(item["stored"])
    return {"stored": stored, "name": stored[16:], "mime": item["mime"]}


def text_with_files(text: str, attachments: list[dict]) -> str:
    """Construct the exact delivered text so admission can reserve reference space."""
    files = [
        json.dumps(str(uploads_dir() / item["stored"]))
        for item in normalize(attachments)
        if item["mime"] == FILE_MIME
    ]
    if files:
        return (
            (text + "\n\n" if text else "")
            + "Attached files (read these paths):\n"
            + "\n".join(files)
        )
    return text


def prepare(text: str, attachments: list[dict]) -> tuple[str, list[tuple[str, str]]]:
    """Reverify all attachments, then build native image blocks and server-owned file paths."""
    pictures = []
    for item in normalize(attachments):
        if item["mime"] != FILE_MIME:
            pictures.extend(native_images.inline([item]))
        else:
            if _digest(item["stored"]) != item["sha256"]:
                raise AttachmentError("an attached file changed after it was attached")
    return text_with_files(text, attachments), pictures
