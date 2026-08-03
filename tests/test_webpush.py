"""Web Push + notifications (#726 Phase 3).

The tests that matter here are the boundary ones. A push message is handed to a third-party
service the operator does not run (FCM, Mozilla autopush); encryption protects it in transit,
but whatever we put in it is now held by someone else's infrastructure. So the payload shape is
a security contract, and these tests are what stop it widening by accident.

The crypto is checked by DECRYPTING with an independently written client-side implementation
rather than asserting that encryption "ran" — an encrypt-only test proves nothing about whether
a browser could ever read the result.
"""

from __future__ import annotations

import json
import os
import socket
import threading

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_sessions import notifications, webpush


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_VAPID_KEYS", str(tmp_path / "vapid.json"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PUSH_SUBS", str(tmp_path / "s.json"))
    monkeypatch.setattr(webpush, "_TRANSPORT", None)


def _client_keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return priv, raw


def _decrypt(body: bytes, client_priv, client_pub_raw: bytes, auth: bytes) -> bytes:
    """An independent RFC 8291 receiver — what a browser does."""
    salt, idlen = body[:16], body[20]
    server_pub_raw = body[21 : 21 + idlen]
    ciphertext = body[21 + idlen :]
    shared = client_priv.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_pub_raw)
    )
    ikm = webpush._hkdf(auth, shared, b"WebPush: info\x00" + client_pub_raw + server_pub_raw, 32)
    cek = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b"\x02"), "missing the RFC 8188 last-record delimiter"
    return plain[:-1]


# --- the third-party boundary -----------------------------------------------------------


def test_payload_carries_only_title_project_and_link():
    """THE contract of this module. A push transits infrastructure the operator does not run,
    so it must never carry screen or transcript content. `build_payload` is the only
    constructor and its signature is the enforcement point — this test fails if the shape
    widens."""
    payload = json.loads(webpush.build_payload(title="T", project="P", url="/s/claude/x"))
    assert set(payload) == {"title", "body", "url"}
    assert payload == {"title": "T", "body": "P", "url": "/s/claude/x"}


def test_no_session_content_can_reach_a_push_body():
    """End-to-end: drive the real notification → fanout path with a notification whose fields
    contain screen-looking text, and assert none of it appears in the encrypted body."""
    priv, pub_raw = _client_keypair()
    auth = os.urandom(16)
    notifications.subscribe(
        {
            "endpoint": "https://fcm.googleapis.com/fcm/send/ep/abc",
            "keys": {"p256dh": webpush._b64e(pub_raw), "auth": webpush._b64e(auth)},
        }
    )
    secret = "SUPER-SECRET-TRANSCRIPT-LINE"
    note = notifications.add(
        title="Relay session cap",
        project="battlelab-cloud",
        session_id="codex:bbbbbbbb-0000-4000-8000-000000000002",
        engine="codex",
        # A `reason` is exactly the field someone would later be tempted to widen into
        # "a bit of the screen". It must not travel.
        reason=secret,
        action_id="a1",
    )
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(201)

    webpush._TRANSPORT = httpx.MockTransport(handler)
    try:
        report = notifications.fanout(note, base_url="https://x")
    finally:
        webpush._TRANSPORT = None
    assert report["sent"] == 1
    plain = _decrypt(captured[0], priv, pub_raw, auth)
    assert secret.encode() not in plain
    assert set(json.loads(plain)) == {"title", "body", "url"}


def test_encrypted_body_round_trips_to_a_real_receiver():
    """Decrypt with an independent implementation — an encrypt-only assertion would pass on a
    body no browser could read."""
    priv, pub_raw = _client_keypair()
    auth = os.urandom(16)
    payload = webpush.build_payload(title="T", project="P", url="/u")
    body = webpush.encrypt(payload, webpush._b64e(pub_raw), webpush._b64e(auth))
    assert int.from_bytes(body[16:20], "big") == webpush.RECORD_SIZE
    assert body[20] == 65  # uncompressed P-256 point
    assert _decrypt(body, priv, pub_raw, auth) == payload


