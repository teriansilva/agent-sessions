"""Engine registry + the engine-qualified-id key functions (split out of the single-file
``engines.py``, #265 S1).

A small registry merges the **present** providers so ``/api/sessions`` is engine-agnostic,
and ``parse_key`` is the single gate that resolves an id to its provider and validates the
native shape before any dispatch. See the package ``__init__`` docstring for the identity model.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..chat_store import ChatStoreKind
from ..scanner import Session
from . import base
from .antigravity import AntigravityProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .kimi import KimiProvider
from .opencode import OpenCodeProvider
from .shell import ShellProvider

log = logging.getLogger("agent_sessions.engines")

#: `store.layout` → the in-tree store kind that implements it (#853 P2). A kind is behaviour only —
#: scanning, lookup, reconcile, the unattended hooks. Identity, argv, capabilities and the binary
#: come from the engine's manifest (`plugins/first_party/<id>/plugin.toml`).
STORE_KINDS: dict[str, type] = {
    "claude-projects": ClaudeProvider,
    "opencode-sqlite": OpenCodeProvider,
    "codex-rollouts": CodexProvider,
    "gemini-tmp": GeminiProvider,
    "antigravity-cli": AntigravityProvider,
    "kimi-code": KimiProvider,
    "shell-records": ShellProvider,
    "battlelab-chat": ChatStoreKind,
}


#: Why a manifest did not load, by loader key — surfaced read-only on `/api/engines` (#853 P4) so
#: a broken plugin is visible on the Agents page instead of only in the journal. Never executable.
LOAD_PROBLEMS: dict[str, str] = {}


def _build_roster(first_party_dir: Path | None = None) -> list[base.EngineProvider]:
    """The live roster: every IN-TREE manifest, in `display.order` (#853 P2).

    Local manifests never reach this list — `load_first_party()` does not read them — until install
    and operator confirmation exist (P5/P6). An in-tree manifest that fails to load is a build
    defect, not an operator error, so it is logged loudly and its engine is left out rather than
    taking the whole roster down (the fail-soft rule every store reader already follows).
    """
    from ..plugins import load_first_party

    loaded = load_first_party(first_party_dir=first_party_dir)
    LOAD_PROBLEMS.clear()
    LOAD_PROBLEMS.update(loaded.problems)
    for key, why in loaded.problems.items():
        log.error("engine manifest %s did not load: %s", key, why)
    roster: list[base.EngineProvider] = []
    if not loaded.providers:
        # Never a silent empty sidebar: with no manifests every id fails `parse_key` and every
        # session 404s, so this is logged at the loudest level the app has.
        log.critical("no engine manifests loaded — the roster is EMPTY (%s)", loaded.problems)
    for prov in loaded.providers.values():
        layout = prov.manifest.store.layout if prov.manifest.store else None
        kind = STORE_KINDS.get(layout)
        if kind is None:
            log.error(
                "engine manifest %s names store layout %r with no kind", prov.engine_id, layout
            )
            continue
        roster.append(prov.attach_kind(kind()))
    return roster


# Order is scan/display order (`display.order` in each manifest); a provider only surfaces when
# present. Shell is last — the agent engines lead, and the always-present plain terminal (#636)
# trails them.
_PROVIDERS: list[base.EngineProvider] = _build_roster()
_BY_ID: dict[str, base.EngineProvider] = {p.engine_id: p for p in _PROVIDERS}
#: The one engine whose ids may arrive without an `engine:` prefix (pre-multi-engine bookmarks).
_BARE_ID_ENGINE: str | None = next(
    (p.engine_id for p in _PROVIDERS if p.manifest.session_id.legacy_bare_id), None
)


#: Callbacks run after every `reload` — for the few consumers that keep a VIEW of the roster
#: (`agent_usage.REPORTERS`, its panel list). Registering one is how a consumer stays conformant.
_RELOAD_LISTENERS: list[Callable[[], None]] = []


def on_reload(fn: Callable[[], None]) -> Callable[[], None]:
    _RELOAD_LISTENERS.append(fn)
    return fn


def reload(first_party_dir: Path | None = None) -> None:
    """Rebuild the roster from the manifests — the RELOAD BOUNDARY of #853 (P3: app start; P6
    adds an explicit action). Never mid-request: callers are app start and tests.

    ``first_party_dir`` is the loader's existing test seam; the live app never passes it, so no
    runtime path can load a manifest from anywhere but the in-tree `plugins/first_party/`.
    """
    global _PROVIDERS, _BY_ID, _BARE_ID_ENGINE
    roster = _build_roster(first_party_dir)
    _PROVIDERS = roster
    _BY_ID = {p.engine_id: p for p in roster}
    _BARE_ID_ENGINE = next(
        (p.engine_id for p in roster if p.manifest.session_id.legacy_bare_id), None
    )
    apply_retirement()
    invalidate_scan_cache()
    for fn in list(_RELOAD_LISTENERS):
        fn()


# --- retirement (#853 P3) -------------------------------------------------------------------------
#
# An engine whose manifest is gone at a reload is not simply forgotten:
#
# * with a live `dtach` master left it RETIRES — rebuilt from the recorded manifest copy
#   (`plugins.roster_state`) so `parse_key` still validates its ids and ATTACH still works, but it
#   is in no listing, has no entrypoint, and every new-work path refuses it with "agent removed";
# * with none left it is REMOVED — its copy is dropped and its id tombstoned, so the refusal still
#   says "agent removed" rather than "unknown engine" until a manifest with that id loads again.
#
# Nothing stored is touched: sessions stay on disk and in the sidecar, budgets stay in prefs.

REMOVED_REASON = "agent removed"
_RETIRING: dict[str, base.EngineProvider] = {}
_REMOVED: set[str] = set()
#: What the last retirement pass refused in the recorded state — reported, never acted on.
RETIREMENT_PROBLEMS: list[str] = []


def _engines_with_masters(candidates=()) -> set[str] | None:
    """Engine ids with a dtach socket whose master is NOT provably dead — or ``None`` when the
    runtime directory could not be listed at all.

    ``None`` is UNKNOWN, never "no masters" (Hermes on PR #1132): a transient permission or I/O
    error is not evidence that any session ended, and reading it as such would delete the only
    recorded manifest of an engine whose sessions are still running.

    Sockets are matched against the CANDIDATE engine ids (the recorded ones) with the same
    sanitising `ptybridge.socket_path` applies, and a socket counts for EVERY candidate it could
    belong to. A file name alone cannot prove ownership: `my-agent-abc123.sock` is both
    `my-agent:abc123` and `my:agent-abc123`. Picking one owner — the first hyphen (independent
    review of #1132) or the longest prefix (Hermes on #1132) — would call the other dead and delete
    its only recorded manifest while its session runs. Ambiguity is uncertainty, and uncertainty
    keeps an engine reachable.

    Verdict-aware on purpose (#355): a starved master can time out a probe, and UNKNOWN read as
    dead would end an engine's retirement while its session is still running — stranding exactly
    the terminal this whole mechanism exists to keep reachable. Only DEAD counts as gone.
    """
    from .. import ptybridge

    prefixes = [(ptybridge._UNSAFE.sub("_", e) + "-", e) for e in candidates]
    out: set[str] = set()
    try:
        entries = list(ptybridge.runtime_dir().iterdir())
    except OSError:
        return None
    for sock in entries:
        if sock.suffix != ".sock":
            continue
        owners = {e for pre, e in prefixes if sock.name.startswith(pre)} - out
        if not owners:
            continue
        try:
            if ptybridge.probe_master(sock) != ptybridge.DEAD:
                out |= owners
        except OSError:
            out |= owners  # cannot tell → keep them reachable
    return out


def _retiring_provider(m) -> base.EngineProvider:
    from ..plugins import plugins_home, provenance
    from ..plugins.provider import PluginProvider

    prov = PluginProvider(m, trust=provenance.FIRST_PARTY, root=plugins_home() / m.id)
    prov.retiring = True
    # The store kind (when this build still has it) answers `lookup` ONLY, so ATTACH is authorized
    # against the session's real cwd exactly as for an active engine. It is never scanned.
    kind = STORE_KINDS.get(m.store.layout) if m.store is not None else None
    if kind is not None:
        try:
            prov.attach_kind(kind())
        except ValueError:
            log.warning("retiring engine %s: store kind does not match; no lookup", m.id)
    return prov


def apply_retirement() -> None:
    """Reconcile the loaded roster with the recorded one (the reload boundary)."""
    global _RETIRING, _REMOVED, _BARE_ID_ENGINE
    from ..plugins import roster_state

    problems: list[str] = []
    state = roster_state.load()
    problems += state.problems
    active = set(_BY_ID)
    live = _engines_with_masters(state.manifests)
    if live is None:
        # Unknown is not dead: every recorded engine stays reachable and every copy is kept.
        problems.append(
            "the session runtime directory could not be listed; every removed engine is kept "
            "retiring and no recorded manifest was dropped"
        )
        live = set(state.manifests)
    retiring: dict[str, base.EngineProvider] = {}
    removed = set(state.removed) - active
    for eid, (name, raw) in list(state.manifests.items()):
        if eid in active:
            continue
        if eid in live:
            try:
                retiring[eid] = _retiring_provider(roster_state.parse_copy(eid, name, raw))
                continue
            except Exception as e:  # noqa: BLE001 — a bad copy is reported, never trusted
                problems.append(f"the recorded manifest for {eid!r} did not load ({e})")
                # Kept on disk (a later build may read it) and its masters stay unreachable — but
                # new work aimed at it still says "agent removed", never "unknown engine".
                removed.add(eid)
                continue
        del state.manifests[eid]
        removed.add(eid)
    for p in _PROVIDERS:
        rec = roster_state.manifest_bytes(p.manifest)
        if rec is not None:
            state.manifests[p.engine_id] = rec
    state.removed = removed
    if state.intact:
        try:
            roster_state.save(state)
        except OSError as e:
            problems.append(f"the roster state could not be saved ({e.strerror})")
    else:
        # A predecessor we could not read is never overwritten (Hermes on PR #1132): it may hold
        # the only copy of a retiring engine. Nothing from it is trusted this time; the next
        # reload reads it again.
        problems.append("the recorded roster was not saved over, because it could not be read")
    _RETIRING = retiring
    _REMOVED = removed
    # Legacy bare-id bookmarks keep parsing while their engine retires (they name live sessions
    # too); an ACTIVE claimant always wins.
    if _BARE_ID_ENGINE is None or _BARE_ID_ENGINE not in _BY_ID:
        _BARE_ID_ENGINE = next(
            (e for e, p in retiring.items() if p.manifest.session_id.legacy_bare_id),
            next((p.engine_id for p in _PROVIDERS if p.manifest.session_id.legacy_bare_id), None),
        )
    RETIREMENT_PROBLEMS[:] = problems
    for why in problems:
        log.error("engine retirement: %s", why)
    for eid in retiring:
        log.warning("engine %s was removed; its live sessions stay attachable (retiring)", eid)


def get_any(engine_id: str) -> base.EngineProvider | None:
    """An ACTIVE or RETIRING provider. For paths about an EXISTING session — parsing, resolving,
    tearing down, pruning its socket — which must keep working while the engine retires. Every
    new-work path (launch, handoff target, dispatch, listings) uses `get`, which is active only."""
    return _BY_ID.get(engine_id) or _RETIRING.get(engine_id)


def is_retiring(ref) -> bool:
    return bool(getattr(ref, "retiring", False)) if not isinstance(ref, str) else ref in _RETIRING


def retiring_providers() -> list[base.EngineProvider]:
    return list(_RETIRING.values())


def removed_reason(engine_id: str) -> str | None:
    """ "agent removed" for a retiring or tombstoned engine, else None. What every new-work path
    says instead of "unknown engine" when the engine used to exist."""
    if engine_id in _RETIRING or (engine_id in _REMOVED and engine_id not in _BY_ID):
        return REMOVED_REASON
    return None


def launchable_bin(prov: base.EngineProvider | None) -> str | None:
    """The binary a launch of this engine would exec, or None when it cannot launch.

    The ONE answer the UI and the launcher share (#853 P2): Settings, the new-session picker and
    the handoff check ask the provider — provenance, never a PATH lookup — so an engine is never
    offered that the launcher would then refuse. It walks directories, so call it OFF the loop.
    """
    if prov is None:
        return None
    try:
        return prov.entrypoint_path()
    except base.EngineError:
        return None


def can_start(prov: base.EngineProvider | None) -> bool:
    """Can this engine start a new session RIGHT NOW — the one answer the new-session picker and
    `/api/engines` share (#853 P2; `chat` #1209). A terminal engine needs a launchable binary; a
    `chat` engine needs its endpoint configured (it runs no binary). Call it OFF the loop."""
    if prov is None or not getattr(prov, "supports_new", False) or is_retiring(prov):
        return False
    m = getattr(prov, "manifest", None)
    if m is not None and m.runtime == "chat":
        from .. import chat_config

        return chat_config.is_configured(prov.engine_id)
    return launchable_bin(prov) is not None


def all_providers() -> list[base.EngineProvider]:
    return list(_PROVIDERS)


def present_providers() -> list[base.EngineProvider]:
    """Providers usable on this host (binary and/or data store present)."""
    return [p for p in _PROVIDERS if p.is_present()]


def get(engine_id: str) -> base.EngineProvider | None:
    return _BY_ID.get(engine_id)


# --- the manifest, asked (#853 P3) ---------------------------------------------------------------
#
# Every consumer that used to name an engine asks one of these instead. They take an engine id, a
# session key (`<engine>:<native>`) or a provider, so a caller holding any of the three never has
# to parse or look anything up itself. An unknown engine answers like an engine that declares
# nothing: default-deny, never an exception on a hot path.


def manifest_of(ref):
    """The loaded manifest for an engine id, a session key or a provider — or None."""
    if ref is None:
        return None
    if isinstance(ref, str):
        ref = get_any(ref.split(":", 1)[0])
    return getattr(ref, "manifest", None)


def engine_ids() -> list[str]:
    """The roster, in display order. The ONE list of engines; nothing else keeps its own."""
    return [p.engine_id for p in _PROVIDERS]


def ids_where(pred: Callable) -> list[str]:
    """Engine ids (roster order) whose manifest satisfies ``pred``."""
    return [p.engine_id for p in _PROVIDERS if pred(p.manifest)]


def is_agent(ref) -> bool:
    """Is there an agent behind this engine? `identity.kind == "terminal"` (the plain shell, #636)
    has none — nothing may be handed off from or to it, and no input may be authored for it."""
    m = manifest_of(ref)
    return m is not None and m.identity.kind == "agent"


def owns_transcript(ref) -> bool:
    """`capabilities.owns_transcript`: another process of this engine may be writing the SAME
    transcript (a claude background-agent fork), so archive/launch must ask `transcript_owner`."""
    m = manifest_of(ref)
    return m is not None and m.can("owns_transcript")


def is_pty(ref) -> bool:
    """Does a session of this engine run as a binary under `dtach` (#853 §7)?"""
    m = manifest_of(ref)
    return m is not None and m.runtime == "pty"


def require_pty(ref) -> None:
    """The shared terminal-runtime guard. Every terminal-only path (launch, attach, scrollback
    repaint, ready rule, menus, raw-TTY repair, admission, unattended start) calls this rather
    than assuming a terminal exists — an engine of another runtime is refused, never treated as
    one."""
    m = manifest_of(ref)
    if m is None:
        raise base.EngineError("unknown engine")
    if m.runtime != "pty":
        raise base.EngineError(f"{m.id} does not run in a terminal (runtime {m.runtime!r})")


def terminal_of(ref):
    """The manifest's `terminal` block, or None for an unknown or non-`pty` engine — so a
    terminal-only consumer that asks gets "nothing special", never another runtime's settings."""
    m = manifest_of(ref)
    return m.terminal if m is not None and m.runtime == "pty" else None


def store_for_engine(engine_id: str, name: str | None = None, home: Path | None = None) -> Path:
    """``engine_id``'s OWN store (or named path), from its manifest. Raises `EngineError` when it
    declares none — a reader must then find nothing, never fall back to another engine's store."""
    import os

    from ..plugins.provider import store_location

    m = manifest_of(engine_id)
    got = (
        store_location(m, home=home or Path.home(), env=os.environ, name=name)
        if m is not None
        else None
    )
    if got is None:
        raise base.EngineError(f"{engine_id} declares no store path {name!r}")
    return got


def store_for_layout(layout: str, name: str | None = None, home: Path | None = None) -> Path:
    """The store (or its named path) of the engine whose manifest selects ``layout``.

    A store kind and its transcript adapter are shaped by one LAYOUT, not by one engine id, so
    that is what they ask by. Resolved at call time against the live environment, so an env
    override set by a test or the operator is honoured exactly as the provider honours it.
    """
    import os

    from ..plugins.provider import store_location

    # Retiring engines too (Hermes on PR #1132): their store kinds still answer `lookup` for a
    # live session's ATTACH authorization. An active engine of the same layout comes first.
    for p in [*_PROVIDERS, *_RETIRING.values()]:
        m = p.manifest
        if m.store is not None and m.store.layout == layout:
            got = store_location(m, home=home or Path.home(), env=os.environ, name=name)
            if got is not None:
                return got
            raise base.EngineError(f"store layout {layout!r} declares no path {name!r}")
    raise base.EngineError(f"no loaded engine uses store layout {layout!r}")


def supports_orchestrator_input(prov: base.EngineProvider | None) -> bool:
    """May the Pulse orchestrator write server-authored input into this engine's sessions?
    (#726)

    **Default-deny.** A provider that does not declare ``supports_orchestrator_input = True``
    is not actuable. That default is the point, not an implementation detail: ``shell`` is a
    bare ``bash -l`` with no agent behind it (#636), so a "continue" nudge typed into one would
    be *executed as a shell command* — and ``parse_key`` cannot catch that, because a
    ``shell:<uuid>`` key is perfectly well-formed. If the flag defaulted on, the next agentless
    engine added to the registry would silently re-open that hole. The failure mode has to be
    "a new engine can't be driven until someone says it can", never the reverse.

    This gates the *delivery* boundary. ``dispatch`` (starting a NEW session and seeding it) is a
    different question with an existing answer — ``handoff.seed_start_state`` — and must use that
    rather than this, so the orchestrator's spawn targets can never drift from the handoff
    picker's.
    """
    return bool(getattr(prov, "supports_orchestrator_input", False))


def expects_raw_tty(prov: base.EngineProvider | None) -> bool:
    """Does this engine run a TUI that puts its PTY into raw mode and keeps it there? (#804)

    **Default-deny**, for the same reason as ``supports_orchestrator_input`` above. This flag
    authorises writing termios to a live session's terminal, and the one engine that must never
    be written to looks exactly like the ones that must — ``shell`` (#636) is a bare ``bash -l``
    whose terminal is cooked *by design* between commands, so "repairing" it would break the
    operator's own line editing. A flag that defaulted on would silently include the next
    agentless engine somebody adds. The failure mode has to be "a new engine's PTY is left
    alone until someone says it's a raw TUI", never the reverse.

    Separate from ``supports_orchestrator_input`` on purpose: that one asks whether the app may
    author *content* for an engine, this one asks what its terminal is supposed to look like.
    They agree today and would be tempting to collapse — but an engine could accept orchestrator
    input without running a raw TUI, and overloading one flag for both would make that engine
    unrepresentable.
    """
    return bool(getattr(prov, "expects_raw_tty", False))


def orchestrator_input_engines() -> set[str]:
    """Engine ids the orchestrator may write to — the default-deny set above, resolved once so
    callers can filter a card list without touching providers per row."""
    return {p.engine_id for p in _PROVIDERS if supports_orchestrator_input(p)}


def scan_all() -> list[Session]:
    """Every session from every PRESENT provider, merged — the ordinary listing.

    **Deliberately independent of** :func:`scan_all_checked`, and it must stay that way. The
    display path is fail-soft per provider and per record by design (a repo invariant, not a
    preference): one unreadable store or one malformed record must never take the sidebar down.
    Routing this through the checked scan to share one code path did exactly that — a single
    corrupt ``shell`` record hid every healthy shell session from ordinary listings (Hermes on
    PR #1000, review 4898). Maintenance strictness belongs only on the maintenance path.
    """
    from .. import perfstats

    out: list[Session] = []
    # One sample per PROVIDER as well as one for the whole walk (#1048): "the scan is slow" is not
    # actionable, "codex is 2.4 s of a 3.7 s walk" is. Recording is best-effort by construction and
    # never raises into the walk.
    with perfstats.timed("scan_all_ms"):
        for p in present_providers():
            with perfstats.timed(f"scan_{p.engine_id}_ms"):
                out.extend(p.scan())
    return out


def scan_all_checked() -> tuple[list[Session], list[str]]:
    """``(sessions, problems)`` — the rows AND the evidence that they are COMPLETE, from ONE pass.

    Both must come from the same pass. Asking a separate probe afterwards lets a store that failed
    the first read and recovered before the second answer "empty, and nothing went wrong": the rows
    and their completeness would then describe different moments (Hermes on PR #1000, review 4894).

    **Presence is not consulted for a provider that can report its own read failures.**
    ``is_present()`` is itself a READ — opencode's needs a launchable CLI or a readable DB — so a
    corrupt store answers "absent", and a presence filter would quietly turn *unreadable* into
    *nothing to see*. A provider exposing ``scan_checked()`` is therefore always asked: its own
    scan already separates an absent store (legitimately empty) from an unreadable one (raises).
    Providers without one stay presence-gated and fail-soft per record, though a wholly unreadable
    store still raises out of ``scan()`` and is recorded here.
    """
    rows: list[Session] = []
    problems: list[str] = []
    for p in _PROVIDERS:
        checked = getattr(p, "scan_checked", None)
        if checked is None and not p.is_present():
            continue
        try:
            if checked is not None:
                # `(rows, problems)`: a provider keeps every record that read cleanly and names the
                # ones that did not, so one bad file costs only itself (review 4898, finding 4).
                got, trouble = checked()
                rows.extend(got)
                problems.extend(trouble)
            else:
                rows.extend(p.scan())
        except Exception as e:  # noqa: BLE001 — one unreadable store must not blank the others
            # A store whose ROOT could not be listed raises out of the checked scan; there are no
            # partial rows to keep in that case, only the fact that we could not look.
            problems.append(
                f"{p.engine_id}: its session store could not be read ({type(e).__name__})"
            )
    return rows, problems


# Short-lived scan-snapshot cache (#561). A single `/api/sessions` request re-walks the whole
# ``~/.claude/projects`` tree with three reads per JSONL, and a keystroke burst (no debounce),
# the 15 s poll, and "load more" pagination each trigger a full walk. This memoises the parsed
# ``scan_all()`` snapshot for a short TTL so that burst collapses to a single real disk walk. Only
# the DISK WALK is cached — routes still build rows from fresh ``metadata.load()`` / ``webterm``
# state every request, so the live "working" signal, favorites, renames, etc. never go stale.
#
# Keyed on ``str(Path.home())`` (re-resolved per call) because the Claude scanner walks
# ``Path.home()/.claude/projects`` — tests monkeypatch ``$HOME`` to a ``mktemp -d`` home, so a
# global singleton would leak one test's sessions into another.
#
# TTL (#652 L1): at 1.5 s the snapshot was warm for only ~1.5 s of each 15 s poll window, so a
# deliberate search keystroke-settle or a project switch between polls almost always landed on a
# COLD walk of the whole live+archive tree (~3 reads per JSONL). Raised to 10 s so those actions
# hit a warm snapshot. Safe because every in-app write that changes what the scanner sees already
# calls ``invalidate_scan_cache()`` (archive/unarchive/new-session), so the only staleness this
# guards is a session created OUTSIDE the app (a CLI launch / a running agent writing a fresh
# JSONL) — already bounded by the 15 s poll, which re-walks on cache expiry (10 s < 15 s).
_SCAN_CACHE_TTL_S = 10.0


# ONE walk coordinator per home (#991). The sidebar's snapshot (``scan_all_cached``) and the fresh
# fallback the terminal route authorizes against (``scan_all_since``) are two ways of asking for the
# same full walk, so they share one coordinator: a walk is a numbered GENERATION with a start time,
# at most one runs per home at any moment, and every caller either reuses a generation it can trust
# or joins/starts the single next one. Two walks never run at once — which is the whole GIL-convoy
# bug of #991, where eight concurrent walks took twice as long as eight serial ones.
#
# The coordinator's lock is held only to pick a generation, never across a walk, so invalidation
# and new callers never wait on the disk. Invalidation does not drop data under anyone's feet: it
# marks every generation numbered below ``valid_from_seq`` as untrustworthy for LATER callers.


@dataclass(eq=False)
class _Generation:
    seq: int
    started: float
    done: bool = False
    finished: float = 0.0
    result: list[Session] | None = None
    error: BaseException | None = None


class _Coordinator:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.running: _Generation | None = None
        self.latest: _Generation | None = None
        self.next_seq = 0
        self.valid_from_seq = 0


_coordinators: dict[str, _Coordinator] = {}
_coordinators_lock = threading.Lock()


def _coordinator() -> _Coordinator:
    key = str(Path.home())
    with _coordinators_lock:
        coord = _coordinators.get(key)
        if coord is None:
            coord = _coordinators[key] = _Coordinator()
        return coord


def _outcome(gen: _Generation) -> list[Session]:
    if gen.error is not None:
        raise gen.error
    return gen.result if gen.result is not None else []


def _obtain(
    coord: _Coordinator,
    *,
    completed_ok: Callable[[_Generation], bool],
    running_ok: Callable[[_Generation], bool],
) -> list[Session]:
    """Reuse a completed generation ``completed_ok`` accepts, join a running one ``running_ok``
    accepts, or — when the running walk is one this caller cannot trust — wait for it to end and
    decide again, which makes every such caller share the single NEXT walk. A walk that raises
    hands its exception to every caller waiting on it; nobody is left waiting."""
    with coord.cond:
        while True:
            latest = coord.latest
            if latest is not None and completed_ok(latest):
                return _outcome(latest)
            running = coord.running
            if running is None:
                gen = _Generation(seq=coord.next_seq, started=time.monotonic())
                coord.next_seq += 1
                coord.running = gen
                break
            if running_ok(running):
                while not running.done:
                    coord.cond.wait()
                return _outcome(running)
            while coord.running is running:
                coord.cond.wait()

    # Walk OUTSIDE the lock. Resolved through the package namespace so a
    # ``monkeypatch.setattr(engines, "scan_all", …)`` (the established test seam) is honoured.
    from .. import engines as _pkg

    try:
        result, error = _pkg.scan_all(), None
    except BaseException as exc:  # noqa: BLE001 — handed to every waiter, then re-raised below
        result, error = None, exc
    with coord.cond:
        gen.result, gen.error = result, error
        gen.finished, gen.done = time.monotonic(), True
        coord.running = None
        coord.latest = gen
        coord.cond.notify_all()
    return _outcome(gen)


def set_scan_cache_ttl(seconds: float) -> None:
    """Set the scan-snapshot TTL (seconds). ``0`` disables caching — the test suite sets this so
    each request re-walks (mutation-then-rescan tests stay deterministic); the dedicated cache
    tests opt back in."""
    global _SCAN_CACHE_TTL_S
    _SCAN_CACHE_TTL_S = max(0.0, float(seconds))


def invalidate_scan_cache() -> None:
    """Make every existing walk untrustworthy. Called after any write that changes what the scanner
    sees (archive/unarchive — Claude moves the JSONL; a new-session launch writes a fresh JSONL) so
    the next list request re-walks instead of serving the just-mutated tree stale.

    Never waits on a walk in flight (the coordinator lock is not held across walks); a walk that
    started before this call finishes normally for the callers already waiting on it, but no later
    caller reuses it."""
    with _coordinators_lock:
        coords = list(_coordinators.values())
    for coord in coords:
        with coord.cond:
            coord.valid_from_seq = coord.next_seq
            coord.latest = None
            coord.cond.notify_all()


def scan_all_cached() -> list[Session]:
    """``scan_all()`` behind the short TTL snapshot (#561), keyed on the effective home. Read path
    for the sidebar list; falls straight through when the TTL is 0.

    Served through the walk coordinator (#991): a warm generation is reused, a burst of misses joins
    the one walk in flight, and the walk never overlaps a ``scan_all_since`` walk."""
    from .. import engines as _pkg

    ttl = _SCAN_CACHE_TTL_S
    if ttl <= 0:
        return _pkg.scan_all()
    coord = _coordinator()

    def warm(gen: _Generation) -> bool:
        return (
            gen.error is None
            and gen.seq >= coord.valid_from_seq
            and time.monotonic() - gen.finished < ttl
        )

    def current(gen: _Generation) -> bool:
        return gen.seq >= coord.valid_from_seq

    return _obtain(coord, completed_ok=warm, running_ok=current)


# ONE walk per paging sequence (#1007 Phase 3). The map pages the whole set in up to 20 requests,
# and a mutation that lands between two of them calls ``invalidate_scan_cache()``, so the next page
# paid a SECOND cold walk — measured 6.82 s for the sequence instead of 3.46 s. A sequence may
# therefore PIN the walk its first page used: page 1 asks for ``snapshot=new`` and gets back an
# opaque token, and later pages pass it back to be served that same walk.
#
# What is pinned is the WALK and nothing else. It is a performance cache, never an authorization
# cache (#991): every request still reloads metadata, projects and prefs and re-applies the full
# membership scope — roots, ``folder_exclusions``, archived state, visibility — to the pinned rows,
# so a pin can never decide what a request may see. It does NOT freeze ordering either: a rename,
# favourite or archive between pages reorders the per-request sort, and that is accepted.
#
# The token is ``secrets.token_urlsafe``: it carries no scope, names no session and grants nothing.
# Anything that is not a live token — ``new``, garbage, an expired or evicted token, a token minted
# under another home — takes the normal ``scan_all_cached()`` path and mints a fresh one. It never
# errors and never answers empty. Callers that pass no token (the sidebar, the single-row lookup,
# the terminal's authorization walk) never read or populate this store.
#
# Bounded twice, so no client can grow it or keep an entry alive:
# * LIFETIME — a fixed 60 s from mint, never extended by use. The walk is paid BEFORE the mint, so
#   the window only has to cover the remaining round trips: at most 19 pages of 200 rows, which it
#   allows about 3 s each (server work per page is sub-millisecond; the rest is transfer). A
#   sequence that outlives it falls back to a normal scan and a new token — exactly the pre-pin
#   cost, never an error. The walk it serves is at most the 10 s scan TTL older than the mint.
# * COUNT — at most 8 live tokens; minting the ninth evicts the oldest. Tokens minted from the same
#   walk share one list rather than copying it, so the memory held is at most 8 walks and in
#   practice one.
_SNAPSHOT_TTL_S = 60.0
_SNAPSHOT_MAX = 8


@dataclass(frozen=True, eq=False)
class _Pin:
    home: str
    sessions: list[Session]
    minted: float


_pins: OrderedDict[str, _Pin] = OrderedDict()
_pins_lock = threading.Lock()


def scan_all_pinned(token: str) -> tuple[list[Session], str]:
    """The walk ``token`` pinned, with ``token`` — or, for anything that is not a live token for
    this home, a normal ``scan_all_cached()`` result with a NEWLY minted token (#1007 Phase 3).

    Only the disk walk is reused. The caller must re-apply every scope check to these rows on every
    request; see the block comment above for why this can never become an authorization cache."""
    from .. import engines as _pkg

    home = str(Path.home())
    with _pins_lock:
        now = time.monotonic()
        for stale in [k for k, p in _pins.items() if now - p.minted >= _SNAPSHOT_TTL_S]:
            del _pins[stale]
        pin = _pins.get(token)
        if pin is not None and pin.home == home:
            return pin.sessions, token

    # Not a live pin: the ordinary cached read (never under the pin lock — it may walk).
    sessions = _pkg.scan_all_cached()
    fresh = secrets.token_urlsafe(16)
    with _pins_lock:
        _pins[fresh] = _Pin(home=home, sessions=sessions, minted=time.monotonic())
        while len(_pins) > _SNAPSHOT_MAX:
            _pins.popitem(last=False)
    return sessions, fresh


def scan_all_since(arrival: float) -> list[Session]:
    """A full walk that STARTED at or after ``arrival`` (a ``time.monotonic()`` reading the caller
    took when its request arrived), through the same coordinator as the sidebar snapshot (#991).

    This is the freshness guarantee the terminal route authorizes against for a provider that
    cannot read one session directly: the answer reflects the store as it was no earlier than the
    connect itself — never a snapshot from before it, never a walk from before the last
    invalidation. Callers that arrived before a walk started share it; callers that arrived while
    an older walk ran share the one walk after it."""
    coord = _coordinator()

    def fresh(gen: _Generation) -> bool:
        return gen.started >= arrival and gen.seq >= coord.valid_from_seq

    return _obtain(coord, completed_ok=fresh, running_ok=fresh)


def resolve_session(engine_id: str, native: str, *, arrival: float | None = None) -> Session | None:
    """The session ``engine_id:native`` as it is NOW, or ``None`` (#991). Blocking — call it off the
    event loop.

    A provider with a single-key ``lookup`` reads just that session (fresh on every call, no walk);
    any other provider is answered by a walk that started at or after ``arrival`` (default: now).
    The TTL snapshot never answers this, because the terminal route authorizes an ATTACH/RESUME
    against the row. A failed read is ``None`` — the route's existing unknown-row rules then apply
    (fail closed wherever a boundary is configured)."""
    if arrival is None:
        arrival = time.monotonic()
    prov = get_any(engine_id)
    if prov is None:
        return None
    if is_retiring(prov):
        return _resolve_retiring(prov, engine_id, native)
    try:
        lookup = getattr(prov, "lookup", None)
        if lookup is not None:
            row = lookup(native)
            if row is not None and row.engine == engine_id and row.uuid == native:
                return row
            return None
        rows = scan_all_since(arrival)
    except Exception:  # noqa: BLE001 — resolution is fail-soft; the caller fails closed
        log.warning("resolving %s:%s failed", engine_id, native, exc_info=True)
        return None
    return next((s for s in rows if s.engine == engine_id and s.uuid == native), None)


def _resolve_retiring(prov, engine_id: str, native: str) -> Session | None:
    """One session of a RETIRING engine (Hermes on PR #1132): read from ITS OWN recorded store —
    the lookup inside its store scope, or, for a kind with no single-key lookup, its own scan —
    because a retiring engine is in no shared walk. Fail-soft to None, so the terminal's
    fail-closed rule for an unidentifiable session still applies."""
    try:
        with base.store_scope(engine_id):
            lookup = getattr(prov, "lookup", None)
            if lookup is not None:
                row = lookup(native)
                rows = [row] if row is not None else []
            else:
                rows = prov.scan()
    except Exception:  # noqa: BLE001 — resolution is fail-soft; the caller fails closed
        log.warning("resolving retiring %s:%s failed", engine_id, native, exc_info=True)
        return None
    return next((s for s in rows if s.engine == engine_id and s.uuid == native), None)


def session_key(s: Session) -> str:
    """The engine-qualified identity for a scanned session."""
    return f"{s.engine}:{s.uuid}"


def is_new_session_placeholder(raw: str) -> bool:
    """True if ``raw`` is an ``<engine>:new-<uuid>`` new-session placeholder for an engine that
    mints its own id and reconciles (opencode, codex — #127/#315). Engine-agnostic: gated on the
    provider's ``new_session_reconciles`` flag, not a hard-coded engine id."""
    if ":" not in raw:
        return False
    engine_id, _, native = raw.partition(":")
    prov = get_any(engine_id)
    return bool(getattr(prov, "new_session_reconciles", False)) and bool(
        base._NEW_PLACEHOLDER_RE.fullmatch(native)
    )


def is_opencode_new_placeholder(raw: str) -> bool:
    """Deprecated back-compat alias of :func:`is_new_session_placeholder`."""
    return is_new_session_placeholder(raw)


def parse_key(raw: str, *, allow_new_placeholder: bool = False) -> tuple[base.EngineProvider, str]:
    """Resolve an engine-qualified id (``engine:native_id``) to (provider, native_id).

    Back-compat: a bare value matching Claude's UUID shape is treated as a Claude
    id, so pre-multi-engine clients / bookmarks keep working. Raises ``EngineError``
    on an unknown engine or a native id that fails the provider's pattern — this is
    the validation gate before any dispatch.

    ``allow_new_placeholder`` (set ONLY by the ws ``new=1`` launch path, #127/#315) also
    accepts the ``new-<uuid>`` placeholder for any engine whose ``new_session_reconciles``
    flag is set (opencode, codex — they mint their own id). It is NOT accepted on the
    resume/attach path, so a placeholder can never be used to attach to or resume an
    arbitrary session.
    """
    if ":" in raw:
        engine_id, _, native = raw.partition(":")
        # A RETIRING engine still parses: its live masters must stay attachable (#853 P3). What
        # it may not do — launch, resume, seed — is refused where that happens, by reason.
        prov = get_any(engine_id)
        if prov is None:
            raise base.EngineError(f"unknown engine: {engine_id!r}")
    else:
        prov = get_any(_BARE_ID_ENGINE or "")
        if prov is None:
            raise base.EngineError("an engine-qualified id is required")
        native = raw
    # `fullmatch`, never `match`: every pattern ends in `$`, and `$` also matches just before a
    # trailing newline, so `match` accepted `<uuid>\n` as a valid id (found by #853 P2's matrix).
    if (
        allow_new_placeholder
        and getattr(prov, "new_session_reconciles", False)
        and base._NEW_PLACEHOLDER_RE.fullmatch(native)
    ):
        return prov, native
    if not prov.id_pattern.fullmatch(native):
        raise base.EngineError(f"bad {prov.engine_id} id: {native!r}")
    return prov, native


def archive_state(prov, native: str) -> str:
    """Has the ENGINE'S OWN store archived this session? ``archived`` · ``not-archived`` ·
    ``unreadable`` (#896 review 24, finding 1).

    **This is deliberately NOT an existence check, and the difference is the whole design.** An
    earlier version classified a session as live/archived/absent, which meant re-implementing every
    scanner's semantics — and got two of them wrong in opposite directions: a `claude -p` one-shot
    (`entrypoint: "sdk-cli"`, which `scanner` skips because it is not a session anyone can attach
    to) read as a live session, and a malformed `shell` record did too, while Gemini — which pins
    its id BEFORE writing its chat file — was refused because "no file yet" was reported as "cannot
    tell".

    Existence is what `scan()` is for, and its fail-soft answer is fine THERE because it is only
    ever used as POSITIVE evidence: finding a semantically valid row proves a session exists, and
    not finding one simply means the live writer has to prove it instead. What `scan()` cannot be
    trusted for is the NEGATIVE half — "there is no archived row" — because a swallowed read
    produces exactly that answer. So only that question gets its own reader.

    Most engines have no engine-side archive at all: the flag lives only in the sidecar, which the
    caller reads under the writer's own flock and asks first. For them "no row in a tree that does
    not exist" is a real `not-archived`. `claude` is the one that also MOVES the transcript, and it
    implements this.
    """
    fn = getattr(prov, "archive_state", None)
    if fn is not None:
        try:
            state = str(fn(native))
        except Exception:  # noqa: BLE001 — a provider that raises has told us it cannot answer
            return "unreadable"
        return state if state in ("archived", "not-archived", "unreadable") else "unreadable"
    try:
        for row in prov.scan() or []:
            rid = _row_field(row, "id")
            ruuid = _row_field(row, "uuid")
            if ruuid == native or (rid and rid.endswith(native)):
                return "archived" if _row_field(row, "archived", raw=True) else "not-archived"
    except Exception:  # noqa: BLE001
        return "unreadable"
    # NO ROW. For a sidecar-only engine there is no tree an archived session could be hiding in,
    # and the sidecar has already been read under its writer's flock. An engine that grows one
    # implements `archive_state` — this default cannot answer for a tree it does not know about.
    return "not-archived"


def _row_field(row, name: str, *, raw: bool = False):
    value = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
    return value if raw else str(value or "")


def physical_key(key: str, aliases: dict[str, str] | None = None) -> str:
    """Resolve an engine-qualified ``key`` to the PHYSICAL key its live resources are
    under (#127 alias layer).

    For opencode new-session, the dtach socket / single-writer lock / scrollback buffer /
    metadata are all keyed by the ``new-<uuid>`` placeholder the master was launched
    under. Once reconciled, an alias ``placeholder → real`` is persisted; an attach by the
    *real* id must therefore resolve back to the placeholder. So this maps a real id to
    its placeholder (the inverse of the stored map) and leaves everything else unchanged.

    Pass ``aliases`` (``metadata.load_aliases()``) to avoid re-reading the sidecar; omit
    to read it. Idempotent and safe for non-opencode keys (returns ``key``).
    """
    if aliases is None:
        from .. import metadata as _md

        aliases = _md.load_aliases()
    # stored map is placeholder→real; we need real→placeholder for resource lookup.
    for placeholder, real in aliases.items():
        if real == key:
            return placeholder
    return key


def logical_key(key: str, aliases: dict[str, str] | None = None) -> str:
    """Resolve an engine-qualified ``key`` to the LOGICAL key its *engine* knows it by —
    the inverse of :func:`physical_key` (#611).

    A session launched on a mint-its-own-id engine (codex / opencode / antigravity) keeps the
    ``new-<uuid>`` placeholder as its physical key for life: that's what the dtach socket, the
    lock, the ring and the sidecar are keyed by. But the engine's own transcript store is keyed
    by the REAL id it minted. Anything that wants to read that store — the AI reviewer and its
    recap — must map placeholder → real, or ``parse_key`` rejects the placeholder shape and the
    transcript silently reads as empty.

    The stored alias map is already ``placeholder → real``, so this is a direct lookup. Pass
    ``aliases`` (``metadata.load_aliases()``) to avoid re-reading the sidecar; omit to read it.
    Idempotent, and a no-op for pinned-id engines and unreconciled placeholders (returns
    ``key``).
    """
    if aliases is None:
        from .. import metadata as _md

        aliases = _md.load_aliases()
    return aliases.get(key, key)


def canonical_key(raw: str) -> str:
    """Normalize a raw/back-compat id to its canonical ``engine:native_id`` form."""
    prov, native = parse_key(raw)
    return f"{prov.engine_id}:{native}"


def parse_runtime_key(raw: str) -> tuple[base.EngineProvider, str]:
    """`parse_key` that also accepts a late-id engine's ``new-<uuid>`` placeholder (#989).

    **INTERNAL to owned-runtime teardown, and nothing else.** A dispatch that failed before its
    engine revealed a real id leaves a master, a lock and a ring keyed on the placeholder, and
    stopping it must not need an id that never existed. Every public path — adoption, resume,
    routes — keeps `parse_key`/`canonical_key`, which refuse the placeholder shape, so a
    placeholder can never be adopted, attached to or resumed.
    """
    return parse_key(raw, allow_new_placeholder=True)


#: What a LATE-ID engine must declare before it may be dispatched unattended (#989). Each one is a
#: provider method; the dispatcher only asks. A pinned-id engine keeps the existing path, where the
#: id is known before the launch and is therefore bound at launch.
LATE_ID_CAPABILITIES: tuple[str, ...] = (
    # How THIS engine honours `bypass=False` with nobody watching (#1050). Required, not optional:
    # a late-id engine whose permissions are config-side would otherwise launch with whatever its
    # default is — opencode's is allow-all — while the dispatcher believed it had withheld bypass.
    "unattended_launch",
    "unattended_preflight",
    "start_evidence",
    "bind_session",
    "snapshot_session_ids",
)


def unattended_start_state(prov: base.EngineProvider | None) -> tuple[bool, str | None]:
    """``(supported, reason)`` for an UNATTENDED launch of ``prov`` (#989).

    Separate from `handoff.seed_start_state` on purpose: that one answers "can this engine take a
    seed at all", which an operator watching a terminal is allowed to rely on. This one answers
    "can the app tell, with nobody watching, that the brief will land in an agent and which session
    it became". A mission offers and dispatches only engines for which both are true.

    **Default-deny for late-id engines**, for the reason the dispatcher refused them before this
    existed: their store cannot be asked before the brief is typed, and a trust or consent dialog
    is armed, painted and quiet. An engine that has not declared every capability is refused, and
    the reason names what is missing rather than stopping at the session id.
    """
    if prov is None:
        return False, "unknown engine"
    if not getattr(prov, "new_session_reconciles", False):
        return True, None
    missing = [c for c in LATE_ID_CAPABILITIES if not callable(getattr(prov, c, None))]
    if missing:
        return False, (
            f"{prov.engine_id} does not reveal its session id until after its first turn, and "
            f"has no unattended start check yet (missing: {', '.join(missing)}), so nothing can "
            "tell a live agent from a first-run or consent screen before the brief is typed. "
            "Unattended dispatch is refused for this engine; start it from a terminal."
        )
    return True, None
