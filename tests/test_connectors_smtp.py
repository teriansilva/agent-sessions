"""Email through the Commander's own account (#1836, #1746 P2b), against a fake SMTP server with
real sockets and real TLS (`smtp_fake`: a CA and a certificate for ``mail.example.test`` made per
test, the name pinned to 127.0.0.1). Pinned here:

* TLS always: a plaintext-only server, a refused STARTTLS and a certificate for another name are
  all refused BEFORE AUTH; the socket goes to the vetted address, resolved once (rebinding can't
  move it), and an unsafe address is never connected to;
* outcomes (D7, not idempotent): a 5xx to RCPT TO is `failed` with no DATA; a timeout after DATA
  is `unconfirmed` and DATA is written exactly once, ever; a drop after the final 250 is `sent`;
* the message on the wire is what the card showed (From, To, Subject, body), for ASCII, UTF-8
  and quoted-printable; the From address is the configured sender, in the envelope and the
  header, and a change of it makes a waiting card stale;
* typed recipients: one plain address each, normalized, never twice, bound to the account; an
  account change switches sending off; a hand-edited address is never sent to;
* the password never reaches an error, a log, a response or the store's plain text.
"""

from __future__ import annotations

import email
import email.policy
import json
import logging
import threading
import time

import httpx
import pytest

from agent_sessions import ask_messages, connector_http, connector_smtp, prefs, review
from agent_sessions.connectors import candidates, service, store
from smtp_fake import NAME, FakeSMTP
from test_ask_orchestrator import App, Script, answer, call
from test_connectors_messengers import run

PASSWORD = "app-password-123456"
ACCOUNT = "hq@example.com"
SENDER = "release-bot@example.com"
CREW = "Crew@example.com"
TRICKY = "Grüße zum Release 🙏\n\nDeploy window 22:00–23:00 UTC.\n.a line with a leading dot\nends"


@pytest.fixture
def mail(tmp_path, monkeypatch):
    made: list[FakeSMTP] = []
    calls: list[str] = []

    async def resolve(host, port):
        calls.append(host)
        assert host == NAME, host
        return ["127.0.0.1"]

    monkeypatch.setattr(connector_http, "_resolve", resolve)
    monkeypatch.setattr(connector_smtp, "COMMAND_TIMEOUT_S", 2.0)
    monkeypatch.setattr(connector_smtp, "SESSION_DEADLINE_S", 4.0)

    def make(mode: str = "smtps", **kw) -> FakeSMTP:
        d = tmp_path / f"srv{len(made)}"
        d.mkdir()
        fake = FakeSMTP(d, mode=mode, **kw)
        monkeypatch.setattr(connector_smtp, "_CA_FILE", fake.ca)
        made.append(fake)
        return fake

    make.resolved = calls  # type: ignore[attr-defined]
    yield make
    for f in made:
        f.close()


def _configure(fake: FakeSMTP, *, sender: str = SENDER) -> dict:
    inst = store.add("smtp")
    store.patch(inst["id"], {"origin": fake.origin, "account": ACCOUNT, "token": PASSWORD})
    return store.patch(inst["id"], {"sender": sender})


def _email(fake: FakeSMTP, *, sender: str = SENDER, addresses=None) -> dict:
    inst = _configure(fake, sender=sender)
    got = run(service.run_test(inst["id"]))
    assert got["status"] == "ok", got
    inst = store.read()["instances"][0]
    rows = addresses or [{"address": CREW, "label": "Release crew"}]
    store.set_addresses(inst["id"], rows, inst["generation"])
    return store.set_send(inst["id"], True)


# ---- origins, addresses, the From address ---------------------------------------------------


