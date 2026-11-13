"""Unattended agent updates (#1994): installed catalog agents update themselves.

After a catalog refresh (and at startup), every installed, enabled, catalog-managed agent whose
accepted, FRESH catalog offers a newer recipe runs the existing pipeline without a human click:
install a new generation (sha256-verified, as always), run its OFFLINE checks
(`manager.offline_checks`: binary, version, store; never a vendor conversation, quota or
vendor-side history), then activate it through `manager.activate_offline`, whose preconditions
(the observed active generation, the roster revision, the pause switch and catalog freshness) are
re-checked in the activation commit itself. The operator always wins:

* **Pause** (`update.auto_update_enabled`, the one switch shared with the app's own updates)
  stops every pass. A checked candidate waits and is activated after Resume, if nothing else
  changed.
* **Disable, Roll back, or any other roster decision** during an attempt moves the active
  generation or the roster revision: the attempt ends ``superseded`` and its candidate stays
  installed and inactive.
* **Roll back** away from an auto-activated generation *declines* that exact recipe digest for
  that agent; it is never installed unattended again. A different (newer) recipe is eligible.

A ``failed`` attempt (its checks did not pass) is never retried for the same digest, in this cut
or any later one, and posts one bell notice. A busy skip (another plugin operation holds the
worker fence) records nothing. Attempt records are keyed by (agent, recipe digest, cut sequence)
in ``auto-updates.json`` beside the manager document, written under the manager document's lock;
a separate file keeps an older build able to read ``manager.json``.

What never updates unattended: a local manifest, an adopted executable, a chat/API runtime, a
disabled or retired agent, an agent the catalog stopped offering, and anything while the accepted
catalog is stale or bundled-only. A first install still needs the human review.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid

from . import agent_catalog, notifications, prefs, update
from .plugins import manager, process, storage

log = logging.getLogger(__name__)

STATE = "auto-updates.json"
FORMAT = 1
#: Terminal states. ``failed`` and declined digests are remembered separately and forever (per
#: agent, bounded), so pruning old attempt records never re-enables a rejected version.
TERMINAL = frozenset({"activated", "failed", "superseded"})
STATES = frozenset({"planned", "installed", "checked"}) | TERMINAL
MAX_ATTEMPTS = 512
MAX_REMEMBERED = 64


class Busy(Exception):
    """Another plugin operation holds the worker fence: skip, consume nothing."""


def _now() -> int:
    return int(time.time())


def _empty() -> dict:
    return {"format": FORMAT, "attempts": {}, "declined": {}, "failed": {}}


def _path():
    return storage.root() / STATE


def _read() -> dict:
    """Caller holds the manager document's lock."""
    doc = storage.read(_path())
    if doc is None:
        return _empty()
    if (
        set(doc) != {"format", "attempts", "declined", "failed"}
        or doc["format"] != FORMAT
        or not all(isinstance(doc[k], dict) for k in ("attempts", "declined", "failed"))
        or any(
            not isinstance(a, dict) or a.get("state") not in STATES
            for a in doc["attempts"].values()
        )
    ):
        raise storage.StateError("automatic update records are unreadable; left untouched")
    return doc


def _write(doc: dict) -> None:
    attempts = doc["attempts"]
    if len(attempts) > MAX_ATTEMPTS:
        # Only terminal records are pruned, oldest first; failed/declined digests live on in
        # their own maps, so nothing pruned here can re-enable a rejected version.
        terminal = sorted(
            (k for k, a in attempts.items() if a["state"] in TERMINAL),
            key=lambda k: attempts[k]["updated_at"],
        )
        for key in terminal[: len(attempts) - MAX_ATTEMPTS]:
            del attempts[key]
    storage.write(_path(), doc)


def records() -> dict:
    with storage.locked(manager.DOCUMENT):
        return _read()


def _key(plugin_id: str, digest: str, sequence: int | None) -> str:
    return f"{plugin_id}|{digest}|{sequence if sequence is not None else 0}"


