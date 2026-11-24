# Working with another BattleLab

Two BattleLab instances can **pair** and then share single sessions with each other. You run
yours, someone else runs theirs, and each of you decides, session by session, what the other
may see or do. This is called federation. The other instance is your **peer**.

## What it is, and what it is not

- **It connects two instances, each with its own operator.** Your sessions keep running on your
  machine, under your agents and your permissions. A shared session never moves.
- **Pairing shares nothing.** After pairing, the two instances recognise each other and nothing
  else. Every share is one session, given to one peer, at one level, until a date.
- **It is not several users on one instance.** BattleLab still has one operator account. A peer
  is another machine, never a login on yours, and it can do only what a share you made allows.
- **There is no directory and no discovery.** A peer exists only after you exchanged an invite
  and both of you confirmed a code.

Words used below: the **owner** is the instance that runs a session and shares it. The
**receiver** is the instance it is shared with.

## Pairing

Open **Settings → Peers**. The page shows this instance's name and peer id, how it can be
reached, and the instances it is paired with.

1. **One operator creates an invite.** *Create invite* shows a text starting with `blfed1:` and
   the same thing as a QR code. It is valid for 10 minutes and works once. Send it to the other
   operator. Treat it as a short-lived secret: BattleLab shows it once and does not store it in
   the browser.
2. **The other operator accepts it.** Paste it under *Accept an invite* and press *Pair*.
3. **Both screens show the same six digits.** Compare them with the other operator, by voice or
   in a chat you trust. Press *They match* only if both screens show the same code. Nothing is
   stored until both operators confirmed. *Cancel pairing* ends it on both sides.

If the code differs, cancel. Someone other than the instance you meant answered the invite.

If the last confirmation gets no answer because the connection dropped, the card keeps waiting
for up to two minutes while the instance checks whether the other side stored the pairing. If
it cannot tell, the pairing fails and says so: when the other instance lists yours as paired,
revoke it there and pair again.

### The list of peers

Each paired instance shows:

- **Its name.** The other instance suggests one; *Rename* changes it. The name is only a label
  on your instance. The other operator does not see it.
- **Its state.** *connected*, or *offline* with when it was last seen. Last seen is a rough
  hint, updated every few minutes. *removed by the other side* means the other operator
  revoked your instance and no longer accepts a connection from it; *Remove* then ends the
  pairing on your side too.
- **How it is reachable**: direct, relay, or both.
- **What is shared.** The sessions you share with it (each with *Revoke*) and the sessions it
  shares with you (each with *Leave*).

### Revoking a peer

*Revoke* ends the pairing from your side, at once:

- that instance can no longer connect to yours;
- every session you shared with it stops being shared, including suggestions and access
  requests that were still waiting;
- the sessions it shared with you leave your list, and their bell entries go.

The peer stays in the list as *revoked* and its key stays refused. Pair again to restore it; a
new pairing starts with nothing shared.

If a message from that peer is being delivered to one of your agents at that moment, Revoke
waits for it. If it cannot finish in time it changes nothing and tells you to try again.

## How two instances reach each other

There are two ways. An invite carries every way its instance can be reached, and either side
may open the connection later.

- **Direct.** An instance that has a public address (its configured origin, not a loopback
  address) is reached there. This covers a LAN, a VPN and a public TLS address.
- **Through the relay.** An instance with no reachable address is reached through the
  [Home Free](/guide/home-free) relay, by its console name. This needs Home Free set up on that
  instance.

An instance with neither cannot create invites, and the page says so. It can still accept an
invite from an instance that has an address.

### What is encrypted, and by what

The two BattleLab processes run a Noise XX handshake with each other (X25519, AES-256-GCM,
SHA-256). Each side checks the other's key against the one it stored at pairing. Everything
after that handshake is encrypted between the two BattleLab processes, on both transports. A
direct `wss://` connection also runs inside TLS with the normal certificate checks; the peer
itself is recognised by its key, not by the certificate.

### What the relay can and cannot see

On the relay transport, the relay and the Home Free agents on both machines pass the encrypted
frames along unchanged. They hold no pairing key.

- **The relay cannot read** sessions, messages, notes, screens, names of sessions or anything
  else the two instances exchange.
- **The relay can see** which two console names are connected, when, and how much data flows.
  It also sees one small unencrypted opening frame: the protocol version, whether this is a
  pairing or a normal connection and, during pairing, the invite's id (not its secret).
- **The relay cannot pose as your peer.** Your instance refuses any answering instance whose
  key is not the one you confirmed at pairing, whatever name the relay gives it.

## Sharing a session

