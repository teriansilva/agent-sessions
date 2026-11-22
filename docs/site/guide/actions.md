# Actions

An **action** is one step an automation can take: start a session, message a session, run a review
loop, tell HQ, start a mission, or send a message through a messenger you set up. **Library → Actions** shows every action BattleLab has, so you
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

An action whose need is checked and missing (for example *Send a message* before any messenger can
send) can't be picked in the wizard. Its row has a dashed border, says what is missing, and has a
**Set up** link to the place in Settings where you fix it.

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

The other classes are listed so you know what a later action means when it arrives.

## The actions today

| Action | Category | Effect | Started by |
|---|---|---|---|
| Start a mission | BattleLab | Write · BattleLab | a schedule, once, or Run now |
| Start a session | BattleLab | Write · BattleLab | any trigger |
| Message a session | BattleLab | Write · BattleLab | any trigger |
| Review loop | Forge | Write · BattleLab | a webhook only ([Review loop with Forgejo](/guide/review-loop)) |
| Tell HQ | BattleLab | Notify | any trigger |
| Send a message | Messaging | Notify | any trigger |

What each one asks for, and how its approval works, is in [Automations](/guide/automations), step 3 (*What*).

## Send a message

**What it is for.** A short note to you or your team when something happens: a backup finished, a
pull request was opened, a nightly job has a result. It goes through a messenger you already set up
in **Settings → AI → HQ → HQ connectors**: Telegram, Matrix or mail.

**What you pick.**

- **Send with:** the messenger. Only one that can send now can be picked: tested, with sending
  switched on and at least one recipient you allowed. One that can't is listed but greyed out.
- **To:** one of the recipients you allowed for that messenger. You never type an address here, and
  a webhook delivery can never choose one.
- **Message:** plain text, or a template. On a webhook, a template's own fields can each take a
  fixed value or a typed value from the delivery, as for any automation. For mail, the first line is
  the subject.

**What keeps it safe.**

- The approval names the messenger, the recipient and, for mail, the From address. A different
  recipient or messenger asks for your approval again. A changed From address pauses the automation
  until you approve it again.
- A template with a secret is refused: a message leaves BattleLab in clear text.
- Right before each send, BattleLab checks again that the automation is still on, that sending is
  still on for that messenger, that the recipient is still allowed and that the text holds no
  secret. If any of these fails, nothing is sent and the run says why. Turning the automation off
  while a message is already on its way can't recall it; the change you made then says *a run in
  progress may still complete*.
- Sending shares the messenger's limit with HQ: at most 20 messages an hour through one messenger.
- A message whose answer never came back is recorded as *may have been sent* and is never sent
  again.

## More actions later

More actions come in later releases: running a command or script you saved, and reading from your
forge or files. Each will have the same card here, and each will ask for your approval before an
automation can use it. Categories without an action yet don't have a chip until their first action
arrives.