def test_mail_origins_are_smtps_or_smtp_only_and_only_for_mail():
    mail_schemes = store.schemes_for("smtp")
    assert (
        store.normalize_origin("smtps://Mail.Example.com", mail_schemes)
        == "smtps://mail.example.com"
    )
    assert store.normalize_origin("smtp://mail.example.com:2587", mail_schemes) == (
        "smtp://mail.example.com:2587"
    )
    for bad in ("https://mail.example.com", "smtps://u:p@mail.example.com", "smtps://h/x",
                "smtps://169.254.169.254"):  # fmt: skip
        with pytest.raises(store.Refused):
            store.normalize_origin(bad, mail_schemes)
    with pytest.raises(store.Refused):
        store.normalize_origin("smtps://mail.example.com")  # never for an HTTP connector
    inst = store.add("smtp")
    with pytest.raises(store.Refused):
        store.patch(inst["id"], {"origin": "https://mail.example.com"})
    forge = store.add("forgejo")
    with pytest.raises(store.Refused):
        store.patch(forge["id"], {"origin": "smtps://mail.example.com"})


@pytest.mark.parametrize(
    "raw",
    [
        "Release Crew <crew@example.com>",
        "crew@example.com\r\nBcc: x@evil.test",
        "crew@example.com\nX: y",
        '"crew team"@example.com',
        "crew(comment)@example.com",
        "crew@example.com, other@example.com",
        "crew@localhost",
        "crew@[127.0.0.1]",
        "crew@exämple.com",
        " crew@example.com",
        "crew",
        "a" * 65 + "@example.com",
    ],
)
def test_an_address_that_is_not_one_plain_mailbox_is_refused_in_words(mail, raw):
    inst = _configure(mail())
    run(service.run_test(inst["id"]))
    inst = store.read()["instances"][0]
    with pytest.raises(store.Refused) as e:
        store.set_addresses(inst["id"], [{"address": raw, "label": ""}], inst["generation"])
    assert e.value.code == "invalid_address" and str(e.value)
    assert store.read()["instances"][0]["recipients"] == []


def test_addresses_are_normalized_deduplicated_and_labels_default(mail):
    inst = _configure(mail())
    run(service.run_test(inst["id"]))
    gen = store.read()["instances"][0]["generation"]
    with pytest.raises(store.Refused) as e:
        store.set_addresses(
            inst["id"],
            [
                {"address": "Ops@Example.COM", "label": ""},
                {"address": "Ops@example.com", "label": ""},
            ],
            gen,
        )
    assert e.value.code == "duplicate_address"
    out = store.set_addresses(
        inst["id"],
        [{"address": "Ops@Example.COM", "label": ""}, {"address": "ops@example.com", "label": "x"}],
        gen,
    )
    # The domain is lower-cased; the local part is kept (it can be case-sensitive).
    assert [(r["id"], r["label"], r["kind"]) for r in out["recipients"]] == [
        ("Ops@example.com", "Ops@example.com", "email"),
        ("ops@example.com", "x", "email"),
    ]
    with pytest.raises(store.Refused) as e:
        store.set_addresses(inst["id"], [], gen - 1)
    assert e.value.code == "stale_candidates"


def test_a_sender_change_moves_the_generation_not_the_password(mail):
    fake = mail()
    inst = _email(fake)
    before = store.read()["instances"][0]
    store.patch(inst["id"], {"sender": "other@example.com"})
    after = store.read()["instances"][0]
    assert after["generation"] == before["generation"] + 1
    assert after["credential_revision"] == before["credential_revision"]
    assert after["tested"] is None and not store.sendable(after)
    # The password still opens: a new test passes without entering it again.
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    with pytest.raises(store.Refused) as e:
        store.patch(inst["id"], {"sender": "Name <x@example.com>"})
    assert e.value.code == "invalid_sender"


def test_typed_and_listed_recipients_do_not_cross(mail, auth_cfg):
    inst = _configure(mail())
    with pytest.raises(store.Refused):
        run(candidates.list_candidates(inst["id"]))
    with pytest.raises(store.Refused):
        store.set_recipients(
            inst["id"], [CREW], {"generation": inst["generation"], "candidates": {}}
        )
    tg = store.add("telegram")
    with pytest.raises(store.Refused):
        store.set_addresses(tg["id"], [{"address": CREW, "label": ""}], tg["generation"])


