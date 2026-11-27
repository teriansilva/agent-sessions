# Settings

Settings lives at `/settings`, one page per section, in six groups: **General · Appearance · AI ·
Agents · System · About**. On a desktop the groups and their sections are a sidebar; on a phone they are an index you
tap into. Every section has its own address, `/settings/<section>` — the id is shown beside each
heading below — so a link can open exactly the page it means. An old `/settings/ai-review` link
still works: it lands on Endpoint & model, or on the prompt it named.

Preferences are server-side state, not browser state: they live in
`~/.config/agent-sessions/prefs.json` (override with `AGENT_SESSIONS_PREFS`), so they follow you
between the desktop and the phone. That file is deliberately separate from the install root's `env`
— reinstalling replaces releases, not your preferences. The terminal font and text size are the
exception, along with the rest of Appearance: those are saved per device, so a phone and a desktop
can differ. A device with no choice of its own starts from the last one saved anywhere.

## General

### Profile · `profile`

- **Avatar**: an image (PNG, JPEG or WebP) shown in a small circle beside your messages in API
  sessions. Everyone who opens one of your API share links sees it too. Your browser crops it to a
  square and scales it to 256 px before uploading. **Remove** goes back to your initial on the
  accent colour. Visitors on a share link appear as their initial in a colour picked from the name
  they gave, and the agent appears as 🤖.
- **Display name**: the name visitors of your share links see on your messages instead of
  "Operator", and the letter on your disc when you have no image. Leave it empty to stay
  "Operator". Up to 40 characters; your login name is never shown to visitors.

### Session defaults · `session-defaults`

- **Compose box** — whether the compose bar starts **Auto** (open on touch, collapsed on desktop),
  **Open** or **Collapsed** when a session opens. Applies after the next reload.
- **Session list order** — **Recent activity** or **Creation date**. Favorites always pin to the
  top.

### Projects moved to HQ

