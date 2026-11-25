"""Which projects run a playbook — the interface the authoring store asks before a delete (#1191).

Deleting a `local` playbook that projects still run is refused, listing those projects (#1192's
agreed rule). The answer comes from the lifecycle's durable binding/apply records. Even a pending
binding holds the source until removed, so authoring cannot invalidate an accepted operation.
The installed registry reads those records lazily without acquiring the root lock a second time;
`set_registry` remains the test seam.

**The contract a registry must keep (PR 2).** `projects_running` is called INSIDE the authoring
store's exclusive root lock (`store.root_lock`), so a delete and the question are one step. A
deployment that is being recorded must therefore hold the same root lock (shared is enough) while
it re-checks that the playbook still exists at the revision it reviewed, and while it commits the
record: then either the delete sees the deployment and refuses, or the deployment sees the
playbook gone and refuses. Neither can pass the other in between.

**Fail closed.** A registry that raises, or answers with something that is not a list of project
records, makes the delete refuse (`DeploymentsUnavailable`): "I could not tell which projects run
it" is never "no project runs it".
"""

from __future__ import annotations

from typing import Protocol


class DeploymentsUnavailable(RuntimeError):
    """The registry could not answer, so a destructive caller must refuse."""


class Registry(Protocol):
    def projects_running(self, playbook_id: str) -> list[dict]:
        """`[{project_id, name?}]` for every project with a deployment of `playbook_id`."""
        ...


class _StoredDeployments:
    """Lazy import keeps this seam independent of authoring-store initialization."""

    def projects_running(self, playbook_id: str) -> list[dict]:
        from .deployment_state import Registry

        return Registry().projects_running(playbook_id)


_registry: Registry = _StoredDeployments()


def set_registry(registry: Registry) -> Registry:
    """Install `registry`; returns the previous one (so a test can restore it)."""
    global _registry
    previous = _registry
    _registry = registry
    return previous


def projects_running(playbook_id: str) -> list[dict]:
    """`[{project_id, name}]` sorted by project id, or `DeploymentsUnavailable`."""
    try:
        raw = _registry.projects_running(playbook_id)
    except Exception as e:  # noqa: BLE001 — any failure to answer is "unknown", never "none"
        raise DeploymentsUnavailable(
            f"could not tell which projects run this playbook ({type(e).__name__})"
        ) from None
    if not isinstance(raw, list):
        raise DeploymentsUnavailable("could not tell which projects run this playbook")
    out: dict[str, dict] = {}
    for item in raw:
        pid = item.get("project_id") if isinstance(item, dict) else None
        if not isinstance(pid, str) or not pid:
            raise DeploymentsUnavailable("could not tell which projects run this playbook")
        name = item.get("name")
        out[pid] = {"project_id": pid, "name": name if isinstance(name, str) else pid}
    return [out[k] for k in sorted(out)]
