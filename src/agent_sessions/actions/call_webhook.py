"""Call a webhook (#2248 P2): one HMAC-signed POST of typed fields to a destination you set.

``inputs = {destination, allow_private?, fields}``:

* ``destination`` — an ``https`` URL the operator saves in the automation (never a credential in
  it, never a fragment). It is part of the consent: a different address is a different grant. A
  delivery can never change it; it fills ``fields`` only.
* ``allow_private`` — off by default. While it is off, an address that resolves to a private,
  loopback, shared or otherwise non-public network is refused at save (a literal address) and at
  every run (what the name resolves to then). Link-local and cloud-metadata addresses are refused
  always (`net_vetting`).
* ``fields`` — fixed values; ``from_delivery`` maps more names to a webhook delivery's typed values
  (the P2a types: number, choice, short text, yes/no), carried in the body with their type.

**The request** (`_post`, the one request site, inventoried in ``tests/test_prompts_registry.py``):
``POST`` with a JSON body ``{event, automation, run, sent_at, fields}``, signed with HMAC-SHA256
under the destination's own secret (`destination_secrets`, made at save and shown once) in
``X-BattleLab-Signature: sha256=<hex>``. The name is resolved once and the connection pinned to
the vetted address (TLS still verified against the name); no ambient proxy, no redirects (a
3xx is reported, never followed), a short deadline, a small reply read and discarded.

**Authority at the effect boundary.** Under the effect lock, the saved configuration and the
destination's secret are read, the run's inputs are recorded, and the automation's authority is
read again after the destination is vetted and right before the first byte (the transport trace,
as `connector_http` does). A reply that never came back is ``failed`` with "may have been
delivered" and is never sent again.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .. import automations as model
from .. import net_vetting, webhook_events
from . import destination_secrets, plugins

ID = "call_webhook"
LABEL = "Call a webhook"
WEBHOOK = True
MESSAGE = False
SECRET_REFUSAL = ""
TARGET_WORDS = {
    "destination": "a different destination",
    "allow_private": "which network addresses it may reach changed",
}
_INPUTS = {"destination", "allow_private", "fields"}

URL_MAX = 2000
FIELD_VALUE_MAX = 2000
FIELDS_MAX = 16
BODY_MAX = 64 * 1024
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 15.0
REPLY_READ_MAX = 4096
SIGNATURE_HEADER = "X-BattleLab-Signature"

#: Test seams: a mock transport, and name resolution.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


# ---- the destination ----------------------------------------------------------------------------


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """A public address. An IPv6 address that carries an IPv4 one (mapped, 6to4, NAT64) is
    judged as that IPv4 address (`net_vetting.embedded_ipv4`, the rule `unsafe_ip` applies): a
    translator delivers to it, so ``64:ff9b::a00:5`` is 10.0.0.5, never a public address."""
    embedded = net_vetting.embedded_ipv4(ip)
    return (embedded if embedded is not None else ip).is_global


def normalize_destination(raw: object) -> str:
    """The destination as stored: ``https://host[:port]/path[?query]``, host lower-cased. Refuses
    anything else, so the consent shows exactly the address a request goes to."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > URL_MAX:
        raise model.AutomationError(f"destination must be an https address of at most {URL_MAX}")
    raw = raw.strip()
    if any(ord(c) < 33 or ord(c) == 127 for c in raw):
        raise model.AutomationError("destination must not contain spaces or control characters")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise model.AutomationError("destination is not a valid address") from None
    if parts.scheme.lower() != "https":
        raise model.AutomationError("destination must be an https address")
    if not parts.hostname:
        raise model.AutomationError("destination needs a host")
    if parts.username is not None or parts.password is not None:
        raise model.AutomationError(
            "destination must not carry a user name or password; the request is signed instead"
        )
    if parts.fragment:
        raise model.AutomationError("destination must not have a #fragment")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


def _literal_problem(destination: str, allow_private: bool) -> str | None:
    """A destination written as an address that is refused (checked at save; a NAME is checked
    against what it resolves to at every run)."""
    host = urlsplit(destination).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if net_vetting.unsafe_ip(ip) and not net_vetting.is_loopback(ip):
        return "destination is a link-local, metadata, multicast or reserved address"
    if not allow_private and not _public(ip):
        return (
            "destination is a private or local network address; allow private addresses for "
            "this action if you mean it"
        )
    return None


# ---- validation and save ------------------------------------------------------------------------


