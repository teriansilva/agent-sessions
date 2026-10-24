# The operating model: Commander, HQ, missions, playbooks, skills and specialists

How BattleLab's work concepts fit together. The goal is that **all daily work can be handled by
agents**: code, and also mail, files, invoices and research. The operator (the **Commander**)
commands; HQ plans with them; specialists do the work.

This is the product contract for anything that plans, runs or shares work. Read it before you
change missions, playbooks, skills, specialists, HQ planning, automations or the gallery.

**Status.** This document describes the **target** model decided in #1613 (2026-10-23). Much of it
is not built yet. Sections mark what exists **today** and what is **target**; §10 summarises both, with the phase that
delivers each target. The user guide describes only what ships. A PR that changes the model
updates this document in the same PR.

---

## 1. Glossary

Every noun has one meaning. UI copy, docs and code comments use these words.

| Noun | Is | Contains / decides | Never | Status |
|---|---|---|---|---|
| **Commander** | The operator | Approves plans and gates; closes missions (a clean routine sortie may close itself, §5) | — | today |
| **HQ** | The Commander's planning and command staff: the HQ conversation, speaking as the selected HQ contact | Plans missions with the Commander; executes an approved plan | Approves a gate or its own plan; is a specialist. An HQ contact (a General) changes tone only | today (plans and executes: target P5/P6) |
| **Project** | A domain of work with a place: a repository, a mailbox, a cloud-files folder, a ledger | Exactly one playbook, project intel and equipment | Moves into another project | today (exactly one playbook, equipment: target P2/P4) |
| **Playbook** | The project's standard operating procedures: the playbook bundle | Named **procedures** plus materials (instructions, templates, variables) | Names a specialist, engine or model; says *what* to achieve | today (several procedures, roles: target P2) |
| **Procedure** | One flow in a playbook (*Ship a change*, *Investigate*, *Triage inbox*, *Chase unpaid invoices*) | Steps. Each step names a **role**: required skills, an optional minimum grade, its evidence, a bounded review loop, independence | — | today as a flow (roles: target P2) |
| **Skill** | Portable know-how: instructions plus files | — | Mentions a project, says "done", or grants access | today (stored, not yet activated: target P3) |
| **Equipment** | Access: MCP servers, tools, credentials, variable collections | — | Belongs to a specialist | target (P3/P6) |
| **Project intel** | Project-local knowledge: `CLAUDE.md`, invariants, and **project memory** (the BattleLab memory tool: per project, off by default, notes reviewed through an inbox, hints never instructions) | — | Travels with a specialist, or stores load-bearing state | today |
| **Agent** | An installed CLI or API client from the agent catalog (Settings → Agents). **Unchanged meaning** | The engines and models a specialist can run on | Is a specialist | today |
| **Specialist** | A job in the **pool**: a title (*Intern*, *Bookkeeper*, *Implementer*, *Reviewer*), its skills, a **preselected engine and model** plus effort, a **grade**, a **capacity** and a service record. Versioned | Skills, engine and model, grade, capacity | Runs by itself; holds equipment | target (P3) |
| **Pool** | The Commander's roster of specialists. Global; every mission is planned from it | Specialists | Belongs to one project | target (P3) |
| **Grade** | A specialist's model tier: **Intern** = fast, **Specialist** = strong, **Senior** = frontier | — | Rank. Rank is earned from outcomes | target (vocabulary P2, mapping P3) |
| **Session** | One **instance** of a specialist on **one procedure step**: a separately fenced episode | — | Outlives its step as an identity, or carries context to another step | today as a session (one per step: target P6) |
| **Mission plan** | A reusable plan: objectives, dependencies, project **slots**, role needs (skills and minimum grade) | — | Contains real projects, specialists or secret values | target (P7a) |
| **Mission** | A plan bound to real projects and specialists, executing | Objectives, the team, the event log, the conversation | Bypasses a project's playbook | today (one project; objectives per project: target P4) |
| **Objective** | One project, one of its procedures, and that procedure's inputs | Step states | — | today from a checklist (project + procedure: target P4) |
| **Patrol** | A session outside any mission: ad-hoc work. An API session by default; a terminal when chosen explicitly | — | — | today |
| **Standing operation** | An ongoing mission: a pinned mission plan plus triggers (schedule, event, loop until a condition), standing consent, limits and a **ledger**. It stays open and launches sorties | Triggers, limits, ledger, sortie history | Does work itself | target (P7b) |
| **Sortie** | One run of a standing operation: a normal, bounded mission launched by a trigger | Its objectives and outcome | Carries context to the next sortie except through the ledger | target (P7b) |
| **Ledger** | A standing operation's **server-owned structured state** between sorties: cursors, a "done" set and totals, kept beside the automation's claims and watermarks. Read at the start of a sortie and written when it closes | — | Is project memory; holds secrets | target (P7b) |
| **Automation** | A trigger source (schedule, loop, webhook). It fires sorties of a standing operation | — | Exceeds its consented scope | today (starts one-off missions or sessions; fires sorties: target P7b) |
| ~~Checklist~~ | **To be retired (P1).** It becomes a procedure step's evidence | — | — | **today:** Library → Checklists, picked per mission. **Retired in P1** |

