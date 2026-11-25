"""A verified webhook delivery, handed to one automation (#2221 P2).

`routes/hooks.py` verifies the signature once and hands the delivery to each automation that
picked the webhook; this module decides for ONE of them, alone. Its action (start a session,
message a session, tell HQ, or a plugin that allows it) acts on the events its conditions choose
(`webhook_events.matches`), with the template values its mapping takes from the delivery, typed
(`webhook_events.extract`). A value that fails its type refuses the delivery and nothing starts.
The admission (`automations_store.begin_delivery`) is one transaction bound to the configuration
the delivery was matched against and to the webhook it came through. The claim is the body's
fingerprint, so a redelivered body never runs twice, or, when the trigger names `once_per`
fields (#2222), their canonical values (`webhook_events.once_key`), so one value set runs once
whatever else the body carries.

Delivery values are data: they fill typed template fields and nothing else. The payload never
chooses the folder, the session, the agent, the template or the permissions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from . import automation_runner as runner
from . import automations as model
from . import automations_store as store
from . import forge_hook, webhook_events

log = logging.getLogger("agent_sessions.webhook_actions")

EVENT_HEADERS = ("x-forgejo-event", "x-gitea-event", "x-github-event")
IGNORED = "ignored: not one of the events it acts on"


def _label(headers: dict) -> str:
    """The event name the sender gave, for display only (unsigned, never matched on)."""
    for name in EVENT_HEADERS:
        if headers.get(name):
            return forge_hook.display(headers.get(name))
    return "delivery"


async def handle(
    aid: str, body: bytes, headers: dict, *, registry, source: str | None = None
) -> None:
    """Act on one VERIFIED delivery that came through webhook ``source``. Never raises."""
    try:
        if source is None:
            return  # only a webhook from the list feeds a generic action
        for _attempt in range(2):
            if not await _handle_once(aid, body, headers, registry=registry, source=source):
                return
        log.warning("automation %s: it kept changing; delivery dropped", aid)
    except Exception:  # noqa: BLE001 — a delivery must never take the app down
        log.warning("automation %s: delivery handling failed", aid, exc_info=True)


async def _handle_once(aid: str, body: bytes, headers: dict, *, registry, source: str) -> bool:
    """One match and admission. True when the automation changed under it (match again)."""
    row = await asyncio.to_thread(store.get, aid)
    config = row["config"]
    if config is None or config["action"]["kind"] not in store.DELIVERY_ACTIONS:
        return False
    if store.source_of(aid, config) != source:
        return False  # switched to another webhook since this delivery was routed
    label = _label(headers)
    fp = hashlib.sha256(body).hexdigest()
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return await _reject(
            aid, row, source, fp, label, "the delivery is not a JSON object; nothing started"
        )
    if not webhook_events.matches(config["trigger"].get("when") or [], payload):
        await asyncio.to_thread(store.note_delivery, aid, label, IGNORED)
        return False
    mapping = model.mapping_of(config["action"])
    try:
        delivered = webhook_events.extract(mapping, payload)
    except webhook_events.ValueRejected as e:
        return await _reject(aid, row, source, fp, label, f"{e}; nothing started")
    once_per = config["trigger"].get("once_per") or []
    if once_per:
        # The claim is the chosen values, not the body (#2222): the same pull request head re-sent
        # with a different body (a fresh `updated_at`) runs once.
        try:
            slot = f"once:{webhook_events.once_key(once_per, payload)}"
        except webhook_events.ValueRejected as e:
            return await _reject(aid, row, source, fp, label, f"{e}; nothing started")
    else:
        slot = f"delivery:{fp}"
    res = await asyncio.to_thread(
        store.begin_delivery,
        aid,
        source=source,
        slot=slot,
        expect_revision=row["revision"],
        inputs={"event": label, "delivered": delivered},
    )
    if res.get("stale_config"):
        return True
    await asyncio.to_thread(store.note_delivery, aid, label, res.get("reason") or "started")
    run = res.get("run")
    if run is not None and run["state"] == "dispatching":
        runner.spawn(run, registry=registry)
    return False


async def _reject(aid: str, row: dict, source: str, fp: str, label: str, why: str) -> bool:
    """A refused delivery, recorded under the revision it was decided against: if the automation
    changed meanwhile, nothing is claimed or counted and the delivery is decided again (True)."""
    res = await asyncio.to_thread(
        store.record_delivery,
        aid,
        source=source,
        slot=f"rejected:{fp}",
        outcome="refused",
        reason=f"rejected: {why}",
        inputs={"event": label},
        expect_revision=row["revision"],
    )
    return bool(isinstance(res, dict) and res.get("stale_config"))