# ---- TLS, vetting, the probe ---------------------------------------------------------------


@pytest.mark.parametrize(
    "setup, code",
    [
        ({"mode": "starttls", "offer_starttls": False}, "tls_required"),
        ({"mode": "starttls", "starttls_code": 454}, "tls_required"),
        ({"mode": "smtps", "name": "other.example.test"}, "tls_failed"),
        ({"mode": "starttls", "name": "other.example.test"}, "tls_failed"),
    ],
)
def test_no_tls_or_the_wrong_certificate_is_refused_before_auth(mail, setup, code):
    mode = setup.pop("mode")
    name = setup.pop("name", NAME)
    fake = mail(mode, name=name)
    for k, v in setup.items():
        setattr(fake, k, v)
    inst = _configure(fake)
    got = run(service.run_test(inst["id"]))
    assert got["status"] == "failed" and got["code"] == code, got
    assert fake.auths == [] and "AUTH" not in fake.commands
    assert PASSWORD not in json.dumps(got)


@pytest.mark.parametrize("mode", ["smtps", "starttls"])
@pytest.mark.parametrize("mech", ["PLAIN", "LOGIN"])
def test_the_probe_signs_in_over_tls_and_sends_nothing(mail, mode, mech):
    fake = mail(mode)
    fake.auth = mech
    inst = _configure(fake)
    got = run(service.run_test(inst["id"]))
    assert got == {"status": "ok", "login": ACCOUNT, "repos": 0, "generation": got["generation"]}
    assert fake.auths == [(ACCOUNT, PASSWORD)] and fake.tls_ok == [True]
    assert fake.data_commands == 0 and not any(c.startswith("MAIL") for c in fake.commands)


def test_a_wrong_password_is_rejected_and_named_without_the_password(mail, caplog):
    fake = mail()
    fake.password = "something-else-entirely"
    inst = _configure(fake)
    caplog.set_level(logging.DEBUG)
    got = run(service.run_test(inst["id"]))
    assert got["status"] == "failed" and got["code"] == "rejected"
    assert PASSWORD not in json.dumps(got) and PASSWORD not in caplog.text


def test_an_unsafe_address_is_never_connected_to(mail, monkeypatch):
    fake = mail()
    inst = _configure(fake)

    async def metadata(host, port):
        return ["169.254.169.254"]

    monkeypatch.setattr(connector_http, "_resolve", metadata)
    got = run(service.run_test(inst["id"]))
    assert got["status"] == "failed" and got["code"] == "refused_destination"
    assert fake.connections == 0


def test_dns_rebinding_cannot_move_the_connection(mail, monkeypatch):
    fake = mail()
    inst = _configure(fake)
    answers = iter([["127.0.0.1"], ["169.254.169.254"], ["169.254.169.254"]])
    asked = []

    async def rebinding(host, port):
        asked.append(host)
        return next(answers)

    monkeypatch.setattr(connector_http, "_resolve", rebinding)
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    # Resolved once; TLS still verified the NAME on a socket to the vetted address.
    assert asked == [NAME] and fake.tls_ok == [True]


# ---- sending through HQ: the card and the wire ---------------------------------------------


@pytest.fixture
def hq(monkeypatch, auth_cfg, mail):
    prefs.set_ai_review(
        {"egress_acknowledged": True, "enabled": True, "base_url": "https://ai.test/v1",
         "api_key": "sk-ask-test", "model": "m"}
    )  # fmt: skip
    app = App(auth_cfg)
    app.c.__enter__()
    try:
        yield app, mail
    finally:
        app.c.__exit__(None, None, None)


