# Mission control

Mission control answers one question: **what needs me right now** — and then lets you do something
about it without opening a terminal.

It is the `/mission` route (`/pulse` redirects there), and it used to be a dashboard: a grid of session cards, a "state of your
work" banner, and a separate Ask box. That told you what mattered and then handed you off to a
terminal to act. The console replaces all three with a shape you already know — a rail of missions
on the left, one thread in the middle, and the mission's own detail beside it. Ask was not folded
into the console: it lives on the [Dashboard](/guide/dashboard) (`/dashboard`; `/ask` still works), first in the top
bar's section nav.

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
choice means — the chosen checklist's objectives, or, for No checklist, what happens instead. With
an AI endpoint configured the option reads **No checklist — AI writes the objectives**: the
orchestrator writes the objectives from your instruction and the supervisor judges them (see
[No checklist](#no-checklist) below). Without an endpoint it is notes only, and the mission can
never confirm itself finished.

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
merged. The shipped `Investigate` checklist gates on *a finding is written down*, which the
supervisor judges (below); *you have confirmed it* stays yours to settle and does not gate.

You can edit the list at any time: add, rename, reorder, remove, or mark one **NOT REQUIRED**.

That last label is deliberate and worth understanding. Marking an objective not required is a
decision that it did not *need* to hold — it is **not** a claim that it does. The console cannot
mark an objective met. Two things can: an **observation** (a probe the server ran) or a
**judgment** (the supervisor's independent reading, at or above your confidence setting, with the
evidence it quoted). A judgment can at most propose that a mission is finished; you close it. Both
are the next section.

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

### Judged objectives

Some objectives have no fact a server can fetch — *a finding is written down*, *every issue tagged
`type:bug` is closed*. For those the checklist uses the **Supervisor judges** kind
(`supervisor_judged`; older configurations say `agent_judged`, which still reads correctly).

- **Who judges.** The supervisor, in a separate model call with its own prompt. The working agent
  is never asked whether it is done: its own "I'm done" is something the judge reads, never
  evidence.
- **What it reads.** The mission's instruction, the objective, and for up to three of the mission's
  recently active sessions (by transcript activity, not screen activity) the transcript tail and the
  live screen, plus a bounded view of the checkout's uncommitted changes to tracked files. A new,
  untracked file is listed by name and size only — its contents are never sent. Every source is
  labelled.
- **Evidence is checked, not trusted.** The judge must quote what it read. A quote is kept only if
  it appears word for word in the source it names and is at least 20 characters and three words
  long (for Chinese, Japanese, Korean or Thai text, ten of those characters instead of three
  words) — a single word matches almost anything; a "met" with nothing verifiable counts as *not
  judged*. A judgment belongs to the objective's exact wording — its title and its direction. If
  you rename an objective or change its direction, the judgment made about the old wording stops
  counting at once (it is kept as history), a judgment still in flight is thrown away, and the new
  wording is judged on a later pass. For an agent that keeps a transcript the screen is context
  only — a quote from the transcript or the diff is needed. Quotes are stored and shown as plain text, with credentials in
  URLs removed.
- **The threshold.** A judgment counts as met only at or above **Judge an objective met at or
  above** in [Settings → AI → Mission control](/guide/settings) — **0.90** by default, and 0.90 is
  also the floor. The row reads **judged met (0.93)** with a `judged` tag, never *observed*; below
  the threshold it reads **judged not yet (0.62)**. **Show evidence** opens the quotes, the reason
  and when it was checked. Changing the threshold re-applies it to the judgments already made, on
  the next pass, without asking the model again.
- **It holds while the output is unchanged.** A judgment records a fingerprint of what it read.
  New session output marks it **stale** — it no longer counts — and a later pass judges it again
  while the mission is `running` (a mission in `review` is marked stale but not re-judged: the
  next move there is yours). Output that changes while the judge is still reading also counts as
  new: the judgment is marked stale at once. The
  live screen of an agent with a transcript is left out of that fingerprint, so a spinner or a
  clock on an idle session does not cost a new judgment.
- **Overruling it.** **Not met — judge again** (on the evidence, and in the row's ⋯ menu) puts the
  objective back to pending, and — on a mission in `review` — moves the mission back to `running`.
  The supervisor does not judge that same output again; it waits for new output.
- **Every objective gets its turn.** Required objectives are judged first, and within them the one
  judged longest ago (or never) goes first, so a mission whose output keeps changing still judges
  every objective in turn. Across missions, each sweep first checks every mission, then hands
  out its judge calls in turn: the mission that was last judged longest ago (or never) goes first,
  and a mission that just had its turn goes to the back, so busy missions cannot keep a quiet one
  waiting. Every attempt counts as a turn,
  including one that failed — and so does a turn where there was nothing to read (no session
  output and no changes in the checkout): that mission waits 30 minutes before it is first in line
  again.
- **Unknown is never met.** An endpoint error, a timeout, a reply that breaks the contract, or
  evidence that does not verify leaves the row not judged, with the reason. With no AI endpoint
  configured a judged row says it cannot be judged, and never settles.
- **Cost.** At most two judge calls per mission per sweep, six per sweep, and six more shared by
  the early readings of newly started missions between two sweeps; required objectives first, and
  none at all when nothing the judgment read has changed. Only a call to the model is counted: a
  mission with nothing to read, or nothing due, uses none of the six. A reply that breaks the
  contract, or evidence that does not verify, is not retried until the output changes; a timeout,
  an endpoint error or a missing endpoint is retried on the same output no sooner than 30 minutes
  later.
- **What a judgment can do.** Settle one objective. When every required objective holds, the
  mission is *proposed* for `review` at the next sweep (so up to about five minutes after the
  verdict, once it has been checked against the output again), exactly as with observations —
  nothing is closed, no text is typed, no address is opened. Session output is untrusted, and an instruction hidden in it can
  push the judge towards "met"; that is why the most a judgment can do is propose review, and why
  the evidence and the overrule are one tap away.

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
yours. It lists the required objectives by how they hold — **observed** (a probe saw it),
**judged** (the supervisor's reading, with its confidence) and **waived** (you marked it not
required) — because those are different claims.

### No checklist

"At least one" is the point. What a mission started with **No checklist** gets depends on whether
an AI endpoint is configured:

- **With an endpoint**, the orchestrator writes the objectives from your instruction — at most six,
  each a concrete outcome — and every one of them is a required, **judged** objective. If it writes
  none it can use, the mission gets one instead: **Done as you instructed**, judged against the
  instruction itself. So the mission is measured, and it is proposed for review when the judgments
  hold. The model only ever chooses the wording of these objectives: it cannot give one a probe or
  an address, and the most a judgment can do is propose review. You close the mission, or drop any
  objective you do not want judged.
- **Without an endpoint**, it gets notes-only objectives: nothing on it can be checked and nothing
  is required, so it is unmeasured rather than finished, and it never proposes its own completion.
  You close it.

Objectives you add to a list later from the console are notes — the add row takes a title, not a
check.

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
model at all. What goes quiet is suggestion, recaps, progress judgements and **judged objectives** —
a judged row says it cannot be judged, and never settles — and the page says so rather than looking
broken. Completion proposals do not go quiet: when at least one objective is
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
| The Ask box | [Ask](/guide/ask), in the middle of the [Dashboard](/guide/dashboard) (`/dashboard`) |
| A session no mission owns | Adopted from the session itself — its row menu or its pane header |

Deliberately unchanged: the **terminal**, the **sidebar**, and the **per-session recap** — which is
more useful, not less, when one mission is driving several sessions. Jumping into a session and
typing at it directly is always available; it just stopped being the only way.

::: info Verified against
Commit `f5611f0` — `src/agent_sessions/missions.py § STATES, _ALLOWED, PROBE_KINDS, gate_tally, gates_settled, _op_add`;
`src/agent_sessions/mission_supervisor.py § NUDGE_BUDGET, assess`;
#1088 — `src/agent_sessions/mission_judge.py`; `src/agent_sessions/missions.py § PROBE_ALIASES, observe_objective, judgment_counts, _op_reject_judgment, INSTRUCTION_GATES_MAX`;
`src/agent_sessions/mission_objectives.py § propose`; `src/agent_sessions/prefs.py § ORCH_JUDGE_CONF_*`;
`src/agent_sessions/mission_probes.py`; `src/agent_sessions/prefs.py § DEFAULT_MISSION_PLAYBOOKS, ORCH_AI_DIRECTION_CONF_*`;
`src/agent_sessions/actuator.py § draft_auto_allowed, _announce_auto_direction`;
`web/src/lib/routes.ts § ASK_PATH`; `web/src/components/shell/sections.ts`;
`web/src/components/pulse/{NewMissionForm,MissionConsole,MissionRail,MissionObjectives,MissionThreadEvent}.tsx`;
`web/src/routes/OrchestratorSettings.tsx`; `web/src/routes/settingsTabs.ts`;
`src/agent_sessions/pulse.py § SCAN_DEPTHS, DEFAULT_DEPTH, coerce_depth, WINDOW_DAYS_*, RECENT_ACTIVE_S, SLOW_SESSION_CAP, SYNTH_CALL_SPACING_S, run_scan`;
`web/src/routes/Pulse.tsx`, `web/src/routes/PulseSettings.tsx`, `web/src/components/pulse/`.
:::
