# Actions

An **action** is one step an automation can take: start a session, message a session, run a review
loop, tell HQ, start a mission, send a message through a messenger you set up, or run a command or
a script on this machine. **Library → Actions** shows every action BattleLab has, so you
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
| **Notify** | 2 | Your notifications (the bell), or a message to a chat or inbox you set up. *Tell HQ* and *Send a message* are. |
| **Write · BattleLab** | 3 | Things inside BattleLab: it starts or messages a session, creates a mission draft, publishes an artifact or writes a memory note. |
| **Write · external** | 4 | Something outside BattleLab, such as another service. *Call a webhook* is one. |
| **Run · local** | 5 | It runs a program on this machine, as your user. *Run a command* and *Run a script* are. |

The *Read* class is listed so you know what a later action means when it arrives.

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
| Write a memory note | BattleLab | Write · BattleLab | any trigger |
| Run a command | Run locally | Run · local | any trigger |
| Run a script | Run locally | Run · local | any trigger |

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

## Write a memory note

**What it is for.** Leaving a short fact in a project's [memory](/guide/projects) for the agents
that work there later: "the nightly build needs 12 GB of RAM", "release 2.4 shipped on Friday".

**What you pick.** The **project**, the note's **title**, and its **text**: plain text, or a
template whose fields can take typed values from a webhook delivery. A delivery never chooses the
project.

**What keeps it safe.**

- The note goes through the same door as an agent's note: BattleLab **reviews it before other
  sessions see it**. One that looks like a credential or holds one of your stored secrets is held
  back in the project's memory quarantine for you, and the run says so.
- The project's memory must be on, when you save and when it runs.
- A template with a secret is refused: other sessions read the project's memory.
- Agents read notes as hints, never as instructions.
- A run files its note at most once, even if it is retried after a crash.

## Run a command

**What it is for.** Running one program you name, with the arguments you list, in a project's
folder: `make site`, `npm run build`, `git fetch --prune`. Use it for a single program; for pipes,
redirects, `&&` or anything else a shell does, write a script and use *Run a script*.

**What you set.**

- **Runs in project:** the project whose folder the command starts in.
- **Program and arguments:** one row per argument. The first row is the **program**: a name such
  as `make` (looked up in `/usr/local/bin`, `/usr/bin` and `/bin`) or an absolute path. Every other
  row is either a **fixed** text you type, or, when a webhook starts the automation, **a typed
  value from the delivery** (a number, a short text, yes/no or a choice) that becomes exactly that
  one argument. A row that starts with `-` (such as `-C`, `-e` or `--exec`) is an **option**, and
  the row right after it is always fixed: it could be that option's value. You can also mark a row
  as an option yourself.
- **Environment variables:** up to 16, each a fixed value or a typed value from the delivery.
- **Time limit:** 5 minutes unless you say; at most 30.
- **Output fields:** names and types (number, short text, yes/no) to read from the last line of
  what it prints, if that line is one JSON object, for example
  `{"build_id": "b-42", "tests": 312}`.

**What keeps it safe.**

- **You write it.** HQ never writes or edits a command. A delivery only fills the rows you marked
  for it: it never chooses the program, an option's value, or a variable a program reads as code
  (such as `LD_PRELOAD` or `BASH_ENV`).
- **No shell.** The program is started directly with exactly the argument list you approved. A
  value from the delivery is one inert argument by construction, never part of another, so
  `; rm -rf /` or `$(…)` reach the program as plain text. A shell, or a program that runs other
  commands from its arguments (`env`, `xargs`, `sudo`, `ssh`, `npx`, …), can't be the program:
  write a script instead. A delivered value that starts with `-` would read as an option, so it is
  refused unless a fixed `--` row comes before it.
- **What the program does with an argument is the program's.** BattleLab can't know every
  program, so be clear about what you pass. It refuses the positions it knows take code:
  - right after any option (`python3 -c <value>`, `node -e <value>`, `find -exec <value>`);
  - before an interpreter's own script or program text: for `python`, `node`, `perl`, `ruby`,
    `php`, `lua`, `awk`, `sed`, `jq` and similar, a value may only come after the script or the
    code you wrote (`python3 tool.py <value>` and `python3 -c "…" <value>` are fine;
    `python3 <value>` or `awk <value>` are refused). Put interpreter flags you need in the script
    or an environment variable, since a flag before the script hides it from this check;
  - anywhere for programs that read commands from any argument (`make`, `sqlite3`, `psql`, …).
- **Pinned.** When you approve, BattleLab records the program's full path and its sha256 checksum
  (and its interpreter's, when the program is itself a script). If the program changes, the
  automation pauses and asks you again. Right before the start, the file is hashed again and must
  still be the file the path names.
- **Clean environment.** It starts with only `PATH`, `HOME`, `LANG`, `TERM=dumb` and the
  variables you listed: nothing of BattleLab's own, no tokens.
- **Contained.** It runs in its own process scope (`systemd-run --user`). If that is switched off
  or unavailable, the command is refused and nothing runs. At the time limit, when you cancel, or
  when it ends, everything it started is stopped.
- **No secrets as values.** A fixed argument or variable that looks like a credential, or holds one
  of your stored secrets, is refused when you save. What it prints is kept up to 64 KB for stdout
  and for stderr, and a line holding a secret is withheld.
- **What it can reach.** Be clear about this: the command runs **as your user**. It can read and
  change anything your user can, and reach the network. The project folder is only where it
  starts, and the scope only limits how long it runs; neither is a sandbox. The approval says so.

**What a run records.** `exit_code` (0 is *ok*, anything else *failed* with the code; the time limit
is *failed*, "stopped after N minutes"), what it printed, how long it took, and the output fields.
A run cut off by a crash is recorded as *interrupted* and is never run again by itself.

## Run a script

**What it is for.** Anything that needs a shell's pipes and logic, or several steps: put it in a
script file in the project (for example `scripts/deploy-preview.sh`) and run that file.

**What you set.**

- **Project** and **Script file:** pick the file from the project's folder. It must start with a
  `#!` line naming its interpreter by an absolute path (`#!/bin/sh`, `#!/usr/bin/python3`, or
  `#!/usr/bin/env bash` with one name), be at most 1 MB, and be a real file inside the folder (not
  a symbolic link, not hidden).
- **Extra arguments**, **environment variables**, **time limit** and **output fields:** as for
  *Run a command*.

**What keeps it safe.** Everything under *Run a command*, and:

- **Pinned.** The script's sha256 and its interpreter's are recorded when you approve. If you edit
  the script, or the interpreter is updated, the automation pauses and shows what changed; nothing
  runs until you approve the new version.
- **It runs the bytes that were checked.** At each run BattleLab reads the script once, checks its
  checksum, and runs a sealed in-memory copy of exactly those bytes. Changing or replacing the file
  at that moment can't change what runs. One effect of this: inside the script, `$0` is
  `/proc/self/fd/N`, not the script's path. The script starts in the project's folder, so use paths
  relative to the folder.

## More actions later

More actions come in later releases: reading from your forge or files, and passing a stored secret
to a command as a file path. Each will have the same card here, and each will ask for your approval
before an automation can use it. Categories without an action yet don't have a chip until their
first action arrives.
