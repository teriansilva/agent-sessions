"""HQ connectors, P1b (#1829): the read-only Nextcloud files connector (`nextcloud`).

Against a fake Nextcloud (OCS + WebDAV) that records every request:

* the store: a `basic` connector carries an account; changing it needs the password again, inside
  the lock; an older store without the field still reads;
* the transport: only GET and PROPFIND, on the kind's paths, refused before the secret is fetched;
  the PROPFIND body and Depth come from the transport's table; Basic auth is built there;
  a DOCTYPE or entity in a reply is refused;
* the adapter: paths use the user id the probe named (not the login name); an href outside the
  account's root, or climbing out of it, is dropped; binary, non-text and oversize files are
  refused before they are fetched;
* HQ's storage tools: the root ref, search by name, list a folder, read a document; forge kinds
  refused on storage and `files` on a forge;
* the #1750 guarantees: the password reaches the origin only and nothing else holds it; a
  credential-shaped document is withheld whole; a rebinding name connects only to the vetted
  address.
"""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import quote, unquote

import httpx
import pytest

from agent_sessions import connector_http, template_secrets
from agent_sessions.connectors import hq, kinds, manifest, service, store

PASSWORD = "Xk3pa-Wq9Rt-Lm2Zs-Yv8Nc-Bd4Fh"
ACCOUNT = "anna"
UID = "anna@example.com"  # what Nextcloud names: the DAV root uses this, not the login
ORIGIN = "https://cloud.example"
HOST_IP = "10.0.0.9"
CRED = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ROOT = f"/remote.php/dav/files/{UID}/"


def run(coro):
    return asyncio.run(coro)


def _response(
    href: str, *, folder=False, size=0, ctype="", modified="Sat, 01 Nov 2026 07:00:00 GMT"
):
    rt = "<d:collection/>" if folder else ""
    extra = (
        ""
        if folder
        else f"<d:getcontentlength>{size}</d:getcontentlength>"
        f"<d:getcontenttype>{ctype}</d:getcontenttype>"
    )
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype>{rt}"
        f"</d:resourcetype>{extra}<d:getlastmodified>{modified}</d:getlastmodified></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def _multistatus(*responses: str) -> bytes:
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        + "".join(responses)
        + "</d:multistatus>"
    ).encode()


def _href(rel: str) -> str:
    return quote(ROOT + rel)