def _remember(doc: dict, which: str, plugin_id: str, digest: str) -> None:
    held = [d for d in doc[which].get(plugin_id, []) if d != digest] + [digest]
    doc[which][plugin_id] = held[-MAX_REMEMBERED:]


def _mutate(fn):
    with storage.locked(manager.DOCUMENT):
        doc = _read()
        result = fn(doc)
        _write(doc)
        return result


def _set_state(key: str, state: str, **fields) -> dict:
    def change(doc):
        attempt = doc["attempts"][key]
        attempt.update(fields, state=state, updated_at=_now())
        if state == "failed":
            _remember(doc, "failed", attempt["plugin_id"], attempt["digest"])
        return dict(attempt)

    return _mutate(change)


# --- the operator's own decisions --------------------------------------------------------------


def note_rollbacks(snapshot: dict | None = None) -> list[str]:
    """Decline every auto-activated digest the operator moved away from (#1994).

    An activated attempt whose generation is no longer the ENABLED agent's active one was rolled
    back (or replaced by the operator's own choice). That exact digest is declined for that agent;
    a disabled agent declines nothing (re-enabling the same generation is not a rollback)."""
    snapshot = snapshot or manager.snapshot()
    declined = []

    def change(doc):
        for attempt in doc["attempts"].values():
            if attempt["state"] != "activated" or attempt.get("declined"):
                continue
            row = snapshot["plugins"].get(attempt["plugin_id"]) or {}
            if row.get("enabled") is True and row.get("active") != attempt.get("generation"):
                _remember(doc, "declined", attempt["plugin_id"], attempt["digest"])
                attempt["declined"] = True
                declined.append(attempt["digest"])

    _mutate(change)
    return declined


def reconcile(snapshot: dict | None = None) -> list[str]:
    """Settle attempts the manager already finished but this ledger never recorded (#1994).

    Activation commits to the manager document first and to this ledger second, so a process
    death between the two leaves a live activation recorded as ``checked``. Its operation is
    durable (`state: "complete"`, written in the same commit as the roster change), so it is
    settled from there, BEFORE candidates are computed and before `note_rollbacks` runs: an
    unsettled activation would otherwise be invisible to the rollback rule, and a later cut could
    reinstall a version the operator rolled back."""
    snapshot = snapshot or manager.snapshot()
    ops = snapshot["operations"]
    settled = []

    def change(doc):
        for key, attempt in doc["attempts"].items():
            if attempt["state"] in TERMINAL:
                continue
            op = ops.get(attempt.get("activate_op") or "")
            if op is not None and op.get("state") == "complete":
                attempt.update(state="activated", updated_at=_now())
                settled.append(key)

    _mutate(change)
    return settled


def retry_notices() -> int:
    """Deliver every failed-update notice that did not get through (#1994): a bell write that
    raised, or a process death between recording the failure and notifying. Independent of
    eligibility (a failed digest is never a candidate again), and never reinstalls or rechecks."""
    owed = [
        (key, attempt)
        for key, attempt in records()["attempts"].items()
        if attempt["state"] == "failed" and not attempt.get("notified")
    ]
    for key, attempt in owed:
        _notify_failed(key, attempt)
    return len(owed)


def _paused() -> bool:
    """The shared switch, read again wherever work is about to start (#1994 review): Pause
    stops installation and checks, not only activation."""
    from . import agent_catalog_refresh

    if not update.auto_update_enabled():
        return True
    with contextlib.suppress(ValueError, OSError):
        # An operator's catalog opt-out that could not yet be migrated to Paused (a read-only env
        # file) still freezes: before #1994 nothing ever installed on its own.
        if agent_catalog_refresh.status()["automatic"] is False:
            return True
    return False


class Paused(Exception):
    """The operator paused automatic updates while this pass was running."""


# --- eligibility ---------------------------------------------------------------------------------


def _version(entry) -> tuple[int, int, int] | None:
    return agent_catalog._version(entry)


