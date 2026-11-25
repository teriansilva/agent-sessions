"""The connector store: ``connectors.json`` (#1750).

``AGENT_SESSIONS_CONNECTORS_STORE`` (default ``~/.config/agent-sessions/connectors.json``): 0600,
read-modify-write under ``atomicjson.json_write_lock``, an atomic fsynced replace, and a STRICT
read. A file that exists but does not parse, or holds a record that does not validate, is
**unreadable**: every route and HQ tool refuses (`StoreUnreadable`) and the file is never
rewritten, so a damaged store is never "repaired" into an empty one that drops a credential.

An instance::

    {"id", "manifest_id", "manifest_version", "origin", "generation", "credential_revision",
     "tested": None | {"generation", "manifest_version", "login", "repos", "at"},
     "last_test": None | {"generation", "ok", "code", "at"},
     "enabled": {"read": bool}, "envelopes": {"token": {kid, nonce, ct}}, "created_at"}

An email connector (#1836) also has ``account``, the messenger keys and ``sender`` (its From
address); its origin is ``smtps://`` or ``smtp://`` (`kinds.ORIGIN_SCHEMES`) and its recipients
are addresses the Commander typed (`set_addresses`), never discovered.

**Generation vs credential revision.** ``generation`` moves on ANY change to what a test proved:
origin, token, or the bundled manifest's version. ``credential_revision`` moves only when the
token or origin changes, and is part of the token's AAD (`secrets.aad`). So a manifest bump clears
``tested`` (a re-test is needed) and keeps the token decryptable, while an origin change needs the
token again and the old envelope never decrypts under the new revision.

**Tested is bound to its generation.** `settle_test` records a probe's result only when the
generation is still the one the probe read, so a probe of an old origin or token can never mark a
newer configuration tested. ``read`` counts as enabled only while ``tested`` is current
(`readable`); enabling requires it, inside the lock.

**Lock order:** ``template_secrets.registration_fence`` → this store's lock. A token write holds
both, so it joins the redaction set strictly before or after any redacting write.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import secrets as _random
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

from .. import atomicjson, net_vetting, template_secrets
from . import kinds
from . import manifest as manifest_mod
from . import secrets as creds

STORE_VERSION = 1
INSTANCES_MAX = 32
AUDIT_MAX = 200
LOGIN_MAX = 80
#: A `basic` connector's account name (#1829): plain text, never secret, never a ':' (it is the
#: user part of a Basic credential), no leading or trailing space.
ACCOUNT_RE = re.compile(r"[A-Za-z0-9_.@'-](?:[A-Za-z0-9_.@' -]{0,62}[A-Za-z0-9_.@'-])?")
ID_RE = re.compile(r"c_[0-9a-f]{12}")
CAPABILITIES = ("read",)
#: A send-capable (messenger) connector's capabilities (#1832). A connector's record has exactly
#: its manifest's: a read-only record stays `{"read"}` and is never rewritten with `send`.
SEND_CAPABILITIES = ("read", "send")
#: The keys only a messenger instance has (#1832), all together: who it may reach, which bot and
#: origin that choice was made for, and why sending was last switched off by a change of either.
MESSENGER_KEYS = frozenset({"recipients", "bound", "send_off"})
RECIPIENTS_MAX = 20
RECIPIENT_ID_RE = re.compile(r"[!A-Za-z0-9:._=+/-]{1,255}")
RECIPIENT_KINDS = frozenset({"private chat", "group", "supergroup", "channel", "room"})
LABEL_MAX = 100
SEND_OFF_REASONS = frozenset({"identity_changed", "origin_changed"})
#: An email connector's own key (#1836): the From address every message shows. ``""`` until set.
EMAIL_KEYS = frozenset({"sender"})
#: A typed recipient's kind (#1836): one plain address, which is also its id.
EMAIL_KIND = "email"
ADDRESS_MAX = 254
#: ONE plain mailbox, ASCII only: a dot-atom local part (no quoted local part, no comment, no
#: display name, no space, no CR/LF) and a host name of at least two labels (no address literal).
#: Anything else is refused in words, never "cleaned" into a different address.
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
ADDRESS_RE = re.compile(rf"(?:{_ATOM}(?:\.{_ATOM})*)@(?:{_LABEL}(?:\.{_LABEL})+)")
#: The send rate cap per connector (#1833): at most this many sends claimed per window. A claim is
#: counted when it is TAKEN, before the send's `deciding` is written, so a crash between the two
#: can only make the cap stricter. `sends` (the claim times inside the window) exists only on a
#: messenger that has claimed one.
SEND_RATE_MAX = 20
SEND_RATE_WINDOW_S = 3600.0
#: A messenger's NOTIFY ROUTE (#1841, #1746 P3b): a short copy of each bell event to ONE saved
#: recipient the Commander says is their own. `notify` (the block) and `enabled.notify` exist only
#: on an instance that was ever configured for it, so a store without one is what an older build
#: reads. The block's fields, exactly:
NOTIFY_KEYS = frozenset(
    {"destination", "identity", "generation", "classes", "window_s", "quiet", "cap_per_hour",
     "include_line", "since", "off"}
)  # fmt: skip
#: The bell classes a route can carry (`hq_dispatch` classes; watches join in P3c).
NOTIFY_CLASSES = ("needs you", "mission escalation", "automation failed")
NOTIFY_WINDOWS = (0, 30, 60, 120, 300, 600)
NOTIFY_WINDOW_DEFAULT = 60
NOTIFY_CAP_MAX = 60
NOTIFY_CAP_DEFAULT = 20
#: Why a route went back to off by itself, in the same locked write as the change that caused it.
NOTIFY_OFF_REASONS = frozenset(
    {"connector_changed", "needs_test", "recipients_unbound", "destination_removed"}
)
_HHMM_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
#: The audit row's operations.
AUDIT_OPS = frozenset({"added", "changed", "tested", "enabled", "disabled", "removed"})
CODE_RE = re.compile(r"[a-z_]{1,40}")

_INSTANCE_KEYS = frozenset(
    {
        "id", "manifest_id", "manifest_version", "origin", "generation", "credential_revision",
        "tested", "last_test", "enabled", "envelopes", "created_at",
    }
)  # fmt: skip


class StoreUnreadable(RuntimeError):
    """The store exists but cannot be read completely. Nothing is read from it or written to it."""


class NotFound(LookupError):
    pass


class Refused(ValueError):
    """A mutation the store refuses; ``code`` is stable and short, the message content-free."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def store_path() -> Path:
    override = os.environ.get("AGENT_SESSIONS_CONNECTORS_STORE")
    if override:
        return Path(override)
    return Path.home() / ".config" / "agent-sessions" / "connectors.json"


# ---- origins -----------------------------------------------------------------------------------

_LOOPBACK_NAMES = frozenset({"localhost"})


def schemes_for(adapter: str | None) -> frozenset[str]:
    """The origin schemes an adapter kind may use (`kinds.ORIGIN_SCHEMES`; http(s) otherwise)."""
    return kinds.ORIGIN_SCHEMES.get(adapter or "", kinds.HTTP_SCHEMES)