In short: a **skill** is how to do a task, a **playbook** is how this project works, a **mission
plan** is what we achieve and in what order, and a **mission** is this work, now.

## 2. How they relate

```
COMMANDER ⇄ HQ ── plans with, then executes ──▶ MISSION ◀── instantiated from 0–1 ── MISSION PLAN
                                                  ├── OBJECTIVE ×n ─▶ PROJECT ─1:1─▶ PLAYBOOK ─▶ PROCEDURE ─▶ STEP
                                                  │                         (role: skills · min grade · evidence · review · independence)
                                                  └── TEAM ─▶ SPECIALIST ×n from the POOL ── one instance per step ─▶ SESSION
SPECIALIST = title + skills + preselected engine & model (grade) + capacity ── runs on ─▶ AGENT
PROJECT ─▶ intel + equipment      (issued to a session for its step only)
STANDING OPERATION ── a trigger (automation) fires ─▶ SORTIE = a bounded MISSION   (ledger between sorties)
```

### Rules

These rules are **target** (P2–P6) unless a rule says otherwise; §10 shows what exists today.

- **Project and playbook.** A project has exactly one playbook. A playbook has one or more procedures.
- **Mission scope.** A mission has one or more objectives. Each objective names exactly one project and one procedure, so a mission spans every project its objectives name.
- **What vs how.** The mission decides **what** happens and **in what order**. The project's playbook decides **how**. When no procedure fits, HQ drafts a **mission-local procedure**. It passes the same validator as a playbook's procedures, picks its targets by index from server-built lists, keeps the floor, and is shown in full when the Commander approves. The Commander may save it into the playbook.
- **Every objective has a project.** When none fits, HQ proposes creating one.
- **Roles and independence.** One specialist instance fills each step's role. Independence always means at least a **different instance**, never a shared context. A step may demand more: a different specialist, engine or model.
- **Skills.** Matched by **exact id**, never by similarity. A specialist whose engine cannot load a skill cannot hold a role that needs it.
- **Grade.** A step's minimum grade is checked against the **effective model**: the per-mission override if there is one, else the specialist's preselected model. It is checked at approval and again at every launch. By default, HQ proposes the **cheapest qualifying** specialist and says why.
- **Replication and capacity.** A specialist is a template, so many instances can run at once across missions. Its capacity, the concurrency caps and the engine usage budgets bound them. A launch reserves capacity atomically before its session starts, and releases it on every exit: completion, launch failure, cancellation, crash recovery.
- **Sessions.** One session per step, never one per objective. A specialist's skills and service record persist across sessions; its context never does.
- **Equipment.** Bound to the project. Issued to a session only after its capacity reservation succeeds, and revoked on every exit of that session.

## 3. What a specialist knows

| Layer | Example | Scope | Read by |
|---|---|---|---|
| **Project memory** (the BattleLab memory tool, today) | "Invoices live in /Finance/2026" | Project | Every session in the project |
| **Specialist notes** (target, P3) | Bookkeeper on Finance: "Acme pays ~3 weeks late" | Specialist × project | Every instance of that specialist in that project |
| **Skills** | "How to match bank lines to invoices" | Anywhere | Any specialist holding the skill |

