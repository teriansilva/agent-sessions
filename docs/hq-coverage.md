# HQ coverage ledger (#1558)

What an operator can do in BattleLab, and how HQ reaches each piece of it. The epic (#1558) is the
plan; this file is the record, checked by `tests/test_hq_coverage.py` against the code itself, not
against this text.

**Codes.**
- `have` — HQ can do it today.
- `#1554` / `#1443` / `#1786` / `#1932` / `#2163` — owned by that issue.
- `P1`–`P7` — this epic's phases.
- `never` — a recorded exclusion: credentials, prompts, widening permission or autonomy, approving
  a tap, plugin installs, outsiders driving agents, consent. A note like `off only` means
  narrowing is in that phase and widening is never.
- `device` — browser-local state.
- `screen` — what ONE browser shows (#2102): HQ's `show` tool asks the asking device to open a
  session, a mission, an artifact or a section, or to run a map command. No route is called and
  no platform state changes; the device's own outcome is never reported back.
- `n/a` — no operator meaning: diagnostics, HQ itself, the public visitor surface, uploads.
- `prefs` / `nested` — see the prefs map.

**What the test checks.** It fails on any route the app registers, any field `POST /api/prefs`
accepts (top-level keys read off the handler; nested keys of the split blocks read off their
validators), or any `/api/` / `/ws/` literal in `web/src` that this file does not map. It also
fails on an entry here that no longer exists. So a new capability can't skip the ledger.

## The ledger

**Codes.** `have` = HQ can do it today. `#1554` / `#1443` / `#1786` = owned there. `P1`–`P7` = this epic's phases. `R` = read tool, `W` = confirmable action. `device` = browser-local state, not platform state (HQ can't reach this browser's storage; it can offer an **Open** link instead). `screen` = what the asking browser shows, moved by HQ's `show` (#2102). `never` = recorded exclusion (reasons in the next section). `n/a` = no operator meaning.

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
| Session clusters (#1997): list; create, rename, keep / let HQ rename, merge, dissolve; move a session to a cluster / back to HQ's choice; re-cluster now | GET/POST /api/clusters, PATCH/DELETE /api/clusters/{id}, POST …/merge, PUT /api/sessions/{sid}/cluster, POST /api/clusters/recluster | P2 R/W (HQ's tools and cards land with #1997 P5) |
| Archive / unarchive | POST …/archive, …/unarchive | P1 W |
| Archive sessions older than N | POST /api/sessions/archive-older | P1 W (partly done when `skipped`) |
| Close an API session (stop worker); discard a pending start | POST /api/structured/sessions/{key}/stop | P1 W |
| Start a pending API session | POST /api/structured/sessions/{key}/start | P7 W (starts agents) |
| Adopt into / detach from a mission | POST /api/missions/{id}/adopt, …/detach | P5 W |
| Hand off to another agent (prepare preview, edit seed, archive source) | POST /api/handoff/prepare, POST /api/handoff | P7 W (starts agents; seed shown verbatim on the card) |
| Draft: read / save / clear | GET/PUT …/draft | P7 R/W |
| Type into a terminal session (text) | WS input | have (`send_nudge`, sent on HQ's authority, sessions no mission holds, #1960) → P7 widens to keys (Esc, arrows, Tab, Return) |
| API session: send a message, send now, withdraw a queued message, interrupt | POST /api/structured/sessions/{key}/turns, …/send-now, …/withdraw, …/interrupt | message: have (`send_nudge`, one guarded structured turn, sessions no mission holds, #1960); the rest P7 W |
| API session: answer the agent's question / reply async / skip question | POST …/decisions (answer / cancel / reject) | P7 W |
| API session: approve / reject a tool request | POST …/decisions | **never** (permission prompts are operator-only, `permission-prompts.md`) |
| API session: approve always (standing grant) | POST …/decisions (always + grant) | **never** (grants permission) |
| API session: skip permissions | POST …/skip-permissions | **never** (grants permission) |
| Chat-runtime session: send / retry | POST /api/chat/{sid}/messages, …/retry | P7 W |
| Chat-runtime: approve or reject a proposed file edit | POST /api/chat/…/proposals/{p}/decide | P7 W (diff on the card) |
| Take over a session from another tab; repaint; scroll; copy; text size −/+ | WS / client | device |
| Open in map / full screen, focus, minimise, arrange, close a window, close all | client | have (`show`, `screen`: a closed directive to the asking device only, applied once; #2102) |
| Window cap, cluster layout, group-by | client | device (HQ answers carry Open / Open in map links) |
| Compose box state, sent-message history, dictation, always-send-now | localStorage | device |

#### New session and projects
| Capability | Route(s) | HQ |
|---|---|---|
| Start a session (agent, project, brief, bypass) | WS new=1 / POST /api/structured/sessions / POST /api/chat/new | have (`propose_session`); P7 adds model/effort and API/chat agents |
| Start a session from a specialist (its version, model and Library skills) (#1910) | WS new=1 `specialist`, `specialist_version` | have (`propose_session` `specialist_index`, pinned version; HQ's web research) |
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
| Create a mission and dispatch its plan | POST /api/missions, POST …/dispatch | #1443 P3 |
| Begin / mark done / start again / reopen / not yet / mark failed | POST …/state | P5 W |
| Abandon | POST …/state → abandoned | P5 W (destructive) |
| Archive (stops agents) / unarchive (record only, or restart agents) | POST …/archive, …/unarchive | P5 W (destructive / starts agents) |
| Plan again; plan manually; change plan project / engine / brief | POST …/plan, PATCH …/plan | P5 W |
| Objectives: add, rename, waive, reorder, remove, overrule the judge, edit / reset / clear direction, preview a direction | PATCH …/objectives, POST /api/mission-directions/preview | P5 W/R |
| Stand down an objective's nudges | POST …/objectives/{key}/stand-down | P5 W |
| Procedures (#1777): list the project's procedures by index, apply one or a mission-local one (in a project chosen by index, after earlier objectives); read a plan revision with its derived step and objective states | GET …/procedures, GET …/procedures/projects, GET …/procedures/{index}/inputs, POST …/procedure, GET …/plan/revisions/{latest\|n} | P5 W/R |
| Create a DRAFT mission from a project (by index) and an instruction, applying the project's deployed procedure (by index, `list_procedures`) on the same card (#1950); nothing runs until Begin | POST /api/missions, POST …/procedure | have (`propose_mission`, a `mission` card running both routes as the operator; the route resolves the folder, HQ never sends a path or an input; HQ continues once after the tap; **never** approve, Begin or launch) |
| Team (#1950): read the team with its re-derived verdicts, the per-step candidate and model lists; assemble, assign, override a step's model, clear it | GET …/plan/team, GET …/plan/team/choices, POST …/plan/ops | have (`read_plan`, `read_pool`; `assemble`, `assign`, `set_override`, `clear_override` as cards) |
| Plan approval (#1951): read the card (pins, re-check, consent); approve a plan revision; give standing consent; revoke either | GET …/plan/approval, POST …/plan/approve, POST …/plan/consent, POST …/plan/approval/revoke, POST …/plan/consent/revoke | read and revoke P6 R/W (revoking narrows); **never** for approve and consent (the Commander's own tap, D3: HQ may stage, never decide) |
| Steps (#1581): read each step's runs and whether it can start; start an agent step in its own API session; stop a step's session | GET …/steps, POST …/steps/{step}/start, POST …/steps/{step}/stop, POST …/steps/{step}/hold, POST …/budget, POST …/steps/pause, …/steps/continue, …/steps/stop-all | read, stop, pause and stop-all P6 R/W (they narrow); **never** for start, continue (the operator's tap; HQ launches steps only under an approval in force, through the runner, #1951 slice B), the per-step hold (clearing one lets HQ start the step) or the budget (raising a cap widens) |
| Relay words to a held session; edit an AI-drafted direction and send | POST …/relay | P5 W |
| Spawn a sub-agent beside a held session | POST …/spawn | P5 W (starts agents) |
| Answer menus automatically (auto-choose) | PATCH …/autonomy | **never** for on (widens autonomy); off is P5 W |
| Send a message to the mission / retry / dismiss an ambiguous turn | POST …/message/stream, …/turns/{t}/ack | P5 W |
| Answer the mission's question (option / free text) | POST …/answer | P5 W |
| Archive finished missions older than N days (preview, then archive) | GET/POST /api/missions/archive-older | P5 R/W (destructive) |
| The mission debrief (#1740 P2): read a finished mission's summary and its facts from the records; regenerate the summary | GET …/debrief, POST …/debrief | P5 R/W (external: AI endpoint) |
| The mission shelf (#1740): read what a mission produced; set a verdict and note on one item; open a file item's bytes | GET …/shelf, PATCH …/shelf/{item}, GET …/shelf/{item}/diff, GET …/shelf/{item}/raw | P5 R/W (the bytes are n/a: HQ never moves files) |

#### Decisions, Needs you, notifications, Recent work, dashboard
| Capability | Route(s) | HQ |
|---|---|---|
| Needs-you list and one item's details | GET /api/pulse/needs-you, …/{sid}/details | have (`needs_you`, #2137; on a call `offer_decisions` puts a decision to the Commander, who answers it aloud); details P2 R |
| Approve a staged decision (also with edited text) / choose an option | POST /api/pulse/actions/{id}/approve, …/choose | **never** (a tap HQ proposes to approve another tap removes the tap; HQ points to that card) |
| Reject / dismiss a staged decision | POST /api/pulse/actions/{id}/reject | P2 W |
| Dismiss a session from Needs you | POST /api/pulse/needs-you/{sid}/dismiss | P2 W |
| Answer an agent's HQ report with one of its options (#1882) | POST /api/needs-you/{sid}/report/answer | **never** (the tap is the approval; HQ reads the report with `session_report` and points to the card) |
| Dismiss an agent's HQ report (#1882) | POST /api/needs-you/{sid}/report/dismiss | P2 W |
| Evidence behind a decision (screen / transcript / recap) | GET /api/pulse/evidence/{sid} | P2 R |
| Notifications: list, mark read (all or some), dismiss one / all, clear settled | GET/POST /api/pulse/notifications/* | have to list (`notifications`, #2137); P2 W the rest |
| Recent work recap (read, refresh) | GET/POST /api/pulse/recap | P2 R/W |
| Pulse overview cards; orchestrator health | GET /api/pulse, GET /api/pulse/orchestrator | P2 R |
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
| Sessions can report to HQ (#1882) | POST /api/prefs `hq_reports` | P2 W off only (turning it on gives new launches a capability: never) |
| Question routing (#2028): on/off, model, thinking | POST /api/prefs `hq_route` | P2 W (the model from the endpoint's own list, as `ai_review.model`) |
| Typed HQ turns and mission conversations (#2040): model, thinking | POST /api/prefs `hq_models` | P2 W (the model from the endpoint's own list, as `ai_review.model`; no new authority) |
| AI endpoint provider, URL and key; the egress acknowledgement; remove key; test | POST /api/prefs `ai_review`, POST /api/ai-review/endpoint/test | **never** (decides where HQ's own requests go, and who receives session content; `ai-key-origin.md`, `ai-providers.md`) |
| Connectors (#1750): list them and their state | GET /api/connectors | have (`list_connectors` reads the enabled ones; the store, not this route) |
| Connectors: add, set origin and token, test, enable or disable read, remove | POST/PATCH/DELETE /api/connectors/instances* | **never** (a connector token is a credential, and its origin decides where HQ's requests go; #1746 out of scope) |
| Connectors: a messenger's recipients and its send switch (#1832) | GET …/recipients/candidates, PUT …/recipients, POST …/capabilities `{send}` | **never** (who HQ may message is the Commander's consent, and so is the switch) |
| Connectors: a messenger's notify route, and its recent deliveries (#1841) | POST …/notify, GET …/deliveries | **never** to set it (where notifications go is the Commander's consent); P2 to read the deliveries |
| Send a message through a messenger connector (#1833) | the proposal decide route, `{decision: "send"}` | have (`propose_message` stages a card; only the Commander's tap sends) |
| Change or withdraw a card HQ proposed in this conversation (#2120) | the proposal decide route, `{decision: "dismiss"}` | have (`replaces` on a propose tool closes the card it corrects; `withdraw_card` dismisses one as the turn's operator; nothing of either card runs) |
| AI model (Auto or specific), thinking level, request timeout; list models; the provider table | POST /api/prefs `ai_review.model`, `ai_review.model_mode`, `ai_review.thinking`, GET /api/ai-review/models, GET /api/ai-review/providers | P2 W/R (model from the server's own list only) |
| Session review: enable, interval, depth, notify, rename sessions | POST /api/prefs `ai_review.enabled`, `session_review` | P2 W |
| Auto-sort settings | POST /api/prefs `auto_sort` | P2 W |
| Session clusters: on/off, the agent tool, the re-cluster interval (#1997) | POST /api/prefs `clusters` | P2 W, both directions (none widens autonomy or bypass: the agent tool reads its own group and posts notes, it cannot act on a session) |
| Orchestrator (session assistance / mission orchestration): interval, notify, stale hours, proposal lifetime | PATCH /api/automation/{scope} (revision-checked; Settings › HQ › Autonomy and the mission thread's *turn off AI-written directions*) | P2 W |
| Orchestrator thresholds (`confidence_min`, `ai_direction_confidence_min`, `judge_confidence_min`), `max_actions_per_pass` | PATCH /api/automation/{scope} | P2 W to raise a threshold or lower the cap only; the other direction widens autonomy and is **never** |
| Orchestrator **on**, autonomy **up**, AI directions auto-send **on**, allowed verbs widened | PATCH /api/automation/{scope} | **never** (widens autonomy); turning any of them **down or off** is P2 W |
| Orchestrator nudge text; orchestrator prompt | PATCH /api/automation/{scope} `nudge_template`; PATCH /api/prompts/{id} | **never** (text automation types into sessions; prompt text) |
| Pulse: the recent window (how far back needs-you, the recap and HQ look) | POST /api/prefs `pulse` | P2 W |
| Forge connection: URL, owner, token; clear token | POST /api/prefs `forge` | **never** (credential) |
| Prompts: read the catalog | GET /api/prompts | P2 R |
| Prompts: edit / reset | PATCH /api/prompts/{id} | **never** (the guarded `ask_orchestrator` prompt is HQ's own fence; others shape every model call) |
| Agents: roster, one agent's manifest, installation evidence | GET /api/engines, /api/engines/{id}, /api/plugins | #1554 + P6 R |
| Agent budgets: token limit, manual used, alert threshold, notify | PATCH /api/agents/budgets | P2 W |
| Agent operator models (add / remove a model id) | POST /api/prefs `agent_defaults.models` | P2 W |
| Agent session defaults (model, effort) | POST /api/prefs `agent_defaults.session_defaults` | P2 W |
| Model grades per agent (#1693): which grade each model is | POST /api/prefs `model_grades` | P2 W |
| Specialist pool limit (#1693): instances at once | POST /api/prefs `pool_max_instances` | P2 W |
| Default agent for new sessions | POST /api/prefs `agent_defaults.default_engine` | P2 W |
| Permission-bypass default **on** | POST /api/prefs `agent_defaults.bypass=true` | **never** (widens permission); off is P2 W |
| Chat-agent endpoint URL / key / model / tools | PATCH /api/agents/{engine}/endpoint | **never** (credential + where requests go) |
| Plugins: catalog refresh, auto-check daily | POST /api/plugins/feed/refresh, PATCH /api/agents/catalog/preferences | P6 W |
| Plugins: review + install, verify, sign in, endpoint candidate | POST /api/plugins/review, …/install, …/verify, …/signin, WS signin | **never** (runs fetched code; sign-in is an interactive vendor login) |
| Plugins: enable (activate), disable, remove, reload roster, recover, cancel operation | POST /api/plugins/activate, …/disable, …/remove, …/reload, …/recover, …/operations/{id}/cancel | P6 W (remove is destructive) |
| 2FA enroll / confirm / disable / recovery codes; password; sign out; device link | /api/2fa/*, /api/password, /change-password, /logout, /link/* | **never** (credentials and identity) |
| Artifacts (#1932): find by provenance and get the link | GET /api/artifacts, GET /api/artifacts/{id} | have (`artifacts_find`) |
| Artifacts: share publicly / revoke (the operator's act in the artifact's page) | POST, DELETE /api/artifacts/{id}/share | **never** (public exposure) |
| Artifacts: the global switch; activation per session, project, mission; delete | GET/PUT /api/artifacts/settings, GET/PUT /api/artifacts/activation, DELETE /api/artifacts/{id} | **never** (activation widens what agents may publish; delete is the operator's own act) |
| Public shares: list; revoke one; revoke all | GET /api/shares, DELETE /api/shares/{id}, POST …/revoke-all | P6 R/W |
| Public shares: create (view only) | POST /api/shares mode=view | P6 W (external: public exposure; password and expiry typed on the card) |
| Public shares: create "can send messages" | POST /api/shares mode=send | **never** (lets outsiders drive an agent) |
| Reply links (#1956): create; list; revoke; revoke all | POST /api/reply-shares; GET /api/reply-shares, DELETE /api/reply-shares/{id}, POST …/revoke-all | **never** to create (publishes to anyone with the link); P6 to list and revoke |
| Updates: version, settings, check; auto-update, channel | GET /api/version, /api/update/*, POST /api/update/settings | P6 R/W |
| Update now / reinstall (restarts the service) | POST /api/update/apply, GET /api/update/progress | P6 W (external; the card says the conversation reconnects) |
| Usage analytics consent | POST /api/prefs `analytics_consent` | **never** (consent is the operator's own act) |
| Host and resources (read); resource limits (edit) | GET /api/system, /api/system/resources; POST /api/system/resources | P6 R/W |
| The session limit: the count and what runs (#1819) | GET /api/sessions/capacity | P6 R |
| Maintenance: scrollback size and clear; prune (preview, prune); compact (measure, compact) | GET/POST /api/scrollback*, /api/maintenance/prune, /api/maintenance/compact | P6 R/W (destructive) |
| Peers (#1246): this instance's id, paired instances and whether each is connected; revoke a peer | GET /api/federation, POST /api/federation/peers/{peer_id}/revoke | P6 R/W (revoking narrows) |
| Peers (#1246): make or withdraw an invite, accept one, confirm or deny the short code, rename a peer | POST /api/federation/invites, DELETE …/invites/{invite_id}, POST /api/federation/pairings, POST …/pairings/{pairing_id}/confirm, …/deny, PATCH /api/federation/peers/{peer_id} | **never** (pairing decides which other instance this one trusts; the confirmation is the Commander's own comparison of two screens) |
| Sessions shared with a peer (#2054): the grants of this instance (list); end one | GET /api/federation/grants, POST /api/federation/grants/{grant_id}/revoke | P6 R/W (revoking narrows) |
| Sessions shared with a peer (#2054, #2056, #2057): share an API session with a paired instance (view, suggest or send), or a terminal session's screen (view only); change a grant's expiry or level | POST /api/federation/grants, PATCH /api/federation/grants/{grant_id} | **never** (decides what another instance may read of a session, whether it may propose or send messages to its agent, and for how long) |
| Sessions a peer shares with this instance (#2054): the list; read one (snapshot, events, older turns) | GET /api/federation/remote, GET …/remote/{peer_id}/{grant_id}/snapshot, …/events, …/history | P6 R |
| A terminal session a peer shares with this instance (#2056): its current screen as a redacted text frame (view only; nothing is typed into or resized on the owner's terminal) | GET /api/federation/remote/{peer_id}/{grant_id}/frame | P6 R |
| What a peer suggests for a session (#2057): the list with its text; approve one (also with edited text) | GET /api/federation/suggestions, POST /api/federation/suggestions/{suggestion_id}/approve | **never** (approve is a tap that approves another party's proposal; the list returns unapproved text another party wrote, so it is in no model-facing read tool, generic ones included) |
| What a peer suggests for a session (#2057): reject one | POST /api/federation/suggestions/{suggestion_id}/reject | P6 W (it narrows; the card names the peer and the session, never the suggested text) |
| Writing to a session a peer shares with this instance (#2057): suggest a message, send one, read the states of one's own suggestions, withdraw one | POST /api/federation/remote/{peer_id}/{grant_id}/suggest, …/turns, GET …/suggestions, POST …/suggestions/{suggestion_id}/withdraw | **never** for suggest and send (a model must not send to another instance's agent); the states read and the withdraw are P6 R/W (ids and states, no text; withdrawing narrows) |
| What became of one message this instance submitted to a session a peer shares (#2061): by its retry key, the stored delivery state, the suggestion's state, or unknown | GET /api/federation/remote/{peer_id}/{grant_id}/operations/{client_key} | P6 R (a state and no text; unknown is never reported as "did not arrive") |
| Working together, the owner's side (#2059): what peers ask for, with their note (list); the notes of a share (read, add, edit, delete, mark read) | GET /api/federation/access-requests, GET /api/federation/grants/{grant_id}/notes, POST …/notes, PATCH and DELETE …/notes/{note_id}, POST …/notes/read | **never** (a request's note and a share's notes are text another operator wrote, and a note is a message between two people: no note is ever model input, so none is in any model-facing read tool, generic ones included, and no model writes one. **Granting on request is not a route at all**: it is the grant change above, which is already `never`) |
| Working together, the owner's side (#2059): decline an access request; hide a note from the peer | POST /api/federation/access-requests/{request_id}/decline, POST /api/federation/grants/{grant_id}/notes/{note_id}/hide | P6 W (both narrow; the card names the peer and the session, never the request's note or the note's text) |
| Working together, the receiving side (#2059): ask the owner for more access; answer a pending question or tool permission of a shared session; read and write the notes of a share | POST /api/federation/remote/{peer_id}/{grant_id}/access, POST …/decisions, GET and POST …/notes, PATCH and DELETE …/notes/{note_id}, POST …/notes/read | **never** (a model must not answer a decision on another instance's agent, ask another operator for authority, or read or write a note) |
| Working together, the receiving side (#2059): whether an access request was answered | GET /api/federation/remote/{peer_id}/{grant_id}/access | P6 R (a level and a state; presence, `watching` and `owner_present`, rides on the grants list and the snapshot read above) |
| Working together, the receiving side (#2062): take one's own pending access request back | POST /api/federation/remote/{peer_id}/{grant_id}/access/withdraw | P6 W (it narrows: a request is a record, and withdrawing it changes no grant) |
| Continue here (#2059): start a new LOCAL session briefed from a session a peer shares (prepare, preview, create) | POST /api/federation/remote/{peer_id}/{grant_id}/continue, GET …/continue/{handle}, POST …/continue/{handle}/create | P7 W (as the handoff: it starts an agent, and the seed is shown verbatim on the card; the seed frames the shared record as data) |
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
| Secret files: upload / replace (file chosen on the card, or a Secret-shared chat attachment moved server-side); agent access on / off; the projects a secret is bound to; its access log | PUT /api/variables/{vid}/file; PATCH /api/variables/{vid} (`agent_access`); PUT /api/variables/{vid}/projects; GET /api/variables/{vid}/access-log | #1786 W (P4a: `list_secrets`, `secret.create` with the value typed on the card, `secret.access_on` / `access_off`, never voice- or auto-confirmed; P4b: `secret.store_attachment`, the custody-transfer move of a Secret-shared chat attachment) |
| Legacy template-variables routes (API-only) | /api/template-variables* | P3 (via the same store) |
| Project MCP servers (#2163): list, add, edit, remove; enable per project; check; review and install a package | GET/POST/PATCH/DELETE /api/mcp/servers*, PUT …/projects, POST …/check, …/review, …/install, GET /api/mcp/operations/{op} | have (P2 of #2163: `list_mcp_servers`; `propose_mcp` create / install / check / enable cards; a project's own credential through `propose_secret` create with a project, a shared one through `propose_secret` bind; never voice- or auto-confirmed, never a credential value) → edit and remove: #2163 (the Library page) |
| Memory: projects, notes, inbox, quarantine; export | GET /api/memory*, …/export | P3 R |
| Memory: bounded on-demand notebook discovery, indexed search and current note read | shared Memory service | **have** (#2036: current-turn refs; no startup preload) |
| HQ memory: explicit remember and confirmable correction / forget | common admission; existing cards | **have** (#2036; HQ scope only, no operational authority) |
| Memory capture and embedding connection, test and key | /api/memory/_hq/capture; /api/memory-search/settings and …/test | **never** (operator changes consent and outbound destination) |
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
| Share bundles: export skills / playbooks to a file; import a file through a preview (#1714) | GET /api/share/catalog, …/imports, POST /api/share/export, …/import/preview, …/import | n/a (a file downloaded to and uploaded from this browser; HQ never moves files) |
| Automations: list, detail | GET /api/automations, /api/automations/{aid} | have (`list_automations`, `automation_detail`, #1611) |
| Automations: runs, run detail, origins | GET /api/automations/{aid}/runs, …/runs/{run_id}, …/origins | P4 R |
| Automations: create, edit | POST /api/automations, PATCH /api/automations/{aid} | have (`propose_library`: saved off; an enabled one's widening edit is a consent card; never bypass on or auto-choose, #1611) |
| Automations: duplicate, delete | POST/DELETE /api/automations* | P4 W |
| Automations: a review loop's webhook secret (make or rotate, shown once) (#2202) | POST /api/automations/{aid}/webhook-secret | **never** (a credential) |
| Automations: pause, resume, turn off, run now | POST …/pause, …/resume, …/disable, …/run | P4 W (run now starts agents) |
| Automations: enable (consent to its scope) | POST …/enable `revision, consent, scope_digest` | have (`propose_library`: the card shows the server's own scope lines; the tap is the consent, #1611) |
| Specialists (#1693): list | GET /api/specialists | have (`list_specialists`, #1910) |
| Specialists (#1693): detail, a pinned version | GET /api/specialists/{sid}, …/versions/{n} | P4 R |
| Specialists: create, edit (a new version), archive, unarchive | POST /api/specialists, PATCH /api/specialists/{sid}, POST …/archive, …/unarchive | P4 W (revision-checked; the grade is derived, never chosen) |
| Specialists: release a held slot (#1693 S4) | POST /api/specialists/reservations/{rid}/release | P4 W (only a slot reconcile holds) |
| Mission plans (#1924, #1613 P7a): list, read one with its derived steps | GET /api/mission-plans, GET /api/mission-plans/{pid} | P5 R |
| Mission plans: archive / unarchive; save a mission's plan revision as a mission plan | POST /api/mission-plans/{pid}/archive, POST /api/missions/{mission_id}/plan/save-as-plan | P5 W (revision-checked; inert: nothing is planned or launched) |

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
| POST /api/pulse/chat | n/a (HQ is its successor; #1443 retires `pulse/chat`) |
| GET /api/agents/catalog, POST /api/agents/catalog/refresh | P6 (aliases of `/api/plugins`) |
| GET /api/perf, GET /api/system/sessions, GET /healthz, GET /api/auth-check | n/a (diagnostics / infrastructure) |
| POST /api/folders/mkdir | P1 (same as /api/fs/mkdir) |
| GET /api/structured/clients/{engine}, GET …/containment | P7 R |
| /p/*, /api/public/* | n/a (the public share visitor's surface) |
| /artifacts/* | n/a (hosted artifact bytes; HQ hands out the link, never the bytes) |

## Never through HQ (recorded decision, §3b)

Each row is a credential, a grant of more permission or autonomy, or a tap that would approve another tap. A prompt-injected screen could turn any of them against the operator. **Turning a permission or an autonomy down is always allowed; turning it up is never.** Each row can be moved by a later operator-approved issue with its own security review.

| Group | Rows | Why |
|---|---|---|
| Credentials and identity | password, 2FA, sign out, device link, AI endpoint URL/key, chat-agent endpoint URL/key, forge token, connector setup (origin, token, test, enable, remove) | credentials; and the endpoints decide where HQ's own requests go |
| HQ's own fence | prompt edits | the guarded `ask_orchestrator` prompt is HQ's fence |
| Widening permission or autonomy | bypass defaults on (global or per project), skip permissions, approve-always grants, orchestrator on, autonomy up, AI directions auto-send on, allowed verbs widened, mission auto-choose on | HQ would grant itself or agents more power |
| Approving taps | approve or choose a staged decision, approve a tool request, answer a permission prompt | a proposal to approve a proposal removes the operator's tap |
| Running fetched code / vendor login | plugin review/install/verify/sign-in | supply-chain and credential surface |
| Outsiders driving agents | public share in "can send messages" mode | external control of an agent |
| Consent | analytics consent | consent is the operator's own act |
| Trusting another instance | federation invites, accepting one, confirming or denying a pairing, renaming a peer (#1246) | a paired instance can later be granted access to sessions; who is trusted, and under which name it is shown, is the Commander's decision |
| Sharing a session with another instance | granting a paired instance access to a session, changing that grant's expiry or level (#2054, #2057) | it decides what another instance's operator may read, and whether that instance may propose or send messages to the agent; ending a grant narrows and is allowed |
| A peer's suggestions | approving a message a paired instance suggested; reading the suggestions (#2057) | approving is the Commander's tap on another party's proposal, and an unapproved suggestion is text a third party wrote: it reaches no model before a human on this side approved it. Rejecting narrows and is allowed |
| Driving another instance's agent | suggesting or sending a message to a session a peer shares with this instance (#2057) | a model here must not instruct an agent on someone else's machine; the Commander writes those messages |


## Route map

```hq-routes
GET    /                                                                          n/a  # SPA shell
POST   /api/2fa/confirm                                                           never  # credentials
POST   /api/2fa/disable                                                           never  # credentials
POST   /api/2fa/enroll                                                            never  # credentials
POST   /api/2fa/recovery-codes                                                    never  # credentials
PATCH  /api/agents/budgets                                                        P2
GET    /api/agent-accounts                                                         P2  # read: pools, labels and health, never a path (#1820, HQ is its P5)
POST   /api/agent-accounts/{engine}                                                P2
PUT    /api/agent-accounts/{engine}/order                                          P2
PUT    /api/agent-accounts/{engine}/new-sessions                                   P2
PATCH  /api/agent-accounts/{engine}/{account}                                      P2
DELETE /api/agent-accounts/{engine}/{account}                                      P2
POST   /api/agent-accounts/{engine}/{account}/status                               P2  # external: the vendor's status command
POST   /api/agent-accounts/{engine}/{account}/signin/cancel                        never  # ends an interactive vendor login
POST   /api/agent-accounts/{engine}/{account}/signin/callback                      never  # carries a one-use OAuth code from the operator's browser (#2106)
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
GET    /api/ask/conversations/{cid}/attachments/{aid}                     n/a  # HQ itself (a file shared in the chat, #1707)
POST   /api/ask/conversations/{cid}/attachments                            n/a  # HQ itself (share a file in the chat, #1707)
DELETE /api/ask/conversations/{cid}/attachments/{aid}                     n/a  # HQ itself (take back an unsent file, #1707/#1720)
DELETE /api/ask/conversations/{cid}                                               n/a  # HQ itself (delete a conversation, #1564)
DELETE /api/ask/conversations/{cid}/follow/{key:path}                             n/a  # HQ itself
POST   /api/ask/conversations/{cid}/message                                       n/a  # HQ itself
POST   /api/ask/conversations/{cid}/message/stream                                n/a  # HQ itself
POST   /api/ask/conversations/{cid}/seen                                          n/a  # HQ itself
POST   /api/ask/conversations/{cid}/turns/{turn_id}/stop                         n/a  # HQ itself (stop one running turn, #2076)
GET    /api/ask/proposals/{pid}/content                                           n/a  # HQ itself (a library card's draft, #1611)
POST   /api/ask/proposals/{pid}/decide                                            n/a  # HQ itself
GET    /api/ask/unread                                                            n/a  # HQ itself
GET    /api/hq/dispatches                                                         n/a  # HQ itself (its Dispatches, #1837)
GET    /api/hq/dispatches/{bid}                                                   n/a  # HQ itself (one dispatch, #1837)
POST   /api/hq/dispatches/seen                                                    n/a  # HQ itself (the unread cursor, #1837)
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
POST   /api/automations/{aid}/webhook-secret                                      never  # a credential, shown once (#2202)
GET    /api/automations/{aid}/runs                                                P4
POST   /api/chat/new                                                              P7
GET    /api/chat/{sid}                                                            P7
POST   /api/chat/{sid}/messages                                                   P7
POST   /api/chat/{sid}/turns/{turn_id}/proposals/{proposal_id}/decide             P7
POST   /api/chat/{sid}/turns/{turn_id}/retry                                      P7
GET    /api/clusters                                                              P2  # read; HQ tools land with #1997 P5
POST   /api/clusters                                                              P2  # create, on a card (#1997 P5)
POST   /api/clusters/recluster                                                    P2  # re-cluster now (external: AI), on a card (#1997 P5)
PATCH  /api/clusters/{cid}                                                        P2  # rename / keep / let HQ rename, on a card (#1997 P5)
DELETE /api/clusters/{cid}                                                        P2  # dissolve, on a card (#1997 P5)
POST   /api/clusters/{cid}/merge                                                  P2  # on a card (#1997 P5)
GET    /api/config                                                                n/a  # app shell read
GET    /api/connectors                                                            have
POST   /api/connectors/instances                                                  never
DELETE /api/connectors/instances/{iid}                                            never
PATCH  /api/connectors/instances/{iid}                                            never
POST   /api/connectors/instances/{iid}/capabilities                               never
GET    /api/connectors/instances/{iid}/deliveries                                 P2  # read: what a notify route sent (#1841)
POST   /api/connectors/instances/{iid}/notify                                     never  # consent to where notifications go (#1841)
GET    /api/connectors/instances/{iid}/recipients/candidates                      never  # consent to a messenger's recipients (#1832)
PUT    /api/connectors/instances/{iid}/recipients                                 never  # consent to a messenger's recipients (#1832)
POST   /api/connectors/instances/{iid}/test                                       never
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
GET    /api/reply-shares                                                          P6  # list (#1956)
POST   /api/reply-shares                                                          never  # publishes a reply to anyone with the link: the operator's own act (#1956)
POST   /api/reply-shares/revoke-all                                               P6  # narrowing (#1956)
DELETE /api/reply-shares/{share_id}                                               P6  # narrowing (#1956)
GET    /api/share/catalog                                                         n/a  # share bundles (#1714): the export picker
GET    /api/share/imports                                                         n/a  # share bundles: which copies came from a file
POST   /api/share/export                                                          n/a  # share bundles: a file download
POST   /api/share/import/preview                                                  n/a  # share bundles: a file upload
POST   /api/share/import                                                          n/a  # share bundles: confirms an uploaded file's preview
GET    /api/mcp/servers                                                           have  # list_mcp_servers (#2163 P2)
POST   /api/mcp/servers                                                           have  # propose_mcp create, the server as a bound draft on the card (#2163 P2)
DELETE /api/mcp/servers/{sid}                                                     #2163
GET    /api/mcp/servers/{sid}                                                     have  # list_mcp_servers per-project state; the cards' facts
PATCH  /api/mcp/servers/{sid}                                                     #2163
POST   /api/mcp/servers/{sid}/check                                               have  # propose_mcp check; the outcome in words + counts
PUT    /api/mcp/servers/{sid}/projects                                            have  # propose_mcp enable, bound to the revision
PUT    /api/mcp/servers/{sid}/projects/{pid}/credentials                          have  # propose_secret create with project + server: the value typed on the card (#2163 P2)
POST   /api/mcp/servers/{sid}/review                                              have  # propose_mcp install runs it at staging
POST   /api/mcp/servers/{sid}/install                                             have  # propose_mcp install, the reviewed lock only
GET    /api/mcp/operations/{op_id}                                                have  # propose_mcp install: the review it waits on, the install it starts
GET    /api/maintenance/compact                                                   P6
POST   /api/maintenance/compact                                                   P6
GET    /api/maintenance/prune                                                     P6
POST   /api/maintenance/prune                                                     P6
GET    /api/memory                                                                P3
POST   /api/memory-search                                                        have  # search_memory, shared scoped service (#2036)
GET    /api/memory-search/status                                                 n/a   # safe search health counts
GET    /api/memory-search/settings                                               never  # connection metadata, operator settings
PATCH  /api/memory-search/settings                                               never  # outbound destination and encrypted key
POST   /api/memory-search/test                                                   never  # operator-triggered draft destination test
PUT    /api/memory/_hq/capture                                                    never  # explicit capture consent
GET    /api/memory/{pid}/notes/{slug}                                             have  # current-turn read_memory references
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
GET    /api/mission-plans                                                         P5
GET    /api/mission-plans/{pid}                                                   P5
POST   /api/mission-plans/{pid}/archive                                           P5
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
POST   /api/missions/{mission_id}/plan/ops                                        P5
GET    /api/missions/{mission_id}/plan/approval                                   P6
POST   /api/missions/{mission_id}/plan/approve                                    never  # the Commander's tap (D3)
POST   /api/missions/{mission_id}/plan/approval/revoke                            P6
POST   /api/missions/{mission_id}/plan/consent                                    never  # standing consent is the Commander's
POST   /api/missions/{mission_id}/plan/consent/revoke                             P6
GET    /api/missions/{mission_id}/plan/revisions/{revision}                       P5
GET    /api/missions/{mission_id}/plan/team                                       P5
GET    /api/missions/{mission_id}/plan/team/choices                               P5
POST   /api/missions/{mission_id}/plan/save-as-plan                               P5
POST   /api/missions/{mission_id}/plan                                            P5
POST   /api/missions/{mission_id}/procedure                                       P5
GET    /api/missions/{mission_id}/procedures/{index}/inputs                       P5
GET    /api/missions/{mission_id}/procedures/projects                             P5
GET    /api/missions/{mission_id}/procedures                                      P5
POST   /api/missions/{mission_id}/relay                                           P5
GET    /api/missions/{mission_id}/screen/{session_key:path}                       P5
GET    /api/missions/{mission_id}/debrief                                         P5
POST   /api/missions/{mission_id}/debrief                                         P5
GET    /api/missions/{mission_id}/shelf                                           P5
PATCH  /api/missions/{mission_id}/shelf/{item_id}                                 P5
GET    /api/missions/{mission_id}/shelf/{item_id}/diff                            P5
GET    /api/missions/{mission_id}/shelf/{item_id}/raw                             n/a  # file bytes
POST   /api/missions/{mission_id}/spawn                                           P5
GET    /api/missions/{mission_id}/steps                                           P6
POST   /api/missions/{mission_id}/steps/{step_id}/start                           never  # the operator's tap; HQ's runner starts under an approval (#1951)
POST   /api/missions/{mission_id}/steps/{step_id}/stop                            P6
POST   /api/missions/{mission_id}/steps/{step_id}/hold                            never  # the operator's (clearing lets HQ start it)
POST   /api/missions/{mission_id}/budget                                          never  # caps are the operator's (raising widens)
POST   /api/missions/{mission_id}/steps/pause                                     P6  # narrows
POST   /api/missions/{mission_id}/steps/continue                                  never  # the operator's confirmation
POST   /api/missions/{mission_id}/steps/stop-all                                  P6  # narrows
POST   /api/missions/{mission_id}/state                                           P5
POST   /api/missions/{mission_id}/turns/{turn_id}/ack                             P5
POST   /api/missions/{mission_id}/unarchive                                       P5
POST   /api/needs-you/{session_id:path}/report/answer                             never  # the Commander's option tap IS the approval (#1882)
POST   /api/needs-you/{session_id:path}/report/dismiss                            P2  # dismiss an agent's report (#1882)
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
POST   /api/plugins/operations/{operation_id}/signin/callback                     never  # carries a one-use OAuth code from the operator's browser (#2106)
POST   /api/plugins/recover                                                       P6
POST   /api/plugins/reload                                                        P6
POST   /api/plugins/remove                                                        P6
POST   /api/plugins/review                                                        never  # runs fetched code / vendor login
POST   /api/plugins/signin                                                        never  # runs fetched code / vendor login
POST   /api/plugins/verify                                                        never  # runs fetched code / vendor login
GET    /api/plugins/{plugin_id}/generations/{generation_id}/activation            never  # plugin install surface
GET    /api/plugins/{plugin_id}/generations/{generation_id}/endpoint              never  # plugin install surface
PATCH  /api/plugins/{plugin_id}/generations/{generation_id}/endpoint              never  # plugin install surface
POST   /api/plugins/{plugin_id}/generations/{generation_id}/endpoint/test         never  # plugin install surface
POST   /api/prefs                                                                 prefs  # per field: see the prefs map
DELETE /api/profile/avatar                                                        n/a  # an image the operator picks in a file chooser (#1931)
GET    /api/profile/avatar                                                        n/a  # upload bytes
POST   /api/profile/avatar                                                        n/a  # an image the operator picks in a file chooser (#1931)
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
GET    /api/public/avatar                                                         n/a  # public share visitor surface
GET    /api/public/events                                                         n/a  # public share visitor surface
POST   /api/public/interrupt                                                      n/a  # public share visitor surface
POST   /api/public/name                                                           n/a  # public share visitor surface
GET    /api/public/reply/{share_id}                                               n/a  # reply-link visitor surface: ciphertext only (#1956)
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
POST   /api/pulse/chat                                                            n/a  # HQ's predecessors
GET    /api/pulse/evidence/{session_id:path}                                      P2
GET    /api/pulse/needs-you                                                       have
GET    /api/pulse/needs-you/{session_id:path}/details                             P2
POST   /api/pulse/needs-you/{session_id:path}/dismiss                             P2
GET    /api/pulse/notifications                                                   have
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
GET    /api/scrollback                                                            P6
POST   /api/scrollback/clear                                                      P6
GET    /api/sessions                                                              P1
GET    /api/sessions/capacity                                                     P6
POST   /api/sessions/archive-older                                                P1
GET    /api/sessions/{sid}                                                        P1
POST   /api/sessions/{sid}/archive                                                P1
PUT    /api/sessions/{sid}/cluster                                                P2  # move to a cluster / back to HQ's choice, on a card (#1997 P5)
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
GET    /api/specialists                                                           have  # list_specialists (#1910)
POST   /api/specialists                                                           P4
GET    /api/specialists/{sid}                                                     P4
PATCH  /api/specialists/{sid}                                                     P4
POST   /api/specialists/{sid}/archive                                             P4
POST   /api/specialists/{sid}/unarchive                                           P4
POST   /api/specialists/reservations/{rid}/release                                P4
GET    /api/specialists/{sid}/versions/{n}                                        P4
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
PUT    /api/variables/{vid}/file                                                  #1786  # secret files (P4 of #1786)
GET    /api/variables/{vid}/access-log                                            #1786  # secret access log (P4 of #1786)
PUT    /api/variables/{vid}/projects                                              have  # propose_secret bind: adds projects only, never voice (#2163 P2)
GET    /api/version                                                               n/a  # app shell read
GET    /change-password                                                           never  # identity
POST   /change-password                                                           never  # identity
GET    /healthz                                                                   n/a  # diagnostics
POST   /hooks/a/{aid}                                                             n/a  # the forge's signed webhook, not an operator action (#2202)
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
GET    /api/artifacts                                                             have
GET    /api/artifacts/settings                                                    never  # widens agents
PUT    /api/artifacts/settings                                                    never  # widens agents
GET    /api/artifacts/activation                                                  never  # widens agents
PUT    /api/artifacts/activation                                                  never  # widens agents
GET    /api/artifacts/{aid}                                                       have
DELETE /api/artifacts/{aid}                                                       never  # destructive
POST   /api/artifacts/{aid}/share                                                 never  # public exposure
DELETE /api/artifacts/{aid}/share                                                 never  # the share's own control
GET    /artifacts/p/{public_id}                                                   n/a  # hosted bytes
GET    /artifacts/p/{public_id}/{rest:path}                                       n/a  # the public viewer page (#2068)
GET    /artifacts/p/{public_id}/~/{rest:path}                                     n/a  # hosted bytes
GET    /artifacts/p/{public_id}/~meta                                             n/a  # the public viewer's title/kind/time
GET    /artifacts/v/{token}/{rest:path}                                           n/a  # hosted bytes
GET    /artifacts/v/{token}                                                       n/a  # hosted bytes
GET    /artifacts/{ref}                                                           n/a  # hosted bytes
GET    /artifacts/{ref}/{rest:path}                                               n/a  # hosted bytes
GET    /p/                                                                        n/a  # public share visitor surface
GET    /p/{token}                                                                 n/a  # public share visitor surface
GET    /r/                                                                        n/a  # reply-link visitor surface (#1956)
GET    /r/{share_id}                                                              n/a  # reply-link visitor surface (#1956)
GET    /{spa_path:path}                                                           n/a  # SPA shell
WS     /ws/term/{sid}                                                             P7  # terminal input; text nudges: have (send_nudge)
WS     /ws/plugins/signin/{operation_id}                                          never  # interactive vendor login
GET    /api/federation                                                            P6  # peers and their state (#1246)
POST   /api/federation/invites                                                    never  # trusting another instance
DELETE /api/federation/invites/{invite_id}                                        never  # part of the pairing the Commander runs
POST   /api/federation/pairings                                                   never  # trusting another instance
POST   /api/federation/pairings/{pairing_id}/confirm                              never  # the Commander compares the short code
POST   /api/federation/pairings/{pairing_id}/deny                                 never  # part of the pairing the Commander runs
PATCH  /api/federation/peers/{peer_id}                                            never  # the name a trusted instance is shown under
POST   /api/federation/peers/{peer_id}/revoke                                     P6  # narrows: ends trust in a peer
GET    /api/federation/grants                                                     P6  # what this instance shares with peers (#2054)
POST   /api/federation/grants                                                     never  # decides what another instance may read
PATCH  /api/federation/grants/{grant_id}                                          never  # decides for how long another instance may read
POST   /api/federation/grants/{grant_id}/revoke                                   P6  # narrows: ends one grant
GET    /api/federation/remote                                                     P6  # sessions peers share with this instance (#2054)
GET    /api/federation/remote/{peer_id}/{grant_id}/snapshot                       P6  # read a shared session (view only)
GET    /api/federation/remote/{peer_id}/{grant_id}/events                         P6  # read: cursors and kinds
GET    /api/federation/remote/{peer_id}/{grant_id}/history                        P6  # read: older turns
GET    /api/federation/remote/{peer_id}/{grant_id}/frame                          P6  # read: a shared terminal's screen as text (#2056)
GET    /api/federation/suggestions                                                never  # unapproved text another party wrote (#2057)
POST   /api/federation/suggestions/{suggestion_id}/approve                        never  # the Commander's tap on another party's proposal
POST   /api/federation/suggestions/{suggestion_id}/reject                         P6  # narrows: declines a peer's suggestion
POST   /api/federation/remote/{peer_id}/{grant_id}/suggest                        never  # a model must not send to another instance's agent
POST   /api/federation/remote/{peer_id}/{grant_id}/turns                          never  # a model must not send to another instance's agent
GET    /api/federation/remote/{peer_id}/{grant_id}/suggestions                    P6  # read: one's own suggestions' ids and states, no text
POST   /api/federation/remote/{peer_id}/{grant_id}/suggestions/{suggestion_id}/withdraw  P6  # narrows: takes a suggestion back
GET    /api/federation/remote/{peer_id}/{grant_id}/operations/{client_key}         P6  # read: what became of one's own message, a state and no text (#2061)
GET    /api/federation/access-requests                                            never  # a peer's own words: its note on a request (#2059)
POST   /api/federation/access-requests/{request_id}/decline                       P6  # narrows: declines a request for more access
GET    /api/federation/grants/{grant_id}/notes                                    never  # notes are between two operators, never model input
POST   /api/federation/grants/{grant_id}/notes                                    never  # a note is a person's message to a person
POST   /api/federation/grants/{grant_id}/notes/read                               never  # part of reading the notes
PATCH  /api/federation/grants/{grant_id}/notes/{note_id}                          never  # a note is a person's message to a person
DELETE /api/federation/grants/{grant_id}/notes/{note_id}                          never  # a note is a person's message to a person
POST   /api/federation/grants/{grant_id}/notes/{note_id}/hide                     P6  # narrows: hides a note from the peer
POST   /api/federation/remote/{peer_id}/{grant_id}/access                         never  # asks another operator for authority
GET    /api/federation/remote/{peer_id}/{grant_id}/access                         P6  # read: a level and a request's state
POST   /api/federation/remote/{peer_id}/{grant_id}/access/withdraw                P6  # narrows: takes one's own access request back (#2062)
POST   /api/federation/remote/{peer_id}/{grant_id}/decisions                      never  # a model must not answer for another instance's agent
GET    /api/federation/remote/{peer_id}/{grant_id}/notes                          never  # notes are never model input
POST   /api/federation/remote/{peer_id}/{grant_id}/notes                          never  # a note is a person's message to a person
POST   /api/federation/remote/{peer_id}/{grant_id}/notes/read                     never  # part of reading the notes
PATCH  /api/federation/remote/{peer_id}/{grant_id}/notes/{note_id}                never  # a note is a person's message to a person
DELETE /api/federation/remote/{peer_id}/{grant_id}/notes/{note_id}                never  # a note is a person's message to a person
POST   /api/federation/remote/{peer_id}/{grant_id}/continue                       P7  # Continue here: prepare a brief (#2059)
GET    /api/federation/remote/{peer_id}/{grant_id}/continue/{handle}              P7  # Continue here: the prepared brief again
POST   /api/federation/remote/{peer_id}/{grant_id}/continue/{handle}/create       P7  # Continue here: starts a new local session
WS     /ws/fed                                                                    n/a  # another instance's channel; Noise-authenticated, never a browser's
WS     /ws/agent-accounts/{engine}/{account}/signin                                never  # interactive vendor login (#1820)
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
clusters                        P2  # session clusters on/off, the agent tool, the interval; both directions (#1997)
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
hq_models                       P2  # typed/mission conversation model and thinking (#2040); model from the server's list
hq_reports                      P2  # off only; turning it on is never (#1882)
hq_route                        P2  # question routing (#2028): on/off, model from the server's list, thinking
model_grades                    P2  # which grade a model is; specialists derive theirs from it
onboarded                       n/a  # onboarding flag
operator_name                   n/a  # a name the operator types in Settings › Profile (#1942)
overview_expanded               n/a  # map cluster layout state
pool_max_instances              P2  # how many specialist instances may run at once
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
