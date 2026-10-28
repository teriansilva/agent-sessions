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
from . import manifest as manifest_mod
from . import store
from .adapters import forgejo

ADAPTERS = {"forgejo-api": forgejo}


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
        for_read=False,
    )


def _safe_login(login: object) -> str:
    """The forge's account name as stored and shown: a plain username that the redaction guard
    leaves unchanged, or empty (Hermes on #1782: a forge can echo the token back as the login)."""
    from .. import ask_store, template_secrets

    if not isinstance(login, str) or not forgejo.NAME_RE.fullmatch(login):
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
    login = await asyncio.to_thread(_safe_login, found["login"])
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
    return {"status": "ok", "login": login, "repos": found["repos"],
            "generation": generation}  # fmt: skip
