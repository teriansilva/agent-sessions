"""Historical playbook bindings also hold global variable references (#1390).

Call only while holding session_input.mutation_fence(), before any reference-changing
publication. Read deployment records through their existing descriptor/shape checks,
without acquiring authoring/project locks below the authority fence. Every new binding
intent is written inside that same fence; later lifecycle writes preserve or remove its
references. Concurrent record replacement refuses rather than returning a partial list.
"""

from __future__ import annotations

import os


def deployment_projects(name: str) -> list[str]:
    from . import template_vars
    from .playbooks import deployment_state as state
    from .playbooks import store

    result: set[str] = set()
    root = None
    try:
        root = store._open_root(create=False)
        if root is None:
            return []
        directory = state._directory(root, state.DIRECTORY, False)
        if directory is None:
            return []
        try:
            for entry in sorted(os.listdir(directory)):
                pid = template_vars.project_id(entry)
                project = state._directory(directory, pid, False)
                if project is None:
                    raise store.Conflict("a deployment disappeared during inventory")
                try:
                    held = state.Locked(root, directory, project, pid)
                    record = held.read()
                finally:
                    os.close(project)
                if record is None or not state.holds(record):
                    continue
                prior, owned = record.get("prior_bindings"), record.get("owned_bindings")
                if (
                    not isinstance(prior, dict)
                    or not isinstance(owned, dict)
                    or set(prior) != set(owned)
                ):
                    raise ValueError("incomplete binding ownership inventory")
                snapshots = [(prior, True), (owned, False)]
                journal = record.get("binding_operation")
                if journal is not None:
                    if not isinstance(journal, dict):
                        raise ValueError("invalid binding journal")
                    before, after = journal.get("before"), journal.get("after")
                    if (
                        not isinstance(before, dict)
                        or not isinstance(after, dict)
                        or set(before) != set(after)
                    ):
                        raise ValueError("incomplete binding intent inventory")
                    snapshots.extend([(before, True), (after, False)])
                for snapshot, allow_absent in snapshots:
                    if (
                        not isinstance(snapshot, dict)
                        or len(snapshot) > template_vars.PROJECT_VARIABLES_MAX
                    ):
                        raise ValueError("incomplete binding inventory")
                    for alias, item in snapshot.items():
                        template_vars._name(alias)
                        if item is None and allow_absent:
                            continue  # an explicitly absent prior binding
                        parsed = template_vars._coerce_record(
                            item, template_vars.BINDING_SNAPSHOT_VERSION
                        )
                        if (
                            parsed != item
                            or parsed["project_id"] != pid
                            or parsed["scope"] != "project"
                            or parsed["name"] != alias
                        ):
                            raise ValueError("invalid binding identity")
                        if alias == name and parsed.get("ref") == template_vars.REF_GLOBAL:
                            result.add(pid)
            state._verify(root, state.DIRECTORY, directory)
        finally:
            os.close(directory)
    except (OSError, ValueError, TypeError, KeyError, store.StoreError):
        raise template_vars.VariableRefsUnknown(
            "the playbook binding inventory could not be read in full"
        ) from None
    finally:
        if root is not None:
            os.close(root)
    return sorted(result)
