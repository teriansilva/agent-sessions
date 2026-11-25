"""The `telegram-bot` adapter (#1832): a bot the Commander created, read for two facts only.

* ``GET /getMe`` — the probe: the bot's username, which `tested.login` keeps and the recipient
  allowlist is bound to (a different bot is a new consent).
* ``GET /getUpdates?limit=100`` — recipient discovery: the chats that recently wrote to the bot.

The transport writes the token into the path (`/bot<token>/…`); this module names only the method.

**Metadata only.** From each update only its ``chat`` is taken: the id, the type, and a title or
name. Message text, captions, files and the sender's other fields are never copied out of the
parsed reply, never logged and never stored; the reply itself goes out of scope when this returns.
**Nothing is acknowledged:** `getUpdates` is never sent an ``offset``, so another consumer of the
same bot still receives every update. A bot with a webhook can't be read this way; Telegram
answers 409, reported as ``webhook_active``.
"""

from __future__ import annotations

from ... import connector_http
from ...connector_http import Bound, ConnectorError
from .common import text

UPDATES_LIMIT = 100
CANDIDATES_MAX = 50
#: The update fields that carry a chat, in the order checked.
CHAT_FIELDS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
)
KINDS = {
    "private": "private chat",
    "group": "group",
    "supergroup": "supergroup",
    "channel": "channel",
}


async def _get(bound: Bound, method: str, params: dict | None = None) -> object:
    try:
        got = await connector_http._request(bound, "GET", f"/{method}", params)
    except ConnectorError as e:
        if e.status == 409:
            raise ConnectorError(
                "webhook_active",
                "This bot has a webhook, so BattleLab can't see its chats. Use a separate bot "
                "for BattleLab.",
                status=409,
            ) from None
        raise
    if not isinstance(got, dict) or got.get("ok") is not True:
        raise ConnectorError("bad_reply", "the address did not answer as Telegram does")
    return got.get("result")


async def probe(bound: Bound) -> dict:
    me = await _get(bound, "getMe")
    username = me.get("username") if isinstance(me, dict) else None
    if not isinstance(username, str) or not me.get("is_bot"):
        raise ConnectorError("bad_reply", "that token does not belong to a bot")
    return {"login": username, "repos": 0}


def _chat_of(update: object) -> dict | None:
    if not isinstance(update, dict):
        return None
    for field in CHAT_FIELDS:
        holder = update.get(field)
        chat = holder.get("chat") if isinstance(holder, dict) else None
        if isinstance(chat, dict):
            return chat
    return None


def _label(chat: dict) -> str:
    title = text(chat.get("title"), 100)
    if title:
        return title
    name = " ".join(
        p for p in (text(chat.get("first_name"), 60), text(chat.get("last_name"), 60)) if p
    )
    user = text(chat.get("username"), 40)
    if name and user:
        return f"{name} (@{user})"[:100]
    return name or (f"@{user}" if user else "")


async def candidates(bound: Bound) -> list[dict]:
    """``[{id, label, kind}]``: the distinct chats of the recent updates, newest first."""
    updates = await _get(bound, "getUpdates", {"limit": UPDATES_LIMIT})
    seen: dict[str, dict] = {}
    for update in reversed(updates if isinstance(updates, list) else []):
        chat = _chat_of(update)
        if chat is None:
            continue
        cid = chat.get("id")
        if not isinstance(cid, int) or isinstance(cid, bool):
            continue
        key = str(cid)
        kind = KINDS.get(chat.get("type"))
        label = _label(chat)
        if key in seen or kind is None or not label or not label.isprintable():
            continue
        seen[key] = {"id": key, "label": label, "kind": kind}
        if len(seen) >= CANDIDATES_MAX:
            break
    return list(seen.values())
