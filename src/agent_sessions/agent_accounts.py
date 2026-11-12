"""Agent accounts: one home per login, laid over the agent's default home (#1820).

An agent CLI keeps one login in its home directory. To hold several, each account gets a home of
its own under ``AGENT_SESSIONS_ACCOUNTS_DIR`` (default ``~/.config/agent-sessions/agent-accounts``,
0700), and a launch points the vendor's documented home variable at it. The vendor's CLI signs in
there and is the only thing that ever writes or reads the login.

A bare second home would also be a second session store: its sessions would be missing from the
sidebar and could not be resumed from another account. So an account home is an **overlay**. The
entries that hold the login are private to it; the entries every account must share (the session
store, settings, skills) are symlinks into the default home. Which entries are which is a reviewed
recipe in ``plugins.kinds.ACCOUNT_RECIPES``, selected by the manifest's ``accounts.kind``; nothing
here names an agent.

What this module never does (docs/invariants/agent-accounts.md):

* **It never opens, copies or moves a file inside a home.** It makes directories and symlinks and
  reads link targets and file types, nothing else. ``tests/test_agent_accounts.py`` pins this on
  the module's syntax tree.
* **It never repairs a home.** An entry that should be a shared link and is not means the CLI
  replaced it (an update, an atomic rewrite), and two copies now exist. Deleting either could
  lose a session, so :func:`problems` reports it and the caller refuses that account.
* **It never touches the default login.** The only write to the default home is creating a
  shared directory that does not exist yet, empty.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .plugins import kinds

ROOT_ENV = "AGENT_SESSIONS_ACCOUNTS_DIR"
#: One path segment each: an engine id and an account id name a directory, never a path.
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class AccountError(ValueError):
    """An account home cannot be built or used as asked. The message names entries, never values."""


@dataclass(frozen=True)
class Recipe:
    """One member of ``kinds.ACCOUNT_RECIPES``, typed. Reviewed code, never manifest text."""

    kind: str
    home_env: str
    default_home: str
    private: tuple[str, ...]
    shared_dirs: tuple[str, ...]
    shared_files: tuple[str, ...]
    clear_env: tuple[str, ...]
    login: tuple[str, ...]
    status: tuple[str, ...]


def recipe(kind: str) -> Recipe | None:
    """The recipe a manifest's ``accounts.kind`` selects; None for "none" or an unknown kind."""
    raw = kinds.ACCOUNT_RECIPES.get(kind)
    return None if raw is None else Recipe(kind=kind, **raw)


def root() -> Path:
    raw = os.environ.get(ROOT_ENV)
    return Path(raw) if raw else Path.home() / ".config" / "agent-sessions" / "agent-accounts"


def _segment(value: object, what: str) -> str:
    if not isinstance(value, str) or not _SEGMENT_RE.fullmatch(value):
        raise AccountError(f"invalid {what}")
    return value


def home(engine_id: str, account_id: str) -> Path:
    """Where this account's home is. Pure: nothing is created or checked."""
    return root() / _segment(engine_id, "agent id") / _segment(account_id, "account id")


def default_home(rec: Recipe, env: Mapping[str, str] | None = None) -> Path:
    """The home the CLI uses for the default login in ``env``: its own variable when the
    environment already sets one, the vendor's default otherwise."""
    env = os.environ if env is None else env
    current = env.get(rec.home_env)
    if current:
        return Path(current)
    return Path.home() / rec.default_home.removeprefix("~/")


def _lstat(path: Path) -> os.stat_result | None:
    """None when nothing is there. Any other failure (a parent this user cannot enter) is an
    :class:`AccountError`: the question could not be answered, which refuses like a bad answer."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AccountError(f"{path.name}: cannot be examined ({exc.strerror})") from exc


def _make(what: str, name: str, make) -> None:
    try:
        make()
    except OSError as exc:
        raise AccountError(f"{name}: {what} cannot be created ({exc.strerror})") from exc


def _is_dir(path: Path) -> bool:
    """Following links: the default home's own entry may be a link the operator made."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except OSError:
        return False