def validate_inputs(inputs: dict, mapped: dict) -> dict:
    model._obj(inputs, "a call_webhook action's inputs", _INPUTS, {"destination"})
    destination = normalize_destination(inputs["destination"])
    allow_private = inputs.get("allow_private", False)
    if not isinstance(allow_private, bool):
        raise model.AutomationError("allow_private must be true or false")
    why = _literal_problem(destination, allow_private)
    if why:
        raise model.AutomationError(why)
    fields = inputs.get("fields", {})
    if not isinstance(fields, dict) or len(fields) > FIELDS_MAX:
        raise model.AutomationError(f"fields must be an object of at most {FIELDS_MAX} fields")
    clean: dict[str, str] = {}
    for k, v in fields.items():
        if not isinstance(k, str) or not webhook_events.FIELD_RE.match(k):
            raise model.AutomationError(f"{k!r} is not a field name (a-z, 0-9, _)")
        clean[k] = model._text(v, f"field {k}", FIELD_VALUE_MAX, multiline=True)
    both = sorted(set(mapped) & set(clean))
    if both:
        raise model.AutomationError(f"{both} has a fixed value and a value from the delivery")
    if len(clean) + len(mapped) > FIELDS_MAX:
        raise model.AutomationError(f"at most {FIELDS_MAX} fields in all")
    return {
        "destination": destination,
        "allow_private": allow_private,
        "fields": dict(sorted(clean.items())),
    }


def mappable(_inputs: dict) -> None:
    return None  # any field name `validate_inputs` accepted


def _withheld(value: str) -> bool | None:
    """Whether a fixed value would be WITHHELD by the rule every persisted text goes through
    (`ask_store.guard_text`: a stored secret, or a credential shape). None: it can't be judged."""
    from .. import ask_store

    try:
        return ask_store.guard_text(value, len(value)) != value
    except ask_store.StoreRefused:
        return None


def check_save(config: dict) -> None:
    """The destination is fully checked by `validate_inputs` (its name at each run). A fixed field
    value that looks like a credential, or holds one of your stored secrets, is refused here: it
    would be stored in the automation and sent in clear in every request body."""
    for name, value in config["action"]["inputs"]["fields"].items():
        withheld = _withheld(value)
        if withheld is None:
            raise model.AutomationError(
                "the fields can't be checked for secrets right now; try again", status=503
            )
        if withheld:
            raise model.AutomationError(
                f"field {name} looks like a credential or holds a stored secret: a fixed value is "
                "kept in the automation and sent in every request; the signature already proves "
                "who sent it"
            )


# ---- pins, scope, consent -----------------------------------------------------------------------


def pins(_action: dict) -> None:
    return None


def drift(_stored: dict, _current: dict) -> str:  # pragma: no cover — no pins
    return ""


def target(action: dict, _pins: dict) -> dict:
    inp = action["inputs"]
    return {"destination": inp["destination"], "allow_private": inp["allow_private"]}


def data(action: dict) -> dict:
    return {"fields": action["inputs"]["fields"]}


def lines(scope: dict) -> list[str]:
    t = scope.get("target") or {}
    fields = (scope.get("inputs") or {}).get("fields")
    out = [f"Sends one signed POST to {t.get('destination')}"]
    if t.get("allow_private") is True:
        out.append("It may reach a private or local network address: you allowed it")
    elif t.get("allow_private") is False:
        out.append("Never to a private, local or link-local network address")
    else:
        out.append(f"Private network addresses: {t.get('allow_private')!r}")
    out.append(
        f"Signed with HMAC-SHA256 in {SIGNATURE_HEADER}, with a secret made for this destination "
        "and shown to you once"
    )
    if isinstance(fields, dict) and fields:
        out.append("The JSON body carries these fixed fields:")
        out.extend(f"  {k} = {v}" for k, v in sorted(fields.items()))
    elif isinstance(fields, dict):
        out.append("The JSON body carries no fixed fields")
    else:
        out.append(f"Fields: {fields!r}")
    out.append(
        "It follows no redirect and waits at most "
        f"{int(TOTAL_TIMEOUT_S)} s; a delivery never changes the address"
    )
    return out


def fill(action: dict, delivered: dict) -> dict:
    """This run's delivered values, typed again for the JSON body (a number as a number, yes/no as
    true/false), beside the fixed fields."""
    mapping = action.get("from_delivery") or {}
    typed: dict[str, object] = {}
    for k, v in delivered.items():
        kind = (mapping.get(k) or {}).get("type")
        if kind == "number":
            num = float(v)
            typed[k] = int(num) if num.is_integer() and re.fullmatch(r"-?\d+", v) else num
        elif kind == "yes_no":
            typed[k] = v == "yes"
        else:
            typed[k] = v
    inp = action["inputs"]
    fields = dict(sorted({**inp["fields"], **typed}.items()))
    return {**action, "inputs": {**inp, "fields": fields}}


# ---- the request --------------------------------------------------------------------------------


class _Refused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


