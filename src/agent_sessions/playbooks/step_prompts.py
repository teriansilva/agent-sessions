"""Retained direct step instructions, portable source snapshots and pure composition (#1391).

These functions read no store, resolve no credential and dispatch nothing. A template source
is provenance, never a live binding: its retained copy stays usable after source deletion.
Bindings map source field names to declared portable inputs, never local variable IDs/values.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid

from .. import templates
from ..plugins.manifest import ManifestError, _Reader
from ..template_send import FIELD_TOKEN_RE
from . import schema

PROMPT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
REVISION_RE = re.compile(r"^[0-9a-f]{64}$")


def new_id() -> str:
    return "prompt-" + uuid.uuid4().hex


def revision(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def template_snapshot(template: object, where: str = "source.template") -> dict:
    """Validate the portable definition only; there is no value-library read here."""
    from .validate import _text_tokens

    try:
        result = templates.validate(template, check_files=False)
    except templates.TemplateError as exc:
        raise ManifestError(where, str(exc)) from None
    if result["images"]:
        raise ManifestError(where, "image attachments are not supported by direct text prompts")
    if any(field["kind"] == "secret" for field in result["fields"]):
        raise ManifestError(
            where,
            "a secret cannot be interpolated into workflow/API instructions; "
            "use a supported credential binding or reference file",
        )
    fields = {field["name"]: field for field in result["fields"]}
    for key in ("name", "description", "body"):
        _text_tokens(result[key], f"{where}.{key}", fields)
    for i, field in enumerate(result["fields"]):
        for key in ("label", "default"):
            _text_tokens(field[key], f"{where}.fields[{i}].{key}", fields)
    return result


def _bindings(raw: object, snapshot: dict, variables: dict, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ManifestError(where, "must map each source field to a declared input name")
    names = {field["name"] for field in snapshot["fields"]}
    if set(raw) != names:
        raise ManifestError(where, "must map exactly the source template's fields")
    for name, alias in raw.items():
        if not isinstance(alias, str) or not schema.VARIABLE_NAME_RE.fullmatch(alias):
            raise ManifestError(f"{where}.{name}", "must be a portable input name")
        variable = variables.get(alias)
        if variable is None or variable["kind"] != "text":
            raise ManifestError(f"{where}.{name}", "must name a declared non-secret input")
    return dict(raw)


def read(reader: _Reader, variables: dict) -> dict:
    from .validate import _prose, _text_tokens

    prompt = {
        "id": reader.str("id", pattern=PROMPT_ID_RE),
        "title": reader.str("title", "Instructions", max_len=schema.NAME_MAX),
        "body": _prose(reader, "body", max_len=templates.BODY_MAX, required=True),
    }
    for key in ("title", "body"):
        _text_tokens(prompt[key], reader._f(key), variables)
    source = reader.table("source", required=False)
    if source is not None:
        sid = source.str("id", pattern=templates.ID_RE)
        expected = source.str("revision", pattern=REVISION_RE)
        snapshot = template_snapshot(source.raw("template"), source._f("template"))
        bindings = _bindings(source.raw("bindings"), snapshot, variables, source._f("bindings"))
        source.done()
        if revision(snapshot) != expected:
            raise ManifestError(source._f("revision"), "does not match the retained source")
        prompt["source"] = {
            "id": sid,
            "revision": expected,
            "template": snapshot,
            "bindings": bindings,
        }
    reader.done()
    return prompt


def retain_template(template: dict, bindings: dict, variables: dict) -> dict:
    """Return a new retained copy with fresh identity and explicit symbolic input mapping."""
    snapshot = template_snapshot({k: template[k] for k in templates.EDITABLE_KEYS if k in template})
    mapping = _bindings(bindings, snapshot, variables, "source.bindings")
    body = FIELD_TOKEN_RE.sub(lambda match: "{{" + mapping[match[1]] + "}}", snapshot["body"])
    # The title is display-only (compose never expands it). Source field tokens become their
    # plain names: no source-local alias dangles, and the title can only shrink, so a valid
    # template name always fits the title cap whatever input names it maps to.
    prompt = {
        "id": new_id(),
        "title": FIELD_TOKEN_RE.sub(lambda match: match[1], snapshot["name"]),
        "body": body,
        "source": {
            "id": template["id"],
            "revision": revision(snapshot),
            "template": snapshot,
            "bindings": mapping,
        },
    }
    return read(_Reader(prompt, "prompt"), variables)


def source_comparison(prompt: dict, current: dict | None, variables: dict) -> dict:
    """Read-only comparison. Adopting a candidate is a separate draft edit by the operator."""
    retained = read(_Reader(prompt, "prompt"), variables)
    source = retained.get("source")
    if source is None:
        raise ManifestError("prompt.source", "a manual prompt has no template source")
    if current is None:
        return {"state": "deleted", "retained": retained, "candidate": None}
    if current.get("id") != source["id"]:
        raise ManifestError("prompt.source.id", "the comparison names another template")
    # Added/removed fields require explicit mapping; keep the old copy and report the refusal.
    try:
        snapshot = template_snapshot(
            {k: current[k] for k in templates.EDITABLE_KEYS if k in current}
        )
        if revision(snapshot) == source["revision"]:
            return {"state": "current", "retained": retained, "candidate": None}
        candidate = retain_template(current, source["bindings"], variables)
    except ManifestError as exc:
        return {"state": "changed", "retained": retained, "candidate": None, "reason": str(exc)}
    candidate["id"] = retained["id"]
    return {"state": "changed", "retained": retained, "candidate": candidate}


def compose(step: dict, variables: dict, values: dict, *, limit: int) -> str:
    """All inputs and the entire instruction must fit before the caller receives any text.

    The effect owner supplies its admitted runtime limit and repeats current binding/profile
    authority checks. This pure helper cannot authorize an execution or partially send one.
    """
    from .validate import _text_tokens, _variable_default

    if type(limit) is not int or not 0 < limit <= schema.MAX_INITIAL_INSTRUCTION:
        raise ManifestError("instruction.limit", "must be within the supported runtime bound")
    prompts = step.get("prompts", [])
    parts = [("brief", step.get("brief", ""), set())]
    for i, prompt in enumerate(prompts):
        source = prompt.get("source")
        required = (
            {
                source["bindings"][field["name"]]
                for field in source["template"]["fields"]
                if field["required"]
            }
            if source
            else set()
        )
        parts.append((f"prompts[{i}].body", prompt["body"], required))
    result: list[str] = []
    size = 0
    for where, text, required in parts:
        names = set(_text_tokens(text, where, variables)) | required
        replacements = {}
        for name in names:
            if name not in values:
                raise ManifestError(where, f"input {name!r} has no resolved value")
            value = _variable_default(
                variables[name], values[name], where, max_len=limit, multiline=True
            )
            if (
                (variables[name]["required"] or name in required)
                and isinstance(value, str)
                and not value.strip()
            ):
                raise ManifestError(where, f"input {name!r} requires a value")
            if isinstance(value, str) and templates._SURROGATE_RE.search(value):
                raise ManifestError(where, f"input {name!r} contains invalid Unicode")
            replacements[name] = (
                ("true" if value else "false") if type(value) is bool else str(value)
            )
        expanded_size = len(text) + sum(
            len(replacements[m[1]]) - len(m[0]) for m in FIELD_TOKEN_RE.finditer(text)
        )
        if size + expanded_size + (2 if result else 0) > limit:
            raise ManifestError(where, f"composed initial instruction exceeds {limit} characters")
        rendered = FIELD_TOKEN_RE.sub(lambda match, values=replacements: values[match[1]], text)
        if not rendered.strip():
            continue
        size += len(rendered) + (2 if result else 0)
        if size > limit:
            raise ManifestError(where, f"composed initial instruction exceeds {limit} characters")
        result.append(rendered)
    if not result:
        raise ManifestError("instruction", "the initial instruction is empty")
    return "\n\n".join(result)


def duplicate(prompts: list[dict]) -> list[dict]:
    out = copy.deepcopy(prompts)
    for prompt in out:
        prompt["id"] = new_id()
    return out