def candidates(snapshot: dict, catalog, doc: dict) -> list[dict]:
    """Every agent that may update now: installed, enabled, catalog-managed, a console runtime,
    offered a strictly newer recipe by a FRESH remote catalog, and neither declined nor failed."""
    if catalog.source != "remote" or catalog.stale or catalog.error:
        return []
    offered = {
        c.entry.manifest.id: c
        for c in catalog.choices
        if c.source in ("signed", "bundled") and not c.included
    }
    found = []
    for plugin_id, row in snapshot["plugins"].items():
        try:
            manager._validate_row(plugin_id, row)
        except (ValueError, OSError):
            continue
        if row["enabled"] is not True or row["active"] is None:
            continue
        gen = row["generations"][row["active"]]
        if not manager.catalog_managed(gen):
            continue
        try:
            active_entry = manager._entry(gen["review"], fresh=False)
        except (ValueError, OSError):
            continue
        if active_entry.manifest.runtime != "pty":
            continue
        choice = offered.get(plugin_id)
        if choice is None or choice.entry.digest == gen["review"]["recipe_digest"]:
            continue
        ours, theirs = _version(active_entry), _version(choice.entry)
        if ours is None or theirs is None or theirs <= ours:
            continue
        digest = choice.entry.digest
        if digest in doc["declined"].get(plugin_id, []) or digest in doc["failed"].get(
            plugin_id, []
        ):
            continue
        key = _key(plugin_id, digest, catalog.sequence)
        attempt = doc["attempts"].get(key)
        if attempt is not None and attempt["state"] in TERMINAL:
            continue
        found.append(
            {
                "key": key,
                "plugin_id": plugin_id,
                "digest": digest,
                "sequence": catalog.sequence,
                "version": choice.entry.manifest.identity.version,
                "label": choice.entry.manifest.identity.label,
                "from_generation": row["active"],
                "from_version": active_entry.manifest.identity.version,
            }
        )
    return found


# --- one attempt -------------------------------------------------------------------------------


def _plan(target: dict) -> dict:
    def change(doc):
        attempt = doc["attempts"].get(target["key"])
        if attempt is None:
            attempt = {
                **{k: target[k] for k in target if k != "key"},
                "state": "planned",
                "install_op": None,
                "generation": None,
                "verify_op": None,
                "activate_op": None,
                "error": None,
                "notified": False,
                "created_at": _now(),
                "updated_at": _now(),
            }
            doc["attempts"][target["key"]] = attempt
        return dict(attempt)

    return _mutate(change)


def _install(key: str, attempt: dict) -> dict | None:
    """Install the target under the worker fence the caller holds. None: try again next pass
    (an interrupted/failed download is not a failed CHECK and never consumes the digest)."""
    if attempt["install_op"] is not None:
        try:
            op = manager.operation(attempt["install_op"])
        except manager.ManagerError:
            op = None
        if op is not None and op["state"] == "installed":
            return _set_state(key, "installed", generation=op["id"])
        if op is not None and op["state"] == "planned":
            op = manager._run_install(op["id"])
            if op["state"] == "installed":
                return _set_state(key, "installed", generation=op["id"])
        # failed / interrupted / unknown: a fresh reviewed attempt below.
    item = manager.review(plugin_id=attempt["plugin_id"])
    if item["recipe_digest"] != attempt["digest"]:
        # The catalog moved between planning and review: this target is gone, not failed.
        _set_state(key, "superseded", error="the catalog changed before installation")
        return None
    op_id = str(uuid.uuid4())
    _set_state(key, "planned", install_op=op_id)
    manager.begin_install(op_id, item["id"], item["digest"], actor=manager.AUTO_UPDATE_ACTOR)
    op = manager._run_install(op_id)
    if op["state"] != "installed":
        _set_state(key, "planned", install_op=None, error=op.get("error"))
        return None
    return _set_state(key, "installed", generation=op_id)