class Nextcloud:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.account = ACCOUNT
        self.password = PASSWORD
        self.uid = UID
        self.files = {
            "notes.md": ("text/markdown", b"# Notes\nship it\n"),
            "Documents/plan.txt": ("text/plain", b"the plan\n"),
            "Documents/keys.txt": ("text/plain", CRED.encode()),
            "photo.jpg": ("image/jpeg", b"\xff\xd8\xff"),
            "big.log": ("text/plain", b"x" * (300 * 1024)),
        }
        self.extra_root: list[str] = []  # hostile responses injected into the root listing
        self.search_extra: list[dict] = []  # hostile search results
        self.sizes: dict[str, str] = {}  # a stated size that differs from the bytes (malformed)
        self.paginated = False
        self.doctype = False

    def _auth_ok(self, request: httpx.Request) -> bool:
        want = "Basic " + base64.b64encode(f"{self.account}:{self.password}".encode()).decode()
        return request.headers.get("authorization") == want

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._auth_ok(request):
            return httpx.Response(401, json={"message": "unauthorized"})
        path = unquote(request.url.raw_path.decode().split("?", 1)[0])
        if path == "/ocs/v2.php/cloud/user":
            assert request.headers.get("ocs-apirequest") == "true"
            return httpx.Response(200, json={"ocs": {"data": {"id": self.uid}}})
        if path == "/ocs/v2.php/search/providers/files/search":
            term = request.url.params["term"]
            return httpx.Response(200, json=self.search_reply(term))
        if request.method == "PROPFIND":
            assert request.content.startswith(b'<?xml version="1.0" encoding="utf-8"?><d:propfind')
            depth = request.headers["depth"]
            body = self._propfind(path, depth)
            if body is None:
                return httpx.Response(404)
            if self.doctype:
                body = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + body[21:]
            return httpx.Response(207, content=body, headers={"content-type": "application/xml"})
        if request.method == "GET" and path.startswith(ROOT):
            rel = path[len(ROOT) :]
            if rel in self.files:
                return httpx.Response(200, content=self.files[rel][1])
        return httpx.Response(404)

    def search_reply(self, term: str) -> dict:
        """The shape Nextcloud's unified search answers for the files provider: a web link in
        `resourceUrl` (never used) and the file's path, relative to the user's files, in
        `attributes.path`."""
        entries = [
            {
                "thumbnailUrl": f"{ORIGIN}/core/preview?fileId=1",
                "title": name.rsplit("/", 1)[-1],
                "subline": "in Documents",
                "resourceUrl": f"{ORIGIN}/index.php/f/1",
                "icon": "icon-filetype-text",
                "rounded": False,
                "attributes": {"fileId": "1", "path": "/" + name},
            }
            for name in self.files
            if term in name
        ]
        entries.append(
            {"title": "Documents", "icon": "icon-folder", "resourceUrl": f"{ORIGIN}/index.php/f/2",
             "attributes": {"fileId": "2", "path": "/Documents"}}
        )  # fmt: skip
        entries += self.search_extra
        return {
            "ocs": {
                "meta": {"status": "ok"},
                "data": {"name": "Files", "isPaginated": self.paginated,
                         "entries": entries, "cursor": 20 if self.paginated else None},
            }
        }  # fmt: skip

    def _propfind(self, path: str, depth: str) -> bytes | None:
        if not path.startswith(ROOT):
            return None
        rel = path[len(ROOT) :].rstrip("/")
        if depth == "0":
            if rel not in self.files:
                return (
                    _multistatus(_response(_href(rel + "/"), folder=True))
                    if rel == "Documents"
                    else None
                )
            ctype, data = self.files[rel]
            size = self.sizes.get(rel, len(data))
            return _multistatus(_response(_href(rel), size=size, ctype=ctype))
        children = {f for f in self.files if (f.rsplit("/", 1)[0] if "/" in f else "") == rel}
        folders = {"Documents"} if rel == "" else set()
        out = [_response(_href(rel + "/" if rel else ""), folder=True)]
        out += [_response(_href(f + "/"), folder=True) for f in sorted(folders)]
        out += [
            _response(
                _href(f), size=self.sizes.get(f, len(self.files[f][1])), ctype=self.files[f][0]
            )
            for f in sorted(children)
        ]
        if rel == "":
            out += self.extra_root
        return _multistatus(*out)


@pytest.fixture
def nc(monkeypatch):
    f = Nextcloud()
    lookups: list[str] = []

    async def fake_resolve(host, port):
        lookups.append(host)
        if host != "cloud.example":
            raise OSError("no such host")
        return [HOST_IP]

    monkeypatch.setattr(connector_http, "_resolve", fake_resolve)
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(f.handler))
    f.lookups = lookups  # type: ignore[attr-defined]
    return f


def _configured() -> dict:
    inst = store.add("nextcloud")
    return store.patch(inst["id"], {"origin": ORIGIN, "account": ACCOUNT, "token": PASSWORD})


def _ready() -> dict:
    inst = _configured()
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "ok", out
    return store.set_capability(inst["id"], True)


def _bound():
    inst = store.read()["instances"][0]
    b = service._bound(inst)
    return connector_http.Bound(
        b.instance_id, b.generation, b.origin, b.auth_scheme, b.label, adapter=b.adapter,
        principal=(inst["tested"] or {}).get("login", ""), for_read=False,
    )  # fmt: skip


def _paths(nc) -> list[tuple[str, str]]:
    return [(r.method, unquote(r.url.raw_path.decode().split("?", 1)[0])) for r in nc.requests]


# ---- the manifest and the store ----------------------------------------------------------------


def test_the_bundled_nextcloud_manifest_is_storage_with_basic_auth():
    m = manifest.bundled()["nextcloud"]
    assert (m.adapter, m.category, m.auth_kind, m.auth_scheme) == (
        "nextcloud", "storage", "basic", "Basic",
    )  # fmt: skip
    assert set(m.reads) == kinds.STORAGE_READS and m.origin_mode == "operator"


def test_a_scheme_and_a_category_must_match_their_kinds():
    text = "\n".join(
        [
            "contract = 1",
            "[identity]",
            'id = "x"',
            'label = "X"',
            'publisher = "battlelab"',
            'version = "1"',
            'category = "versioning"',
            "[adapter]",
            'kind = "nextcloud"',
            "[origin]",
            'mode = "operator"',
            "[auth]",
            'kind = "basic"',
            'scheme = "Basic"',
            'scopes = ["app-password"]',
            "[capabilities]",
            'read = ["folders"]',
            "",
        ]
    )
    with pytest.raises(manifest.ManifestError, match="category"):
        manifest.parse(text.encode())
    storage = text.replace('"versioning"', '"storage"')
    with pytest.raises(manifest.ManifestError, match="scheme"):
        manifest.parse(storage.replace('scheme = "Basic"', 'scheme = "Bearer"').encode())


