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
| Call a webhook | Web | Write · external | any trigger |
| Publish an artifact | Files | Write · BattleLab | any trigger |

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

## Call a webhook

**What it is for.** Telling another service that something happened, for example a deploy tool, a
chat bridge of your own, or a status page: one signed `POST` with a few fields.

**What you set.**

- **Address:** an `https://` address. It is saved in the automation and is part of what you
  approve; a webhook delivery can never change it.
- **Allow a private or local network address:** off by default. While it is off, an address that
  is (or whose name resolves to) a private, local or shared network address is refused, at save and
  again before every request. Turn it on only for a service on your own network. Link-local and
  cloud-metadata addresses are always refused.
- **Fields:** the JSON body's fields. Each is a fixed value or, when a webhook starts the
  automation, a typed value from the delivery (a number stays a number, yes/no becomes
  `true`/`false`).

The body is `{"event": "battlelab.automation", "automation": …, "run": …, "sent_at": …,
"fields": {…}}`.

**The signing secret.** When you first save the automation, BattleLab makes a secret for this
address and shows it **once**, with a Copy button. Give it to the receiving service. Each request
carries `X-BattleLab-Signature: sha256=<hex>`, the HMAC-SHA256 of the raw body under that secret,
so the receiver can check that the request came from your BattleLab and was not changed. The
secret is never shown again, never in a run's record and never in a log. Lost it? Press **Make a
new secret** in the action's settings; the old one stops at once. (If a call is being sent at
that very moment, BattleLab asks you to try again in a moment rather than let that call go out
with the old secret.) A new address gets a new secret when you save.

**What keeps it safe.**

- Your approval names the address, whether private addresses are allowed, and every fixed field.
  A different address, or allowing private addresses, asks for your approval again.
- No redirect is followed: an answer that points elsewhere is reported, and nothing is sent there.
- Each request waits at most 15 seconds and uses no proxy from the environment.
- Right before the request leaves, BattleLab checks again that the automation is still on. A fixed
  field that looks like a credential or holds one of your stored secrets is refused when you save
  (the signature already proves who sent the request), and checked again at every run.
- An address that only reaches a private network through an IPv6 translation (NAT64, 6to4) counts
  as the private address it reaches.
- A request whose answer never came back is recorded as *may have been delivered* and is never sent
  again.

## Publish an artifact

**What it is for.** Putting a file or a small site that a project produces (a nightly report, a
build's coverage page) at a stable artifact link (Library → Artifacts) you can open from anywhere.

**What you pick.**

- **Project** and **File or folder:** a path inside the project's folder, for example
  `reports/nightly.md`. It can't be absolute, can't climb out with `..` and can't be hidden. A
  folder is published as a site.
- **Title** and **Why:** fixed, or on a webhook a short text from the delivery. A delivery never
  chooses the project or the path.

**What keeps it safe.**

- It goes through the same checks as an agent's publish: inside your project folders and outside
  your excluded folders, no symbolic link, no hidden or key file, and **a file with a secret in it
  refuses the whole publish**.
- The artifact is **private**: only you can open it until you share it. Each run adds a revision
  behind the same link while it is private. Once you share it publicly, the next run starts a new,
  private artifact, so an automation never puts new content behind a public link.
- The project's folder is pinned when you approve the automation. If the project is moved to
  another folder, the automation pauses until you approve it again. Before every run the path is
  checked again, and artifacts must still be on for the project.
- The run records the artifact's link.

## More actions later

More actions come in later releases: running a command or script you saved, and reading from your
forge or files. Each will have the same card here, and each will ask for your approval before an
automation can use it. Categories without an action yet don't have a chip until their first action
arrives.
