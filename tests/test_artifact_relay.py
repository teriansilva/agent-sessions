"""A shared artifact hosted on the BattleLab relay, for a Home Free box (#2190).

The rules pinned here:
- in ``AUTH_MODE=none`` a share uploads the latest revision through the local Home Free agent and
  the public link is the relay's; sharing again does not upload again;
- revoke and delete remove the relay copy FIRST, and keep the record (with the reason) when the
  relay can't confirm it, so a public copy is never orphaned;
- a republish of a relay-shared artifact replaces the copy under the same id;
- **a private artifact never reaches the agent socket**: not on publish, not in a mode with a
  login, not on anything but share / revoke / delete / republish-of-a-shared-artifact;
- the agent's link sends only what the relay asks for, each blob checked against the manifest,
  and only to the relay's own upload path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import threading
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    agent_tools,
    artifact_broker,
    artifact_relay,
    artifact_store,
    files,
    prefs,
    secrets_broker,
)
from agent_sessions.homefree import peerpipe
from agent_sessions.homefree.artifactlink import ArtifactLink, LinkError, upload_base

SESSION = "claude:aaaaaaaa-0000-4000-8000-000000000001"
MANIFEST = types.SimpleNamespace(tools_mcp=agent_tools.CLAUDE_FLAG)
RELAY_ID = "a" * 26
RELAY_URL = f"https://{RELAY_ID}.artifacts.example.test/"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PROJECTS", str(tmp_path / "projects.json"))
    files.reset_capabilities_for_test()
    prefs.set_artifacts_enabled(True)
    yield
    artifact_store.PUBLIC_AVAILABLE = True


@pytest.fixture
def proj(tmp_path) -> Path:
    p = tmp_path / "home" / "proj"
    (p / "dist" / "docs").mkdir(parents=True)
    (p / "dist" / "index.html").write_text("<h1>hi</h1>")
    (p / "dist" / "docs" / "index.html").write_text("docs")
    return p


@pytest.fixture
def origin(proj) -> artifact_store.Origin:
    return artifact_store.Origin(
        engine="claude", session_key=SESSION, phys_key=SESSION, cwd=str(proj)
    )


def _publish(origin) -> dict:
    return artifact_store.publish(origin, {"path": "dist", "title": "Hi", "why": "w"})


class FakeAgent:
    """The Home Free agent's peer socket, answering artifact ops; records every request."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="relayart-")  # short: AF_UNIX paths are ~108 bytes
        self.path = str(Path(self.dir) / peerpipe.SOCKET_NAME)
        self.requests: list[dict] = []
        self.put_delay = 0.0  # seconds before answering an artifact_put (a slow upload)
        self.answers: dict[str, dict] = {
            "artifact_put": {"ok": True, "public_id": RELAY_ID, "url": RELAY_URL},
            "artifact_delete": {"ok": True},
        }
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def client(reader, writer):
            raw = await peerpipe.read_frame(reader, peerpipe.ARTIFACT_REQUEST_MAX)
            req = json.loads(raw)
            self.requests.append(req)
            if req["op"] == "artifact_put" and self.put_delay:
                await asyncio.sleep(self.put_delay)
            peerpipe.write_frame(writer, json.dumps(self.answers[req["op"]]).encode())
            await writer.drain()
            writer.close()

        async def main():
            self._server = await asyncio.start_unix_server(client, path=self.path)
            self._ready.set()
            await asyncio.Event().wait()

        self._task = self._loop.create_task(main())
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._task.cancel)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def agent(monkeypatch):
    fake = FakeAgent()
    monkeypatch.setenv("AGENT_SESSIONS_HOMEFREE_PEER_SOCKET", fake.path)
    yield fake
    fake.close()


@pytest.fixture
def none_client(tmp_path, monkeypatch, password_hash):
    from agent_sessions.auth import AuthConfig
    from agent_sessions.main import create_app

    monkeypatch.setenv("AGENT_SESSIONS_USERNAME", "marcus")
    monkeypatch.setenv("AGENT_SESSIONS_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    cfg = AuthConfig.from_env()
    c = TestClient(create_app(cfg), base_url="https://your-domain.example")
    headers = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}
    return c, headers


