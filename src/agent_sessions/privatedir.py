"""A directory this process can trust — a verified private subtree (#1006).

Why this module exists, stated as the defect it removes. ``gitwrite.hooks_void()`` used to hand git
a ``tempfile.mkdtemp()`` directory in the **shared** temp dir as ``core.hooksPath`` — one per
*process*, never removed (34,282 of them on this host on 2026-09-16), and re-validated only by
``os.path.isdir()``. A name in a world-writable directory is not ours: delete one and any local
user may recreate it holding executable hooks, which a peer instance then hands to git. The fix is
not to prune harder — it is to stop putting the name somewhere anyone else can take it.

**The trust-anchor policy, and why "every ancestor is ours" is the wrong rule.** Requiring every
ancestor to be owned by the effective uid rejects an ordinary install outright: ``/`` and ``/home``
are root-owned on every normal Linux system. So the rule is two-part, split at the **anchor** — the
first component this process actually owns:

* **Above the anchor** — every ancestor must be a real directory (never a symlink), must not be
  writable by any other account, **and** must be owned by a *trusted* owner: root, or this euid.
  Mode bits alone are not sufficient, and the issue's earlier wording that they were is corrected
  here: the mode we observe is a fact about this instant, while the **ownership** is the fact about
  who may change it. An unrelated owner sitting at ``0755`` today can ``chmod 0777`` tomorrow, so a
  mode-only ancestor rule verifies a property its own subject is free to revoke.
* **At and below the anchor** — every component must be owned by the effective uid, be a real
  directory, and not be writable by any other account.

**"Writable by another account" is the question — and the write BIT is the faithful answer.** Any
group- or other-write bit disqualifies, with no exception for a group that merely *looks*
exclusive. An earlier cut did admit group-write when the owning group had no other members, because
Debian and Ubuntu's per-user-group convention (``umask 002``) produces ``0775`` directories nobody
else can touch, and refusing those denied git writes on an ordinary install — the mirror defect
this module also has to avoid. That exception could not be made sound, for two independent reasons,
both reproduced in review of PR #1013: ``st_mode``'s group bits may be an ACL **mask** rather than
the owning group's permissions (so a named unrelated account can hold effective ``rwx`` under an
exclusive group), and NSS enumeration cannot prove a group has no other members (``pwd.getpwall()``
omits accounts that still resolve individually, and answers incompletely without raising).

Refusing the bit closes both at once, and it is **faithful** rather than merely strict: the mask
bounds every named ACL entry, so an entry with effective write forces the group bit on, and a mask
that clears the bit strips that entry's write too. The cost is operational and is stated plainly —
an install whose runtime ancestry is group-writable is refused until the operator tightens it, and
the refusal names the exact ``chmod``. The app's own ``~/.agent-sessions`` is created ``0775`` by
``mkdir(parents=True, mode=0o700)``, whose mode applies only to the FINAL component, so an existing
deployment can need that one-time fix. See :func:`_refuse_foreign_write`.

**Shared temp is refused as an anchor, sticky bit or not.** The sticky bit stops a local user
*deleting* another's entry; it does nothing to stop them *creating* a name we would later trust,
which is precisely the defect above. The refusal is therefore by **identity** (see
:func:`_refuse_shared_temp`) and not merely by mode, so a host whose ``/tmp`` is somehow not
world-writable is refused just the same.

**Fail closed.** Every failure here — including a ``stat`` that simply could not be read — refuses.
It never falls back to shared temp, never "repairs" a path an operator supplied, and never deletes
unexpected contents. That shape is deliberate and was learned the expensive way: PR #1000 shipped a
fail-*soft* read at a destructive eligibility boundary, and it let prune delete an **active**
session's scrollback. A read failure at a boundary that decides "is this ours" is the same shape,
so it gets the same answer.

Scope note: :func:`agent_sessions.ptybridge.runtime_dir` is *not* itself a boundary — it takes an
environment override, does ``mkdir(exist_ok=True)`` and validates nothing. This module supplies the
verification for the one caller that needs it; hardening ``runtime_dir()`` for its existing pty
callers is deliberately **not** done here (#1006, "Out of scope").
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class PrivateDirError(RuntimeError):
    """A path could not be established as a private subtree owned by this process."""


@dataclass(frozen=True)
class TrustPolicy:
    """Where a private subtree may be anchored.

    ``test_roots`` is the **test harness's** exception and has no production spelling: there is no
    environment variable, route, pref or config key anywhere that sets it, and this module never
    reads ``os.environ`` at all. Production evaluates :data:`PRODUCTION`, whose ``test_roots`` is
    empty; the suite injects a permissive policy through :func:`set_policy_for_test` because
    pytest's ``tmp_path`` lives under ``/tmp`` — which the rules above refuse **by design**, so the
    exception has to be declared rather than silently skipped.

    It is a *tuple* because the suite legitimately needs two: ``tmp_path`` (where the tests build
    their fixtures) and a short ``mkdtemp`` runtime dir. The two cannot be one directory — an
    ``AF_UNIX`` socket path is capped at 108 bytes and ``tmp_path`` alone can spend ~90 of them on
    a long test name, so the runtime dir has to live somewhere shorter.

    Pinned by ``tests/test_privatedir.py`` (``..._has_no_production_spelling``).
    """

    #: Absolute paths the harness declares as anchors. Components ABOVE one are not checked; the
    #: at-and-below rules apply from it downwards exactly as in production.
    test_roots: tuple[str, ...] = ()


#: The only policy production ever evaluates.
PRODUCTION = TrustPolicy()

_test_policy: TrustPolicy | None = None


def set_policy_for_test(policy: TrustPolicy | None) -> None:
    """Install (or clear, with ``None``) the suite's policy. Called from ``tests/conftest.py``."""
    global _test_policy
    _test_policy = policy