def test_only_a_basic_connector_has_an_account_and_an_old_store_still_reads():
    forge = store.add("forgejo")
    assert "account" not in store.read()["instances"][0]
    with pytest.raises(store.Refused):
        store.patch(forge["id"], {"account": "anna"})
    cloud = store.add("nextcloud")
    assert service.catalog()["instances"][1]["account"] == ""
    assert cloud["id"]


def test_a_new_account_needs_the_password_again_inside_the_lock(nc):
    inst = _ready()
    with pytest.raises(store.Refused) as e:
        store.patch(inst["id"], {"account": "bob"})
    assert e.value.code == "account_needs_token"
    assert store.read()["instances"][0]["account"] == ACCOUNT  # unchanged
    changed = store.patch(inst["id"], {"account": "bob", "token": "Other-App-Pass-0987654"})
    assert changed["account"] == "bob" and changed["tested"] is None
    with pytest.raises(store.Refused) as e:
        store.patch(inst["id"], {"account": "bad:name"})
    assert e.value.code == "invalid_account"


def test_a_password_needs_an_account():
    inst = store.add("nextcloud")
    with pytest.raises(store.Refused) as e:
        store.patch(inst["id"], {"origin": ORIGIN, "token": PASSWORD})
    assert e.value.code == "needs_account"


# ---- the transport -------------------------------------------------------------------------------


def test_only_reading_methods_on_the_kinds_paths_and_refused_before_the_secret(nc, monkeypatch):
    _configured()
    b = _bound()
    fetched: list[str] = []
    real = connector_http._token
    monkeypatch.setattr(connector_http, "_token", lambda bound: fetched.append("x") or real(bound))
    for method, path, kw in (
        ("PUT", ROOT + "x.txt", {}),
        ("DELETE", ROOT + "notes.md", {}),
        ("MKCOL", ROOT + "new/", {}),
        ("PROPPATCH", ROOT, {}),
        ("PROPFIND", ROOT, {"propfind": "anything", "depth": "1"}),
        ("PROPFIND", ROOT, {"propfind": "entries", "depth": "infinity"}),
        ("GET", ROOT, {"propfind": "entries"}),
        ("GET", "/index.php/apps/files", {}),
        ("GET", "/ocs/v2.php/apps/files_sharing/api/v1/shares", {}),
        ("GET", "/remote.php/dav/files/../admin/", {}),
    ):
        with pytest.raises(connector_http.ConnectorError) as e:
            run(connector_http._request(b, method, quote(path), **kw))
        assert e.value.code == "refused", (method, path)
    assert fetched == [] and nc.requests == []


def test_basic_auth_is_built_by_the_transport_and_goes_to_the_origin_only(nc):
    _ready()
    want = "Basic " + base64.b64encode(f"{ACCOUNT}:{PASSWORD}".encode()).decode()
    assert {r.headers["authorization"] for r in nc.requests} == {want}
    assert {r.headers["host"] for r in nc.requests} == {"cloud.example"}
    assert all(r.url.host == HOST_IP for r in nc.requests)


def test_a_doctype_in_a_reply_is_refused(nc):
    _ready()
    nc.doctype = True
    with pytest.raises(connector_http.ConnectorError) as e:
        run(
            connector_http._request(
                _bound(), "PROPFIND", quote(ROOT), propfind="entries", depth="1", reply="xml"
            )
        )
    assert e.value.code == "bad_reply"


# ---- the probe and the adapter -------------------------------------------------------------------


def test_the_probe_names_the_user_id_and_paths_use_it_not_the_login(nc):
    inst = _configured()
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "ok" and out["login"] == UID and out["repos"] == 4
    dav = [p for m, p in _paths(nc) if m == "PROPFIND"]
    assert dav == [ROOT]  # the id, never /files/anna/


def test_an_unusable_user_id_fails_the_test(nc):
    nc.uid = "a/b"
    inst = _configured()
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "failed"