# --- the app, over a real local socket ----------------------------------------------------------


def test_a_home_free_share_hosts_the_latest_revision_on_the_relay(none_client, agent, origin):
    c, h = none_client
    a = _publish(origin)
    r = c.post(f"/api/artifacts/{a['id']}/share", headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["public_url"] == RELAY_URL and out["hosted_on"] == "relay"
    assert out["visibility"] == "public"
    [put] = agent.requests
    assert put["op"] == "artifact_put" and put["public_id"] is None and put["kind"] == "site"
    assert set(put["manifest"]) == {"index.html", "docs/index.html"}
    # Every blob the manifest names is a real file of the box's own store, with that hash.
    for meta in put["manifest"].values():
        blob = Path(put["blobs"][meta["sha256"]])
        assert hashlib.sha256(blob.read_bytes()).hexdigest() == meta["sha256"]
    # Sharing again is the same answer, with no second upload.
    again = c.post(f"/api/artifacts/{a['id']}/share", headers=h).json()
    assert again["public_url"] == RELAY_URL and len(agent.requests) == 1


def test_revoke_deletes_the_relay_copy_then_clears_it(none_client, agent, origin):
    c, h = none_client
    a = _publish(origin)
    c.post(f"/api/artifacts/{a['id']}/share", headers=h)
    r = c.delete(f"/api/artifacts/{a['id']}/share", headers=h)
    assert r.status_code == 200 and r.json()["visibility"] == "private"
    assert r.json()["public_url"] is None
    assert agent.requests[-1] == {"op": "artifact_delete", "public_id": RELAY_ID, "ref": None}


def test_a_revoke_the_relay_cannot_confirm_keeps_the_copy_visible(none_client, agent, origin):
    c, h = none_client
    a = _publish(origin)
    c.post(f"/api/artifacts/{a['id']}/share", headers=h)
    agent.answers["artifact_delete"] = {"ok": False, "code": "no_relay"}
    r = c.delete(f"/api/artifacts/{a['id']}/share", headers=h)
    assert r.status_code == 502 and r.json()["detail"] == "relay_failed"
    assert "not connected" in r.json()["reason"]
    assert c.get(f"/api/artifacts/{a['id']}").json()["public_url"] == RELAY_URL
    # Deleting the artifact is held back the same way: nothing left behind to revoke it.
    assert c.delete(f"/api/artifacts/{a['id']}", headers=h).status_code == 502
    assert artifact_store.get(a["id"]) is not None
    # A copy the relay no longer knows is gone either way: the record clears.
    agent.answers["artifact_delete"] = {"ok": False, "code": "not_found"}
    assert c.delete(f"/api/artifacts/{a['id']}", headers=h).status_code == 200
    assert artifact_store.get(a["id"]) is None


def test_without_home_free_the_answer_is_the_none_mode_reason(none_client, origin):
    c, h = none_client
    a = _publish(origin)
    r = c.post(f"/api/artifacts/{a['id']}/share", headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "public_off"
    assert "AUTH_MODE=none" in r.json()["reason"]


def test_a_relay_that_does_not_host_yet_says_so(none_client, agent, origin):
    c, h = none_client
    a = _publish(origin)
    agent.answers["artifact_put"] = {"ok": False, "code": "artifacts_unavailable"}
    r = c.post(f"/api/artifacts/{a['id']}/share", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "artifacts_unavailable"
    assert "does not host public artifacts yet" in r.json()["reason"]
    assert c.get(f"/api/artifacts/{a['id']}").json()["visibility"] == "private"


# --- the agent's tool, and republish --------------------------------------------------------------


@pytest.fixture
def broker(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(artifact_broker, "_server_running", True)
    return runtime


def _public_token(proj) -> str:
    artifact_store.set_activation("session", SESSION, "public")
    env = artifact_broker.launch_env(
        SESSION, SESSION, str(proj), engine="claude", manifest=MANIFEST
    )
    return Path(env[artifact_broker.ENV_TOKEN_FILE]).read_text()


def test_the_tools_share_goes_to_the_relay_only_after_the_brokers_checks(broker, agent, proj):
    token = _public_token(proj)
    artifact_store.PUBLIC_AVAILABLE = False
    pub = artifact_broker.handle(token, "publish", {"path": "dist", "title": "Hi", "why": "w"})
    aid = pub["artifact"]["id"]
    assert agent.requests == []  # a publish alone uploads nothing
    req = {"op": "share", "args": {"artifact_id": aid}}
    first = artifact_broker.handle(token, "share", req["args"])
    # The contract the follow-up relies on: public_off comes only after mode and ownership passed.
    assert first == {"ok": False, "error": "public_off"}
    out = asyncio.run(artifact_broker._relay_followup(req, first))
    assert out["ok"] is True and out["artifact"]["public_url"] == RELAY_URL
    assert [r["op"] for r in agent.requests] == ["artifact_put"]


def test_a_republish_replaces_the_relay_copy_under_the_same_id(broker, agent, proj):
    token = _public_token(proj)
    artifact_store.PUBLIC_AVAILABLE = False
    args = {"path": "dist", "title": "Hi", "why": "w"}
    aid = artifact_broker.handle(token, "publish", args)["artifact"]["id"]
    asyncio.run(artifact_relay.share(aid))
    (proj / "dist" / "index.html").write_text("<h1>v2</h1>")
    reply = artifact_broker.handle(token, "publish", {**args, "artifact_id": aid})

    async def publish_then_wait():
        await artifact_broker._relay_followup({"op": "publish", "args": args}, reply)
        await asyncio.gather(*artifact_broker._BACKGROUND)

    asyncio.run(publish_then_wait())
    put = agent.requests[-1]
    assert put["op"] == "artifact_put" and put["public_id"] == RELAY_ID
    v2 = hashlib.sha256(b"<h1>v2</h1>").hexdigest()
    assert put["manifest"]["index.html"]["sha256"] == v2


def test_a_private_artifact_never_reaches_the_agent_socket(broker, agent, proj, origin):
    """The never-transits regression: only share / revoke / delete / republish-of-a-shared
    artifact may talk to the relay. A private artifact's whole life does not."""
    artifact_store.set_activation("session", SESSION, "private")
    env = artifact_broker.launch_env(
        SESSION, SESSION, str(proj), engine="claude", manifest=MANIFEST
    )
    token = Path(env[artifact_broker.ENV_TOKEN_FILE]).read_text()
    artifact_store.PUBLIC_AVAILABLE = False
    args = {"path": "dist", "title": "Hi", "why": "w"}
    reply = artifact_broker.handle(token, "publish", args)
    aid = reply["artifact"]["id"]

    async def life():
        await artifact_broker._relay_followup({"op": "publish", "args": args}, reply)
        await asyncio.gather(*artifact_broker._BACKGROUND)
        refused = artifact_broker.handle(token, "share", {"artifact_id": aid})
        assert refused == {"ok": False, "error": "not_public"}
        await artifact_broker._relay_followup(
            {"op": "share", "args": {"artifact_id": aid}}, refused
        )
        await artifact_relay.republish(aid)  # not shared: nothing to replace
        assert await artifact_relay.revoke(aid) is None

    asyncio.run(life())
    artifact_store.delete(aid)
    assert agent.requests == []


def test_with_a_login_sharing_never_touches_the_relay(agent, origin):
    a = _publish(origin)
    shared = artifact_store.share(a["id"])
    assert shared["hosted_on"] == "box" and "/artifacts/p/" in shared["public_url"]
    assert agent.requests == []


# --- the agent's link ---------------------------------------------------------------------------


class FakeWS:
    """The agent's control socket: answers each request the way the relay does."""

    def __init__(self, link: ArtifactLink, replies: dict[str, dict]) -> None:
        self.link, self.replies, self.sent = link, replies, []

    async def send(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        reply = dict(self.replies[msg["t"]], rid=msg["rid"])
        asyncio.get_running_loop().call_soon(self.link.on_message, reply)


def _blobs(tmp_path, files_: dict[str, bytes]):
    manifest, blobs = {}, {}
    for name, body in files_.items():
        sha = hashlib.sha256(body).hexdigest()
        p = tmp_path / sha
        p.write_bytes(body)
        manifest[name] = {"sha256": sha, "size": len(body), "type": "text/html"}
        blobs[sha] = str(p)
    return manifest, blobs


def _link(replies, handler):
    link = ArtifactLink("wss://relay.example.test/relay/ws", transport=httpx.MockTransport(handler))
    ws = FakeWS(link, replies)
    link.attach(ws)
    return link, ws


def test_the_link_uploads_what_the_relay_asks_and_commits(tmp_path):
    manifest, blobs = _blobs(tmp_path, {"index.html": b"<h1>hi</h1>", "a.html": b"a"})
    want = sorted(blobs)[:1]  # the relay already holds the other one
    puts = []

    def handler(request: httpx.Request) -> httpx.Response:
        puts.append(
            (
                request.method,
                str(request.url),
                request.headers.get("X-Artifact-Upload-Token"),
                request.content,
            )
        )
        return httpx.Response(204)

    replies = {
        "artifact_put": {
            "t": "artifact_upload",
            "public_id": RELAY_ID,
            "token": "tok",
            "need": want,
            "put": "/artifacts/upload/",
        },
        "artifact_commit": {"t": "artifact_ok", "public_id": RELAY_ID, "url": RELAY_URL},
    }
    link, ws = _link(replies, handler)
    out = asyncio.run(link.put(None, "site", manifest, blobs))
    assert out == {"ok": True, "public_id": RELAY_ID, "url": RELAY_URL}
    assert [m["t"] for m in ws.sent] == ["artifact_put", "artifact_commit"]
    assert puts == [
        (
            "PUT",
            f"https://relay.example.test/artifacts/upload/{want[0]}",
            "tok",  # the token travels as a header, never in the URL
            Path(blobs[want[0]]).read_bytes(),
        )
    ]


def test_the_link_refuses_what_it_must_not_send(tmp_path):
    manifest, blobs = _blobs(tmp_path, {"index.html": b"<h1>hi</h1>"})
    sha = next(iter(blobs))

    def never(request):  # pragma: no cover - reaching it is the failure
        raise AssertionError("uploaded")

    base = {"t": "artifact_upload", "public_id": RELAY_ID, "token": "t", "need": [sha]}
    # A relay that names any path but its own upload route gets nothing.
    link, _ = _link({"artifact_put": {**base, "put": "https://elsewhere.test/x/"}}, never)
    with pytest.raises(LinkError, match="refused"):
        asyncio.run(link.put(None, "site", manifest, blobs))
    # A blob whose bytes no longer match the manifest is never sent.
    Path(blobs[sha]).write_bytes(b"changed!!!!")
    link, _ = _link({"artifact_put": {**base, "put": "/artifacts/upload/"}}, never)
    with pytest.raises(LinkError, match="blob_mismatch"):
        asyncio.run(link.put(None, "site", manifest, blobs))
    # Malformed requests stop before the relay hears anything.
    with pytest.raises(LinkError, match="bad_request"):
        asyncio.run(link.put("not-a-relay-id", "site", manifest, blobs))
    with pytest.raises(LinkError, match="bad_request"):
        asyncio.run(link.put(None, "site", manifest, {}))


def test_relay_refusals_and_a_lost_connection_come_back_as_codes(tmp_path):
    manifest, blobs = _blobs(tmp_path, {"index.html": b"x"})
    link, _ = _link({"artifact_put": {"t": "artifact_error", "code": "quota_exceeded"}}, None)
    with pytest.raises(LinkError, match="quota_exceeded"):
        asyncio.run(link.put(None, "site", manifest, blobs))
    detached = ArtifactLink("wss://relay.example.test/relay/ws")
    with pytest.raises(LinkError, match="no_relay"):
        asyncio.run(detached.delete(RELAY_ID))

    async def lost():
        link = ArtifactLink("wss://relay.example.test/relay/ws")

        class Silent:
            async def send(self, text):
                pass

        link.attach(Silent())
        task = asyncio.create_task(link.delete(RELAY_ID))
        await asyncio.sleep(0)
        link.detach()  # the control connection dropped mid-request
        with pytest.raises(LinkError, match="no_relay"):
            await task

    asyncio.run(lost())


def test_the_upload_base_follows_the_relay_url():
    assert upload_base("wss://relay.example.test/relay/ws") == "https://relay.example.test"
    assert upload_base("ws://127.0.0.1:4300/relay/ws") == "http://127.0.0.1:4300"
    with pytest.raises(LinkError):
        upload_base("https://relay.example.test/relay/ws")


def test_the_peer_socket_validates_artifact_requests():
    class Link:
        async def put(self, *a):
            return {"ok": True, "public_id": RELAY_ID, "url": RELAY_URL}

        async def delete(self, pid, ref=None):
            return {"ok": True}

    def pipe(artifacts):
        return peerpipe.PeerPipe(
            relay_url="wss://r/relay/ws",
            console_name="viper-1",
            identity=None,
            identity_path="/tmp/x",
            app_host="127.0.0.1",
            app_port=None,
            artifacts=artifacts,
        )

    ok = pipe(Link())
    full = {
        "op": "artifact_put",
        "public_id": None,
        "ref": "ab" * 16,
        "kind": "site",
        "manifest": {},
        "blobs": {},
    }
    assert asyncio.run(ok._artifact(full))["ok"] is True
    assert asyncio.run(ok._artifact({**full, "extra": 1})) == {"ok": False, "code": "bad_request"}
    delete = {"op": "artifact_delete", "public_id": RELAY_ID, "ref": None}
    assert asyncio.run(ok._artifact(delete))["ok"]
    assert (
        asyncio.run(ok._artifact({"op": "artifact_delete", "public_id": RELAY_ID}))["code"]
        == "bad_request"
    )
    none = pipe(None)
    assert asyncio.run(none._artifact(full)) == {"ok": False, "code": "no_relay"}


# --- review 8519: one copy per artifact under concurrency, no orphan, retry, visibility -----------


def test_the_put_carries_the_artifacts_stable_ref(none_client, agent, origin):
    c, h = none_client
    a = _publish(origin)
    c.post(f"/api/artifacts/{a['id']}/share", headers=h)
    [put] = agent.requests
    assert put["ref"] == artifact_relay.ref_of(a["id"]) and a["id"] not in put["ref"]


def test_concurrent_first_shares_make_one_upload(agent, origin):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    agent.put_delay = 0.3

    async def both():
        return await asyncio.gather(artifact_relay.share(a["id"]), artifact_relay.share(a["id"]))

    first, second = asyncio.run(both())
    assert first["public_url"] == second["public_url"] == RELAY_URL
    assert [r["op"] for r in agent.requests] == ["artifact_put"]


def test_a_delete_during_a_first_share_leaves_no_public_copy(agent, origin):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    agent.put_delay = 0.3

    async def race():
        share = asyncio.create_task(artifact_relay.share(a["id"]))
        await asyncio.sleep(0.05)  # the upload is in flight
        deleted = await artifact_relay.delete_artifact(a["id"])
        return await share, deleted

    _, deleted = asyncio.run(race())
    assert deleted is True and artifact_store.get(a["id"]) is None
    # The delete waited for the share, then removed the copy it made.
    assert [r["op"] for r in agent.requests] == ["artifact_put", "artifact_delete"]
    assert agent.requests[1]["public_id"] == RELAY_ID


def test_a_copy_the_box_cannot_record_is_deleted_again(agent, origin, monkeypatch):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)

    def cannot_record(*args, **kwargs):
        raise artifact_store.ArtifactsUnavailable("disk full")

    monkeypatch.setattr(artifact_store, "set_relay", cannot_record)
    with pytest.raises(artifact_store.ArtifactsUnavailable):
        asyncio.run(artifact_relay.share(a["id"]))
    assert [r["op"] for r in agent.requests] == ["artifact_put", "artifact_delete"]


def test_a_failed_refresh_is_retried_by_the_next_share(broker, agent, proj):
    token = _public_token(proj)
    artifact_store.PUBLIC_AVAILABLE = False
    args = {"path": "dist", "title": "Hi", "why": "w"}
    aid = artifact_broker.handle(token, "publish", args)["artifact"]["id"]
    asyncio.run(artifact_relay.share(aid))
    (proj / "dist" / "index.html").write_text("<h1>v2</h1>")
    artifact_broker.handle(token, "publish", {**args, "artifact_id": aid})
    agent.answers["artifact_put"] = {"ok": False, "code": "no_relay"}  # the relay is down
    asyncio.run(artifact_relay.republish(aid))
    parts = artifact_store.relay_parts(aid)
    assert parts["relay_rev"] != parts["latest"]  # the copy is behind, and the box knows it
    agent.answers["artifact_put"] = {"ok": True, "public_id": RELAY_ID, "url": RELAY_URL}
    out = asyncio.run(artifact_relay.share(aid))  # the operator shares again
    assert out["public_url"] == RELAY_URL
    put = agent.requests[-1]
    assert put["op"] == "artifact_put" and put["public_id"] == RELAY_ID
    assert put["manifest"]["index.html"]["sha256"] == hashlib.sha256(b"<h1>v2</h1>").hexdigest()
    parts = artifact_store.relay_parts(aid)
    assert parts["relay_rev"] == parts["latest"]
    # Now up to date, sharing again uploads nothing.
    n = len(agent.requests)
    asyncio.run(artifact_relay.share(aid))
    assert len(agent.requests) == n


def test_a_relay_copy_counts_as_public_in_the_lists_and_usage(agent, origin):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    asyncio.run(artifact_relay.share(a["id"]))
    public = artifact_store.list_artifacts(visibility="public")
    private = artifact_store.list_artifacts(visibility="private")
    assert [x["id"] for x in public["items"]] == [a["id"]] and private["items"] == []
    assert artifact_store.usage()["public"] == 1
    asyncio.run(artifact_relay.revoke(a["id"]))
    assert artifact_store.list_artifacts(visibility="public")["items"] == []
    assert artifact_store.usage()["public"] == 0


# --- review 8525: an uncertain first upload never loses its revocation ---------------------------


def test_an_unacknowledged_first_upload_is_still_revoked_by_ref(agent, origin):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    # The relay committed, but the reply was lost on the way back.
    agent.answers["artifact_put"] = {"ok": False, "code": "no_relay"}
    with pytest.raises(artifact_relay.RelayRefused):
        asyncio.run(artifact_relay.share(a["id"]))
    shown = artifact_store.get(a["id"])
    assert shown["visibility"] == "public" and shown["relay_uncertain"] is True
    assert [x["id"] for x in artifact_store.list_artifacts(visibility="public")["items"]] == [
        a["id"]
    ]
    # A restart changes nothing: the intent is in the store, not in memory.
    assert artifact_store.relay_parts(a["id"])["relay_pending"] is True
    out = asyncio.run(artifact_relay.revoke(a["id"]))
    assert agent.requests[-1] == {
        "op": "artifact_delete",
        "public_id": None,
        "ref": artifact_relay.ref_of(a["id"]),
    }
    assert out["visibility"] == "private" and out["relay_uncertain"] is False


def test_an_unacknowledged_first_upload_blocks_a_delete_until_the_relay_confirms(agent, origin):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    agent.answers["artifact_put"] = {"ok": False, "code": "no_relay"}
    with pytest.raises(artifact_relay.RelayRefused):
        asyncio.run(artifact_relay.share(a["id"]))
    agent.answers["artifact_delete"] = {"ok": False, "code": "no_relay"}  # relay still down
    with pytest.raises(artifact_relay.RelayRefused):
        asyncio.run(artifact_relay.delete_artifact(a["id"]))
    assert artifact_store.get(a["id"]) is not None  # kept: the copy may still be online
    agent.answers["artifact_delete"] = {"ok": True}
    assert asyncio.run(artifact_relay.delete_artifact(a["id"])) is True
    assert agent.requests[-1]["ref"] == artifact_relay.ref_of(a["id"])


def test_a_failed_record_and_a_failed_cleanup_keep_the_intent(agent, origin, monkeypatch):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    real = artifact_store.set_relay

    def disk_full(*args, **kwargs):
        raise artifact_store.ArtifactsUnavailable("disk full")

    monkeypatch.setattr(artifact_store, "set_relay", disk_full)
    agent.answers["artifact_delete"] = {"ok": False, "code": "no_relay"}  # the cleanup fails too
    with pytest.raises(artifact_relay.RelayRefused):
        asyncio.run(artifact_relay.share(a["id"]))
    monkeypatch.setattr(artifact_store, "set_relay", real)  # the store recovers
    assert artifact_store.relay_parts(a["id"])["relay_pending"] is True
    agent.answers["artifact_delete"] = {"ok": True}
    assert asyncio.run(artifact_relay.delete_artifact(a["id"])) is True
    assert agent.requests[-1]["ref"] == artifact_relay.ref_of(a["id"])


def test_a_refusal_that_proves_no_copy_leaves_the_artifact_private(agent, origin):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    agent.answers["artifact_put"] = {"ok": False, "code": "quota_exceeded"}
    with pytest.raises(artifact_relay.RelayRefused):
        asyncio.run(artifact_relay.share(a["id"]))
    shown = artifact_store.get(a["id"])
    assert shown["visibility"] == "private" and shown["relay_uncertain"] is False


def test_a_refresh_whose_record_fails_keeps_the_shared_link(agent, origin, monkeypatch):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    asyncio.run(artifact_relay.share(a["id"]))
    n = len(agent.requests)

    def disk_full(*args, **kwargs):
        raise artifact_store.ArtifactsUnavailable("disk full")

    monkeypatch.setattr(
        artifact_store,
        "relay_parts",
        lambda aid: {
            "kind": "site",
            "manifest": {},
            "blobs": {},
            "relay_public_id": RELAY_ID,
            "relay_rev": 0,
            "relay_pending": False,
            "latest": 1,
        },
    )
    monkeypatch.setattr(artifact_store, "set_relay", disk_full)
    asyncio.run(artifact_relay.republish(a["id"]))  # logs, does not raise
    # A replacement whose record failed is never taken down: no delete was sent.
    assert [r["op"] for r in agent.requests[n:]] == ["artifact_put"]


# --- review 8534: a refused retry never erases an earlier attempt's uncertainty ------------------


def test_a_refused_retry_keeps_an_earlier_unacknowledged_copy_revocable(agent, origin):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    agent.answers["artifact_put"] = {"ok": False, "code": "no_relay"}  # committed, reply lost
    with pytest.raises(artifact_relay.RelayRefused):
        asyncio.run(artifact_relay.share(a["id"]))
    agent.answers["artifact_put"] = {"ok": False, "code": "quota_exceeded"}  # the retry is refused
    with pytest.raises(artifact_relay.RelayRefused):
        asyncio.run(artifact_relay.share(a["id"]))
    # The earlier copy may still be online: still public / unconfirmed, still revocable.
    shown = artifact_store.get(a["id"])
    assert shown["visibility"] == "public" and shown["relay_uncertain"] is True
    # Reopen the store ("a restart"): the intent is on disk.
    artifact_store._ready_for = None
    assert artifact_store.relay_parts(a["id"])["relay_pending"] is True
    n = len(agent.requests)
    assert asyncio.run(artifact_relay.delete_artifact(a["id"])) is True
    assert agent.requests[n:] == [
        {"op": "artifact_delete", "public_id": None, "ref": artifact_relay.ref_of(a["id"])}
    ]


def test_a_fresh_refused_share_still_returns_to_private(agent, origin):
    artifact_store.PUBLIC_AVAILABLE = False
    a = _publish(origin)
    agent.answers["artifact_put"] = {"ok": False, "code": "rate_limited"}
    with pytest.raises(artifact_relay.RelayRefused):
        asyncio.run(artifact_relay.share(a["id"]))
    assert artifact_store.get(a["id"])["visibility"] == "private"
