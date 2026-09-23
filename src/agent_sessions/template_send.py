"""Server-side template send (#1090, Phase 2) — the only way a template with a secret field is sent.

A template with any ``kind: "secret"`` field cannot go through the composer: the composer is in
the browser, and the browser never holds a stored secret. So such a template is rendered HERE —
the same literal ``{{name}}`` substitution as ``web/src/lib/templateMessage.ts``, pinned against it
by a shared fixture (``tests/fixtures/template_render_cases.json``) — and delivered through
``session_input.send_input``, the single server-owned write seam. The browser gets back only the
MASKED text (``[secret: name]``), which is what its sent history keeps.

**Delivery is three fenced writes, spaced like the composer's.** Clear the prompt line (Ctrl-A
Ctrl-K), wait ``CLEAR_DELAY_S``, one bracketed paste, wait ``ENTER_DELAY_S`` (longer with images),
then Enter — each its own ``send_input`` call, because a clear or an Enter landing in the same PTY
read as the paste is the "press Enter twice" bug (#180, #226, #1062). Only the first write waits
for the screen to go quiet; the paste's own echo would otherwise hold the Enter back.

**Every write is fenced by the same re-check**, as ``precondition`` (after the quiet wait) and as
``final_guard`` (under the write lock, right before byte one): the session's cwd is still inside the
operator's roots/exclusions, and the template is still the revision the picker showed. Its
``policy_fingerprint`` digests the roots, the exclusions and the secret key's id, so a change
between the guard and the write refuses too. Scope is also checked at ENTRY, so an out-of-scope
session is a plain 403 and ``send_input`` is never called.

**What each outcome means** is the seam's, not ours: ``refused`` is a busy or unready session,
``failed`` wrote zero bytes, and only ``aborted`` is a partial write — the one case that may have
put part of a message in front of the agent. A paste that landed but whose Enter did not is said
plainly: typed, not submitted.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import threading
import time
from dataclasses import dataclass

from . import engines, prefs, project_dirs, session_input, template_secrets, template_vars
from . import templates as tstore

FIELD_TOKEN_RE = re.compile(r"\{\{([a-z][a-z0-9_]{0,31})\}\}")
CLEAR_DELAY_S = 0.08
ENTER_DELAY_S = 0.06
ENTER_DELAY_AFTER_IMAGES_S = 0.12
LINE_CLEAR = b"\x01\x0b"

#: Reason prefixes our own guard uses, so a ``stale`` outcome can say WHICH of our facts moved.
_SCOPE = "scope:"
_TEMPLATE = "template:"


class SendRefused(Exception):
    """A send that must not happen (or did not complete). ``status`` is the HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Rendered:
    text: str  # what the agent receives — holds plaintext secrets; never leaves the server
    masked: str  # what the browser gets back and its sent history keeps
    typed_secrets: tuple[str, ...]  # typed-once values
    secrets_used: tuple[str, ...] = ()  # EVERY secret value this message carries, stored or typed


def mask(name: str) -> str:
    return f"[secret: {name}]"


def substitute(body: str, fields: list[dict], values: dict[str, str]) -> str:
    """Literal ``{{name}}`` replace for DECLARED fields that have a value — the TS twin's rule."""
    declared = {f["name"] for f in fields}

    def one(m: re.Match) -> str:
        name = m.group(1)
        if name not in declared or name not in values:
            return m.group(0)
        return values[name]

    return FIELD_TOKEN_RE.sub(one, body)


def assemble(text: str, paths: list[str]) -> str:
    """``Compose.send()``'s assembly: trimmed text (if any), then each path, space-joined."""
    parts = [text.strip()] if text.strip() else []
    parts.extend(paths)
    return " ".join(parts)


def _value(raw: object, name: str) -> str:
    """A value the picker sent, under the template body's own rules (string, no control
    characters — ESC could end the bracketed paste early, #618 — and the body's length cap)."""
    return tstore._text({name: raw}, name, max_len=tstore.BODY_MAX, multiline=True)


