"""Where an HQ-imported skill came from (#2145): display-only, unverified, never trust.

A skill HQ imported from a repository link records ``{url, commit, path, unverified: true}`` as
an extended attribute of the skill's directory, set on the STAGING directory before the one
publishing rename (`skills._stage`), so a save carries bytes and source together or neither.

It is deliberately NOT a gallery origin (`origins.py`): a gallery origin is derived from a signed
publication and verified; this record is the operator's own import, written by the app from the
commit it fetched, and nothing about it is checked against the repository again. It never earns a
Verified badge, never enables Review update or a refresh, and nothing re-fetches it. The Library
shows it as "From a repository (unverified)". A record that cannot be read reads as None.
"""

from __future__ import annotations

import errno
import json
import os
import re

from . import contract

XATTR = "user.battlelab.repo_origin"
URL_MAX = 2048
PATH_MAX = 512
_SHA = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def check(value: object) -> dict:
    """The one accepted shape, exactly; anything else refuses the save."""
    if not isinstance(value, dict) or set(value) - {"url", "commit", "path", "unverified"}:
        raise contract.GalleryError("Invalid repository source.")
    url, commit, path = value.get("url"), value.get("commit"), value.get("path")
    if (
        not isinstance(url, str)
        or not url.lower().startswith("https://")
        or len(url) > URL_MAX
        or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url)
        or "@" in url.split("/", 3)[2]
    ):
        raise contract.GalleryError("A repository source must be a public https link.")
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise contract.GalleryError("A repository source names one full commit id.")
    if (
        not isinstance(path, str)
        or len(path) > PATH_MAX
        or path.startswith("/")
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path)
        or any(p in (".", "..") for p in path.split("/"))
    ):
        raise contract.GalleryError("Invalid repository folder.")
    if value.get("unverified", True) is not True:
        raise contract.GalleryError("A repository source is always unverified.")
    return {"url": url, "commit": commit, "path": path, "unverified": True}


def bind(fd: int, value: dict) -> None:
    data = json.dumps({"format": 1, **check(value)}, sort_keys=True).encode("utf-8")
    try:
        os.setxattr(fd, XATTR, data, flags=os.XATTR_CREATE)
        os.fsync(fd)
    except OSError:
        raise contract.GalleryError(
            "This filesystem cannot keep the skill's repository source; nothing was saved."
        ) from None
    if read(fd) != check(value):
        raise contract.GalleryError("The repository source changed while staging.")


def read(fd: int) -> dict | None:
    try:
        raw = os.getxattr(fd, XATTR)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP):
            return None
        return None
    try:
        record = json.loads(raw)
        if not isinstance(record, dict) or record.pop("format", None) != 1:
            return None
        return check(record)
    except (ValueError, contract.GalleryError):
        return None