def active_policy() -> TrustPolicy:
    """The policy in force. :data:`PRODUCTION` unless the harness installed one."""
    return _test_policy or PRODUCTION


def _shared_temp_dirs() -> frozenset[str]:
    """Directories shared between local accounts **by definition**, so never a trust anchor.

    Resolved per call rather than at import: ``tempfile.gettempdir()`` consults ``TMPDIR`` and a
    value captured at import would miss a later one — and this list is a *refusal* set, where
    missing an entry is the failure that matters.
    """
    # S108 reads these literals as "insecure use of a temp directory". It is inverted here: this
    # is the REFUSAL set, and naming these paths is precisely what stops them ever being trusted.
    out = {"/tmp", "/var/tmp", "/dev/shm", tempfile.gettempdir()}  # noqa: S108
    return frozenset(os.path.realpath(p) for p in out)


def _refuse_shared_temp(component: Path) -> None:
    """Refuse shared temp by NAME — deliberately without consulting the mode.

    This is what makes the refusal hold "sticky bit or not": a ``/tmp`` at ``0755`` would sail
    through the mode rule while still being the directory whose name-takeover defect started
    #1006.
    """
    if str(component) in _shared_temp_dirs():
        raise PrivateDirError(
            f"{component}: a shared temp directory is never a trust anchor — the sticky bit "
            "stops a local user deleting another's entry, not creating a name we would trust"
        )


def _refuse_foreign_write(component: Path, st: os.stat_result, euid: int) -> None:
    """Refuse a directory that any account other than this one can write.

    **Any** group- or other-write bit disqualifies. An earlier cut admitted group-write when the
    owning group looked exclusive; it could not be made sound, for two independent reasons raised
    in review of PR #1013 and reproduced on a real filesystem:

    * **The mode bits can be an ACL mask, not the owning group's permissions.** Under an extended
      POSIX ACL, ``st_mode``'s group bits report the *mask*, so a directory can have an exclusive
      owning group while a named unrelated account holds effective ``rwx``. MEASURED: ``0770`` with
      ``user:65534:rwx`` was classified private and admitted by the old rule.
    * **NSS enumeration cannot prove a group is exclusive.** ``pwd.getpwall()`` may omit accounts
      that still resolve individually (sssd's ``enumerate`` defaults to false), and an incomplete
      answer raises nothing to catch — so absence from the list was never evidence.

    Refusing the bit removes both, and is *faithful* rather than merely strict: the mask bounds
    every named ACL entry, so an entry with effective write forces the group bit on, and a mask
    that clears the bit strips that entry's write (MEASURED both ways). The bit therefore answers
    exactly the question that matters — can a second account write here.
    """
    if st.st_mode & stat.S_IWOTH:
        raise PrivateDirError(
            f"{component}: other-writable (mode {stat.S_IMODE(st.st_mode):04o}) — any local "
            "account could replace what lives under it"
        )
    if st.st_mode & stat.S_IWGRP:
        # The remediation rides on the message: strict refusal is right, but an install whose
        # runtime ancestry is group-writable (the `umask 002` shape) would otherwise be left with
        # a refusal it cannot act on.
        raise PrivateDirError(
            f"{component}: group-writable (mode {stat.S_IMODE(st.st_mode):04o}) — another account "
            f"in group {st.st_gid} could replace what lives under it, and these bits may be an ACL "
            f"mask rather than that group's own permissions. Fix with: chmod g-w {component}"
        )


def _check_above_anchor(component: Path, st: os.stat_result, euid: int) -> None:
    """An ancestor: a trusted owner (root or us) **and** no group/other write. Both, not either."""
    if st.st_uid not in (0, euid):
        raise PrivateDirError(
            f"{component}: owned by uid {st.st_uid}, which is neither root nor this process "
            f"(uid {euid}) — an untrusted owner may widen this directory's mode at any time, so "
            "the mode alone establishes nothing"
        )
    _refuse_foreign_write(component, st, euid)
    _refuse_shared_temp(component)