def render(
    template: dict,
    values: object,
    *,
    library: dict[str, str],
    secrets: dict[str, str],
    secret_state: dict[str, str],
) -> Rendered:
    """Resolve every field and render. ``TemplateError`` = bad input (422); ``SendRefused`` = a
    library value the send needs is missing, the wrong kind, or needs re-entry (409)."""
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise tstore.TemplateError("values must be an object")
    fields = template["fields"]
    declared = {f["name"]: f for f in fields}
    unknown = sorted(set(values) - set(declared))
    if unknown:
        raise tstore.TemplateError(f"values for fields this template does not declare: {unknown}")

    real: dict[str, str] = {}
    shown: dict[str, str] = {}
    typed: list[str] = []
    for f in fields:
        name, kind, source = f["name"], f["kind"], f["source"]
        given = values.get(name)
        if kind == "secret" and source == "library":
            if given is not None:
                raise tstore.TemplateError(f"{name} is a stored secret and cannot be overridden")
            state = secret_state.get(name)
            if state is None:
                raise SendRefused(409, f"the library has no secret named {name}")
            if state != "ok" or name not in secrets:
                raise SendRefused(409, f"{name} needs re-entry — nothing was sent")
            real[name], shown[name] = secrets[name], mask(name)
        elif kind == "secret":
            v = "" if given is None else _value(given, name)
            if len(v) < template_secrets.SECRET_MIN:
                raise tstore.TemplateError(
                    f"{name} must be at least {template_secrets.SECRET_MIN} characters"
                )
            if v != v.strip():
                # Assembly trims the message: an edge secret would be sent in a form the
                # redaction set does not hold (Hermes on #1105).
                raise tstore.TemplateError(f"{name} cannot start or end with whitespace")
            real[name], shown[name] = v, mask(name)
            typed.append(v)
        elif source == "library":
            if name not in library:
                # A secret of the same name is NOT a text value — a kind mismatch is "missing".
                raise SendRefused(409, f"the library has no text variable named {name}")
            v = library[name] if given is None else _value(given, name)
            real[name] = shown[name] = v
        else:
            v = f["default"] if given is None else _value(given, name)
            real[name] = shown[name] = v
        if f["required"] and not real[name].strip():
            raise tstore.TemplateError(f"{f['label'] or name} is required")

    paths = [i["path"] for i in template["images"]]
    text = assemble(substitute(template["body"], fields, real), paths)
    masked = assemble(substitute(template["body"], fields, shown), paths)
    used = tuple(real[f["name"]] for f in fields if f["kind"] == "secret" and f["name"] in real)
    return Rendered(text=text, masked=masked, typed_secrets=tuple(typed), secrets_used=used)


# ---- delivery ----------------------------------------------------------------------------------


def _boundary() -> tuple[list[str], list[str]]:
    return project_dirs.effective_roots(), prefs.get_folder_exclusions()


def _scope_ok(cwd: str | None) -> bool:
    roots, exclusions = _boundary()
    if cwd is None:
        # No row to authorize against: fail CLOSED exactly where a boundary is configured — the
        # terminal ATTACH rule (routes/terminal.py, #867 round 7).
        return not (roots or exclusions)
    return project_dirs.in_scope(cwd, roots=roots, exclusions=exclusions)


def resolve_target(session: object) -> tuple[str, str | None]:
    """``(physical key, cwd)`` for the session the picker is in, or ``SendRefused``.

    ``parse_key`` refuses an unreconciled ``new-<uuid>`` placeholder, which has no id a send could
    be bound to. Blocking (``resolve_session`` may walk a store) — call it off the event loop."""
    if not isinstance(session, str) or not session:
        raise SendRefused(422, "session is required")
    try:
        prov, native = engines.parse_key(session)
    except Exception:
        raise SendRefused(422, "not a session id this app can address") from None
    key = f"{prov.engine_id}:{native}"
    row = engines.resolve_session(prov.engine_id, native)
    cwd = row.cwd if row is not None else None
    if not _scope_ok(cwd):
        raise SendRefused(403, "Outside your project folders — nothing was sent")
    return engines.physical_key(key), cwd


