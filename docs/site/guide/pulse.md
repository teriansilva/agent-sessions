# Mission control

Mission control answers one question: **what needs me right now** — and then lets you do something
about it without opening a terminal.

It is the `/pulse` route, and it used to be a dashboard: a grid of session cards, a "state of your
work" banner, and a separate Ask box. That told you what mattered and then handed you off to a
terminal to act. The console replaces all three with a shape you already know — a rail of missions
on the left, one thread in the middle, and the mission's own detail beside it.

## A mission is the unit of work

A **mission** is an objective list, a state, one or more sessions, and an ordered event log, kept
in its own database. It is what turns "I have eleven sessions open" into "I have three pieces of
work, and one of them is waiting on me".

Missions have a small lifecycle, and the console only ever offers you the transitions the server
actually allows:

| State | Means |
|---|---|
| `draft` | Started from an instruction. May not have a project yet. |
| `planned` | Decided on. Ready to begin — but nothing has been launched yet. |
| `dispatching` | Being started: the mission is acquiring the session it will run in. |
| `running` | Work is underway. |
| `review` | Every gate is met — the console thinks it looks finished and is asking you. |
| `done` · `failed` | Closed. Can be reopened. |
| `abandoned` | Closed, and terminal in the strong sense — **not** reopenable. |

`abandoned` is the only one you cannot undo, which is why the console asks twice before it.

## Starting one

**NEW MISSION** is a mode of the composer rather than a pop-up dialog, so it is reachable from
every state the page can be in — including a completely fresh install where there is nothing else
on screen yet.

You give it an instruction and a project. The project matters: the server resolves the mission's
working directory from it, and a mission with no project stays a `draft` — legal, but it cannot
start work, and the form says so before you press START rather than letting you find out later.

Objectives arrive a moment after the mission does; see below.

## Adopting a session you already started

Work started from the sidebar is not orphaned. Live sessions no mission owns are listed under
**UNTRACKED**, and **ADOPT** attaches one to a mission.

A session belongs to at most one mission at a time. Adopting one another mission already holds
comes back with a refusal that **names the holder**, so you are told where it went rather than just
"no".

Once a mission holds a live session, **BEGIN** moves it to `running`. That path exists so a session
you started yourself does not have to pretend it was dispatched — the timeline should not contain a
launch that never happened. It is refused until the mission actually holds a session, because a
running mission with nothing in it has nothing to follow through on.

## Objectives — what "done" means, decided before the work

A mission carries a checklist, and the point is that it is written **before** the work rather than
argued about after. "The agent said it's done" is a report about one turn; an objective list is a
set of facts.

Objectives come from a **playbook** — an operator-authored template list. The shipped
`Ship a change` playbook gates on: a branch exists, a PR is open, checks are green, it has been
reviewed, it is merged.

You can edit the list at any time: add, rename, reorder, remove, or mark one **NOT REQUIRED**.

That last label is deliberate and worth understanding. Marking an objective not required is a
decision that it did not *need* to hold — it is **not** a claim that it does. The console cannot
mark an objective met, and neither can a model: only an observation can, which is the next section.

### The four meanings of an empty list

A mission is created before its objectives exist, so an empty checklist is not evidence that there
are none. The pane says which case you are in: still being worked out, none proposed, could not be
produced, or genuinely empty.

## How an objective becomes met

Objectives are settled by **probes** — a read-only check the server runs. A probe is one of three
answers, and the third is the one that matters:

- **it holds** → the objective is marked met, with the fact that settled it kept on the row;
- **it does not** → nothing is marked; the supervisor has something to act on;
- **we could not look** → **nothing is settled.** The row keeps its last observed state, is shown
  as stale, and names the reason ("forge unreachable").

That third case is why a forge outage does not look like your work going backwards. An objective
nobody could check has not been shown to be unmet.

Probes that talk to a forge need a [forge connection](/guide/settings); without one they answer
*unknown* rather than failing. Probe targets can only ever come from a playbook a person typed —
there is no path from model output to an address the server will fetch.

## Follow-through — the part that nudges

The supervisor is what the console is *for*: agents stop on mundane things, a check goes red, a
session stalls, and nothing picks it up.

