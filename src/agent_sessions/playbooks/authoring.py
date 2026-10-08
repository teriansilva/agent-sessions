"""Read-only editor vocabulary from the format and deployment authorities (#1359).

No probes, launches, model requests, or changes to deployment authority happen here.
"""

from .. import engines, missions
from . import review, schema


def vocabulary() -> dict:
    roster = engines.registry.current()
    return {
        "agents": sorted(eid for eid, p in roster.by_id.items() if review.assignment_available(p)),
        "actors": sorted(schema.ACTOR_KINDS),
        "memory": sorted(schema.MEMORY_MODES),
        "distinct": sorted(schema.DISTINCT_CONSTRAINTS),
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
            "variables": schema.MAX_VARIABLES,
            "distinct": schema.MAX_DISTINCT_FROM,
            "rework_min": schema.REWORK_ROUNDS_MIN,
            "rework_max": schema.REWORK_ROUNDS_MAX,
        },
    }
