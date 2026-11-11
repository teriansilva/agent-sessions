"""A fake SMTP server for #1836's tests: real sockets on 127.0.0.1 and real TLS, with a CA and a
server certificate generated per test (`cryptography`). It records every command, every AUTH it
was given, and every message it received, and can be told to misbehave at each step."""

from __future__ import annotations

import base64
import datetime
import socket
import socketserver
import ssl
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

NAME = "mail.example.test"


def make_certs(tmp: Path, name: str = NAME) -> tuple[str, str, str]:
    """``(ca file, server cert file, server key file)``: a CA and a leaf for ``name``."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BattleLab test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )  # fmt: skip
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, cert_file, key_file = tmp / "ca.pem", tmp / "server.pem", tmp / "server.key"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(ca_file), str(cert_file), str(key_file)


class FakeSMTP:
    """``mode``: ``smtps`` (implicit TLS) or ``starttls``. Knobs, all off by default:

    * ``offer_starttls`` (False: a plaintext-only server), ``starttls_code`` (454: refuses it);
    * ``auth`` (the mechanisms offered), ``password`` (what AUTH accepts);
    * ``mail_code`` / ``rcpt_code`` / ``data_code`` (the reply to each), ``size`` (SIZE offered);
    * ``eight_bit`` (8BITMIME offered);
    * ``final``: ``"250"`` (default), ``"hang"`` (no reply after the data), ``"drop"`` (close
      after the data), ``"550"``; ``drop_after_final``: close after the final 250, before QUIT.
    """

    def __init__(self, tmp: Path, *, mode: str = "smtps", name: str = NAME) -> None:
        self.ca, cert, key = make_certs(tmp, name)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.mode = mode
        self.offer_starttls = True
        self.starttls_code = 220
        self.auth = "PLAIN LOGIN"
        self.account = "hq@example.com"
        self.password = "app-password-123456"
        self.mail_code = 250
        self.rcpt_code = 250
        self.data_code = 354
        self.size: int | None = None
        self.eight_bit = True
        self.final = "250"
        self.drop_after_final = False
        self.commands: list[str] = []
        self.auths: list[tuple[str, str]] = []
        self.messages: list[bytes] = []
        self.envelopes: list[tuple[str, list[str]]] = []
        self.data_commands = 0
        self.connections = 0
        self.tls_ok: list[bool] = []
        self.release = threading.Event()
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                fake._serve(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def origin(self) -> str:
        scheme = "smtps" if self.mode == "smtps" else "smtp"
        return f"{scheme}://{NAME}:{self.port}"

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    # ---- the session ----

    def _serve(self, sock: socket.socket) -> None:
        self.connections += 1
        sock.settimeout(30)
        tls = False
        if self.mode == "smtps":
            try:
                sock = self.ctx.wrap_socket(sock, server_side=True)
            except (ssl.SSLError, OSError):
                self.tls_ok.append(False)
                return
            tls = True
            self.tls_ok.append(True)
        f = sock.makefile("rb")

        def say(line: str) -> None:
            sock.sendall(line.encode() + b"\r\n")

        say(f"220 {NAME} ESMTP fake")
        sender, rcpts = "", []
        try:
            while True:
                raw = f.readline(8192)
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                verb = line.split(" ", 1)[0].upper()
                self.commands.append(verb if verb == "AUTH" else line)
                if verb in ("EHLO", "HELO"):
                    feats = [NAME]
                    if self.mode == "starttls" and not tls and self.offer_starttls:
                        feats.append("STARTTLS")
                    if self.auth:
                        feats.append(f"AUTH {self.auth}")
                    if self.eight_bit:
                        feats.append("8BITMIME")
                    if self.size is not None:
                        feats.append(f"SIZE {self.size}")
                    for i, ft in enumerate(feats):
                        say(f"250{'-' if i < len(feats) - 1 else ' '}{ft}")
                elif verb == "STARTTLS":
                    if self.starttls_code != 220 or not self.offer_starttls:
                        say(f"{self.starttls_code or 502} no TLS today")
                        continue
                    say("220 go ahead")
                    try:
                        sock = self.ctx.wrap_socket(sock, server_side=True)
                    except (ssl.SSLError, OSError):
                        self.tls_ok.append(False)
                        return
                    tls = True
                    self.tls_ok.append(True)
                    f = sock.makefile("rb")
                elif verb == "AUTH":
                    parts = line.split(" ")
                    mech = parts[1].upper()
                    if mech == "PLAIN":
                        blob = parts[2] if len(parts) > 2 else None
                        if blob is None:
                            say("334 ")
                            blob = f.readline().decode().strip()
                        _, user, pw = base64.b64decode(blob).decode().split("\0")
                    else:
                        if len(parts) > 2:  # LOGIN with the user name as initial response
                            user = base64.b64decode(parts[2]).decode()
                        else:
                            say("334 " + base64.b64encode(b"Username:").decode())
                            user = base64.b64decode(f.readline().strip()).decode()
                        say("334 " + base64.b64encode(b"Password:").decode())
                        pw = base64.b64decode(f.readline().strip()).decode()
                    self.auths.append((user, pw))
                    if (user, pw) == (self.account, self.password) and tls:
                        say("235 ok")
                    else:
                        say("535 bad credentials")
                elif verb == "MAIL":
                    sender = line[len("MAIL FROM:") :].split(" ")[0].strip("<>")
                    say(f"{self.mail_code} sender")
                elif verb == "RCPT":
                    rcpts.append(line[len("RCPT TO:") :].strip().strip("<>"))
                    say(f"{self.rcpt_code} recipient")
                elif verb == "DATA":
                    self.data_commands += 1
                    say(f"{self.data_code} go on")
                    if self.data_code != 354:
                        continue
                    buf = bytearray()
                    while True:
                        chunk = f.readline(1 << 20)
                        if not chunk:
                            # The client went away before the end-of-data line: like a real
                            # server, nothing is taken.
                            return
                        if chunk == b".\r\n":
                            break
                        buf += chunk[1:] if chunk.startswith(b"..") else chunk
                    self.messages.append(bytes(buf))
                    self.envelopes.append((sender, list(rcpts)))
                    if self.final == "hang":
                        self.release.wait(120)
                        return
                    if self.final == "drop":
                        return
                    if self.final != "250":
                        say(f"{self.final} rejected")
                        continue
                    say("250 2.0.0 Ok: queued as 4ABC123")
                    if self.drop_after_final:
                        return
                elif verb == "RSET":
                    sender, rcpts = "", []
                    say("250 ok")
                elif verb == "QUIT":
                    say("221 bye")
                    return
                else:
                    say("502 not here")
        except (OSError, ValueError):
            return
        finally:
            try:
                sock.close()
            except OSError:
                pass
