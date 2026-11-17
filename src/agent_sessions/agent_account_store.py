"""Which accounts an agent has, which one each session runs on (#1820 P1a).

`agent_accounts` builds and checks one account's home; this module is the record around it:

* **the pool** — per agent, an ordered list of accounts the operator added (id, label, when);
* **the new-session pick** — which account a NEW session of that agent starts on, ``default``
  (the login the CLI already has) unless the operator chose one;
* **assignments** — which account each session was started on, so its every relaunch uses the
  same login.

The rules (docs/invariants/agent-accounts.md):

* **An agent whose manifest declares no account kind never reads this store**, and its launch
  environment is byte-identical to one before accounts existed (`for_launch` returns None first).
* **A session's account is fixed when it is created**, ``default`` included: a new session's
  choice is recorded whatever it is. A resume uses its assignment; a session with none predates
  accounts, ran on the default login and stays there. Changing an existing session's account is a
  cross-account resume, which only the real-CLI proof can show is safe, so it is not offered here.
* **Doubt refuses, never falls back.** An assignment to an account that was removed, a home that
  `agent_accounts.problems` rejects, or a store that cannot be read refuses the launch. Starting
  such a session on another login would bill the wrong account and could lose its history.
* **Removing an account forgets the record only.** Its home stays on disk: nothing here deletes
  inside a home, and the operator may still want what the vendor stored there.
* **Labels only.** The store holds the operator's own label for an account, never anything the
  vendor's CLI wrote. Whether an account is signed in is asked of the CLI by its exit status
  (`signed_in`); nothing it prints is read.

Stored at ``<accounts dir>/state.json`` (0600, in the 0700 accounts directory).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import agent_accounts
from .atomicjson import atomic_write_json, json_write_lock

VERSION = 1
#: The operator expects two or three; five bounds the probe load P2 adds per account (#1820).
MAX_ACCOUNTS = 5
LABEL_MAX = 40
DEFAULT = "default"
#: How long `signed_in` waits for a vendor's status command.
STATUS_TIMEOUT_S = 20


class AccountRefused(RuntimeError):
    """This launch must not start. ``detail`` is shown to the operator: labels, never paths."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class StoreError(ValueError):
    """A change the operator asked for was refused. The message is safe to show."""


@dataclass(frozen=True)
class Launch:
    """A launch on one account: what its process environment must say."""

    engine: str
    account_id: str
    label: str
    recipe: agent_accounts.Recipe
    home: Path

    def apply(self, env: dict[str, str]) -> None:
        """Edit ``env`` in place: the account's home set, outranking credentials removed."""
        fresh = agent_accounts.launch_env(self.recipe, self.home, env)
        env.clear()
        env.update(fresh)


def _path() -> Path:
    return agent_accounts.root() / "state.json"


def _empty() -> dict:
    return {"version": VERSION, "engines": {}, "assignments": {}}


def _valid(doc: object) -> bool:
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        return False
    engines, assignments = doc.get("engines"), doc.get("assignments")
    if not isinstance(engines, dict) or not isinstance(assignments, dict):
        return False
    for pool in engines.values():
        if not isinstance(pool, dict) or not isinstance(pool.get("accounts"), list):
            return False
        if not isinstance(pool.get("new_sessions", DEFAULT), str):
            return False
        for acc in pool["accounts"]:
            if not isinstance(acc, dict) or not isinstance(acc.get("id"), str):
                return False
            if not isinstance(acc.get("label"), str):
                return False
    return all(
        isinstance(row, dict) and isinstance(row.get("account"), str)
        for row in assignments.values()
    )


def _read() -> dict:
    """The stored document. Missing is empty; anything unreadable raises — callers refuse."""
    try:
        with _path().open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as exc:
        raise AccountRefused("the account store cannot be read") from exc
    if not _valid(doc):
        raise AccountRefused("the account store cannot be read")
    return doc


def _write(doc: dict) -> None:
    atomic_write_json(_path(), doc)


def _mutate(fn):
    try:
        agent_accounts.ensure_root()
    except agent_accounts.AccountError as exc:
        raise StoreError(str(exc)) from None
    with json_write_lock(_path()):
        try:
            doc = _read()
        except AccountRefused as exc:
            raise StoreError(exc.detail) from None
        out = fn(doc)
        _write(doc)
        return out


def _pool(doc: dict, engine: str) -> dict:
    return doc["engines"].setdefault(engine, {"accounts": [], "new_sessions": DEFAULT})


def _label(value: object) -> str:
    if not isinstance(value, str):
        raise StoreError("a label is text")
    label = " ".join(value.split())
    if not label or len(label) > LABEL_MAX:
        raise StoreError(f"a label is 1 to {LABEL_MAX} characters")
    if any(unicodedata.category(ch).startswith("C") for ch in label):
        raise StoreError("a label holds no control characters")
    if label.casefold() == DEFAULT:
        raise StoreError("that label is reserved")
    return label


def _find(pool: dict, account_id: str) -> dict:
    for acc in pool["accounts"]:
        if acc["id"] == account_id:
            return acc
    raise StoreError("no such account")


