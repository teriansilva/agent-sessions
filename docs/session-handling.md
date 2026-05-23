# Session handling — the rock-solid contract

The #1 invariant of agent-sessions, enforced in code **and** locked by tests:

> **One session id ⇒ at most one running agent ⇒ exactly one writer of that
> session's on-disk history.** Ever. Across reconnects, app restarts, deploys,
> multiple browser tabs, and even multiple app instances (prod + staging) that
> share the same filesystem.

This document is the spec the implementation and its test suite must satisfy
(#64 Phase 0). It exists because we hit every one of these failure modes with
the ttyd+Zellij + ws-bridge stack: a session resumed in prod **and** staging at
once, two agents writing the same `~/.claude` JSONL, "the agent doesn't
remember what we did," and duplicate rows.

## Identity

- A session's identity is `key = "{engine}:{native_id}"` (e.g. `claude:<uuid>`,
  `opencode:<ses_…>`). This is the URL (`/s/:engine/:id`), the socket name, and
  the lock name — one string, everywhere. No mutable label (the Zellij tab-label
  failure mode) is ever the identity.

## The single-writer lock (the core guarantee)

Resuming/launching a session is gated by an **advisory exclusive file lock**:

- Lock path: `${AGENT_SESSIONS_LOCK_DIR:-~/.agent-sessions/locks}/{sanitized-key}.lock`,
  held for the **lifetime of the agent process** via `fcntl.flock(LOCK_EX|LOCK_NB)`.
- The lock dir is **shared filesystem state**, so the lock is honored by *every*
  app instance on the host — prod and staging cannot both resume `claude:6a73…`.
- Decision (`sessions.open_action`, synchronous so it runs atomically between
  coroutines — two concurrent opens of the same id can't both reach LAUNCH):
  1. Live master at `socket_path(key)` ⇒ **ATTACH** (never relaunch), no lock.
  2. Else `try flock(key, LOCK_EX|LOCK_NB)`:
     - **Acquired** ⇒ sole writer ⇒ **LAUNCH** (re-check the socket between the test
       and the acquire — if a master appeared, release and ATTACH).
     - **Would block** (held by another instance, or by an in-flight launch in ours
       whose connection still holds the fd) ⇒ **BUSY**: do NOT launch a second agent —
       the client retries and attaches once the master is up. The ws path closes **4409**.
- **Lock handoff to the master (the master-lifetime / cross-instance guarantee).** On
  LAUNCH the acquired fd is passed (`pass_fds`) to the spawned `dtach` process, so the
  long-lived `dtach` master inherits it. The launching connection then **closes its own
  fd without `LOCK_UN`** (`SessionLock.transfer`): the flock is held on the shared open
  file description, so it stays held while the master keeps its inherited fd, and the
  kernel releases it only when the **master dies** (the last fd closes). This is what
  makes the guarantee survive an app restart/redeploy/crash, and hold across instances
  even where the `dtach` socket isn't shared. (`LOCK_UN` would release the lock for the
  whole shared description — defeating the handoff — so the transfer path never unlocks;
  `release()` is only for the lost-race path where no master inherited the fd.)
- If a launch fails before a master exists (bad cwd / unresolvable binary), `transfer`
  closes the last fd and the lock frees immediately — no BUSY wedge.

## Attach, never relaunch

- A live session = a `dtach` master at `socket_path(key)` (single chokepoint;
  honors the real→temp alias map for engines that can't pin an id up front).
- **Open/resume = attach to the existing master** (`dtach -A` to the *same*
  socket). We never spawn a second `--resume <same id>` for an id that already
  has a master. Liveness is read from the socket's existence, never guessed.
- Multiple viewers (tabs/devices) attaching to one master are fine — they share
  the one agent (one writer). The transport multiplexes output to each viewer.

## Reconnect continuity (delta-resume)

A transient ws drop must be invisible — never blank, never relaunch:

- Per-key durable ring buffer (cap, e.g. 256 KB) **plus a monotonic absolute byte
  counter** (`total`).
- Client tracks the absolute offset it has consumed; on (re)connect it sends
  `?have=<offset>`.
- Server: if `0 < have <= total` and `have` is still within the ring →
  stream **only** `ring[have - (total-len(ring)):]` (the delta) — screen
  continues seamlessly. Else (fresh attach / fell behind the ring) → full replay
  (inline) or a redraw nudge (alt-screen), then a `{"t":"seq","n":total}` control
  frame sets the client's authoritative offset.
- Keepalive ping + capped-backoff reconnect. **Never** clear the terminal on a
  transient drop; **never** relaunch the agent on reconnect.
- A mid-escape-sequence drop reassembles because the xterm parser state persists
  across the reconnect on the same client.

## Lifecycle

- States: `starting → attached → detached → exited`. Detach ≠ kill: closing a
  viewer terminates only that viewer's `dtach` *client*; the master (agent) lives.
- Stale-socket reaping: a socket with no live master is removed before a new
  attach decision.
- Exit: when the agent process exits, the master goes away, the lock releases,
  the socket is cleaned; the session becomes resumable-from-history only.

## Failure modes this design eliminates

| Failure we hit | Prevented by |
|---|---|
| Same id resumed in prod **and** staging (double JSONL writer) | shared-filesystem `flock` per key |
| Reopen spawns a 2nd `--resume <id>` in a new tab | attach-never-relaunch + lock |
| "Agent doesn't remember what we did" | single writer ⇒ one coherent history |
| Duplicate rows / two agents one session | one master per key, liveness from socket |
| Flicker/blank on network blip | delta-resume + never-blank-on-drop |
| Orphan duplicate clients | detach≠kill + stale reaping |

## Tests that lock the invariant (release gate)

- **Unit:** lock acquire/contend (second acquire blocks → attach path chosen);
  `socket_path` alias resolution; delta-resume slice math (delta / full / fresh /
  fell-behind); stale-socket reaping.
- **Integration:** opening an already-running id attaches and does **not** spawn
  a 2nd process (assert process count == 1 for the key); a second app instance
  pointed at the same lock dir cannot resume a held id.
- **E2E (Playwright):** reconnect after a forced drop continues without blanking;
  refresh/deep-link to `/s/:engine/:id` re-attaches the same agent; new-session
  landing at `/` never auto-resumes.

No release/cutover ships unless all of the above are green.
