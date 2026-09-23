# Mission control

Mission control answers one question: **what needs me right now** — and then lets you do something
about it without opening a terminal.

It is the `/mission` route (`/pulse` redirects there), and it used to be a dashboard: a grid of session cards, a "state of your
work" banner, and a separate Ask box. That told you what mattered and then handed you off to a
terminal to act. The console replaces all three with a shape you already know — a rail of missions
on the left, one thread in the middle, and the mission's own detail beside it. Ask was not folded
into the console: it is its own section at `/ask`, first in the top bar's section nav.

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
| `review` | At least one objective is required, and every required one is met (or you marked it not required) — the console thinks it looks finished and is asking you. |
| `done` · `failed` | Closed. Can be reopened. |
| `abandoned` | Closed, and terminal in the strong sense — **not** reopenable. |

`abandoned` is the only one you cannot undo, which is why the console asks twice before it.

## Starting one

Entering Missions opens on the new-mission page: the brief is the centre of the screen, and
anything that needs you is listed below it. A
**Template** control inserts a template from your library into the brief; nothing is sent until you
start the mission. See [Templates](/guide/templates).

The new-mission page is where Missions lands whenever no mission is selected, not a pop-up
dialog, so it is there in every state the page can be in — including a completely fresh install
where there is nothing else on screen yet. **+ New mission** at the top of the rail brings you back
to it from any mission and puts the cursor in the brief.

You give it an instruction and a project. The project matters: the server resolves the mission's
working directory from it, and a mission with no project stays a `draft` — legal, but it cannot
start work — so **Start mission** stays disabled until you pick one, rather than letting you find
out later.

If you have any checklists configured, the same row has a **Checklist** picker: your default is
preselected (No checklist, if you have not set a default), you can pick another, or pick **No checklist**. The line under the box says what the
choice means — the chosen checklist's objectives, or, for No checklist, that the mission can never
confirm itself finished.

Objectives arrive a moment after the mission does; see below.

## Adopting a session you already started

Work started from the sidebar is not orphaned. Adopt it from the session itself: the **⋯** menu on
its sidebar row, or the session pane's header, offers to adopt the session into a mission. A session
a mission holds carries that mission's tag in the sidebar, and **Filter by mission** narrows the
session list to one mission's sessions.

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

Objectives come from a **checklist** — an operator-authored template list, edited in **Settings →
AI → Checklists** and chosen per mission on the new-mission form. The shipped `Ship a change`
checklist gates on: a branch exists, a PR is open, checks are green, it has been reviewed, it is
merged.

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
*unknown* rather than failing. Probe targets can only ever come from a checklist a person typed —
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

### Directions

A nudge types your text, never the model's. By default that is the global nudge from Settings. An
objective can carry a **direction** instead: text you write on the checklist template, or for one
mission, such as `PR #{pr} checks are {checks} on {branch}. Open the failing check and fix it.`

- **Placeholders are a closed list.** `{pr}`, `{pr_state}`, `{checks}` and `{review}` come from
  that objective's own latest probe observation; `{repo}` and `{branch}` come from its probe
  arguments. An unknown placeholder, or one the objective's probe cannot fill, is refused when you
  save. Probe detail text, titles, the checkout's own branch and session content are never used.
- **Copied, not linked.** A mission copies the template's direction when its objectives are
  created. Editing the checklist later changes no running mission; you can reset one to the
  checklist's current direction, write your own, or clear it.
- **Never half-filled.** If a fact is missing, stale, or was observed for a different target or
  head, the nudge is held and the mission asks for you. It is not sent with blanks, and it does not
  fall back to the global nudge.
- **What was shown is what is sent.** A proposal records the exact text and what it is about: the
  objective, the probe target, and each fact's value, PR and head. If any of those has changed by
  the time it is approved or sent automatically, including an edit to the global nudge, it is not
  sent. A re-probe that finds the same facts changes nothing, so a proposal waits for you as long
  as its facts hold and are fresh. A nudge without a direction never goes stale on a re-probe.
- **Where you write one.** In **Settings → AI → Checklists**, each objective has a **Direction**
  field. Tap a fact chip to insert a placeholder that objective's check can fill. The preview shows
  the text filled with example facts, and an unknown placeholder is refused there in the same words a
  save uses. For a single mission, use an objective's ⋯ → **Edit direction**. You can keep the copy,
  reset it to the checklist's current direction, write your own, or have none. Objectives with a
  direction carry a small *direction* mark.
- **What you approve is what is typed.** On Suggest, the decision shows **Will type**: the exact
  text, the facts it was filled with and when they were checked. The AI's *Why now* is kept
  separate. If the text is no longer true, the decision says **not sendable** and offers only
  Dismiss. The thread records a **Nudged** row for each nudge that was sent, with **Show text** for
  exactly what was typed, and a **Held** row for a nudge that was not.
- **Drafts from the AI.** For an objective without a direction, mission control may draft one from
  the session. It shows as a dashed **AI-drafted direction** card and, by default, waits for your
  tap. **Send as written** types exactly the text shown. **Edit** puts the text in
  that session's message box under the mission's **Context**, where sending it replaces the draft
  and it goes as your own message. **Dismiss** drops it. A draft you send counts against the
  objective's nudge budget like a nudge does; your own edited message does not.
