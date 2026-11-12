"""THE outbound site for mail (#1836, #1746 D2): an email connector's ONE transport, separately
reviewed (mail is not HTTP, so it never goes through `connector_http`, and never around it).

**The adapter never sees the password.** A `connector_http.Bound` names the instance and its
generation; this module reads the account, the password and the From address from the store for
exactly that generation (`store.token_for`, fenced on SENDING for a send), right before use.

**Transport rules** (the same as `connector_http`'s, for SMTP):

* the origin is ``smtps://host[:465]`` (implicit TLS) or ``smtp://host[:587]`` (STARTTLS, which
  MUST succeed); the host is resolved ONCE and vetted (`net_vetting`: no link-local, metadata,
  multicast, unspecified or reserved address) and the socket goes to the VETTED address, never
  resolving the name again; TLS verifies the certificate against the configured NAME;
* TLS before anything else: no plaintext fallback, and ``AUTH`` (PLAIN or LOGIN) only on a TLS
  socket; a server without STARTTLS, or a failed handshake, is ``tls_required`` / ``tls_failed``;
* every socket operation has a `COMMAND_TIMEOUT_S` timeout, and the whole session (connect to the
  final reply) a `SESSION_DEADLINE_S` deadline, enforced by a watchdog that shuts the connection
  (through its own duplicate descriptor, so it reaches the TLS socket too);
* the message is built HERE from a fixed header set (From, To, Subject, Date, Message-ID, the MIME
  headers): the addresses are validated mailboxes and the subject is one line, so no header can
  be injected; the encoded message is capped (`WIRE_MAX`) before ``MAIL FROM``;
* errors are short codes with a content-free detail: never the password, never a server reply.

**Outcomes (D7, for a protocol that is not idempotent).** `sent` only on the server's final 250
to the message data. `failed` only when it is PROVEN nothing was submitted: refused before the
``DATA`` command (connect, TLS, AUTH, size, or a 4xx/5xx to ``MAIL FROM`` / ``RCPT TO``), or a
refusal of ``DATA`` itself, before the message is written. Anything ambiguous once ``DATA`` was
written (a timeout, a drop, a missing or unexpected final reply) is `unconfirmed`, and it is never
submitted again: one call writes the message at most once.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import socket
import ssl
import threading
from datetime import UTC, datetime
from email import policy as email_policy
from email.message import EmailMessage
from urllib.parse import urlsplit

from . import connector_http, net_vetting
from .connector_http import Bound, ConnectorError

COMMAND_TIMEOUT_S = 10.0
SESSION_DEADLINE_S = 60.0
VET_TIMEOUT_S = 10.0
#: The encoded message's size on the wire, headers included, before dot-stuffing.
WIRE_MAX = 256 * 1024
SUBJECT_MAX = 78
#: A line longer than this cannot travel as 7bit/8bit (RFC 5321's 998 + CRLF).
LINE_MAX = 998
DEFAULT_PORTS = {"smtps": 465, "smtp": 587}
#: The id a server's final 250 names ("… queued as 4ABC123"), when it names one.
_QUEUED_RE = re.compile(r"queued as <?([A-Za-z0-9._@-]{1,100})>?", re.IGNORECASE)
_MSGID_DOMAIN = "battlelab.invalid"

#: Test seam: a CA file the tests' fake server is signed by. ``None`` (always, outside tests) is
#: the system's trust store. Never a way to turn verification off: hostname and chain stay checked.
_CA_FILE: str | None = None


# ---- the message -----------------------------------------------------------------------------


def split_mail(text: str) -> tuple[str, str]:
    """``(subject, body)`` of a message HQ wrote: the subject is the first line (tabs as spaces,
    trimmed), the body is the rest, without the blank lines that separate them. The card shows
    exactly these two, and the transport sends exactly these two."""
    first, _, rest = text.partition("\n")
    return " ".join(first.replace("\t", " ").split()), rest.lstrip("\n")


def subject_problem(text: str) -> str | None:
    """Why ``text`` can't be one email, or None."""
    subject, _ = split_mail(text)
    if not subject:
        return "the first line is the subject, and it is empty"
    if len(subject) > SUBJECT_MAX:
        return f"the first line is the subject: at most {SUBJECT_MAX} characters"
    return None