def normalize_origin(raw: object, schemes: frozenset[str] = kinds.HTTP_SCHEMES) -> str:
    """``scheme://host[:port]``, lower-cased, or `Refused`. Only an origin: no path, query,
    fragment or credentials. A token-bearing origin is ``https`` — ``http`` only for a loopback
    literal or ``localhost`` (and `connector_http` checks again at every request that the name
    really resolves to loopback). A literal refused address (link-local, metadata, multicast,
    unspecified, reserved) is refused here; a NAME is vetted at every request.

    ``schemes`` is the kind's (`schemes_for`): an email connector (#1836) takes ``smtps://`` or
    ``smtp://`` only, and every other kind never takes either. TLS is always required for mail
    (``smtp://`` means STARTTLS that must succeed), so mail has no loopback exception to make."""
    mail = schemes != kinds.HTTP_SCHEMES
    want = "an smtps:// or smtp:// server" if mail else "an http(s) origin"
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 300:
        raise Refused(f"origin must be {want}", "invalid_origin")
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        raise Refused("origin is not a valid URL", "invalid_origin") from None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in schemes or not host:
        raise Refused(f"origin must be {want}", "invalid_origin")
    if parts.username or parts.password:
        raise Refused("origin must not carry credentials", "invalid_origin")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise Refused("origin must be an origin only, without a path", "invalid_origin")
    literal: net_vetting.IP | None
    try:
        import ipaddress

        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and net_vetting.unsafe_ip(literal):
        raise Refused("origin is a refused address (link-local or metadata)", "refused_destination")
    if scheme == "http" and not mail:
        loopback = (
            net_vetting.is_loopback(literal) if literal is not None else host in _LOOPBACK_NAMES
        )
        if not loopback:
            raise Refused(
                "a token is only sent over https (http only to this machine)", "https_required"
            )
    netloc = f"[{host}]" if literal is not None and literal.version == 6 else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


# ---- reading -----------------------------------------------------------------------------------


def _int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _valid_tested(t: object) -> bool:
    if t is None:
        return True
    return (
        isinstance(t, dict)
        and set(t) == {"generation", "manifest_version", "login", "repos", "at"}
        and _int(t["generation"])
        and isinstance(t["manifest_version"], str)
        and isinstance(t["login"], str)
        and len(t["login"]) <= LOGIN_MAX
        and _int(t["repos"])
        and isinstance(t["at"], int | float)
    )


def _valid_last_test(t: object) -> bool:
    if t is None:
        return True
    return (
        isinstance(t, dict)
        and set(t) == {"generation", "ok", "code", "at"}
        and _int(t["generation"])
        and isinstance(t["ok"], bool)
        and (t["code"] is None or (isinstance(t["code"], str) and CODE_RE.fullmatch(t["code"])))
        and isinstance(t["at"], int | float)
    )


def _valid_recipient(r: object) -> bool:
    return (
        isinstance(r, dict)
        and set(r) == {"id", "label", "kind"}
        and isinstance(r["id"], str)
        and RECIPIENT_ID_RE.fullmatch(r["id"]) is not None
        and isinstance(r["label"], str)
        and 0 < len(r["label"]) <= LABEL_MAX
        and r["label"].isprintable()
        and r["kind"] in RECIPIENT_KINDS
    )


def fit_recipient(rid: object, label: object, kind: object) -> dict | None:
    """An adapter's candidate in the stored shape, or None when it can't be one.

    The label is the server's own reading, made to fit: stripped, the id when empty or not
    printable, and cut to `LABEL_MAX` with an ellipsis. An id or kind the store can't hold is
    no candidate at all, so the page never offers what a save would refuse (#1832, Hermes 7603)."""
    if not isinstance(rid, str) or RECIPIENT_ID_RE.fullmatch(rid) is None:
        return None
    if kind not in RECIPIENT_KINDS:
        return None
    text = label.strip() if isinstance(label, str) else ""
    if not text or not text.isprintable():
        text = rid
    if len(text) > LABEL_MAX:
        text = text[: LABEL_MAX - 1] + "…"
    return {"id": rid, "label": text, "kind": kind}


def valid_address(raw: object) -> bool:
    """One plain mailbox (#1836): `ADDRESS_RE`, at most `ADDRESS_MAX` characters, its domain
    lower-case (as `normalize_address` stores it). Checked when saved AND again at every send."""
    if not isinstance(raw, str) or len(raw) > ADDRESS_MAX or not ADDRESS_RE.fullmatch(raw):
        return False
    local, _, domain = raw.rpartition("@")
    return len(local) <= 64 and domain == domain.lower()


def normalize_address(raw: object) -> str:
    """The address as stored: the domain lower-cased, the local part kept as typed (a local part
    can be case-sensitive), nothing else changed. `Refused` with the reason in words."""
    if not isinstance(raw, str) or not raw:
        raise Refused("enter an address, like name@example.com", "invalid_address")
    if raw != raw.strip() or any(c.isspace() for c in raw):
        raise Refused("an address has no spaces or line breaks", "invalid_address")
    if "<" in raw or ">" in raw or '"' in raw or "(" in raw or "," in raw or ";" in raw:
        raise Refused(
            "write one plain address, like name@example.com, without a name, quotes or a list",
            "invalid_address",
        )
    local, at, domain = raw.rpartition("@")
    out = f"{local}{at}{domain.lower()}"
    if not at or not valid_address(out):
        raise Refused("that isn't an address, like name@example.com", "invalid_address")
    return out


def _valid_email_recipient(r: object) -> bool:
    return (
        isinstance(r, dict)
        and set(r) == {"id", "label", "kind"}
        and valid_address(r["id"])
        and isinstance(r["label"], str)
        and 0 < len(r["label"]) <= LABEL_MAX
        and r["label"].isprintable()
        and r["kind"] == EMAIL_KIND
    )


def _valid_messenger(inst: dict) -> bool:
    rs = inst["recipients"]
    email = "sender" in inst
    if email and not (
        isinstance(inst["sender"], str) and (inst["sender"] == "" or valid_address(inst["sender"]))
    ):
        return False
    check = _valid_email_recipient if email else _valid_recipient
    if not isinstance(rs, list) or len(rs) > RECIPIENTS_MAX or not all(map(check, rs)):
        return False
    if len({r["id"] for r in rs}) != len(rs):
        return False
    b = inst["bound"]
    if b is not None and not (
        isinstance(b, dict)
        and set(b) == {"origin", "identity"}
        and all(isinstance(v, str) and len(v) <= 300 for v in b.values())
    ):
        return False
    off = inst["send_off"]
    return off is None or (
        isinstance(off, dict)
        and set(off) == {"reason", "was", "now", "at"}
        and off["reason"] in SEND_OFF_REASONS
        and isinstance(off["was"], str)
        and isinstance(off["now"], str)
        and isinstance(off["at"], int | float)
    )


@functools.lru_cache(maxsize=1)
def _zones() -> frozenset[str]:
    import zoneinfo

    return frozenset(zoneinfo.available_timezones())


def valid_zone(tz: object) -> bool:
    """An IANA zone name this host knows (`zoneinfo.available_timezones()`)."""
    return isinstance(tz, str) and 0 < len(tz) <= 64 and tz in _zones()


def valid_quiet(q: object) -> bool:
    return q is None or (
        isinstance(q, dict)
        and set(q) == {"from", "to", "tz"}
        and isinstance(q["from"], str)
        and _HHMM_RE.fullmatch(q["from"]) is not None
        and isinstance(q["to"], str)
        and _HHMM_RE.fullmatch(q["to"]) is not None
        and q["from"] != q["to"]
        and valid_zone(q["tz"])
    )


