# Sessions

A BattleLab session is a real process on your host, running under a `dtach` PTY that BattleLab
owns. That single design decision is where most of the product's behaviour comes from.

## Close the tab, keep the work

The browser is a **viewer**, not the process. Closing the tab, losing Wi-Fi, putting the phone in
your pocket, or redeploying BattleLab itself does not stop the agent — the `dtach` master keeps
running and you reattach mid-stream. For the agent engines a reboot is survivable too, because
resume replays the engine's own conversation store.

## One writer, always

The invariant is `{engine}:{id}` ⇒ one `dtach` master ⇒ one writer, arbitrated by a single-writer
advisory `flock` per session key in a shared lock directory. Every attach resolves to exactly one
of three outcomes:

| Outcome | Meaning |
|---|---|
| **ATTACH** | A live master exists — join it. |
| **LAUNCH** | The lock was won — start the process. |
| **BUSY** | The lock is held elsewhere — someone else has it. |

Because the decision runs through a filesystem lock rather than in-process state, it holds **across
multiple app instances** on the same host. There is no double-resume and no double-write even if
you run two copies of BattleLab.

On LAUNCH the lock file descriptor is handed to the spawned `dtach` process, so the lock lives
exactly as long as the master and survives BattleLab restarting or being redeployed underneath it.

## Scroll-up is two mechanisms

Scrolling up in a session combines:

1. **The scrollback ring** — a raw-byte replay ring per session, capped by
   `AGENT_SESSIONS_SCROLLBACK_BYTES` (default 8 MiB, floored at 256 KiB). This is what a plain
   shell has, and all it has.
2. **The transcript renderer** — a console-style render of the engine's *own* saved conversation,
   which is what lets you scroll back past anything the ring ever held.

Transcript rendering is bounded on three axes so a huge session cannot exhaust memory:
`AGENT_SESSIONS_TRANSCRIPT_MAX_LINES` (default 20000),
`AGENT_SESSIONS_TRANSCRIPT_MAX_MESSAGES` (default 2000) and
`AGENT_SESSIONS_TRANSCRIPT_TAIL_BYTES` (default 8 MiB of the log's tail). Paging is
**width-independent**: `AGENT_SESSIONS_HISTORY_PAGE_TURNS` (default 50) is the cursor step, so a
page always consumes the same number of *turns* and the same cursor selects the same window at any
terminal width. The two line/byte caps on a page truncate the rendered output only and never move
the cursor.

## Archiving frees runtime, never history

Archiving a session reaps its **runtime footprint** first — terminates the `dtach` master and the
agent's process group, clears the scrollback and VT mirror, releases the owner lease, and unlinks
the stale socket under the single-writer lock — and only then records the archive.

Releasing the lock is the point: it means a later unarchive-and-open is a clean **LAUNCH** rather
than a **BUSY**. The on-disk transcript is preserved throughout, so unarchiving and opening
relaunches from history. Archive frees resources; it never destroys conversation.

Teardown is best-effort and never blocks the archive itself.

## Organising the list

Titles, project assignment, favourites and archive state all live in BattleLab's own metadata
sidecar, keyed by the engine-qualified id — never in the engine's store. Favourited (`sticky`)
sessions pin to the top of the list. `POST /api/sessions/archive-older` bulk-archives everything
past a cutoff.

Filtering (`q`, `project`, `engine`) is applied to the **full** archived-scoped set *before*
pagination, so `total` and "load more" describe the filtered result rather than a filtered view of
one page. The dropdown facets are computed over the full set *before* filters, so they keep listing
every option no matter how narrow the current filter is.

::: info Verified against
Commit `218cf3a` — `docs/session-handling.md`; `docs/reference.md § Runtime / storage`; `src/agent_sessions/sessionlock.py`; `src/agent_sessions/runtime_cleanup.py`.
:::
