"""Public agent definitions: release-bundled authority and verified remote updates (#1370).

The installed release supplies a usable catalog without a network request. Once a remote
catalog has been accepted, its durable floor constrains the bundled fallback too: an expired
remote recipe is installable on release authority only when its exact digest is bundled AND
still offered by the highest accepted remote catalog. Damaged evidence is never first use.
The legacy ``plugins`` package/state names remain installation compatibility details.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from .plugins import feed

BUNDLED_FILE = Path(__file__).with_name("agent_catalog.json")
SOURCE_URL = "https://github.com/teriansilva/agent-sessions/tree/main/release/recipes/linux-x64"
HISTORY_URL = "https://github.com/teriansilva/agent-sessions/commits/main/release/recipes/linux-x64"


@dataclass(frozen=True)
class Choice:
    entry: feed.Entry
    source: str | None
    reason: str | None = None
    included: bool = False


@dataclass(frozen=True)
class Catalog:
    choices: tuple[Choice, ...]
    sequence: int | None
    expires_at: int | None
    digest: str
    bundled_digest: str
    source: str
    stale: bool = False
    error: str | None = None


def bundled() -> tuple[tuple[feed.Entry, ...], str]:
    data = BUNDLED_FILE.read_bytes()
    doc = feed.decode(data)
    if set(doc) != {"format", "agents"} or type(doc["format"]) is not int or doc["format"] != 1:
        raise feed.FeedError("the bundled agent catalog has an unsupported format")
    if not isinstance(doc["agents"], list) or not 0 < len(doc["agents"]) <= feed.MAX_PLUGINS:
        raise feed.FeedError("the bundled agent catalog has an invalid agent list")
    entries = tuple(feed.entry(raw, signed=True) for raw in doc["agents"])
    if len({e.manifest.id for e in entries}) != len(entries):
        raise feed.FeedError("the bundled agent catalog has duplicate identities")
    return entries, hashlib.sha256(data).hexdigest()


def current() -> Catalog:
    """Read one verified selection; unavailable entries remain visible for diagnosis."""
    entries, bundle_digest = bundled()
    by_digest = {e.digest: e for e in entries}
    included = {e.manifest.id for e in entries if e.manifest.runtime == "api"}
    try:
        remote = feed.historical()
    except (ValueError, OSError):
        reason = "Saved catalog trust evidence is unavailable. Restore it before installing."
        return Catalog(
            tuple(Choice(e, None, reason, e.manifest.id in included) for e in entries),
            None,
            None,
            bundle_digest,
            bundle_digest,
            "unavailable",
            error=reason,
        )
    if remote is None:
        return Catalog(
            tuple(
                Choice(
                    e,
                    None if e.manifest.id in included else "bundled",
                    "Included with BattleLab; configure its source agent."
                    if e.manifest.id in included
                    else None,
                    e.manifest.id in included,
                )
                for e in entries
            ),
            None,
            None,
            bundle_digest,
            bundle_digest,
            "bundled",
        )
    now = int(time.time())
    stale = remote.expires_at <= now or remote.issued_at > now + feed.CLOCK_SKEW
    choices = []
    seen = set()
    for entry in remote.entries:
        seen.add(entry.manifest.id)
        source = "signed" if not stale else "bundled" if entry.digest in by_digest else None
        reason = None if source else "The remote catalog expired; refresh before installing."
        choices.append(Choice(entry, source, reason))
    for entry in entries:
        if entry.manifest.id not in seen:
            builtin = entry.manifest.id in included
            reason = (
                "Included with BattleLab; configure its source agent."
                if builtin
                else "This agent is not offered by the last accepted remote catalog."
            )
            choices.append(Choice(entry, None, reason, builtin))
    return Catalog(
        tuple(choices),
        remote.sequence,
        remote.expires_at,
        remote.digest,
        bundle_digest,
        "remote",
        stale=stale,
    )


def select(agent_id: str) -> tuple[Choice, int | None]:
    catalog = current()
    choice = next((c for c in catalog.choices if c.entry.manifest.id == agent_id), None)
    if choice is None:
        raise feed.FeedError("the catalog does not offer this agent")
    if choice.source is None:
        raise feed.FeedError(choice.reason or "this catalog entry cannot be installed")
    return choice, catalog.sequence if choice.source == "signed" else None


def reviewed(digest: str, source: str) -> feed.Entry:
    """Recheck an exact review's authority immediately before staging an installation."""
    for choice in current().choices:
        if choice.entry.digest == digest and choice.source == source:
            return choice.entry
    raise feed.FeedError("the catalog changed; review this installation again")
