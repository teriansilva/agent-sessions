"""HQ proposes a message; the Commander taps Send (#1833, #1746 P2a, D7).

``propose_message`` stages a card (``ask_store`` proposal kind ``message``) and a sidecar entry:
the connector, a recipient the Commander allowed (#1832, picked by the index `list_recipients`
returned this turn), and the exact text. The card is what is SHOWN; the entry is what a tap sends,
bound to the card by a keyed digest over instance, generation, recipient and text.

**The tap** (``decide_locked``, under the conversation's decide lock, owned from the route):

1. only a ``pending`` entry can be decided; any other state is returned as it is, so a second tap
   from another tab or worker never sends;
2. re-checked: the binding (recomputed, and equal to the card's), the connector still sendable at
   the card's generation, the recipient still allowed, the text still clean against the secrets
   known NOW; any failure is ``stale`` and nothing is sent;
3. the rate claim (`store.take_send`) is taken BEFORE ``deciding`` is written, so a crash between
   the two can only make the cap stricter;
4. ``deciding`` is written durably (fsynced) BEFORE the send task starts;
5. the task rechecks a Matrix room LIVE (still joined, still unencrypted; a failed lookup is
   ineligible), sends through `connector_http._send` exactly once (a Matrix repeat only with the
   same txnId, inside that call), and settles ``sent`` / ``failed`` / ``unconfirmed``.

**Never replayed.** A ``deciding`` entry whose lock is free is ``interrupted``
(`ask_proposals.recover`), shown as "may have been sent". Sending again is a new card and a new
tap. Outcomes are the provider's own facts (`connector_http._send`).

**Email** (#1836) is the same flow through the mail transport (`connector_smtp._send`). Its card
also shows the From address (the connector's ``sender``), the To address, and the subject and
body the text splits into (`connector_smtp.split_mail`). The From address is in the binding and
in the entry, and the transport's last fence requires the store's to still equal it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import unicodedata
import uuid

from . import ask_store, connector_http, connector_smtp, template_secrets
from .connectors import hq as connector_hq
from .connectors import kinds
from .connectors import manifest as manifest_mod
from .connectors import store as connector_store

log = logging.getLogger(__name__)

KIND = "message"
PENDING, DECIDING = "pending", "deciding"
SENT, FAILED, UNCONFIRMED = "sent", "failed", "unconfirmed"
STALE, DISMISSED, INTERRUPTED = "stale", "dismissed", "interrupted"

INTERRUPTED_REASON = (
    "the server stopped while this was being sent: it may have been sent, so check the chat. "
    "It is not sent again."
)

#: An email's (#1836): where the Commander can look is their own sent mail.
INTERRUPTED_REASON_EMAIL = (
    "the server stopped while this was being sent: it may have been sent, so check your sent "
    "mail. It is not sent again."
)


def interrupted_reason(row: dict) -> str:
    return INTERRUPTED_REASON_EMAIL if "sender" in row else INTERRUPTED_REASON


#: Sends in flight in this process: ``{proposal id: task}``.
_TASKS: dict[str, asyncio.Task] = {}

#: Test seams (#1833's kill tests): awaited just before the provider request and just after it.
_HOOK_BEFORE_SEND = None
_HOOK_AFTER_SEND = None


class Refused(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def normalize(text: str) -> str:
    """The text exactly as it will be sent: line endings as ``\\n``, control characters other
    than newline and tab removed, outer whitespace trimmed. Nothing else changes."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = "".join(c for c in t if c in "\n\t" or unicodedata.category(c) != "Cc")
    return t.strip()


def binding(
    instance_id: str, generation: int, recipient_id: str, text: str, sender: str | None = None
) -> str:
    """The card ↔ entry binding. An email's (#1836) also covers its From address; every other
    message's canonical form is unchanged (``sender`` None)."""
    parts: list = [instance_id, generation, recipient_id, text]
    if sender is not None:
        parts.append(sender)
    canon = json.dumps(parts, ensure_ascii=False)
    return template_secrets.keyed_digest("ask-message", canon)