def test_each_encryption_uses_a_fresh_salt_and_ephemeral_key():
    """Reusing either across messages would leak plaintext relationships."""
    _priv, pub_raw = _client_keypair()
    auth = os.urandom(16)
    a = webpush.encrypt(b"x", webpush._b64e(pub_raw), webpush._b64e(auth))
    b = webpush.encrypt(b"x", webpush._b64e(pub_raw), webpush._b64e(auth))
    assert a[:16] != b[:16], "salt reused"
    assert a[21:86] != b[21:86], "ephemeral server key reused"


# --- VAPID -------------------------------------------------------------------------------


def test_vapid_signature_is_raw_rs_not_der():
    """Push services reject a DER signature silently. `cryptography` signs to DER, so the
    fixed-width re-encoding is load-bearing and easy to regress."""
    hdr = webpush._vapid_header("https://fcm.googleapis.com/fcm/send/x", "mailto:a@b")
    token = hdr["Authorization"].split("t=")[1].split(",")[0]
    h, c, sig = token.split(".")
    assert json.loads(webpush._b64d(h)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(webpush._b64d(c))["aud"] == "https://fcm.googleapis.com"
    assert len(webpush._b64d(sig)) == 64


def test_private_key_is_0600_and_only_the_public_half_is_exposed(tmp_path):
    pub = webpush.public_key()
    keyfile = tmp_path / "vapid.json"
    assert oct(keyfile.stat().st_mode & 0o777) == "0o600"
    assert len(webpush._b64d(pub)) == 65
    assert "PRIVATE KEY" not in pub
    assert webpush.public_key() == pub  # stable across calls


def test_a_gone_subscription_is_pruned_not_retried_forever():
    priv, pub_raw = _client_keypair()
    notifications.subscribe(
        {
            "endpoint": "https://fcm.googleapis.com/fcm/send/ep/dead",
            "keys": {"p256dh": webpush._b64e(pub_raw), "auth": webpush._b64e(os.urandom(16))},
        }
    )
    webpush._TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(410))
    try:
        note = notifications.add(title="T", project="P", session_id="claude:x", engine="claude")
        report = notifications.fanout(note)
    finally:
        webpush._TRANSPORT = None
    assert report["pruned"] == 1
    assert notifications.list_subscriptions() == []


def test_a_failing_push_service_never_loses_the_bell_entry():
    """In-app first: push is the extra. A dead service degrades the experience; it must not
    mean the operator never hears about an escalation."""
    _priv, pub_raw = _client_keypair()
    notifications.subscribe(
        {
            "endpoint": "https://fcm.googleapis.com/fcm/send/ep/flaky",
            "keys": {"p256dh": webpush._b64e(pub_raw), "auth": webpush._b64e(os.urandom(16))},
        }
    )
    note = notifications.add(title="T", project="P", session_id="claude:x", engine="claude")
    webpush._TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(500))
    try:
        report = notifications.fanout(note)
    finally:
        webpush._TRANSPORT = None
    assert report["failed"] == 1 and report["sent"] == 0
    assert notifications.listing()["unread"] == 1  # the bell still has it


def test_push_errors_never_embed_the_endpoint():
    """An endpoint is a per-device capability URL — leaking it into a log or an error message
    hands out the ability to push to that browser."""
    sub = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/ep/CAPABILITY-SECRET",
        "keys": {
            "p256dh": webpush._b64e(_client_keypair()[1]),
            "auth": webpush._b64e(os.urandom(16)),
        },
    }
    webpush._TRANSPORT = httpx.MockTransport(lambda r: httpx.Response(503))
    try:
        with pytest.raises(webpush.PushError) as ei:
            webpush.send(sub, b"{}")
    finally:
        webpush._TRANSPORT = None
    assert "CAPABILITY-SECRET" not in str(ei.value)


