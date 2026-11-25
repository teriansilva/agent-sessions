"""Send a message (#2248 P2): through a messenger the operator set up, to a recipient they allowed.

``inputs = {connector, recipient, message}``: the connector instance (Telegram, Matrix or mail,
`connectors/`), one of ITS saved recipients (`list_recipients`' data: the allowlist the operator
built in Settings → AI → HQ connectors, never an address typed per run), and the P2a message —
text, or a template with fixed values and typed values from a webhook delivery
(``from_delivery``). A delivery fills template fields and nothing else: it never chooses the
messenger, the recipient or the From address.

**One send site.** The message goes through the connectors' own transport and fences:
`connector_http._send` (Telegram, Matrix) or `connector_smtp._send` (mail), with the token fetch
fenced on SENDING (`Bound(for_send=True)`: switched on, tested, recipients bound to this bot), the
rate claim (`store.take_send`), a Matrix room checked live, and a final check at the outbound
boundary (`_final_check`), run under the registration fence and again before the first byte:
sending still on at this generation, the recipient still allowed, the From address the one
approved, no secret in the text now, AND the automation's own authority. A refusal there sends
nothing.

**Consent** names the messenger, its kind, the recipient and (mail) the From address. The From
address is approved content: it is pinned, and a changed one needs approval again. A template that
carries a secret is refused (at save and again at the run): a message leaves BattleLab in clear.
"""

from __future__ import annotations

import contextlib

from .. import automations as model
from . import plugins

ID = "send_message"
LABEL = "Send a message"
WEBHOOK = True
MESSAGE = True
#: Why a template with a secret is refused (the refusal's words, not a secret).
SECRET_REFUSAL = (
    "a template with a secret can't be sent as a message: "  # noqa: S105
    "it would leave BattleLab in clear text"
)
TARGET_WORDS = {
    "connector": "a different messenger",
    "connector_kind": "a different messenger",
    "recipient": "a different recipient",
    "sender": "a different From address",
}
_INPUTS = {"connector", "recipient", "message"}


def _store():
    from ..connectors import store

    return store


# ---- validation and save ------------------------------------------------------------------------


def validate_inputs(inputs: dict, mapped: dict) -> dict:
    cstore = _store()
    model._obj(inputs, "a send_message action's inputs", _INPUTS, _INPUTS)
    iid = inputs["connector"]
    if not isinstance(iid, str) or not cstore.ID_RE.fullmatch(iid):
        raise model.AutomationError("connector must be a connector id")
    rid = inputs["recipient"]
    if not isinstance(rid, str) or not (
        cstore.RECIPIENT_ID_RE.fullmatch(rid) or cstore.valid_address(rid)
    ):
        raise model.AutomationError("recipient must be one of the connector's saved recipients")
    return {"connector": iid, "recipient": rid, "message": inputs["message"]}


def mappable(_inputs: dict) -> set[str]:  # pragma: no cover — a message plugin maps its template
    return set()


def _instance(iid: str) -> dict | None:
    doc = _store().read()
    return next((i for i in doc["instances"] if i["id"] == iid), None)


def _adapter(inst: dict) -> str:
    from ..connectors import manifest as manifest_mod

    m = manifest_mod.bundled().get(inst["manifest_id"])
    return m.adapter if m is not None else ""


def _label(inst: dict) -> str:
    from ..connectors import manifest as manifest_mod

    m = manifest_mod.bundled().get(inst["manifest_id"])
    return m.label if m is not None else inst["manifest_id"]


def check_save(config: dict) -> None:
    cstore = _store()
    inp = config["action"]["inputs"]
    try:
        inst = _instance(inp["connector"])
    except cstore.StoreUnreadable:
        raise model.AutomationError("the connector settings can't be read right now", 503) from None
    if inst is None or not cstore.MESSENGER_KEYS <= set(inst):
        raise model.AutomationError("that messenger doesn't exist (Settings → AI → HQ connectors)")
    if not cstore.sendable(inst):
        raise model.AutomationError(
            f"{_label(inst)} can't send now: test it, allow a recipient and switch sending on "
            "in Settings → AI → HQ connectors"
        )
    if inp["recipient"] not in {r["id"] for r in inst["recipients"]}:
        raise model.AutomationError("that recipient isn't one you allowed for this messenger")
    msg = inp["message"]
    if "text" in msg:
        _text_problem(inst, msg["text"], raise_=True)


