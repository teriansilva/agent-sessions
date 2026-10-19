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

### Session defaults · `session-defaults`

- **Compose box** — whether the compose bar starts **Auto** (open on touch, collapsed on desktop),
  **Open** or **Collapsed** when a session opens. Applies after the next reload.
- **Session list order** — **Recent activity** or **Creation date**. Favorites always pin to the
  top.

### Projects · `projects`

- **Projects** — group sessions across folders: a project adopts launch folders and can be assigned
  per session. Projects are metadata; assigning or archiving never moves session files.
- **Folder discovery** — root directories and excluded folders. A root is a hard scope: folders
  outside it are hidden from the sidebar, the filter and the pickers too.
- **Session overview** — which projects show: **Show all (hide a few)** or **Only included**.

These visibility controls are easy to confuse and do genuinely different things;
[Projects](/guide/projects) has the table.

## Appearance

How things look, split by what is being styled. `/settings` opens on Interface.

### Interface · `appearance`

How BattleLab itself looks: the frame, menus, buttons and panels.

- **Mode**: **Dark**, **Light**, or **Match device**, which follows your operating system's
  light/dark setting and switches the moment it changes, open terminals included. Each card shows a
  miniature of the app in that mode.
- **Accent**: the brand colour on buttons, highlights, selection and the terminal cursor. Amber
  (`#ffb000`) by default. Pick one of 16 presets, or a custom colour from the colour well or a hex
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

**Reading sessions** sets how deeply a session is read before a decision, and whether you are told:
*Recognise questions and choices* (on), *Decision context* Standard | Deep (Standard), and *Notify
me when a session needs me* (on). A notification is taken back when the session stops needing you,
with one exception described under [Notifications](/guide/notifications). See [Ask](/guide/ask#settings).

### Auto-sort · `ai-auto-sort`

When enabled, sessions that belong to no project are classified against your existing projects and
assigned automatically — but only when the classifier clears the confidence floor, and never a
session you assigned yourself.

### Mission control · `ai-mission-control`

Three panels, everything that tunes [Mission control](/guide/missions):

- **Orchestrator** — **Mission control may act on my sessions**. Off stops the scheduled pass, the
  mission supervisor, delivering nudges, and starting or dispatching missions. The autonomy tier is
  **Off**, **Suggest** (every action waits for your approval) or **YOLO** (acts without asking above
  the confidence threshold). **Run now** runs one pass immediately. **Judge an objective met at or
  above** is the confidence a [judged objective](/guide/missions#judged-objectives) needs before it
  counts as met: 0.90 to 1.00, default 0.90, saved when you let go of the slider. 0.90 is the floor
  and cannot be lowered. Confidence is the model's own opinion, so a judgment can at most move a
  mission to review; you still close it. It also states whether missions run with permission
  bypass — that is the **Permission bypass for new sessions and missions** default under **Agents ›
  Defaults**, read as each mission agent starts. Scheduled mission automations always keep
  bypass off regardless of this default.
- **Session scan** — keeps the **Sessions without a mission** list current: a background scan and
  its interval, how many days back it looks, the scan depth (**Fast** or **Slow**), and **Scan
  now**.
- **Forge connection** — the forge (Forgejo, Gitea or GitHub) where mission objectives such as "a PR
  is open" or "checks are green" are checked. Read-only; the token is stored server-side. Without
  one, forge objectives read as *unknown* rather than failed.

### Checklists (moved to Missions)

The checklists a new mission starts with are no longer a Settings page: they live under **Library →
Checklists** (the chevron beside Library in the top bar), beside templates, automations and playbooks — see
the [missions guide](/guide/missions). The old address, `/settings/ai-playbooks`, still works and
redirects there.

### Prompts · `ai-prompts`

Every system prompt BattleLab sends, with the JSON shape each one must return and **Reset to
default**. The list of prompts is in the [Reference](/reference/).

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
rows; **Add agent** sets up a new one. The public catalog is the line under the list; open
**Catalog details** for its links, history and the daily check.

An agent's page starts with its status, the one thing to do if any, and a **⋯** menu for reload,
disable and remove. Below are its usage (a percentage marked *plan* comes from the agent itself;
the rest count against a token limit you set here), its models, its installation in one line with
the verification evidence behind it, and **Technical details** folded away. Kimi Code reports its
plan quota from version 2.1 on, so a counter you kept for it by hand is no longer used — update
kimi if its row shows an error instead.

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

**Two-factor authentication** — enrollment, recovery codes, and turning it off, which needs a
current code or your password — and **Account**, with **Sign out**. With login off
(`AGENT_SESSIONS_AUTH_MODE=none`, as in Home Free) there is nothing to manage, and the page explains
that instead. See [First login](/start/first-login).

### Updates · `updates`

The running version, **Automatic updates** (a daily check that installs new releases with the same
rollback-guarded installer), the release channel — **stable** (tagged releases) or **main** (the
development branch) — and the manual update control. The channel and the toggle persist
server-side and apply immediately. See [Update & rollback](/start/update).

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

**API session memory limit** sets the combined memory allowance for an agent and its tools,
in GiB (default **8**, range **1–1024**). Reaching the limit stops that session and its tools.
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

## About · `about`

The version, the licence, a link to the source code, a contact address and a way to support the
project.

::: tip A setting that appears not to save
Preferences are written server-side immediately. If a panel appears to lose a value on remount, the
cause is a stale client-side copy of the configuration rather than a failed write — the value is on
disk. Reloading the page shows the true state.
:::

::: info Verified against
Commit `8b1c66b` — `web/src/routes/settingsTabs.ts § SETTINGS_GROUPS, SETTINGS_SECTIONS, legacySettingsTarget`; `web/src/routes/Settings.tsx`; `web/src/routes/{AiEndpointSetup,AiReviewSettings,AutoSortSettings,OrchestratorSettings,PulseSettings,ForgeSettings,MissionPlaybooks,PromptsSettings,AiActivityPanel}.tsx`; `src/agent_sessions/prefs.py § THEMES, DEFAULT_ACCENT, COMPOSE_DEFAULTS, store path`.
:::