def _email_fields(sender: str, address: str, text: str) -> dict:
    subject, body = connector_smtp.split_mail(text)
    return {"sender": sender, "address": address, "subject": subject, "body": body}


def view(row: dict) -> dict:
    state = row.get("state")
    rec = row.get("recipient") or {}
    email = (
        _email_fields(row["sender"], rec.get("id") or "", row.get("text") or "")
        if isinstance(row.get("sender"), str)
        else {}
    )
    return {
        **email,
        "state": state,
        "connector": row.get("connector") or "",
        # The card's own shape (the live view is merged INTO the card): a label and a kind.
        "recipient": rec.get("label") or "",
        "recipient_kind": rec.get("kind") or "",
        "text": row.get("text") or "",
        "code": row.get("code") or "",
        "reason": row.get("reason") or "",
        "can_send": state == PENDING,
        "can_dismiss": state == PENDING,
    }


# ---- staging ---------------------------------------------------------------------------------


async def stage(*, cid: str, turn_id: str, turn, args: dict, append_card) -> dict:
    """Stage one message card. ``turn`` is this turn's `connectors.hq.TurnConnectors`."""
    idx, listed = connector_hq._listed(turn, args)
    rows = turn.recipients.get(idx)
    if rows is None:
        raise Refused("refused: call list_recipients for this connector first", "unknown_recipient")
    ri = args.get("recipient_index")
    if not isinstance(ri, int) or isinstance(ri, bool) or not 0 <= ri < len(rows):
        raise Refused(
            "refused: use a recipient_index list_recipients returned in this turn",
            "unknown_recipient",
        )
    raw = args.get("text")
    if not isinstance(raw, str):
        raise Refused("text must be the message, as a string", "bad_arguments")
    try:
        inst = await asyncio.to_thread(connector_hq.sendable_now, listed)
    except connector_hq.Refused as e:
        raise Refused(str(e), e.code) from None
    recipient = rows[ri]
    if recipient["id"] not in {r["id"] for r in inst["recipients"]}:
        raise Refused("refused: that recipient is no longer allowed", "unknown_recipient")
    m = manifest_mod.bundled()[inst["manifest_id"]]
    text = normalize(raw)
    limit = kinds.SEND_MAX_CHARS.get(m.adapter, 0)
    if not text:
        raise Refused("refused: the message is empty", "bad_arguments")
    if len(text) > limit:
        raise Refused(f"refused: {m.label} takes at most {limit} characters", "too_long")
    sender: str | None = None
    if m.adapter == "smtp":
        sender = inst["sender"]
        why = connector_smtp.subject_problem(text)
        if why:
            raise Refused(f"refused: {why}", "bad_arguments")
        wire = connector_smtp.build(
            sender=sender, to=recipient["id"], text=text, msg_id="0" * 32, eight_bit=False
        )
        if len(wire) > connector_smtp.WIRE_MAX:
            raise Refused("refused: that email is too large once encoded", "too_long")
    # A secret or a credential-shaped text is refused, never redacted into a different message.
    if await asyncio.to_thread(ask_store.guard_text, text, len(text)) != text:
        raise Refused(
            "refused: the message holds a secret or something that looks like a credential",
            "credential",
        )
    # WHO WOULD READ IT, before the card is shown (#1833, Hermes 7610): a Matrix room is looked up
    # live here, and the card carries its member count. A room that can't be checked is refused
    # now, not offered for a tap the send fence would then turn stale.
    members: int | None = None
    if m.adapter == "matrix-cs":
        why, facts = await _room_now(_bound(inst), recipient["id"])
        if why:
            raise Refused(f"refused: {why.removesuffix('; nothing was sent')}", "unknown_recipient")
        members = facts["members"]
    bind = await asyncio.to_thread(
        binding, inst["id"], inst["generation"], recipient["id"], text, sender
    )
    email = _email_fields(sender, recipient["id"], text) if sender is not None else {}

    pid = uuid.uuid4().hex
    try:
        await append_card(
            cid,
            {
                "type": "proposal",
                "turn_id": turn_id,
                "action_id": pid,
                "kind": KIND,
                "binding": bind,
                "connector": m.label,
                "recipient": recipient["label"],
                "recipient_kind": recipient["kind"],
                **({"recipient_members": members} if isinstance(members, int) else {}),
                "text": text,
                **email,
            },
        )
    except Exception:
        raise Refused(
            "refused: the message could not be recorded; nothing will be sent", "not_recorded"
        ) from None
    entry = {
        "id": pid,
        "turn_id": turn_id,
        "kind": KIND,
        "state": PENDING,
        "ts": time.time(),
        "instance_id": inst["id"],
        "generation": inst["generation"],
        "binding": bind,
        "connector": m.label,
        "recipient": dict(recipient),
        "text": text,
        **({"sender": sender} if sender is not None else {}),
    }
    from . import ask_proposals

    try:
        await asyncio.to_thread(ask_proposals.add, cid, entry)
    except Exception:
        raise Refused(
            "refused: the message could not be recorded; nothing will be sent", "not_recorded"
        ) from None
    out = {"connector": m.label, "recipient": recipient["label"], "characters": len(text)}
    if sender is not None:
        out["subject"] = email["subject"]
    return out