Projects, folder discovery and the session overview are no longer in Settings: they live in
**HQ → Projects** (`/projects`), with everything else a project's setup needs. An old
`/settings/projects` link opens it. See [Projects](/guide/projects#project-administration-hq-projects).

## Appearance

How things look, split by what is being styled. `/settings` opens on Interface.

### Interface · `appearance`

How BattleLab itself looks: the frame, menus, buttons and panels.

- **Mode**: **Dark**, **Light**, or **Match device**, which follows your operating system's
  light/dark setting and switches the moment it changes, open terminals included. Each card shows a
  miniature of the app in that mode.
- **Motion**: how much the frame moves. **Match device** (the default) follows your device's
  reduce-motion setting: Reduced when it asks, Full otherwise. **Full** runs the ambient scanlines
  and pulses, the start-up sequence (about a second, once per visit; any key, click or tap skips
  it, and nothing waits for it), arrivals, streamed answers and transitions. **Reduced** keeps
  only short fades on state changes: no loops, no start-up sequence, no portrait video, steady
  status lights. **Off** stops everything that moves; the look stays complete. Motion never touches
  the terminal, the editor or the text you are typing, and never delays input. Saved on this
  device. Ambient motion also pauses on its own while the tab is hidden and after ten minutes
  without input.

  The **atmosphere** (the hex grid, scanlines, grain and slow sweep behind the panels) and the
  **start-up sequence** have no switches of their own: Motion decides both. The atmosphere moves
  only in Full and on the dark mode; Reduced and Off keep it as a still frame without the sweep, and
  Light shows only a static hex grid. The start-up sequence runs only in Full.
- **Accent**: the brand colour on buttons, highlights, selection and the terminal cursor, and the
  colour of the frame itself: the lit rail along each panel's top edge, the hazard notches, ticks
  and panel edges all take it, so a new accent recolours the whole frame at once. Where an accent
  would not read on a mode's ground (Yellow or Ice on light), those lines and accent-coloured text
  are mixed toward the text colour just enough to stay readable. Status lights keep their own
  colours whatever the accent. Amber (`#ffb000`) by default. Pick one of 16 presets, or a custom colour from the colour well or a hex
  value. **Reset** returns to amber.
- **Opening links**: how a session link from outside the app opens on this device: *Ask each
  time*, *Full screen* or *In map*. Desktop only; *Ask each time* resets a remembered choice. See
  [Opening links](/guide/sessions#opening-links).

### Terminal sessions · `terminal-appearance`

How every terminal looks. Every CLI agent and the plain shell share one terminal, so each of these
is one choice.

- **Colour scheme**: the ground, the text and the 16 ANSI colours agents paint with. **BattleLab**
  (the default, today's terminal), Solarized, Gruvbox, Catppuccin, One and GitHub follow the mode
  and switch with it. Dracula, Nord and Tokyo Night are dark only and stay dark in light mode.
  A card marked *Some colours low contrast* has at least one of its six main colours under 3:1
  on its own ground. That is measured, not a judgement, and it applies to BattleLab's dark
  variant too. The cursor always takes your accent.
- **Terminal font**: one face for every engine. Faces this device does not have are greyed out
  rather than quietly falling back.
- **Terminal text size**: decides how many columns the agent lays out against. Smaller text means
  a wider terminal, which is what a column-laid-out interface like opencode needs on a phone.

### API sessions · `api-appearance`

How the conversation in an API session reads: codex-api, claude-api, opencode-api and chat agents.
Only the conversation changes. Terminal sessions and the pane's own controls (header, composer)
keep the app's look. A live preview at the top shows every change before you leave the page.

- **Palette**: **BattleLab** (the app's own colours, your accent on your messages), Graphite,
  Solarized, Nord, Sepia or High contrast. Each has a dark and a light variant and follows
  Interface → Mode. Every text colour in every palette meets WCAG AA.
- **Background**: None, Grid, Dots, Ruled or Scanlines, a faint static pattern behind the
  messages that scrolls with them.
- **Text font**: System sans, Serif, Mono, or a custom font stack.
- **Code font**: the app's mono face, the terminal's font, or a custom font stack. A custom stack
  follows the same rules as the terminal font, and an unusable one is refused with the reason.
- **Text size**: 12–18 px, 14 by default. Buttons keep their full size.
- **Reset API sessions** returns everything to the default, which is exactly today's look.

## AI

### Endpoint & model · `ai-endpoint`

One OpenAI-compatible endpoint powers every AI feature: session review and recaps, handoff briefs,
auto-sort, the mission-control scan, the orchestrator and mission turns. **Connection** holds the
base URL and the key — stored server-side and never sent to the browser. **Model** picks the model
and how long one request may take. See [AI review](/guide/ai-review).

### Session review · `ai-session-review`

Periodic review on or off, how often it runs, and the list of sessions excluded from review (you
exclude one from its row actions in the sidebar). **Review now** on a session works whenever the
endpoint is configured, even with periodic review off. See [AI review](/guide/ai-review).

**Titles** — *Rename sessions with the reviewed title* (off). On, the title follows every review.
Off, every session keeps its own title (a manual rename still wins); the review still writes its
summary, recap and intervention flag, and auto-sort still reads them. Turning it back on restores
the reviewed titles at once. The default was *on* before this release. A value you saved is kept,
and a value saved by an older version counts as your choice: older versions stored the whole block
whenever you changed any of these settings. Where the new default is what turns renaming off,
sessions that already had a reviewed title keep it as their name. That is copied locally on the
first start, with no request to the endpoint.

*Title new sessions once* (on). While renaming is off, each new session's first messages are sent
once to the AI endpoint, in one request, even with periodic review off. The title that comes back
is kept: later reviews never change it. Only the title is stored; that request writes no summary,
recap or intervention flag. Without a configured endpoint nothing is sent, and the switch is
unavailable while renaming is on or no endpoint is set. Sessions that existed when this version
first started are left as they are.

**Reading sessions** sets how deeply a session is read before a decision, and whether you are told:
*Recognise questions and choices* (on), *Decision context* Standard | Deep (Standard), and *Notify
me when a session needs me* (on). A notification is taken back when the session stops needing you,
with one exception described under [Notifications](/guide/notifications). See [HQ](/guide/hq#settings).

### Auto-sort · `ai-auto-sort`

When enabled, sessions that belong to no project are classified against your existing projects and
assigned automatically — but only when the classifier clears the confidence floor, and never a
session you assigned yourself.

### Session clusters · `ai-clusters`

HQ groups each project's sessions by topic, with the AI endpoint from *Endpoint & model*. On by
default; it does nothing until an endpoint is set.

- *Group sessions into clusters* (on): shows the clusters in the sidebar's project tree and the
  cluster on each session row. Off hides them (the tree still lists your projects and missions)
  and stops the sweep; your clusters are kept and come back when you turn it on again.
- *Sessions see their cluster* (on): agents get the BattleLab cluster tool from their next launch.
- *Re-cluster*: every 15 minutes, 30 minutes (default), hour or 4 hours. Only projects whose
  sessions changed are sent to the model.
- **Re-cluster now** looks at every project at once.

See [Projects](/guide/projects#session-clusters).

### HQ · `ai-hq`

One home for HQ, with links to each group. Old `ai-mission-control`, `ai-hq-calls` and
`ai-hq-connectors` addresses redirect to the matching group here. The former Appearance contact
link, `/settings/appearance#hq-contact`, also redirects here.

- **Contact** — who answers in [HQ](/guide/hq#hq-contacts): Random (a new contact each day), one
  of the thirteen contacts, or Off. Arrow keys move and select. This choice follows you across devices.
- **HQ models** — model and thinking choices for typed turns, question routing, calls and mission conversations. Each field saves separately on the shared AI endpoint. **Inherit** follows the enclosing call when present, otherwise **Endpoint & model**; a typed-only choice does not become the default for the other rows. Source labels show the rule beside each field. **Auto · provider policy** is distinct from Inherit. Typed/mission rows inherit by default; routing/calls keep thinking Off. A model-list failure preserves saved choices and offers **Reload model list**. A failed save keeps the last confirmed value and offers **Reload settings**.
- **Question routing** — enables the session-search classifier. On endpoints without tool support, routing skips the session search for general questions.
  With routing off, every message searches. Tool-capable endpoints keep their tool-routing path.
- **Autonomy** — two independent policies, **Session assistance** for sessions no active mission
  holds, and **Mission orchestration** for mission work. Each has its own enable switch, Off / Suggest /
  YOLO tier, confidence, notifications and nudge. Changing one does not grant anything to the other.
  Session assistance has **Run now**, the pass interval and idle window. Mission orchestration has the
  judgment threshold and the explicit opt-in for AI-written directions. Plan approval and
  mission-specific permissions still apply. Mission agents never skip tool prompts; the agent
  default for sessions you start cannot grant bypass to missions.
- **Session reports** — whether sessions may report their status to HQ.
- **Recent window** — how far back the Needs you list, the work recap and HQ look (1–3 days).
  The background session scan, its depth and **Scan now** were retired; nothing showed what it wrote.
- **Mission checks** — the existing **Forge connection** for reading PRs, checks and reviews.
  Its token stays server-side. An unavailable reading is unknown, not failed.
- **Calls** — speech services, local speech runtime, each General's voice, language, turn-taking,
  interruption, captions and where the audio goes. See [Calling HQ](/guide/calls).
- **Connectors** — the services [HQ reads from](/guide/hq#connectors), including Forgejo, GitHub,
  Nextcloud, Telegram, Matrix and Email. Each keeps its address, write-only credentials, test,
  read permission, recipient selection and messaging opt-in. Changing the address requires the
  token again; changing the bot's identity revokes the messaging opt-in.

Autonomy changes save with the revision you loaded. A concurrent change reloads the latest policy
and asks you to review it; it never repeats the rejected edit. An unreadable policy disables that
editor until a successful **Reload settings**. There is no fallback that combines the two policies.

**Judge an objective met at or above** stays within 0.90–1.00. Confidence is the model's own opinion;
a judgment can at most move a mission to review. You still close it.

### Checklists (removed)

The old `/settings/ai-playbooks` and `/mission/checklists` addresses redirect to
**Library → Playbooks**. See [missions](/guide/missions#where-objectives-come-from).

### Prompts · `ai-prompts`

Every system prompt BattleLab sends, with the JSON shape each one must return and **Reset to
default**. **HQ contacts** holds each General's voice, labelled by name, and **HQ — conversation**
is HQ's own prompt. The list of prompts is in the [Reference](/reference/).

### Activity · `ai-activity`

What AI work is running right now and the last run of each kind — the mission control scan, AI
review, auto-sort, orchestrator passes, the mission supervisor, objective checks, mission questions
and mission chat. Two scans of the same kind never overlap: one started while the first is still
running is refused, not queued (a manual scan answers 409).

## System

### All agents · `agents`

One row per agent CLI: whether it is **Ready** or needs you (**Needs setup**, **Update
available**, **Not installed**, **Unavailable**, **Disabled**, **Retiring**, **Invalid**), the ways
it can run (Terminal, API, Chat — each links to that agent's page), and what it has spent. When
something needs you, the row carries the one button for it. **Needs attention** shows only those
rows; **Add agent** sets up a new one. Its picker offers only the agents this host can install;
any it can't install are greyed out and listed under it with the reason. The included API
agents are named there rather than offered; one whose source agent is missing and can't be
installed here says which agent it needs first. The public catalog is the line under the list; open **Catalog details** for its links, history and the daily check.

An agent's page starts with its status, the one thing to do if any, and a **⋯** menu for reload,
disable and remove. Below are its usage (a percentage marked *plan* comes from the agent itself,
one meter per quota window with its reset time, the window that runs out first outlined; the rest
count against a token limit you set here), its models, its installation in one line with
the verification evidence behind it, and **Technical details** folded away. Kimi Code reports its
plan quota from version 2.1 on, so a counter you kept for it by hand is no longer used — update
kimi if its row shows an error instead.

For an agent that holds several accounts (Claude Code, Codex), **Usage** shows the account new
sessions start on: its name, the email the agent reports for it, and its plan. **Accounts** lists
every account, the Default login included, with its email and a meter per window; choosing another
account for new sessions switches **Usage**, the dashboard and the usage alerts to that account.
Every account is asked on the usual 15-minute sweep, or when you press Refresh.

**Agents › Defaults** holds the default agent, the permission-bypass default, and **Usage
alerts**: the **Alert at** threshold and whether you are notified.

An API client's page (Codex — API, Claude — API, opencode — API) has **New sessions**: the
model and reasoning effort a new session of that client starts with. Both come from the
client's own model list, and an effort is offered only for a model that has levels. They are
preselected on the new-session screen, where you can still change them for one session (**Reset
to defaults** brings them back); a running session keeps what it started with. If the client
stops offering a saved model or level, the page and the new-session screen say so and start
with the client's default instead.

### Security · `security`

**Password** — change the password you sign in with: your current password, then the new one
(at least 12 characters) twice. Devices already signed in stay signed in. **Two-factor
authentication** — enrollment, recovery codes, and turning it off, which needs a current code or
your password — and **Account**, with **Sign out**. With login off
(`AGENT_SESSIONS_AUTH_MODE=none`, as in Home Free) there is nothing to manage, and the page explains
that instead. See [First login](/start/first-login).

### Peers · `peers`

Pair this BattleLab with another one and see the instances it is paired with: **Create invite**
or **Accept an invite**, compare the six-digit code on both screens, then rename a peer, see
whether it is connected, and revoke it. Under each peer are the sessions you share with it
(**Revoke**) and the sessions it shares with you (**Leave**). Pairing shares nothing by itself.
See [Working with another BattleLab](/guide/federation).

### Updates · `updates`

The running version, **Automatic updates** — on by default: BattleLab and the agents you installed
from the catalog update themselves daily, BattleLab through the same rollback-guarded installer as
**Update now**. **Pause** (it asks first) stops both until you press **Resume**; the catalog is
still checked while paused. Then the release channel — **stable** (tagged releases) or **main**
(the development branch) — and the manual update control. Pause and the channel persist
server-side and apply immediately. See [Update & rollback](/start/update) and
[Agent catalog](/guide/agent-catalog).

### Usage analytics · `analytics` {#usage-analytics}

Whether this install sends the BattleLab team a daily **active-install report**, so they can see how
many installs are in use. It is **off unless you said yes**: setup asks as its last step before
Launch, with the box unticked, and an install that existed before this setting starts off and is
not asked.

When on, the report goes out on days you open BattleLab — at most one per day, with up to three
delivery attempts — as one request to the team's self-hosted Umami at `analytics.superstatus.io`.
It carries exactly:

| Field | Value |
|---|---|
| Install ID | a random UUID created on this machine when you agree — not derived from the machine, your account or your network |
| Version | the BattleLab version, e.g. `0.21.0` |
| OS | the operating system name, e.g. `Linux` |

Nothing about your sessions, prompts, code, files, projects, hostnames or account is sent. Umami uses
the connecting IP address to estimate an approximate location and does not store the address; the
web server in front of it keeps standard access logs, which include it, for up to 52 days.

Switching it off deletes the install ID and stops future reports; a report already under way may
still arrive. Switching it back on creates a new ID, and if today already had a delivery attempt its
first report waits for the next day. `AGENT_SESSIONS_ANALYTICS=0` turns it off for the whole server —
the toggle is then disabled and says why.

### Host · `system`

The host's OS, platform, CPU and load, memory, disk, uptime and Python version.

### Resources · `resources`

**Session limit.** How many agent sessions can run at once, sized for this machine. BattleLab reads
the host's memory and CPUs (and any container limit on them) and recommends the lower of two
figures: 1 GiB per session after keeping 10% of memory (at least 2 GiB) for the system, and 6
sessions per CPU. You can set anything from 1 to 256. While you haven't saved a value, the limit
follows the recommendation.

A session counts while its agent runs, whichever way it was started: terminal or API, by you, a
mission, an automation or HQ. An agent's own subagents don't count, and neither does a session that
has ended. At the limit a new start is refused. Archive a running session, wait for one to end, or
raise the limit. An automation step that would start a session waits in the automation queue
instead and starts on its own when a slot frees (see [Automations](automations.md#the-automation-queue)).
Nothing that's running is ever stopped by this limit, including when you lower it.

**Reserved for automations** (beside the limit; at least 1, default 1) keeps that many of the slots
for automation steps. Other starts (yours, a mission's, HQ's) can use the rest, "Other starts can use
5 of 6"; an automation step can use any free slot. If the reserve equals the limit, only
automations can start sessions. A reserve above a limit you later lower reserves the whole limit.

**API session memory limit** sets the combined memory allowance for an agent and its tools,
in GiB (default **8**, range **1–1024**). Reaching the limit kills one of that session's
processes, usually the largest tool; the agent usually keeps running and sees a failed command.
Choose an allowance that fits your workload and server. A lower host limit still takes precedence.

**Advanced process and thread limits** contains the console/API task budgets and background
library thread default. Explicit host overrides are shown there.

**Save limits** applies to new launches. Running sessions keep their existing limits and are
not restarted. **Restore recommended** fills in defaults; save to apply them.

### Maintenance · `maintenance`

- **Archive old sessions** — archive every session untouched for longer than a number of hours.
  Archived sessions are hidden from the list and can be unarchived; nothing is deleted.
- **Scrollback cache** — how much terminal history is cached on disk, and a control to clear it.
  Clearing drops only the cache; sessions and their transcripts are untouched.
- **Prune** — remove BattleLab's own leftovers, never session history. Each cache shows a dry-run
  count and size, and nothing is removed until you confirm. **Ended agent worker records** are the
  state files API-agent workers leave behind: only a worker proved stopped, that no session still
  names, is removed. Anything BattleLab can't prove (a session record it can't read, systemd not
  answering) is kept, and the result lists what was skipped and why.

## About · `about`

The version, the licence, a link to the source code, a contact address and a way to support the
project.

::: tip A setting that appears not to save
Preferences are written server-side immediately. If a panel appears to lose a value on remount, the
cause is a stale client-side copy of the configuration rather than a failed write — the value is on
disk. Reloading the page shows the true state.
:::

::: info Verified against
Commit `8b1c66b` — `web/src/routes/settingsTabs.ts § SETTINGS_GROUPS, SETTINGS_SECTIONS, legacySettingsTarget`; `web/src/routes/Settings.tsx`; `web/src/routes/{AiEndpointSetup,AiReviewSettings,AutoSortSettings,OrchestratorSettings,PulseSettings,ForgeSettings,MissionPlaybooks,PromptsSettings,AiActivityPanel}.tsx`; `src/agent_sessions/prefs.py § THEMES, DEFAULT_ACCENT, COMPOSE_DEFAULTS, store path`; `web/src/routes/AppearanceInterface.tsx § MOTIONS`; `web/src/theme/motion.ts`; `web/src/theme/accent.ts § hudLineFor, accentTextFor`; `web/src/components/hud/nightjar.css § .nj-atmo`; `web/src/components/shell/useBoot.ts`.
:::
