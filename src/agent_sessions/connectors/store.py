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
import json
import os
import re
import secrets as _random
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

from .. import atomicjson, net_vetting, template_secrets
from . import manifest as manifest_mod
from . import secrets as creds

STORE_VERSION = 1
INSTANCES_MAX = 32
AUDIT_MAX = 200
LOGIN_MAX = 80
ID_RE = re.compile(r"c_[0-9a-f]{12}")
CAPABILITIES = ("read",)
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


def normalize_origin(raw: object) -> str:
    """``scheme://host[:port]``, lower-cased, or `Refused`. Only an origin: no path, query,
    fragment or credentials. A token-bearing origin is ``https`` — ``http`` only for a loopback
    literal or ``localhost`` (and `connector_http` checks again at every request that the name
    really resolves to loopback). A literal refused address (link-local, metadata, multicast,
    unspecified, reserved) is refused here; a NAME is vetted at every request."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 300:
        raise Refused("origin must be an http(s) origin", "invalid_origin")
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        raise Refused("origin is not a valid URL", "invalid_origin") from None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        raise Refused("origin must be an http(s) origin", "invalid_origin")
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
    if scheme == "http":
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


def _valid_instance(inst: object) -> bool:
    if not isinstance(inst, dict) or set(inst) != _INSTANCE_KEYS:
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
            if normalize_origin(origin) != origin:
                return False
        except Refused:
            return False
    if not (_int(inst["generation"]) and _int(inst["credential_revision"])):
        return False
    if not (_valid_tested(inst["tested"]) and _valid_last_test(inst["last_test"])):
        return False
    enabled = inst["enabled"]
    if not isinstance(enabled, dict) or set(enabled) != set(CAPABILITIES):
        return False
    if not all(isinstance(v, bool) for v in enabled.values()):
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


def readable(inst: dict, bundled: dict[str, manifest_mod.Manifest] | None = None) -> bool:
    """Whether HQ may read through this instance now: read enabled, a token and an origin, and
    the last successful test is of THIS generation and manifest version."""
    return (
        inst["enabled"]["read"]
        and bool(inst["origin"])
        and "token" in inst["envelopes"]
        and tested_current(inst, bundled)
    )


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
        "generation": inst["generation"],
        "tested_generation": t["generation"] if t is not None else None,
        "tested": (
            {"login": t["login"], "repos": t["repos"], "at": t["at"]}
            if t is not None and current
            else None
        ),
        "last_test": inst["last_test"],
        "enabled": {"read": bool(inst["enabled"]["read"] and current)},
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
        doc["instances"].append(inst)
        _audit(doc, "added", inst, now)
        return dict(inst)

    return _mutate(apply)  # type: ignore[return-value]


def _old_token(inst: dict) -> str | None:
    env = inst["envelopes"].get("token")
    return None if env is None else creds.open_(inst["id"], inst["credential_revision"], env)


def patch(iid: str, body: dict) -> dict:
    """Change the origin and/or the token. Validated, then applied INSIDE the lock against the
    stored instance: an origin change without a new token (or ``token: null``) is refused there,
    so a racing save cannot pair the old token with the new origin. A real change moves the
    generation and the credential revision and clears ``tested``."""
    if not isinstance(body, dict) or not body or not set(body) <= {"origin", "token"}:
        raise Refused("send origin and/or token", "bad_body")
    new_origin = normalize_origin(body["origin"]) if "origin" in body else None
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
        if new_origin is not None and m.fixed_origin and new_origin != m.fixed_origin:
            raise Refused("this connector's origin is fixed", "fixed_origin")
        has_token = "token" in inst["envelopes"]
        why = creds.origin_violation(inst["origin"], has_token, new_origin, body)
        if why is not None:
            raise Refused(why, "origin_needs_token")
        origin = new_origin if new_origin is not None else inst["origin"]
        if cred_op == "set" and not origin:
            raise Refused("set the origin together with the token", "needs_origin")
        origin_changed = origin != inst["origin"]
        if not origin_changed and cred_op == "keep":
            return dict(inst)  # nothing changed: no new generation
        if cred_op == "clear" and not has_token and not origin_changed:
            return dict(inst)
        old = _old_token(inst)
        if old is not None and (cred_op != "keep" or origin_changed):
            template_secrets.retire([old])  # BEFORE the write (the redaction set keeps it)
        inst["origin"] = origin
        inst["generation"] += 1
        inst["credential_revision"] += 1
        inst["tested"] = None
        if cred_op == "set":
            inst["envelopes"] = {
                "token": creds.seal(inst["id"], inst["credential_revision"], new_token)
            }
        else:
            # cleared, or an origin move with `token: null`: the old envelope is dropped (and
            # could not be opened under the new revision anyway).
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


def settle_test(
    iid: str, generation: int, *, ok: bool, code: str | None, login: str = "", repos: int = 0
) -> bool:
    """Record a probe of ``generation``. Only if the instance is STILL at that generation: else
    nothing is recorded and the answer is False (``stale``)."""

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
        _audit(doc, "tested", inst, now)
        return True

    with _locked() as (path, doc):
        # Settle WITHOUT reconciling first: a manifest bump since the probe also makes it stale.
        bundled = _bundled()
        inst = _find(doc, iid)
        m = bundled.get(inst["manifest_id"])
        if m is None or inst["manifest_version"] != m.version:
            return False
        result = apply(doc, time.time())
        if result:
            _write(path, doc)
        return result


def set_capability(iid: str, read: bool) -> dict:
    if not isinstance(read, bool):
        raise Refused("read must be true or false", "bad_body")

    def apply(doc: dict, now: float) -> dict:
        inst = _find(doc, iid)
        if read:
            if not tested_current(inst):
                raise Refused("test this connector before enabling it", "needs_test")
            if not inst["origin"] or "token" not in inst["envelopes"]:
                raise Refused("set the origin and token first", "needs_token")
        inst["enabled"]["read"] = read
        _audit(doc, "enabled" if read else "disabled", inst, now)
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


def token_for(iid: str, generation: int, *, for_read: bool = True) -> tuple[dict, str]:
    """``(instance, token)`` for a request bound to ``generation`` — `Refused` when the instance
    moved on, its token cannot be opened, or (``for_read``, every HQ read) read is not enabled
    NOW. This is the fence a disable needs: it does not move the generation, so it is checked at
    the last point before a request leaves. ONLY `connector_http` calls this."""
    doc = read()
    inst = _find(doc, iid)
    if inst["generation"] != generation:
        raise Refused("the connector changed since this was asked", "stale")
    if for_read and not readable(inst):
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
