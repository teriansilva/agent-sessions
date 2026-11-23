"""The ONE environment an agent process (or a probe of one) is started with (#1078).

Every agent BattleLab launched used to inherit the app's whole environment, so an agent could read
``AGENT_SESSIONS_SECRET_KEY`` (which signs the session cookie and the CSRF token) with ``env`` and
mint an admin session. ``AGENT_SESSIONS_PASSWORD_HASH`` and ``AGENT_SESSIONS_VAPID_KEYS`` came
along the same way. The environment is now built through :func:`scrubbed` at every spawn seam:
the interactive terminal (`webterm`), the unattended dispatch (`headless_dispatch`), the server's
own `dtach -a` reader (`session_stream`) and the vendor probes (`engine_auth`, `agent_usage`).

**Defence-in-depth, not a sandbox.** Agents run as the same UID as the app, so the install env
file, ``2fa.json`` and ``/proc/<app-pid>/environ`` stay readable to them; that exposure is tracked
separately and is not closed here. This stops the casual/``env``-dump leak, and existing ``dtach``
masters keep their old environment until they restart.

**The rule: a denylist of the known secret names plus a pattern, scoped to the app's prefix.**
An allowlist of every ``AGENT_SESSIONS_*`` name would be brittle (a new engine variable would need
a code change before it reached its agent), while an allowlist of names that merely *look*
non-secret would fail open on a new secret. The pattern is the fail-closed half: any
``AGENT_SESSIONS_*`` name containing ``SECRET``, ``PASSWORD``, ``KEY`` or ``TOKEN`` is dropped.
Nothing outside the ``AGENT_SESSIONS_`` prefix is touched — a vendor credential (``*_API_KEY``,
``*_TOKEN``) is the agent's own and must reach it.

Phase-1 inventory of the names the pattern catches (``grep -rhoE 'AGENT_SESSIONS_[A-Z0-9_]+'``):

===============================  ====================================================
name                             what it is
===============================  ====================================================
``_SECRET_KEY``                  cookie / CSRF signing secret — the finding
``_PASSWORD_HASH``               the admin PBKDF2 hash — credential material
``_VAPID_KEYS``                  the push private-key file — names the key path
``_TEMPLATE_SECRETS_KEY``        the secret-variable encryption key file
``_RELEASE_KEY``                 the installer's release-signing key (scripts)
``_ACTION_SECRETS``              the webhook-signing secret store path
``_PLAYBOOK_SECRETS``            the playbook secret-reference root
``_SECRETS_RUNTIME``             the secrets broker's runtime root
``_FORCE_PASSWORD_CHANGE``       a first-run boolean (dropped by ``PASSWORD``; harmless)
===============================  ====================================================

No non-``AGENT_SESSIONS_*`` credential is written to the install env file: the installer seeds
``MALLOC_ARENA_MAX`` and ``HOMEFREE_RELAY_URL`` (an address, not a secret). Vendor credentials
and provider keys are the engine's own and are deliberately left alone.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: The names that are, or name the location of, one of the app's own secrets. Kept explicit so
#: the list is auditable even where the pattern would also catch the name.
SECRET_NAMES = frozenset(
    {
        "AGENT_SESSIONS_SECRET_KEY",
        "AGENT_SESSIONS_PASSWORD_HASH",
        "AGENT_SESSIONS_VAPID_KEYS",
        "AGENT_SESSIONS_TEMPLATE_SECRETS_KEY",
        "AGENT_SESSIONS_RELEASE_KEY",
    }
)

#: The fail-closed half: a newly added ``AGENT_SESSIONS_*`` secret is dropped by its marker.
_MARKERS = ("SECRET", "PASSWORD", "KEY", "TOKEN")


def is_secret(name: str) -> bool:
    """Is ``name`` one of the app's own secret-bearing variables?"""
    return name.startswith("AGENT_SESSIONS_") and (
        name in SECRET_NAMES or any(marker in name for marker in _MARKERS)
    )


def scrubbed(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``base`` (default: the process environment) with the app's secrets removed."""
    env = dict(os.environ if base is None else base)
    for name in [key for key in env if is_secret(key)]:
        del env[name]
    return env