**Today**, every session in a project with memory turned on reads that project's memory. The rest of this section is **target (P3)**:

- **Every instance starts informed:** its project's memory plus its specialist's notes there.
- **Notes belong to the specialist, not the instance,** so instances never diverge.
- **Nothing follows a specialist across projects except its skills.** A lesson that recurs across projects is promoted into a reviewed skill.
- **Both kinds of notes go through the same inbox review** and are hints, never instructions.

## 4. Lifecycle

**Today** a mission is created from an instruction plus a checklist, planned by a one-shot planner, and dispatched to terminal sessions. The lifecycle below is **target** (P4–P6):

1. **Plan.** The Commander and HQ draft objectives, from a mission plan or from scratch.
2. **Bind.** Each objective gets a project and one of its procedures, or a mission-local procedure.
3. **Assemble.** HQ matches every role's skills and minimum grade against the pool. It proposes a specialist per role, plus optional alternates, each with a one-line reason. The Commander adjusts, including per-step model overrides.
4. **Approve.** The Commander approves the enumerated plan (§6).
5. **Execute.** HQ starts one session per step as dependencies clear. Each step's evidence is judged independently. The floor and the playbook's approval gates always come to the Commander.
6. **Debrief.** The Commander closes the mission (a clean routine sortie may close itself, see §5). Service records update from judged outcomes only. The mission can be saved as a mission plan.

## 5. Ongoing work: standing operations and sorties

**Target (P7b).** Today an automation starts a new, unrelated mission or session on each run (§6).

A loop is never one mission that runs forever. A **standing operation** launches **sorties**, each a bounded mission with its own objectives, judge and pins, and a fresh session per step.

- **Ledger.** Server-owned structured state, never memory notes (notes are prose hints that go through review and can be quarantined; the ledger is not). Each sortie reads the ledger at start and writes it back when it closes, so completed items are not redone. **The ledger alone is not replay-safe:** an effect that happened before a crash, ahead of the close, is an *unknown outcome*. It is never replayed automatically: the item is checked at its source where possible, otherwise the sortie asks the Commander (P7b acceptance).
- **Concurrency.** One sortie at a time by default. A trigger that fires while one runs is skipped or queued.
- **Limits.** Max sorties per day, a budget per sortie, the cheapest qualifying specialists. A **circuit breaker** pauses the operation after repeated failures and tells the Commander. Pause, resume and stand down are always available.
- **Consent.** Standing consent covers the operation. Any change to its plan, playbooks, specialists or equipment voids it until re-consented.
- **The floor applies to every sortie, every time.**
- **Closing.** A sortie closes itself only when every objective is judged met, no floor item is pending, and the standing consent grants *routine close*. Everything else waits for the Commander, who gets a digest instead of a close button per sortie. The Commander closes one-off missions and the standing operation itself.

## 6. Authority

**Today.**

- **HQ (Ask)** reads, asks and proposes. None of its tools launches, dispatches, types or changes anything; only the operator's tap does (`docs/invariants/ask-orchestrator.md`).
- **Automations** (`docs/invariants/automations.md`) are the existing standing authority. Once the operator consents to an automation's scope (a digest the server shows), its `start_mission`, `start_session` and `send_to_session` actions run on their trigger without a fresh tap. They do so within that pinned scope, with caps, expiry and re-approval on drift.
- **Plan approval** as authority does not exist today.

**Target (P6), not in force.** It takes effect only when P6 ships, after the Commander records it in their own comment on #1613. Until then the rule above holds. **Approving a plan is the tap.** It
authorises HQ to launch exactly the enumerated specialist instances on exactly the enumerated steps
at that plan revision, and nothing else.

- **Pinned:** the plan revision; each objective's project, procedure and playbook revision; the specialist versions and each step's effective engine and model; the equipment per project; the allowed actions. The approval stores their digest.
- **Re-checked at every launch and every gate.** A stale binding refuses that launch with its reason. Nothing is silently remapped. Any addition or swap (specialist, alternate, model, objective, project, equipment) asks again.
- **Standing consent (automations).** A consent record with the server's scope lines and digest, an expiry the Commander sets, and revocation. It is voided by any change to the plan, playbook, specialists or equipment. Importing a plan, or approving one earlier, is never standing consent.
- **Unchanged.** The floor (irreversible, outward, money, credentials) always reaches the Commander. HQ's never-list stays: HQ never approves a gate, a tool request or a floor item, and never widens permission or autonomy. Permission bypass does not apply to mission step sessions.
- **The Commander closes missions.** The judge can at most move a mission to review (`docs/invariants/mission-judge.md`). The one exception is a clean routine sortie (§5).