def _valid_identity(ident: object) -> bool:
    """What notify was pinned to at enable: the destination's id, the label shown then, and for
    Matrix the ONE other joined member's user id (``member``; "" for Telegram and mail)."""
    return (
        isinstance(ident, dict)
        and set(ident) == {"id", "label", "member"}
        and isinstance(ident["id"], str)
        and 0 < len(ident["id"]) <= 255
        and isinstance(ident["label"], str)
        and len(ident["label"]) <= LABEL_MAX
        and ident["label"].isprintable()
        and isinstance(ident["member"], str)
        and len(ident["member"]) <= 255
    )


def _valid_notify(inst: dict) -> bool:
    n = inst["notify"]
    if not isinstance(n, dict) or set(n) != NOTIFY_KEYS:
        return False
    on = inst["enabled"].get("notify")
    if not isinstance(n["destination"], str) or len(n["destination"]) > 255:
        return False
    if n["destination"] == "":
        if on or n["identity"] is not None:
            return False  # notify is never on without a destination
    elif not _valid_identity(n["identity"]) or n["identity"]["id"] != n["destination"]:
        return False
    cls = n["classes"]
    if not (
        isinstance(cls, list)
        and all(isinstance(c, str) and c in NOTIFY_CLASSES for c in cls)
        and len(set(cls)) == len(cls)
    ):
        return False
    off = n["off"]
    return (
        _int(n["generation"])
        and isinstance(n["window_s"], int)
        and not isinstance(n["window_s"], bool)
        and n["window_s"] in NOTIFY_WINDOWS
        and valid_quiet(n["quiet"])
        and isinstance(n["cap_per_hour"], int)
        and not isinstance(n["cap_per_hour"], bool)
        and 1 <= n["cap_per_hour"] <= NOTIFY_CAP_MAX
        and isinstance(n["include_line"], bool)
        and isinstance(n["since"], int | float)
        and not isinstance(n["since"], bool)
        and (
            off is None
            or (
                isinstance(off, dict)
                and set(off) == {"reason", "at"}
                and off["reason"] in NOTIFY_OFF_REASONS
                and isinstance(off["at"], int | float)
            )
        )
    )


def _valid_instance(inst: object) -> bool:
    # `account` (#1829) is present only on a `basic` connector, so a store holding none of them is
    # byte-for-byte what an older build reads.
    # An email connector (#1836) has all of them plus `sender`: no older build reads it.
    keys = set(inst) if isinstance(inst, dict) else None
    email = _INSTANCE_KEYS | {"account"} | MESSENGER_KEYS | EMAIL_KEYS
    # A notify route (#1841) adds `notify` to a messenger, and only once one was configured.
    notify = keys is not None and "notify" in keys and MESSENGER_KEYS <= keys
    if notify:
        keys = keys - {"notify"}
    if keys not in (
        _INSTANCE_KEYS,
        _INSTANCE_KEYS | {"account"},
        _INSTANCE_KEYS | MESSENGER_KEYS,
        _INSTANCE_KEYS | MESSENGER_KEYS | {"sends"},
        email,
        email | {"sends"},
    ):
        return False
    if "sends" in keys and not (
        isinstance(inst["sends"], list)
        and len(inst["sends"]) <= SEND_RATE_MAX
        and all(isinstance(t, int | float) and not isinstance(t, bool) for t in inst["sends"])
    ):
        return False
    messenger = MESSENGER_KEYS <= keys
    if messenger and not _valid_messenger(inst):
        return False
    if "account" in inst and not (
        isinstance(inst["account"], str)
        and (inst["account"] == "" or ACCOUNT_RE.fullmatch(inst["account"]))
    ):
        return False
    if not isinstance(inst["id"], str) or not ID_RE.fullmatch(inst["id"]):
        return False
    if not isinstance(inst["manifest_id"], str) or not manifest_mod.ID_RE.fullmatch(
        inst["manifest_id"]
    ):
        return False
    if not isinstance(inst["manifest_version"], str):
        return False
    origin = inst["origin"]
    if not isinstance(origin, str):
        return False
    if origin:
        try:
            schemes = schemes_for("smtp") if "sender" in keys else kinds.HTTP_SCHEMES
            if normalize_origin(origin, schemes) != origin:
                return False
        except Refused:
            return False
    if not (_int(inst["generation"]) and _int(inst["credential_revision"])):
        return False
    if not (_valid_tested(inst["tested"]) and _valid_last_test(inst["last_test"])):
        return False
    enabled = inst["enabled"]
    want = set(SEND_CAPABILITIES if messenger else CAPABILITIES) | ({"notify"} if notify else set())
    if not isinstance(enabled, dict) or set(enabled) != want:
        return False
    if not all(isinstance(v, bool) for v in enabled.values()):
        return False
    if notify and not _valid_notify(inst):
        return False
    envs = inst["envelopes"]
    if not isinstance(envs, dict) or not set(envs) <= {"token"}:
        return False
    if not all(template_secrets.valid_envelope(e) for e in envs.values()):
        return False
    if envs and not origin:
        return False  # a token is never stored without the origin it is bound to
    return isinstance(inst["created_at"], int | float)


def _valid_audit(row: object) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == {"at", "op", "id", "manifest_id", "generation"}
        and isinstance(row["at"], int | float)
        and isinstance(row["op"], str)
        and row["op"] in AUDIT_OPS
        and isinstance(row["id"], str)
        and ID_RE.fullmatch(row["id"]) is not None
        and isinstance(row["manifest_id"], str)
        and _int(row["generation"])
    )


def _empty() -> dict:
    return {"version": STORE_VERSION, "instances": [], "audit": []}


