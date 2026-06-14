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

## opencode new-session: placeholder→real alias (#127)

Claude/gemini accept `--session-id <id>`, so the bridge mints the id up front and
keys the socket/lock/buffer/metadata by it before launch. **opencode can't pin a
new-session id** (`opencode --session` only *continues*; there is no
create-returning-id). So opencode new-session uses **launch-then-reconcile** with a
stable alias layer — never a physical rename of a live dtach socket:

1. **Placeholder.** The client mints `new-<uuid>` and opens
   `/s/opencode/new-<uuid>?new=1&cwd=…`. `new-<uuid>` is a valid opencode id **only**
   on the `new=1` launch path (`parse_key(allow_new_placeholder=True)`); resume/attach
   still requires `ses_…`. The socket, lock, buffer, and metadata are all keyed by this
   placeholder — it is the **physical key**.
2. **Snapshot + launch.** Before launch the route snapshots the set of `ses_…` ids in
   that `cwd` from `opencode.db` (read-only). It launches `opencode <dir>` (no
   `--session` → opencode mints its own id) under the placeholder dtach socket.
3. **Reconcile (alias).** A coroutine running alongside the PTY bridge polls
   `opencode.db` (read-only) for an id in that `cwd` not in the snapshot:
   - **exactly one new id** → ours: persist `opencode:new-<uuid> → opencode:<ses_…>` in
     the metadata sidecar (`metadata.set_alias`), then push `{"t":"id","sid":…}` to the
     client.
   - **≥2 new ids** (two same-cwd launches in the window) → **ambiguous**: do **not**
     guess (never attach to the wrong session); keep serving under the placeholder.
   - **none yet** (opencode may not write the row until first input) → keep polling; if
     the poll budget exhausts, keep serving under the placeholder forever (never blocks
     the terminal).