# ---- the tap ---------------------------------------------------------------------------------


def _card_binding(cid: str, pid: str) -> str | None:
    for r in ask_store.read(cid):
        if r.get("type") == "proposal" and r.get("action_id") == pid and r.get("kind") == KIND:
            return r.get("binding")
    return None


def precheck(cid: str, row: dict) -> str | None:
    """Why this entry may not be sent NOW, or None. Blocking."""
    rec = row["recipient"]
    sender = row.get("sender")
    expect = binding(row["instance_id"], row["generation"], rec["id"], row["text"], sender)
    if row.get("binding") != expect or _card_binding(cid, row["id"]) != expect:
        return "the message changed since it was proposed; nothing was sent"
    try:
        doc = connector_store.read()
    except connector_store.StoreUnreadable:
        return "the connector store can't be read just now; nothing was sent"
    inst = next((i for i in doc["instances"] if i["id"] == row["instance_id"]), None)
    if inst is None or not connector_store.sendable(inst):
        return f"{row.get('connector') or 'the connector'} may not send now; nothing was sent"
    if inst["generation"] != row["generation"]:
        return f"{row.get('connector')} changed since this was proposed; nothing was sent"
    if rec["id"] not in {r["id"] for r in inst["recipients"]}:
        return f"{rec.get('label')} is no longer an allowed recipient; nothing was sent"
    if (sender is not None) != ("sender" in inst):
        return "this message is not for that connector; nothing was sent"
    if sender is not None:
        if inst["sender"] != sender:
            return "the From address changed since this was proposed; nothing was sent"
        if not (connector_store.valid_address(rec["id"]) and connector_store.valid_address(sender)):
            return f"{rec.get('label')} is not one plain address now; nothing was sent"
    try:
        if ask_store.guard_text(row["text"], len(row["text"])) != row["text"]:
            return "the message now holds a secret; nothing was sent"
    except ask_store.StoreRefused:
        return "the message could not be checked for secrets; nothing was sent"
    return None