A message **to the Commander** (for example "these invoices are unpaid") uses BattleLab's own
notification path and is not an outward action. Sending anything to anyone else is a floor item.

## 7. API first

**Target**, rolled out as API clients become mission-ready (#1273). API sessions are the preferred execution surface: terminals are not reliable enough to drive
unattended.

**Today.** Missions and automations launch and drive terminal sessions. **Target** (as API clients become mission-ready):

- **Missions and automations** run only on API sessions.
- **New sessions** default to the engine's API client when one is ready. A terminal is an explicit choice.
- **Terminal sessions** stay available for interactive work and for engines without an API client. They get no new agent-driving features (nudges, auto-answers, unattended starts, screen-read supervision).
- **An engine without a mission-ready API client** cannot hold a mission role.
- **Unattended means API, everywhere.** Nothing that runs without the Commander watching drives a terminal: mission dispatch, sorties, automations and orchestrator nudges. Terminal input is timing-based and fails too often unattended. Existing terminal-targeting automations are legacy: they keep working, are labelled unreliable, and get no new triggers. Nudges to terminal sessions stay proposals the Commander taps.
- **The critical path:** until an API client is mission-ready, daily work cannot run unattended.

## 8. Galleries

**Today** the gallery carries playbooks and skills. Mission plans are **target (P7a)**.

There are three public kinds: **playbooks** and **skills** (the existing feed), and **mission plans** (a separate feed).

- **The existing feed stays exactly as it is.** It is format 1, `playbook | skill`, so older clients never see a mission plan.
- **New clients read both feeds**, and keep working when the mission-plan feed is missing.
- **Publishing policy for mission plans:**
  - no secret values;
  - no real account or project identifiers (slots only);
  - nothing whose purpose is to infringe someone else's IP, break a platform's terms or send unsolicited messages;
  - publisher review.
- **Import is inert.** It runs nothing until a Commander plans and approves a mission from it.

## 9. Decisions

"Operator" means the Commander decided it; "derived" means the architect chose it as reversible or strictly safer (workflow §3b), and it may be overridden.

| # | Decision | Date · source |
|---|---|---|
| D1 | "Agent" keeps meaning the CLI or API client; the workers are **Specialists** in the **Pool**. No product rename | 2026-10-23 · operator (specialist pool; "agents are agents" kept) |
| D2 | HQ plans every mission with the Commander, on a tool-capable API session. It is never a specialist | 2026-10-23 · operator ("D2–D4 as recommended") |
| D3 | Approving the enumerated plan is the tap (§6). Shipping it waits for the operator's own recorded approval | 2026-10-23 · operator in chat; requires the operator's own comment on #1613 before P6 |
| D4 | The Commander closes missions | 2026-10-23 · operator ("D2–D4 as recommended") |
| D5 | Specialists preselect an engine and a model and are versioned; a mission pins the version. A per-mission model override never edits the specialist | 2026-10-23 · operator ("preselected model", "model and engine") |
| D6 | Specialists replicate; capacity replaces exclusivity. Approved alternates per role | 2026-10-23 · operator ("a pool … each specialist is replicated") |
| D7 | Missions run on API sessions only; patrols may be terminal or API | 2026-10-23 · derived (#1472 §2), confirmed by D17 |
| D8 | Permission bypass stays off for mission step sessions | 2026-10-23 · derived, strictly safer (#1581) |
| D9 | Every objective names a project; mission-local procedures are allowed, under the same validation | 2026-10-23 · derived (reversible) |
| D10 | Checklists retire into procedure evidence, through a reversible migration | 2026-10-23 · operator ("checklists are probably redundant") |
| D11 | Equipment binds to projects and lives only as long as one step's session | 2026-10-23 · derived, least privilege |
| D12 | Service records and ranks count judged outcomes only, never tokens, time or session counts | 2026-10-23 · derived (honest gamification) |
| D13 | The military vocabulary is a UI skin; the product nouns above stay plain | 2026-10-23 · derived (avoid naming churn) |
| D14 | Skills match by exact id | 2026-10-23 · derived (auditable matching) |
| D15 | One session per step | 2026-10-23 · derived from Hermes review (aligns #1581) |
| D16 | Mission plans get a separate gallery feed | 2026-10-23 · derived from Hermes review (gallery compatibility) |
| D17 | API sessions are preferred for all agent work | 2026-10-23 · operator ("prefer api agents … terminals are not reliable enough") |
| D18 | Grades are model tiers, checked on the effective model at approval and at every launch | 2026-10-23 · operator ("simpler models can do simpler work") |
| D19 | Ongoing work is a standing operation that launches bounded sorties, with a ledger, limits and a circuit breaker | 2026-10-23 · operator ("the sortie concept seems to make sense") |
| D20 | A clean sortie under *routine close* consent closes itself into a digest; everything else waits for the Commander | 2026-10-23 · derived; the operator accepted the sortie concept including it |
| D21 | Knowledge has three layers: project memory (per project), specialist notes (per specialist × project) and skills (anywhere). Nothing follows a specialist across projects except its skills; load-bearing state never lives in memory | 2026-10-23 · operator asked; architect's choice ("you choose the best solution") |

## 10. Today vs target

| Concept | Today on `main` | Target, and the phase that delivers it |
|---|---|---|
| Glossary, this document | — | P0 |
| Checklists | Library → Checklists; missions pick one | Retired into procedure evidence (P1) |
| Playbook steps | Name an engine and a model; one flow per bundled playbook | Role + skills + minimum grade; several procedures per project (P2) |
| Project memory | The BattleLab memory tool, per project | Unchanged; plus specialist notes per specialist × project (P3) |
| Specialists, the pool | Not present; skills are stored but turned on nowhere; client profiles per engine | A versioned pool on client profiles; skills activated per session (P3) |
| Mission scope | One project per mission | One project per objective; single project first, then cross-project (P4) |
| Planning | A one-shot planner; the lead agent is planned in #1472 | HQ plans with the Commander (P5, needs mission-ready API clients) |
| Execution | Terminal-supervised missions | API sessions under plan approval (P6, needs mission-ready API clients and the recorded D3 approval) |
| Mission plans | — | Library and a separate public feed (P7a) |
| Ongoing work | Automations start one-off missions or sessions | Standing operations launching sorties, with a ledger (P7b, after P6) |
| The map | Sessions map | THEATRE campaign map (P8) |
| Ranks | Brand fiction only | Outcome-only service records (P9) |

## 11. Proving scenario

The model is accepted when one synthetic mission runs end to end on a test install (fake mailbox,
test files folder, fixture ledger; no real accounts):

> *"Sort today's mail, file the attachments, find invoices that are still unpaid and remind me."*

- Three projects, each with its own procedure and equipment:
  - *Mail*: *Triage inbox*; equipment is mailbox read plus moving messages between folders.
  - *Files*: *File attachments*; equipment is write access to the target folder.
  - *Finance*: *Chase unpaid invoices*; equipment is ledger read.
- Three specialists, each with its own session per step, proposed by HQ with reasons and approved once:
  - an Intern-grade **triager** on *Mail*;
  - an Intern-grade **filer** on *Files*, which receives the attachments as the triage step's recorded outputs;
  - a Specialist-grade **bookkeeper** on *Finance*.
- Equipment boundaries, asserted from the grant records:
  - the triager's sessions hold only mail equipment;
  - the filer's sessions hold only files-folder write;
  - the bookkeeper's sessions hold only ledger read;
  - no session ever holds another project's equipment.
- The reminder to the Commander arrives through BattleLab's notifications with no extra approval.
- The same mission edited to "email the customer" stops at the floor and waits.
- The Commander closes the mission, and the service records show the judged outcomes.
- Run as a standing operation ("every weekday 07:00"), sortie 2 skips what sortie 1 handled, a clean sortie closes itself into the digest, and "email the customer" still stops and asks.
