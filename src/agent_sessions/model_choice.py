"""Which model a launch uses (#1189) — the ONE resolver for every launch path.

The interactive new-session socket, a resume, and an unattended mission dispatch all come through
`select`, and the argv pair is only ever produced from the `Selection` it returns. That is the
whole argv-safety argument, in order:

1. **Shape.** A requested value is a `str` of `_MODEL_ID_RE` — ASCII, no space, no `=`, and a first
   character that cannot start an option — or it is refused. Anything else (a list, a number, a
   leading `-`, a NUL, a unicode lookalike) never gets as far as a membership test.
2. **Membership + canonicalisation.** The value must name a model the engine's manifest lists (by
   id or alias — an alias is replaced by its id) or an id the operator added under
   `agent_defaults.models`. A value in neither is refused, never replaced by `default`: a stale
   choice (an operator id since removed, a model from another engine) is a 422, not a silent
   downgrade.
3. **The flag is the manifest's**, from `kinds.MODEL_FLAGS`, and the pair is two argv elements.
   `PluginProvider._assemble` accepts only a `Selection` (never a string), re-checks its shape and
   that its flag is still the manifest's — so what was validated is exactly what is appended.

`default` means "no flag": today's behaviour. It is never evidence of which model ran
(`transcript.effective_model` is the only evidence). A new launch on `default` records nothing; a
RESUME that explicitly asks for `default` on an engine that honours the flag on resume REPLACES the
session's record with the `default` marker (read back as "nothing recorded"), so the next ordinary
resume does not quietly re-apply the model the operator just stepped away from.

**Records follow the launch.** A record is written only once the launch it describes actually
produced a master (the ws route waits for the dtach master; headless dispatch records after its
socket exists). A refused argv, a failed spawn, or an attach leaves any prior record untouched.

**Resume.** A new launch applies the flag. A resume applies it only when the manifest says the
engine honours it (`launch.model.on_resume`); otherwise a resume asking for a model is accepted only
when it is the model the session recorded — and a session with nothing recorded matches nothing —
and refused before spawn with every other value. Attaching to a running master never reaches here:
attach-never-relaunch means a live master's model is never changed.

**Engines whose model is configured elsewhere** (their own config, or a chat agent's endpoint)
offer `default` only; a non-default request to one is refused.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import metadata, prefs
from .plugins import kinds
from .plugins.manifest import _MODEL_ID_RE, MODEL_DEFAULT

DEFAULT = MODEL_DEFAULT
log = logging.getLogger(__name__)


class ModelRefused(ValueError):
    """A model request refused before anything is spawned. `code` is stable for callers/tests."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Selection:
    """A resolved choice. Only `select` builds one a launch will accept."""

    engine: str
    #: The canonical model id, or None for `default`.
    model: str | None
    #: The flag to put in argv, or None (default, or a resume that keeps the recorded model).
    flag: str | None
    #: An explicit `default` on a resume whose engine honours the flag there: a successful launch
    #: replaces the session's record with the `default` marker (see `record`).
    replaces_record: bool = False

    def argv(self) -> list[str]:
        """The two argv elements, re-checked at the point of use. Empty when there is no flag."""
        if self.flag is None:
            return []
        if (
            self.flag not in kinds.MODEL_FLAGS
            or not isinstance(self.model, str)
            or not _MODEL_ID_RE.fullmatch(self.model)
            or self.model.startswith("-")
        ):
            raise ModelRefused("malformed", "refusing a malformed model selection")
        return [self.flag, self.model]


def takes_model(manifest) -> bool:
    """Can a launch of this engine name a model at all?"""
    return (
        manifest is not None
        and manifest.launch is not None
        and manifest.launch.model is not None
        and not manifest.models_configured_elsewhere
    )


def operator_ids(engine_id: str) -> list[str]:
    return list(prefs.get_agent_defaults()["models"].get(engine_id, []))


def offered(manifest, added: list[str] | None = None) -> list[dict]:
    """What a picker offers for this engine, `default` excluded (the client puts it first).

    The manifest's models, then the operator's added ids — or nothing when the engine takes no
    model at launch. An added id that collides with a manifest name is dropped here as well as
    refused on write, so a hand-edited prefs file cannot make a name ambiguous.
    """
    if not takes_model(manifest):
        return []
    out = [
        {
            "id": m.id,
            "aliases": list(m.aliases),
            "context_window": m.context_window,
            "source": "manifest",
        }
        for m in manifest.models
    ]
    taken = {n for m in manifest.models for n in (m.id, *m.aliases)}
    for mid in added if added is not None else operator_ids(manifest.id):
        if mid not in taken:
            taken.add(mid)
            out.append({"id": mid, "aliases": [], "context_window": None, "source": "operator"})
    return out


def _canonical(manifest, requested: str, added: list[str]) -> str | None:
    for m in manifest.models:
        if requested == m.id or requested in m.aliases:
            return m.id
    taken = {n for m in manifest.models for n in (m.id, *m.aliases)}
    if requested in added and requested not in taken:
        return requested
    return None