def _check(key: str, attempt: dict) -> dict:
    from .plugins import probes

    if attempt["verify_op"] is not None:
        try:
            op = manager.operation(attempt["verify_op"])
        except manager.ManagerError:
            op = None
        if op is not None and op["state"] in ("verified", "failed"):
            return _settle_check(key, attempt, op)
    op_id = str(uuid.uuid4())
    _set_state(key, "installed", verify_op=op_id)
    item = manager.begin_offline_verify(op_id, attempt["plugin_id"], attempt["generation"])
    manager._set_operation(op_id, "running")
    try:
        asyncio.run(probes.run_offline(item))
    except process.CleanupError:
        manager._set_operation(
            op_id, "cleanup_pending", error="temporary process cleanup is pending"
        )
        raise Busy from None
    except Exception:  # noqa: BLE001 — recorded as the operation's own outcome
        manager._set_operation(
            op_id,
            "failed",
            error="operation could not complete; retry a new attempt",
            if_states=manager._BUSY,
        )
    return _settle_check(key, attempt, manager.operation(op_id))


def _settle_check(key: str, attempt: dict, op: dict) -> dict:
    if op["state"] == "verified":
        return _set_state(key, "checked", verify_op=op["id"])
    failed = _set_state(key, "failed", verify_op=op["id"], error=op.get("error"))
    _notify_failed(key, failed)
    return failed


def _guard(digest: str, plugin_id: str):
    def check(_doc):
        if not update.auto_update_enabled():
            raise manager.ManagerError("automatic updates are paused")
        catalog = agent_catalog.current()
        if catalog.source != "remote" or catalog.stale or catalog.error:
            raise manager.ManagerError("the agent catalog is not fresh")
        if not any(
            c.entry.digest == digest and c.entry.manifest.id == plugin_id
            for c in catalog.choices
            if c.source in ("signed", "bundled")
        ):
            raise manager.ManagerError("the catalog no longer offers this version")

    return check


def _activate(key: str, attempt: dict) -> dict:
    op_id = attempt["activate_op"] or str(uuid.uuid4())
    if attempt["activate_op"] is None:
        _set_state(key, "checked", activate_op=op_id)
    try:
        manager.activate_offline(
            op_id,
            attempt["plugin_id"],
            attempt["generation"],
            expected_active=attempt["from_generation"],
            guard=_guard(attempt["digest"], attempt["plugin_id"]),
        )
    except storage.LockBusy:
        raise Busy from None
    except manager.ManagerError as exc:
        text = str(exc)
        if "busy" in text:
            raise Busy from None
        if "paused" in text or "not fresh" in text:
            # Pause defers: the checked candidate waits for Resume, re-checked then.
            _set_state(key, "checked", activate_op=None)
            return dict(attempt, state="checked")
        return _set_state(key, "superseded", error=text[:200])
    return _set_state(key, "activated")


def _attempt(target: dict) -> str:
    attempt = _plan(target)
    key = target["key"]
    if attempt["state"] in ("planned", "installed"):
        try:
            fence = storage.locked("worker", wait=0)
            fence.__enter__()
        except storage.LockBusy:
            raise Busy from None
        try:
            manager.expire_ready_signins()
            if any(o["state"] in manager._BUSY for o in manager.snapshot()["operations"].values()):
                raise Busy
            # The operator may have changed the roster since planning.
            row = manager.snapshot()["plugins"].get(target["plugin_id"]) or {}
            if row.get("active") != attempt["from_generation"] or row.get("enabled") is not True:
                _set_state(key, "superseded", error="the agent changed before installation")
                return "superseded"
            if attempt["state"] == "planned":
                if _paused():
                    raise Paused
                attempt = _install(key, attempt)
                if attempt is None or attempt["state"] != "installed":
                    return "retry"
            if _paused():
                raise Paused
            attempt = _check(key, attempt)
        finally:
            fence.__exit__(None, None, None)
        if attempt["state"] != "checked":
            return attempt["state"]
    if attempt["state"] == "checked":
        return _activate(key, attempt)["state"]
    return attempt["state"]