def _shared(rec: Recipe) -> list[tuple[str, bool]]:
    return [(n, True) for n in rec.shared_dirs] + [(n, False) for n in rec.shared_files]


def _link_problem(name: str, link: Path, target: Path, is_dir: bool) -> str | None:
    st = _lstat(link)
    if st is None:
        return f"{name}: the shared link is missing"
    if not stat.S_ISLNK(st.st_mode):
        return f"{name}: a separate copy exists in this account's home"
    try:
        points_at = os.readlink(link)
    except OSError as exc:
        return f"{name}: the shared link cannot be read ({exc.strerror})"
    if points_at != str(target):
        return f"{name}: the shared link points somewhere else"
    if is_dir and not _is_dir(target):
        return f"{name}: the shared directory is missing from the default home"
    return None


def _private_dir(path: Path) -> None:
    """Make ``path`` (0700) or accept it only as a real directory of ours that nobody else can
    enter. Never chmod'ed into shape: a directory somebody else prepared is refused, not adopted.
    The chain above the accounts directory is the app's own config directory, trusted as it is
    for every other store there."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AccountError(f"{path.name}: cannot be created ({exc.strerror})") from exc
    st = _lstat(path)
    if (
        st is None
        or not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.geteuid()
        or stat.S_IMODE(st.st_mode) & 0o077
    ):
        raise AccountError(f"{path.name}: not a private directory of this user")


def problems(rec: Recipe, account_home: Path, shared_home: Path) -> list[str]:
    """Why this home must not be launched with; empty when it is sound.

    Checked before every launch. A shared entry that is no longer the link :func:`build` made
    would split the session store, and a private entry that became a link would hand this
    account another login, so either refuses the account instead of being repaired.
    """
    try:
        return _problems(rec, account_home, shared_home)
    except AccountError as exc:
        return [str(exc)]


def _problems(rec: Recipe, account_home: Path, shared_home: Path) -> list[str]:
    st = _lstat(account_home)
    if st is None or not stat.S_ISDIR(st.st_mode):
        return ["the account's home is missing"]
    if st.st_uid != os.geteuid():
        return ["the account's home belongs to another user"]
    if stat.S_IMODE(st.st_mode) & 0o077:
        return ["the account's home is readable by other users"]
    found = []
    for name in rec.private:
        st = _lstat(account_home / name)
        if st is not None and not stat.S_ISREG(st.st_mode):
            found.append(f"{name}: the login entry is not a regular file")
    for name, is_dir in _shared(rec):
        problem = _link_problem(name, account_home / name, shared_home / name, is_dir)
        if problem is not None:
            found.append(problem)
    return found


def build(rec: Recipe, account_home: Path, shared_home: Path) -> None:
    """Create ``account_home`` as an overlay of ``shared_home``. Idempotent; never destructive.

    An entry that already exists and is not the expected link is refused, not replaced: it is
    either a second copy of shared state or something this module did not make.
    """
    if not account_home.is_absolute() or not shared_home.is_absolute():
        raise AccountError("account homes are absolute paths")
    if not _is_dir(shared_home):
        # The agent has never run here, so there is no default home to share a store with.
        raise AccountError("the agent's default home does not exist yet; start the agent once")
    for directory in (account_home.parent.parent, account_home.parent, account_home):
        _private_dir(directory)
    for name, is_dir in _shared(rec):
        target = shared_home / name
        if is_dir and not _is_dir(target):
            if _lstat(target) is not None:
                raise AccountError(f"{name}: not a directory in the default home")
            _make("the shared directory", name, lambda target=target: os.mkdir(target, 0o700))
        link = account_home / name
        if _lstat(link) is None:
            _make("the shared link", name, lambda t=target, link=link: os.symlink(t, link))
    found = problems(rec, account_home, shared_home)
    if found:
        raise AccountError("; ".join(found))


def launch_env(rec: Recipe, account_home: Path, base: Mapping[str, str]) -> dict[str, str]:
    """``base`` as a launch on this account must see it: the vendor's home variable set, and every
    credential variable that would outrank the account's login removed. Pure."""
    env = {k: v for k, v in base.items() if k not in rec.clear_env}
    env[rec.home_env] = str(account_home)
    return env
