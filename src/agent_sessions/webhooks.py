"""Webhooks as a list (#2221 P1): a name, an address and a secret — usable by any service and any
automation. Read `docs/invariants/webhooks.md` before changing anything here.

**No provider.** A webhook does not know who sends to it. A delivery is authenticated by an
HMAC-SHA256 of its raw body under the webhook's secret, in any of the common header forms
(`signature_header`); what a delivery MEANS is decided by each automation that picked it.

**Stored here, in `automations.db` (schema v3):**

* ``webhooks`` — id, name, the last signed delivery's time, event label and **shape** (field paths
  and value types only — never a value), the rolling unsigned-delivery counter, and
  ``legacy_automation`` for a review loop's own webhook from #2202 (see below).
* ``webhook_secrets`` — AES-GCM envelopes (`template_secrets.encrypt`), one per generation, each
  with the AAD it was sealed under. The plaintext is returned ONCE by the call that makes it.
  Rotation keeps the previous generation valid for `SECRET_OVERLAP_S`.

**#2202's review loops** became webhooks in the v2 → v3 migration: same id as their automation,
same secret envelopes (with their original AAD, so they still decrypt), ``legacy_automation`` set.
The review loop was retired (#2222): its address `/hooks/a/{id}` answers the uniform 404, its
automation loads unreadable and runs nothing, and its legacy webhook stays in the list, readable
and never pickable, until that automation is deleted (which deletes it too).
"""

from __future__ import annotations

import json
import re
import secrets as _secrets
import time
import uuid

from . import automations_store as store
from . import template_secrets

#: How long a rotated-out secret keeps verifying deliveries, so the sender can be updated.
SECRET_OVERLAP_S = 24 * 3600
UNSIGNED_WINDOW_S = 24 * 3600
WEBHOOKS_MAX = 100
NAME_MAX = 60
#: Bounds on a delivery's recorded shape.
SHAPE_MAX_PATHS = 200
SHAPE_MAX_DEPTH = 6
PATH_MAX = 160

ID_RE = re.compile(r"^[a-z0-9]{1,64}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class WebhookError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


# ---- the shape of a delivery (names and types, never values) -------------------------------------


def _type_of(v: object) -> str:
    if isinstance(v, bool):
        return "yes/no"
    if isinstance(v, int | float):
        return "number"
    if isinstance(v, str):
        return "text"
    if v is None:
        return "empty"
    if isinstance(v, list):
        return "list"
    return "object"


def shape_of(payload: object) -> list[dict]:
    """``[{path, type}]`` of a JSON payload, depth- and count-bounded. Lists are walked through
    their first element (``labels[].name``). Never a value."""
    out: list[dict] = []

    def walk(v: object, path: str, depth: int) -> None:
        if len(out) >= SHAPE_MAX_PATHS:
            return
        if isinstance(v, dict) and depth < SHAPE_MAX_DEPTH:
            for k in sorted(v)[:SHAPE_MAX_PATHS]:
                if not isinstance(k, str) or not k or _CTRL_RE.search(k) or len(k) > 64:
                    continue
                walk(v[k], f"{path}.{k}" if path else k, depth + 1)
            return
        if isinstance(v, list) and v and depth < SHAPE_MAX_DEPTH:
            walk(v[0], f"{path}[]", depth + 1)
            return
        if path and len(path) <= PATH_MAX:
            out.append({"path": path, "type": _type_of(v)})

    walk(payload, "", 0)
    return out


# ---- signatures ---------------------------------------------------------------------------------

#: The header forms a signed delivery may carry, all HMAC-SHA256 of the raw body, hex.
SIGNATURE_HEADERS = (
    ("x-forgejo-signature", ""),
    ("x-gitea-signature", ""),
    ("x-hub-signature-256", "sha256="),
)


def signature_header(headers) -> str | None:
    """The hex signature a delivery carries, from the first header form present, or None."""
    for name, prefix in SIGNATURE_HEADERS:
        raw = headers.get(name)
        if raw is None:
            continue
        raw = str(raw).strip()
        if prefix:
            if not raw.lower().startswith(prefix):
                return ""
            raw = raw[len(prefix) :]
        return raw
    return None


# ---- the store ----------------------------------------------------------------------------------


def _aad(wid: str, gen: int) -> str:
    return f"webhook:{wid}:{gen}"


def _name(raw: object) -> str:
    if not isinstance(raw, str):
        raise WebhookError("name must be text")
    name = raw.strip()
    if not name or len(name) > NAME_MAX or _CTRL_RE.search(name):
        raise WebhookError(f"name must be 1–{NAME_MAX} characters")
    return name


def _row(con, wid: str):
    if not ID_RE.match(wid or ""):
        raise store.NotFound("webhook")
    r = con.execute("SELECT * FROM webhooks WHERE id=?", (wid,)).fetchone()
    if r is None:
        raise store.NotFound("webhook")
    return r


def _seal(con, wid: str, ts: float) -> str:
    """A new current secret for ``wid``: retire the current one with an overlap, drop expired
    ones, seal the new plaintext. Returns the plaintext. Inside the caller's transaction."""
    plaintext = _secrets.token_hex(32)
    con.execute(
        "DELETE FROM webhook_secrets WHERE webhook_id=? AND retire_at IS NOT NULL AND retire_at<=?",
        (wid, ts),
    )
    con.execute(
        "UPDATE webhook_secrets SET retire_at=? WHERE webhook_id=? AND retire_at IS NULL",
        (ts + SECRET_OVERLAP_S, wid),
    )
    gen = int(
        con.execute(
            "SELECT COALESCE(MAX(gen), 0) + 1 FROM webhook_secrets WHERE webhook_id=?", (wid,)
        ).fetchone()[0]
    )
    aad = _aad(wid, gen)
    try:
        env = template_secrets.encrypt(aad, plaintext)
    except template_secrets.SecretKeyUnavailable as e:
        raise store.StoreError(f"the secret could not be stored ({e})", 503) from None
    con.execute(
        "INSERT INTO webhook_secrets (webhook_id, gen, envelope, aad, created_at, retire_at) "
        "VALUES (?,?,?,?,?,NULL)",
        (wid, gen, json.dumps(env), aad, ts),
    )
    return plaintext


def create(name: object, *, now: float | None = None) -> tuple[dict, str]:
    """A new webhook and its first secret. Returns ``(public, secret)`` — the secret's only
    appearance."""
    ts = time.time() if now is None else now
    clean = _name(name)
    with store._tx() as con:
        if con.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0] >= WEBHOOKS_MAX:
            raise WebhookError(f"at most {WEBHOOKS_MAX} webhooks", 409)
        wid = uuid.uuid4().hex[:16]
        con.execute(
            "INSERT INTO webhooks (id, name, revision, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (wid, clean, ts, ts),
        )
        secret = _seal(con, wid, ts)
    return get(wid, now=ts), secret