# --- the pool ------------------------------------------------------------------------------------


def _recipe_for(prov) -> agent_accounts.Recipe:
    rec = agent_accounts.recipe(getattr(prov.manifest, "accounts_kind", "none"))
    if rec is None:
        raise StoreError("this agent does not hold several accounts")
    return rec


def listing(prov) -> dict:
    """One agent's pool as the API shows it: labels, ids and health, never a path or a login."""
    rec = _recipe_for(prov)
    try:
        pool = _read()["engines"].get(prov.engine_id) or {"accounts": [], "new_sessions": DEFAULT}
    except AccountRefused as exc:
        return {"engine": prov.engine_id, "error": exc.detail, "accounts": []}
    shared = agent_accounts.default_home(rec)
    rows = []
    for acc in pool["accounts"]:
        home = agent_accounts.home(prov.engine_id, acc["id"])
        rows.append(
            {
                "id": acc["id"],
                "label": acc["label"],
                "created_at": acc.get("created_at"),
                "problems": agent_accounts.problems(rec, home, shared),
            }
        )
    return {
        "engine": prov.engine_id,
        "new_sessions": pool.get("new_sessions", DEFAULT),
        "max_accounts": MAX_ACCOUNTS,
        "accounts": rows,
    }


def add(prov, label: object) -> dict:
    """Add an account and build its home. The home is built before the record is written, so a
    record never names a home that does not exist; a home left by a failed record is harmless
    (nothing launches with an unrecorded account) and is reused by nothing."""
    rec = _recipe_for(prov)
    clean = _label(label)
    engine = prov.engine_id

    def change(doc):
        pool = _pool(doc, engine)
        if len(pool["accounts"]) >= MAX_ACCOUNTS:
            raise StoreError(f"an agent holds at most {MAX_ACCOUNTS} accounts")
        if any(a["label"].casefold() == clean.casefold() for a in pool["accounts"]):
            raise StoreError("another account already has that label")
        taken = {a["id"] for a in pool["accounts"]}
        account_id = "a-" + secrets.token_hex(4)
        if account_id in taken:
            raise StoreError("try again")
        home = agent_accounts.home(engine, account_id)
        if os.path.lexists(home):
            raise StoreError("try again")  # an id whose home already exists is never adopted
        try:
            agent_accounts.build(rec, home, agent_accounts.default_home(rec))
        except agent_accounts.AccountError as exc:
            raise StoreError(str(exc)) from None
        acc = {"id": account_id, "label": clean, "created_at": round(time.time())}
        pool["accounts"].append(acc)
        return dict(acc)

    return _mutate(change)


def rename(prov, account_id: str, label: object) -> None:
    _recipe_for(prov)
    clean = _label(label)

    def change(doc):
        pool = _pool(doc, prov.engine_id)
        acc = _find(pool, account_id)
        if any(
            a["label"].casefold() == clean.casefold() and a["id"] != account_id
            for a in pool["accounts"]
        ):
            raise StoreError("another account already has that label")
        acc["label"] = clean

    _mutate(change)


def reorder(prov, order: object) -> None:
    _recipe_for(prov)

    def change(doc):
        pool = _pool(doc, prov.engine_id)
        ids = [a["id"] for a in pool["accounts"]]
        if (
            not isinstance(order, list)
            or not all(isinstance(i, str) for i in order)
            or len(order) != len(ids)
            or set(order) != set(ids)
        ):
            raise StoreError("the order must name every account once")
        by_id = {a["id"]: a for a in pool["accounts"]}
        pool["accounts"] = [by_id[i] for i in order]

    _mutate(change)


def remove(prov, account_id: str) -> None:
    """Forget an account. Its home stays on disk, and sessions assigned to it refuse to launch
    rather than start on another login, for good: `add` never adopts an existing home or id, so a
    re-added label is a new account those sessions never resolve to. A new-session pick of it
    falls back to the default."""
    _recipe_for(prov)

    def change(doc):
        pool = _pool(doc, prov.engine_id)
        _find(pool, account_id)
        pool["accounts"] = [a for a in pool["accounts"] if a["id"] != account_id]
        if pool.get("new_sessions") == account_id:
            pool["new_sessions"] = DEFAULT

    _mutate(change)


def set_new_sessions(prov, account_id: object) -> None:
    """Which account new sessions of this agent start on: an account id, or ``default``."""
    _recipe_for(prov)
    if not isinstance(account_id, str):
        raise StoreError("name an account, or default")

    def change(doc):
        pool = _pool(doc, prov.engine_id)
        if account_id != DEFAULT:
            _find(pool, account_id)
        pool["new_sessions"] = account_id

    _mutate(change)


def assignment(keys: Iterable[str]) -> str | None:
    """The account a session was started on, or None (the default login, recorded or not).
    Never raises."""
    try:
        rows = _read()["assignments"]
    except AccountRefused:
        return None
    for key in keys:
        row = rows.get(key)
        if row is not None:
            return None if row["account"] == DEFAULT else row["account"]
    return None