def _notify_failed(key: str, attempt: dict) -> None:
    """One bell notice per failed attempt (#1994), EVER.

    The bell's own tombstone (`notifications.add(once=True)`) is the durable announcement
    identity: written in the same write as the row, untouched by dismissal or eviction, and
    checked under the bell's lock. So a notice that landed before a process died, and was then
    dismissed, is never re-announced; ``None`` from the bell means "already announced", and only
    then, or after a fresh row, does this ledger record ``notified``."""
    if attempt.get("notified"):
        return
    try:
        notifications.add(
            title=(
                f"{attempt['label']} {attempt['version']} didn't pass its checks; "
                f"you're still on {attempt['from_version']}."
            ),
            project="",
            session_id="",
            engine="",
            reason="This version won't be retried. A newer one will be, once the catalog has one.",
            action_id=f"agent-update-failed:{attempt['plugin_id']}:{attempt['digest']}",
            once=True,
        )
    except Exception:  # noqa: BLE001 — a failing bell must not undo the recorded failure
        log.warning("could not post the failed-update notice for %s", attempt["plugin_id"])
        return
    _set_state(key, "failed", notified=True)


# --- the pass ----------------------------------------------------------------------------------


def run_pass() -> str:
    """One blocking pass. Returns ``paused`` / ``stale`` / ``busy`` / ``ok``."""
    # Bookkeeping first, paused or not: settle interrupted activations, then record the
    # operator's rollbacks against them, then deliver any notice that did not get through.
    snapshot = manager.snapshot()
    reconcile(snapshot)
    note_rollbacks(snapshot)
    retry_notices()
    if _paused():
        return "paused"
    catalog = agent_catalog.current()
    if catalog.source != "remote" or catalog.stale or catalog.error:
        return "stale"
    targets = candidates(snapshot, catalog, records())
    for target in targets:
        try:
            result = _attempt(target)
        except Busy:
            return "busy"
        except Paused:
            return "paused"  # the remaining targets wait for Resume
        except (manager.ManagerError, storage.StateError, ValueError, OSError) as exc:
            log.warning("automatic update of %s stopped: %s", target["plugin_id"], exc)
            continue
        log.info("automatic update of %s: %s", target["plugin_id"], result)
    return "ok"


async def run_once() -> str:
    try:
        return await asyncio.to_thread(run_pass)
    except Exception:  # noqa: BLE001 — never let a pass take the refresh loop down
        log.exception("automatic agent update pass failed")
        return "error"


# --- one-time migrations (#1994) ---------------------------------------------------------------


def migrate() -> dict:
    """Move the two retired switches to their replacements, never losing an operator's choice.

    * The publisher pref → ``AGENT_SESSIONS_FEED_PUBLISHER=1`` in the install env file: written,
      read back, and only then is the pref dropped. An explicit host value (``0`` or ``1``) is
      never overwritten. A read-only or failing env file keeps the pref, which stays honoured.
    * The catalog's daily-check opt-out (``automatic: false``) → Paused: the shared pause switch
      is written and read back first, then the catalog setting returns to automatic. If the pause
      can't be persisted, the opt-out stays exactly as it was.
    """
    from . import agent_catalog_refresh

    done = {"publisher": None, "paused": None}
    try:
        if prefs.get_feed_alerts():
            if update.feed_publisher() is None:
                try:
                    if update.set_feed_publisher(True) is True:
                        prefs.clear_feed_alerts()
                        done["publisher"] = "migrated"
                except OSError:
                    done["publisher"] = "kept"
            elif update.feed_publisher() is True:
                prefs.clear_feed_alerts()  # an interrupted migration's last step
                done["publisher"] = "migrated"
            else:
                done["publisher"] = "host-config"
    except Exception:  # noqa: BLE001
        log.exception("publisher alert migration failed; the legacy setting is still honoured")
    try:
        settings = agent_catalog_refresh.status()
        if settings["automatic"] is False:
            try:
                update.set_settings(auto_update=False)
            except OSError:
                done["paused"] = "kept"
            else:
                if update.auto_update_enabled() is False:
                    agent_catalog_refresh.configure(True)
                    done["paused"] = "migrated"
    except Exception:  # noqa: BLE001
        log.exception("catalog check migration failed; its setting is unchanged")
    return done