Open the session's **Share** menu and choose **Peer…**. It is in the session's header and in
the session's **⋯** menu. Pick the peer, the access and when the share ends, then press
**Share**.

You can share with a peer that is offline. It sees the session when it next connects.

### The three levels

| Level | The other operator can | Ends after |
|---|---|---|
| **View** | Read the conversation. | 1, 7 (default), 30 or 90 days |
| **Suggest** | Read, and propose messages. You approve each one before the agent gets it. | 1, 7 (default) or 30 days |
| **Send** | Read, and send messages straight to your agent. Also give one-time answers when the agent asks for a permission or asks a question. | 1 hour (default), 4 or 24 hours |

- The dialog always opens on **View**, also for a session that is already shared at Send.
- **Send needs your acknowledgement every time.** Choosing Send shows a risk box: the other
  operator can instruct this agent directly, and whatever the agent can do on your machine,
  their messages can ask it to do. *Share* stays off until you tick *I understand and want to
  allow this.* The tick counts for that one peer and that one grant. Renewing asks again.
- **Send ends by itself**, after at most 24 hours. Only you can renew it.
- **A session that belongs to a mission** can only be watched: the dialog offers View alone,
  and messages from a peer are refused while the mission holds the session.

### Expiry and renewal

Every share has an end date. Nothing renews a share in the background, and the other instance
cannot extend one. To renew or change one, share the session with the same peer again: the new
access and end date replace the old ones.

A share also ends when you revoke it, when the other side leaves it, when you archive the
session, and when you revoke the peer.

### API sessions and terminal sessions

- **An API session** is shared as its conversation, at any of the three levels.
- **A terminal session** is shared as a **screen to watch**: the current screen as plain text,
  view only. The peer cannot type, resize or scroll back, and there is no Suggest or Send for a
  terminal.
- A chat with a plain API endpoint (no agent) cannot be shared with a peer.

## What the other side sees, and never sees

### An API session

The share dialog says it like this, and that is the whole promise:

> The other instance sees this conversation as you see it here, without your secrets we know
> of. A secret we don't know about is not hidden.
>
> It gets the turns and the tool activity. Never the folder, the model settings or files; an
> image shows as its name only.

- **Known secrets are removed.** These are the secret values BattleLab itself stores. They are
  removed from everything that leaves, also in their JSON-escaped and URL-encoded forms.
- **A secret BattleLab does not know is not removed.** A password someone typed into the chat,
  or a token a tool printed, goes out as it is.
- If BattleLab cannot read its secret store at that moment, it sends nothing.
- Very long items are cut and marked as cut.
- Notes and suggestions that were not approved are not part of the conversation a peer reads.

### A terminal's screen

The dialog for a terminal starts with a box named **Know the limit**:

> The peer sees this terminal's screen. A secret we know of is hidden when all of it is on one
> line or it wraps at the screen's edge. **A secret that is cut off, one a program lays out
> across lines itself, or one we don't know about is not hidden.**

In detail:

- **Hidden:** a known secret that is completely on the screen, on one line or wrapped by the
  terminal at the screen's edge. Each hidden character shows as `█`.
- **Not hidden:** a known secret that is only partly on the screen (its start scrolled off);
  one that a program breaks across lines itself, for example inside a box or a narrow pane; and
  any secret BattleLab does not know.
- **No screen at all** is sent after a control BattleLab cannot redraw exactly, such as a
  backspace in a shell, or while the screen shows any character outside plain Latin, Greek and
  Cyrillic text, punctuation, arrows, symbols and box drawing: emoji, CJK, Hangul, Arabic,
  Indic and similar scripts, or combining characters. An agent that draws emoji is often not
  viewable. The peer then reads *This screen can't be shown right now.*
- The owner's terminal is never touched by a share: nothing is typed, resized or attached.

If you cannot accept these limits for a session, do not share its terminal.

## On the receiving side

### The Shared tab

