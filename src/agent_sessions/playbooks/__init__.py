"""Playbook bundles (#1096): the deployable "how this repository works" object.

Naming (#1096, operator decision 2026-09-23): *playbook* is the BUNDLE this package reads. The
older internal identifiers `playbook_id`, `prefs.mission_playbooks`, `prefs.PlaybookError` and
`/settings/ai-playbooks` keep meaning *checklist* (#1091); they are not renamed (see
`docs/invariants/playbook-format.md`).

Phase 1 (#1190) is the format only: `schema` (the closed value space), `tree` (the node-policy
walk), `validate` (manifest, flows, runbooks, templates, materials) and `loader` (bundled source,
fail-soft per bundle). No UI, no apply, no network, no git.
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