def snapshot(template_id: str, library_names: tuple[str, ...]) -> str:
    """Everything a rendered message was resolved AGAINST, as one comparable digest: the scope
    boundary, the secret key's id, the template's revision and the revision of every library
    variable it used (Hermes on #1105).

    Taken ONCE, before the secrets are read, and BOUND to the send. Every stage compares against
    the bound value — the guard (``precondition`` and ``final_guard``) with ``==``, and the seam's
    ``policy_fingerprint`` re-reading it inside the cross-process fence right before byte one. The
    template and variable stores commit under that same fence (``session_input.mutation_fence``),
    so an edit either lands before the re-read (and refuses) or after the byte (and is irrelevant).
    """
    roots, exclusions = _boundary()
    try:
        rev = str(tstore.get_template(template_id)["updated_at"])
    except tstore.TemplateNotFound:
        rev = "deleted"
    revs = template_vars.revisions()
    lib = [f"{n}={revs.get(n, 'missing')}" for n in library_names]
    raw = "\0".join(
        [*roots, "|", *exclusions, "|", template_secrets.current_kid(), "|", rev, "|", *lib]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


#: One template send per session at a time. Its three writes are separate seam calls, so two
#: overlapping sends would interleave as clear, clear, paste A, paste B, Enter, Enter — one merged
#: prompt (Hermes on #1105). A second send while one is in flight is refused, never queued.
_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


@contextlib.contextmanager
def _reserve(phys: str):
    with _in_flight_lock:
        if phys in _in_flight:
            raise SendRefused(409, "Another template is being sent into this session — wait for it")
        _in_flight.add(phys)
    try:
        yield
    finally:
        with _in_flight_lock:
            _in_flight.discard(phys)


def _map(outcome: session_input.Outcome, *, stage: str) -> SendRefused:
    state, detail = outcome.state, outcome.detail or ""
    if state == "not_live":
        return SendRefused(409, "This session isn't running — open it and try again")
    if state == "stale":
        if detail.startswith(_SCOPE):
            return SendRefused(409, "Your project folders changed — review and send again")
        if detail.startswith(_TEMPLATE):
            return SendRefused(409, "This template was edited — review and send again")
        return SendRefused(
            409, "Something changed in the session before sending — review and send again"
        )
    if state == "refused":
        return SendRefused(409, "The session is busy — try again in a moment")
    if state == "aborted":
        return SendRefused(
            502,
            "Sending was interrupted; part of the message may have reached the agent — "
            "check the terminal",
        )
    # failed / timeout: zero bytes of THIS write reached the session.
    if stage == "clear":
        return SendRefused(502, "Couldn't send — nothing reached the agent")
    return SendRefused(502, "Couldn't send the message — check the terminal before retrying")


def deliver(
    phys: str,
    rendered: Rendered,
    *,
    cwd: str | None,
    template_id: str,
    expected_updated_at: float,
    has_images: bool,
    bound: str | None = None,
    library_names: tuple[str, ...] = (),
) -> None:
    """Clear, paste, Enter — three fenced writes. BLOCKING; run under ``asyncio.to_thread``.

    ``bound`` is the ``snapshot`` the message was resolved against; every stage refuses if the
    current snapshot differs. Omitted (tests of the write sequence alone) it is taken here."""
    if bound is None:
        bound = snapshot(template_id, library_names)

    def current() -> str:
        return snapshot(template_id, library_names)

    def guard() -> tuple[bool, str]:
        if not _scope_ok(cwd):
            return False, f"{_SCOPE} the session is outside the project folders"
        try:
            now = tstore.get_template(template_id)
        except tstore.TemplateNotFound:
            return False, f"{_TEMPLATE} the template was deleted"
        if now["updated_at"] != expected_updated_at:
            return False, f"{_TEMPLATE} the template was edited"
        if current() != bound:
            return False, f"{_TEMPLATE} the template, its variables or the secret key changed"
        return True, ""

    def write(payload: bytes, *, quiet: bool) -> session_input.Outcome:
        return session_input.send_input(
            phys,
            payload,
            precondition=guard,
            final_guard=guard,
            policy_fingerprint=current,
            require_quiet=quiet,
        )

    # Encoded BEFORE the first write: nothing that can fail may happen after the prompt line
    # was cleared except the writes themselves.
    paste = b"\x1b[200~" + rendered.text.encode("utf-8") + b"\x1b[201~"
    o = write(LINE_CLEAR, quiet=True)
    if not o.ok:
        raise _map(o, stage="clear")
    time.sleep(CLEAR_DELAY_S)
    o = write(paste, quiet=False)
    if not o.ok:
        raise _map(o, stage="paste")
    time.sleep(ENTER_DELAY_AFTER_IMAGES_S if has_images else ENTER_DELAY_S)
    o = write(b"\r", quiet=False)
    if not o.ok:
        raise SendRefused(
            502, "The message was typed but not submitted — press Enter in the terminal"
        )


def send(template_id: str, payload: object) -> dict:
    """The whole route body, blocking. Returns ``{masked, template}``; raises ``SendRefused`` /
    ``TemplateError`` / ``TemplateNotFound``."""
    if not isinstance(payload, dict):
        raise tstore.TemplateError("expected a JSON object")
    unknown = sorted(set(payload) - {"session", "values", "expected_updated_at"})
    if unknown:
        raise tstore.TemplateError(f"unknown fields: {unknown}")
    expected = tstore.parse_fence(payload.get("expected_updated_at"))
    template = tstore.get_template(template_id)
    if template["updated_at"] != expected:
        raise SendRefused(409, "This template was edited — review and send again")
    phys, cwd = resolve_target(payload.get("session"))
    library_names = tuple(sorted(f["name"] for f in template["fields"] if f["source"] == "library"))
    # Bound BEFORE any secret is read, and verified after: the values below were resolved against
    # exactly this key, template revision and variable revisions (Hermes on #1105).
    bound = snapshot(template_id, library_names)
    rendered = render(
        template,
        payload.get("values"),
        library=template_vars.values(),
        secrets=template_vars.secret_values(),
        secret_state=template_vars.secret_state(),
    )
    if snapshot(template_id, library_names) != bound:
        raise SendRefused(409, "The template, its variables or the secret key changed — try again")
    with _reserve(phys):
        # EVERY secret this message carries — stored and typed — into the redaction set BEFORE
        # the first byte, held for the TTL whatever later happens to the store or the key.
        template_secrets.remember(list(rendered.secrets_used))
        deliver(
            phys,
            rendered,
            cwd=cwd,
            template_id=template_id,
            expected_updated_at=expected,
            has_images=bool(template["images"]),
            bound=bound,
            library_names=library_names,
        )
    # The message IS delivered. Nothing after this line may turn that into a failure: a 503 "try
    # again" here would invite a retry that submits the instruction twice (Hermes on #1105,
    # round 3). The usage counter is bookkeeping — any failure (a busy fence, I/O, a newer
    # store) keeps the delivered result and says so separately.
    try:
        used = tstore.mark_used(template_id)
        counted = True
    except Exception:  # noqa: BLE001 — best-effort after a completed delivery
        used = template
        counted = False
    return {"masked": rendered.masked, "template": used, "counted": counted}