def _real_keys() -> dict:
    """Genuine P-256 material. Subscription now DECODES the key material at registration
    (a bad row used to abort the whole fanout at send time), so placeholder strings like
    {"p256dh": "a"} are correctly rejected and can't stand in for a browser's keys."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {"p256dh": webpush._b64e(pub_raw), "auth": webpush._b64e(os.urandom(16))}


# --- the subscription store ---------------------------------------------------------------


def test_the_client_never_sees_the_endpoint_path():
    pub = notifications.subscribe(
        {
            "endpoint": "https://fcm.googleapis.com/fcm/send/CAPABILITY-SECRET",
            "keys": _real_keys(),
        }
    )
    assert "CAPABILITY-SECRET" not in json.dumps(pub)
    assert pub["origin"] == "https://fcm.googleapis.com"
    assert json.dumps(notifications.list_subscriptions()).find("CAPABILITY-SECRET") == -1


def test_subscriptions_must_be_https_with_keys():
    for bad in (
        {"endpoint": "http://fcm.googleapis.com/fcm/send/x", "keys": _real_keys()},
        {
            "endpoint": "https://fcm.googleapis.com/fcm/send/x",
            "keys": {"p256dh": _real_keys()["p256dh"]},
        },
        {"endpoint": "https://fcm.googleapis.com/fcm/send/x"},
        {},
    ):
        with pytest.raises(ValueError):
            notifications.subscribe(bad)


def test_resubscribing_the_same_endpoint_is_idempotent():
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/ep/1", "keys": _real_keys()}
    first = notifications.subscribe(sub)
    again = notifications.subscribe(sub)
    assert first["id"] == again["id"]
    assert len(notifications.list_subscriptions()) == 1


def test_notifications_are_a_bounded_ring():
    for i in range(notifications.NOTIFY_MAX + 25):
        notifications.add(title=f"n{i}", project="p", session_id="claude:x", engine="claude")
    assert len(notifications.listing()["notifications"]) == notifications.NOTIFY_MAX


def test_notification_stores_are_0600(tmp_path):
    notifications.add(title="T", project="P", session_id="claude:x", engine="claude")
    notifications.subscribe(
        {"endpoint": "https://fcm.googleapis.com/fcm/send/e", "keys": _real_keys()}
    )
    for name in ("n.json", "s.json"):
        assert oct((tmp_path / name).stat().st_mode & 0o777) == "0o600"


# --- #730 review: the JSON stores lost updates under concurrency -------------------------


def test_concurrent_adds_do_not_lose_notifications(tmp_path, monkeypatch):
    """Read → mutate → replace with no lock is a lost update, and the shared `<path>.tmp` made
    it worse: both writers open the SAME temp file, so one `os.replace` unlinks the file the
    other is still writing into — FileNotFoundError, not just a silent loss."""
    import threading

    store = tmp_path / "notifications.json"
    errors: list[BaseException] = []
    ready = threading.Barrier(8)

    def writer(n: int) -> None:
        try:
            ready.wait(timeout=10)
            notifications.add(
                title=f"n{n}",
                project="p",
                reason="r",
                session_id=f"claude:{n}",
                engine="claude",
                action_id=f"a{n}",
                path=store,
            )
        except BaseException as e:  # noqa: BLE001 — the point is to surface ANY writer failure
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"a concurrent writer failed outright: {errors!r}"
    rows = notifications.listing(path=store)["notifications"]
    assert len(rows) == 8, f"lost updates: only {len(rows)} of 8 notifications persisted"
    assert len({r["action_id"] for r in rows}) == 8


def test_a_mark_read_racing_an_add_keeps_both_effects(tmp_path):
    """The cross-operation case: a mark-read racing an orchestrator add. Whichever lands
    second must build on the first's result, not overwrite the snapshot it read."""
    import threading

    store = tmp_path / "notifications.json"
    first = notifications.add(
        title="existing",
        project="p",
        reason="r",
        session_id="claude:0",
        engine="claude",
        action_id="a0",
        path=store,
    )

    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def do_mark() -> None:
        try:
            ready.wait(timeout=10)
            notifications.mark_read([first["id"]], path=store)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def do_add() -> None:
        try:
            ready.wait(timeout=10)
            notifications.add(
                title="new",
                project="p",
                reason="r",
                session_id="claude:1",
                engine="claude",
                action_id="a1",
                path=store,
            )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=do_mark), threading.Thread(target=do_add)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)

    assert not errors, f"a writer failed: {errors!r}"
    rows = notifications.listing(path=store)["notifications"]
    assert len(rows) == 2, "the add and the mark-read overwrote each other's snapshot"


# --- #730 review finding 2: the WIRING, not the mechanism --------------------------------


