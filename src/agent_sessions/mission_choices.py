"""The approved per-mission menu grant, owned by the mission controller (#1019, #1060).

It shares the supervisor's existing model call and bounded input. Parsing, confidence, menu
binding, persistence and delivery use the existing orchestrator/actuator contracts. No standalone
pass may propose for a held session, and no model field can manufacture the server's grant.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from . import actuator, engines, mission_fence, missions, orchestrator, prefs, screen_menus


def snapshot(session_key: str) -> dict | None:
    """Read the exact menu offered to the model, only under the mission's current opt-in."""
    cfg = prefs.get_mission_orchestration()
    if not cfg.get("enabled") or cfg.get("autonomy") != "yolo":
        return None
    try:
        if not missions.auto_choose_mission(session_key):
            return None
        screen = orchestrator.scrollback.live_tail_text(
            mission_fence.physical_of(session_key), orchestrator.PROMPT_SCREEN_CHARS
        )
        menu = screen_menus.parse(screen, screen_menus.engine_of(session_key))
        digest = orchestrator._digest_menu(menu) if menu is not None else None
        # Digest bounds currently fit easily; never carry authority for context omitted by a
        # future parser's larger menu. The supervisor's existing input cap remains unchanged.
        if digest is not None and len(json.dumps(digest, ensure_ascii=False)) > 3000:
            return None
        return digest
    except Exception:  # an unreadable opt-in or frame is no grant
        return None


def model_input(body: str, fingerprint: str, menu: dict | None, limit: int) -> tuple[str, str]:
    """Put the server-parsed menu inside the existing session-input budget, never beside it."""
    if menu is None:
        return body, fingerprint
    section = "\n\nSERVER-PARSED MENU:\n" + json.dumps(menu, ensure_ascii=False)
    if len(section) > limit:
        return body, fingerprint  # caller must not offer choices for an omitted menu
    combined = body[: max(0, limit - len(section))] + section
    return combined, hashlib.sha256((fingerprint + section).encode()).hexdigest()


def reading(reply: object, menu: dict | None) -> dict | None:
    """Keep only a numbered choice from the server menu actually included in this reading."""
    raw = reply.get("choose") if isinstance(reply, dict) else None
    if not isinstance(raw, dict) or menu is None:
        return None
    option, conf = raw.get("option"), raw.get("confidence")
    if not isinstance(option, int) or isinstance(option, bool):
        return None
    if not any(o.get("n") == option for o in menu["options"]):
        return None
    if not isinstance(conf, int | float) or isinstance(conf, bool) or not (0 <= conf <= 1):
        return None
    return {
        "verb": "choose",
        "option": option,
        "confidence": float(conf),
        "reason": str(raw.get("reason") or "")[:400],
    }


async def propose(
    mission_id: str, session_key: str, original: dict, *, registry=None
) -> dict | None:
    """Persist and attempt this reading's ONE choice under its ORIGINAL automation authority.

    None means no action was persisted, so the supervisor may reconsider unchanged input on
    the next sweep. Any persisted action returns its record, including uncertain deliveries.
    """
    action = reading({"choose": original.get("choose")}, original.get("seen_menu"))
    if action is None:
        return None
    ctx = await missions.run_admitted(
        lambda: orchestrator.auto_choose_context(
            session_key, action["option"], original.get("seen_menu")
        )
    )
    if ctx is None or ctx["mission_id"] != mission_id:
        return None
    cfg = prefs.get_mission_orchestration()
    state, reason = orchestrator._decide(action, cfg, auto_choose=True)
    now = time.time()
    rec = {
        **{key: value for key, value in action.items() if key != "reason"},
        "rationale": action["reason"],
        "id": uuid.uuid4().hex,
        "session_id": session_key,
        "mission_id": mission_id,
        "authority": original.get("authority") or {},
        "state": state,
        "tier": cfg["autonomy"],
        "ts": now,
        "expires_at": now + int(cfg["proposal_ttl_minutes"]) * 60,
        "engine": screen_menus.engine_of(session_key),
        "precondition": ctx["precondition"],
        "label": ctx["label"],
        "menu": ctx["menu"],
    }
    if state == "approved":
        rec["auto_choose"] = True
    if reason:
        rec["escalation_reason"] = reason
    term = engines.terminal_of(screen_menus.engine_of(session_key))
    if state == "approved" and term is not None and term.menu_digit_submits:
        rec["submit"] = "digit"
    kept = await missions.run_admitted(lambda: orchestrator._persist([rec]))
    if not kept:
        return None
    outcomes = await actuator.deliver_pass_actions(kept, registry=registry)
    return outcomes[0] if outcomes else kept[0]