def test_hrefs_outside_the_account_root_or_climbing_out_are_dropped(nc):
    nc.extra_root = [
        _response("/remote.php/dav/files/someone-else/secret.txt", size=1, ctype="text/plain"),
        _response(quote(ROOT + "Documents/../../../etc/passwd"), size=1, ctype="text/plain"),
        _response("https://evil.example" + quote(ROOT) + "x.txt", size=1, ctype="text/plain"),
    ]
    _ready()
    turn = hq.TurnConnectors()
    listing = run(hq.list_connectors(turn))[0]
    root = listing["connectors"][0]["root"]
    out = run(hq.list_dir(turn, {"connector_index": 0, "item_ref": root}))[0]
    names = {e["name"] for e in out["untrusted_connector_content"]["entries"]}
    # The third is a full URL on another host whose PATH is under the root: the path rule keeps it
    # as a name only, and it is fetched from THIS origin if read, never from that host.
    assert "secret.txt" not in names and "passwd" not in names
    assert {"notes.md", "Documents", "photo.jpg", "big.log"} <= names


def test_hrefs_are_decoded_before_the_root_check(nc):
    """#1829 (Hermes): an encoded climb (`%2e%2e`, `%2F`) is decoded first, then checked against
    the account root and `valid_path`: it can never pass as an ordinary name."""
    nc.extra_root = [
        _response(
            ROOT.replace("@", "%40") + "%2e%2e/%2e%2e/admin/x.txt", size=1, ctype="text/plain"
        ),
        _response(
            ROOT.replace("@", "%40") + "Documents%2F..%2F..%2Fadmin.txt", size=1, ctype="text/plain"
        ),
        _response(
            "/remote.php/dav/files/anna/x.txt", size=1, ctype="text/plain"
        ),  # the login, not the id
    ]
    _ready()
    turn = hq.TurnConnectors()
    run(hq.list_connectors(turn))
    out = run(hq.list_dir(turn, {"connector_index": 0, "item_ref": "folder:"}))[0]
    names = {e["name"] for e in out["untrusted_connector_content"]["entries"]}
    assert names == {"notes.md", "Documents", "photo.jpg", "big.log"}


def test_binary_non_text_and_oversize_documents_are_refused_before_the_get(nc):
    _ready()
    b = _bound()
    from agent_sessions.connectors.adapters import nextcloud

    for name, code in (("photo.jpg", "binary"), ("big.log", "too_large")):
        before = len(nc.requests)
        with pytest.raises(connector_http.ConnectorError) as e:
            run(nextcloud.read_file(b, name))
        assert e.value.code == code
        assert [r.method for r in nc.requests[before:]] == ["PROPFIND"]  # never fetched


# ---- HQ's storage tools --------------------------------------------------------------------------


def test_hq_lists_searches_and_reads_the_commanders_files(nc):
    _ready()
    turn = hq.TurnConnectors()
    listing = run(hq.list_connectors(turn))[0]
    row = listing["connectors"][0]
    assert row["category"] == "storage" and row["search_kinds"] == ["files"]
    root = run(hq.list_dir(turn, {"connector_index": 0, "item_ref": row["root"]}))[0]
    refs = {e["name"]: e["ref"] for e in root["untrusted_connector_content"]["entries"]}
    assert refs["Documents"] == "folder:Documents/" and refs["notes.md"] == "doc:notes.md"
    docs = run(hq.list_dir(turn, {"connector_index": 0, "item_ref": "folder:Documents/"}))[0]
    assert {e["name"] for e in docs["untrusted_connector_content"]["entries"]} == {
        "plan.txt",
        "keys.txt",
    }
    note = run(hq.read(turn, {"connector_index": 0, "item_ref": "doc:notes.md"}))[0]
    body = note["untrusted_connector_content"]
    assert body["text"] == "# Notes\nship it\n" and body["content_type"] == "text/markdown"
    assert "cloud.example" in note["provenance"]
    found = run(hq.search(turn, {"connector_index": 0, "kind": "files", "query": "plan"}))[0]
    items = found["untrusted_connector_content"]["items"]
    assert {i["ref"] for i in items} == {"doc:Documents/plan.txt", "folder:Documents/"}
    assert "note" not in found["untrusted_connector_content"]


def test_a_ref_not_minted_this_turn_is_refused_and_nothing_is_read(nc):
    _ready()
    turn = hq.TurnConnectors()
    run(hq.list_connectors(turn))
    before = len(nc.requests)
    with pytest.raises(hq.Refused):
        run(hq.read(turn, {"connector_index": 0, "item_ref": "doc:Documents/keys.txt"}))
    assert len(nc.requests) == before


def test_kinds_are_checked_against_the_connectors_category(nc, monkeypatch):
    _ready()
    turn = hq.TurnConnectors()
    run(hq.list_connectors(turn))
    with pytest.raises(hq.Refused, match="files"):
        run(hq.search(turn, {"connector_index": 0, "kind": "repos", "query": "app"}))


