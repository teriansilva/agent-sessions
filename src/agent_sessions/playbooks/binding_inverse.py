"""Plan the exact inverse of deployment-owned bindings in the existing scoped store.

No store mutation occurs here. The remove coordinator must call this under the shared variable
mutation fence, durably journal before/after, settle materials first and retire displaced secret
envelopes through template_vars before publishing the returned records. Another project's or
the global record is never an operand of this inverse.
"""

from __future__ import annotations

import copy

from .. import template_vars
from . import store


def prepare(record: dict, records: list[dict]) -> tuple[list[dict], dict, dict]:
    """Return new records and exact before/after entries, refusing intervening operator edits."""
    try:
        pid = template_vars.project_id(record["project_id"])
        owned, prior = record["owned_bindings"], record["prior_bindings"]
        if (
            not isinstance(owned, dict)
            or not isinstance(prior, dict)
            or set(owned) != set(prior)
            or len(owned) > template_vars.PROJECT_VARIABLES_MAX
        ):
            raise ValueError
        for snapshot in (owned, prior):
            for name, item in snapshot.items():
                if item is None and snapshot is prior:
                    continue
                value = template_vars._coerce_record(item, template_vars.STORE_VERSION)
                if (
                    value != item
                    or value["scope"] != "project"
                    or value["project_id"] != pid
                    or value["name"] != name
                ):
                    raise ValueError
    except (KeyError, TypeError, ValueError, template_vars.VariableError):
        raise store.Conflict("the deployment's binding ownership is damaged") from None
    current = {r["name"]: r for r in records if r["project_id"] == pid}
    if any(current.get(name) != expected for name, expected in owned.items()):
        raise store.Conflict("a deployment binding has operator edits; no binding was removed")
    result = [
        copy.deepcopy(r) for r in records if not (r["project_id"] == pid and r["name"] in owned)
    ]
    for name, item in prior.items():
        if item is None:
            continue
        if item.get("ref") == "global":
            target = next(
                (r for r in records if r["project_id"] is None and r["name"] == name), None
            )
            if target is None or target["kind"] != item["kind"]:
                raise store.Conflict(
                    f"{name}: the original global reference can no longer be restored"
                )
        result.append(copy.deepcopy(item))
    result.sort(key=template_vars._key)
    return result, copy.deepcopy(owned), copy.deepcopy(prior)