async def decide_locked(cid: str, row: dict, fd: int, decision: str) -> tuple[int, dict]:
    """Decide a message entry. OWNS ``fd`` (the conversation's decide lock) from the call on."""
    from . import ask_proposals

    handed = False
    pid = row["id"]
    try:
        if row.get("state") != PENDING:
            return 200, view(row)
        if decision == "dismiss":
            done = await asyncio.to_thread(
                ask_proposals._update, cid, pid, state=DISMISSED, settled_at=time.time()
            )
            return 200, view(done or row)
        if decision != "send":
            raise ask_proposals.ProposalError(422, "a message is sent or dismissed")
        why = await asyncio.to_thread(precheck, cid, row)
        if why:
            done = await asyncio.to_thread(
                ask_proposals._update, cid, pid, state=STALE, reason=why, settled_at=time.time()
            )
            return 200, view(done or row)
        try:
            await asyncio.to_thread(
                connector_store.take_send,
                row["instance_id"],
                row["generation"],
                row["recipient"]["id"],
            )
        except connector_store.Refused as e:
            state = FAILED if e.code == "rate_limited" else STALE
            done = await asyncio.to_thread(
                ask_proposals._update,
                cid,
                pid,
                state=state,
                code=e.code,
                reason=f"{e}; nothing was sent",
                settled_at=time.time(),
            )
            return 200, view(done or row)
        # DURABLE BEFORE ANYTHING IS SENT: from here a crash leaves `deciding`, which the next
        # read settles as `interrupted` and never sends again.
        approved = dict(row)
        done = await asyncio.to_thread(
            ask_proposals._update, cid, pid, state=DECIDING, decided_at=time.time()
        )
        if done is None:
            raise ask_proposals.ProposalError(404, "no such proposal")
        task = asyncio.create_task(_send_task(cid, approved, fd))
        _TASKS[pid] = task
        handed = True
        return 202, view(done)
    finally:
        if not handed:
            os.close(fd)


def _bound(inst: dict) -> connector_http.Bound:
    m = manifest_mod.bundled()[inst["manifest_id"]]
    return connector_http.Bound(
        instance_id=inst["id"],
        generation=inst["generation"],
        origin=inst["origin"],
        auth_scheme=m.auth_scheme,
        label=m.label,
        adapter=m.adapter,
        for_read=False,
        for_send=True,
    )


async def _room_now(bound: connector_http.Bound, recipient_id: str) -> tuple[str | None, dict]:
    """A Matrix room as it is NOW: ``(why it can't be used, or None; its facts)``. The bot must
    still be in it, it must be unencrypted and its members countable; a lookup that fails is
    ineligible, never assumed fine."""
    from .connectors.adapters import matrix

    try:
        joined = await connector_http._request(bound, "GET", "/_matrix/client/v3/joined_rooms")
        rooms = joined.get("joined_rooms") if isinstance(joined, dict) else None
        if not isinstance(rooms, list) or recipient_id not in rooms:
            return "the bot is no longer in that room; nothing was sent", {}
        facts = await matrix.room_facts(bound, recipient_id)
    except connector_http.ConnectorError:
        return "the room could not be checked just now; nothing was sent", {}
    if facts["unavailable"]:
        return f"that room can't be used: {facts['unavailable']}; nothing was sent", facts
    return None, facts


async def _live_ineligible(bound: connector_http.Bound, recipient_id: str) -> str | None:
    """The send's live room check (`_room_now`); None for a messenger with no rooms."""
    if bound.adapter != "matrix-cs":
        return None
    why, _facts = await _room_now(bound, recipient_id)
    return why


def _recheck(bound: connector_http.Bound, recipient_id: str):
    """Before a Matrix repeat (same txnId): is the room still eligible NOW? A lookup that fails is
    a no (Hermes 7612)."""

    async def check() -> bool:
        return await _live_ineligible(bound, recipient_id) is None

    return check


#: Codes the final check raises (`_final_check`), each read as `stale` with its own reason.
FENCE_REASONS = {
    "recipient_withdrawn": "{label} is no longer an allowed recipient; nothing was sent",
    "send_disabled": "{connector} may not send now; nothing was sent",
    "connector_changed": "{connector} changed since this was proposed; nothing was sent",
    "secret_now": "the message now holds a secret; nothing was sent",
}


