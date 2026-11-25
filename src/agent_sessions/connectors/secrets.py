"""A connector's credential: AES-GCM envelopes and the origin rule (#1746 D3, #1750).

**Encryption** reuses the template-secret key file (``template_secrets``): one key, one rotation
story, and a copy of the store without the key file decrypts nothing. The AAD is
``connector:<instance_id>:<credential_revision>:<field>``, so an envelope copied onto another
instance, or kept across a credential change, does not decrypt. The credential revision moves
when the token or the origin changes, never for a manifest version bump (which only needs a
re-test), so a token saved for one origin cannot be opened for another.

**The origin rule** is the AI key's (`prefs.key_origin_violation`): a stored token is bound to the
origin it was saved with. A change of origin must bring a new token, or clear it (``token: null``).
Blank and the mask mean "keep", and so can never authorize a move. The check runs INSIDE the store
lock (`store.patch`), against the stored instance it is about to change, so a racing save cannot
pair the old token with a new origin.

**Redaction.** Every decryptable stored token is part of ``template_secrets.redaction_values()``
(`store.token_values_checked`), and a replaced or removed token is retired into the in-memory set
for its TTL BEFORE the store is written, all under ``template_secrets.registration_fence()``.
"""

from __future__ import annotations

from .. import template_secrets

#: What the API shows for a stored token, and what a client may send back to mean "keep".
MASK = "••••••••"
TOKEN_MIN = template_secrets.SECRET_MIN
TOKEN_MAX = 512


class CredentialError(ValueError):
    """A token or an origin change was refused. The message never holds the token."""


def aad(instance_id: str, credential_revision: int, field: str = "token") -> str:
    return f"connector:{instance_id}:{credential_revision}:{field}"


def seal(instance_id: str, credential_revision: int, token: str) -> dict:
    """The envelope for ``token`` under this instance's current credential revision. Raises
    `template_secrets.SecretKeyUnavailable` when the key file is unusable (callers refuse)."""
    return template_secrets.encrypt(aad(instance_id, credential_revision), token)


def open_(instance_id: str, credential_revision: int, env: object) -> str | None:
    """The token, or ``None``: no key, another key, tampered, or another instance or revision."""
    if not template_secrets.valid_envelope(env):
        return None
    return template_secrets.decrypt(aad(instance_id, credential_revision), env)  # type: ignore[arg-type]


def open_checked(instance_id: str, credential_revision: int, env: object) -> str | None:
    """`open_` for REDACTION: an existing but unusable key file raises
    (`template_secrets.decrypt_checked`), so one failed read never becomes an empty set."""
    if not template_secrets.valid_envelope(env):
        return None
    return template_secrets.decrypt_checked(aad(instance_id, credential_revision), env)  # type: ignore[arg-type]


def is_new_token(value: object) -> bool:
    """A REPLACEMENT token: a non-empty string that is not the mask. Blank and the mask keep the
    stored token, so neither may authorize an origin change."""
    return isinstance(value, str) and value.strip() not in ("", MASK)


def valid_token(value: str) -> str:
    """The token as it will be stored and sent, or `CredentialError`. A header value: no
    whitespace inside, no control characters, bounded; at least ``SECRET_MIN`` so redaction can
    match it without mangling ordinary words."""
    token = value.strip()
    if len(token) < TOKEN_MIN:
        raise CredentialError(f"the token is shorter than {TOKEN_MIN} characters")
    if len(token) > TOKEN_MAX:
        raise CredentialError(f"the token is longer than {TOKEN_MAX} characters")
    if not token.isascii() or not token.isprintable() or any(c.isspace() for c in token):
        raise CredentialError("the token must be printable ASCII without spaces")
    return token


def origin_violation(
    stored_origin: str, has_token: bool, new_origin: str | None, patch: dict
) -> str | None:
    """Why ``patch`` may not move this instance to ``new_origin`` (normalized; ``None`` when the
    patch names no origin), or None when it may — the `prefs.key_origin_violation` shape. Called
    inside the store lock, against the stored instance."""
    if not has_token or new_origin is None or new_origin == stored_origin:
        return None
    if not stored_origin:
        return None  # a token saved before any origin was never bound: the first origin binds it
    if "token" in patch and (patch["token"] is None or is_new_token(patch["token"])):
        return None
    return (
        "enter the token for the new origin, or clear it (token: null) — a stored token is only "
        "sent to the origin it was saved for"
    )
