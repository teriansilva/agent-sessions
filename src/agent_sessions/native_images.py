"""Image input for native API turns (#1332 Phase 3).

A turn names its pictures by the upload they were pasted into (``routes/upload.py``), never by a
path: the stored basename is the only thing that can name an upload, and it is opened through
the same bound, no-follow descriptor walk the read-back route uses. Two checkpoints:

* **Admission** (the web request, before anything durable): each name is an upload, a regular
  file, within the size cap, and its bytes ARE a picture of an allowed format (sniffed — the
  upload route itself accepts any file). What survives is ``{stored, sha256, mime}``; that, not
  the bytes, is what the journal binds, so a retry with the same operation id must name the
  same pictures with the same content.
* **Inlining** (the worker, after the durable claim): the bytes are read again and must still
  hash to the admitted digest, so a file replaced between the two never reaches the model. Only
  then are they base64-encoded into the protocol frame. A mismatch refuses the frame, and the
  turn is recorded as not sent.

The bytes go to the worker's agent and nowhere else: the journal, the snapshot and the browser
see the names and digests only; the browser shows a thumbnail through the existing read-back
route.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re

from .routes.upload import IMAGE_TYPES, STORED_RE, open_upload

#: Pictures per turn, and bytes per picture (the Claude API's per-image limit; Codex accepts it).
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
#: The formats a turn may carry, by sniffed content. The suffix must agree.
MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class ImageError(ValueError):
    """A safe refusal; the message names no path and echoes no client value."""


def sniff(head: bytes) -> str | None:
    """The picture format the bytes actually are, or ``None``."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read(stored: str) -> bytes:
    try:
        fd, st = open_upload(stored)
    except OSError:
        raise ImageError("an attached image is no longer available") from None
    with os.fdopen(fd, "rb") as fh:
        if st.st_size > MAX_IMAGE_BYTES:
            raise ImageError(f"an attached image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MiB")
        data = fh.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"an attached image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MiB")
    return data


def _name(stored) -> str:
    if not isinstance(stored, str) or STORED_RE.fullmatch(stored) is None:
        raise ImageError("an attachment must name an upload")
    suffix = os.path.splitext(stored)[1].lower()
    if suffix not in IMAGE_TYPES:
        raise ImageError("only PNG, JPEG, GIF and WebP images can be attached")
    return stored


def admit(names) -> list[dict]:
    """Admission: upload names from the client → the identity the journal binds."""
    if names is None:
        return []
    if not isinstance(names, list) or len(names) > MAX_IMAGES:
        raise ImageError(f"a turn carries at most {MAX_IMAGES} images")
    out = []
    for stored in names:
        stored = _name(stored)
        if any(item["stored"] == stored for item in out):
            raise ImageError("the same image is attached twice")
        data = _read(stored)
        mime = sniff(data[:16])
        if mime is None or mime != IMAGE_TYPES[os.path.splitext(stored)[1].lower()]:
            raise ImageError("an attachment is not the image its name says it is")
        out.append({"stored": stored, "sha256": hashlib.sha256(data).hexdigest(), "mime": mime})
    return out


def normalize(value) -> list[dict]:
    """The admitted identity as the private IPC and the journal carry it (shape only)."""
    if not isinstance(value, list) or len(value) > MAX_IMAGES:
        raise ImageError("invalid attachments")
    out = []
    for item in value:
        if type(item) is not dict or item.keys() != {"stored", "sha256", "mime"}:
            raise ImageError("invalid attachments")
        stored = _name(item["stored"])
        digest, mime = item["sha256"], item["mime"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None or mime not in MIMES:
            raise ImageError("invalid attachments")
        out.append({"stored": stored, "sha256": digest, "mime": mime})
    if len({item["stored"] for item in out}) != len(out):
        raise ImageError("invalid attachments")
    return out


def inline(attachments: list[dict]) -> list[tuple[str, str]]:
    """Inlining (worker): ``(mime, base64)`` per picture, each re-read and re-verified."""
    out = []
    for item in attachments:
        data = _read(item["stored"])
        if hashlib.sha256(data).hexdigest() != item["sha256"] or sniff(data[:16]) != item["mime"]:
            raise ImageError("an attached image changed after it was attached")
        out.append((item["mime"], base64.b64encode(data).decode("ascii")))
    return out