def test_an_escalation_actually_raises_a_notification(tmp_path, monkeypatch):
    """The gap Hermes found: `add`/`fanout` existed and were tested directly, but NOTHING in
    production called them — the bell could only ever poll an empty file. An escalation the
    operator is never told about is the single failure this feature exists to remove."""
    from agent_sessions import orchestrator, prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    store = tmp_path / "notifications.json"
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(store))
    prefs.set_orchestrator({"enabled": True, "notify": "escalations"})

    sent: list = []
    monkeypatch.setattr(notifications, "fanout", lambda note: sent.append(note))

    orchestrator._persist(
        [
            {
                "id": "act1",
                "state": "escalated",
                "session_id": "claude:abc",
                "engine": "claude",
                "title": "needs a decision",
                "project": "agent-sessions",
                "rationale": "two plausible options",
            },
            # A proposed action is NOT an escalation; `escalations` must not raise for it.
            {"id": "act2", "state": "proposed", "session_id": "claude:def", "engine": "claude"},
        ]
    )

    rows = notifications.listing(path=store)["notifications"]
    assert [r["action_id"] for r in rows] == [
        "act1"
    ], "an escalated action did not produce a notification (or a proposal wrongly did)"
    assert len(sent) == 1, "the notification was stored but never fanned out to push"
    assert rows[0]["title"] == "needs a decision"


def test_notify_none_stays_silent(tmp_path, monkeypatch):
    """The operator's setting is honoured — `none` means the bell stays quiet even on an
    escalation, and nothing is pushed."""
    from agent_sessions import orchestrator, prefs

    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    store = tmp_path / "notifications.json"
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(store))
    prefs.set_orchestrator({"enabled": True, "notify": "none"})

    sent: list = []
    monkeypatch.setattr(notifications, "fanout", lambda note: sent.append(note))
    orchestrator._persist(
        [{"id": "act1", "state": "escalated", "session_id": "claude:abc", "engine": "claude"}]
    )

    assert notifications.listing(path=store)["notifications"] == []
    assert sent == []


# --- #730 review round 2 ------------------------------------------------------------------


def test_concurrent_first_use_mints_exactly_one_vapid_key(tmp_path, monkeypatch):
    """The VAPID pair is a long-lived IDENTITY. Two first-use requests both minting means each
    returns its own public key, and every browser that subscribed against a losing key is
    permanently unreachable — with nothing logged and nothing to notice."""
    import concurrent.futures

    keys_path = tmp_path / "vapid.json"
    barrier = threading.Barrier(12)

    def mint() -> str:
        barrier.wait(timeout=15)
        _priv, pub = webpush.load_or_create_keys(keys_path)
        return pub

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        returned = list(ex.map(lambda _: mint(), range(12)))

    assert len(set(returned)) == 1, f"{len(set(returned))} distinct keys handed out"
    # And the survivor is the one on disk, so every subscription can actually be signed for.
    assert webpush.load_or_create_keys(keys_path)[1] == returned[0]


def test_a_corrupt_key_file_refuses_rather_than_rotating(tmp_path):
    """Silently minting a replacement strands every registered device. That has to be loud."""
    keys_path = tmp_path / "vapid.json"
    keys_path.write_text("{ this is not json")
    with pytest.raises(webpush.PushError, match="refusing to rotate"):
        webpush.load_or_create_keys(keys_path)


def test_malformed_key_material_is_refused_at_subscribe(tmp_path, monkeypatch):
    """Two arbitrary strings satisfied the old isinstance check and then raised binascii deep
    in the encryption path — outside the transport's exception boundary."""
    store = tmp_path / "subs.json"
    with pytest.raises(ValueError, match="base64url|P-256|16 bytes"):
        notifications.subscribe(
            {
                "endpoint": "https://fcm.googleapis.com/fcm/send/a",
                "keys": {"p256dh": "a", "auth": "b"},
            },
            path=store,
        )