def test_a_credential_shaped_document_is_withheld_whole(nc):
    _ready()
    turn = hq.TurnConnectors()
    run(hq.list_connectors(turn))
    run(hq.list_dir(turn, {"connector_index": 0, "item_ref": "folder:"}))
    run(hq.list_dir(turn, {"connector_index": 0, "item_ref": "folder:Documents/"}))
    out = run(hq.read(turn, {"connector_index": 0, "item_ref": "doc:Documents/keys.txt"}))[0]
    assert CRED not in json.dumps(out)


# ---- the #1750 guarantees ------------------------------------------------------------------------


def test_the_password_is_held_by_nothing_but_the_request(nc):
    _ready()
    turn = hq.TurnConnectors()
    outs = [
        run(hq.list_connectors(turn))[0],
        run(hq.list_dir(turn, {"connector_index": 0, "item_ref": "folder:"}))[0],
    ]
    assert PASSWORD not in json.dumps(outs)
    assert PASSWORD not in json.dumps(service.catalog())
    assert PASSWORD.encode() not in store.store_path().read_bytes()
    assert PASSWORD in template_secrets.redaction_values()


def test_a_rebinding_name_connects_only_to_the_vetted_address(nc, monkeypatch):
    answers = iter([[HOST_IP], ["169.254.169.254"], ["169.254.169.254"]])
    lookups: list[str] = []

    async def rebinding(host, port):
        lookups.append(host)
        return next(answers)

    _configured()
    monkeypatch.setattr(connector_http, "_resolve", rebinding)
    run(connector_http._request(_bound(), "GET", "/ocs/v2.php/cloud/user", {"format": "json"}))
    rq = nc.requests[-1]
    assert rq.url.host == HOST_IP and rq.headers["host"] == "cloud.example"
    assert lookups == ["cloud.example"]


def test_search_results_mint_only_paths_under_the_account_root(nc):
    """#1829 (Hermes): a result's path must be one `valid_path` accepts; its web link is never a
    path. Climbing, dot, empty, missing and absolute-URL results are dropped before any ref."""
    nc.search_extra = [
        {"title": "up", "attributes": {"path": "/../../etc/passwd"}},
        {"title": "dot", "attributes": {"path": "/Documents/./plan.txt"}},
        {"title": "empty", "attributes": {"path": "/"}},
        {"title": "no path", "resourceUrl": f"{ORIGIN}/index.php/f/9", "attributes": {}},
        {"title": "control", "attributes": {"path": "/a\x00b.txt"}},
        {"title": "encoded", "attributes": {"path": "/a%2Fb.txt"}},
    ]
    _ready()
    turn = hq.TurnConnectors()
    run(hq.list_connectors(turn))
    found = run(hq.search(turn, {"connector_index": 0, "kind": "files", "query": "plan"}))[0]
    refs = {i["ref"] for i in found["untrusted_connector_content"]["items"]}
    # A literal "%2F" is a name, not a separator: it stays one segment, encoded as such.
    assert refs == {"doc:Documents/plan.txt", "folder:Documents/", "doc:a%2Fb.txt"}
    from agent_sessions.connectors.adapters import nextcloud

    assert nextcloud._dav_path(UID, "a%2Fb.txt", folder=False).endswith("/a%252Fb.txt")


def test_a_cut_listing_or_search_says_so(nc, monkeypatch):
    from agent_sessions.connectors.adapters import common, nextcloud

    monkeypatch.setattr(nextcloud, "DIR_MAX", 2)
    nc.paginated = True
    _ready()
    turn = hq.TurnConnectors()
    run(hq.list_connectors(turn))
    root = run(hq.list_dir(turn, {"connector_index": 0, "item_ref": "folder:"}))[0]
    assert "more entries" in root["untrusted_connector_content"]["note"]
    assert len(root["untrusted_connector_content"]["entries"]) == 2
    found = run(hq.search(turn, {"connector_index": 0, "kind": "files", "query": "plan"}))[0]
    assert "first results" in found["untrusted_connector_content"]["note"]
    assert common.DIR_MAX == 200  # only the adapter's own cap was lowered