def _text_problem(inst: dict, text: str, *, raise_: bool = False) -> str | None:
    from .. import connector_smtp
    from ..connectors import kinds

    adapter = _adapter(inst)
    cap = kinds.SEND_MAX_CHARS.get(adapter, 4096)
    why = None
    if len(text) > cap:
        why = f"{_label(inst)} takes at most {cap} characters"
    elif adapter == "smtp":
        why = connector_smtp.subject_problem(text)
    if why and raise_:
        raise model.AutomationError(why)
    return why


# ---- pins, scope, consent -----------------------------------------------------------------------


def pins(action: dict) -> dict:
    """The messenger as approved: its kind and (mail) its From address. A removed one is a drift
    (it can never come back under the same id); a send switched off is refused at the run."""
    cstore = _store()
    iid = action["inputs"]["connector"]
    try:
        inst = _instance(iid)
    except cstore.StoreUnreadable as e:
        raise model.PinsUnavailable(f"the connector settings could not be read ({e})") from None
    if inst is None or not cstore.MESSENGER_KEYS <= set(inst):
        return {"connector": iid, "missing": "that messenger was removed from your connectors"}
    return {"connector": iid, "kind": inst["manifest_id"], "sender": inst.get("sender")}


def drift(stored: dict, current: dict) -> str:
    if stored.get("sender") != current.get("sender"):
        return "the messenger's From address changed since you approved this automation"
    return "the messenger changed since you approved this automation"


def target(action: dict, pins_: dict) -> dict:
    p = pins_.get("plugin") or {}
    inp = action["inputs"]
    return {
        "connector": inp["connector"],
        "connector_kind": p.get("kind"),
        "recipient": inp["recipient"],
        "sender": p.get("sender"),
    }


def data(_action: dict) -> dict:
    return {}  # the message is the scope's own `message` field, disclosed in full


def _names(iid: object, rid: object) -> tuple[str, str]:
    """The messenger's and the recipient's labels as stored now, for the consent lines only (the
    ids are what is approved and are always shown beside them)."""
    with contextlib.suppress(Exception):
        inst = _instance(str(iid))
        if inst is not None:
            rec = next((r for r in inst.get("recipients", []) if r["id"] == rid), None)
            return _label(inst), (rec or {}).get("label") or str(rid)
    return "a messenger", str(rid)


def lines(scope: dict) -> list[str]:
    t = scope.get("target") or {}
    conn, rec = _names(t.get("connector"), t.get("recipient"))
    out = [
        f"Sends a message through {conn} (connector {t.get('connector')}, "
        f"kind {t.get('connector_kind')})",
        f"To {rec} ({t.get('recipient')}), a recipient you allowed for it",
    ]
    if t.get("sender") is not None:
        out.append(f"From {t.get('sender')}")
    out.append("A delivery never changes the messenger, the recipient or the From address")
    out.append(
        "Before each send it checks that sending is still on, the recipient is still allowed and "
        "the text holds no secret; otherwise nothing is sent"
    )
    return out


# ---- the run ------------------------------------------------------------------------------------

#: Why the final check refused, read as `refused` with nothing sent.
_FENCE = {
    "recipient_withdrawn": "that recipient is no longer allowed; nothing was sent",
    "send_disabled": "that messenger may not send now; nothing was sent",
    "connector_changed": "the messenger changed; nothing was sent",
    "secret_now": "the message now holds a secret; nothing was sent",
    "stale": "the messenger changed; nothing was sent",
    "needs_token": "the messenger's token needs to be entered again; nothing was sent",
}
_STOPPED = "automation_stopped"