4. **Resolve through the alias EVERYWHERE.** The stored map is `placeholder → real`. The
   live resources are under the **placeholder**, so an attach by the *real* id resolves
   **real → placeholder** (`engines.physical_key`, the inverse of the stored map) before
   any socket / single-writer-lock / scrollback-buffer / metadata derivation. The session
   list resolves each scanned `ses_…` row's metadata via its physical key, so a title set
   while on the placeholder follows the real row — **one row, no placeholder/ghost row**
   (the #64 failure mode). The placeholder is never scanned (it isn't in `opencode.db`),
   so it can't appear as a second row.
5. **Client converge.** On `{"t":"id","sid":"opencode:ses_…"}` the SessionView replaces
   the URL `/s/opencode/new-<uuid>` → `/s/opencode/ses_…` (history replace, no reload) and
   drops the fresh-launch state, while keeping the terminal mounted under its original key
   — the live socket is preserved (no relaunch/flicker). A later reload attaches by the
   real id and resolves back to the placeholder socket via the persisted alias.
6. **Restart survival.** The alias lives in the on-disk sidecar, and the dtach
   socket/lock stay under the placeholder for the master's lifetime. After an app restart
   a fresh instance reads the alias and still resolves real → placeholder, so an attach by
   the real id finds the running master. `opencode.db` is **strictly read-only**
   throughout.

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

- A live session = a `dtach` master at `socket_path(key)` that is **actually
  accepting connections** — file presence alone is not enough (a master that
  crashed without unlinking can leave its `.sock` file behind). `session_exists`
  probes the sock with a non-blocking `connect()` so an orphan is correctly
  classified as not-alive and the open path takes LAUNCH. #165.
- **Mode-explicit dtach (#165):** the server's `open_action` is the sole
  ATTACH-vs-LAUNCH decision; dtach is never allowed to fall back on its own:
  - ATTACH path runs `dtach -a <sock>` (attach-only — fails loud if no master
    is accepting; the client retries through `open_action` rather than the server
    silently spawning a second writer).
  - LAUNCH path holds the fcntl lock, unlinks any orphan `.sock` (`unlink_if_stale`)
    so a stale file from a previous generation can't block `bind()`, then runs
    `dtach -c <sock> <agent>` (create-only — fails loud if a master is somehow
    racing for the path; the lock makes that impossible in practice).
- Multiple viewers (tabs/devices) attaching to one master are fine — they share
  the one agent (one writer). The transport multiplexes output to each viewer.

## Restart / reconnect taxonomy

What survives what — and why:

| Event | Browser WS | dtach master | Agent process | On-disk session | Notes |
|---|---|---|---|---|---|
| Browser tab close / network drop | dies | **alive** | **alive** | unchanged | Server keeps the master + buffer; client reopens with `?have=N` for delta-resume. |
| Browser reload | dies | **alive** | **alive** | unchanged | Same path. URL is bookmarkable identity. |
| `systemctl --user restart agent-sessions` (deploy) | dies | **alive** | **alive** | unchanged | `KillMode=process` on the unit — systemd SIGTERMs only the broker's main PID; dtach + the agent are children, untouched. The new broker re-attaches via `dtach -a <existing sock>`. **No turn loss.** #165. |
| Broker SIGKILL / OOM | dies | **alive** | **alive** | unchanged | Same outcome — children outlive ungraceful broker death too. |
| Agent itself exits (`/quit`, crash, OOM) | gets EOF / "ended" | gone (dtach exits when its child dies) | gone | preserved | Sock is removed by dtach; `session_exists` returns False; next attach for the key takes the LAUNCH path. |
| Host reboot / `systemctl --user daemon-reexec` / user logout | dies | gone | gone | preserved (jsonl on disk) | Unavoidable. On next boot, the session is resumable-from-history only — the in-memory turn state is lost. |

The first four rows are the cases #165 makes survivable. The last row is the only one where context can be lost, and only because the kernel destroyed the whole user-session that owns the processes.

(Slice 2 — separate issue — adds a server-side `SessionStream` registry rebuilt from `/proc` on startup so the broker has rich state for the live agents it rediscovers after a restart, and so headless sessions still update `last_output_at` for the activity indicator. The single-resume invariant in this doc holds for slice 1 alone; the registry is observability + UX on top.)

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

## Per-tab ownership protocol (#184 slice 3)

A session can be open in more than one browser tab at the same time — for
example, a forgotten tab on the desktop while the user opens the same URL on a
phone. The single-writer invariant on disk is already absolute, but at the
**WebSocket** layer we need to decide which tab's keystrokes the agent sees,
and surface that decision to the other tab so the user isn't typing into the
void.

### Identity

Each browser carries a stable `tr-browser-fp` (128-bit hex, `localStorage`); each
tab gets a fresh `tab_id` (64-bit hex, in-memory). The pair `(fp, tab_id)` is the
**claim key**.

### URL extension

`/ws/term/{key}?have=N&new=…&fp=…&tab=…&force=0|1`

- `fp` + `tab` are sent on every connect. The pre-slice-3 server (and tests)
  ignore them, so the new params are **additive** and don't change behavior for
  clients that send neither.
- `force=1` is sent **only** on a deliberate take-over: the next connect demotes
  the prior owner. A transient reconnect after a drop MUST NOT include `force=1`,
  or it would shut a legitimate prior owner out.

### Server-side claim layer (`SessionRegistry`)

A per-session `Claim` records `(fp, tab_id, last_seen)`. On each WS attach the
registry decides the role:

- **owner**: no prior claim, or the prior claim matches `(fp, tab_id)`, or the
  prior claim's `last_seen` is older than the 5-second lease, or `force=1`.
- **secondary**: a fresh claim for a different `(fp, tab_id)` exists.

A force takeover sets the prior `Claim.demoted` event so the prior owner's WS
bridge can flip itself into read-only mode without tearing the WS down.

### Server-side input gate

The WS bridge (`webterm.run`) accepts a `read_only_gate: asyncio.Event`. When the
gate is set — at connect for secondaries, or mid-session when the owner is
demoted — `pump_in` silently drops `i` / `r` / raw-byte frames. The server gate
is the source of truth: a misbehaving secondary client cannot write to the
agent regardless of what it sends.

### Control frames

- `{"t":"role","role":"owner"}` — server tells the client "you hold the role."
  Sent once at connect; sent again if the role flips.
- `{"t":"role","role":"secondary"}` — client renders the read-only banner with
  a Take-over button.

### Take-over flow

1. Secondary tab sees `{"t":"role","role":"secondary"}` → banner renders.
2. User clicks **Take over** → React tears down the current `TermSocket` and
   reconnects with `?force=1`.
3. Server's `claim(force=True)` demotes the prior owner; that bridge gets
   `claim.demoted.is_set()`, flips its `read_only_gate`, sends
   `{"t":"role","role":"secondary"}` to its own client → that tab now sees the
   banner.

### Backward compatibility

A client sending neither `fp` nor `tab` (e.g. an older `web/dist`) goes straight
to the legacy owner path — no claim recorded, no role frame; the server doesn't
gate input. New clients always send the pair.

### Tests that lock this surface

- Unit on `browserFp.ts` (mint + persist + idempotent + localStorage-failure
  fallback + per-tab id distinct).
- Unit on `SessionRegistry.claim` / `refresh` / `release` (owner vs secondary,
  force takeover, stale lease, heartbeat, legacy attach).
- Vitest on `Terminal`: fp + tab in URL on every connect; banner appears on
  secondary; Take-over reconnects with `force=1` exactly once.

## Per-session systemd scopes (#346 Phase B)

Session masters used to spawn as direct broker children, so every agent process
tree shared `agent-sessions.service`'s cgroup — one runaway session's OOM kill
failed the whole unit (pre-Phase-A `OOMPolicy` default) and every session drew
from one `TasksMax` budget. On LAUNCH the built dtach argv is now wrapped by
`scopedspawn.wrap()`:

```
systemd-run --user --scope --collect --quiet \
  --unit as-<engine>-<sid8>-<nonce>.scope \
  -p TasksMax=512 … -- dtach -c <sock> -z -E -r winch <agent argv…>
```

- `--scope` fork/execs the payload in-process, so the PTY wiring, the
  controlling tty (`start_new_session`) and the single-writer lock fd
  (`pass_fds`) pass through unchanged — pinned by an integration test that
  asserts the flock survives into the payload on hosts with a user manager.
- `--collect` GCs the scope when its last process exits; the random nonce in
  the unit name means kill → instant relaunch never collides with a
  not-yet-collected predecessor.
- Only LAUNCH is scoped. Viewer attaches (`dtach -a`) and headless
  `SessionStream` readers stay in the broker cgroup — they die with their
  websocket and would add a systemd round-trip per reconnect.
- The reaper and manual Restart (#331) are unaffected: they find masters by
  `/proc` cmdline scan and signal by process group, both cgroup-agnostic.

### Configuration

| Env | Default | Meaning |
|---|---|---|
| `AGENT_SESSIONS_SESSION_SCOPES` | `1` | `0` disables scoping entirely (logged once as *disabled (config)*). |
| `AGENT_SESSIONS_SCOPE_PROPERTIES` | `TasksMax=512` | Space-separated `Key=Value` systemd properties applied per scope. Strictly validated (`Key=Value` charset) so env contents can never inject argv tokens. Memory limits are deliberately not defaulted — opt in after verifying controller delegation on staging. |
| `AGENT_SESSIONS_SYSTEMD_RUN_BIN` | `systemd-run` | Override for tests/unusual installs. |

### Fallback ladder (OSS installs, non-systemd hosts)

A session is never refused because isolation is unavailable:

1. Flag off → plain spawn, one-shot `disabled (config)` log line.
2. `systemd-run --user` probe fails (no binary, no user manager, no DBus) →
   plain spawn, one-shot `unavailable (probe)` warning; the failed probe is
   re-tried after a cooldown so a transiently broken user manager recovers.
3. A scope creation that fails at spawn time exits immediately → the websocket
   closes `4502` (retryable, Phase A) and the next attempt re-runs the ladder.

Existing masters keep running in whatever cgroup they were born in; the scope
applies from each session's next launch. No migration is needed.