def rotate(wid: str, *, now: float | None = None) -> str:
    """A new secret (shown once); the previous one keeps verifying for `SECRET_OVERLAP_S`."""
    ts = time.time() if now is None else now
    with store._tx() as con:
        _row(con, wid)
        secret = _seal(con, wid, ts)
        con.execute("UPDATE webhooks SET revision=revision+1, updated_at=? WHERE id=?", (ts, wid))
    return secret


def rename(wid: str, name: object, *, expect_revision: int | None = None) -> dict:
    clean = _name(name)
    with store._tx() as con:
        r = _row(con, wid)
        if expect_revision is not None and int(r["revision"]) != expect_revision:
            raise store.Conflict()
        con.execute(
            "UPDATE webhooks SET name=?, revision=revision+1, updated_at=? WHERE id=?",
            (clean, time.time(), wid),
        )
    return get(wid)


def delete(wid: str) -> None:
    """Refused while any automation references it — checked INSIDE the deleting transaction, which
    automation create/update also check under theirs (`automations_store._check_webhook_ref`), so
    neither side can leave a dangling reference."""
    with store._tx() as con:
        r = _row(con, wid)
        if r["legacy_automation"]:
            raise WebhookError("this webhook belongs to an automation; delete that instead", 409)
        for a in con.execute("SELECT config FROM automations").fetchall():
            trig = (store._loads(a["config"], {}) or {}).get("trigger") or {}
            if trig.get("kind") == "webhook" and trig.get("webhook_id") == wid:
                raise WebhookError("this webhook is used by an automation; change that first", 409)
        con.execute("DELETE FROM webhook_secrets WHERE webhook_id=?", (wid,))
        con.execute("DELETE FROM webhooks WHERE id=?", (wid,))


def secrets(wid: str, *, now: float | None = None) -> list[str]:
    """Every secret that verifies a delivery for ``wid`` right now. An envelope that no longer
    decrypts (a lost key) verifies nothing."""
    ts = time.time() if now is None else now
    with store._read() as con:
        rows = con.execute(
            "SELECT envelope, aad FROM webhook_secrets WHERE webhook_id=? AND "
            "(retire_at IS NULL OR retire_at>?) ORDER BY gen",
            (wid, ts),
        ).fetchall()
    out = []
    for r in rows:
        env = store._loads(r["envelope"], None)
        if isinstance(env, dict):
            value = template_secrets.decrypt(r["aad"], env)
            if value:
                out.append(value)
    return out


def consumers(wid: str, *, include_disabled: bool = False) -> list[str]:
    """The automations whose trigger picked ``wid``, or — for a legacy webhook (#2202) — its own
    automation only (its "used by"; no delivery reaches it any more). Enabled ones unless
    ``include_disabled``."""
    with store._read() as con:
        r = con.execute("SELECT legacy_automation FROM webhooks WHERE id=?", (wid,)).fetchone()
    if r is None:
        return []
    out = []
    for row in store.list_all():
        cfg = row.get("config") or {}
        trig = cfg.get("trigger") or {}
        mine = (
            row["id"] == r["legacy_automation"]
            if r["legacy_automation"]
            else trig.get("kind") == "webhook" and trig.get("webhook_id") == wid
        )
        if mine and (include_disabled or row["enabled"]):
            out.append(row["id"])
    return out