_PRE_WRITE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def _post(
    url: str, body: bytes, sig: str, delivery: str, *, allow_private: bool, final_check
) -> dict:
    """THE request (#2248 P2): ``{outcome: sent|failed|unconfirmed, code, status}``. ``failed``
    means it is PROVEN nothing was written (refused before a byte left, or the destination answered
    4xx/3xx); anything after the request may have been written is ``unconfirmed``."""
    if urlsplit(url).scheme != "https":
        return {"outcome": "failed", "code": "https_required", "status": None}
    seen: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        addrs = await _resolve(host, port)
        seen.extend(addrs)
        return addrs

    # Until the request is handed to the client nothing can have been written: a timeout or an
    # error before then (resolving, vetting, the final check) is `failed: not_sent`, never
    # "may have been delivered".
    handed = False
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            dest = await net_vetting.vet(
                url, resolve=resolve, error=_Refused, policy=net_vetting.ANY_HTTP
            )
            if not allow_private:
                for a in seen:
                    if not _public(ipaddress.ip_address(a.split("%")[0])):
                        raise _Refused("private_address")
            final_check(True)
            headers = {
                "Host": dest.host_header,
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "BattleLab-Automation",
                "X-BattleLab-Event": "automation",
                "X-BattleLab-Delivery": delivery,
                SIGNATURE_HEADER: sig,
            }

            async def trace(name: str, _info: dict) -> None:
                if name.endswith("send_request_headers.started"):
                    final_check(False)  # raises before the first byte is written

            extensions: dict = {"trace": trace}
            if dest.sni:
                extensions["sni_hostname"] = dest.sni
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                transport=_TRANSPORT,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                req = client.build_request(
                    "POST", dest.url, headers=headers, content=body, extensions=extensions
                )
                handed = True
                try:
                    resp = await client.send(req, stream=True)
                except _PRE_WRITE:
                    return {"outcome": "failed", "code": "unreachable", "status": None}
                try:
                    status = resp.status_code
                    got = 0
                    async for chunk in resp.aiter_bytes():
                        got += len(chunk)
                        if got > REPLY_READ_MAX:
                            break
                finally:
                    await resp.aclose()
    except _Refused as e:
        return {"outcome": "failed", "code": e.code[:40], "status": None}
    except (TimeoutError, httpx.HTTPError):
        if not handed:
            return {"outcome": "failed", "code": "not_sent", "status": None}
        return {"outcome": "unconfirmed", "code": "no_answer", "status": None}
    if 200 <= status < 300:
        return {"outcome": "sent", "code": "", "status": status}
    if 300 <= status < 400:
        return {"outcome": "failed", "code": "redirect", "status": status}
    if 400 <= status < 500:
        return {"outcome": "failed", "code": "rejected", "status": status}
    return {"outcome": "unconfirmed", "code": "server_error", "status": status}


_WORDS = {
    "private_address": (
        "the destination resolves to a private or local network address, which this action "
        "may not reach"
    ),
    "refused_destination": "the destination resolves to an address that is always refused",
    "unreachable": "the destination could not be reached",
    "invalid_url": "the destination is not a valid address",
    "https_required": "the destination is not https",
    "not_sent": "the destination's address could not be resolved and checked in time",
}
_STOPPED = "automation_stopped"


def effect(run: dict, config: dict, pins_: dict) -> tuple[str, str]:
    """BLOCKING, under the effect lock, after the authority read."""
    from .. import ask_store
    from .. import automations_store as store

    inp = config["action"]["inputs"]
    dest = inp["destination"]
    secret = destination_secrets.signing_secret(run["automation_id"], dest)
    if secret is None:
        return "refused", (
            "this destination has no signing secret; make a new one on the automation's page. "
            "Nothing was sent"
        )
    fields = inp["fields"]
    for k, v in fields.items():
        if isinstance(v, str):
            try:
                clean = ask_store.guard_text(v, len(v)) == v
            except ask_store.StoreRefused:
                return "refused", "the fields could not be checked for secrets; nothing was sent"
            if not clean:
                return "refused", f"field {k} holds a secret or a credential; nothing was sent"
    body = json.dumps(
        {
            "event": "battlelab.automation",
            "automation": run["automation_id"],
            "run": run["id"],
            "sent_at": int(time.time()),
            "fields": fields,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(body) > BODY_MAX:
        return "refused", f"the body is over {BODY_MAX // 1024} KiB; nothing was sent"
    # Recorded BEFORE the request: the destination and the fields, never the secret or signature.
    store.set_inputs(
        run["id"],
        {
            "destination": dest,
            "fields": {k: str(v) for k, v in fields.items()},
            "body_sha256": hashlib.sha256(body).hexdigest(),
        },
    )
    stop = plugins.final(run)
    if stop:
        return stop
    stopped: list = []

    def final_check(_fenced: bool) -> None:
        out = plugins.final(run)
        if out:
            stopped[:] = [out]
            raise _Refused(_STOPPED)

    out = plugins.run_async(
        _post(
            dest,
            body,
            signature(secret, body),
            run["id"],
            allow_private=inp["allow_private"],
            final_check=final_check,
        )
    )
    code, status = out.get("code") or "", out.get("status")
    if out["outcome"] == "sent":
        return plugins.delivered(run, "ok", f"delivered to {dest} ({status})")
    if out["outcome"] == "failed":
        if code == _STOPPED and stopped:
            return stopped[0]
        if code == "redirect":
            return "failed", (
                f"the destination answered with a redirect ({status}), which is never followed"
            )
        if code == "rejected":
            return "failed", f"the destination refused it ({status})"
        if code in ("private_address", "refused_destination", "https_required", "invalid_url"):
            return "refused", f"{_WORDS[code]}; nothing was sent"
        return "failed", f"{_WORDS.get(code, code or 'it failed')}; nothing was sent"
    return "failed", (
        f"no clear answer came back ({'status ' + str(status) if status else code}); "
        "it may have been delivered and is not sent again"
    )