def _read(path: Path) -> dict:
    """The whole document, strictly. Absent → empty; anything unreadable → `StoreUnreadable`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return _empty()
    except OSError as e:
        raise StoreUnreadable(f"the connector store is unreadable ({e.strerror})") from None
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(4 * 1024 * 1024 + 1)
    except OSError as e:
        raise StoreUnreadable(f"the connector store is unreadable ({e.strerror})") from None
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise StoreUnreadable("the connector store is not valid JSON") from None
    if (
        not isinstance(doc, dict)
        or set(doc) != {"version", "instances", "audit"}
        or doc["version"] != STORE_VERSION
        or not isinstance(doc["instances"], list)
        or not isinstance(doc["audit"], list)
        or len(doc["instances"]) > INSTANCES_MAX
        or not all(_valid_instance(i) for i in doc["instances"])
        or not all(_valid_audit(r) for r in doc["audit"])
        or len({i["id"] for i in doc["instances"]}) != len(doc["instances"])
    ):
        raise StoreUnreadable("the connector store holds a record this build cannot read")
    return doc


def read() -> dict:
    """A snapshot (atomic replace: no lock needed to read a whole document)."""
    return _read(store_path())


def signature() -> object:
    """What changes whenever the store does (the redaction cache keys on it)."""
    try:
        st = store_path().stat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return ("unstat", time.time())
    return (st.st_mtime_ns, st.st_size, st.st_ino)


@contextlib.contextmanager
def _locked() -> Iterator[tuple[Path, dict]]:
    path = store_path()
    with atomicjson.json_write_lock(path):
        yield path, _read(path)


def _write(path: Path, doc: dict) -> None:
    atomicjson.atomic_write_json(path, doc, mode=0o600)


def _audit(doc: dict, op: str, inst: dict, now: float) -> None:
    doc["audit"].append(
        {
            "at": now,
            "op": op,
            "id": inst["id"],
            "manifest_id": inst["manifest_id"],
            "generation": inst["generation"],
        }
    )
    del doc["audit"][:-AUDIT_MAX]


def _find(doc: dict, iid: str) -> dict:
    if not isinstance(iid, str) or not ID_RE.fullmatch(iid):
        raise NotFound(iid)
    for inst in doc["instances"]:
        if inst["id"] == iid:
            return inst
    raise NotFound(iid)


def _bundled() -> dict[str, manifest_mod.Manifest]:
    return manifest_mod.bundled()


def _reconcile_manifests(doc: dict) -> bool:
    """A bundled manifest whose version moved invalidates what a test proved: generation + 1,
    ``tested`` cleared, the token kept (its credential revision does not move). Applied under the
    lock by every mutation; readers treat an out-of-date version as not tested (`readable`)."""
    bundled = _bundled()
    changed = False
    for inst in doc["instances"]:
        m = bundled.get(inst["manifest_id"])
        if m is not None and inst["manifest_version"] != m.version:
            inst["manifest_version"] = m.version
            inst["generation"] += 1
            inst["tested"] = None
            changed = True
    return changed


def _mutate(fn: Callable[[dict, float], object]) -> object:
    with _locked() as (path, doc):
        _reconcile_manifests(doc)
        now = time.time()
        out = fn(doc, now)
        _notify_settle(doc, now)
        # NEVER WRITE WHAT THE STRICT READ REFUSES (#1832, Hermes 7603). A record the reader
        # rejects makes the whole store unreadable, and every connector with it, until someone
        # repairs the file by hand. A mutation that would produce one is refused instead.
        if not all(_valid_instance(i) for i in doc["instances"]):
            raise Refused("that change can't be stored", "invalid_record")
        _write(path, doc)
        return out


# ---- the instance's state, as every reader sees it ----------------------------------------------


def tested_current(inst: dict, bundled: dict[str, manifest_mod.Manifest] | None = None) -> bool:
    bundled = _bundled() if bundled is None else bundled
    m = bundled.get(inst["manifest_id"])
    t = inst["tested"]
    return (
        m is not None
        and inst["manifest_version"] == m.version
        and t is not None
        and t["generation"] == inst["generation"]
        and t["manifest_version"] == m.version
    )


def has_reads(inst: dict, bundled: dict[str, manifest_mod.Manifest] | None = None) -> bool:
    """Whether this instance's manifest declares anything to read. A send-only messenger does
    not, and read can never be switched on for it (#1832, Hermes 7603)."""
    bundled = _bundled() if bundled is None else bundled
    m = bundled.get(inst["manifest_id"])
    return m is not None and bool(m.reads)


def readable(inst: dict, bundled: dict[str, manifest_mod.Manifest] | None = None) -> bool:
    """Whether HQ may read through this instance now: its manifest reads, read enabled, a token
    and an origin, and the last successful test is of THIS generation and manifest version."""
    return (
        has_reads(inst, bundled)
        and inst["enabled"]["read"]
        and bool(inst["origin"])
        and "token" in inst["envelopes"]
        and tested_current(inst, bundled)
    )


def binding_current(inst: dict, bundled: dict[str, manifest_mod.Manifest] | None = None) -> bool:
    """The saved recipients were chosen for THIS bot at THIS origin (#1832): `bound` equals the
    current origin and the identity the current test named. A rotation of the same bot's token
    keeps it; a different bot or a moved origin does not."""
    b = inst.get("bound")
    t = inst["tested"]
    return (
        b is not None
        and t is not None
        and tested_current(inst, bundled)
        and b == {"origin": inst["origin"], "identity": t["login"]}
    )


def sendable(inst: dict, bundled: dict[str, manifest_mod.Manifest] | None = None) -> bool:
    """Whether HQ may propose (and, #1833, a tap may send) through this instance now: send
    switched on, a current test, at least one recipient, and the recipients bound to this bot and
    origin. A read-only connector is never sendable."""
    return (
        MESSENGER_KEYS <= set(inst)
        and bool(inst["enabled"].get("send"))
        and bool(inst["origin"])
        and "token" in inst["envelopes"]
        and bool(inst["recipients"])
        and inst.get("sender", "-") != ""  # an email connector needs its From address (#1836)
        and binding_current(inst, bundled)
    )


# ---- notify routes (#1841) ----------------------------------------------------------------------


def app_origin() -> str:
    """The install's own origin, which every link a route sends starts with
    (``AGENT_SESSIONS_ORIGIN``; the direct origin, relay links wait for #1735). "" when unset:
    notify can't be enabled."""
    return (os.environ.get("AGENT_SESSIONS_ORIGIN") or "").strip().rstrip("/")


def _recipient_ids(inst: dict) -> set[str]:
    return {r["id"] for r in inst.get("recipients") or []}


def notify_off_reason(
    inst: dict, bundled: dict[str, manifest_mod.Manifest] | None = None
) -> str | None:
    """Why this instance's notify route can't hold now, or None. Checked by every write
    (`_notify_settle`), the claim, and the send's last fence: a new generation (credential, origin,
    From address or manifest), a test no longer current, recipients re-bound to another bot or
    account, or the destination no longer a saved recipient."""
    n = inst.get("notify")
    if n is None or not n["destination"]:
        return "destination_removed"
    if inst["generation"] != n["generation"]:
        return "connector_changed"
    if not tested_current(inst, bundled):
        return "needs_test"
    if not binding_current(inst, bundled):
        return "recipients_unbound"
    if n["destination"] not in _recipient_ids(inst):
        return "destination_removed"
    return None


def notify_on(inst: dict, bundled: dict[str, manifest_mod.Manifest] | None = None) -> bool:
    """Whether this instance is a live notify route NOW: switched on, and nothing turned it off."""
    return bool(inst["enabled"].get("notify")) and notify_off_reason(inst, bundled) is None


def notify_edit_ok(
    inst: dict, generation: int, destination: str, bundled: dict | None = None
) -> str | None:
    """Whether a copy delivered under ``generation`` to ``destination`` may still be EDITED: None,
    or the code it settles ``retract_lost`` with. It does not ask the notify switch: switching
    notify off stops new copies, never the marking of copies already sent through the same
    credential."""
    if inst["generation"] != generation:
        return "connector_changed"
    if not tested_current(inst, bundled) or not binding_current(inst, bundled):
        return "connector_changed"
    if destination not in _recipient_ids(inst):
        return "destination_removed"
    return None


def _notify_settle(doc: dict, now: float) -> None:
    """A route goes back to OFF in the same locked write as the change that ends it (#1841)."""
    bundled = _bundled()
    for inst in doc["instances"]:
        if not inst["enabled"].get("notify"):
            continue
        why = notify_off_reason(inst, bundled)
        if why is not None:
            inst["enabled"]["notify"] = False
            inst["notify"]["off"] = {"reason": why, "at": now}


def destination_kind_ok(inst: dict, rec: dict) -> bool:
    """A saved recipient a route may use: a Telegram PRIVATE chat (its id is the person's user
    id), a Matrix room (checked live for exactly two members), or a typed mail address."""
    m = _bundled().get(inst["manifest_id"])
    adapter = m.adapter if m is not None else ""
    if adapter == "telegram-bot":
        return rec["kind"] == "private chat"
    if adapter == "matrix-cs":
        return rec["kind"] == "room"
    return adapter == "smtp" and rec["kind"] == EMAIL_KIND


def _notify_defaults() -> dict:
    return {
        "destination": "", "identity": None, "generation": 0, "classes": list(NOTIFY_CLASSES),
        "window_s": NOTIFY_WINDOW_DEFAULT, "quiet": None, "cap_per_hour": NOTIFY_CAP_DEFAULT,
        "include_line": False, "since": 0, "off": None,
    }  # fmt: skip


def notify_why(inst: dict, bundled: dict[str, manifest_mod.Manifest]) -> str | None:
    """Why notify can't be switched on now, in words; None when it can."""
    if not app_origin():
        return "Set AGENT_SESSIONS_ORIGIN first: every notification carries a link into the app."
    if not tested_current(inst, bundled):
        return "Test this connector first."
    if not inst["recipients"]:
        return "Choose one of your saved recipients: save yours first."
    if not binding_current(inst, bundled):
        return "Save your recipients again for this bot or account first."
    if not any(destination_kind_ok(inst, r) for r in inst["recipients"]):
        return "Notifications go to a private chat with the bot: save yours as a recipient first."
    return None


def _public_notify(inst: dict, bundled: dict[str, manifest_mod.Manifest]) -> dict:
    n = inst.get("notify") or _notify_defaults()
    why = notify_why(inst, bundled)
    return {
        "notify": {
            "enabled": notify_on(inst, bundled),
            "destination": n["destination"],
            "identity": dict(n["identity"]) if n["identity"] else None,
            "classes": list(n["classes"]),
            "window_s": n["window_s"],
            "quiet": dict(n["quiet"]) if n["quiet"] else None,
            "cap_per_hour": n["cap_per_hour"],
            "include_line": n["include_line"],
            "since": n["since"] or None,
            "off": dict(n["off"]) if n["off"] else None,
        },
        "notify_ok": why is None,
        "notify_why": why,
    }


def status(inst: dict, bundled: dict[str, manifest_mod.Manifest]) -> str:
    if inst["manifest_id"] not in bundled:
        return "unavailable"
    if not inst["origin"]:
        return "needs_origin"
    if "token" not in inst["envelopes"]:
        return "needs_token"
    if not tested_current(inst, bundled):
        return "needs_test"
    return "ready"


def _public_messenger(inst: dict, bundled: dict[str, manifest_mod.Manifest]) -> dict:
    """A messenger's recipients, whether they are still bound to this bot, the effective send
    switch, and why sending was last switched off by a change of bot or origin."""
    current = binding_current(inst, bundled)
    return {
        "recipients": [dict(r) for r in inst["recipients"]],
        "recipients_bound": current,
        "recipients_chosen_for": (inst["bound"] or {}).get("identity"),
        "send": bool(sendable(inst, bundled)),
        "send_off": dict(inst["send_off"]) if inst["send_off"] else None,
        **_public_notify(inst, bundled),
    }


def public(inst: dict, bundled: dict[str, manifest_mod.Manifest]) -> dict:
    """The client view: never a token, never an envelope."""
    t = inst["tested"]
    current = tested_current(inst, bundled)
    return {
        "id": inst["id"],
        "manifest_id": inst["manifest_id"],
        "origin": inst["origin"],
        "status": status(inst, bundled),
        "token_set": "token" in inst["envelopes"],
        "account": inst.get("account"),
        "generation": inst["generation"],
        "tested_generation": t["generation"] if t is not None else None,
        "tested": (
            {"login": t["login"], "repos": t["repos"], "at": t["at"]}
            if t is not None and current
            else None
        ),
        "last_test": inst["last_test"],
        "enabled": {"read": bool(inst["enabled"]["read"] and current)},
        **(_public_messenger(inst, bundled) if MESSENGER_KEYS <= set(inst) else {}),
        **({"sender": inst["sender"]} if "sender" in inst else {}),
        "created_at": inst["created_at"],
    }


# ---- mutations ---------------------------------------------------------------------------------


def add(manifest_id: str) -> dict:
    bundled = _bundled()
    m = bundled.get(manifest_id) if isinstance(manifest_id, str) else None
    if m is None:
        raise Refused("no such connector in the catalog", "unknown_manifest")

    def apply(doc: dict, now: float) -> dict:
        if len(doc["instances"]) >= INSTANCES_MAX:
            raise Refused(f"at most {INSTANCES_MAX} connectors", "too_many")
        inst = {
            "id": f"c_{_random.token_hex(6)}",
            "manifest_id": m.id,
            "manifest_version": m.version,
            "origin": m.fixed_origin or "",
            "generation": 1,
            "credential_revision": 1,
            "tested": None,
            "last_test": None,
            "enabled": {"read": False},
            "envelopes": {},
            "created_at": now,
        }
        if m.auth_kind == "basic":
            inst["account"] = ""
        if m.sends:
            inst["enabled"] = {"read": False, "send": False}
            inst.update({"recipients": [], "bound": None, "send_off": None})
        if m.adapter in kinds.TYPED_RECIPIENTS:
            inst["sender"] = ""
        doc["instances"].append(inst)
        _audit(doc, "added", inst, now)
        return dict(inst)

    return _mutate(apply)  # type: ignore[return-value]


def _old_token(inst: dict) -> str | None:
    env = inst["envelopes"].get("token")
    return None if env is None else creds.open_(inst["id"], inst["credential_revision"], env)


def patch(iid: str, body: dict) -> dict:
    """Change the origin, the account (a `basic` connector, #1829) and/or the token. Validated,
    then applied INSIDE the lock against the stored instance: an origin or account change without
    a new token (or ``token: null``) is refused there, so a racing save cannot pair the old secret
    with a new origin or a new account. A real change moves the generation and the credential
    revision and clears ``tested``.

    An email connector's From address (``sender``, #1836) is part of what a card was approved
    for: changing it moves the GENERATION and clears ``tested`` (so a card proposed before it is
    stale at the tap, and sending waits for a new test), but not the credential revision, so the
    password stays decryptable."""
    if (
        not isinstance(body, dict)
        or not body
        or not set(body) <= {"origin", "token", "account", "sender"}
    ):
        raise Refused("send origin, account, sender and/or token", "bad_body")
    raw_origin = body.get("origin")
    new_sender: str | None = None
    if "sender" in body:
        if body["sender"] == "":
            new_sender = ""
        else:
            try:
                new_sender = normalize_address(body["sender"])
            except Refused as e:
                raise Refused(f"From address: {e}", "invalid_sender") from None
    new_account: str | None = None
    if "account" in body:
        raw = body["account"]
        if not isinstance(raw, str) or not ACCOUNT_RE.fullmatch(raw.strip()):
            raise Refused("the account must be a user name without ':'", "invalid_account")
        new_account = raw.strip()
    cred_op = "keep"
    new_token = ""
    if "token" in body:
        if body["token"] is None:
            cred_op = "clear"
        elif not isinstance(body["token"], str):
            raise Refused("token must be a string or null", "bad_body")
        elif creds.is_new_token(body["token"]):
            try:
                new_token = creds.valid_token(body["token"])
            except creds.CredentialError as e:
                raise Refused(str(e), "invalid_token") from None
            cred_op = "set"

    def apply(doc: dict, now: float) -> dict:
        inst = _find(doc, iid)
        m = _bundled().get(inst["manifest_id"])
        if m is None:
            raise Refused("this connector is no longer in the catalog", "unknown_manifest")
        # The origin's schemes are the KIND's: known only here, from the stored instance.
        new_origin = (
            normalize_origin(raw_origin, schemes_for(m.adapter)) if "origin" in body else None
        )
        if new_sender is not None and "sender" not in inst:
            raise Refused("this connector has no From address", "bad_body")
        sender_changed = new_sender is not None and new_sender != inst["sender"]
        if new_origin is not None and m.fixed_origin and new_origin != m.fixed_origin:
            raise Refused("this connector's origin is fixed", "fixed_origin")
        if new_account is not None and m.auth_kind != "basic":
            raise Refused("this connector has no account", "bad_body")
        has_token = "token" in inst["envelopes"]
        why = creds.origin_violation(inst["origin"], has_token, new_origin, body)
        if why is not None:
            raise Refused(why, "origin_needs_token")
        old_account = inst.get("account", "")
        account = new_account if new_account is not None else old_account
        account_changed = account != old_account
        if account_changed and has_token and old_account and cred_op == "keep":
            raise Refused(
                "enter the password for the new account, or clear it (token: null) — a stored "
                "password is only sent for the account it was saved with",
                "account_needs_token",
            )
        origin = new_origin if new_origin is not None else inst["origin"]
        if cred_op == "set" and not origin:
            raise Refused("set the origin together with the token", "needs_origin")
        if cred_op == "set" and m.auth_kind == "basic" and not account:
            raise Refused("set the account together with the password", "needs_account")
        origin_changed = origin != inst["origin"]
        cred_moved = origin_changed or account_changed or cred_op != "keep"
        if cred_op == "clear" and not has_token and not origin_changed and not account_changed:
            cred_moved = False
        if not cred_moved:
            if not sender_changed:
                return dict(inst)  # nothing changed: no new generation
            # Only the From address: a new generation, the password kept (same revision).
            inst["sender"] = new_sender
            inst["generation"] += 1
            inst["tested"] = None
            _audit(doc, "changed", inst, now)
            return dict(inst)
        if sender_changed:
            inst["sender"] = new_sender
        old = _old_token(inst)
        if old is not None and (cred_op != "keep" or origin_changed or account_changed):
            template_secrets.retire([old])  # BEFORE the write (the redaction set keeps it)
        inst["origin"] = origin
        if "account" in inst:
            inst["account"] = account
        inst["generation"] += 1
        inst["credential_revision"] += 1
        inst["tested"] = None
        if cred_op == "set":
            inst["envelopes"] = {
                "token": creds.seal(inst["id"], inst["credential_revision"], new_token)
            }
        else:
            # cleared, or an origin/account move with `token: null`: the old envelope is dropped
            # (and could not be opened under the new revision anyway).
            inst["envelopes"] = {}
        _audit(doc, "changed", inst, now)
        return dict(inst)

    with template_secrets.registration_fence():
        return _mutate(apply)  # type: ignore[return-value]


def snapshot_for_test(iid: str) -> dict:
    """The instance a probe will test: read under the lock (so it is one committed state)."""
    with _locked() as (_path, doc):
        inst = dict(_find(doc, iid))
    bundled = _bundled()
    m = bundled.get(inst["manifest_id"])
    if m is None:
        raise Refused("this connector is no longer in the catalog", "unknown_manifest")
    if inst["manifest_version"] != m.version:
        # A pending manifest bump: commit it first so the probe tests the generation it bumps to.
        _mutate(lambda doc, now: None)
        with _locked() as (_path, doc):
            inst = dict(_find(doc, iid))
    if not inst["origin"]:
        raise Refused("set the origin first", "needs_origin")
    if "token" not in inst["envelopes"]:
        raise Refused("set the token first", "needs_token")
    return inst


def _rebind_check(doc: dict, inst: dict, now: float) -> None:
    """After a passing test of a messenger (#1832): recipients chosen for another bot or origin
    are no consent for this one. Sending is switched OFF in this same locked write, the reason is
    kept on the instance, and only a fresh save of recipients re-binds them. The audit row is the
    ordinary `disabled` one, so the store's audit trail keeps the shape older builds read."""
    if not MESSENGER_KEYS <= set(inst) or inst["bound"] is None:
        return
    b, identity = inst["bound"], inst["tested"]["login"]
    if b == {"origin": inst["origin"], "identity": identity}:
        return  # the same bot at the same origin: a token rotation keeps the consent
    reason = "origin_changed" if b["origin"] != inst["origin"] else "identity_changed"
    was = b["origin"] if reason == "origin_changed" else b["identity"]
    is_now = inst["origin"] if reason == "origin_changed" else identity
    inst["send_off"] = {"reason": reason, "was": was[:300], "now": is_now[:300], "at": now}
    if inst["enabled"]["send"]:
        inst["enabled"]["send"] = False
        _audit(doc, "disabled", inst, now)


def settle_test(
    iid: str, generation: int, *, ok: bool, code: str | None, login: str = "", repos: int = 0
) -> bool:
    """Record a probe of ``generation``. Only if the instance is STILL at that generation: else
    nothing is recorded and the answer is False (``stale``).

    A FAILED probe of the current generation withdraws an earlier pass (#1825, Hermes): a token
    revoked on the forge, or an address that stopped answering as this API, must not stay
    "ready" and keep being read through because it once passed. Reading stops at once (`readable`
    needs a current pass); the stored read choice is kept, so it applies again once a test
    passes, as after any edit (#1746 D4)."""

    def apply(doc: dict, now: float) -> bool:
        inst = _find(doc, iid)
        if inst["generation"] != generation:
            return False
        inst["last_test"] = {"generation": generation, "ok": ok, "code": code, "at": now}
        if ok:
            inst["tested"] = {
                "generation": generation,
                "manifest_version": inst["manifest_version"],
                "login": login[:LOGIN_MAX],
                "repos": max(0, int(repos)),
                "at": now,
            }
            _rebind_check(doc, inst, now)
        else:
            inst["tested"] = None
        _audit(doc, "tested", inst, now)
        return True

    with _locked() as (path, doc):
        # Settle WITHOUT reconciling first: a manifest bump since the probe also makes it stale.
        bundled = _bundled()
        inst = _find(doc, iid)
        m = bundled.get(inst["manifest_id"])
        if m is None or inst["manifest_version"] != m.version:
            return False
        now = time.time()
        result = apply(doc, now)
        if result:
            _notify_settle(doc, now)  # a failed re-test turns a notify route off (#1841)
            _write(path, doc)
        return result


def set_capability(iid: str, read: bool) -> dict:
    if not isinstance(read, bool):
        raise Refused("read must be true or false", "bad_body")

    def apply(doc: dict, now: float) -> dict:
        inst = _find(doc, iid)
        if read:
            if not has_reads(inst):
                raise Refused("this connector has nothing to read", "no_reads")
            if not tested_current(inst):
                raise Refused("test this connector before enabling it", "needs_test")
            if not inst["origin"] or "token" not in inst["envelopes"]:
                raise Refused("set the origin and token first", "needs_token")
        inst["enabled"]["read"] = read
        _audit(doc, "enabled" if read else "disabled", inst, now)
        return dict(inst)

    return _mutate(apply)  # type: ignore[return-value]


def set_send(iid: str, send: bool) -> dict:
    """Switch sending on or off (#1832). Off always succeeds, at once. On needs everything
    `sendable` needs except the switch itself: a current test, recipients, and recipients bound
    to this bot and origin (a re-binding save after any change of either)."""
    if not isinstance(send, bool):
        raise Refused("send must be true or false", "bad_body")

    def apply(doc: dict, now: float) -> dict:
        inst = _find(doc, iid)
        if not MESSENGER_KEYS <= set(inst):
            raise Refused("this connector does not send", "bad_body")
        if send:
            if not tested_current(inst):
                raise Refused("test this connector before enabling it", "needs_test")
            if not inst["recipients"]:
                raise Refused("save at least one recipient first", "needs_recipients")
            if not binding_current(inst):
                raise Refused(
                    "the recipients were chosen for another bot or address; list and save them "
                    "again first",
                    "recipients_unbound",
                )
        if inst["enabled"]["send"] != send:
            inst["enabled"]["send"] = send
            _audit(doc, "enabled" if send else "disabled", inst, now)
        return dict(inst)

    return _mutate(apply)  # type: ignore[return-value]


def set_recipients(iid: str, ids: list[str], snapshot: dict) -> dict:
    """Save the allowlist (#1832) from a candidate SNAPSHOT the server took without this lock
    (`connectors.candidates`): ``{"generation", "candidates": {id: {label, kind, eligible}}}``.

    Under the lock, the instance must still be at the snapshot's generation (else
    `stale_candidates`), tested, and every id must be an ELIGIBLE candidate in it, or already
    allowed and not listed ineligible now. Labels and kinds come from the snapshot (or the saved
    entry), never from the client. The whole list is written, bound to this bot and origin, and
    any `send_off` reason is cleared; or nothing is."""
    if (
        not isinstance(ids, list)
        or len(ids) > RECIPIENTS_MAX
        or not all(isinstance(i, str) for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise Refused(f"send at most {RECIPIENTS_MAX} distinct recipient ids", "bad_body")

    def apply(doc: dict, now: float) -> dict:
        inst = _find(doc, iid)
        if not MESSENGER_KEYS <= set(inst):
            raise Refused("this connector has no recipients", "bad_body")
        if "sender" in inst:
            raise Refused("an email connector's addresses are typed, not listed", "bad_body")
        if inst["generation"] != snapshot.get("generation"):
            raise Refused("the connector changed; list the chats again", "stale_candidates")
        if not tested_current(inst):
            raise Refused("test this connector first", "needs_test")
        listed = snapshot.get("candidates", {})
        saved = {r["id"]: r for r in inst["recipients"]}
        out = []
        for rid in ids:
            c = listed.get(rid)
            if c is not None:
                if not c["eligible"]:
                    raise Refused("that chat can't be used", "unknown_recipient")
                out.append({"id": rid, "label": c["label"], "kind": c["kind"]})
            elif rid in saved and binding_current(inst):
                # Kept without being listed only by the SAME bot at the same origin (a chat
                # that went quiet). After a change of either, consent is re-given only from
                # what the new bot itself lists (#1832, Hermes 7603).
                out.append(dict(saved[rid]))
            else:
                raise Refused("that is not a chat the bot can see", "unknown_recipient")
        inst["recipients"] = out
        inst["bound"] = {"origin": inst["origin"], "identity": inst["tested"]["login"]}
        inst["send_off"] = None
        if not out and inst["enabled"]["send"]:
            inst["enabled"]["send"] = False
            _audit(doc, "disabled", inst, now)
        _audit(doc, "changed", inst, now)
        return dict(inst)

    return _mutate(apply)  # type: ignore[return-value]


def set_addresses(iid: str, entries: object, generation: object) -> dict:
    """Save an email connector's allowlist (#1836): addresses the Commander TYPED, as
    ``[{"address", "label"}]``. Each address must be one plain mailbox (`normalize_address`: the
    domain lower-cased, the local part as typed); two equal after that are refused, in words. A
    label is plain text (at most `LABEL_MAX`), the address when left empty.

    Under the lock the instance must still be at ``generation`` (the one the page showed, else
    `stale_candidates`) and tested. The whole list is written, bound to this account at this
    origin like #1832's, and any `send_off` reason cleared; or nothing is."""
    if not isinstance(entries, list) or len(entries) > RECIPIENTS_MAX:
        raise Refused(f"send at most {RECIPIENTS_MAX} addresses", "bad_body")
    if not _int(generation):
        raise Refused("send the generation the page shows", "bad_body")
    out: list[dict] = []
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e, dict) or set(e) != {"address", "label"}:
            raise Refused("each address is {address, label}", "bad_body")
        address = normalize_address(e["address"])
        label = e["label"]
        if not isinstance(label, str):
            raise Refused("a label is text", "bad_body")
        label = " ".join(label.split())
        if len(label) > LABEL_MAX or not label.isprintable():
            raise Refused(f"a label is plain text, at most {LABEL_MAX} characters", "bad_label")
        if address in seen:
            raise Refused(f"{address} is listed twice", "duplicate_address")
        seen.add(address)
        out.append({"id": address, "label": label or address, "kind": EMAIL_KIND})

    def apply(doc: dict, now: float) -> dict:
        inst = _find(doc, iid)
        if "sender" not in inst:
            raise Refused("this connector's recipients are chosen from a list", "bad_body")
        if inst["generation"] != generation:
            raise Refused("the connector changed; look at it again", "stale_candidates")
        if not tested_current(inst):
            raise Refused("test this connector first", "needs_test")
        inst["recipients"] = out
        inst["bound"] = {"origin": inst["origin"], "identity": inst["tested"]["login"]}
        inst["send_off"] = None
        if not out and inst["enabled"]["send"]:
            inst["enabled"]["send"] = False
            _audit(doc, "disabled", inst, now)
        _audit(doc, "changed", inst, now)
        return dict(inst)

    return _mutate(apply)  # type: ignore[return-value]


def take_send(iid: str, generation: int, recipient_id: str) -> None:
    """Claim one send (#1833) under the store lock: the instance must still be sendable at
    ``generation`` with ``recipient_id`` allowed, and fewer than `SEND_RATE_MAX` sends claimed in
    the last `SEND_RATE_WINDOW_S`. The claim is recorded in the same write. Refused otherwise:
    `stale` (changed, not sendable, recipient gone) or `rate_limited`."""

    def apply(doc: dict, now: float) -> None:
        inst = _find(doc, iid)
        if inst["generation"] != generation or not sendable(inst):
            raise Refused("the connector changed since this was proposed", "stale")
        if recipient_id not in {r["id"] for r in inst["recipients"]}:
            raise Refused("that recipient is no longer allowed", "stale")
        if "sender" in inst and not (valid_address(recipient_id) and valid_address(inst["sender"])):
            # Re-checked at the send (#1836): a stored address no build would accept now.
            raise Refused("that address is not one plain address", "stale")
        recent = [t for t in inst.get("sends", []) if now - t < SEND_RATE_WINDOW_S]
        if len(recent) >= SEND_RATE_MAX:
            raise Refused(
                f"at most {SEND_RATE_MAX} messages an hour through one connector", "rate_limited"
            )
        inst["sends"] = [*recent, now]

    _mutate(apply)


_NOTIFY_BODY = frozenset(
    {"enabled", "destination_id", "classes", "window_s", "quiet", "cap_per_hour", "include_line"}
)


def check_notify_body(body: object) -> dict:
    """``POST …/notify``'s body, every field checked, each refusal in words (#1841)."""
    if not isinstance(body, dict) or set(body) != _NOTIFY_BODY:
        raise Refused(
            "send exactly {enabled, destination_id, classes, window_s, quiet, cap_per_hour, "
            "include_line}",
            "bad_body",
        )
    if not isinstance(body["enabled"], bool) or not isinstance(body["include_line"], bool):
        raise Refused("enabled and include_line are true or false", "bad_body")
    dest = body["destination_id"]
    if not isinstance(dest, str) or len(dest) > 255:
        raise Refused("choose one of your saved recipients", "bad_body")
    cls = body["classes"]
    if (
        not isinstance(cls, list)
        or not all(isinstance(c, str) and c in NOTIFY_CLASSES for c in cls)
        or len(set(cls)) != len(cls)
    ):
        raise Refused("classes are some of: " + ", ".join(NOTIFY_CLASSES), "bad_classes")
    if body["enabled"] and not cls:
        raise Refused("choose at least one kind of notification", "bad_classes")
    w = body["window_s"]
    if not isinstance(w, int) or isinstance(w, bool) or w not in NOTIFY_WINDOWS:
        raise Refused(
            "the bundle window is one of 0, 30, 60, 120, 300 or 600 seconds", "bad_window"
        )
    cap = body["cap_per_hour"]
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= NOTIFY_CAP_MAX:
        raise Refused(f"the hourly cap is 1 to {NOTIFY_CAP_MAX}", "bad_cap")
    q = body["quiet"]
    if q is not None:
        if not (isinstance(q, dict) and set(q) == {"from", "to", "tz"}):
            raise Refused("quiet hours are {from, to, tz}, or null", "bad_quiet")
        if not all(isinstance(q[k], str) and _HHMM_RE.fullmatch(q[k]) for k in ("from", "to")):
            raise Refused("quiet hours start and end at a time like 22:00 and 07:00", "bad_quiet")
        if q["from"] == q["to"]:
            raise Refused("quiet hours need a different start and end", "bad_quiet")
        if not valid_zone(q["tz"]):
            raise Refused("that time zone isn't one this server knows", "bad_quiet")
        q = {"from": q["from"], "to": q["to"], "tz": q["tz"]}
    return {
        "enabled": body["enabled"], "destination": dest, "classes": list(cls), "window_s": w,
        "quiet": q, "cap_per_hour": cap, "include_line": body["include_line"],
    }  # fmt: skip


def set_notify(iid: str, settings: dict, *, generation: int, identity: dict | None) -> dict:
    """Save a messenger's notify route (#1841) from `check_notify_body`'s ``settings``.

    Off always succeeds and keeps the settings and the pinned destination. On needs, UNDER THE
    LOCK: the instance still at ``generation`` (the one the route layer pinned ``identity`` at),
    `AGENT_SESSIONS_ORIGIN` set, a current test, recipients bound to this bot or account, and the
    destination a saved recipient of an eligible kind. A route already on to the SAME destination
    keeps its pinned identity and ``since`` (re-pinning needs off, then on); anything else pins
    ``identity`` and sets ``since`` to now, so nothing from before it is ever announced."""

    def apply(doc: dict, now: float) -> dict:
        inst = _find(doc, iid)
        if not MESSENGER_KEYS <= set(inst):
            raise Refused("this connector does not send", "bad_body")
        prev = inst.get("notify") or _notify_defaults()
        block = {
            **prev,
            **{
                k: settings[k]
                for k in ("classes", "window_s", "quiet", "cap_per_hour", "include_line")
            },
        }
        if not settings["enabled"]:
            inst["notify"] = block
            if inst["enabled"].get("notify"):
                _audit(doc, "disabled", inst, now)
            inst["enabled"]["notify"] = False
            return dict(inst)
        dest = settings["destination"]
        if inst["generation"] != generation:
            raise Refused("the connector changed; look at it again", "stale")
        if not app_origin():
            raise Refused(
                "set AGENT_SESSIONS_ORIGIN first: every notification carries a link", "needs_origin"
            )
        if not tested_current(inst):
            raise Refused("test this connector before enabling it", "needs_test")
        if not binding_current(inst):
            raise Refused(
                "the recipients were chosen for another bot or account; save them again first",
                "recipients_unbound",
            )
        rec = next((r for r in inst["recipients"] if r["id"] == dest), None)
        if rec is None:
            raise Refused("choose one of your saved recipients", "unknown_recipient")
        if not destination_kind_ok(inst, rec):
            raise Refused(
                "notifications go only to a private chat with the bot, a room of just you and "
                "the bot, or your own address",
                "not_private",
            )
        kept = (
            inst["enabled"].get("notify")
            and prev["destination"] == dest
            and notify_off_reason(inst) is None
        )
        if not kept:
            if identity is None or identity.get("id") != dest:
                raise Refused("we couldn't check that destination; try again", "identity_unchecked")
            block["identity"] = {
                "id": dest,
                "label": str(identity.get("label") or rec["label"])[:LABEL_MAX],
                "member": str(identity.get("member") or "")[:255],
            }
            block["since"] = now
        block.update({"destination": dest, "generation": inst["generation"], "off": None})
        inst["notify"] = block
        if not inst["enabled"].get("notify"):
            _audit(doc, "enabled", inst, now)
        inst["enabled"]["notify"] = True
        return dict(inst)

    return _mutate(apply)  # type: ignore[return-value]


def remove(iid: str) -> None:
    def apply(doc: dict, now: float) -> None:
        inst = _find(doc, iid)
        old = _old_token(inst)
        if old is not None:
            template_secrets.retire([old])  # BEFORE the write
        doc["instances"] = [i for i in doc["instances"] if i["id"] != iid]
        _audit(doc, "removed", inst, now)

    with template_secrets.registration_fence():
        _mutate(apply)


# ---- the credential, for the transport and for redaction ----------------------------------------


def token_for(
    iid: str,
    generation: int,
    *,
    for_read: bool = True,
    for_send: bool = False,
    for_notify: str = "",
    destination: str = "",
) -> tuple[dict, str]:
    """``(instance, token)`` for a request bound to ``generation`` — `Refused` when the instance
    moved on, its token cannot be opened, or (``for_read``, every HQ read) read is not enabled
    NOW. This is the fence a disable needs: it does not move the generation, so it is checked at
    the last point before a request leaves. ONLY `connector_http` calls this."""
    doc = read()
    inst = _find(doc, iid)
    if inst["generation"] != generation:
        raise Refused("the connector changed since this was asked", "stale")
    if for_notify == "send":
        # A notify copy (#1841) is fenced on the ROUTE, never on `send`: switched on, nothing
        # turned it off, and still to the destination it was claimed for.
        if not notify_on(inst) or inst["notify"]["destination"] != destination:
            raise Refused("that notify route is off now", "notify_off")
    elif for_notify == "edit":
        # Marking a delivered copy: the generation and destination it went out under.
        why = notify_edit_ok(inst, generation, destination)
        if why is not None:
            raise Refused("the connector changed since that copy was sent", why)
    elif for_send:
        # A SEND (#1833) is fenced on sending, never on reading: switched on, tested, recipients
        # bound to this bot — checked at the last point before the request leaves.
        if not sendable(inst):
            raise Refused("that connector may not send now", "send_disabled")
    elif for_read and not readable(inst):
        raise Refused("that connector is not enabled for reading", "connector_disabled")
    env = inst["envelopes"].get("token")
    token = None if env is None else creds.open_(iid, inst["credential_revision"], env)
    if token is None:
        raise Refused("the connector's token needs to be entered again", "needs_token")
    return inst, token


def token_values_checked() -> list[str]:
    """Every decryptable stored token, for ``template_secrets.redaction_values``. RAISES on an
    unreadable store or an unusable key (fail closed: the transport then refuses to send)."""
    doc = read()
    out: list[str] = []
    for inst in doc["instances"]:
        env = inst["envelopes"].get("token")
        if env is None:
            continue
        v = creds.open_checked(inst["id"], inst["credential_revision"], env)
        if v is not None:
            out.append(v)
    return out
