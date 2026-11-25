"""The connector API's operations that are more than one store call (#1750): the catalog view and
the generation-fenced test.

**The test is fenced on the generation it probed.** It reads the instance (generation ``G``) under
the lock, probes WITHOUT the lock (a slow forge must not hold every other connector write), then
settles under the lock only if the generation is still ``G`` (`store.settle_test`). Otherwise the
answer is ``stale`` and nothing is recorded, so a probe of an old origin or token can never mark a
newer configuration tested.
"""

from __future__ import annotations

import asyncio

from .. import connector_http, prefs
from . import candidates, store
from . import manifest as manifest_mod
from .adapters import ADAPTERS, common, matrix


def disclosure() -> str:
    """What the Commander is told before enabling read: where HQ sends what it reads."""
    try:
        origin = prefs.endpoint_origin(prefs.get_ai_review().get("base_url"))
    except Exception:  # noqa: BLE001 — unreadable prefs: say so rather than name nothing
        origin = None
    where = origin or "your AI endpoint (none is configured yet)"
    return (
        f"What HQ reads through a connector is sent to your AI endpoint ({where}) as part of "
        "HQ's conversation. Read stays off until you enable it."
    )


def catalog() -> dict:
    bundled = manifest_mod.bundled()
    doc = store.read()
    return {
        "catalog": [m.public() for m in bundled.values()],
        "instances": [store.public(i, bundled) for i in doc["instances"]],
        "disclosure": disclosure(),
    }


def _bound(inst: dict) -> connector_http.Bound:
    """The connection test's binding: it runs before read can be enabled (``for_read=False``)."""
    m = manifest_mod.bundled()[inst["manifest_id"]]
    return connector_http.Bound(
        instance_id=inst["id"],
        generation=inst["generation"],
        origin=inst["origin"],
        auth_scheme=m.auth_scheme,
        label=m.label,
        adapter=m.adapter,
        for_read=False,
    )


def _safe_login(login: object, adapter: str = "forgejo-api") -> str:
    """The forge's account name as stored and shown: a plain username that the redaction guard
    leaves unchanged, or empty (Hermes on #1782: a forge can echo the token back as the login)."""
    from .. import ask_store, template_secrets

    # A forge login is a plain name; a Nextcloud user id may also hold '@', "'" and inner spaces
    # (e-mail and LDAP ids, #1829), the store's account shape. It names the DAV root, so an id
    # the guard would change is refused rather than shown or used.
    name_re = (
        store.ACCOUNT_RE
        if adapter in ("nextcloud", "smtp")
        else matrix.USER_ID_RE
        if adapter == "matrix-cs"
        else common.NAME_RE
    )
    if not isinstance(login, str) or not name_re.fullmatch(login):
        return ""
    try:
        guarded = ask_store.guard_text(
            login, len(login), secrets=template_secrets.redaction_values()
        )
    except (ask_store.StoreRefused, template_secrets.RedactionUnavailable):
        return ""
    return login if guarded == login and len(login) <= store.LOGIN_MAX else ""


async def run_test(iid: str) -> dict:
    inst = await asyncio.to_thread(store.snapshot_for_test, iid)
    generation = inst["generation"]
    m = manifest_mod.bundled()[inst["manifest_id"]]
    adapter = ADAPTERS[m.adapter]
    try:
        found = await adapter.probe(_bound(inst))
    except connector_http.ConnectorError as e:
        settled = await asyncio.to_thread(
            store.settle_test, iid, generation, ok=False, code=e.code[:40]
        )
        if not settled:
            return {"status": "stale", "generation": generation}
        return {"status": "failed", "code": e.code, "detail": e.detail, "generation": generation}
    login = await asyncio.to_thread(_safe_login, found["login"], m.adapter)
    if m.adapter in ("nextcloud", "telegram-bot", "matrix-cs", "smtp") and not login:
        # Every later read names its paths by this id: without it nothing could be read.
        settled = await asyncio.to_thread(
            store.settle_test, iid, generation, ok=False, code="bad_reply"
        )
        if not settled:
            return {"status": "stale", "generation": generation}
        return {"status": "failed", "code": "bad_reply", "generation": generation,
                "detail": f"{m.label} named an account this connector cannot use"}  # fmt: skip
    settled = await asyncio.to_thread(
        store.settle_test,
        iid,
        generation,
        ok=True,
        code=None,
        login=login,
        repos=found["repos"],
    )
    if not settled:
        return {"status": "stale", "generation": generation}
    out = {"status": "ok", "login": login, "repos": found["repos"], "generation": generation}
    if isinstance(found.get("warning"), str):
        out["warning"] = found["warning"][:300]  # adapter-written text, never the forge's
    return out


async def notify_identity(inst: dict, destination: str) -> dict | None:
    """The identity a notify route is pinned to (#1841), read FRESH, without the store lock:
    ``{id, label, member}``, or None when the route is already on to that destination (it keeps
    its pin: re-pinning needs off, then on). `store.set_notify` checks the generation under the
    lock, so a pin read from an older configuration is refused there.

    Telegram: the private chat must be in the server's candidate snapshot at this generation
    (its id is the person's user id; the label is the name and @username Telegram showed).
    Matrix: the room's joined members must be exactly the bot and ONE other user, unencrypted;
    that user is pinned. Mail: the saved address is the identity (BattleLab can't verify who
    reads it)."""
    from .. import notify_routes

    rec = next((r for r in inst.get("recipients") or [] if r["id"] == destination), None)
    if rec is None:
        raise store.Refused("choose one of your saved recipients", "unknown_recipient")
    if not store.destination_kind_ok(inst, rec):
        raise store.Refused(
            "notifications go only to a private chat with the bot, a room of just you and the "
            "bot, or your own address",
            "not_private",
        )
    if store.notify_on(inst) and inst["notify"]["destination"] == destination:
        return None
    m = manifest_mod.bundled()[inst["manifest_id"]]
    if m.adapter == "telegram-bot":
        snap = candidates.snapshot(inst["id"])
        current = snap["generation"] == inst["generation"]
        c = snap["candidates"].get(destination) if current else None
        if c is None or not c["eligible"] or c["kind"] != "private chat":
            raise store.Refused(
                "that private chat isn't in the bot's recent chats: send the bot a message, then "
                "refresh the list and try again",
                "identity_unchecked",
            )
        return {"id": destination, "label": c["label"], "member": ""}
    if m.adapter == "matrix-cs":
        facts = await matrix.room_facts(_bound(inst), destination)
        bot = inst["tested"]["login"] if inst["tested"] else ""
        member, why = notify_routes.matrix_pin(facts, bot)
        if member is None:
            raise store.Refused(why, "not_private")
        return {"id": destination, "label": rec["label"], "member": member}
    return {"id": destination, "label": rec["label"], "member": ""}