def _propose(app: App, monkeypatch, text: str):
    script = Script(
        call("list_connectors", {}, cid="c1"),
        call("list_recipients", {"connector_index": 0}, cid="c2"),
        call(
            "propose_message", {"connector_index": 0, "recipient_index": 0, "text": text}, cid="c3"
        ),
        answer("Here is the email; tap Send when you're ready."),
    )
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    cid = app.new()
    turn = app.ask(cid, "mail the crew the window").json()["turn"]
    assert turn["status"] == "done", turn
    cards = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
    return cid, (cards[0] if cards else None), script


def _tap(app: App, card: dict):
    return app.c.post(
        f"/api/ask/proposals/{card['action_id']}/decide", json={"decision": "send"}, headers=app.hdr
    )


def _settled(app: App, cid: str) -> dict:
    import time

    end = time.monotonic() + 20
    while True:
        (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
        if card["state"] != "deciding" or time.monotonic() > end:
            return card
        time.sleep(0.05)


def _received(fake: FakeSMTP) -> email.message.EmailMessage:
    (raw,) = fake.messages
    return email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[return-value]


@pytest.mark.parametrize(
    "text, eight_bit, cte",
    [
        ("Release v0.19.5 is green\n\nReady for your go.", True, "7bit"),
        (TRICKY, True, "8bit"),
        (TRICKY, False, "quoted-printable"),
    ],
)
def test_the_mail_on_the_wire_is_what_the_card_showed(hq, monkeypatch, text, eight_bit, cte):
    app, mail = hq
    fake = mail()
    fake.eight_bit = eight_bit
    _email(fake)
    cid, card, script = _propose(app, monkeypatch, text)
    listed = json.loads(script.tool_results(2)[-1]["content"])
    assert listed["recipients"][0]["address"] == CREW
    subject, body = connector_smtp.split_mail(text)
    assert (card["sender"], card["address"], card["subject"], card["body"]) == (
        SENDER, CREW, subject, body,
    )  # fmt: skip
    assert card["recipient"] == "Release crew" and card["text"] == text
    assert fake.data_commands == 0  # proposing sends nothing
    assert _tap(app, card).status_code == 202
    done = _settled(app, cid)
    assert done["state"] == "sent", done
    got = _received(fake)
    assert got["From"] == card["sender"] and got["To"] == card["address"]
    assert got["Subject"] == card["subject"]
    assert got.get_content().replace("\r\n", "\n") == card["body"] + "\n"
    assert got["Content-Transfer-Encoding"] == cte
    assert got["Message-ID"] == f"<{card['action_id']}@battlelab.invalid>"
    # The envelope sender is the configured From too, and the login is a different account.
    assert fake.envelopes == [(SENDER, [CREW])] and fake.auths[-1] == (ACCOUNT, PASSWORD)
    assert fake.data_commands == 1


def test_a_from_address_change_makes_a_waiting_card_stale(hq, monkeypatch):
    app, mail = hq
    fake = mail()
    inst = _email(fake)
    cid, card, _ = _propose(app, monkeypatch, "Subject\n\nbody")
    store.patch(inst["id"], {"sender": "someone-else@example.com"})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    assert store.set_send(inst["id"], True)
    r = _tap(app, card)
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "stale"
    assert not any(c.startswith("MAIL") for c in fake.commands)


@pytest.mark.parametrize(
    "knobs, state, data_commands",
    [
        ({"rcpt_code": 550}, "failed", 0),
        ({"rcpt_code": 451}, "failed", 0),
        ({"mail_code": 553}, "failed", 0),
        ({"data_code": 554}, "failed", 1),
        ({"final": "hang"}, "unconfirmed", 1),
        ({"final": "drop"}, "unconfirmed", 1),
        ({"final": "550"}, "unconfirmed", 1),
        ({"drop_after_final": True}, "sent", 1),
        ({"size": 100}, "failed", 0),
    ],
)
def test_outcomes_follow_what_the_server_proved(hq, monkeypatch, knobs, state, data_commands):
    app, mail = hq
    fake = mail()
    for k, v in knobs.items():
        setattr(fake, k, v)
    _email(fake)
    cid, card, _ = _propose(app, monkeypatch, "Window tonight\n\n22:00 UTC")
    assert _tap(app, card).status_code == 202
    done = _settled(app, cid)
    assert done["state"] == state, done
    assert fake.data_commands == data_commands
    # Never resubmitted: a second tap returns the state, and nothing else reaches the server.
    again = _tap(app, card)
    assert again.status_code == 200 and again.json()["proposal"]["state"] == state
    assert fake.data_commands == data_commands and len(fake.messages) <= 1


def test_an_oversize_or_subjectless_email_is_refused_at_staging(hq, monkeypatch):
    app, mail = hq
    fake = mail()
    _email(fake)
    huge = "Big\n\n" + "€" * 31000  # under the character cap, over the wire cap once encoded
    _cid, card, script = _propose(app, monkeypatch, huge)
    assert card is None
    assert "too large" in script.tool_results(3)[-1]["content"]
    _cid, card, script = _propose(app, monkeypatch, "x" * 79 + "\n\nbody")
    assert card is None and "subject" in script.tool_results(3)[-1]["content"]
    assert fake.connections == 1  # the test only


def test_the_password_is_refused_as_message_text_and_never_stored_plain(hq, monkeypatch):
    app, mail = hq
    fake = mail()
    _email(fake)
    _cid, card, script = _propose(app, monkeypatch, f"Creds\n\nit is {PASSWORD}")
    assert card is None and "secret" in script.tool_results(3)[-1]["content"]
    assert PASSWORD not in store.store_path().read_text()
    listing = app.c.get("/api/connectors", headers=app.hdr)
    assert listing.status_code == 200 and PASSWORD not in listing.text
    assert listing.json()["instances"][0]["sender"] == SENDER


def test_an_account_change_switches_sending_off_until_addresses_are_saved(mail):
    fake = mail()
    inst = _email(fake)
    fake.account = "other@example.com"
    store.patch(inst["id"], {"account": "other@example.com", "token": PASSWORD})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    now = store.read()["instances"][0]
    assert not store.sendable(now) and now["enabled"]["send"] is False
    assert now["send_off"]["reason"] == "identity_changed"
    with pytest.raises(store.Refused) as e:
        store.set_send(inst["id"], True)
    assert e.value.code == "recipients_unbound"
    store.set_addresses(inst["id"], [{"address": CREW, "label": ""}], now["generation"])
    assert store.set_send(inst["id"], True)


def test_a_hand_edited_address_is_never_sent_to(hq, monkeypatch):
    """The store reads strictly: an address no build would save makes the whole store unreadable,
    which refuses the tap (and every connector) rather than send anywhere. Fail closed."""
    app, mail = hq
    fake = mail()
    _email(fake)
    _cid, card, _ = _propose(app, monkeypatch, "Subject\n\nbody")
    path = store.store_path()
    doc = json.loads(path.read_text())
    doc["instances"][0]["recipients"][0]["id"] = "Crew <crew@example.com>"
    path.write_text(json.dumps(doc))
    with pytest.raises(store.StoreUnreadable):
        store.read()
    r = _tap(app, card)
    assert r.status_code == 503
    assert not any(c.startswith("MAIL") for c in fake.commands)
    assert fake.data_commands == 0


def test_the_mail_transport_refuses_a_bound_that_is_not_a_send():
    bound = connector_http.Bound("c_000000000000", 1, "smtps://mail.example.test", "Basic", "Email",
                                 adapter="smtp", for_read=False)  # fmt: skip
    out = run(connector_smtp._send(bound, CREW, "S\n\nb", "a" * 32, SENDER))
    assert out == {"outcome": "failed", "code": "refused", "message_id": ""}
    with pytest.raises(connector_http.ConnectorError):
        connector_smtp.build(sender=SENDER, to="x@y.test\r\nBcc: z@q.test", text="s\n\nb",
                             msg_id="a" * 32, eight_bit=True)  # fmt: skip
    assert ask_messages.binding("c_1", 1, CREW, "t") != ask_messages.binding(
        "c_1", 1, CREW, "t", SENDER
    )


# ---- the outbound boundary (#1833's final check, Hermes 7610) ----------------------------------


def test_an_address_withdrawn_after_the_claim_gets_no_mail(hq, monkeypatch):
    app, mail = hq
    fake = mail()
    inst = _email(fake)
    _cid, card, _ = _propose(app, monkeypatch, "Release window\n\nTonight at nine.")
    cid = _cid

    async def withdraw():
        now = store.read()["instances"][0]
        store.set_addresses(
            inst["id"], [{"address": "ops@example.com", "label": "Ops"}], now["generation"]
        )

    monkeypatch.setattr(ask_messages, "_HOOK_BEFORE_SEND", withdraw)
    assert _tap(app, card).status_code == 202
    done = _settled(app, cid)
    assert done["state"] == "stale" and "no longer an allowed recipient" in done["reason"], done
    assert fake.messages == [] and fake.data_commands == 0


def test_a_refusal_after_the_servers_354_writes_no_message(hq, monkeypatch):
    """The last check runs inside the session, after DATA's 354 and before the message: a refusal
    there closes the connection without the end-of-data line, so the server takes nothing."""
    app, mail = hq
    fake = mail()
    _email(fake)
    cid, card, _ = _propose(app, monkeypatch, "Release window\n\nTonight at nine.")
    calls: list[bool] = []

    def refusing(row):
        def check(fenced: bool) -> None:
            calls.append(fenced)
            if len(calls) >= 2:  # the in-session check, after the 354
                raise connector_http.ConnectorError("secret_now", "a secret")

        return check

    monkeypatch.setattr(ask_messages, "_final_check", refusing)
    assert _tap(app, card).status_code == 202
    done = _settled(app, cid)
    assert calls == [True, True]
    assert done["state"] == "stale" and "now holds a secret" in done["reason"], done
    assert fake.data_commands == 1 and fake.messages == []


@pytest.mark.parametrize("mode", ["smtps", "starttls"])
def test_the_session_deadline_interrupts_a_stalled_read_over_tls(mail, monkeypatch, mode):
    """Hermes 7755: TLS wraps the socket by taking over its descriptor and detaching the original,
    so a watchdog holding the original shut nothing, and a server stalled after the data held the
    send for the whole command timeout. The deadline is fired here the moment the server has the
    message and goes silent; the send must return well inside the command timeout."""
    fake = mail(mode)
    fake.final = "hang"
    monkeypatch.setattr(connector_smtp, "COMMAND_TIMEOUT_S", 30.0)
    monkeypatch.setattr(connector_smtp, "SESSION_DEADLINE_S", 600.0)  # fired by hand below
    watches: list = []

    class Recorded(connector_smtp._Watch):
        def __init__(self) -> None:
            super().__init__()
            watches.append(self)

    monkeypatch.setattr(connector_smtp, "_Watch", Recorded)
    fired: list[float] = []

    def fire_when_stalled() -> None:
        end = time.monotonic() + 20
        while not fake.messages and time.monotonic() < end:
            time.sleep(0.02)
        fired.append(time.monotonic())
        watches[0]._fire()

    threading.Thread(target=fire_when_stalled, daemon=True).start()
    out = connector_smtp._session(
        "smtps" if mode == "smtps" else "smtp",
        "127.0.0.1",
        NAME,
        fake.port,
        ACCOUNT,
        PASSWORD,
        sender=SENDER,
        to=CREW,
        text="Window tonight\n\n22:00 UTC",
        msg_id="m-deadline",
    )
    returned = time.monotonic()
    assert fake.messages and fired, "the server never had the message"
    assert out["outcome"] == "unconfirmed", out
    assert returned - fired[0] < 5.0, returned - fired[0]