def _final_check(row: dict) -> connector_http.FinalCheck:
    """The check `connector_http._send_once` runs at the outbound boundary (#1833, Hermes 7610).

    The claim (`take_send`) checked the allowlist and the secrets, but the request leaves later:
    a recipient withdrawn, or a secret registered, in between must still stop it. Fenced, it runs
    under the registration fence, so a registration lands wholly before it (caught) or after it.
    The connector store's read is atomic, so an allowlist save lands before (caught) or after."""
    rid, text = row["recipient"]["id"], row["text"]

    def refuse(code: str) -> None:
        raise connector_http.ConnectorError(code, FENCE_REASONS[code])

    def check(fenced: bool) -> None:
        fence = template_secrets.registration_fence() if fenced else contextlib.nullcontext()
        with fence:
            try:
                doc = connector_store.read()
            except connector_store.StoreUnreadable:
                refuse("send_disabled")
            inst = next((i for i in doc["instances"] if i["id"] == row["instance_id"]), None)
            if inst is None or not connector_store.sendable(inst):
                refuse("send_disabled")
            if inst["generation"] != row["generation"]:
                refuse("connector_changed")
            if rid not in {r["id"] for r in inst["recipients"]}:
                refuse("recipient_withdrawn")
            try:
                clean = ask_store.guard_text(text, len(text)) == text
            except ask_store.StoreRefused:
                clean = False
            if not clean:
                refuse("secret_now")

    return check


async def _send_task(cid: str, row: dict, fd: int) -> None:
    """Send once and settle, whatever happens. Owns ``fd`` (the decide lock)."""
    from . import ask_proposals

    pid = row["id"]
    started = False
    settle: dict = {}
    try:
        doc = await asyncio.to_thread(connector_store.read)
        inst = next(i for i in doc["instances"] if i["id"] == row["instance_id"])
        bound = _bound(inst)
        if bound.generation != row["generation"]:
            settle = {"state": STALE, "reason": "the connector changed; nothing was sent"}
            return
        why = await _live_ineligible(bound, row["recipient"]["id"])
        if why:
            settle = {"state": STALE, "reason": why}
            return
        if _HOOK_BEFORE_SEND is not None:
            await _HOOK_BEFORE_SEND()
        started = True
        if bound.adapter == "smtp":
            out = await connector_smtp._send(
                bound,
                row["recipient"]["id"],
                row["text"],
                pid,
                row.get("sender") or "",
                final_check=_final_check(row),
            )
        else:
            out = await connector_http._send(
                bound,
                row["recipient"]["id"],
                row["text"],
                idem_key=pid,
                final_check=_final_check(row),
                recheck=_recheck(bound, row["recipient"]["id"]),
            )
        if _HOOK_AFTER_SEND is not None:
            await _HOOK_AFTER_SEND()
        settle = {
            "state": out["outcome"],
            "code": out.get("code", ""),
            "message_id": out.get("message_id", ""),
        }
        if out["outcome"] == FAILED and out.get("code") in FENCE_REASONS:
            # The final check refused it before a byte left: out of date, nothing sent.
            settle["state"] = STALE
            settle["reason"] = FENCE_REASONS[out["code"]].format(
                label=row["recipient"].get("label") or "the recipient",
                connector=row.get("connector") or "the connector",
            )
        elif out["outcome"] == FAILED:
            settle["reason"] = (
                f"{row.get('connector')} did not take it ({out.get('code')}); nothing was sent"
            )
        elif out["outcome"] == UNCONFIRMED:
            settle["reason"] = "no clear answer came back after it went out; it is not sent again"
    except asyncio.CancelledError:
        # Shutdown: leave `deciding` — the next read settles it `interrupted`, never re-sent.
        settle = {}
        raise
    except Exception:  # noqa: BLE001 — every send ends settled
        log.exception("message send %s failed", pid)
        if started:
            settle = {
                "state": UNCONFIRMED,
                "code": "error",
                "reason": "the send ended unexpectedly; it is not sent again",
            }
        else:
            settle = {
                "state": FAILED,
                "code": "error",
                "reason": "the send could not start; nothing was sent",
            }
    finally:
        try:
            if settle:
                await asyncio.to_thread(
                    ask_proposals._update, cid, pid, settled_at=time.time(), **settle
                )
        finally:
            os.close(fd)
            _TASKS.pop(pid, None)


async def drain(timeout: float = 30.0) -> None:
    """Shutdown: wait for sends in flight, then cancel what is left (it reads `interrupted`)."""
    if not _TASKS:
        return
    _done, pending = await asyncio.wait(list(_TASKS.values()), timeout=timeout)
    for t in pending:
        t.cancel()
