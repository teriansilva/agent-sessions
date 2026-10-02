"""Admin credential management (#65 Phase 4).

The single admin credential lives in the env file as a hash (`AGENT_SESSIONS_PASSWORD_HASH`)
— the plaintext is never persisted. Changing it rewrites the hash and clears the
first-run "force password change" flag, via the secure env writer.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from . import envfile
from .auth import hash_password

FORCE_CHANGE_KEY = "AGENT_SESSIONS_FORCE_PASSWORD_CHANGE"
HASH_KEY = "AGENT_SESSIONS_PASSWORD_HASH"


def random_password() -> str:
    """A strong URL-safe password (~24 chars)."""
    return secrets.token_urlsafe(18)


def set_password(env_path: Path, password: str) -> None:
    """Persist a new admin password (hash only) and clear the force-change flag."""
    envfile.update(env_path, {HASH_KEY: hash_password(password), FORCE_CHANGE_KEY: None})