def build(
    *, sender: str, to: str, text: str, msg_id: str, eight_bit: bool, now: datetime | None = None
) -> bytes:
    """The message as it goes on the wire: the fixed header set and a text/plain UTF-8 body.
    ASCII travels as 7bit; other text as 8bit when the server offers 8BITMIME and no line is too
    long, else quoted-printable."""
    from .connectors import store

    if not (store.valid_address(sender) and store.valid_address(to)):
        raise ConnectorError("refused", "not a plain address")
    subject, body = split_mail(text)
    if "\n" in subject or "\r" in subject:
        raise ConnectorError("refused", "the subject is one line")
    long_line = any(len(line.encode()) > LINE_MAX for line in body.split("\n"))
    if long_line:
        cte = "quoted-printable"
    elif body.isascii():
        cte = "7bit"
    else:
        cte = "8bit" if eight_bit else "quoted-printable"
    msg = EmailMessage(policy=email_policy.SMTP)
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = (now or datetime.now(UTC)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = f"<{msg_id}@{_MSGID_DOMAIN}>"
    msg.set_content(body + ("\n" if not body.endswith("\n") else ""), charset="utf-8", cte=cte)
    return msg.as_bytes()


def _stuff(raw: bytes) -> bytes:
    """The DATA payload: CRLF line ends, a leading dot doubled, and the terminating ``.``."""
    data = re.sub(rb"\r\n|\n|\r", b"\r\n", raw)
    data = re.sub(rb"(?m)^\.", b"..", data)
    if not data.endswith(b"\r\n"):
        data += b"\r\n"
    return data + b".\r\n"


# ---- the connection ----------------------------------------------------------------------------


def _target(origin: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of a stored ``smtps://`` / ``smtp://`` origin."""
    parts = urlsplit(origin)
    if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
        raise ConnectorError("refused", "not a mail server origin")
    return parts.scheme, parts.hostname, parts.port or DEFAULT_PORTS[parts.scheme]


async def _vet(host: str, port: int) -> str:
    """The ONE vetted address for ``host`` (`net_vetting`, the HTTP connectors' rule)."""
    literal = f"[{host}]" if ":" in host else host
    try:
        async with asyncio.timeout(VET_TIMEOUT_S):
            dest = await net_vetting.vet(
                f"https://{literal}:{port}",
                resolve=connector_http._resolve,
                error=ConnectorError,
                policy=net_vetting.TOKEN_HTTPS_OR_LOOPBACK,
            )
    except TimeoutError:
        raise ConnectorError("timeout", f"{host} could not be resolved in time") from None
    return dest.address


class _Watch:
    """The session deadline: shuts the connection down when it passes, so a blocked read returns.

    It holds its OWN duplicate of the connection's descriptor (Hermes 7755). TLS, implicit or by
    STARTTLS mid-session, wraps the socket by taking over its descriptor and detaching the
    original, so the original object shuts nothing once wrapped. ``shutdown(2)`` acts on the
    connection, not the descriptor, so the duplicate stops reads and writes on the TLS socket too,
    including one blocked in the handshake itself."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self.expired = False
        self._timer = threading.Timer(SESSION_DEADLINE_S, self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def watch(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock.dup()
            if self.expired:
                self._shut()

    def _shut(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _fire(self) -> None:
        with self._lock:
            self.expired = True
            self._shut()

    def cancel(self) -> None:
        self._timer.cancel()
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None


def _client(scheme: str, address: str, watch: _Watch) -> smtplib.SMTP:
    """An `smtplib` client whose socket goes to the vetted ``address``. ``connect`` is called with
    the configured NAME, and the socket hook records it as ``_host``, which `smtplib.starttls`
    uses as the TLS server name (``connect`` alone does not set it); implicit TLS wraps with the
    name directly. Either way the certificate is verified against the NAME, never the address."""
    context = ssl.create_default_context(cafile=_CA_FILE)
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    def raw_socket(port: int, timeout: float) -> socket.socket:
        sock = socket.create_connection((address, port), timeout)
        watch.watch(sock)
        return sock

    def literal(sock: socket.socket) -> str:
        # EHLO names this side by its address literal (RFC 5321 4.1.3), never a looked-up name.
        own = sock.getsockname()[0]
        return f"[IPv6:{own}]" if ":" in own else f"[{own}]"

    if scheme == "smtps":

        class Implicit(smtplib.SMTP_SSL):
            def _get_socket(self, host, port, timeout):
                sock = raw_socket(port, timeout)
                self.local_hostname = literal(sock)
                return self.context.wrap_socket(sock, server_hostname=host)

        client: smtplib.SMTP = Implicit(
            local_hostname="localhost", timeout=COMMAND_TIMEOUT_S, context=context
        )
    else:

        class Starttls(smtplib.SMTP):
            def _get_socket(self, host, port, timeout):
                self._host = host  # STARTTLS's server_hostname: the configured name
                sock = raw_socket(port, timeout)
                self.local_hostname = literal(sock)
                return sock

        client = Starttls(local_hostname="localhost", timeout=COMMAND_TIMEOUT_S)
        client.tls_context = context  # type: ignore[attr-defined]
    client.set_debuglevel(0)  # never a transcript: it would hold the AUTH exchange
    return client


class _Fail(Exception):
    """A refusal with nothing submitted."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _open(client: smtplib.SMTP, scheme: str, host: str, port: int, account: str, password: str):
    """Connect, TLS, AUTH. Raises `_Fail` (or a socket / TLS / SMTP error) before any envelope."""
    client.connect(host, port)
    client.ehlo()
    if scheme == "smtp":
        if not client.has_extn("starttls"):
            raise _Fail("tls_required")
        try:
            code, _ = client.starttls(context=client.tls_context)  # type: ignore[attr-defined]
        except smtplib.SMTPResponseException:
            raise _Fail("tls_required") from None  # refused (454, 501…): never plaintext instead
        if code != 220:
            raise _Fail("tls_required")
        client.ehlo()
    if not isinstance(client.sock, ssl.SSLSocket):
        raise _Fail("tls_required")  # never AUTH on a plaintext socket
    offered = (client.esmtp_features.get("auth") or "").upper().split()
    mech = "PLAIN" if "PLAIN" in offered else "LOGIN" if "LOGIN" in offered else None
    if mech is None:
        raise _Fail("no_auth")
    client.user, client.password = account, password
    try:
        client.auth(mech, getattr(client, f"auth_{mech.lower()}"))
    except smtplib.SMTPAuthenticationError:
        raise _Fail("rejected") from None
    finally:
        client.password = ""


def _pre_code(e: BaseException, watch: _Watch) -> str:
    if watch.expired:
        return "timeout"
    if isinstance(e, ssl.SSLCertVerificationError):
        return "tls_failed"
    if isinstance(e, ssl.SSLError):
        return "tls_failed"
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return "tls_required"
    if isinstance(e, TimeoutError):
        return "timeout"
    if isinstance(e, ConnectionRefusedError):
        return "unreachable"
    if isinstance(e, smtplib.SMTPResponseException):
        return "refused"
    return "unreachable"


def _refusal(code: int) -> str:
    return "deferred" if 400 <= code < 500 else "refused"


def _probe_session(scheme: str, address: str, host: str, port: int, account: str, password: str):
    watch = _Watch()
    client = _client(scheme, address, watch)
    watch.start()
    try:
        _open(client, scheme, host, port, account, password)
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            pass
    except _Fail as e:
        raise ConnectorError(e.code, _DETAILS.get(e.code, f"{host} refused")) from None
    except (OSError, smtplib.SMTPException) as e:
        code = _pre_code(e, watch)
        raise ConnectorError(code, _DETAILS.get(code, f"{host} could not be reached")) from None
    finally:
        watch.cancel()
        client.close()


def _session(
    scheme: str,
    address: str,
    host: str,
    port: int,
    account: str,
    password: str,
    *,
    sender: str,
    to: str,
    text: str,
    msg_id: str,
    final_check=None,
) -> dict:
    """One submission. Runs in a worker thread; returns an outcome, never raises.

    ``final_check(fenced)`` (#1833's outbound boundary, Hermes 7610) runs after the server's 354
    and before the message is written. A refusal closes the connection without the end-of-data
    line, so the server never takes the message, and the outcome is `failed`."""
    watch = _Watch()
    client = _client(scheme, address, watch)
    watch.start()
    phase = "open"
    try:
        _open(client, scheme, host, port, account, password)
        phase = "envelope"
        eight = client.has_extn("8bitmime")
        raw = build(sender=sender, to=to, text=text, msg_id=msg_id, eight_bit=eight)
        if len(raw) > WIRE_MAX:
            raise _Fail("too_large")
        limit = (client.esmtp_features.get("size") or "").strip()
        if limit.isdigit() and 0 < int(limit) < len(raw):
            raise _Fail("too_large")
        options = []
        if eight and b"Content-Transfer-Encoding: 8bit" in raw:
            options.append("BODY=8BITMIME")
        if limit.isdigit():
            options.append(f"SIZE={len(raw)}")
        code, _ = client.mail(sender, options)
        if code != 250:
            raise _Fail(_refusal(code))
        code, _ = client.rcpt(to)
        if code not in (250, 251):
            raise _Fail(_refusal(code))
        payload = _stuff(raw)
        # From here the server may hold the message: nothing below is ever retried.
        phase = "data"
        code, _ = client.docmd("data")
        if code != 354:
            phase = "envelope"  # DATA refused: the message itself was never written
            raise _Fail(_refusal(code))
        if final_check is not None:
            phase = "envelope"  # nothing of the message is written until this passes
            final_check(True)
        phase = "written"
        client.send(payload)  # THE one write of the message
        code, reply = client.getreply()
        if code != 250:
            return {"outcome": "unconfirmed", "code": "no_final_ok", "message_id": ""}
        phase = "done"
        text_reply = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else ""
        found = _QUEUED_RE.search(text_reply)
        mid = found.group(1) if found else f"<{msg_id}@{_MSGID_DOMAIN}>"
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            pass  # accepted already: a drop after the final 250 is still `sent`
        return {"outcome": "sent", "code": "", "message_id": mid[:255]}
    except _Fail as e:
        return {"outcome": "failed", "code": e.code, "message_id": ""}
    except ConnectorError as e:
        return {"outcome": "failed", "code": e.code[:40], "message_id": ""}
    except (OSError, smtplib.SMTPException) as e:
        if phase in ("data", "written"):
            return {"outcome": "unconfirmed", "code": "no_answer", "message_id": ""}
        return {"outcome": "failed", "code": _pre_code(e, watch), "message_id": ""}
    except Exception:  # noqa: BLE001 — an outcome, whatever happened
        if phase in ("data", "written", "done"):
            return {"outcome": "unconfirmed", "code": "error", "message_id": ""}
        return {"outcome": "failed", "code": "error", "message_id": ""}
    finally:
        watch.cancel()
        client.close()


_DETAILS = {
    "tls_required": "the server does not offer TLS, so nothing is sent to it",
    "tls_failed": "the server's certificate could not be verified for its name",
    "no_auth": "the server offers no sign-in BattleLab uses (PLAIN or LOGIN)",
    "rejected": "the server rejected the account or password",
    "timeout": "the server did not answer in time",
    "unreachable": "the server could not be reached",
    "refused": "the server refused the connection",
}


# ---- the entry points ----------------------------------------------------------------------------


def _credential(bound: Bound, *, for_send: bool) -> tuple[dict, str, str]:
    """``(instance, account, password)`` at THIS generation, fenced like `connector_http`'s. A
    notify copy (#1841, ``bound.for_notify``) is fenced on its route, never on ``send``."""
    from .connectors import store

    notify = bound.for_notify if for_send else ""
    try:
        inst, password = store.token_for(
            bound.instance_id,
            bound.generation,
            for_read=False,
            for_send=for_send and not notify,
            for_notify=notify,
            destination=bound.destination,
        )
    except store.NotFound:
        raise ConnectorError("stale", "the connector was removed") from None
    except store.Refused as e:
        raise ConnectorError(e.code, str(e)) from None
    except store.StoreUnreadable as e:
        raise ConnectorError("store_unreadable", str(e)) from None
    if inst["origin"] != bound.origin or "sender" not in inst:
        raise ConnectorError("stale", "the connector changed since this was asked")
    account = inst.get("account") or ""
    if not account:
        raise ConnectorError("needs_account", "the connector's account needs to be entered")
    return inst, account, password


async def probe(bound: Bound) -> dict:
    """Save and test: connect, TLS, AUTH, QUIT, sending nothing. The login is the account."""
    if bound.adapter != "smtp":
        raise ConnectorError("refused", "not a mail connector")
    scheme, host, port = _target(bound.origin)
    address = await _vet(host, port)
    _inst, account, password = await asyncio.to_thread(_credential, bound, for_send=False)
    await asyncio.to_thread(_probe_session, scheme, address, host, port, account, password)
    return {"login": account, "repos": 0}


async def _send(
    bound: Bound, recipient: str, text: str, idem_key: str, sender: str, *, final_check=None
) -> dict:
    """THE mail send (#1836): one submission per call, never repeated. ``sender`` is the From
    address the card showed; the store's must still equal it at this last fence."""
    if bound.adapter != "smtp" or not (bound.for_send or bound.for_notify == "send"):
        return {"outcome": "failed", "code": "refused", "message_id": ""}
    if not re.fullmatch(r"[0-9a-f]{32}", idem_key):
        return {"outcome": "failed", "code": "refused", "message_id": ""}
    try:
        scheme, host, port = _target(bound.origin)
        address = await _vet(host, port)
        inst, account, password = await asyncio.to_thread(_credential, bound, for_send=True)
    except ConnectorError as e:
        return {"outcome": "failed", "code": e.code[:40], "message_id": ""}
    # Out of date, nothing sent: the codes `ask_messages.FENCE_REASONS` reads as `stale`.
    if inst["sender"] != sender:
        return {"outcome": "failed", "code": "connector_changed", "message_id": ""}
    if recipient not in {r["id"] for r in inst["recipients"]}:
        return {"outcome": "failed", "code": "recipient_withdrawn", "message_id": ""}
    if final_check is not None:
        try:
            await asyncio.to_thread(final_check, True)
        except ConnectorError as e:
            return {"outcome": "failed", "code": e.code[:40], "message_id": ""}
    return await asyncio.to_thread(
        _session, scheme, address, host, port, account, password,
        sender=sender, to=recipient, text=text, msg_id=idem_key, final_check=final_check,
    )  # fmt: skip
