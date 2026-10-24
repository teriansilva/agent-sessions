# HQ coverage ledger (#1558)

What an operator can do in BattleLab, and how HQ reaches each piece of it. The epic (#1558) is the
plan; this file is the record, checked by `tests/test_hq_coverage.py` against the code itself, not
against this text.

**Codes.**
- `have` — HQ can do it today.
- `#1554` / `#1443` — owned by that issue.
- `P1`–`P7` — this epic's phases.
- `never` — a recorded exclusion: credentials, prompts, widening permission or autonomy, approving
  a tap, plugin installs, outsiders driving agents, consent. A note like `off only` means
  narrowing is in that phase and widening is never.
- `device` — browser-local state.
- `n/a` — no operator meaning: diagnostics, HQ itself, the public visitor surface, uploads.
- `prefs` / `nested` — see the prefs map.

**What the test checks.** It fails on any route the app registers, any field `POST /api/prefs`
accepts (top-level keys read off the handler; nested keys of the split blocks read off their
validators), or any `/api/` / `/ws/` literal in `web/src` that this file does not map. It also
fails on an entry here that no longer exists. So a new capability can't skip the ledger.

## The ledger

**Codes.** `have` = HQ can do it today. `#1554` / `#1443` = owned there. `P1`–`P7` = this epic's phases. `R` = read tool, `W` = confirmable action. `device` = browser-local state, not platform state (HQ can't reach this browser's storage; it can offer an **Open** link instead). `never` = recorded exclusion (reasons in the next section). `n/a` = no operator meaning.

#### Sessions (sidebar, row menu, pane)
| Capability | Route(s) | HQ |
|---|---|---|
| Find sessions by topic | `pulse_chat` find | have (`find_sessions`) |
| List / filter sessions (project, agent, mission, archived, search, paging) | GET /api/sessions | P1 R (`list_sessions` with the same filters; archived included) |
| One session's state, summary, screen, pending decision | GET /api/sessions/{sid}, scrollback | have (`session_status`) |
| Session brief / recap; older scrollback (history) | GET /api/sessions/{sid}, GET …/history | P1 R |
| Review now (AI summary) | POST /api/sessions/{sid}/review | P1 W (external: AI endpoint) |
| Exclude / include in AI review | POST …/review-exclude | P1 W |
| Exclude / include in orchestration | POST …/orchestrator-exclude | P1 W |
| Favourite / unfavourite | POST …/favorite, …/unfavorite | P1 W |
| Rename | POST …/rename | P1 W |
| Tag / clear tag | POST …/tag | P1 W |
| Colour (API-only today) | POST …/color | P1 W |
| Move to project / unassign | PATCH …/metadata | P1 W |
| Archive / unarchive | POST …/archive, …/unarchive | P1 W |
| Archive sessions older than N | POST /api/sessions/archive-older | P1 W (partly done when `skipped`) |
| Close an API session (stop worker); discard a pending start | POST /api/structured/sessions/{key}/stop | P1 W |
| Start a pending API session | POST /api/structured/sessions/{key}/start | P7 W (starts agents) |
| Adopt into / detach from a mission | POST /api/missions/{id}/adopt, …/detach | P5 W |
| Hand off to another agent (prepare preview, edit seed, archive source) | POST /api/handoff/prepare, POST /api/handoff | P7 W (starts agents; seed shown verbatim on the card) |
| Draft: read / save / clear | GET/PUT …/draft | P7 R/W |
| Type into a terminal session (text) | WS input | have (`propose_nudge`, sessions no mission holds) → P7 widens to keys (Esc, arrows, Tab, Return) |
| API session: send a message, send now, withdraw a queued message, interrupt | POST /api/structured/sessions/{key}/turns, …/send-now, …/withdraw, …/interrupt | P7 W |
| API session: answer the agent's question / reply async / skip question | POST …/decisions (answer / cancel / reject) | P7 W |
| API session: approve / reject a tool request | POST …/decisions | **never** (permission prompts are operator-only, `permission-prompts.md`) |
| API session: approve always (standing grant) | POST …/decisions (always + grant) | **never** (grants permission) |
| API session: skip permissions | POST …/skip-permissions | **never** (grants permission) |
| Chat-runtime session: send / retry | POST /api/chat/{sid}/messages, …/retry | P7 W |
| Chat-runtime: approve or reject a proposed file edit | POST /api/chat/…/proposals/{p}/decide | P7 W (diff on the card) |
| Take over a session from another tab; repaint; scroll; copy; text size −/+ | WS / client | device |
| Share link (clipboard), open in map / full screen, minimise, arrange, window cap, cluster layout, group-by | client | device (HQ answers carry Open / Open in map links) |
| Compose box state, sent-message history, dictation, always-send-now | localStorage | device |

#### New session and projects
| Capability | Route(s) | HQ |
|---|---|---|
| Start a session (agent, project, brief, bypass) | WS new=1 / POST /api/structured/sessions / POST /api/chat/new | have (`propose_session`); P7 adds model/effort and API/chat agents |
| List agents and whether they can start | GET /api/engines | have (`list_agents`) + #1554 |
| List models for an API agent | GET /api/structured/clients/{engine}/models | P7 R |
| Choose or create a folder | GET /api/fs/dirs, POST /api/fs/mkdir | P1 W (folder picked **on the card**; the model never sees a path) |
| List projects (incl. archived, counts) | GET /api/projects | have (`list_projects`) → P1 adds archived + counts |
| Create a project (name, colour, folder, defaults) | POST /api/projects | P1 W (folder on the card) |
| Rename / colour a project | PATCH /api/projects/{pid} | P1 W |
| Project launch defaults (agent, model, effort) | PATCH /api/projects/{pid} `launch_defaults` | P2 W |
| Project launch default **bypass on** | PATCH … `launch_defaults.bypass=true` | **never** (widens permission); turning it off is P2 W |
| Adopt / release a folder; set default folder | PATCH … `folders`, `default_folder` | P1 W (folder on the card) |
| Default project for new sessions | POST /api/prefs `default_project_id` | P1 W |
| Archive / unarchive a project (+ members), retry failed members | POST /api/projects/{pid}/archive, …/unarchive | P1 W (partly done) |
| Delete a project | DELETE /api/projects/{pid} | P1 W (destructive) |
| Auto-sort now | POST /api/projects/auto-sort | P1 W (external: AI endpoint) |
| Folder discovery: roots, exclusions, visibility mode, hide/show folder, folder display names | POST /api/prefs `project_roots`, `folder_exclusions`, `projects_mode`, `projects_hidden`/`included`, `project_names` | P2 W (folders on the card) |
| Discovered folders | GET /api/folders | P1 R (names only, no paths) |

#### Missions
| Capability | Route(s) | HQ |
|---|---|---|
| List / search / filter missions; one mission's status | GET /api/missions, GET /api/missions/{id} | have |
| Mission objectives, context (folder, git, roster), "now" strip, live screen of a held session, older events | GET …/objectives, …/context, …/now, …/screen/{key}, ?events_before_seq | P5 R |
| Create a mission (+ checklist) and dispatch its plan | POST /api/missions, POST …/dispatch | #1443 P3 |
| Begin / mark done / start again / reopen / not yet / mark failed | POST …/state | P5 W |
| Abandon | POST …/state → abandoned | P5 W (destructive) |
| Archive (stops agents) / unarchive (record only, or restart agents) | POST …/archive, …/unarchive | P5 W (destructive / starts agents) |
| Plan again; plan manually; change plan project / engine / brief | POST …/plan, PATCH …/plan | P5 W |
| Objectives: add, rename, waive, reorder, remove, overrule the judge, edit / reset / clear direction, preview a direction | PATCH …/objectives, POST /api/mission-directions/preview | P5 W/R |
| Stand down an objective's nudges | POST …/objectives/{key}/stand-down | P5 W |
| Relay words to a held session; edit an AI-drafted direction and send | POST …/relay | P5 W |
| Spawn a sub-agent beside a held session | POST …/spawn | P5 W (starts agents) |
| Answer menus automatically (auto-choose) | PATCH …/autonomy | **never** for on (widens autonomy); off is P5 W |
| Send a message to the mission / retry / dismiss an ambiguous turn | POST …/message/stream, …/turns/{t}/ack | P5 W |
| Answer the mission's question (option / free text) | POST …/answer | P5 W |
| Archive finished missions older than N days (preview, then archive) | GET/POST /api/missions/archive-older | P5 R/W (destructive) |

#### Decisions, Needs you, notifications, Recent work, dashboard
| Capability | Route(s) | HQ |
|---|---|---|
| Needs-you list and one item's details | GET /api/pulse/needs-you, …/{sid}/details | P2 R |
| Approve a staged decision (also with edited text) / choose an option | POST /api/pulse/actions/{id}/approve, …/choose | **never** (a tap HQ proposes to approve another tap removes the tap; HQ points to that card) |
| Reject / dismiss a staged decision | POST /api/pulse/actions/{id}/reject | P2 W |
| Dismiss a session from Needs you | POST /api/pulse/needs-you/{sid}/dismiss | P2 W |
| Evidence behind a decision (screen / transcript / recap) | GET /api/pulse/evidence/{sid} | P2 R |
| Notifications: list, mark read (all or some), dismiss one / all, clear settled | GET/POST /api/pulse/notifications/* | P2 R/W |
| Recent work recap (read, refresh) | GET/POST /api/pulse/recap | P2 R/W |
| Pulse overview cards; orchestrator health | GET /api/pulse, GET /api/pulse/orchestrator | P2 R |
| Pulse scan now | POST /api/pulse/scan | P2 W (external: AI) |
| Run one orchestrator pass now | POST /api/pulse/orchestrate | P2 W (it acts only within the configured autonomy) |
| Dashboard tiles (running, recent, missions, quota) | GET /api/dashboard/sessions, /api/missions, /api/agents/usage | P2 R / #1554 |
| Refresh usage now | POST /api/agents/usage/refresh | P2 W (external: agent CLIs) |
| Push: list devices, remove a device | GET /api/pulse/push/key, POST …/unsubscribe | P2 R/W |
| Push: enable on this device | browser permission + POST …/subscribe | device |
| AI activity (running AI tasks) | GET /api/ai/activity | P2 R |

#### Settings
| Capability | Route(s) | HQ |
|---|---|---|
| Read the allowlisted settings view | prefs getters | #1554 |
| Session defaults: compose default, list order | POST /api/prefs | P2 W |
| Theme, accent, terminal scheme / font / size, API look | POST /api/prefs (+ localStorage) | P2 W (server value; another device's stored choice wins there, as today) |
| Motion, link-open mode | localStorage | device |
| HQ contact | POST /api/prefs `hq_contact` | P2 W |
| AI endpoint provider, URL and key; the egress acknowledgement; remove key; test | POST /api/prefs `ai_review`, POST /api/ai-review/endpoint/test | **never** (decides where HQ's own requests go, and who receives session content; `ai-key-origin.md`, `ai-providers.md`) |
| AI model (Auto or specific), thinking level, request timeout; list models; the provider table | POST /api/prefs `ai_review.model`, `ai_review.model_mode`, `ai_review.thinking`, GET /api/ai-review/models, GET /api/ai-review/providers | P2 W/R (model from the server's own list only) |
| Session review: enable, interval, depth, notify, rename sessions | POST /api/prefs `ai_review.enabled`, `session_review` | P2 W |
| Auto-sort settings | POST /api/prefs `auto_sort` | P2 W |
| Orchestrator: thresholds, interval, notify, stale hours, nudge text | POST /api/prefs `orchestrator.*` | P2 W |
| Orchestrator **on**, autonomy **up**, AI directions auto-send **on**, allowed verbs widened | POST /api/prefs `orchestrator`, PATCH /api/automation/{scope} | **never** (widens autonomy); turning any of them **down or off** is P2 W |
| Pulse: auto scan, interval, window, depth | POST /api/prefs `pulse` | P2 W |
| Forge connection: URL, owner, token; clear token | POST /api/prefs `forge` | **never** (credential) |
| Prompts: read the catalog | GET /api/prompts | P2 R |
| Prompts: edit / reset | PATCH /api/prompts/{id} | **never** (the guarded `ask_orchestrator` prompt is HQ's own fence; others shape every model call) |
| Agents: roster, one agent's manifest, installation evidence | GET /api/engines, /api/engines/{id}, /api/plugins | #1554 + P6 R |
| Agent budgets: token limit, manual used, alert threshold, notify | PATCH /api/agents/budgets | P2 W |
| Agent operator models (add / remove a model id) | POST /api/prefs `agent_defaults.models` | P2 W |
| Agent session defaults (model, effort) | POST /api/prefs `agent_defaults.session_defaults` | P2 W |
| Default agent for new sessions | POST /api/prefs `agent_defaults.default_engine` | P2 W |
| Permission-bypass default **on** | POST /api/prefs `agent_defaults.bypass=true` | **never** (widens permission); off is P2 W |
| Chat-agent endpoint URL / key / model / tools | PATCH /api/agents/{engine}/endpoint | **never** (credential + where requests go) |
| Plugins: catalog refresh, auto-check daily | POST /api/plugins/feed/refresh, PATCH /api/agents/catalog/preferences | P6 W |
| Plugins: review + install, verify, sign in, endpoint candidate | POST /api/plugins/review, …/install, …/verify, …/signin, WS signin | **never** (runs fetched code; sign-in is an interactive vendor login) |
| Plugins: enable (activate), disable, remove, reload roster, recover, cancel operation | POST /api/plugins/activate, …/disable, …/remove, …/reload, …/recover, …/operations/{id}/cancel | P6 W (remove is destructive) |
| 2FA enroll / confirm / disable / recovery codes; password; sign out; device link | /api/2fa/*, /api/password, /change-password, /logout, /link/* | **never** (credentials and identity) |
| Public shares: list; revoke one; revoke all | GET /api/shares, DELETE /api/shares/{id}, POST …/revoke-all | P6 R/W |
| Public shares: create (view only) | POST /api/shares mode=view | P6 W (external: public exposure; password and expiry typed on the card) |
| Public shares: create "can send messages" | POST /api/shares mode=send | **never** (lets outsiders drive an agent) |
| Updates: version, settings, check; auto-update, channel | GET /api/version, /api/update/*, POST /api/update/settings | P6 R/W |
| Update now / reinstall (restarts the service) | POST /api/update/apply, GET /api/update/progress | P6 W (external; the card says the conversation reconnects) |
| Usage analytics consent | POST /api/prefs `analytics_consent` | **never** (consent is the operator's own act) |
| Host and resources (read); resource limits (edit) | GET /api/system, /api/system/resources; POST /api/system/resources | P6 R/W |
| Maintenance: scrollback size and clear; prune (preview, prune); compact (measure, compact) | GET/POST /api/scrollback*, /api/maintenance/prune, /api/maintenance/compact | P6 R/W (destructive) |
| What's new / onboarding done flags, tour, help | POST /api/prefs `onboarded`, `whats_new_seen`; client | n/a |

#### Library
| Capability | Route(s) | HQ |
|---|---|---|
| Templates: list, read | GET /api/templates | P3 R |
| Templates: create, edit, duplicate, delete | POST/PATCH/DELETE /api/templates/{id} | P3 W (revision-checked) |
| Templates: send a template to a session (incl. secret fields typed on the card) | POST /api/templates/{id}/send, …/used | P3 W (external: types into the session) |
| Templates: suggestions (read, analyse, write me a template, dismiss) | GET/POST /api/templates/suggest*, …/write, …/suggestions/{sid}/dismiss | P3 R/W (external: AI endpoint) |
| Template images upload | POST /api/upload | n/a (HQ has no file input; it can reference an uploaded file) |
| Variables library: list (secrets show only `set` / `needs re-entry`) | GET /api/variables | P3 R |
| Variables: create / edit / delete a text variable; collections create / edit / delete; enable shared variables | POST/PATCH/DELETE /api/variables*, /api/variable-collections*, POST /api/variables/migrate | P3 W |
| Secrets: create / replace (value typed on the card), delete | POST/PATCH/DELETE /api/variables (kind secret) | P3 W (secret_input) |
| Legacy template-variables routes (API-only) | /api/template-variables* | P3 (via the same store) |
| Checklists: view; edit and save the whole block | GET /api/config `mission_playbooks`, POST /api/prefs `mission_playbooks` | P3 R/W (revision-checked) |
| Memory: projects, notes, inbox, quarantine; export | GET /api/memory*, …/export | P3 R |
| Memory: on/off, edit / delete note, clear all, quarantine rewrite / delete, import preview and import, ingest | PUT/PATCH/DELETE/POST /api/memory/{pid}/* | P3 W (clear is destructive) |
| Playbooks: list, detail, authoring schema | GET /api/playbooks, /api/playbooks/{pid}, …/authoring/schema | have (`list_playbooks`, `playbook_detail`, `playbook_schema`, #1611) |
| Playbooks: projects using one, verify a deployment | GET /api/playbooks/{pid}/projects, /api/projects/{p}/playbook/verify | P4 R |
| Playbooks: create, edit (files), duplicate | POST /api/playbooks, PUT /api/playbooks/{pid}, …/duplicate | have (`propose_library`, revision-checked, the files or diff on the card, #1611) |
| Playbooks: revert an explicit format 5 upgrade to the retained format ≤4 copy (#1654) | POST /api/playbooks/{pid}/revert-format | P4 W (revision-checked; the format 5 tree is retained) |
| Playbooks: delete, set / clear default, save as new | DELETE /api/playbooks/{pid}, …/default, …/authoring/copy | P4 W |
| Playbook step prompts: import a template, compare | POST /api/playbooks/authoring/prompts/import, …/compare | P4 R/W |
| Deploy a playbook to a project: review → confirm → (create target) → bind → apply → verify | POST …/review, …/review/confirm, …/review/create-target, /api/projects/{p}/playbook/bind, …/apply | P4 W (external: writes into the project folder; secrets on the card) |
| Remove a deployment (plan, remove) | POST /api/projects/{p}/playbook/remove/plan, …/remove | P4 W (destructive) |
| Update projects from a playbook (fleet review → update; resume) | POST …/fleet/review, …/fleet/update, GET …/fleet/{op} | P4 W (external; partly done per project) |
| Playbook git: clone, seed, remote, publish (API-only today) | /api/playbooks/clone, /api/projects/{p}/playbook/git/* | P4 W (external: network clone, git push, forge PR) |
| Gallery: browse, look inside, refresh | GET /api/gallery*, POST /api/gallery/refresh | P4 R/W (external: relay fetch) |
| Gallery: add to my playbooks / skills; compare and add an updated copy | POST /api/gallery/import, …/compare, …/update | P4 W |
| Skills: list, read; save an edited copy; create a new one | GET /api/library/skills*, POST …/copies, POST /api/library/skills | have (`list_skills`, `skill_detail`, `propose_library`, #1611) |
| Automations: list, detail | GET /api/automations, /api/automations/{aid} | have (`list_automations`, `automation_detail`, #1611) |
| Automations: runs, run detail, origins | GET /api/automations/{aid}/runs, …/runs/{run_id}, …/origins | P4 R |
| Automations: create, edit | POST /api/automations, PATCH /api/automations/{aid} | have (`propose_library`: saved off; an enabled one's widening edit is a consent card; never bypass on or auto-choose, #1611) |
| Automations: duplicate, delete | POST/DELETE /api/automations* | P4 W |
| Automations: pause, resume, turn off, run now | POST …/pause, …/resume, …/disable, …/run | P4 W (run now starts agents) |
| Automations: enable (consent to its scope) | POST …/enable `revision, consent, scope_digest` | have (`propose_library`: the card shows the server's own scope lines; the tap is the consent, #1611) |

#### Files and git (Files drawer)
| Capability | Route(s) | HQ |
|---|---|---|
| List a folder, read a file, file capabilities, raw / download | GET /api/files/* | P6 R (paths relative to the session's folder only, inside the roots) |
| Write a file (sha-bound) | POST /api/files/write | P6 W (diff on the card; displace, never overwrite) |
| Upload files / folders; collisions | POST /api/files/upload* | n/a (operator-supplied files) |
| Git: status, branches, push target, log, diff | GET /api/git/* | P6 R |
| Git: fetch, pull | POST /api/git/fetch, …/pull | P6 W (external) |
| Git: stage / unstage, commit staged, commit selected / all, settle | POST /api/git/stage, …/commit, …/commit-paths, …/settle | P6 W |
| Git: discard / revert a file | POST /api/git/discard | P6 W (destructive) |
| Git: revert a commit | POST /api/git/revert | P6 W |
| Git: switch / create branch, delete a merged branch | POST /api/git/switch, …/branch/delete | P6 W |
| Git: push | POST /api/git/push | P6 W (external) |

#### API-only routes (no UI caller today)
| Route | HQ |
|---|---|
| POST /api/missions/{id}/message, POST /api/ask/conversations/{cid}/message | n/a (non-streaming twins of UI routes) |
| POST /api/pulse/ask[/stream], POST /api/pulse/chat | n/a (HQ is their successor; #1443 retires `pulse/chat`) |
| PATCH /api/automation/{scope} | see Settings › orchestrator (down/off only) |
| GET /api/agents/catalog, POST /api/agents/catalog/refresh | P6 (aliases of `/api/plugins`) |
| GET /api/perf, GET /api/system/sessions, GET /healthz, GET /api/auth-check | n/a (diagnostics / infrastructure) |
| POST /api/folders/mkdir | P1 (same as /api/fs/mkdir) |
| GET /api/structured/clients/{engine}, GET …/containment | P7 R |
| /p/*, /api/public/* | n/a (the public share visitor's surface) |

## Never through HQ (recorded decision, §3b)

Each row is a credential, a grant of more permission or autonomy, or a tap that would approve another tap. A prompt-injected screen could turn any of them against the operator. **Turning a permission or an autonomy down is always allowed; turning it up is never.** Each row can be moved by a later operator-approved issue with its own security review.

| Group | Rows | Why |
|---|---|---|
| Credentials and identity | password, 2FA, sign out, device link, AI endpoint URL/key, chat-agent endpoint URL/key, forge token | credentials; and the endpoints decide where HQ's own requests go |
| HQ's own fence | prompt edits | the guarded `ask_orchestrator` prompt is HQ's fence |
| Widening permission or autonomy | bypass defaults on (global or per project), skip permissions, approve-always grants, orchestrator on, autonomy up, AI directions auto-send on, allowed verbs widened, mission auto-choose on | HQ would grant itself or agents more power |
| Approving taps | approve or choose a staged decision, approve a tool request, answer a permission prompt | a proposal to approve a proposal removes the operator's tap |
| Running fetched code / vendor login | plugin review/install/verify/sign-in | supply-chain and credential surface |
| Outsiders driving agents | public share in "can send messages" mode | external control of an agent |
| Consent | analytics consent | consent is the operator's own act |


## Route map

```hq-routes
GET    /                                                                          n/a  # SPA shell
POST   /api/2fa/confirm                                                           never  # credentials
POST   /api/2fa/disable                                                           never  # credentials
POST   /api/2fa/enroll                                                            never  # credentials
POST   /api/2fa/recovery-codes                                                    never  # credentials
PATCH  /api/agents/budgets                                                        P2
GET    /api/agents/catalog                                                        P6
PATCH  /api/agents/catalog/preferences                                            P6
POST   /api/agents/catalog/refresh                                                P6
GET    /api/agents/usage                                                          #1554  # read
POST   /api/agents/usage/refresh                                                  P2
GET    /api/agents/{engine_id}/endpoint                                           P6  # read: endpoint configured (no key)
PATCH  /api/agents/{engine_id}/endpoint                                           never  # credential + where requests go
POST   /api/agents/{engine_id}/endpoint/test                                      never  # credential + where requests go
POST   /api/ai-review/endpoint/test                                               never  # decides where HQ's requests go
GET    /api/ai-review/models                                                      P2  # read
GET    /api/ai-review/providers                                                   P2  # read: the static provider table
GET    /api/ai/activity                                                           P2  # read
GET    /api/ask/conversations                                                     n/a  # HQ itself
POST   /api/ask/conversations                                                     n/a  # HQ itself
GET    /api/ask/conversations/{cid}                                               n/a  # HQ itself
DELETE /api/ask/conversations/{cid}                                               n/a  # HQ itself (delete a conversation, #1564)
DELETE /api/ask/conversations/{cid}/follow/{key:path}                             n/a  # HQ itself
POST   /api/ask/conversations/{cid}/message                                       n/a  # HQ itself
POST   /api/ask/conversations/{cid}/message/stream                                n/a  # HQ itself
POST   /api/ask/conversations/{cid}/seen                                          n/a  # HQ itself
GET    /api/ask/proposals/{pid}/content                                           n/a  # HQ itself (a library card's draft, #1611)
POST   /api/ask/proposals/{pid}/decide                                            n/a  # HQ itself
GET    /api/ask/unread                                                            n/a  # HQ itself
GET    /api/auth-check                                                            n/a  # diagnostics
PATCH  /api/automation/{scope}                                                    P2  # down/off only; widening never
GET    /api/automations                                                           have  # list_automations
POST   /api/automations                                                           have  # propose_library automation create
GET    /api/automations/origins                                                   P4
GET    /api/automations/runs/{run_id}                                             P4
DELETE /api/automations/{aid}                                                     P4
GET    /api/automations/{aid}                                                     have  # automation_detail
PATCH  /api/automations/{aid}                                                     have  # propose_library automation edit (#1611)
POST   /api/automations/{aid}/disable                                             P4
POST   /api/automations/{aid}/enable                                              have  # propose_library automation enable (#1611)
POST   /api/automations/{aid}/pause                                               P4
POST   /api/automations/{aid}/resume                                              P4
POST   /api/automations/{aid}/run                                                 P4
GET    /api/automations/{aid}/runs                                                P4
POST   /api/chat/new                                                              P7
GET    /api/chat/{sid}                                                            P7
POST   /api/chat/{sid}/messages                                                   P7
POST   /api/chat/{sid}/turns/{turn_id}/proposals/{proposal_id}/decide             P7
POST   /api/chat/{sid}/turns/{turn_id}/retry                                      P7
GET    /api/config                                                                n/a  # app shell read
GET    /api/dashboard/sessions                                                    P2  # read
GET    /api/engines                                                               have  # list_agents (+#1554)
GET    /api/engines/{engine_id}                                                   P6  # read
GET    /api/files/capabilities                                                    P6
GET    /api/files/list                                                            P6
GET    /api/files/raw                                                             P6
GET    /api/files/read                                                            P6
POST   /api/files/upload                                                          n/a  # operator-supplied files
POST   /api/files/upload/batch                                                    n/a  # operator-supplied files
POST   /api/files/upload/skip                                                     n/a  # operator-supplied files
POST   /api/files/write                                                           P6
GET    /api/folders                                                               P1  # folder card
POST   /api/folders/mkdir                                                         P1  # folder card
GET    /api/fs/dirs                                                               P1  # folder card
POST   /api/fs/mkdir                                                              P1  # folder card
GET    /api/gallery                                                               P4
POST   /api/gallery/compare                                                       P4
GET    /api/gallery/content/{digest}                                              P4
POST   /api/gallery/import                                                        P4
GET    /api/gallery/operations/{operation}                                        P4
POST   /api/gallery/refresh                                                       P4
POST   /api/gallery/update                                                        P4
POST   /api/git/branch/delete                                                     P6
GET    /api/git/branches                                                          P6
POST   /api/git/commit                                                            P6
POST   /api/git/commit-paths                                                      P6
GET    /api/git/diff                                                              P6
POST   /api/git/discard                                                           P6
POST   /api/git/fetch                                                             P6
GET    /api/git/log                                                               P6
POST   /api/git/pull                                                              P6
POST   /api/git/push                                                              P6
GET    /api/git/push-target                                                       P6
POST   /api/git/revert                                                            P6
POST   /api/git/settle                                                            P6
POST   /api/git/stage                                                             P6
GET    /api/git/status                                                            P6
POST   /api/git/switch                                                            P6
POST   /api/handoff                                                               P7
POST   /api/handoff/prepare                                                       P7
GET    /api/library/skills                                                        have  # list_skills
POST   /api/library/skills                                                        have  # propose_library skill create (#1611)
GET    /api/library/skills/{id}                                                   have  # skill_detail
POST   /api/library/skills/{id}/copies                                            have  # propose_library skill edit (#1611)
GET    /api/maintenance/compact                                                   P6
POST   /api/maintenance/compact                                                   P6
GET    /api/maintenance/prune                                                     P6
POST   /api/maintenance/prune                                                     P6
GET    /api/memory                                                                P3
GET    /api/memory/{pid}                                                          P3
POST   /api/memory/{pid}/clear                                                    P3
PUT    /api/memory/{pid}/enabled                                                  P3
GET    /api/memory/{pid}/export                                                   P3
GET    /api/memory/{pid}/import                                                   P3
POST   /api/memory/{pid}/import                                                   P3
POST   /api/memory/{pid}/ingest                                                   P3
DELETE /api/memory/{pid}/notes/{slug}                                             P3
PATCH  /api/memory/{pid}/notes/{slug}                                             P3
DELETE /api/memory/{pid}/quarantine/{qid}                                         P3
PATCH  /api/memory/{pid}/quarantine/{qid}                                         P3
POST   /api/mission-directions/preview                                            P3
GET    /api/missions                                                              P5
POST   /api/missions                                                              #1443  # mission start
GET    /api/missions/archive-older                                                P5
POST   /api/missions/archive-older                                                P5
GET    /api/missions/{mission_id}                                                 P5
POST   /api/missions/{mission_id}/adopt                                           P5
POST   /api/missions/{mission_id}/answer                                          P5
POST   /api/missions/{mission_id}/archive                                         P5
PATCH  /api/missions/{mission_id}/autonomy                                        P5  # off only; on never
GET    /api/missions/{mission_id}/context                                         P5
POST   /api/missions/{mission_id}/detach                                          P5
POST   /api/missions/{mission_id}/dispatch                                        #1443  # mission start
POST   /api/missions/{mission_id}/message                                         n/a  # non-streaming twin
POST   /api/missions/{mission_id}/message/stream                                  P5
GET    /api/missions/{mission_id}/now                                             P5
GET    /api/missions/{mission_id}/objectives                                      P5
PATCH  /api/missions/{mission_id}/objectives                                      P5
POST   /api/missions/{mission_id}/objectives/{objective_key}/stand-down           P5
PATCH  /api/missions/{mission_id}/plan                                            P5
POST   /api/missions/{mission_id}/plan                                            P5
POST   /api/missions/{mission_id}/relay                                           P5
GET    /api/missions/{mission_id}/screen/{session_key:path}                       P5
POST   /api/missions/{mission_id}/spawn                                           P5
POST   /api/missions/{mission_id}/state                                           P5
POST   /api/missions/{mission_id}/turns/{turn_id}/ack                             P5
POST   /api/missions/{mission_id}/unarchive                                       P5
POST   /api/password                                                              never  # credentials
GET    /api/perf                                                                  n/a  # diagnostics
GET    /api/playbooks                                                             have  # list_playbooks
POST   /api/playbooks                                                             have  # propose_library playbook create (#1611)
POST   /api/playbooks/authoring/copy                                              P4
POST   /api/playbooks/authoring/prompts/compare                                   P4
POST   /api/playbooks/authoring/prompts/import                                    P4
GET    /api/playbooks/authoring/schema                                            have  # playbook_schema
POST   /api/playbooks/clone                                                       P4
DELETE /api/playbooks/{pid}                                                       P4
GET    /api/playbooks/{pid}                                                       have  # playbook_detail
PUT    /api/playbooks/{pid}                                                       have  # propose_library playbook edit (#1611)
DELETE /api/playbooks/{pid}/default                                               P4
PUT    /api/playbooks/{pid}/default                                               P4
POST   /api/playbooks/{pid}/duplicate                                             have  # propose_library playbook duplicate (#1611)
POST   /api/playbooks/{pid}/fleet/review                                          P4
POST   /api/playbooks/{pid}/fleet/update                                          P4
GET    /api/playbooks/{pid}/fleet/{operation}                                     P4
GET    /api/playbooks/{pid}/projects                                              P4
POST   /api/playbooks/{pid}/revert-format                                         P4  # #1654: revert an explicit format 5 upgrade
POST   /api/playbooks/{pid}/review                                                P4
POST   /api/playbooks/{pid}/review/confirm                                        P4
POST   /api/playbooks/{pid}/review/create-target                                  P4
GET    /api/plugins                                                               P6
POST   /api/plugins/activate                                                      P6
POST   /api/plugins/disable                                                       P6
POST   /api/plugins/feed/refresh                                                  P6
POST   /api/plugins/install                                                       never  # runs fetched code / vendor login
GET    /api/plugins/operations/{operation_id}                                     P6
POST   /api/plugins/operations/{operation_id}/cancel                              P6
POST   /api/plugins/recover                                                       P6
POST   /api/plugins/reload                                                        P6
POST   /api/plugins/remove                                                        P6
POST   /api/plugins/review                                                        never  # runs fetched code / vendor login
POST   /api/plugins/signin                                                        never  # runs fetched code / vendor login
POST   /api/plugins/verify                                                        never  # runs fetched code / vendor login
GET    /api/plugins/{plugin_id}/generations/{generation_id}/endpoint              never  # plugin install surface
PATCH  /api/plugins/{plugin_id}/generations/{generation_id}/endpoint              never  # plugin install surface
POST   /api/plugins/{plugin_id}/generations/{generation_id}/endpoint/test         never  # plugin install surface
POST   /api/prefs                                                                 prefs  # per field: see the prefs map
GET    /api/projects                                                              have  # list_projects (archived + counts in P1)
POST   /api/projects                                                              P1
POST   /api/projects/auto-sort                                                    P1
DELETE /api/projects/{pid}                                                        P1
PATCH  /api/projects/{pid}                                                        P1  # name/colour/folders P1; launch_defaults P2; launch_defaults.bypass on never
POST   /api/projects/{pid}/archive                                                P1
POST   /api/projects/{pid}/unarchive                                              P1
GET    /api/projects/{project}/playbook                                           P4
POST   /api/projects/{project}/playbook/apply                                     P4
POST   /api/projects/{project}/playbook/bind                                      P4
POST   /api/projects/{project}/playbook/git/publish                               P4
POST   /api/projects/{project}/playbook/git/publish/plan                          P4
POST   /api/projects/{project}/playbook/git/remote                                P4
POST   /api/projects/{project}/playbook/git/remote/plan                           P4
POST   /api/projects/{project}/playbook/git/seed                                  P4
GET    /api/projects/{project}/playbook/git/{operation}                           P4
POST   /api/projects/{project}/playbook/remove                                    P4
POST   /api/projects/{project}/playbook/remove/plan                               P4
GET    /api/projects/{project}/playbook/verify                                    P4
GET    /api/prompts                                                               P2  # read
PATCH  /api/prompts/{pid}                                                         never  # HQ's own fence
GET    /api/public/events                                                         n/a  # public share visitor surface
POST   /api/public/interrupt                                                      n/a  # public share visitor surface
POST   /api/public/name                                                           n/a  # public share visitor surface
POST   /api/public/send-now                                                       n/a  # public share visitor surface
GET    /api/public/session                                                        n/a  # public share visitor surface
GET    /api/public/share                                                          n/a  # public share visitor surface
POST   /api/public/turns                                                          n/a  # public share visitor surface
POST   /api/public/unlock                                                         n/a  # public share visitor surface
POST   /api/public/upload                                                         n/a  # public share visitor surface
GET    /api/public/uploads/{image}                                                n/a  # public share visitor surface
GET    /api/pulse                                                                 P2
POST   /api/pulse/actions/{action_id}/approve                                     never  # a tap approving a tap
POST   /api/pulse/actions/{action_id}/choose                                      never  # a tap approving a tap
POST   /api/pulse/actions/{action_id}/reject                                      P2
POST   /api/pulse/ask                                                             n/a  # HQ's predecessors
POST   /api/pulse/ask/stream                                                      n/a  # HQ's predecessors
POST   /api/pulse/chat                                                            n/a  # HQ's predecessors
GET    /api/pulse/evidence/{session_id:path}                                      P2
GET    /api/pulse/needs-you                                                       P2
GET    /api/pulse/needs-you/{session_id:path}/details                             P2
POST   /api/pulse/needs-you/{session_id:path}/dismiss                             P2
GET    /api/pulse/notifications                                                   P2
POST   /api/pulse/notifications/clear-settled                                     P2
POST   /api/pulse/notifications/dismiss                                           P2
POST   /api/pulse/notifications/read                                              P2
POST   /api/pulse/orchestrate                                                     P2
GET    /api/pulse/orchestrator                                                    P2
GET    /api/pulse/push/key                                                        P2
POST   /api/pulse/push/subscribe                                                  device  # browser push permission
POST   /api/pulse/push/unsubscribe                                                P2
GET    /api/pulse/recap                                                           P2
POST   /api/pulse/recap                                                           P2
POST   /api/pulse/scan                                                            P2
GET    /api/scrollback                                                            P6
POST   /api/scrollback/clear                                                      P6
GET    /api/sessions                                                              P1
POST   /api/sessions/archive-older                                                P1
GET    /api/sessions/{sid}                                                        P1
POST   /api/sessions/{sid}/archive                                                P1
POST   /api/sessions/{sid}/color                                                  P1
GET    /api/sessions/{sid}/draft                                                  P7  # drafts
PUT    /api/sessions/{sid}/draft                                                  P7  # drafts
POST   /api/sessions/{sid}/favorite                                               P1
GET    /api/sessions/{sid}/history                                                P1
PATCH  /api/sessions/{sid}/metadata                                               P1
POST   /api/sessions/{sid}/orchestrator-exclude                                   P1
POST   /api/sessions/{sid}/rename                                                 P1
POST   /api/sessions/{sid}/review                                                 P1
POST   /api/sessions/{sid}/review-exclude                                         P1
POST   /api/sessions/{sid}/tag                                                    P1
POST   /api/sessions/{sid}/unarchive                                              P1
POST   /api/sessions/{sid}/unfavorite                                             P1
GET    /api/shares                                                                P6
POST   /api/shares                                                                P6
POST   /api/shares/revoke-all                                                     P6
DELETE /api/shares/{share_id}                                                     P6
GET    /api/structured/clients/{engine}                                           P7
GET    /api/structured/clients/{engine}/models                                    P7
POST   /api/structured/sessions                                                   P7  # API/chat agents on new sessions
GET    /api/structured/sessions/{key}                                             P7
GET    /api/structured/sessions/{key}/containment                                 P7
POST   /api/structured/sessions/{key}/decisions                                   P7  # answers only; approve/always never (see ledger)
GET    /api/structured/sessions/{key}/events                                      P7
GET    /api/structured/sessions/{key}/history                                     P7
POST   /api/structured/sessions/{key}/interrupt                                   P7
POST   /api/structured/sessions/{key}/send-now                                    P7
POST   /api/structured/sessions/{key}/skip-permissions                            never  # widens permission
POST   /api/structured/sessions/{key}/start                                       P7
POST   /api/structured/sessions/{key}/stop                                        P1  # close
POST   /api/structured/sessions/{key}/turns                                       P7
POST   /api/structured/sessions/{key}/withdraw                                    P7
GET    /api/telemetry                                                             P2  # read: CPU, RAM, HQ tokens (footer)
GET    /api/system                                                                P6
GET    /api/system/resources                                                      P6
POST   /api/system/resources                                                      P6
GET    /api/system/sessions                                                       n/a  # diagnostics
GET    /api/template-variables                                                    P3
POST   /api/template-variables                                                    P3
DELETE /api/template-variables/{name}                                             P3
PATCH  /api/template-variables/{name}                                             P3
GET    /api/templates                                                             P3
POST   /api/templates                                                             P3
POST   /api/templates/suggest                                                     P3
GET    /api/templates/suggestions                                                 P3
POST   /api/templates/suggestions/{sid}/dismiss                                   P3
POST   /api/templates/write                                                       P3
DELETE /api/templates/{tid}                                                       P3
PATCH  /api/templates/{tid}                                                       P3
POST   /api/templates/{tid}/send                                                  P3
POST   /api/templates/{tid}/used                                                  P3
POST   /api/update/apply                                                          P6
GET    /api/update/check                                                          P6
GET    /api/update/progress                                                       P6
GET    /api/update/settings                                                       P6
POST   /api/update/settings                                                       P6
POST   /api/upload                                                                n/a  # operator-supplied files
GET    /api/uploads/{stored}                                                      n/a  # upload bytes
POST   /api/variable-collections                                                  P3
DELETE /api/variable-collections/{cid}                                            P3
PATCH  /api/variable-collections/{cid}                                            P3
GET    /api/variables                                                             P3
POST   /api/variables                                                             P3
POST   /api/variables/migrate                                                     P3
DELETE /api/variables/{vid}                                                       P3
PATCH  /api/variables/{vid}                                                       P3
GET    /api/version                                                               n/a  # app shell read
GET    /change-password                                                           never  # identity
POST   /change-password                                                           never  # identity
GET    /healthz                                                                   n/a  # diagnostics
GET    /link/approve                                                              never  # identity
POST   /link/approve                                                              never  # identity
POST   /link/deny                                                                 never  # identity
GET    /link/qr                                                                   never  # identity
POST   /link/start                                                                never  # identity
GET    /link/status                                                               never  # identity
GET    /login                                                                     never  # identity
POST   /login                                                                     never  # identity
POST   /login/totp                                                                never  # identity
POST   /logout                                                                    never  # identity
GET    /p/                                                                        n/a  # public share visitor surface
GET    /p/{token}                                                                 n/a  # public share visitor surface
GET    /{spa_path:path}                                                           n/a  # SPA shell
WS     /ws/term/{sid}                                                             P7  # terminal input; text nudges: have (propose_nudge)
WS     /ws/plugins/signin/{operation_id}                                          never  # interactive vendor login
GET    /api/voice/settings                                                        never  # speech endpoints + tokens decide where call audio goes (voice-calls.md)
POST   /api/voice/settings                                                        never  # same: credentials and destinations
POST   /api/voice/test                                                            never  # tests a draft speech endpoint with a token
POST   /api/voice/voices                                                          never  # reads a draft speech endpoint's lists
POST   /api/voice/preview                                                         n/a  # a fixed line the operator listens to
GET    /api/voice/capabilities                                                    n/a  # whether a call can start: calling HQ is HQ
POST   /api/voice/calls                                                           n/a  # calling HQ is HQ (#1397)
DELETE /api/voice/calls/{call_id}                                                 n/a  # hanging up on HQ
GET    /api/voice/local                                                           never  # the local speech server's status (#1625)
POST   /api/voice/local/use                                                       never  # points call audio at a server and stores its token
WS     /ws/voice/{call_id}                                                        n/a  # the call itself: speech for HQ's own turns
```

## Prefs map (`POST /api/prefs`)

```hq-prefs
accent                          P2
agent_defaults                  nested
agent_defaults.default_engine   P2
agent_defaults.bypass           P2  # off only; turning it on is never
agent_defaults.models           P2
agent_defaults.session_defaults P2
ai_review                       nested
ai_review.api_key               never  # credential
ai_review.base_url              never  # decides where HQ's own requests go
ai_review.enabled               P2
ai_review.interval_minutes      P2
ai_review.max_input_chars       P2
ai_review.model                 P2  # from the server's own model list only
ai_review.model_mode            P2  # Auto or specific
ai_review.prompt                never  # prompt text (legacy field; prompts are never)
ai_review.provider              never  # decides who receives session content (`ai-providers.md`)
ai_review.request_timeout       P2
ai_review.thinking              P2
analytics_consent               never  # consent is the operator's own act
api_look                        P2
auto_sort                       P2
compose_default                 P2
default_project                 n/a  # legacy onboarding prefill
default_project_id              P1
folder_exclusions               P2  # folders on the card
forge                           nested
forge.base_url                  never  # credential surface
forge.enabled                   never  # credential surface
forge.kind                      never  # credential surface
forge.owner                     never  # credential surface
forge.token                     never  # credential
hq_contact                      P2
mission_playbooks               P3
onboarded                       n/a  # onboarding flag
orchestrator                    nested
orchestrator.ai_direction_confidence_min P2  # raise only; lowering widens autonomy
orchestrator.allowed_verbs      P2  # narrow only
orchestrator.auto_ai_directions P2  # off only
orchestrator.autonomy           P2  # down only
orchestrator.confidence_min     P2  # raise only
orchestrator.enabled            P2  # off only
orchestrator.interval_minutes   P2
orchestrator.judge_confidence_min P2  # raise only
orchestrator.max_actions_per_pass P2  # lower only
orchestrator.notify             P2
orchestrator.nudge_template     never  # text automation types into sessions
orchestrator.prompt             never  # prompt text
orchestrator.proposal_ttl_minutes P2
orchestrator.stale_hours        P2
overview_expanded               n/a  # map cluster layout state
project_names                   P2
project_roots                   P2  # folders on the card
projects_hidden                 P2
projects_included               P2
projects_mode                   P2
pulse                           P2
session_list_order              P2
session_review                  P2
term_font_family                P2
term_font_size                  P2
term_scheme                     P2
theme                           P2
whats_new_seen                  n/a  # onboarding flag
```
