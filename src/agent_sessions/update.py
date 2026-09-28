"""Self-update (#65 Phase 5; in-app auto-update #538).

`check()` compares the running version to the latest on the chosen channel (the highest
`v*` tag for ``stable``, the remote ``main`` HEAD for ``main``). `apply()` performs the
update with **no user-supplied input** — it re-runs the installer detached, which builds
the channel's latest into a fresh atomic release, flips `current`, restarts, health-checks,
and rolls back on failure. The HTTP endpoints are authed + CSRF + origin-gated.

Settings (#538): the auto-update opt-in and the release channel are persisted as the fixed
``AGENT_SESSIONS_AUTOUPDATE`` / ``AGENT_SESSIONS_CHANNEL`` keys in the install env file and
read **live** (env file first, process env fallback) — a Settings toggle applies without a
service restart, because the running service's ``os.environ`` snapshot predates the write.
``autoupdate()`` / ``apply_manual()`` share one single-flight lock so the daily loop and the
manual "Update now" can never spawn two installers concurrently.

Release verification (#612)
---------------------------
A git tag is a **movable pointer**. ``latest_ref`` picks the highest ``v*`` tag off the
remote, so anyone able to write to the forge can re-point an existing release tag at a
different commit, and every install tracking ``stable`` would take that code silently — no
diff, no review, no version change. ``scripts/release-manifest.json`` is the committed trust
root that closes it: it records what each tag pointed at when it was cut, and
``verify_release_tag`` refuses to update when the remote disagrees.

**A tag the manifest does not know about is allowed through, deliberately.** The manifest
ships *inside* the repo, so the copy a running build holds is the one that was current when
*that* build was cut and can never contain an entry for a release tagged afterwards. Failing
closed on an unknown tag would therefore not be strict — it would mean **no install ever
auto-updates again**, because every genuine update is by construction a tag the running
build has not heard of. What this buys instead is precise and worth stating plainly:

* **Caught:** retroactive mutation of any release this build knows about — the attack where
  an old, already-reviewed tag is quietly re-pointed.
* **Not caught:** a brand-new tag published after this build was cut. Nothing shipped in an
  older artifact can vouch for a newer one; that needs signature verification over the tag
  object, which the issue anticipates as the eventual replacement.

So this is a real narrowing, not a complete answer, and it is not presented as one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import discover, envfile
from .version import get_version

log = logging.getLogger(__name__)

_DEFAULT_REPO = "https://github.com/teriansilva/agent-sessions.git"

AUTOUPDATE_KEY = "AGENT_SESSIONS_AUTOUPDATE"
CHANNEL_KEY = "AGENT_SESSIONS_CHANNEL"
CHANNELS = ("stable", "main")

# Single-flight (#538): one check/apply at a time, shared between the scheduled loop and
# the manual /api/update/apply path.
_RUN_LOCK = threading.Lock()
# After an installer spawn, the service is about to be restarted by that installer (or the
# spawn failed silently) — the scheduled path skips instead of stacking a second installer.
_SPAWNED_AT: float | None = None
_SPAWN_COOLDOWN_S = 15 * 60
# Recent-runtime status of the last SCHEDULED pass (#538): in-memory by design — a status
# hint for the Settings card, not an audit log. Resets on restart.
_LAST_AUTO: dict[str, object] | None = None
# Why the last update was refused by release verification (#612), or None. A refusal is
# otherwise invisible — `apply()` returning False reads identically to "no installer here" —
# and a silently-not-updating install is exactly what an attacker who moved a tag would want
# nobody to notice. Surfaced on `check()` so the Settings card can show it.
_LAST_BLOCK: str | None = None
# The installer this process spawned (#1085), kept so its exit can be observed — and it reaped —
# through its own handle. See `_pid_alive`.
_INSTALLER: subprocess.Popen | None = None


def _repo_url() -> str:
    return os.environ.get("AGENT_SESSIONS_REPO") or _DEFAULT_REPO


def _env_path() -> Path:
    return Path(os.environ.get("AGENT_SESSIONS_ENV_FILE") or discover.default_env_path())


def _env_get(key: str) -> str | None:
    """``KEY=value`` from the install env file — the live source of truth for settings the
    UI changes at runtime (#538). Fail-soft: absent/unreadable file → None."""
    try:
        for ln in _env_path().read_text().splitlines():
            if ln.startswith(f"{key}="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _channel() -> str:
    ch = _env_get(CHANNEL_KEY) or os.environ.get(CHANNEL_KEY) or "stable"
    return ch if ch in CHANNELS else "stable"


def auto_update_enabled() -> bool:
    v = _env_get(AUTOUPDATE_KEY)
    if v is None:
        v = os.environ.get(AUTOUPDATE_KEY) or ""
    return v.strip().lower() in ("1", "true", "yes")


def settings() -> dict[str, object]:
    """The public update settings — what the Settings card shows and POSTs."""
    return {"auto_update": auto_update_enabled(), "channel": _channel()}


def set_settings(
    *, auto_update: bool | None = None, channel: str | None = None
) -> dict[str, object]:
    """Persist the given settings to the env file (only the two fixed keys — this is NOT a
    generic env editor) and return the new public state. Raises ValueError on a channel
    outside ``CHANNELS``; callers validate types before calling."""
    updates: dict[str, str | None] = {}
    if auto_update is not None:
        updates[AUTOUPDATE_KEY] = "1" if auto_update else "0"
    if channel is not None:
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {CHANNELS}")
        updates[CHANNEL_KEY] = channel
    if updates:
        path = _env_path()
        path.parent.mkdir(parents=True, exist_ok=True)  # dev checkouts have no install dir yet
        envfile.update(path, updates)
    return settings()


def last_auto() -> dict[str, object] | None:
    return dict(_LAST_AUTO) if _LAST_AUTO else None


def record_auto(result: str) -> None:
    global _LAST_AUTO
    _LAST_AUTO = {"ts": time.time(), "result": result}


def _home() -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    ).expanduser()


_RELEASE_TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")


def _release_tags(ls_remote: str) -> list[str]:
    """Release tag names from ``git ls-remote --tags --refs`` output: the FULL name after
    ``refs/tags/``, and only the exact ``vX.Y.Z`` shape. Taking the text after the last slash
    let a remote tag ``v999/z`` stand in for a branch ``z`` (Hermes on #1206)."""
    out = []
    for line in ls_remote.splitlines():
        _sha, _, ref = line.partition("\t")
        name = ref.strip().removeprefix("refs/tags/")
        if ref.strip().startswith("refs/tags/") and _RELEASE_TAG_RE.fullmatch(name):
            out.append(name)
    return out


def latest_ref(channel: str, repo_url: str) -> str | None:
    """The channel's latest ref on the remote: the highest ``v*`` tag (stable) or the
    ``main`` short SHA. None if git/network is unavailable."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        if channel == "main":
            out = subprocess.run(  # noqa: S603
                [git, "ls-remote", repo_url, "main"], capture_output=True, text=True, timeout=15
            )
            sha = out.stdout.split("\t", 1)[0].strip() if out.returncode == 0 else ""
            return sha[:7] or None
        out = subprocess.run(  # noqa: S603
            [git, "ls-remote", "--tags", "--refs", repo_url, "v*"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode != 0:
            return None
        tags = sorted(_release_tags(out.stdout), key=_semver_key)
        return tags[-1] if tags else None
    except (OSError, subprocess.SubprocessError):
        return None


def manifest_path() -> Path | None:
    """The committed trust root, read from the RUNNING build's own tree, or None.

    Deliberately **not** fetched from the remote: a manifest downloaded from the same place
    as the thing it vouches for proves nothing. Its authority comes entirely from having
    shipped inside an artifact that is already installed and running.

    Two layouts, in order. On an install the package lives in ``<rel>/venv/lib/...`` while
    the source tree sits beside it at ``<rel>/src``, so a path relative to ``__file__``
    would point into site-packages and find nothing — the same reason ``installer_path()``
    resolves through ``current/src`` rather than ``__file__``. The second candidate is the
    dev/source checkout, where ``__file__`` IS in the repo.
    """
    for p in (
        _home() / "current" / "src" / "scripts" / "release-manifest.json",
        Path(__file__).resolve().parents[2] / "scripts" / "release-manifest.json",
    ):
        if p.exists():
            return p
    return None


def remote_key(url: str) -> str:
    """A remote's canonical manifest key: host + path, no scheme, no ``.git``, lowercased.

    Must stay identical to ``scripts/gen-release-manifest``'s copy, or every lookup misses and
    the whole trust root silently verifies nothing.
    """
    s = (url or "").strip()
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
            break
    s = s.split("@", 1)[-1]  # scp-style user@host:path
    s = s.replace(":", "/", 1) if "/" not in s.split(":", 1)[0] else s
    s = s.rstrip("/")
    if s.lower().endswith(".git"):
        s = s[: -len(".git")]
    return s.lower()


def load_manifest() -> dict[str, dict[str, dict[str, str]]]:
    """``{tag: {remote_key: {"object": sha, "commit": sha}}}`` — empty when unreadable.

    An empty mapping means "verify nothing", which is the pre-#612 behaviour. That is the
    right degradation for a *source* checkout or an old release that predates the file: it
    cannot make an install less safe than it already was, and the alternative — refusing to
    update without a manifest — would strand exactly those installs.

    **Entries are keyed by remote (schema 2), and that is a correctness requirement rather
    than a generalisation.** A release has one identity per remote that publishes it: this
    project's public mirror is a *snapshot* publish, so its tag for a given version is a
    different object, at a different commit, from the forge's tag of the same name. A schema-1
    manifest (flat ``{tag: {object, commit}}``) generated against one remote and checked
    against another reports every legitimate release as **moved** — and because the updater
    refuses on a mismatch, that is a fleet-wide auto-update outage, not a false alarm. It stayed
    latent only because a manifest lacking an entry for the *current* release degrades open,
    and the current release is the only tag an update ever targets. Found in review on #819.

    Anything that is not schema 2 is ignored, which degrades open rather than mis-comparing.
    """
    p = manifest_path()
    if p is None:
        return {}
    try:
        with p.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict) or doc.get("version") != 2:
        return {}
    rel = doc.get("releases")
    if not isinstance(rel, dict):
        return {}
    out: dict[str, dict[str, dict[str, str]]] = {}
    for tag, per_remote in rel.items():
        if not isinstance(tag, str) or not isinstance(per_remote, dict):
            continue
        entries = {
            k: v for k, v in per_remote.items() if isinstance(k, str) and isinstance(v, dict)
        }
        if entries:
            out[tag] = entries
    return out


def remote_tag_shas(tag: str, repo_url: str) -> dict[str, str]:
    """``{"object": sha, "commit": sha}`` for ``tag`` on the remote, or ``{}`` if unresolvable.

    Queried as ``<tag>*`` so the **peeled** ``refs/tags/<tag>^{}`` line comes back too: for an
    annotated tag ``refs/tags/<tag>`` names the tag object, not the commit, and the peeled
    line is the only way to see the commit without fetching. A lightweight tag has no peeled
    line, and there ``object`` already is the commit.
    """
    git = shutil.which("git")
    if not git or not tag:
        return {}
    try:
        out = subprocess.run(  # noqa: S603
            [git, "ls-remote", "--tags", repo_url, f"{tag}*"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    shas: dict[str, str] = {}
    for line in out.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if ref == f"refs/tags/{tag}":
            shas["object"] = sha.strip()
        elif ref == f"refs/tags/{tag}^{{}}":
            shas["commit"] = sha.strip()
    if "object" in shas:
        shas.setdefault("commit", shas["object"])  # lightweight tag: no peeled line
    return shas


def verify_release_tag(
    tag: str, repo_url: str, remote: dict[str, str] | None = None
) -> tuple[bool, str]:
    """``(ok, reason)`` for updating to ``tag``. See the module docstring for the threat model.

    Fails closed **only** on a genuine contradiction: the manifest knows this tag and the
    remote now points it somewhere else. Unknown tags, a missing manifest, and an
    unresolvable remote all pass — each is an absence of evidence, and turning absence into
    refusal here breaks every legitimate update rather than blocking an attack.
    """
    per_remote = load_manifest().get(tag)
    if not per_remote:
        return True, "not in manifest (newer than this build) — not verified"
    # Compare like with like. A tag's identity is per remote: the public mirror is a snapshot
    # publish, so its object and commit for a version differ from the forge's. Checking one
    # remote's record against another's tag would report every legitimate release as moved,
    # which the updater turns into a refusal — an outage, not a false alarm (#819 review).
    key = remote_key(repo_url)
    entry = per_remote.get(key)
    if not entry:
        return True, f"no trust record for remote {key!r} — not verified"
    remote = remote_tag_shas(tag, repo_url) if remote is None else remote
    if not remote:
        return True, "remote tag could not be resolved — not verified"
    for field in ("object", "commit"):
        want, got = entry.get(field), remote.get(field)
        if want and got and want != got:
            return False, (
                f"{tag} has moved: the manifest records {field} {want} but the remote now "
                f"reports {got}. Refusing to update — a released tag should never change."
            )
    return True, "verified against the release manifest"


def verified_commit(tag: str, repo_url: str, remote: dict[str, str] | None = None) -> str | None:
    """The single commit ``tag`` is allowed to resolve to, or ``None`` if nothing pins it.

    This is what closes the gap between *checking* a tag and *building* it. Verification and
    the installer's clone are two independent lookups of a mutable name, so a tag that moves
    between them passes the check and builds the moved commit anyway. Handing the installer an
    immutable SHA lets it confirm, after the clone and before any build step, that it fetched
    the object that was actually verified.

    The manifest wins when it knows the tag: it is the reviewed record of what that release
    was, whereas the remote is the thing an attacker would have rewritten. (When both exist
    and disagree, :func:`verify_release_tag` has already refused, so this never has to choose
    between two contradictory answers.) For a tag the manifest has never heard of — every
    genuinely new release, per the bootstrap problem in the module docstring — the remote's
    own answer still pins the clone to what *this* process resolved, which is strictly better
    than a bare tag name even though it is not a reviewed value.
    """
    entry = load_manifest().get(tag)
    if entry and entry.get("commit"):
        return entry["commit"]
    if remote is None:
        remote = remote_tag_shas(tag, repo_url)
    return remote.get("commit") or None


def select_stable_target(repo_url: str) -> tuple[str | None, str | None, str]:
    """``(tag, commit, reason)`` for a stable self-update — **both values or neither**.

    One remote lookup feeds every decision, because resolving a mutable name more than once
    is how a verified answer and a built answer come apart (the same defect this whole change
    exists to close). The tag and the commit it resolved to are chosen together and travel
    together.

    **Why this fails closed while the manifest check degrades open** — the two absences are
    not the same kind of absence, and treating them alike is what made the earlier version
    wrong:

    * A **missing manifest entry** is structural. A running build's manifest can never contain
      a release cut after it, so refusing there would mean no install ever auto-updates again.
      Nothing an attacker does creates that condition; it is the ordinary state of every new
      release. Degrading open is the only workable answer.
    * A **missing tag or commit lookup** is transient and *attacker-influenceable*. Someone who
      can write tags can make the lookup come back empty — delete the tag, briefly break the
      ref — and then recreate or repoint it before the installer clones. Degrading open there
      is not tolerance of missing evidence, it is an unauthenticated build triggered on demand.
      Refusing costs only a postponed update, which the next cycle retries.
    """
    tag = latest_ref("stable", repo_url)
    if not tag:
        return None, None, "no release tag could be resolved on the remote — not updating"
    remote = remote_tag_shas(tag, repo_url)
    ok, reason = verify_release_tag(tag, repo_url, remote=remote)
    if not ok:
        return None, None, reason
    commit = verified_commit(tag, repo_url, remote=remote)
    if not commit:
        return (
            None,
            None,
            (
                f"{tag} could not be resolved to a commit, so the build cannot be bound to what "
                f"was verified. Refusing to update — this is recoverable and will retry."
            ),
        )
    return tag, commit, reason


# ---------------------------------------------------------------- release signatures (#832)
SIG_OK = "ok"
SIG_UNKNOWN_SIGNER = "unknown-signer"  # validly signed by a key this install does not trust
SIG_REFUSED = "refused"
_VERIFY_TIMEOUT_S = 120
#: How far down the rotation walk may look before giving up. A bridge is normally one or two
#: releases below the newest; the bound only stops a pathological remote costing a verify per tag.
_WALK_LIMIT = 10


def verify_signature(tag: str, repo_url: str) -> tuple[str, str]:
    """``(status, reason)`` for ``tag``'s release signature, answered by the INSTALLED release's
    own ``install.sh --verify-release`` (#832 Phase 3).

    One implementation of the check, and one trust root: the signer list embedded in the
    installer this install was itself verified with. Nothing here reads a signer list from the
    remote or from the candidate, which is the attacker's to write. Fails closed: no installer,
    an installer that predates the check, a timeout or any unexpected exit is ``refused``.
    """
    inst = installer_path()
    if inst is None:
        return SIG_REFUSED, "no installer found to verify the release signature with"
    try:
        if "verify_release_only" not in inst.read_text(errors="replace"):
            return SIG_REFUSED, "the installed installer predates release signature checks"
    except OSError as e:
        return SIG_REFUSED, f"the installer could not be read ({e.strerror})"
    sh = shutil.which("sh") or "/bin/sh"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_SESSIONS_")}
    env["AGENT_SESSIONS_REPO"] = repo_url
    try:
        out = subprocess.run(  # noqa: S603 — literal argv; the tag is from the remote's tag list
            [sh, str(inst), "--verify-release", tag],
            env=env,
            capture_output=True,
            text=True,
            timeout=_VERIFY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return SIG_REFUSED, f"{tag}: the signature check could not run ({type(e).__name__})"
    reason = (out.stdout.strip().splitlines() or [f"{tag}: no answer from the signature check"])[-1]
    if out.returncode == 0:
        return SIG_OK, reason
    if out.returncode == 3:
        return SIG_UNKNOWN_SIGNER, reason
    return SIG_REFUSED, reason


def _remote_tags(repo_url: str) -> list[str]:
    """Every ``v*`` tag on the remote, highest first. Empty if the remote cannot be listed."""
    git = shutil.which("git")
    if not git:
        return []
    try:
        out = subprocess.run(  # noqa: S603
            [git, "ls-remote", "--tags", "--refs", repo_url, "v*"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return sorted(_release_tags(out.stdout), key=_semver_key, reverse=True)


def _release_key(version: str) -> tuple[int, ...]:
    """The X.Y.Z of a version or tag, for "strictly newer" — a dev/local suffix is not a
    newer release (``0.19.3.dev5+g1a2b`` is 0.19.3, never 0.19.3.5)."""
    base = version.lstrip("v").partition("+")[0]
    return tuple((_semver_key(base) + (0, 0, 0))[:3])


def select_signed_target(repo_url: str) -> tuple[str | None, str | None, str]:
    """``(tag, commit, reason)`` for a stable self-update: the manifest checks of
    :func:`select_stable_target` **and** a release signature (#832).

    **The rotation walk (Phase 4).** When the newest tag is validly signed by a key this
    install does not trust yet, that is what a key rotation looks like to an install that was
    off across it: the bridge release — signed by the old key, carrying both — sits below the
    newest. So step down to the newest release this install *can* verify, install that, and
    let the next cycle re-evaluate with the bridge's wider root.

    Bounded three ways, because "walk down until something verifies" is otherwise a downgrade
    primitive:

    * only an **unknown signer** is steppable. A missing, invalid or tampered signature — or
      any answer the check does not recognise — is an attack, and refuses outright;
    * every candidate is **strictly newer** than the running version, so the walk can never
      reinstall what is running (a loop) or go backwards;
    * no verifiable candidate ⇒ a hard refusal that says so, never a guess.
    """
    tag, commit, reason = select_stable_target(repo_url)
    if not tag or not commit:
        return None, None, reason
    status, why = verify_signature(tag, repo_url)
    if status == SIG_OK:
        return tag, commit, why
    if status != SIG_UNKNOWN_SIGNER:
        return None, None, f"refusing to update: {why}"
    running = _release_key(get_version())
    candidates = [t for t in _remote_tags(repo_url) if t != tag and _release_key(t) > running][
        :_WALK_LIMIT
    ]
    for cand in candidates:
        c_status, c_why = verify_signature(cand, repo_url)
        if c_status == SIG_UNKNOWN_SIGNER:
            continue
        if c_status != SIG_OK:
            return None, None, f"refusing to update: {c_why}"
        remote = remote_tag_shas(cand, repo_url)
        ok, m_why = verify_release_tag(cand, repo_url, remote=remote)
        if not ok:
            return None, None, m_why
        pin = verified_commit(cand, repo_url, remote=remote)
        if not pin:
            return None, None, f"{cand} could not be resolved to a commit — will retry"
        return (
            cand,
            pin,
            (
                f"{tag} is signed by a key this installation does not trust yet; stepping to "
                f"{cand}, the newest release it can verify (a key-rotation bridge)"
            ),
        )
    return (
        None,
        None,
        (
            f"refusing to update: {why}, and no newer release this installation can verify exists "
            f"(a key rotation without a bridge release). Reinstall from the current installer to "
            f"pick up the new release key."
        ),
    )


def _semver_key(tag: str) -> tuple[int, ...]:
    parts = tag.lstrip("v").split(".")
    out = []
    for p in parts:
        num = "".join(c for c in p if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def _running_sha(version: str) -> str | None:
    """The commit the running build was built from, parsed from the version's local
    segment: setuptools_scm's ``+g<sha>`` (e.g. ``0.9.1.dev3+g64eefb3``) or the dev
    placeholder ``0.0.0+<sha>``. Returns lowercase hex, or None for a **clean release**
    version that carries no local segment (``0.9.0``) — nothing to compare a SHA against."""
    local = version.partition("+")[2]
    if not local:
        return None
    token = local.split(".", 1)[0]  # drop .dirty / .dYYYYMMDD suffixes
    if token[:1] == "g":  # setuptools_scm prefixes the git SHA with 'g'
        token = token[1:]
    token = token.lower()
    return token if token and all(c in "0123456789abcdef" for c in token) else None


MAIN_AVAILABLE = "available"
MAIN_CURRENT = "up-to-date"
MAIN_UNDETERMINED = "undetermined"


def _tag_commit(cur: str, repo_url: str) -> str | None:
    """The commit the running build's own release tag points at, or None if unresolvable.

    This is the comparand a clean release version cannot supply itself: ``0.19.2`` carries no
    ``+g<sha>``, so the only way to place it on main's history is to ask the remote what
    ``v0.19.2`` points at. Goes through :func:`remote_tag_shas` rather than a second resolver
    because that one already peels an **annotated** tag (``refs/tags/<t>^{}``) *and* falls
    back to ``object`` for a **lightweight** one — a bare ``^{}`` query would silently miss
    every lightweight tag, which is the shape the public mirror uses (#832)."""
    base = cur.partition("+")[0]  # drop any local segment; the tag names the public version
    if not base:
        return None
    return remote_tag_shas(f"v{base}", repo_url).get("commit")


def _main_update_verdict(cur: str, latest: str | None, repo_url: str) -> str:
    """`main`-channel verdict (#583, #931) — one of the three ``MAIN_*`` constants.

    Compare the running build's commit SHA to the remote HEAD SHA — **never** the SHA to the
    whole version string, which reported "update available" forever whenever main HEAD sat on
    a release tag (a clean ``0.9.0`` never *contains* the SHA, so the old ``latest not in cur``
    heuristic was always true → a reinstall loop).

    A clean release version has no SHA in it, and #583 answered that by calling it current.
    That is wrong on this channel and #931 is what it cost: an install on ``main`` that lands
    on a tag reads as up-to-date against *every* future commit, forever, with the UI's only
    escape hatch hidden behind the same verdict. So resolve the tag to its commit and compare
    SHA to SHA as intended.

    **Undetermined is its own answer, and it is not "up-to-date".** Either side can fail on
    its own — the HEAD lookup and the tag lookup are separate network calls — and a comparison
    that never happened must not render as reassurance. It still never triggers an update:
    callers map it to ``update_available: false``, so failing to tell restores today's
    behaviour rather than reinstating #583's reinstall loop."""
    if not latest:
        return MAIN_UNDETERMINED  # no remote HEAD: nothing was compared
    cur_sha = _running_sha(cur) or _tag_commit(cur, repo_url)
    if not cur_sha:
        return MAIN_UNDETERMINED  # fail closed — never reinstall on a guess
    n = min(len(cur_sha), len(latest))
    if cur_sha.lower()[:n] == latest.lower()[:n]:  # tolerant of differing short lengths
        return MAIN_CURRENT
    return MAIN_AVAILABLE


def check_blocked() -> str | None:
    """Why the last update was refused by release verification, or None."""
    return _LAST_BLOCK


def check() -> dict[str, object]:
    cur = get_version()
    channel = _channel()
    latest = latest_ref(channel, _repo_url())
    verdict = ""
    if channel == "main":
        verdict = _main_update_verdict(cur, latest, _repo_url())
        available = verdict == MAIN_AVAILABLE
    else:
        norm = latest.lstrip("v") if latest else latest
        available = bool(latest) and norm != cur
    info: dict[str, object] = {
        "current": cur,
        "channel": channel,
        "latest": latest,
        "update_available": available,
    }
    if verdict == MAIN_UNDETERMINED:
        # Carried to the UI so "we could not tell" cannot render as "you're on the latest"
        # (#931). Optional, like ``blocked`` below — absent means the comparison happened.
        info["undetermined"] = True
    if _LAST_BLOCK:
        info["blocked"] = _LAST_BLOCK
    return info


# ---------------------------------------------------------------- update progress (#1085)
#
# The installer runs detached with its output discarded, so for the minutes a build takes the
# Updates page could say nothing but "Updating…". `apply()` now names a progress file for it
# (`AGENT_SESSIONS_UPDATE_PROGRESS`) and install.sh writes one small JSON record per milestone.
#
# The record is INPUT, and is read as such: a fixed path under the install prefix (never a
# client-supplied one), a size cap, a JSON object, an enum state, a step id from the table
# below, and plain integers. Display text never comes from the file — the labels are ours, so
# nothing the installer (or anything else that could write that file) prints reaches the page.
#
# It also has to survive what the update itself does: the app restarts part-way through, so
# the record lives on disk, not in this process, and the new process reads the same file.

PROGRESS_STEPS: tuple[tuple[str, str], ...] = (
    ("prepare", "Checking prerequisites"),
    ("fetch", "Downloading the release"),
    ("python", "Installing the Python package"),
    ("web", "Building the web UI"),
    ("switch", "Switching to the new release"),
    ("restart", "Restarting the service"),
    ("health", "Checking it came back"),
)
_STEP_IDS = [sid for sid, _ in PROGRESS_STEPS]
_PROGRESS_STATES = ("running", "done", "failed", "rolled_back")
_PROGRESS_MAX_BYTES = 4096
#: A `running` record older than this with no newer write is not believed any more.
PROGRESS_STALE_S = 45 * 60
#: The seed is written before the spawn; an installer that never overwrote it never started.
_SEED_GRACE_S = 120


def progress_path() -> Path:
    return _home() / "update-progress.json"


def _history_path() -> Path:
    return _home() / "update-history.json"


def _write_json_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def _seed_progress(started_at: int) -> None:
    """The record before the installer's first write, so a page that opens in the gap already
    sees a run. Best-effort: a prefix we cannot write to must not stop the update itself."""
    try:
        _home().mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            progress_path(),
            {"state": "running", "step": "", "pid": 0, "started_at": started_at, "at": started_at},
        )
    except OSError as e:
        log.warning("update progress: could not seed the record (%s)", type(e).__name__)


def _pid_alive(pid: int) -> bool:
    """Is the installer still RUNNING — not merely present in the process table?

    Signal 0 succeeds for a zombie, and the installer is this app's own child while the app lives,
    so an installer that exited through ``set -e`` without reaching ``die`` read as running until
    the stale window (Hermes on #1089).

    **Reaped only through its own handle.** ``apply()`` keeps the ``Popen`` in ``_INSTALLER``, and
    ``poll()`` on it reaps exactly that child. A bare ``waitpid(pid)`` would be wrong twice over: a
    PID from a stale record can by now belong to ANOTHER child of this app (an agent launch, a
    usage probe) whose exit status it would steal, and reaping behind ``Popen``'s back leaves the
    object to ``waitpid`` a reused PID later. Any other PID — the installer of a previous app
    process, now someone else's child — is only looked at: signal 0, then the zombie state in
    ``/proc/<pid>/stat``."""
    proc = _INSTALLER
    if proc is not None and getattr(proc, "pid", None) == pid:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True  # no /proc (or it vanished between the calls): signal 0 said it exists
    # The state is the first field after the parenthesised command, which may contain spaces.
    return stat.rsplit(")", 1)[-1].split()[:1] != ["Z"]


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _read_record(path: Path) -> dict | None:
    try:
        with path.open("rb") as f:
            raw = f.read(_PROGRESS_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _PROGRESS_MAX_BYTES:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _last_duration() -> int | None:
    rec = _read_record(_history_path())
    return _int(rec.get("duration_s")) if rec else None


def _remember_duration(started_at: int, duration: int) -> None:
    """Keep the last completed update's duration for "last update took X" — written once per
    run (keyed on its start), by whichever process first reads the finished record."""
    rec = _read_record(_history_path()) or {}
    if rec.get("started_at") == started_at:
        return
    try:
        _write_json_atomic(_history_path(), {"started_at": started_at, "duration_s": duration})
    except OSError as e:
        log.warning("update progress: could not record the duration (%s)", type(e).__name__)


def progress(now: float | None = None) -> dict[str, object]:
    """The current (or last) self-update, validated. ``state`` is one of ``idle`` (no record),
    ``running``, ``done``, ``failed``, ``rolled_back`` or ``stale``."""
    now = time.time() if now is None else now
    out: dict[str, object] = {
        "state": "idle",
        "steps": len(PROGRESS_STEPS),
        "last_duration_s": _last_duration(),
    }
    rec = _read_record(progress_path())
    if rec is None:
        return out
    state = rec.get("state")
    step = rec.get("step")
    started = _int(rec.get("started_at"))
    at = _int(rec.get("at"))
    pid = _int(rec.get("pid"))
    if state not in _PROGRESS_STATES or started is None or at is None or pid is None:
        return out
    if not isinstance(step, str) or (step and step not in _STEP_IDS):
        return out
    if state == "running":
        if pid == 0:
            # Still the seed: the installer has not written yet. Past the grace, it never will.
            if now - at > _SEED_GRACE_S:
                state = "failed"
        elif not _pid_alive(pid):
            # Exited without writing a terminal record — `set -e` bypasses `die`.
            state = "failed"
        elif now - at > PROGRESS_STALE_S:
            state = "stale"
    index = _STEP_IDS.index(step) + 1 if step else 0
    end = at if state in ("done", "failed", "rolled_back") else now
    elapsed = max(0, int(end - started)) if started else None
    if state == "done" and started and elapsed is not None:
        _remember_duration(started, elapsed)
    out.update(
        {
            "state": state,
            "step": step,
            "step_index": index,
            "label": PROGRESS_STEPS[index - 1][1] if index else "Starting",
            "started_at": started or None,
            "elapsed_s": elapsed,
            "last_duration_s": _last_duration(),
        }
    )
    return out


def installer_path() -> Path | None:
    p = _home() / "current" / "src" / "install.sh"
    return p if p.exists() else None


def apply() -> bool:
    """Run the installer detached to upgrade to the channel's latest (no user input).
    Returns False if the installer isn't found (e.g. a dev checkout, not an install), or if
    the target release tag fails manifest verification (#612) or its signature check (#832)."""
    global _SPAWNED_AT, _LAST_BLOCK
    inst = installer_path()
    if inst is None:
        return False
    # Verify BEFORE spawning, because after the spawn we have no say: the installer resolves
    # the ref itself and this process is about to be restarted by it. `stable` only — `main`
    # tracks a branch by design and has no tag to verify.
    target: str | None = None
    pin: str | None = None
    if _channel() == "stable":
        target, pin, reason = select_signed_target(_repo_url())
        # Both or neither: an installer spawned without a commit to check against is an
        # unverified build, because install.sh deliberately skips the comparison when
        # AGENT_SESSIONS_EXPECT_COMMIT is empty. Refusing here is cheap and self-correcting.
        if not target or not pin:
            _LAST_BLOCK = reason
            log.error("update refused: %s", reason)
            return False
        _LAST_BLOCK = None
    sh = shutil.which("sh") or "/bin/sh"
    env = {**os.environ, "AGENT_SESSIONS_REPO": _repo_url(), "AGENT_SESSIONS_CHANNEL": _channel()}
    # Self-update always moves to the CHANNEL's latest, so an *inherited* AGENT_SESSIONS_REF
    # (which the installer would otherwise prefer) must never survive into the child.
    env.pop("AGENT_SESSIONS_REF", None)
    env.pop("AGENT_SESSIONS_EXPECT_COMMIT", None)
    # ...but the ref this process just resolved and verified is exactly what the child must
    # build, so it is passed forward deliberately. Without it the installer runs its own
    # `ls-remote` and resolves the highest tag a second time, and everything checked above
    # describes a lookup the build never used: a tag that moved in between, or a higher tag
    # published in between, is picked up unverified. Pinning the NAME closes the second case;
    # pinning the immutable COMMIT closes the first, because a moved tag then clones an object
    # the installer can see is not the one that was verified, and it refuses before building.
    if target and pin:
        env["AGENT_SESSIONS_REF"] = target
        env["AGENT_SESSIONS_EXPECT_COMMIT"] = pin
    # Progress (#1085): a fixed path under the prefix, and the start time the record carries.
    started = int(time.time())
    _seed_progress(started)
    env["AGENT_SESSIONS_UPDATE_PROGRESS"] = str(progress_path())
    env["AGENT_SESSIONS_UPDATE_STARTED"] = str(started)
    global _INSTALLER
    _INSTALLER = subprocess.Popen(  # noqa: S603
        [sh, str(inst)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # survive the service restart the installer triggers
    )
    _SPAWNED_AT = time.monotonic()
    return True


def apply_manual() -> str:
    """The manual "Update now" path: single-flight with the scheduled loop AND with a
    just-spawned installer. `apply()` returns right after the detached spawn while the
    installer keeps working through its build/restart window — without the cooldown a
    double-click or retried POST would stack a second installer (Hermes #539).
    Returns 'started' | 'busy' | 'blocked' | 'unavailable'."""
    if not _RUN_LOCK.acquire(blocking=False):
        return "busy"
    try:
        if _SPAWNED_AT is not None and time.monotonic() - _SPAWNED_AT < _SPAWN_COOLDOWN_S:
            return "busy"
        return "started" if apply() else _not_started()
    finally:
        _RUN_LOCK.release()


def _not_started() -> str:
    """Why `apply()` returned False: ``blocked`` when release verification refused (the reason
    is on `check()`'s ``blocked``), ``unavailable`` when there is no installer to run. Keeping
    them apart matters: a refused update must not read as "self-update isn't available"."""
    return "blocked" if installer_path() is not None and _LAST_BLOCK else "unavailable"


def autoupdate() -> str:
    """Check the channel and apply only if an update is available (the scheduled/CLI
    entrypoint). Returns 'up-to-date', 'applied', 'blocked' (release verification refused),
    'unavailable', or 'busy' (another
    check/apply holds the single-flight lock, or an installer spawned moments ago is
    still working through its restart window)."""
    if not _RUN_LOCK.acquire(blocking=False):
        return "busy"
    try:
        if _SPAWNED_AT is not None and time.monotonic() - _SPAWNED_AT < _SPAWN_COOLDOWN_S:
            return "busy"
        info = check()
        if not info["update_available"]:
            return "up-to-date"
        return "applied" if apply() else _not_started()
    finally:
        _RUN_LOCK.release()
