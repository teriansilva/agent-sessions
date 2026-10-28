"""The closed value space a connector manifest selects from (#1746 D1, #1750).

Every set here is **reviewed code**, as in ``plugins/kinds.py``: a manifest may only *choose* a
member, never add one. A new protocol is a PR that adds an adapter kind (and its adapter); a
service that speaks an existing protocol is a new manifest only. P1a adds only what Forgejo needs;
later kinds are added by their own children, each named beside the member it needed.
"""

from __future__ import annotations

#: Manifest contract versions this build reads. A higher contract is refused, never half-read. An
#: ADDITIVE field (e.g. P2's `send`) does not bump it: an older build meets it as an unknown field
#: and refuses the manifest, which is the same fail-closed outcome (the plugin manifest's rule).
CONTRACT_CURRENT = 1

#: `adapter.kind`: the reviewed code that talks to the service. `forgejo-api` (#1750) also serves
#: Gitea and Codeberg, which speak the same `/api/v1`.
ADAPTER_KINDS = frozenset({"forgejo-api"})

#: `auth.kind`. `token-header`: a personal access token, sent by `connector_http` only, as
#: ``Authorization: <scheme> <token>``, only to the origin it was saved for.
AUTH_KINDS = frozenset({"token-header"})

#: `auth.scheme` for `token-header`. Forgejo's own scheme is `token`.
AUTH_SCHEMES = frozenset({"token"})

#: `auth.scopes`: what the Commander is told to grant the token (shown, never enforced here).
AUTH_SCOPES = frozenset({"read:issue", "read:repository"})

#: `identity.category` (the gallery's sections, #1746).
CATEGORIES = frozenset({"versioning"})

#: `capabilities.read`: what HQ may read through the adapter.
READ_OPS = frozenset({"repos", "issues", "pulls", "ci", "files"})

#: `origin.mode`. `operator`: the Commander enters the origin (self-hosted Forgejo). `fixed`: the
#: manifest names it (a hosted service); P1a ships no fixed-origin manifest.
ORIGIN_MODES = frozenset({"operator", "fixed"})

#: `identity.publisher`. P1a ships first-party manifests only.
PUBLISHERS = frozenset({"battlelab"})

#: Which adapter each read op needs: a manifest may not claim a read its adapter cannot do.
ADAPTER_READS = {"forgejo-api": READ_OPS}

#: Which auth each adapter speaks.
ADAPTER_AUTH = {"forgejo-api": frozenset({"token-header"})}