def test_one_bad_subscription_does_not_silence_the_rest(tmp_path, monkeypatch):
    """Best-effort has to MEAN it. A row that fails unexpectedly must cost only that device;
    it used to abort the loop so every later device went unnotified."""
    store = tmp_path / "subs.json"
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/good", "keys": _real_keys()}
    notifications.subscribe(good, path=store)
    # Force a row that blows up in an unanticipated way, bypassing subscribe's validation the
    # way a row written before validation existed would.
    rows = json.loads(store.read_text())
    rows.insert(0, {"id": "bad", "endpoint": "https://fcm.googleapis.com/fcm/send/bad", "keys": {}})
    store.write_text(json.dumps(rows))

    sent: list = []

    def fake_send(row, payload, **k):
        if row.get("id") == "bad":
            raise TypeError("unanticipated failure deep in the crypto path")
        sent.append(row["endpoint"])

    monkeypatch.setattr(webpush, "send", fake_send)
    monkeypatch.setenv("AGENT_SESSIONS_PUSH_SUBS", str(store))
    res = notifications.fanout({"title": "t", "session_id": "claude:x", "engine": "claude"})

    assert sent == [
        "https://fcm.googleapis.com/fcm/send/good"
    ], "a bad row silenced the devices after it"
    assert res["sent"] == 1 and res["failed"] == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/push",
        "https://10.0.0.5/push",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/push",
        "http://fcm.googleapis.com/x",  # not https
        "https:///nohost",
        "https://evil.example/fcm/send/x",  # plausible-looking but not a push service
        "https://fcm.googleapis.com.evil.example/x",  # suffix-confusion attempt
    ],
)
def test_subscription_endpoints_that_point_inward_are_refused(endpoint, tmp_path):
    """The endpoint is attacker-supplied and the SERVER later POSTs to it — an unvalidated one
    is a blind SSRF primitive aimed at loopback, the metadata service, or the LAN."""
    with pytest.raises(ValueError):
        notifications.subscribe(
            {"endpoint": endpoint, "keys": _real_keys()}, path=tmp_path / "s.json"
        )


# --- #730 review round 3: the SSRF control must bind to the actual request ---------------


def test_the_allowlist_is_enforced_at_the_REQUEST_boundary(monkeypatch):
    """Hermes's point: validating in a helper proves nothing if the helper isn't on the path
    the request takes. This drives `send()` itself — the function that opens the connection —
    and asserts no HTTP request is issued for a non-push host."""
    attempted: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(str(request.url))
        return httpx.Response(201)

    monkeypatch.setattr(webpush, "_TRANSPORT", httpx.MockTransport(handler))

    inward = {
        "endpoint": "https://169.254.169.254/latest/meta-data",
        "keys": _real_keys(),
    }
    with pytest.raises(webpush.PushError, match="not a known push-service host"):
        webpush.send(inward, b"payload")
    assert attempted == [], "an HTTP request was issued to a non-push host"

    # …and a real push host still goes through, so the control isn't just refusing everything.
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": _real_keys()}
    webpush.send(good, b"payload")
    assert len(attempted) == 1


def test_dns_rebinding_cannot_defeat_the_allowlist(monkeypatch):
    """The failure mode of the previous resolve-and-check design: the validated resolution is
    not the one the client connects with, so a hostname answering public-then-private slips
    through. An allowlist has no such window — the decision involves no DNS at all, so making
    resolution lie changes nothing."""
    calls = {"n": 0}

    def rebinding_getaddrinfo(host, port, *a, **k):
        calls["n"] += 1
        ip = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)

    # A hostname the attacker controls is refused regardless of what DNS says, first or second.
    with pytest.raises(webpush.PushError, match="not a known push-service host"):
        webpush.assert_allowed_target("https://rebind.evil.example/push")
    assert calls["n"] == 0, "the allowlist consulted DNS; that reintroduces the TOCTOU"


def test_a_suffix_confusion_host_is_refused():
    """`fcm.googleapis.com.evil.example` must not pass as `fcm.googleapis.com`."""
    with pytest.raises(webpush.PushError):
        webpush.assert_allowed_target("https://fcm.googleapis.com.evil.example/x")
    # A genuine subdomain of an allowlisted service is fine (Safari uses web.push.apple.com).
    webpush.assert_allowed_target("https://web.push.apple.com/abc")


def test_a_self_hosted_push_service_can_be_allowlisted(monkeypatch):
    """The allowlist must not lock out a legitimate self-hosted service."""
    with pytest.raises(webpush.PushError):
        webpush.assert_allowed_target("https://push.internal.example/x")
    monkeypatch.setenv("AGENT_SESSIONS_PUSH_ALLOWED_HOSTS", "push.internal.example")
    webpush.assert_allowed_target("https://push.internal.example/x")