def select(
    prov,
    requested: object,
    *,
    resume: bool = False,
    recorded: str | None = None,
    added: list[str] | None = None,
) -> Selection:
    """Resolve a requested model for a launch of `prov`. Raises `ModelRefused` (nothing spawned).

    `recorded` is the session's `model_requested` ("" / None = nothing recorded, which matches
    nothing). `added` defaults to the operator's stored ids, read here — at the launch, not earlier.
    """
    engine = prov.engine_id
    if requested is None or requested == DEFAULT:
        return Selection(engine, None, None)
    if not isinstance(requested, str):
        raise ModelRefused("invalid", "model must be a string")
    if not _MODEL_ID_RE.fullmatch(requested):
        raise ModelRefused("invalid", "model is not a valid model id")
    manifest = prov.manifest
    if not takes_model(manifest):
        if manifest is not None and manifest.models_configured_elsewhere:
            raise ModelRefused(
                "configured_elsewhere",
                f"{engine}: the model is chosen in the agent's own configuration; only "
                f"{DEFAULT!r} can be requested here",
            )
        # No model at all — a terminal with no agent (shell), or an agent with no model flag.
        raise ModelRefused("unsupported", f"{engine} takes no model; only {DEFAULT!r} applies")
    ops = added if added is not None else operator_ids(engine)
    canonical = _canonical(manifest, requested, ops)
    if canonical is None:
        # "not offered", not "no longer": this path cannot tell a removed id from one never listed.
        raise ModelRefused("not_offered", f"{engine}: model {requested!r} is not offered")
    flag = manifest.launch.model.flag
    if not resume or manifest.launch.model.on_resume:
        return Selection(engine, canonical, flag)
    if recorded and recorded == canonical:
        # The engine resumes on the model it started with; nothing to change, so no flag.
        return Selection(engine, canonical, None)
    raise ModelRefused(
        "resume_override",
        f"{engine} cannot change the model of a session it resumes — start a new session to use "
        f"{canonical!r}",
    )


def select_resume(prov, requested: object, key: str) -> Selection:
    """`select` for a RESUME of session `key`.

    **No model asked for** (`None` — the client sends none on a resume): when the session recorded
    one AND the engine honours the flag on resume, the RECORDED model is re-applied through the
    same `select` (re-validated against today's lists), so a session started on a model resumes on
    it rather than on the engine's default while its row still shows the request. A recorded model
    that is no longer offered is refused, never silently defaulted. An engine that cannot take the
    flag on resume resumes as it always has (the row's tag is the request, not a claim).

    An explicit `default` is a deliberate "no flag" and reads nothing. Where the engine honours the
    flag on resume it also REPLACES the record once the launch succeeds (`replaces_record`), so the
    following model-less resume sends no flag either. Where it does not, the engine keeps the model
    it started with, so the record — which names that model — stays.
    """
    m = prov.manifest
    if requested == DEFAULT:
        return Selection(
            prov.engine_id,
            None,
            None,
            replaces_record=takes_model(m) and bool(m.launch.model.on_resume),
        )
    if requested is None:
        if not (takes_model(m) and m.launch.model.on_resume):
            return Selection(prov.engine_id, None, None)
        recorded = recorded_for(key)
        if not recorded:
            return Selection(prov.engine_id, None, None)
        try:
            return select(prov, recorded, resume=True, recorded=recorded)
        except ModelRefused as e:
            # Fixed text short and the id LAST: the ws close reason is capped at 120 bytes, so a
            # long id is what gets cut, never the instruction.
            raise ModelRefused(
                "recorded_not_offered",
                f"model no longer offered; re-add it in Settings → Agents to resume: {recorded}",
            ) from e
    return select(prov, requested, resume=True, recorded=recorded_for(key))


def still_offered(manifest, model_id: str) -> bool:
    """Is `model_id` still a CANONICAL id this engine offers right now (the manifest's list or the
    operator's added ids)? The point-of-use check: `Selection` is a plain dataclass, so the argv
    assembler asks this again rather than trusting whoever built the object."""
    if not takes_model(manifest) or not isinstance(model_id, str):
        return False
    if any(model_id == m.id for m in manifest.models):
        return True
    # An operator id, and only one no manifest name (id OR alias) shadows: an alias is never
    # canonical, so it is never accepted here even if it also appears in the operator's list.
    taken = {n for m in manifest.models for n in (m.id, *m.aliases)}
    return model_id not in taken and model_id in operator_ids(manifest.id)


def recorded_for(key: str) -> str:
    """The session's recorded requested model ("" when none), following placeholder aliases."""
    try:
        return metadata.requested_model(key)
    except Exception:  # noqa: BLE001 — an unreadable sidecar records nothing, and nothing matches
        return ""


def record(key: str, sel: Selection) -> None:
    """Persist a SUCCESSFUL launch's request under `key` (the launch key: a placeholder for a
    late-id engine, carried through adoption by `metadata.requested_model`). Call it only once the
    launch produced a master. `default` writes nothing — except an explicit `default` resume
    (`replaces_record`), which writes the `default` marker over the previous model. The marker (not
    a blank) so it also shadows a placeholder's record; `metadata.requested_model` reads it as "".

    The write goes to `metadata.resolve_key(key)`, never blindly to `key`: for an adopted late-id
    session whose title/sticky/archive live only on the placeholder entry, a write to the logical
    key would create a sparse logical entry that the rows read first, hiding all of that. The
    resolved key is one `metadata.requested_model` reads (logical entry first, then the physical
    one), so the record — or the `default` marker — is still what the next read finds.
    Best-effort: a sidecar write never fails a launch that already happened."""
    if sel.model is None and not sel.replaces_record:
        return
    try:
        metadata.patch(metadata.resolve_key(key), model_requested=sel.model or DEFAULT)
    except Exception:  # noqa: BLE001
        log.warning("could not record the requested model for %s", key, exc_info=True)