It runs on a timer over live missions and does two things. The mechanical half always runs and
costs nothing. The model half is skipped entirely when nothing the session wrote has changed, so an
idle mission is free.

Every objective shows why the supervisor is or is not acting on it:

| Board | Means |
|---|---|
| **READY** | It may nudge this next pass. |
| **WAITING** | A nudge is in flight, or its fate is not yet known. |
| **HELD** | You stood this one down. Quiet on purpose. |
| **SPENT** | The nudge budget for this episode is gone — it needs you. |
| **UNKNOWN** | The ledger could not be read. Not the same as "spent". |
| **MET** | Settled. |

The budget is per objective and resets when that objective's own state changes, so a mission that
makes progress gets a fresh allowance rather than being throttled by a run it has already left
behind. **STAND DOWN** silences one objective; it does not settle it, and the objective stays
visibly unmet.

When every gate is met the supervisor proposes completion and **closes nothing** — it quotes the
list back and the call is yours. It says which objectives were *observed* to hold and which you
waived, because those are different claims.

## The timeline

Every mission keeps an append-only event log: your messages, the model's replies, decisions,
probes, state changes. It pages oldest-on-demand, and it is the answer to "what happened here".

## Archiving

Archiving a mission stops its agents and frees their terminals. It is **destructive of runtime,
never of history**: every transcript is kept, and UNARCHIVE brings the mission back. A live mission
has to be abandoned first, and the console says so before you confirm.

## What the console costs

The scan behind the UNTRACKED list has three depths, and they differ in how much model work they
do — not in how much they see.

| Depth | Model calls | What you get |
|---|---|---|
| **`fast`** (default) | **none** | Ranking and flags from the per-session AI-review summaries already on the sidecar. Free. |
| **`medium`** | **one** per scan | Adds a synthesised summary line. |
| **`slow`** | one, plus a bounded per-session pass | Adds a one-line "state + next step" per session. |

`fast` does no LLM work at all — it re-uses summaries [AI review](/guide/ai-review) already
produced. If you have not configured an AI endpoint, stay on it; it is the default for exactly that
reason.

At `medium` or `slow`, an unavailable endpoint makes the scan **degrade to `fast` curation** with
an explicit `synthesis_skipped` marker rather than failing the page. The `slow` pass is bounded and
serialized — at most **12 sessions** per scan, 1 second apart — a deliberate ceiling on what one
scan can cost you against a metered endpoint or a slow local model.

`window_days` controls how far back it looks (default **3**, allowed 1–30); anything active in the
last 24 hours counts as recent. Results are cached, so the page opens instantly and the scan runs
behind it.

## Without an AI endpoint

The console still works. Missions, adoption, objectives, the timeline and approvals all run with no
model at all. What goes quiet is suggestion, recaps, progress judgements and completion proposals —
and the page says so rather than looking broken.

## On a phone

Three stops: **THREAD** · **OBJECTIVES** · **TIMELINE**, with the mission rail as a drawer. The
mission's own controls sit above the content on every stop, because closing a mission from the
timeline is as reasonable as closing it from the thread.

## What moved, and what did not

The card grid, the "state of your work" banner and the standalone Ask box are **gone**, not
relocated:

| Was | Is now |
|---|---|
| A session card in the grid | A mission row in the rail |
| The banner | The mission's own recap stream |
| The Ask box | The composer, in the thread |
| A session no mission owns | **UNTRACKED**, with ADOPT |

Deliberately unchanged: the **terminal**, the **sidebar**, and the **per-session recap** — which is
more useful, not less, when one mission is driving several sessions. Jumping into a session and
typing at it directly is always available; it just stopped being the only way.

::: info Verified against
`src/agent_sessions/missions.py § STATES, _ALLOWED, PROBE_KINDS`;
`src/agent_sessions/mission_supervisor.py § NUDGE_BUDGET, assess`;
`src/agent_sessions/mission_probes.py`; `src/agent_sessions/prefs.py § DEFAULT_MISSION_PLAYBOOKS`;
`src/agent_sessions/pulse.py § SCAN_DEPTHS, DEFAULT_DEPTH, WINDOW_DAYS_*, SLOW_SESSION_CAP`;
`web/src/routes/Pulse.tsx`, `web/src/components/pulse/`.
:::
