""" "Approve always" grants for API sessions (#1339 Phase 2, operator-approved in #1339).

Operator direction (2026-10-08): "dont be too strict with the checks. people should be able to
do what they want with their battlelab instance … if they want to live dangerously, they can."
So every standing grant the client ITSELF proposed for this request is offered, broad ones
included, each LABELLED honestly: what it matches, how long it lasts and where it is saved, plus
the request's advisory RISKY mark. What remains is integrity, not policy:

* only a grant the request's own payload proposed is offered or accepted (server AND worker
  re-derive it; a forged or altered grant is refused), never one BattleLab invents;
* only shapes this module can describe truthfully: Codex ``acceptForSession`` and
  ``acceptWithExecpolicyAmendment`` (an argv prefix); Claude ``addRules`` with behavior
  ``allow`` and ``addDirectories``, to any destination it names. A suggestion that would CHANGE
  THE SESSION'S MODE (``setMode``) is not a grant and is never offered — the permission mode is
  fixed at create (skip-permissions is chosen there) — and neither are ``deny``/``ask``/remove
  or replace suggestions, which are not approvals;
* malformed suggestions are skipped and never break the read.

Pure functions: the server derives the offered choices from the journaled payload, and the
native worker re-derives them from the request it holds before writing any response. A grant's
id is a hash of the grant object, so both sides agree without sharing state.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import risk_marks

_DESTINATIONS = {
    "session": ("session", "this session only"),
    "localSettings": (
        "persistent",
        "saved to this project, only you (.claude/settings.local.json)",
    ),
    "projectSettings": ("persistent", "saved to this project, shared (.claude/settings.json)"),
    "userSettings": ("persistent", "saved to your user settings — every project (~/.claude)"),
}
_GRANT_ID = re.compile(r"^g[0-9a-f]{16}$")


def grant_id(grant) -> str:
    raw = json.dumps(grant, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "g" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def valid_grant_id(value) -> bool:
    return isinstance(value, str) and _GRANT_ID.fullmatch(value) is not None


def _quote(words: list[str]) -> str:
    return " ".join(words)


def _risk_note(risk: dict | None) -> str:
    if risk and risk.get("level") == risk_marks.RISKY:
        return " · RISKY: " + "; ".join(risk.get("reasons") or [])
    if risk and risk.get("level") == risk_marks.UNKNOWN:
        return " · not classified"
    return ""


def _has_command(command) -> bool:
    if isinstance(command, str):
        return bool(command.strip())
    return (
        isinstance(command, list)
        and bool(command)
        and all(isinstance(t, str) for t in command)
        and any(t.strip() for t in command)
    )


def _claude_target(tool, tool_input) -> bool:
    """A standing grant needs something the operator SAW it would cover (Hermes 5919): a command
    for a command tool, a path for a file tool, a URL for a fetch, else a non-empty input."""
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return False
    if tool in risk_marks.COMMAND_TOOLS:
        return _has_command(tool_input.get("command"))
    for key in ("file_path", "notebook_path", "path", "url"):
        if key in tool_input:
            value = tool_input[key]
            return isinstance(value, str) and bool(value.strip())
    return any(v not in (None, "", [], {}) for v in tool_input.values())


def _words(command) -> list[str]:
    words = risk_marks.command_words(command)
    return words or ([command] if isinstance(command, str) else [])


def _codex(payload: dict, cwd: str | None) -> tuple[dict, list[dict]]:
    command = payload.get("command")
    # The SESSION folder, as for a tool row, so a card and its row always agree.
    risk = risk_marks.classify(command, cwd)
    note = _risk_note(risk)
    offered = payload.get("availableDecisions")
    if not isinstance(offered, list) or not _has_command(command):
        return risk, []  # no displayed command: declineable, never a blind standing grant
    out = []
    for decision in offered:
        if decision == "acceptForSession":
            out.append(
                {
                    "grant": "acceptForSession",
                    "scope": "session",
                    "label": f"Allow `{_quote(_words(command))}` for the rest of this session"
                    + note,
                }
            )
        elif isinstance(decision, dict) and set(decision) == {"acceptWithExecpolicyAmendment"}:
            body = decision["acceptWithExecpolicyAmendment"]
            prefix = body.get("execpolicy_amendment") if isinstance(body, dict) else None
            if (
                not isinstance(body, dict)
                or set(body) != {"execpolicy_amendment"}
                or not isinstance(prefix, list)
                or not prefix
                or not all(isinstance(t, str) and t for t in prefix)
            ):
                continue  # malformed: skipped, never guessed
            what = (
                f"every `{prefix[0]}` command"
                if len(prefix) == 1
                else f"`{_quote(prefix)}` and any command starting with it"
            )
            out.append(
                {
                    "grant": decision,
                    "scope": "persistent",
                    "label": f"Always allow {what} · saved to Codex's exec policy (persists)"
                    + note,
                }
            )
    return risk, out


def _rule_text(rule) -> str | None:
    """`Tool(spec)` / `Tool` for a well-formed rule, else None (malformed → skipped)."""
    if not isinstance(rule, dict) or set(rule) - {"toolName", "ruleContent"}:
        return None
    name, spec = rule.get("toolName"), rule.get("ruleContent")
    if not isinstance(name, str) or not name:
        return None
    if spec is None:
        return name
    if not isinstance(spec, str):
        return None
    return f"{name}({spec})"


def _rule_label(rule) -> str:
    name, spec = rule["toolName"], rule.get("ruleContent")
    if spec is None or spec.strip() in {"", "*", ":*"}:
        return f"every `{name}` call"
    if name == "Bash" and spec.endswith(":*"):
        return f"`{spec[:-2]}` and any command starting with it"
    return f"`{name}({spec})`"


def _claude(payload: dict, cwd: str | None) -> tuple[dict | None, list[dict]]:
    tool = payload.get("tool_name")
    tool_input = payload.get("input")
    risk = risk_marks.classify_tool(tool, tool_input, cwd)
    note = _risk_note(risk)
    suggestions = payload.get("permission_suggestions")
    if not isinstance(suggestions, list) or not _claude_target(tool, tool_input):
        return risk, []  # nothing displayed it would cover: declineable, never a blind grant
    out = []
    for s in suggestions:
        if not isinstance(s, dict) or not isinstance(s.get("destination"), str):
            continue
        place = _DESTINATIONS.get(s["destination"])
        if place is None:
            continue  # a destination this cannot describe truthfully
        scope, where = place
        if (
            s.get("type") == "addRules"
            and set(s) == {"type", "rules", "behavior", "destination"}
            and s["behavior"] == "allow"
            and isinstance(s["rules"], list)
            and 1 <= len(s["rules"]) <= 8
        ):
            shown = [_rule_text(r) for r in s["rules"]]
            if None in shown:
                continue
            what = "; ".join(_rule_label(r) for r in s["rules"])
            out.append(
                {
                    "grant": s,
                    "scope": scope,
                    "label": f"Always allow {what} · {where}" + note,
                    "rules": shown,
                }
            )
        elif (
            s.get("type") == "addDirectories"
            and set(s) == {"type", "directories", "destination"}
            and isinstance(s["directories"], list)
            and 1 <= len(s["directories"]) <= 8
            and all(isinstance(d, str) and d for d in s["directories"])
        ):
            dirs = ", ".join(f"`{d}`" for d in s["directories"])
            out.append(
                {
                    "grant": s,
                    "scope": scope,
                    "label": f"Allow access to {dirs} · {where}" + note,
                    "rules": [f"directory: {d}" for d in s["directories"]],
                }
            )
        # setMode, deny/ask, removeRules, replaceRules, anything else: not an approval — skipped
    return risk, out


def derive(adapter: str, kind: str | None, payload, cwd: str | None) -> dict:
    """``{risk, always: [{id, label, scope, grant}]}`` for one complete pending request.

    ``payload`` is the request as presented (the journaled summary, parsed). Anything that is
    not a dict yields no grant and an ``unknown`` risk for a command."""
    if not isinstance(payload, dict):
        command_like = kind in risk_marks.COMMAND_TOOLS
        return {"risk": risk_marks.unknown() if command_like else None, "always": []}
    try:
        if adapter == "codex-app-server":
            if kind != "command":
                return {"risk": None, "always": []}  # a file change is never approvable
            risk, choices = _codex(payload, cwd)
        elif adapter == "claude-stream-json":
            risk, choices = _claude(payload, cwd)
        else:
            return {"risk": None, "always": []}  # any other adapter maps no standing grant
    except Exception:  # noqa: BLE001 — client-supplied shapes: fail closed, never break the read
        # A malformed request must stay a declineable prompt (Hermes on #1342): no grant, and a
        # risk nobody can vouch for. The snapshot reader and the worker gate both get this.
        return {"risk": risk_marks.unknown(), "always": []}
    seen, always = set(), []
    for c in choices:
        gid = grant_id(c["grant"])
        if gid in seen:
            continue
        seen.add(gid)
        always.append({"id": gid, **c})
    return {"risk": risk, "always": always}


def public(choice: dict) -> dict:
    """What a snapshot shows: never the raw grant object's internals beyond its rule text."""
    out = {"id": choice["id"], "label": choice["label"], "scope": choice["scope"]}
    if "rules" in choice:
        out["rules"] = list(choice["rules"])
    return out