@pytest.mark.parametrize("stated", ["9" * 5000, "12abc", "-1", "\u0661\u0662"])
def test_a_malformed_size_is_a_connector_error_never_an_escape(nc, stated):
    """#1834 (Hermes): `int()` refuses 4300+ digits with a bare ValueError; a stated size must be a
    bounded ASCII decimal or the reply is `bad_reply`, for a listing and for a Depth:0 read."""
    _ready()
    nc.sizes = {"notes.md": stated}
    turn = hq.TurnConnectors()
    run(hq.list_connectors(turn))
    with pytest.raises(hq.Refused) as e:
        run(hq.list_dir(turn, {"connector_index": 0, "item_ref": "folder:"}))
    assert e.value.code == "connector_bad_reply"
    from agent_sessions.connectors.adapters import nextcloud

    with pytest.raises(connector_http.ConnectorError) as e2:
        run(nextcloud.read_file(_bound(), "notes.md"))
    assert e2.value.code == "bad_reply"


def test_a_search_cut_to_fit_the_result_budget_says_so(nc):
    """#1834 (Hermes): Nextcloud said the page was complete, but long valid paths overflow HQ's
    result budget; the entries dropped to fit must be announced."""
    deep = "/".join(["d" * 60] * 12)
    nc.search_extra = [
        {"title": f"f{i}", "attributes": {"path": f"/{deep}/plan-{i:02d}.txt"}} for i in range(14)
    ]
    _ready()
    turn = hq.TurnConnectors()
    run(hq.list_connectors(turn))
    out = run(hq.search(turn, {"connector_index": 0, "kind": "files", "query": "plan"}))[0]
    body = out["untrusted_connector_content"]
    # 16 results, fewer than a full page: Nextcloud's reply alone says nothing was cut.
    assert len(body["items"]) < 16
    assert "first results" in body["note"]


@pytest.mark.parametrize(
    "reply",
    [{"ocs": ["unexpected"]}, {"ocs": {"data": "x"}}, {"ocs": None}, ["not", "an", "object"], {}],
)
def test_a_malformed_ocs_envelope_is_bad_reply_for_probe_and_search(nc, monkeypatch, reply):
    """#1834 (Hermes): every level of an OCS reply is type-checked before it is read; a malformed
    one is `bad_reply`, never an AttributeError and never an apparently empty search."""
    from agent_sessions.connectors.adapters import nextcloud

    _ready()
    real = nc.handler

    def broken(request):
        if "/ocs/" in request.url.path:
            nc.requests.append(request)
            return httpx.Response(200, json=reply)
        return real(request)

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(broken))
    b = _bound()
    for call in (nextcloud.probe(b), nextcloud.search(b, "plan")):
        with pytest.raises(connector_http.ConnectorError) as e:
            run(call)
        assert e.value.code == "bad_reply"


def test_a_search_reply_without_entries_is_bad_reply_not_an_empty_page(nc, monkeypatch):
    from agent_sessions.connectors.adapters import nextcloud

    _ready()
    real = nc.handler
    monkeypatch.setattr(
        connector_http,
        "_TRANSPORT",
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ocs": {"data": {"name": "Files"}}})
            if r.url.path.endswith("/files/search")
            else real(r)
        ),
    )
    with pytest.raises(connector_http.ConnectorError) as e:
        run(nextcloud.search(_bound(), "plan"))
    assert e.value.code == "bad_reply"


@pytest.mark.parametrize(
    "raw",
    [
        # UTF-16 with a byte-order mark: the ASCII bytes of `<!ENTITY` never appear on the wire.
        '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'.encode(
            "utf-16"
        ),
        # UTF-16LE without a mark.
        '<?xml version="1.0" encoding="utf-16le"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>'.encode(
            "utf-16-le"
        ),
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
        b'<?xml version="1.0"?><!doctype x><x/>',
        b'<?xml version="1.0" encoding="iso-8859-1"?><x/>',
        "<?xml version='1.0' encoding='utf-8'?><x>caf\u00e9</x>".encode("latin-1"),  # not UTF-8
    ],
)
def test_xml_is_utf8_only_and_no_declaration_reaches_the_parser(raw):
    """#1834 (Hermes): a UTF-16 document hid its `<!ENTITY>` from a byte scan and was expanded.
    Replies must be UTF-8 without any declaration; the parser itself refuses one too."""
    with pytest.raises(connector_http.ConnectorError) as e:
        connector_http._parse_xml(raw, "cloud.example")
    assert e.value.code == "bad_reply"


def test_plain_utf8_xml_still_parses():
    raw = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:"><d:x>é</d:x></d:multistatus>'
    )
    assert connector_http._parse_xml(raw.encode(), "h").tag == "{DAV:}multistatus"