def _final_check(run: dict, inst0: dict, rid: str, sender: str | None, text: str, stop: list):
    """The check `connector_http._send_once` (and the mail transport) runs at the outbound
    boundary: under the registration fence, and again before the first byte. The connector's
    state as approved, no secret in the text, and the automation's authority."""
    from .. import ask_store, connector_http, template_secrets

    cstore = _store()

    def refuse(code: str) -> None:
        raise connector_http.ConnectorError(code, _FENCE.get(code, "refused"))

    def check(fenced: bool) -> None:
        fence = template_secrets.registration_fence() if fenced else contextlib.nullcontext()
        with fence:
            out = plugins.final(run)
            if out:
                stop[:] = [out]
                refuse(_STOPPED)
            try:
                inst = _instance(inst0["id"])
            except cstore.StoreUnreadable:
                refuse("send_disabled")
            if inst is None or not cstore.sendable(inst):
                refuse("send_disabled")
            if inst["generation"] != inst0["generation"] or inst.get("sender") != sender:
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


def effect(run: dict, config: dict, pins_: dict) -> tuple[str, str]:
    """BLOCKING, under the effect lock, after the authority read."""
    from .. import ask_messages, ask_store, connector_http, connector_smtp
    from .. import automation_runner as runner
    from .. import automations_store as store

    cstore = _store()
    inp = config["action"]["inputs"]
    iid, rid = inp["connector"], inp["recipient"]
    try:
        text, rendered = runner._render_plain(config, pins_, carried_refusal=SECRET_REFUSAL)
    except runner.RunRefused as e:
        return "refused", e.detail
    text = ask_messages.normalize(text)
    if not text:
        return "refused", "the message is empty; nothing was sent"
    # The messenger as it is NOW: the saved configuration was re-read above (`_render_plain`), and
    # its permissions and revocation are read here, then again at the outbound boundary.
    try:
        inst = _instance(iid)
    except cstore.StoreUnreadable:
        return "refused", "the connector settings can't be read right now; nothing was sent"
    if inst is None:
        return "refused", "that messenger was removed; nothing was sent"
    label = _label(inst)
    if not cstore.sendable(inst):
        return (
            "refused",
            f"{label} may not send now (switched off, untested or re-bound); nothing was sent",
        )
    rec = next((r for r in inst["recipients"] if r["id"] == rid), None)
    if rec is None:
        return "refused", "that recipient is no longer allowed; nothing was sent"
    sender = (pins_.get("plugin") or {}).get("sender")
    if inst.get("sender") != sender:
        return "refused", "the From address changed since you approved this; nothing was sent"
    why = _text_problem(inst, text)
    if why:
        return "refused", f"{why}; nothing was sent"
    try:
        if ask_store.guard_text(text, len(text)) != text:
            return "refused", "the message holds a secret or a credential; nothing was sent"
    except ask_store.StoreRefused:
        return "refused", "the message could not be checked for secrets; nothing was sent"
    # Recorded BEFORE anything leaves: a crash after it is `interrupted: outcome unknown`, never a
    # second send. The text holds no secret (checked above).
    store.set_inputs(
        run["id"],
        {**rendered, "via": f"{label} ({iid})", "to": f"{rec['label']} ({rid})"},
    )
    stop = plugins.final(run)
    if stop:
        return stop
    try:
        cstore.take_send(iid, inst["generation"], rid)
    except cstore.Refused as e:
        return "refused", f"{e}; nothing was sent"
    bound = ask_messages._bound(inst)
    why = plugins.run_async(ask_messages._live_ineligible(bound, rid))
    if why:
        return "refused", why
    stopped: list = []
    check = _final_check(run, inst, rid, sender, text, stopped)
    if _adapter(inst) == "smtp":
        out = plugins.run_async(
            connector_smtp._send(bound, rid, text, run["id"], sender or "", final_check=check)
        )
    else:
        out = plugins.run_async(
            connector_http._send(
                bound,
                rid,
                text,
                idem_key=run["id"],
                final_check=check,
                recheck=ask_messages._recheck(bound, rid),
            )
        )
    code = out.get("code") or ""
    if out["outcome"] == "sent":
        return plugins.delivered(run, "ok", f"sent to {rec['label']} through {label}")
    if out["outcome"] == "failed":
        if code == _STOPPED and stopped:
            return stopped[0]
        if code in _FENCE:
            return "refused", _FENCE[code]
        return "failed", f"{label} did not take it ({code or 'error'}); nothing was sent"
    # `unconfirmed`: it may have gone out. Never sent again (no run is ever replayed).
    return "failed", (
        f"no clear answer came back from {label} after it went out ({code or 'unknown'}); "
        "it may have been sent and is not sent again"
    )