def _check_at_or_below_anchor(component: Path, st: os.stat_result, euid: int) -> None:
    """The boundary and everything under it: ours outright, and no group/other write."""
    if st.st_uid != euid:
        raise PrivateDirError(
            f"{component}: owned by uid {st.st_uid}, not by this process (uid {euid}) — at and "
            "below the trust anchor a directory must be ours outright"
        )
    _refuse_foreign_write(component, st, euid)
    _refuse_shared_temp(component)


def _components(path: Path) -> list[Path]:
    """``/a/b`` -> ``[/, /a, /a/b]`` — filesystem root first, the target itself last."""
    return [*reversed(path.parents), path]


def _test_root_index(parts: list[Path], policy: TrustPolicy) -> int | None:
    """Index of the harness's declared anchor, when the target is inside it.

    Returns ``None`` when no test root is installed *or* when the target lies outside it — so a
    production path keeps being judged by the production rules even while the suite's policy is
    active. That is what lets the default-home positive test assert the real rule.
    """
    for root in policy.test_roots:
        try:
            return parts.index(Path(root))
        except ValueError:
            continue
    return None


def _first_owned(parts: list[Path], stats: list[os.stat_result], euid: int) -> int:
    for i, st in enumerate(stats):
        if st.st_uid == euid:
            return i
    raise PrivateDirError(
        f"{parts[-1]}: no component of this path is owned by this process (uid {euid}), so there "
        "is no private subtree to anchor to"
    )


def verify_private_dir(path: str | os.PathLike[str], *, policy: TrustPolicy | None = None) -> str:
    """Verify ``path`` is a directory inside a private subtree this process owns.

    Returns the verified path. Raises :class:`PrivateDirError` on anything it cannot establish —
    there is no soft outcome. A caller holding a return value may rely on every component from the
    filesystem root down having been checked **in this call**, never in an earlier one: "it was
    fine once" is exactly what the old ``os.path.isdir()`` re-check amounted to.
    """
    pol = policy if policy is not None else active_policy()
    p = Path(path)
    if not p.is_absolute():
        raise PrivateDirError(f"{p}: not an absolute path")

    parts = _components(p)
    euid = os.geteuid()

    # One lstat per component, and `lstat` rather than `stat` is the entire symlink defence: a
    # symlink reports S_ISLNK and never S_ISDIR, so "is a real directory" and "is not a symlink"
    # are one question asked once. Resolving first (realpath) would answer about the TARGET and
    # say nothing about the link an attacker controls.
    stats: list[os.stat_result] = []
    for c in parts:
        try:
            st = os.lstat(c)
        except OSError as exc:
            raise PrivateDirError(f"{c}: cannot be read ({exc.strerror})") from exc
        if not stat.S_ISDIR(st.st_mode):
            raise PrivateDirError(f"{c}: not a real directory (a symlink, or not a directory)")
        stats.append(st)

    test_root = _test_root_index(parts, pol)
    if test_root is not None:
        # THE test-harness exception, and it is confined to this branch: ancestors above the
        # declared root go unchecked because pytest's tmp_path sits under /tmp. Everything from
        # the root downwards is judged by the production rules below, unchanged.
        anchor = test_root
    else:
        anchor = _first_owned(parts, stats, euid)
        for i in range(anchor):
            _check_above_anchor(parts[i], stats[i], euid)

    for i in range(anchor, len(parts)):
        _check_at_or_below_anchor(parts[i], stats[i], euid)
    return str(p)


def ensure_private_dir(
    path: str | os.PathLike[str], *, mode: int, policy: TrustPolicy | None = None
) -> str:
    """Create ``path`` at ``mode`` if it is absent, and verify the whole chain either way.

    Creation is idempotent and non-destructive, which is what makes concurrent first use safe: the
    loser of the race takes ``FileExistsError`` and both callers then verify the same directory.
    An existing directory is **never** chmod'ed back into shape — "repairing" a path we did not
    create is indistinguishable from adopting one somebody else prepared, so a wrong mode refuses.
    """
    pol = policy if policy is not None else active_policy()
    p = Path(path)
    # The parent chain is verified BEFORE anything is created under it, so a refused subtree never
    # gets written to in passing.
    verify_private_dir(p.parent, policy=pol)

    created = False
    try:
        os.mkdir(p, mode)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise PrivateDirError(f"{p}: could not be created ({exc.strerror})") from exc
    if created:
        # `mkdir`'s mode argument is masked by the umask, which can only REMOVE bits — so the
        # window between these two calls is never *more* permissive than `mode`. The chmod only
        # makes it exact, and it runs solely on the path this call just created.
        os.chmod(p, mode)

    verify_private_dir(p, policy=pol)
    st = os.lstat(p)
    if stat.S_IMODE(st.st_mode) != mode:
        raise PrivateDirError(
            f"{p}: mode is {stat.S_IMODE(st.st_mode):04o}, expected {mode:04o} — refusing rather "
            "than changing the mode of a directory this call did not create"
        )
    return str(p)
