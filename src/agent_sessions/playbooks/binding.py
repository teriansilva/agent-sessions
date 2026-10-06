"""Resolve unsaved playbook inputs using the existing scoped-variable store (#1191).

This is a preview, not another store: no value is written or encrypted here. Secret inputs only
enter the private fingerprint material; public rows never contain a secret or its envelope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import template_vars
from . import schema, validate
from .store import StoreError


@dataclass
class Resolved:
    public: list[dict]
    text: dict[str, str]
    typed: dict[str, object]
    # Private material is fed to a keyed digest, never returned or logged.
    fingerprint: dict = field(repr=False)


def _text(value: object) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def _typed(var: dict, value: str) -> object:
    raw: object = value
    if var["type"] == "int":
        if not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,15})", value, flags=re.ASCII):
            raise StoreError(f"{var['name']}: needs an integer")
        raw = int(value)
    elif var["type"] == "bool":
        if value not in ("true", "false"):
            raise StoreError(f"{var['name']}: needs true or false")
        raw = value == "true"
    return validate._variable_default(
        var,
        raw,
        f"variable {var['name']}",
        max_len=template_vars.VALUE_MAX,
        multiline=var["type"] == "text",
    )


def resolve(bundle: dict, project_id: str | None, raw: object) -> Resolved:
    """Project → global → default, over one strict store snapshot plus unsaved overrides.

    Secrets have no implicit global fallback. A supplied global reference is an explicit choice;
    unusable existing bindings refuse, including optional bindings. A pre-project review reads
    globals only and is still bound to its explicit destination by the review service.
    """
    pid = template_vars.project_id(project_id) if project_id is not None else "p-preview"
    if not isinstance(raw, list) or len(raw) > schema.MAX_VARIABLES:
        raise StoreError("bindings must be a bounded list")
    variables = {v["name"]: v for v in bundle["variables"]}
    wanted: dict[str, dict] = {}
    for item in raw:
        b = template_vars._binding(item, pid)
        name = b["name"]
        if name not in variables or name in wanted:
            raise StoreError("bindings must name each declared variable at most once")
        if variables[name]["kind"] != b["kind"]:
            raise StoreError(f"{name}: the binding kind differs from the declaration")
        wanted[name] = b
    records = template_vars._read_strictly()
    # A preview without an entity must never accidentally consume an existing project binding.
    relevant = [r for r in records if r["project_id"] in (None, project_id)]
    dependencies = [r for r in relevant if r["name"] in variables]
    project_bindings = {r["name"]: r for r in relevant if r["project_id"] == pid}
    overlay = [r for r in relevant if not (r["project_id"] == pid and r["name"] in wanted)]
    for b in wanted.values():
        if b["kind"] == "secret" and "value" in b:
            continue
        overlay.append(
            {**b, "scope": "project", "project_id": pid, "created_at": 0, "updated_at": 0}
        )
    resolver = template_vars.Resolver(pid, overlay)
    public: list[dict] = []
    text: dict[str, str] = {}
    typed: dict[str, object] = {}
    for name, var in variables.items():
        input_binding = wanted.get(name)
        row = {"name": name, "kind": var["kind"], "type": var["type"]}
        try:
            if var["kind"] == "secret":
                if input_binding is not None and "value" in input_binding:
                    state = {"state": "ok", "source": "input", "revision": None}
                else:
                    state = resolver.secret_state(name)
                if state["state"] != "ok":
                    raise template_vars.BindingUnusable(name, "needs re-entry")
                row.update(state, set=True)
            else:
                default = None if var["default"] is None else _text(var["default"])
                state = resolver.text(name, default)
                value = _typed(var, state["value"])
                if var["required"] and value == "":
                    raise StoreError(f"{name}: a value is required")
                typed[name], text[name] = value, _text(value)
                row.update(state, value=value, state="ok")
                if input_binding is not None and "value" in input_binding:
                    row.update(source="input", revision=None)
            # An explicit global choice and an implicit fallback are distinct reviewed inputs.
            selected_binding = input_binding or project_bindings.get(name)
            row["explicit_global"] = bool(
                selected_binding and selected_binding.get("ref") == template_vars.REF_GLOBAL
            )
        except template_vars.BindingMissing:
            if var["required"]:
                raise StoreError(f"{name}: a value is required", status=409) from None
            row.update(state="missing", source=None, revision=None)
        public.append(row)
    return Resolved(public, text, typed, {"bindings": wanted, "dependencies": dependencies})


def targets(bundle: dict, resolved: Resolved) -> list[dict]:
    """Concrete, validated targets plus explicitly unresolved observation slots. No probes run.

    Target IDs use declaration identity, not an endpoint hash: two items aimed at the same URL
    still need separate confirmation. Expectations never taint target provenance.
    """
    from .. import missions

    sources = {v["name"]: v.get("source") for v in resolved.public}
    states = {v["name"]: v["state"] for v in resolved.public}
    out: list[dict] = []
    for flow_id, flow in bundle["flows"].items():
        for step in flow["steps"]:
            for item in step["checklist"]:
                args: dict = {}
                pending: dict = {}
                defaults: list[str] = []
                for key, symbolic in item["probe_args"].items():
                    ref = validate._whole_var_ref(symbolic)
                    if ref is not None:
                        if ref not in resolved.typed:
                            raise StoreError(f"{ref}: a probe argument needs a value", status=409)
                        args[key] = resolved.typed[ref]
                        if key not in schema.LITERAL_ARGS and sources[ref] == "default":
                            defaults.append(ref)
                    elif isinstance(symbolic, str) and schema.STEP_TOKEN_RE.fullmatch(symbolic):
                        pending[key] = symbolic
                    else:
                        args[key] = symbolic
                # Check every concrete argument even when another argument awaits an observation.
                spec = missions.PROBE_ARG_SCHEMA.get(item["probe"], {})
                for key, value in args.items():
                    spec[key][1](item["probe"], key, value)
                if not pending:
                    missions.validate_probe_args(item["probe"], args)
                out.append(
                    {
                        "id": f"flow:{flow_id}:{step['id']}:{item['key']}",
                        "kind": "probe",
                        "probe": item["probe"],
                        "args": args,
                        "pending": pending,
                        "default_variables": sorted(set(defaults)),
                        "requires_confirmation": bool(defaults),
                        "armed": False,
                    }
                )
    for connection in bundle["connections"]:
        args = {}
        defaults = []
        for key, name in connection["params"].items():
            if name not in resolved.typed:
                raise StoreError(f"{name}: a connection needs a value", status=409)
            args[key] = resolved.typed[name]
            if sources[name] == "default":
                defaults.append(name)
        out.append(
            {
                "id": f"connection:{connection['name']}",
                "kind": "connection",
                "connection_kind": connection["kind"],
                "verify": connection["verify"],
                "args": args,
                "credential": connection["credential"],
                "pending": (
                    {"credential": connection["credential"]}
                    if connection["credential"] is not None
                    and states[connection["credential"]] != "ok"
                    else {}
                ),
                "default_variables": sorted(set(defaults)),
                "requires_confirmation": bool(defaults),
                "armed": False,
            }
        )
    return out