# --- the launch ----------------------------------------------------------------------------------


def for_launch(
    prov,
    keys: list[str],
    *,
    new: bool,
    base_env: Mapping[str, str] | None = None,
) -> Launch | None:
    """The account this launch runs on, or None for the default login.

    ``keys`` are the session's identities, logical first (``engine:native``, then the runtime key
    where it differs); a new session's choice is recorded under each, so a placeholder-keyed
    launch and its later resume under the adopted id both find it. Raises :class:`AccountRefused`
    when the session must not start. Synchronous file I/O: call it off the event loop.
    """
    rec = agent_accounts.recipe(getattr(prov.manifest, "accounts_kind", "none"))
    if rec is None:
        return None  # before any read: an agent without accounts is untouched
    engine = prov.engine_id
    keys = [k for k in dict.fromkeys(keys) if k]
    if new:
        account_id = _record_new(engine, keys)
    else:
        doc = _read()
        account_id = next(
            (doc["assignments"][k]["account"] for k in keys if k in doc["assignments"]), None
        )
    if account_id is None or account_id == DEFAULT:
        return None  # never assigned (an older session), or created on the default login
    doc = _read()
    pool = doc["engines"].get(engine) or {"accounts": []}
    acc = next((a for a in pool["accounts"] if a["id"] == account_id), None)
    if acc is None:
        raise AccountRefused("this session's account was removed; start a new session")
    home = agent_accounts.home(engine, account_id)
    found = agent_accounts.problems(rec, home, agent_accounts.default_home(rec, base_env))
    if found:
        raise AccountRefused(f"account {acc['label']!r} cannot be used: {found[0]}")
    return Launch(engine=engine, account_id=account_id, label=acc["label"], recipe=rec, home=home)


def _record_new(engine: str, keys: list[str]) -> str:
    """Fix a new session's account: the agent's new-session pick, recorded under every key in
    the same write that reads the pick, so a pick changed concurrently cannot split the two.

    ``default`` is recorded too. Without it, a session created on the default login and
    relaunched before it wrote history would be indistinguishable from one never assigned, and
    would take whatever the pick had become meanwhile (Hermes on #2048). A key that already has
    an assignment keeps it."""
    doc = _read()
    held = next((doc["assignments"][k]["account"] for k in keys if k in doc["assignments"]), None)
    if held is not None:
        return held  # a new session relaunched before it wrote history keeps its first account

    def change(doc):
        held = next(
            (doc["assignments"][k]["account"] for k in keys if k in doc["assignments"]), None
        )
        if held is not None:
            return held
        pick = (doc["engines"].get(engine) or {}).get("new_sessions", DEFAULT)
        now = round(time.time())
        for key in keys:
            doc["assignments"][key] = {"account": pick, "at": now}
        return pick

    try:
        return _mutate(change)
    except StoreError as exc:
        raise AccountRefused(str(exc)) from None


def for_signin(prov, account_id: str) -> Launch:
    """The account a sign-in runs on: it must be in the pool and its home sound."""
    rec = _recipe_for(prov)
    try:
        pool = _read()["engines"].get(prov.engine_id) or {"accounts": []}
    except AccountRefused as exc:
        raise StoreError(exc.detail) from None
    acc = _find(pool, account_id)
    home = agent_accounts.home(prov.engine_id, account_id)
    found = agent_accounts.problems(rec, home, agent_accounts.default_home(rec))
    if found:
        raise StoreError(f"account {acc['label']!r} cannot be used: {found[0]}")
    return Launch(
        engine=prov.engine_id, account_id=account_id, label=acc["label"], recipe=rec, home=home
    )


# --- asking the CLI ------------------------------------------------------------------------------


def signed_in(prov, account_id: str) -> bool | None:
    """Ask the vendor's own status command, in the account's home, whether it is signed in.

    True on exit 0, False on exit 1 (both CLIs measured), None for anything else: a timeout, a
    missing binary, another code. Nothing it prints is read; its output goes nowhere. The process
    gets a minimal environment, never the app's, and its own process group, ended on timeout.
    """
    rec = _recipe_for(prov)
    home = agent_accounts.home(prov.engine_id, account_id)
    if agent_accounts.problems(rec, home, agent_accounts.default_home(rec)):
        return None
    try:
        binary = prov.entrypoint_path()
    except Exception:
        return None
    base = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
    }
    env = agent_accounts.launch_env(rec, home, base)
    from .plugins.provider import forced_env

    try:
        env.update(forced_env(prov))  # the agent's own isolation, last, as at every spawn
    except Exception:
        return None
    try:
        proc = subprocess.Popen(  # noqa: S603 — literal argv from a reviewed recipe
            [binary, *rec.status],
            env=env,
            cwd=str(home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        code = proc.wait(timeout=STATUS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        if proc.pid > 1:  # never killpg(1) (kill(-1)): a fake pid must not reach every process
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        proc.wait()
        return None
    return {0: True, 1: False}.get(code)
