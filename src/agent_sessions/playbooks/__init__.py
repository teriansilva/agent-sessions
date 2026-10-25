"""Playbook bundles (#1096): the deployable "how this repository works" object.

Naming (#1096, operator decision 2026-09-23): *playbook* is the BUNDLE this package reads. The
older `missions.playbook_id` column and the `prefs.json` `mission_playbooks` block named
*checklists* (#1091); checklists were removed (#1653), and both are left in place, unread.

Phase 1 (#1190) is the format: `schema` (the closed value space), `tree` (the node-policy walk),
`validate` (manifest, flows, runbooks, templates, materials) and `loader` (fail-soft per bundle).
#1191 adds `store` (every source's gallery + versioned authoring of `local` playbooks,
`docs/invariants/playbook-authoring.md`), `tomlw` (the TOML writer it rewrites documents with) and
`deployments` (the "which projects run it" interface the lifecycle fills). No apply, no network,
no git yet.
"""

from __future__ import annotations

from .errors import PlaybookFormatError
from .loader import BUNDLED_ROOT, card, list_bundles, load_bundle, requires_status

__all__ = [
    "BUNDLED_ROOT",
    "PlaybookFormatError",
    "card",
    "list_bundles",
    "load_bundle",
    "requires_status",
]