def set_relay(wid: str, public_id: str | None, url: str | None) -> None:
    """The webhook's address on the BattleLab relay (Home Free, #2221 P1b), or none."""
    if public_id is not None and not re.match(r"\A[a-z2-7]{26}\Z", public_id):
        raise WebhookError("bad relay id")
    if url is not None and (not url.startswith(f"https://{public_id}.") or len(url) > 200):
        raise WebhookError("bad relay address")
    with store._tx() as con:
        con.execute(
            "UPDATE webhooks SET relay_public_id=?, relay_url=? WHERE id=?", (public_id, url, wid)
        )


def note_delivery(
    wid: str, event: str, note: str, *, shape: list[dict] | None = None, now: float | None = None
) -> None:
    ts = time.time() if now is None else now
    with store._tx() as con:
        if shape is None:
            con.execute(
                "UPDATE webhooks SET last_at=?, last_event=?, last_note=? WHERE id=?",
                (ts, event[:64], note[:300], wid),
            )
        else:
            con.execute(
                "UPDATE webhooks SET last_at=?, last_event=?, last_note=?, last_shape=? "
                "WHERE id=?",
                (ts, event[:64], note[:300], json.dumps(shape), wid),
            )


def note_unsigned(wid: str, *, now: float | None = None) -> int:
    ts = time.time() if now is None else now
    with store._tx() as con:
        r = con.execute(
            "SELECT unsigned_count, unsigned_since FROM webhooks WHERE id=?", (wid,)
        ).fetchone()
        if r is None:
            return 0
        if r["unsigned_since"] is None or ts - float(r["unsigned_since"]) >= UNSIGNED_WINDOW_S:
            count, since = 1, ts
        else:
            count, since = int(r["unsigned_count"]) + 1, float(r["unsigned_since"])
        con.execute(
            "UPDATE webhooks SET unsigned_count=?, unsigned_since=? WHERE id=?", (count, since, wid)
        )
    return count


def _public(r, ts: float, users: list[dict], *, detail: bool) -> dict:
    with store._read() as con:
        cur = con.execute(
            "SELECT created_at FROM webhook_secrets WHERE webhook_id=? AND retire_at IS NULL",
            (r["id"],),
        ).fetchone()
        old = con.execute(
            "SELECT MAX(retire_at) FROM webhook_secrets WHERE webhook_id=? AND retire_at>?",
            (r["id"], ts),
        ).fetchone()[0]
    unsigned = 0
    if r["unsigned_since"] is not None and ts - float(r["unsigned_since"]) < UNSIGNED_WINDOW_S:
        unsigned = int(r["unsigned_count"])
    legacy = r["legacy_automation"]
    out = {
        "id": r["id"],
        "name": r["name"],
        "revision": int(r["revision"]),
        "path": f"/hooks/a/{legacy}" if legacy else f"/hooks/w/{r['id']}",
        # Through the BattleLab relay (Home Free): the public address to give the sender.
        "relay_url": r["relay_url"] if not legacy else None,
        "legacy_automation": legacy,
        "pickable": not legacy,
        "secret_set": cur is not None,
        "secret_created_at": cur["created_at"] if cur else None,
        "previous_valid_until": old,
        "last_delivery_at": r["last_at"],
        "last_event": r["last_event"] or "",
        "last_note": r["last_note"] or "",
        "unsigned_24h": unsigned,
        "used_by": users,
        "created_at": r["created_at"],
    }
    if detail:
        out["last_shape"] = store._loads(r["last_shape"], []) or []
        # The event presets that fit its last delivery (#2221 P2): the wizard's step 2.
        from . import webhook_events

        out["presets"] = webhook_events.presets(out["last_shape"])
    return out


def _users(wid: str) -> list[dict]:
    names = {a["id"]: a["name"] for a in store.list_all()}
    return [{"id": a, "name": names.get(a, a)} for a in consumers(wid, include_disabled=True)]


def get(wid: str, *, now: float | None = None) -> dict:
    ts = time.time() if now is None else now
    with store._read() as con:
        r = _row(con, wid)
    return _public(r, ts, _users(wid), detail=True)


def list_all(*, now: float | None = None) -> list[dict]:
    ts = time.time() if now is None else now
    with store._read() as con:
        rows = con.execute("SELECT * FROM webhooks ORDER BY created_at").fetchall()
    return [_public(r, ts, _users(r["id"]), detail=False) for r in rows]


def exists_pickable(wid: str) -> bool:
    with store._read() as con:
        r = con.execute("SELECT legacy_automation FROM webhooks WHERE id=?", (wid,)).fetchone()
    return r is not None and not r["legacy_automation"]


def name_of(wid: str) -> str:
    with store._read() as con:
        r = con.execute("SELECT name FROM webhooks WHERE id=?", (wid,)).fetchone()
    return r["name"] if r else wid
