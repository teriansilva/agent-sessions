# Actions

An **action** is one step an automation can take: start a session, message a session, run a review
loop, tell HQ, or start a mission. **Library → Actions** shows every action BattleLab has, so you
can see what each one does, how far it reaches and what it needs before you build an
[automation](/guide/automations) around it.

Nothing on this page runs anything. An automation runs an action only after you approve it, and the
wizard's **What** step picks from this same list.

## The gallery

Each action is one card:

- its **category** (BattleLab, Forge, …) and its **effect** tag (see below);
- its **name** and one line saying what it does;
- what it **needs**: **Ready**, or what is missing in its own words, for example "Needs an API agent
  that can start unattended (Settings → Agents)". A need that depends on what you pick in the
  wizard (which project, which session, which reviewer) is named after *Ready* and checked when you
  save;
- **used by**: how many of your automations use it.

Type in **Search actions** to filter by name or description, or press a category chip. Only the
categories that have an action are shown.

## What the effect means

The effect tag says how far an action's effect can reach, least to most. The pips count it; they
are a class, never a state, so they never turn green, amber or red.

| Effect | Pips | What it may change |
|---|---|---|
| **Read** | 1 | Nothing: it only reads. |
| **Notify** | 2 | Your notifications (the bell). *Tell HQ* is one. |
| **Write · BattleLab** | 3 | Things inside BattleLab: it starts or messages a session, or creates a mission draft. |
| **Write · external** | 4 | Something outside BattleLab, such as another service. |
| **Run · local** | 5 | It runs a program on this machine. |

Today every action is either *Notify* or *Write · BattleLab*. The other classes are listed so you
know what a later action means when it arrives.

## The actions today

| Action | Category | Effect | Started by |
|---|---|---|---|
| Start a mission | BattleLab | Write · BattleLab | a schedule, once, or Run now |
| Start a session | BattleLab | Write · BattleLab | any trigger |
| Message a session | BattleLab | Write · BattleLab | any trigger |
| Review loop | Forge | Write · BattleLab | a webhook only ([Review loop with Forgejo](/guide/review-loop)) |
| Tell HQ | BattleLab | Notify | any trigger |

What each one asks for, and how its approval works, is in [Automations](/guide/automations), step 3 (*What*).

## More actions later

More actions come in later releases: sending a message to a chat, working with files, running a
command or script you saved, and calling the web. Each will have the same card here, and each will
ask for your approval before an automation can use it. Categories without an action yet (Messaging,
Files, Run locally, Web) don't have a chip until their first action arrives.