- **The one exception, off unless you turn it on.** On **YOLO** only, **Let mission control send
  AI-written directions on its own** (Settings → AI → Mission control) lets a draft the model rates
  at or above a threshold you set be sent with nobody reading it first. The threshold cannot go
  below **0.90**; below it a draft still waits for your tap. At most one such send happens per
  objective attempt, every send is announced as a notification ("sent automatically by mission
  control, without review"), and the thread row for it carries **Turn off AI-written directions**, one tap to switch the mode off.
  Leaving YOLO turns it off too. Confidence is the model's opinion of its own work, not a safety
  check — the settings page says so beside the switch.

When at least one objective is required and every required one is met or marked not required, the
supervisor proposes completion and **closes nothing** — it quotes the list back and the call is
yours. It says which objectives were *observed* to hold and which you waived, because those are
different claims.

"At least one" is the point. A mission started with **No checklist** gets notes-only objectives:
nothing on it can be checked and nothing is required, so it is unmeasured rather than finished, and
it never proposes its own completion. You close it. Objectives you add to its list later from the
console are notes too — the add row takes a title, not a check — so they do not change that.

## The timeline

Every mission keeps an append-only event log: your messages, the model's replies, decisions,
probes, state changes. It pages oldest-on-demand, and it is the answer to "what happened here".

## Archiving

Archiving a mission stops its agents and frees their terminals. It is **destructive of runtime,
never of history**: every transcript is kept, and UNARCHIVE brings the mission back. A live mission
has to be abandoned first, and the console says so before you confirm.

## What the session scan costs

Mission control keeps a **Sessions without a mission** list current with a session scan. The scan
is configured under Settings → AI → Mission control → **Session scan**, and it has two depths that
differ in how much model work they do — not in how much they see.

| Depth | Model calls | What you get |
|---|---|---|
| **`fast`** (default) | **none** | Ranking and flags from the per-session AI-review results already on the sidecar. Free. |
| **`slow`** | one per session, at most **12** per scan | Adds a one-line "state + next step" per session. |

`fast` does no LLM work at all — it re-uses what [AI review](/guide/ai-review) already produced. If
you have not configured an AI endpoint, stay on it; it is the default for exactly that reason.
There used to be a `medium` depth; it was removed, and a stored `medium` now reads as `fast`.

At `slow`, an unconfigured endpoint makes the scan **degrade to `fast` curation** with an explicit
`synthesis_skipped` marker rather than failing the page, and a session whose own call fails keeps
its AI-review summary line. The `slow` pass is bounded and serialized — at most **12 sessions** per
scan, 1 second apart — a deliberate ceiling on what one scan can cost you against a metered
endpoint or a slow local model. Where a session has a recap, the `slow` pass hands the model the
recap rather than the one-line summary.

`window_days` controls how far back it looks (default **3**, allowed 1–30); anything active in the
last 24 hours counts as recent. Results are cached, so the page opens instantly and the scan runs
behind it.

## Without an AI endpoint

The console still works. Missions, adoption, objectives, the timeline and approvals all run with no
model at all. What goes quiet is suggestion, recaps and progress judgements — and the page says so
rather than looking broken. Completion proposals do not go quiet: when at least one objective is
required and every required one is met or marked not required, the supervisor moves the mission to `review` mechanically,
before any model call.

## On a phone

The mission rail is a drawer. There are no tabs: the mission's objectives and timeline sit behind
one **DETAILS** disclosure above the thread, and opening it never moves the thread or loses what
you were typing. The mission's own controls stay above the content.

## What moved, and what did not

The card grid and the "state of your work" banner are **gone**, not relocated. The Ask box was
relocated — out of mission control entirely, into its own section:

| Was | Is now |
|---|---|
| A session card in the grid | A mission row in the rail |
| The banner | The mission's own recap stream |
| The Ask box | Its own section, `/ask` |
| A session no mission owns | Adopted from the session itself — its row menu or its pane header |

Deliberately unchanged: the **terminal**, the **sidebar**, and the **per-session recap** — which is
more useful, not less, when one mission is driving several sessions. Jumping into a session and
typing at it directly is always available; it just stopped being the only way.

::: info Verified against
Commit `f5611f0` — `src/agent_sessions/missions.py § STATES, _ALLOWED, PROBE_KINDS, gate_tally, gates_settled, _op_add`;
`src/agent_sessions/mission_supervisor.py § NUDGE_BUDGET, assess`;
`src/agent_sessions/mission_probes.py`; `src/agent_sessions/prefs.py § DEFAULT_MISSION_PLAYBOOKS, ORCH_AI_DIRECTION_CONF_*`;
`src/agent_sessions/actuator.py § draft_auto_allowed, _announce_auto_direction`;
`web/src/lib/routes.ts § ASK_PATH`; `web/src/components/shell/sections.ts`;
`web/src/components/pulse/{NewMissionForm,MissionConsole,MissionRail,MissionObjectives,MissionThreadEvent}.tsx`;
`web/src/routes/OrchestratorSettings.tsx`; `web/src/routes/settingsTabs.ts`;
`src/agent_sessions/pulse.py § SCAN_DEPTHS, DEFAULT_DEPTH, coerce_depth, WINDOW_DAYS_*, RECENT_ACTIVE_S, SLOW_SESSION_CAP, SYNTH_CALL_SPACING_S, run_scan`;
`web/src/routes/Pulse.tsx`, `web/src/routes/PulseSettings.tsx`, `web/src/components/pulse/`.
:::
