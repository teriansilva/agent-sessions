"""The `nextcloud` adapter (#1829): the Commander's own files, read-only, exactly these requests
under the instance's origin.

* ``GET /ocs/v2.php/cloud/user`` and one ``PROPFIND Depth:1`` of the root — the probe
* ``PROPFIND Depth:1 /remote.php/dav/files/<id>/<path>/`` — a folder
* ``GET /ocs/v2.php/search/providers/files/search?term=`` — search by name
* ``PROPFIND Depth:0`` then ``GET /remote.php/dav/files/<id>/<path>`` — one text file

``<id>`` is the user ID Nextcloud names in the probe, which can differ from the login name the
app password signs in with (LDAP, e-mail logins); it is kept as ``tested.login`` and reaches the
adapter as ``Bound.principal``.

**A reply cannot steer a read elsewhere.** Every ``href`` is decoded and normalised, and must lie
under the account's own DAV root, or the entry is dropped. Paths are validated with
`common.valid_path` both when listed and again when a ref is used. The transport sends only GET
and PROPFIND for this kind, with the PROPFIND body fixed there, and parses XML only after refusing
any document type or entity.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit

from ... import connector_http
from ...connector_http import Bound, ConnectorError
from .common import DIR_MAX, FILE_MAX_BYTES, SEARCH_LIMIT, seg, text, valid_path

DAV = "{DAV:}"
DAV_ROOT = "/remote.php/dav/files/"
#: Content types read as text. Anything else is refused before the GET; the bytes are checked
#: again after it (no NUL, valid UTF-8).
TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/javascript",
        "application/x-sh",
        "application/x-shellscript",
        "application/sql",
    }
)


def _root(principal: str) -> str:
    if not principal or "/" in principal:
        raise ConnectorError("needs_test", "test the connector first: its user is not known")
    return DAV_ROOT + seg(principal)


def _dav_path(principal: str, path: str, *, folder: bool) -> str:
    if not valid_path(path):
        raise ConnectorError("refused", "not a path in your files")
    base = _root(principal)
    if path == "":
        return base + "/"
    return base + "/" + "/".join(seg(s) for s in path.split("/")) + ("/" if folder else "")


def _is_text(content_type: str) -> bool:
    kind = content_type.split(";", 1)[0].strip().lower()
    return kind.startswith("text/") or kind in TEXT_TYPES


def _prop(response, name: str):
    """``<d:prop>/<name>`` from the 200 propstat of one ``<d:response>``, or None."""
    for stat in response.findall(f"{DAV}propstat"):
        status = stat.findtext(f"{DAV}status") or ""
        if " 200 " not in f"{status} ":
            continue
        prop = stat.find(f"{DAV}prop")
        if prop is not None:
            found = prop.find(f"{DAV}{name}")
            if found is not None:
                return found
    return None


def _modified(value: str | None) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return ""


def _entries(root_el, principal: str, folder: str) -> list[dict]:
    """A multistatus reply as ``[{name, type, path, size, modified, content_type}]``: only hrefs
    under this account's own root, never the folder itself, at most ``DIR_MAX`` (`listing` says
    whether that cap cut it)."""
    if root_el.tag != f"{DAV}multistatus":
        raise ConnectorError("bad_reply", "the server did not answer as WebDAV does")
    prefix = DAV_ROOT + principal + "/"
    out: list[dict] = []
    for response in root_el.findall(f"{DAV}response"):
        href = response.findtext(f"{DAV}href") or ""
        try:
            decoded = unquote(urlsplit(href).path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            continue
        if not decoded.startswith(prefix):
            continue  # outside the account's files: dropped, never followed
        rel = decoded[len(prefix) :].rstrip("/")
        if rel == folder.rstrip("/") or not valid_path(rel) or rel == "":
            continue
        is_dir = (
            (rt := _prop(response, "resourcetype")) is not None
            and rt.find(f"{DAV}collection") is not None
        )  # fmt: skip
        out.append(
            {
                "name": rel.rsplit("/", 1)[-1],
                "type": "dir" if is_dir else "file",
                "path": rel,
                "size": _size(response),
                "modified": _modified(_text_of(response, "getlastmodified")),
                "content_type": text(_text_of(response, "getcontenttype"), 100),
            }
        )
        if len(out) >= DIR_MAX:
            break
    return out


#: The longest size a reply may state, in digits (a petabyte has 16). A longer or non-decimal
#: `getcontentlength` is a malformed reply, never converted: Python refuses `int()` past 4300
#: digits, and that error must not escape the connector's own (Hermes on #1834).
SIZE_DIGITS_MAX = 16


def _size(response) -> int:
    el = _prop(response, "getcontentlength")
    raw = (el.text or "").strip() if el is not None else ""
    if raw == "":
        return 0
    if not (raw.isascii() and raw.isdigit()) or len(raw) > SIZE_DIGITS_MAX:
        raise ConnectorError("bad_reply", "the server reported a file size that is not a size")
    return int(raw)


def _text_of(response, name: str) -> str:
    el = _prop(response, name)
    return (el.text or "").strip() if el is not None else ""


async def _propfind(bound: Bound, path: str, depth: str):
    return await connector_http._request(
        bound, "PROPFIND", path, propfind="entries", depth=depth, reply="xml"
    )


def _ocs_data(reply: object) -> dict:
    """An OCS reply's ``ocs.data`` object, or `bad_reply`: every level is type-checked before it
    is read, so a malformed envelope is a connector error, never an AttributeError and never an
    apparently empty answer (Hermes on #1834)."""
    ocs = reply.get("ocs") if isinstance(reply, dict) else None
    data = ocs.get("data") if isinstance(ocs, dict) else None
    if not isinstance(data, dict):
        raise ConnectorError("bad_reply", "the address did not answer as Nextcloud does")
    return data


async def probe(bound: Bound) -> dict:
    """The user ID Nextcloud names for these credentials, and how many items the root holds."""
    me = await connector_http._request(bound, "GET", "/ocs/v2.php/cloud/user", {"format": "json"})
    data = _ocs_data(me)
    uid = data.get("id") if isinstance(data, dict) else None
    if not isinstance(uid, str) or not uid or "/" in uid or len(uid) > 64:
        raise ConnectorError("bad_reply", "the address did not answer as Nextcloud does")
    entries = _entries(await _propfind(bound, _root(uid) + "/", "1"), uid, "")
    return {"login": uid, "repos": len(entries)}


async def list_folder(bound: Bound, path: str) -> dict:
    """``{entries, complete}``: ``complete`` is False when the folder held more than ``DIR_MAX``
    entries (a reply over the transport's 1 MiB cap is an error, ``too_large``, never a partial
    list)."""
    folder = path.rstrip("/")
    root_el = await _propfind(bound, _dav_path(bound.principal, folder, folder=True), "1")
    entries = _entries(root_el, bound.principal, folder)
    listed = len(root_el.findall(f"{DAV}response")) - 1  # the folder itself is one response
    return {"entries": entries, "complete": listed <= DIR_MAX}


async def search(bound: Bound, query: str) -> dict:
    """``{entries: [{name, type, path}], complete}`` from Nextcloud's unified search (the files
    provider). A result names its file by ``attributes.path``, a path relative to the user's
    files (``/Documents/plan.txt``); its ``resourceUrl`` is a web link and is never used. A result
    without a path that `common.valid_path` accepts (``..``, ``.``, empty or control characters)
    is dropped, so only a path under the account's own DAV root is ever minted. ``complete`` is
    False when Nextcloud said more results exist (``isPaginated`` with a ``cursor``) or the page
    was full."""
    got = await connector_http._request(
        bound,
        "GET",
        "/ocs/v2.php/search/providers/files/search",
        {"term": query, "limit": SEARCH_LIMIT, "format": "json"},
    )
    data = _ocs_data(got)
    rows = data.get("entries")
    if not isinstance(rows, list):
        raise ConnectorError("bad_reply", "the address did not answer as Nextcloud search does")
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        raw = attrs.get("path")
        if not isinstance(raw, str):
            continue
        rel = raw.strip("/")
        if not rel or not valid_path(rel):
            continue
        icon = text(row.get("icon"), 200).lower()
        out.append(
            {
                "name": rel.rsplit("/", 1)[-1],
                "type": "dir" if "folder" in icon else "file",
                "path": rel,
            }
        )
    more = (
        isinstance(data, dict) and bool(data.get("isPaginated")) and data.get("cursor") is not None
    )
    full = isinstance(rows, list) and len(rows) >= SEARCH_LIMIT
    return {"entries": out[:SEARCH_LIMIT], "complete": not (more or full)}


async def read_file(bound: Bound, path: str) -> dict:
    if path == "":
        raise ConnectorError("refused", "not a file path")
    meta = _entries_self(
        await _propfind(bound, _dav_path(bound.principal, path, folder=False), "0")
    )
    if meta["dir"]:
        raise ConnectorError("refused", "that path is a folder; list it instead")
    if meta["size"] > FILE_MAX_BYTES:
        raise ConnectorError("too_large", "that file is too large to read here")
    if not _is_text(meta["content_type"]):
        raise ConnectorError("binary", "that file is not text; only text is read")
    raw = await connector_http._request(
        bound, "GET", _dav_path(bound.principal, path, folder=False), reply="text"
    )
    if not isinstance(raw, bytes) or len(raw) > FILE_MAX_BYTES:
        raise ConnectorError("too_large", "that file is too large to read here")
    if b"\x00" in raw:
        raise ConnectorError("binary", "that file is binary; only text is read")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConnectorError("binary", "that file is not UTF-8 text; only text is read") from None
    return {
        "path": path,
        "size": len(raw),
        "modified": meta["modified"],
        "content_type": meta["content_type"],
        "text": decoded,
    }


def _entries_self(root_el) -> dict:
    """A Depth:0 reply: the one resource's own properties."""
    if root_el.tag != f"{DAV}multistatus":
        raise ConnectorError("bad_reply", "the server did not answer as WebDAV does")
    response = root_el.find(f"{DAV}response")
    if response is None:
        raise ConnectorError("not_found", "no such file")
    rt = _prop(response, "resourcetype")
    size = _size(response)
    return {
        "dir": rt is not None and rt.find(f"{DAV}collection") is not None,
        "size": size,
        "modified": _modified(_text_of(response, "getlastmodified")),
        "content_type": text(_text_of(response, "getcontenttype"), 100),
    }
