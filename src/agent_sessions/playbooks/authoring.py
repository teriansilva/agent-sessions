"""Read-only editor vocabulary from the format and deployment authorities (#1359).

No probes, launches, model requests, or changes to deployment authority happen here.
"""

from .. import engines, missions, templates
from ..plugins.manifest import ManifestError, _Reader
from . import review, schema, step_prompts, store, validate


def template_prompt(body: dict, *, compare: bool = False) -> dict:
    """Return a draft prompt/comparison, without saving a bundle or reading variable values."""
    try:
        reader = _Reader(body, "prompt")
        definitions = validate._variables(_Reader({"variables": reader.raw("variables")}, ""))
        if compare:
            prior = step_prompts.read(_Reader(reader.raw("prompt"), "prompt"), definitions)
            source = prior.get("source")
            if source is None:
                raise ManifestError("prompt.source", "a manual prompt has no template source")
            tid = source["id"]
        else:
            tid = reader.str("template_id", pattern=templates.ID_RE)
            expected = templates.parse_fence(reader.raw("expected_updated_at"))
            bindings = reader.raw("bindings")
        reader.done()
        records, damaged = templates._read(templates.store_path())
        if damaged:
            raise store.StoreError("the template library could not be read in full", status=503)
        current = next((record for record in records if record["id"] == tid), None)
        if current is None and not compare:
            raise store.StoreError("the source template was deleted", status=404)
        if compare:
            return step_prompts.source_comparison(prior, current, definitions)
        assert current is not None
        if current["updated_at"] != expected:
            raise store.StoreError("the source template changed; compare it again", status=409)
        return {"prompt": step_prompts.retain_template(current, bindings, definitions)}
    except (ManifestError, templates.TemplateError) as exc:
        raise store.StoreError(str(exc)) from None
    except templates.TemplateStoreUnsupported as exc:
        raise store.StoreError(str(exc), status=503) from None


def compare_template_prompt(body: dict) -> dict:
    return template_prompt(body, compare=True)


def _skill_label(files: dict, sid: str) -> str:
    """`name:` from SKILL.md's frontmatter when present, else the id. Display text only."""
    text = files.get("SKILL.md") if isinstance(files.get("SKILL.md"), str) else ""
    if text.startswith("---"):
        for line in text.splitlines()[1:40]:
            if line.strip() == "---":
                break
            if line.startswith("name:"):
                name = line[5:].strip().strip("\"'")
                if name:
                    return name[: schema.LABEL_MAX]
    return sid


def _library_skills() -> tuple[list[dict], str | None]:
    """The local skills library by exact id (D14): what a role may require. Never a path.

    Fail-soft: a damaged, unsafe or busy library must not take the authoring schema (and with it
    the editor and the New Project wizard) down; the reason is returned instead."""
    from ..gallery import skills as skill_library
    from ..plugins import provenance

    try:
        listing = skill_library.listing()
    except (OSError, ValueError, store.StoreError, provenance.ProvenanceError):
        # No exception text: this vocabulary also reaches HQ's `playbook_schema` tool.
        return [], "The skills library cannot be read right now; open Library → Skills to check it."
    out = []
    for row in listing.get("skills", []):
        sid = row.get("id")
        if isinstance(sid, str) and schema.SKILL_REF_RE.fullmatch(sid) and not row.get("error"):
            out.append({"id": sid, "label": _skill_label(row.get("files") or {}, sid)})
    return out, None


def vocabulary() -> dict:
    roster = engines.registry.current()
    skills, skills_note = _library_skills()
    return {
        "agents": sorted(eid for eid, p in roster.by_id.items() if review.assignment_available(p)),
        "actors": sorted(schema.ACTOR_KINDS),
        "memory": sorted(schema.MEMORY_MODES),
        "distinct": sorted(schema.DISTINCT_CONSTRAINTS),
        # Format 5 (#1654): what a role may require, and the instance-floor constraint set.
        "distinct_role": sorted(schema.DISTINCT_CONSTRAINTS_ROLE),
        "grades": list(schema.GRADES),
        "skills": skills,
        **({"skills_unavailable": skills_note} if skills_note else {}),
        "variable_types": sorted(schema.VARIABLE_TYPES),
        "probes": {
            kind: {
                "outputs": sorted(schema.OUTPUT_SLOTS.get(kind, ())),
                "args": {
                    name: {
                        "required": required,
                        "type": schema.ARG_TYPE_BY_CONTRACT[contract],
                        "literal": name in schema.LITERAL_ARGS,
                        "variable_types": sorted(
                            schema.VAR_TYPES_FOR_ARG[schema.ARG_TYPE_BY_CONTRACT[contract]]
                        ),
                        "slots": sorted(
                            slot
                            for slot, typ in schema.SLOT_TYPES.items()
                            if typ in schema.ARG_SLOT_TYPES.get(name, ())
                        ),
                    }
                    for name, (required, contract) in args.items()
                },
            }
            for kind, args in missions.PROBE_ARG_SCHEMA.items()
        },
        "limits": {
            "flows": schema.MAX_FLOWS,
            "steps": schema.MAX_STEPS,
            "items": schema.MAX_ITEMS_PER_STEP,
            "prompts": schema.MAX_PROMPTS_PER_STEP,
            "initial_instruction": schema.MAX_INITIAL_INSTRUCTION,
            "variables": schema.MAX_VARIABLES,
            "distinct": schema.MAX_DISTINCT_FROM,
            "rework_min": schema.REWORK_ROUNDS_MIN,
            "rework_max": schema.REWORK_ROUNDS_MAX,
        },
    }