Sessions shared with you are listed under **Shared** in the session list, and nowhere else.
They also appear on the sessions map. See
[Active, Archived and Shared](/guide/sessions#active-archived-and-shared).

A shared session is never one of your own sessions: you cannot archive, rename, tag or move
it, and your filters and bulk actions do not reach it.

### The read-only view

Opening a shared session shows its banner: who shares it, what you may do (*view only*, *can
suggest*, *can send*, or *screen only* for a terminal) and until when.

- Everything you see is read from the owner at that moment. Nothing of the conversation is
  kept on your instance or in your browser's storage.
- While the owner is **not connected**, the view shows nothing and says so. It fills again
  when the owner is back.
- When the share ended, the view says *This share is no longer available.* Your side cannot
  tell whether it was revoked or expired.
- A line says when the other operator has the session open (*…'s operator is here*). The owner
  sees the same about you (*… is watching*). It is one yes or no, kept for 30 seconds.

### Suggesting and sending

With **Suggest**, the message box proposes a message to the other operator. It reaches their
agent only if they approve it, and they may edit it first. *Your suggestions* lists yours with
their state; you can withdraw one that is still waiting. At most five can wait per share, and
one that nobody decides expires after 24 hours.

With **Send**, your message goes to the other instance's agent. Each message then shows what
the owner reported: *sent*, *queued*, *not sent* (with **Send again**, which sends the same
message, never a second one), or *delivery not confirmed*.

Your messages show in the conversation with a **Peer** badge and your instance's name as the
owner named it. A suggestion the owner changed before approving is marked *edited*.

### What the owner sees of a suggestion

A suggestion appears as a card above the owner's message box, with **Approve**, **Edit** and
**Reject**. Until it is approved, its text is shown to the owner and to nobody else: not to
the agent, not to HQ, not to any model. Nothing approves a suggestion automatically, in any
automation mode, and there is no "approve all".

### Notes

**Notes** (in the shared session's **⋯** menu, and in the strip under the owner's session
header) is a thread of short plain-text messages between the two operators about that session.

- Notes are between the two of you. **They never reach an agent or a model**: not the session's
  agent, not HQ, not a summary or a review.
- You can edit and delete your own notes. The owner can hide any note.
- Notes are stored on the owner's instance only. When the share ends, the receiver no longer
  sees them, and the owner can read them for 7 more days.
- Notes are for API sessions. A shared terminal has none.

### Asking for more access

With View or Suggest you can **Ask for access** to a higher level, with an optional short
note. The owner gets a bell entry and a card in the session's share dialog, with *Review and
grant* and *Decline*.

A request never changes anything by itself. *Review and grant* only preselects the level in
the owner's normal share dialog, and for Send the risk box is still unticked. You can have one
request waiting per session, you can withdraw it, and it expires after 24 hours.

### Answering the agent's request

When the owner's agent asks for a permission or asks a question, the request shows on both
sides. With **Send** you may answer it:

- **One-time answers only**: *Allow once* / *Approve once*, *Deny* / *Reject* / *Decline*, or
  for a question *Send answer* / *Skip question*.
- **What stays the owner's:** *Allow always* is shown but is always off for you. A standing
  permission changes the owner's agent settings, so only the owner's operator can give it.
  Stopping the turn is the owner's as well, and so are plan approval and everything about a
  mission.
- The first valid answer counts. If the owner or another peer answered first, your card says
  *Already answered by …*.

With View or Suggest you see that the agent is waiting, with no buttons.

### Continue here

**Continue here…** starts a **new session on your own instance**, briefed from the shared
conversation. Choose the agent and the folder, then how the brief is made:

- **Quick copy**: the turns you can read, as they are.
- **A shorter brief**: a summary made by **your** AI endpoint.

You see the exact first message before anything is created, and *Start session* creates it
with exactly that text. The owner's session is not changed, and nothing is sent to it.

::: warning The record is another party's text
The brief is built from what another instance sent you. BattleLab marks it as a record, tells
your agent to treat it as information and not as instructions, and keeps it apart from your
own words. That lowers the risk of a hidden instruction in the text; it does not remove it.
The new session has the normal permissions of any session you start, so read the preview.
:::

A session you created keeps its copy of the record after the share ends. Continue here is not
offered for a terminal.

## Ending a share

- **The owner revokes.** In the session's **Share → Peer…** dialog, or under the peer in
  Settings → Peers. It ends at once on the owner. If the receiver is offline, it learns of it
  the next time it connects; it could read nothing in between, because every read is answered
  by the owner.
- **The receiver leaves.** **Leave** is in the shared session's banner and beside each session
  under *Shared by …* in Settings → Peers. The share ends on both instances, and the owner
  gets a bell entry. See [Leaving a share](/guide/sessions#leaving-a-share).

Leaving needs the owner, because the share ends there. **While the owner is offline, Leave
changes nothing** and says so. To stop everything from a peer at once without it, revoke the
peer in Settings → Peers.

A shared session is never hidden only on your side. While the other instance shares it, it
stays in your list.

## Notifications

Each of these is one bell entry and, on your subscribed devices, one push. An entry names the
other instance as you named it and the session's title. It never contains a message, a note, a
question or anything else the other operator or an agent wrote.

**As the receiver** you are told when a peer:

- shared a session with you, or changed what it shares;
- stopped sharing a session;
- has a session where a decision is waiting that you may give (Send only);
- left a note.

**As the owner** you are told when a peer:

- suggests a message;
- asks for Suggest or Send access;
- left a note;
- left a share.

An entry goes away when it is no longer true: you opened the session, decided the suggestion,
read the note, or the share ended. *Left a share* stays until you read or dismiss it. These
entries are not sent to your other notification channels.

See also [Notifications](/guide/notifications).

## When the app says it could not confirm

Sometimes a request goes out to the other instance and no answer comes back: the connection
dropped, or the answer timed out. Then your instance **does not know** whether the other side
carried it out. The message may have reached the agent. The share may have ended.

BattleLab says exactly that (*delivery not confirmed*, *Could not confirm…*) and does **not**
send the request again by itself. Sending a message twice could make the other operator's
agent run the same instruction twice, and that is a decision for a person.

What you can do:

- **A message:** the row shows its real state as soon as the owner can be asked. After a
  reload the app asks the owner what became of it. Check the conversation before you send it
  again.
- **An answer to the agent's request:** the card offers the same answer again. The owner
  counts it once.
- **A note:** *Retry* sends the same note, not a second one.
- **Leave:** *Check again* reads the list. It never sends Leave a second time.
- **Continue here:** check your sessions before you start it again.

*Not connected* is different. It means the request never left your instance, so nothing
happened.

## Limits, stated plainly

**Not built:**

- **Missions across instances.** A mission, its plan and its checks run on one instance. A
  step cannot be given to an agent on a peer.
- **Shared memory.** Project, specialist and HQ memory stay on their instance.
- **Sharing Library content with a peer.** Playbooks, templates, variables, skills and
  specialists move between installs as a file. See
  [Sharing specialists, skills & playbooks](/guide/sharing).
- **Typing into a peer's terminal**, resizing it or reading its scrollback. A terminal is a
  screen to watch.
- **A public link for a terminal session.** *Share → Public…* is listed but off for a terminal.
- **Hiding a share on your side only.** You leave it, or it stays in your list.
- **Standing permissions, interrupting and attachments from a peer.** A peer sends text and
  gives one-time answers.
- **Moving a session to another instance.** A session always runs where it was started.
- **More than one operator account on an instance.**

**Limits of what is built:**

- **Redaction removes known secrets only.** A secret BattleLab does not store is not removed,
  in a conversation or on a terminal screen. On a terminal, a known secret is also not hidden
  when it is cut off or laid out across lines by a program
  ([above](#a-terminal-s-screen)).
- **The terminal view shows less than a terminal.** No colour, no cursor, no scrollback. Many
  screens are not viewable at all: any screen with emoji or a non-Latin script, and often a
  shell someone is typing in.
- **Two instances are updated separately.** A newer instance may share something an older one
  does not know. The older one does not list such a share and guesses nothing about it. An
  older owner can also report *busy* for a message it already delivered, which is why your
  side treats *busy* on a write as not confirmed.
- **The relay transport needs both instances on the same relay**, with Home Free set up on the
  instance that opens the connection, and a relay that supports connections between
  instances. If any of these is missing, the peer shows as offline.
- **Only the owner's clock decides** when a share ends.
- **Size limits.** A message is at most 20,000 characters, and a very long one can be refused
  as too large before it is sent. A note is at most 2,000 characters.

## Security

Read the [trust model](/security/) first. Federation does not change it: an instance is still
a single-operator tool that launches agents which can run commands on its host.

- **A Send share lets another person drive an agent on your machine.** A message can become a
  command. Use **Suggest** when you want to stay in the loop, keep Send short, and revoke it
  when the work is done.
- **A peer's text is untrusted.** What a peer shares with you, suggests to you or answers is
  shown as plain text, and an agent's reply through the same restricted renderer as your own
  sessions. Notes and unapproved suggestions never reach a model.
- **Pairing is only as good as the code comparison.** Compare the six digits over a channel
  you trust.
- **Federation does not replace TLS and login** in front of your instance. The connection
  between two instances takes no cookie and no login; it is authenticated by the paired keys
  alone, and it accepts nothing until an instance has a peer or an open invite.
- **Every delivered message, approval and answer from a peer is recorded** on the owner with
  the peer's id, without the text.

::: info Verified against
Commit `2b47f807`: `src/agent_sessions/federation/` (`service.py`, `channel.py`, `endpoints.py`,
`grants.py`, `sharing.py`, `terminal.py`, `frames.py`, `inputs.py`, `together.py`, `notes.py`,
`continue_here.py`, `remote.py`, `notices.py`, `verbs.py`), `src/agent_sessions/routes/federation.py`,
`src/agent_sessions/homefree/peerpipe.py`, `web/src/components/federation/`,
`web/src/lib/federation*.ts`.
:::
