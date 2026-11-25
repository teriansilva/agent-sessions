# Multi-user: the execution and authorization contract (#1469 P0, #2370)

This is the contract every multi-user phase builds on. It ships **no** multi-user behaviour:
nothing here changes what the single operator sees or can do. Later phases implement it; a phase
that needs to change a rule changes this file in the same PR, and its review checks the change.

Status of each part: **frozen** (decided here; a later phase implements it as written),
**starting target** (a number P0b's measurements confirm or replace before it is frozen) or
**open until P0b** (decided from the VM evidence).

Terms. A *person* is a BattleLab user (`user:<user_id>`). A *peer* is a paired instance
(`peer:<peer_id>`, `docs/invariants/federation.md`). A *runtime account* is the Linux account a
session runs as. An *agent account* is a vendor CLI login (#1820, `docs/invariants/agent-accounts.md`).
The *identity store* is the P1 SQLite store of users, logins, memberships and generations.

## 1. The two matrices (frozen)

- [`route-matrix.tsv`](route-matrix.tsv): one row per registered `(method, path)`, WebSockets as
  `WS`. Columns: `module`, `resource`, `action`, `principal_source`, `effect`, `frequency`,
  `status`, `note`.
- [`producer-matrix.tsv`](producer-matrix.tsv): what is not a route. The brokers, notification
  producers, background effects and launch paths, each with a `module:qualname` symbol.

`tests/test_multiuser_route_matrix.py` checks the files against the code. What it can and
cannot catch:

- **Routes, both directions.** Every registered route has exactly one row, and every row names a
  registered route. A new route fails the test until it has a row, and the failure prints a
  suggested row.
- **Values.** Every enum is validated and duplicates are refused. Each row's `principal_source`
  must agree with the route's declared auth dependency, and each WebSocket's with the identity its
  handshake authenticates. A GET is never `mutate`, a mutation is never `read`, and changing a
  public or artifact link is always a `share` action.
- **Authority-changing routes are pinned.** Project creation, link creation and revocation, peer
  grants and pairing, webhook creation, bypass mode and the password are pinned to their exact
  resource and action, so a row that is enum-valid but wrong fails.
- **HQ.** Every HQ operation (`ask_operations.CATALOG`) must reach a classified capability route
  by method and path. The SPA catch-all (`GET /{spa_path:path}`) never counts, since it answers
  any unknown GET. Negative controls (an unknown GET, a missing POST, a real path with the wrong
  method) must stay unclassified.
- **Producers, one direction only.** Each listed producer's symbol must resolve. The test **cannot**
  detect a producer that is added to the code without a row, or a row removed for a producer that
  still exists: unlike routes, producers have no registry to enumerate. A PR that adds a broker,
  notification producer, background effect or launch path adds its row by review (the CLAUDE.md
  invariants row says so). P1 should replace that review rule with a registry if one emerges.

**Vocabularies.** `resource` and `action` follow #1469's permission catalog:
`instance`, `project`, `session`, `mission`, `files`, `credentials`, plus `library` (instance
Library items), `artifact`, `self` (a person's own data: prefs, bell, push, HQ, avatar,
connectors) and `none` (public, no data). `principal_source` is what the request authenticates:

| Value | Authenticator |
|---|---|
| `login` | The login cookie: a declared `require_session` / `require_csrf_and_origin` dependency, or a handler that checks the session itself (its note says where) |
| `public-link` | A public share or artifact link token |
| `view-token` | A signed view token minted for a signed-in viewer (`/artifacts/o`, `/artifacts/v`) |
| `hmac` | A webhook's HMAC secret |
| `peer` | A paired peer's Noise key (`/ws/fed`) |
| `pre-auth` | Nobody yet: the login form and the device-link start |
| `none` | Nothing: liveness and the static SPA shell |

`frequency` is `hot` (per keystroke, frame, event poll or snapshot), `warm` (per page view or
action) or `cold` (setup and administration). §6 keys its cost rules on `hot` rows.

The P0 classification is a reviewed starting point. When P2 wires a route's check and finds that
its row is wrong, the row is corrected in the same PR.

**Findings recorded while classifying** (each is a later phase's work, named in the row's note):
- Two WebSockets, `/ws/plugins/signin/{operation_id}` and
  `/ws/agent-accounts/{engine}/{account}/signin`, authorize by comparing `session_uid(...)` with
  `cfg.username`. A username is a display and login value, never authority. P1 replaces both with
  an administrator check.
- `/api/pulse/chat` classifies instructions and, under `yolo`, can deliver keystrokes into a
  session, so it is classified `session.send`, not configuration.
- `POST /api/shares/revoke-all` and `POST /api/reply-shares/revoke-all` end every public link on
  the instance. Classified `session.share`; in multi-user mode they end only the caller's own
  links unless the caller is an administrator.
- `prefs.json`, the bell ledger, push subscriptions, HQ's stores and the avatar are one global root
  each. They are classified `self`, so P1 must split them per user before non-admin login opens.

## 2. The first non-admin admission gate (frozen)

**Non-admin login stays closed while either matrix has an `unscoped` row.** That covers reads,
mutations, streams, background effects, brokers and launch paths. Read filtering alone never
opens it, and neither does a complete route list while a producer row is `unscoped`.

| `status` | Means |
|---|---|
| `unscoped` | Not yet checked per person. Every row is `unscoped` at P0. |
| `admin-only` | Refuses every non-administrator. A closed, valid state: non-admin login can open before every capability is shared. |
| `scoped` | On a `login` row, the route calls `authorize(actor, action, resource)` for its catalog action, with a test per action covering allowed, denied and revoked. On a `public-link`, `view-token`, `hmac` or `peer` row, its authenticator is bound to an owning person, and every effect is attributed to and authorized as that owner. On a producer row, it mints, fans out or acts only after `authorize()` for the actor it acts for, and its audit records that actor and the execution account. |

Until an authenticator kind is owner-bound, the login routes that **mint** it are `admin-only`:
creating a public link, a webhook, a view token or a peer grant. No non-admin can create an
unscoped back door.

P1 enforces the gate mechanically. The identity store refuses a non-administrator login while
either shipped matrix contains an `unscoped` row, and a test pins that refusal against the files
in the build.

## 3. Principals: a person is not a peer (frozen)

- **A peer stays as shipped.** `federation.grants.authorize` requires a `trusted` peer and compares
  `grant.principal` with `store.principal_of(peer_id)`, where `peer_id` is the channel's
  authenticated peer. Nothing here changes that path.
- **A person is resolved from their login session**, through the identity store, into
  `user:<user_id>`. A person is never stored as, or derived from, a peer row, and never passes the
  trusted-peer check.
- **Two entry points feed one grant check.** `resolve_peer(channel)` and `resolve_user(login)` each
  produce a principal; one grant check then compares it with the grant row. A local route passes
  the user principal; a peer verb passes the peer principal. After P4, a peer's effective right on
  a resource is the intersection of the peer grant and the local user authority it acts under
  (#1246 §7); a peer never becomes a local user or an administrator.
- **The two reads stay.** The check runs when a call arrives and again as the last `await` before
  the answer is returned, with nothing awaited between that read and the `return`
  (`docs/invariants/federation.md`, "Grants and the per-call check"). This holds for both
  principal kinds.
- **One refusal.** A failed check answers the same single code for both kinds, byte for byte, so
  existence stays hidden. The reason is logged and never sent.
- **The `user:` namespace** is built and parsed in one module, like `peer:` in
  `federation/remote.py`. `engines.parse_key` and `sessionlock.lock_path` refuse it, and no local
  consumer (lock, scanner, archive, runtime cleanup, mission adopt, notifications) accepts it.

## 4. Cross-store binding and the migration (frozen)

The identity store and `federation.db` are two SQLite files, so a person's grant spans both.

- **Order of creation:** (1) reserve the grant row in `federation.db` as `pending`, with
  principal `user:<user_id>` and the identity store's current rights generation in a new
  `bound_generation` column; (2) commit the
  authorization in the identity store (the membership ceiling is re-read inside its transaction);
  (3) flip the grant to `live`. A `pending` row is never served.
- **Recovery after a crash between the stores:** a `pending` row older than one minute whose
  identity half is absent is swept to `revoked` (`end_reason = 'unbound'`). A `live` row whose user
  is disabled, deleted or no longer within the membership ceiling is refused at use and revoked by
  the sweep. Either orphaned half is invisible and never served.
- **Ending is one-way:** disabling a user revokes that user's grants, one `grant_events` row each,
  and bumps the rights generation.
- **`grant_events.actor` migration:** today `CHECK (actor IN ('operator', 'system'))`. Schema 6
  admits `user:<id>` with the same format check as the principal. It follows the store's existing
  rule (`PRAGMA user_version`; a file from a newer build is refused, never written): a backup is
  taken first, and an older binary refuses the migrated file.
- **The `pending` state and the `bound_generation` column** are new in schema 6 (`bound_generation` is null for peer grants). `grants.state` gains `pending` beside the shipped `live`, `revoked` and `expired`, and the existing per-call check refuses it like any grant that is not `live`.
- **The index** `grants(principal, state)` is added in the same migration (§6).

## 5. Revocation, the runner protocol and launch provenance (frozen)

**Linearization.**
- **PTY:** the existing authority fence and byte-one admission rule stay. No permission
  withdrawal can commit between the authorization comparison and the first write.
- **API sessions:** a submit, approval or launch is admitted at a recorded exact-generation point
  immediately before the irreversible handoff (`session_input.admit_native`,
  `structured_runtime._admit_gate`), sharing the withdrawal fence.
- **Remote:** a revoke first closes control-plane admission and enters `revoking`. Only the owning
  runner can fence its own admissions and acknowledge the withdrawn generation. The UI reports
  `revoked` only after that acknowledgement, or after the bounded owner-side authorization
  provably expired. An unreachable runner stays visibly `revoking`, and uncertainty never
  authorizes new work.
- **Already admitted work** cannot be undone. A separately authorized stop or interrupt acts on
  exact runtime generations and reports uncertain outcomes honestly.

**Remote bounds** (starting targets): runner authorization TTL 60 s; clock skew tolerance 5 s, with
a request outside it refused; a replayed request id refused for twice the TTL. P4 freezes these
with its transport.

**The runner protocol.** A fixed verb list (`launch`, `stop`, `interrupt`, `submit`, `decide`,
`read_stream`, `revoke_ack`, `status`). Each request is authenticated, bound to one runner
audience, carries an expiry and a request id, and is validated against current authority and
generation. A client-supplied UID, HOME, cwd, engine binary or environment map never selects
authority; the runner derives each from its registered account and the project binding.

**Launch provenance.** Every launch writes a durable record: mode (`personal`, `legacy` or
`confined`), runtime account, project, backend policy version and runtime generation. A missing or
unverifiable record is `legacy`: unrestricted and administrator-only, across restart, upgrade and
rollback.

## 6. Cost on the hot paths (frozen design, starting-target numbers)

**The rights cache.** Each app process caches a person's login session and effective rights
(memberships plus grants), keyed by user and **rights generation**. A grant, membership or
revocation change bumps the generation. A request costs one generation compare and a set lookup;
a miss reads the store off the event loop. Nothing on the event loop does synchronous I/O for
authorization.

**Making a bump visible across processes.** The identity store writes the new generation to a
small generation file next to it, after the commit. Each process watches it (inotify, with a
`stat` fallback at the next check) and drops affected entries. This is never a per-frame database
read.

**Freshness is not only the generation.**
- **Expiry without a bump.** A time-limited grant can expire with no edit. Every cache hit and
  every unchanged-generation stream check also compares the cached `expires_at` with this
  instance's clock.
- **Maximum age.** Each cached entry carries the generation it was read at and a maximum age
  **no longer than half the stream-closing bound**. Past that age the next check re-reads the
  store, so a missed or coalesced watcher event can delay a revoke by at most the bound.
- **Dropping the cache.** On watcher error or reconnect, on process start (a restart begins empty)
  and whenever the authority store is unreadable. An unreadable store fails closed and never
  serves the cache.
- **The stream-closing bound** (starting target: 2 s). After a revoke, a person's open stream
  sends no further frame later than the bound. Streams check on every frame and at least once per
  half bound while idle, so an idle stream cannot outlive it. The bound limits **view** exposure
  only. The admission points (§5) and federation's final read before an answer read the fenced
  value, never the cache, so a stale cache never admits an effect.

**Lists** filter once per request: the person's visible set (projects they belong to, plus their
grant rows) is resolved once, and the full session list is filtered against it by set membership
before facets, counts and paging. There is never an `authorize()` call per session.
`grants(principal, state)` makes "what is shared with me" an indexed read.

**Fan-out** to several recipients runs with bounded concurrency and never blocks its producer.
Recipients come from the audience a decision or grant already names, never from a scan of every
user.

**Budgets** (starting targets, compared against main on the same host in the same run; §7):

| Path | Budget |
|---|---|
| `authorize()` warm | p99 under 1 ms in-process |
| `/api/sessions` with 2,000 sessions | p95 within 10% of baseline, for an administrator and for a member |
| Terminal input-to-echo, API-session token streaming | within 5% of baseline |
| Confined launch | at most 300 ms added at p95 |
| Egress path | at most 50 ms added to the first streamed byte at p95; no throughput loss (§7 rule) |
| Idle runner | under 60 MB RSS |
| New UI | lazy-loaded; at most 30 KB gzipped added to the main bundle |

## 7. Benchmark protocol (frozen before any candidate is judged)

Committed before the first measurement on the VM, so no candidate is judged by rules chosen after
seeing its numbers.

- **Baseline:** main at a recorded full SHA, unconfined, on the same VM, interleaved with the
  candidate (ABAB order) so drift hits both.
- **Fixtures:** synthetic only. 2,000 sessions over 20 projects, three users, both principal
  kinds. The list benchmark also asserts **correctness**: a member's result equals the set computed
  independently from memberships and grants, and never contains another member's Private session.
- **Warm and cold** are reported separately. Cold is the first call after process start with an
  empty cache; warm follows 10 discarded calls.
- **Samples:** launches n = 20 per arm after 3 discarded; requests and stream measurements n = 200
  after warm-up. **Raw samples are kept** in the evidence file.
- **Percentiles:** each budget in §6 names its own. The launch budget is the p95 of the
  difference from baseline.
- **"No throughput loss"** uses a fixed workload: a local mock endpoint replays a 2 MB response in
  4 KB chunks. A candidate passes when the 95% bootstrap confidence interval of its median
  throughput ratio to baseline includes 1.0 or lies above it, or its median is within 3% of
  baseline.
- **The mock endpoint is a lab-only egress allowance.** It exists only in the VM harness, is
  named in the evidence, and never becomes a loopback exception in a production policy.

## 8. Confinement (open until P0b)

**The launch boundary.** Today: the app → `native_containment.launch_argv`'s
`systemd-run --user` unit (`env -i`; `HOME`, `PATH`, `LANG`, optionally the user manager's
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`) → `agent_sessions.native_worker` (trusted; it
holds the app IPC and the state directory) → the `native_exec` trampoline → the vendor CLI from
`spawn_argv`, with its stdio pipes and MCP broker children.

| Candidate | Boundary | Mechanism |
|---|---|---|
| **A** | The vendor CLI child and everything it starts. The wrapper sits inside `spawn_argv`; the worker stays outside the sandbox, inside the unit. The child receives its stdio pipes, a rebuilt environment without the user-manager variables, and explicitly bound broker sockets. | bubblewrap (`--unshare-pid --unshare-ipc --unshare-uts`, `--die-with-parent`, `--new-session`, read-only system binds, the workspace bound read-write, a tmpfs home holding only the session's engine state and its one agent account) plus a Landlock ruleset and seccomp |
| **B** | The whole unit (worker and CLI). | systemd unit sandboxing on the `battlelab-native-<hex>` unit (`PrivateUsers=yes` with `ProtectHome`, `BindPaths`, `RestrictAddressFamilies`); the worker's state directory and app socket are bound in |

**Landlock alone does not meet A2 on a 6.8 kernel.** Landlock ABI 4 (Linux 6.8) restricts the
filesystem and TCP bind/connect. Scoping of signals and abstract UNIX sockets arrives with ABI 6
(Linux 6.12). So both candidates pair Landlock with a PID and IPC namespace.

**Egress** is measured two ways: a network namespace whose only route out is a runner-side
allowlisting CONNECT proxy, and Landlock TCP-connect rules. The evidence records which meets A5
and the §6 budgets.

**Exit.** P0b freezes one backend, version and configuration, and the supported-engine matrix
(`claude-api` first), from the evidence. If no candidate passes every envelope row and the
budgets, restricted execution stays blocked and the freeze says so.

## 9. The attacker matrix and who proves each row

A contract or model result **never** counts as a production proof. The owning phase's child
carries the row ID in its acceptance criteria and re-runs it against the real implementation.

| ID | Attack | In P0 | Production proof |
|---|---|---|---|
| A1 | Read or write outside the workspace, including symlink replacement and rename races on path ancestors | Envelope probe | P2; P3b for PTY |
| A2 | Signal or ptrace a same-UID sibling process | Envelope probe | P2; P3b |
| A3 | Reach the user's systemd manager or session bus, a native daemon, a dtach or other runtime socket; inherit a descriptor or variable it should not have | Envelope probe | P2; P3b |
| A4 | Read the identity DB, `federation.db`, the runner capability, other agent-account homes or a host control socket | Envelope probe, with canary files at the real paths (a canary stands at the identity DB's planned path) | P2 |
| A5 | Reach a loopback or LAN service by any path, including after a reconnect | Envelope probe | P2 |
| A6 | The engine still works: CLI start, its state directory, the vendor API host reachable | Envelope probe | P2 |
| R1 | Revoke races native submit or approval admission; the §6 freshness cases (expiry with no bump, a lost signal, coalesced bumps, a restart mid-stream, an unreadable store, a revoke during a cached stream) | Contract plus model test. Each case asserts the **denial** of the next effect or frame, not merely that the cache was invalidated. | P2 (local grants), P3a (shared `send`) |
| R2 | Revoke or hand-over races PTY byte one | Contract only | P3b |
| R3 | Revoke races a queued launch | Contract only | P2 |
| R4 | Remote revoke, reconnect and replay | Contract only | P4 |
| R5 | Restart or rollback with missing provenance, stale bindings or colliding native IDs | Contract plus model test of the provenance record | P2 (written at launch), P5 (upgrade, rollback) |

Each executed probe produces one evidence row with verdict `pass`, `fail`, `unsupported` or
`not-run`. The four stay distinct in every summary: an unsupported or unexecuted row is never shown
as a pass, and a model result is labelled as one.
