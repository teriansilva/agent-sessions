"""Reviewed adapter code, one module per `kinds.ADAPTER_KINDS` member (#1746 D1)."""

from __future__ import annotations

from types import ModuleType

from . import forgejo, github, nextcloud

#: Which module serves each adapter kind. A kind with no row has no code, so nothing reads it.
ADAPTERS: dict[str, ModuleType] = {
    "forgejo-api": forgejo,
    "github-rest": github,
    "nextcloud": nextcloud,
}


def for_kind(kind: str) -> ModuleType:
    return ADAPTERS[kind]
