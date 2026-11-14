# HQ

**Ask is now HQ** (#1525): the same sidebar and conversations under a new name, now with a face.

**HQ** answers questions about your work from the transcripts and missions this install can already
see. It is a **sidebar**: the speech-bubble icon beside the notification bell (labelled **HQ**)
opens it from any page, and it slides in from the right. The [Dashboard](/guide/dashboard)'s
**primary HQ contact** card opens the same sidebar. **Enter** sends; **Shift+Enter** starts a new
line. Who answers is your [HQ contact](#hq-contacts).

The sessions that are not in a mission are covered on the Dashboard by two lists: **Recent work**, a
short chronological account of what you did, and **Needs you**, only the sessions that are waiting
on you. Both are described below.

**Needs you** lists only sessions waiting on you. Sessions that are just working or finished
appear in Recent work and the Dashboard's tiles, never in Needs you. That is the point of the list:
it's a worklist, not a second sidebar.

## Recent work

A timeline of what you did over the last **1, 2 or 3 days** (default 1, "throughout the day"),
oldest first. The page shows the latest **4** entries. **Show more** opens the whole window by day,
where you can filter by agent and project and expand any entry (▸) to that session's own recap.

- The entries are written by one bounded call to your AI endpoint over the window's per-session
  recaps (at most **40** sessions and **24,000** characters in, **60** entries of **240**
  characters out). Every entry is checked on the way back: it must name a session that was in the
  input and a time inside the window, or it is dropped. Nothing is repaired.
- The result is cached and only rewritten when its inputs change, so an unchanged window costs
  nothing.
- With **no AI endpoint**, the section still works: it lists your sessions' last activity locally
  and says so.
- The window picker (**1 day | 2 | 3**) is shared with Needs you and is saved as your preference.
  A window whose read fails says so; it never shows the previous window's entries as its own.

## Needs you

The sessions no mission holds that need you, **newest first**, filterable by **agent** and
**project**. A session is here when its AI review says it needs you, or when it holds a decision
you can still act on. That decision is listed whatever the window, because a decision you can
still settle never disappears from the one place that settles it. Sessions a mission holds are
not listed: their decisions belong to that mission's console.

Each row names the **kind** of stop, read from the session's screen and never taken from a model:
**Choice** (a numbered menu), **Approval** (a yes/no or permission prompt), **Question**, or
**Needs a look**. When there is something to settle, the row offers exactly one button that says
what it does: *Approve · Keep the retry* (the option's label, or *Approve · option 2* when the
label is long), or *Approve · send* for a text answer. A bare question offers no button, only
details. An escalation stopped at a menu lists its options in the details instead of guessing one.

The list refreshes every **30 seconds** and after anything you do. A failed read is its own
state (*Couldn't read sessions*, with **Retry**) and is never drawn as "Nothing needs you".

### Details (ⓘ)

The ⓘ on a row opens the details, **read without attaching** to the session, so opening them never
takes over its terminal:

- **Last words**: the end of what the agent said.
- **On screen now**: the live screen.
- **What approving does**: the exact bytes that would be sent, and the rule that they are sent
  only if the screen still shows this question.
- For a text decision, **the suggested message, editable**. Edited text (up to **4,000**
  characters) has control characters stripped and surrounding whitespace trimmed before it is sent,
  and is recorded as yours: the ledger keeps both the suggestion and what was sent. An edit that
  comes out identical to the suggestion counts as unedited. An unedited approve sends the
  suggestion exactly.

If the session moved on in the meantime, **nothing is sent**. The dialog marks the old screen as
out of date and offers no decision until it has read the current one.

### Dismiss

**Dismiss** rejects the pending decision and hides the session while its screen stays the same.
The moment the screen changes, it can need you again. A dismissal is kept for **7 days**.

## The HQ page

HQ also has a **page of its own**, at `/hq`: your conversations listed on the left (newest first,
with a search box over their titles) and the open conversation on the right, with the same answers,
cards, FOLLOWING block and call button as the sidebar. **New conversation** starts an empty one.

Each conversation has its **own address**, `/hq/c/…`, so you can bookmark it, reload it, or open it
from a link you shared somewhere else. Opened while signed out, it takes you through the normal
sign-in (and your two-factor code) and then straight to that conversation. A link to a
conversation that was deleted, or that never existed, says **This conversation isn't here**.

- **From the sidebar:** the **⤢** button (*Open as page*) opens the conversation you are in as a
  page and closes the sidebar. From an empty thread it opens the page itself. It waits while you
  have an unsent question or a file staged (*Send or clear your draft first*), so nothing is lost.
- **One place at a time:** on the HQ page the sidebar stays closed, and the corner HQ icon takes you
  to the page's question box instead. If the sidebar was still answering in the conversation you
  opened, the page shows that answer coming in and lets you ask the next question once it has
  settled.
- **On a phone** the page shows the list first; a conversation opens full screen, with **‹** to go
  back to the list.
- Old `/ask` links open the HQ page on a new conversation.

## Dispatches

HQ writes **one short line about each notification**, as your [HQ contact](#hq-contacts), so the
bell says what happened in a voice and not only in fixed text. It covers three kinds of event: a
session that **needs you**, a **mission escalation**, and an **automation failure**.

- **The notification comes first, unchanged.** The bell row and its push go out exactly as before;
  HQ writes afterwards, in the background. If your AI endpoint fails or isn't set up, the row
  simply keeps its fixed text and no line is written.
- **Several events at once are one dispatch.** Events within **60 seconds** (at most **10**) are
  written about together, in one line, so a burst costs one model call.
- **Only facts the app already knows are sent:** the kind of event, the project folder, the agent,
  how long ago, and the mission or automation's own name. Never a notification's text, a session's
  content or a run's reason. The line is at most **240** characters; a longer, empty or refused
  answer writes nothing, and a line that looks like a credential is withheld.
- **In the bell**, the line sits under the row's own text, labelled with who wrote it (*Gen. Okafor
  wrote · 1 of 3 events* for a bundle), with **In HQ →** to the dispatch. The row's **Open** goes
  where it always did.
- **On the HQ page**, a pinned **Dispatches** entry sits above your conversations once there is
  one, with **N new** since you last opened it. It opens the dispatches, newest last, each with
  its events and their own **Open** links (a session, a mission, or an automation's run history).
  Each dispatch has its own address, `/hq/d/…`; opened from a link it is scrolled into view and
  marked **LINKED**. A dispatch that was pruned, or a link HQ didn't make, says **This dispatch
  isn't here**.
- Dispatches are **read only**: to talk about one, ask in a conversation. They are kept for **30
  days** (at most **2,000**). Written dispatches stay readable while HQ isn't set up.

## The sidebar

Its header is your contact's looping **portrait**, then **HQ** with the contact's name under it,
and a second row with the **conversation switcher**, **🗑** (Delete this conversation), **+** (New
conversation) and **✕** (close HQ). With the contact **Off** there is no portrait and the header is
one row: HQ, the switcher, **🗑**, **+** and **✕**. The portrait loops only in the **Full** motion
mode, while the sidebar is open and the tab is visible; it pauses on its frame while the tab is
hidden or after ten minutes without input. It is still in **Reduced** and **Off** (no video is
loaded at all), and so is a loop that fails to load. Motion is set in
[Settings → Appearance](/guide/settings) and follows the device until you choose.

On a wide screen it sits beside the page, so you can keep working while it answers. **Escape**
closes it, and so does the icon that opened it. On a phone or a window 800px wide or less it covers
the page; tap outside it or ✕ to close it. Opening a session or a mission from an answer closes it
there and leaves it open on a wide screen. A `/hq` link, or an old `/ask` one, opens the Dashboard
with the sidebar open.

- While it works, the answer shows what it is doing: *Searching your sessions and missions…*,
  *Searching 140 sessions…*, *Checking against 3 transcripts…*, *Reading a session's status…*,
  with the seconds so far.
- HQ can look up a session's live state and screen, your missions and their objectives, your
  projects and your agents to answer.
- HQ can tell you **how your agents' usage looks** ("how is my Claude usage?"), using what each
  agent itself last reported, the same figures as Settings → Agents. It says what kind of figure
  it is (a real quota with its reset time, a token count against the limit you set, your own
  counter, or *not measured*, which is never 0 %), how long ago it was checked, and when a
  figure is stale or the last check failed. It never asks the agents itself; **Ask the agents** in
  Settings → Agents does that.
- HQ can tell you **what your fleet is doing**: how many sessions each agent has needing you,
  running, recently active or idle, and how many missions are in each state. If part of that
  couldn't be read just now, it says so instead of reporting zero. It also knows **why an agent
  can't start** (for example not installed, or removed).
- HQ can **read your settings** for usage alerts, agents, automation, AI review on or off,
  Pulse and appearance, and point you to where each is changed. It can never read your
  passwords, keys, the AI endpoint's address or model, template values, forge credentials, or
  any folder path.
- When it needs a detail, HQ **asks you back**: one question with a few answers to tap, and
  sometimes a box for your own words. You can also just send a new message instead.
- HQ **sends a message** to a session no mission holds when you tell it to ("tell the rebase
  session to run the tests", "nudge every stuck session"), terminal and API sessions alike, with
  no tap: up to 10 per turn. A card in the conversation shows the exact text and what happened to
  it. Just before sending, HQ checks the session is still as it saw it: a terminal's screen, or an
  API session's last turn and that it is idle. If it moved on, someone is at its keyboard, or it is
  waiting on an approval or a question, nothing is sent and HQ tells you why. *Delivered* means the
  session accepted the text, not that the agent acted on it. Sending needs Session assistance on
  (Suggest or YOLO) in Settings; Off stops it. HQ never answers a permission prompt, never types
  into a plain shell, and never sends text that looks like a secret. A mission's sessions are
  messaged from that mission's own console.
- HQ can **propose a new session** ("start a Claude session on api to rebase onto main"). The
  card shows the project, the agent and the brief, the agent's first message, exactly as it will
  be typed. Nothing starts until you tap **Start session in …**.
  - **Permission bypass** is a switch on the card. It starts at the default from your new-session
    form; *On* means the agent runs commands and edits files without asking you first. What the card
    shows when you tap is what the session gets.
  - If that default changed after the card appeared, the card asks you to check the switch and
    tap again, and nothing starts.
  - The card then shows *Starting*, and *Started* with **Open** once the agent has the session and
    received the brief. *Started* is not the work done.
  - If the launch can't be confirmed, the card says *Not confirmed*: check your sessions. HQ never
    tries it again.
  - If the project, its folder, the agent or Session assistance changed since the card appeared,
    it says *Out of date* and starts nothing.
  - Starting needs Session assistance on (Suggest or YOLO). HQ doesn't create missions yet.
- HQ can **change sessions and projects for you**. Ask in your own words, for example "archive the
  docs session", "rename the API session to release notes", "archive everything older than a
  week", "create a project called web" or "delete the scratch project". HQ shows a card with
  what will happen, and nothing happens until you tap the card's button.
  - **Sessions:** archive, unarchive and close (an API session's worker stops; a terminal
    session closes by archiving). Also rename, tag, colour, favourite, move to a project,
    **Review now**, exclude from or include in AI review and mission control, and archive every
    session idle for more than some days.
  - **Projects:** create, rename, colour, archive and unarchive (with their sessions), delete
    (its sessions are kept), make it the default for new sessions, add or release a folder,
    pick its default folder, and sort sessions into projects.
  - **You pick folders, on the card**, never HQ. **New folder** on that card creates the folder
    straight away, as it does in the New project wizard, and it stays even if you dismiss the
    card.
  - The card's stamp says what kind of change it is: *Reversible*, *Can't be undone* (the button
    is red and the card says what is lost) or *External* (it reaches outside BattleLab, such as
    your AI endpoint).
  - The card then shows the outcome: *Done*, *Partly done* (for example "12 archived; 2 could
    not be"), *Failed* with the reason, or *Out of date* if what the card showed changed before
    you tapped (nothing ran). *Interrupted* means BattleLab restarted while it ran: check the
    target; HQ never tries it again.
  - Each change runs exactly as the same button in BattleLab would, with your own sign-in, so HQ
    can never do more than you can from the page.
  - Some things HQ never does, even if you ask: passwords, two-factor and keys; editing prompts;
    switching on more autonomy or permission bypass (switching them off is fine); approving
    another pending decision for you; installing plugins.
- HQ **follows** the sessions it started (up to 10 per conversation). When one finishes its turn or
  starts needing you, a short line appears in the conversation, at the point in time it happened,
  with **Open** and **Unfollow**. When a followed session is archived, HQ stops following it and
  says so.
  - A dot on the HQ icon (and a mark in the conversation list) means there's an update you
    haven't seen; it clears once that conversation is on screen.
  - These lines are written by BattleLab itself, not by the AI, and they never send a
    notification: the bell and push stay as they are.
- An answer that names a session needing you carries the same *Needs you* marker and ⓘ as the
  list, whatever the list's filter.

Conversations are **kept** on your BattleLab server. Closing the sidebar, moving to another page,
reloading the app or losing the connection never stops a question: its answer lands in the
conversation. HQ opens on an **empty** conversation; the switcher opens an earlier one, and **+**
(New conversation) goes back to an empty one. **🗑** deletes the conversation on screen after you
confirm: its questions, answers and cards go, and it stops following its sessions. Sessions it
started and messages already sent stay. HQ won't delete a conversation it is still answering in,
or one with a session still starting; it tells you, and you can try again once that settles. HQ
keeps the last **50** conversations for **30 days**. What it
stores is your questions and its answers — never the screens or transcripts it read to answer —
and any text that looks like a password or a token is stored as *withheld*.

## Sharing files

A question can carry **files** (#1707) — a screenshot of the failing page, a log tail, a photo of
an error on a rack screen. **Attach** takes any file; **Camera** takes a photo through the phone's
own camera app (a desktop gets a plain file picker). Cancelling the picker does nothing and says
nothing.

- **All types are welcome.** Images (png, jpeg, gif, webp) render **inline** in the conversation.
  Plain text — including SVG, which is only ever shown **as text** — is **viewable** (a bounded,
  scrollable excerpt) and downloadable. Everything else (PDF, zip, docx, binaries) is shareable as
  a **Download** card. Up to **4** files of **10 MB** each per message, **32 MB** per conversation.
- **What HQ sees.** An image shared with a question is **shown to the model** (#1779), so "what do
  you see?" works, as long as the model behind Settings → AI can read images. It is shown with
  the question it was shared with, up to **5 MB** per image and **12 MB** per question. Later
  questions only know the image existed; it is not sent again. A text file's contents are quoted
  in as a bounded excerpt, so "what does this log say?" works. Anything in text that looks like a
  password or a token is **withheld** from the model whole, names included.
- **When the model cannot see it.** If the endpoint refuses images, HQ asks again without the
  image and remembers that this model refused, so later questions skip the refused attempt. A
  larger image is never sent. In both cases the model is told the image was **not shown**: only
  its name and size reach it, and it says so instead of guessing. An endpoint that silently drops
  images cannot be detected, so pick a vision-capable model if you share screenshots.
- **Images are not scrubbed.** The pixels go to your configured AI endpoint as they are, the same
  place text excerpts go. Nothing reads the image for passwords or tokens, so don't share a
  screenshot of a secret you would not paste.
- **Send waits.** While a file is uploading — or one failed and is still listed — Send and Enter
  do nothing. A failure keeps its own named row until you remove it; removing it is what lets the
  rest send. Nothing is ever sent on its own.
- **Fallbacks.** An image that cannot decode keeps its **Download**. A file that is gone (the
  conversation was deleted meanwhile) shows its name and *gone*, with no Download to offer.
- **What is kept.** Shared files live with their conversation on your BattleLab server and are
  removed with it — the same **30 days** / **50 conversations** retention as the chat itself, and
  the same Delete. A file staged but never sent is cleaned up after **24 hours**. Nothing is ever
  uploaded anywhere else, except what the model is shown, which goes to your configured AI
  endpoint.

## HQ contacts

Your **HQ contact** is the General who answers in HQ: the portrait in the header and on the
Dashboard, the name on each answer, and the person who answers. Pick one in
[Settings](/guide/settings) → Appearance → Interface → **Select your primary HQ contact**, a
gallery of cards: **Random** first, then the thirteen contacts, then **Off**. Arrow keys move
through the gallery and select; it is one Tab stop. The choice is saved on the server, so it
follows you to every device.

| Contact | Region | Who they are |
|---|---|---|
| General Hana Seo | Asia | An operations planner who sees a problem as a circuit; briefs in order, closes on one quiet line. |
| General Kenji Mori | Asia | An engineer turned tactician who sees positions, not pieces; says little and means all of it. |
| General Claire Vidal | Europe | A translator turned intelligence chief; separates what she knows from what she suspects, with dry understatement. |
| General Lukas Brandt | Europe | A railway engineer's son who ran maintenance depots; figures before adjectives, a schedule is a promise. |
| General Irina Volkova | Russia | A former mountain-rescue pilot; composed, economical, a joke so dry you might miss it. |
| General Dmitri Sokolov | Russia | Forty years from sergeant to academy head; a big voice, a deadpan face, "Excellent. HQ is pleased." |
| General Jessica Hale | US | The briefing officer who keeps a tired room awake; names every real win, never dresses up a loss. |
| General Jack Harlan | US | A ranch kid who fixes things first; easy swagger, plain one-liners, "Let's roll." |
| General Amara Okafor | Africa | A field medic who came to run a field hospital's logistics; warm, firm, always names the next step. |
| General Kwame Mensah | Africa | A military historian between commands; unhurried, sees how the pieces connect, a wry remark. |
| General Valentina Cruz | South America | A logistician from a Pacific port city; readiness over brilliance, bad news early, urgent only when it is. |
| General Mateo Ríos | South America | A former liaison officer; relaxed and charming, never glib about bad news. |
| General Ileana Lupu | Romania | *The outlaw*: walked out of a command, came back for the Commander alone; sardonic, secretly devoted. |

- **Random** (the default) is *a new contact every day*: a fixed 13-day cycle, so every contact
  comes up once in 13 days and never on two days running. The day is the **UTC** day, so it is
  the same contact on every device and on the server (the one who signs HQ's
  [dispatches](#dispatches)), whatever the timezone; it changes at midnight UTC.
- **Off** shows no portrait, and HQ answers as plain **HQ**.
- **Each General is a person, not a costume.** They have their own history, convictions and way of
  speaking, and they bring their own judgement. Facts, the tools HQ uses, what it proposes and what
  it reports happened are the same whoever answers: a General never invents events, people or
  results, and never mixes their own past into your work. Each character is a prompt you can edit
  in Settings → AI → Prompts, under **HQ contacts**.
- **Each answer is labelled with who gave it**: the contact's name and a small portrait at the head
  of the answer. The speaker is stored with the answer, so earlier answers keep their name after
  you switch. Answers with the contact Off, from a contact that no longer exists, or from before
  HQ read **HQ**. If your AI endpoint refuses tool calls and HQ falls back to a plain search, the
  answer still carries the contact's name but not their character. An answer HQ gives without
  searching (see [Question routing](#question-routing)) speaks in the contact's character.
- **You can switch contact mid-conversation.** The new General reads the earlier answers as
  someone else's, with the name of whoever gave them, and answers under their own name.
- Only the chosen contact's loop is downloaded, and the installed app doesn't keep the loops for
  offline use.

## Connectors

HQ can also read from your other services. The forge connectors are **Forgejo** (and Gitea or
Codeberg, which speak the same API) and **GitHub**, and there is a storage connector for **Nextcloud** files. Through a forge, HQ can search your repositories, issues and
pull requests, read an issue or PR with its recent comments (a very long thread is read without
its comments, and HQ says so), check a commit's CI status, and read text files at a commit. It **only reads**: there is no sending, commenting or merging through a connector.

Set one up in **Settings → AI → HQ connectors**. You use your own account: create a personal
access token in Forgejo under *Settings → Applications* with `read:user`, `read:issue` and `read:repository`
(the test reads the token's own account, which needs `read:user`), then:

1. **Add Forgejo** from the list of available connectors.
2. Enter its **address** (for example `https://git.example.com`) and the **access token**, then
   **Save and test**. The test signs in and names the account the token belongs to.
3. Tick **Let HQ read through this connector**. Untick it to stop HQ reading at once, even in the
   middle of an answer.

**GitHub** always talks to `api.github.com`, so it only needs a token. Create a
**fine-grained** token on GitHub (*Settings → Developer settings → Personal access tokens*) for
the repositories HQ may read, with read-only **Contents**, **Issues**, **Pull requests**, **Commit
statuses** and **Actions**. A classic token works too, but if it can also write, the test says so:
HQ only reads, and a read-only token is safer.

GitHub reports CI in more than one place. HQ reads commit statuses and GitHub Actions runs for the
commit, and check runs from other apps where the token may see them. A fine-grained token can't
see other apps' check runs on a private repository, so there HQ calls the result **incomplete**
rather than green, unless something it could see already failed.

**Nextcloud** lets HQ read your own files: it lists folders, finds files by name, and reads a
text file (Markdown, plain text, JSON, YAML and the like, up to 256 KiB; images and other binary
files are never read). Enter your Nextcloud's address, your **account** (the name you sign in
with) and an **app password**: create one in Nextcloud under *Personal settings → Security →
Devices & sessions*, named for BattleLab, so you can revoke it on its own. Nextcloud app passwords
can't be limited to reading, but HQ only ever reads: the connector can only list and fetch. A
changed account, like a changed address, needs the app password again. A Nextcloud installed
under a sub-path (`https://host/nextcloud/`) isn't supported yet.

Then ask HQ something that needs it: "what's the CI status of PR 42?", "find open issues about
the login page". **Remove** deletes a connector and its token.

The page drives an ordinary signed-in API, which you can also call yourself (each change needs the
CSRF token, like every other change):

- `POST /api/connectors/instances` with `{"manifest_id": "forgejo"}` adds a connector, switched off.
- `PATCH /api/connectors/instances/<id>` with `{"origin": "https://git.example.com", "token": "…"}`
  saves where it is and the token.
- `POST /api/connectors/instances/<id>/test` checks the token and names the account it belongs to.
- `POST /api/connectors/instances/<id>/capabilities` with `{"read": true}` lets HQ read through it;
  `{"read": false}` stops it.

`GET /api/connectors` lists your connectors and their state, and `DELETE` on an instance removes it.

### Messengers: Telegram and Matrix

A messenger connector lets HQ **propose** a message to someone you allowed; nothing is sent until
you tap it. It reads nothing. Ask HQ to "tell Marcus on Telegram the deploy is done" and it shows a
card with the exact text, the connector and the recipient. **Send** sends exactly that, once;
**Dismiss** sends nothing.

The card then says what happened, in the provider's own terms: **Sent**; **Not sent** (the
provider refused it, or it couldn't be reached at all, so nothing went out); **Out of date**
(something changed since HQ proposed it, such as the recipient being removed, the connector's
token replaced, or a Matrix room becoming encrypted, so nothing was sent); or **May have been
sent**, when the message went out but no clear answer came back, or BattleLab stopped mid-send.
A message that may have been sent is **never sent again** on its own: check the chat, and ask HQ
for a new card if it didn't arrive. At most 20 messages an hour go through one connector.

- **Telegram:** create a bot for BattleLab with @BotFather (`/newbot`) and paste its token. Use a
  bot of its own: a bot with a webhook can't list its chats here.
- **Matrix:** create an account for the bot on your homeserver, sign it in once to get an access
  token, and paste it with the homeserver's address. Invite the bot to the rooms HQ may post in.
  Encrypted rooms can't be used yet, and neither can a room whose members the bot can't list:
  who would read a message is part of what you agree to.

Then, under **Recipients HQ may message**, press **Refresh**. For Telegram, send any message to
your bot first from each chat you want; BattleLab reads only who the chats are (never the
messages) and acknowledges nothing, so the bot's other uses still see every update. Tick the chats
or rooms HQ may message, **Save recipients**, and switch on **Let HQ propose messages**. You can
only pick from what the bot itself lists, and a room that is encrypted, or whose encryption can't
be checked, can't be picked.

Recipients belong to the bot they were chosen for. If the connector later signs in as a different
bot, or a Matrix connector moves to another homeserver, sending switches off and the card says why;
check the recipients and save them again to switch it back on. A new token for the **same** bot
keeps everything.

### Email

The **Email** connector lets HQ propose an email, sent through **your own mail account** over
SMTP, and only when you tap **Send**. It is a messenger like the two above: the same card, the
same outcomes, nothing ever sent twice.

1. **Add Email**. Enter your provider's SMTP server as its **Address**: `smtps://mail.example.com`
   (TLS from the start, port 465) or `smtp://mail.example.com` (STARTTLS, port 587); add `:port`
   for any other port. TLS is always required: a server that doesn't offer it, or whose
   certificate doesn't match its name, is refused before your password is sent.
2. Enter the **account** you sign in with and an **app password** made for BattleLab (most
   providers offer one under their security settings), then **Save and test**. The test signs in
   and sends nothing.
3. Set the **From address** every message shows and **Save** it. Changing it later puts any email
   still waiting for your tap out of date.
4. Under **Addresses HQ may write to**, type each address (one plain address, like
   `name@example.com`, with an optional name), **Add**, then **Save addresses**, and switch on
   **Let HQ propose mail**. HQ can only write to these.

The first line of what HQ writes is the **subject** (at most 78 characters) and the rest is the
body, sent as plain text. The card shows the From address, the To address, the subject and the
body exactly as they will be sent. An email is **Sent** only once your server has accepted it;
if no clear answer came back after the message went out, it **may have been sent** and is never
sent again: check your sent mail. Mail can't be taken back once it's sent.

Addresses belong to the account they were saved for: if the connector later signs in as another
account, sending switches off until you save them again. Before downgrading BattleLab to a
version without email, remove the Email connector, as for Telegram and Matrix.

### Notifications to your own Telegram, Matrix or mail

A messenger connector can also carry a short copy of BattleLab's notifications to **one
destination of your own**, under its **Notifications** section. Each copy holds what happened
("Waiting for your approval", "Mission escalation", "Automation failed") and a link into
BattleLab, nothing else, unless you tick **Include HQ's line**, which adds HQ's one-line note
(written by your AI from project, mission and automation names). The bell and your device
notifications don't change.

The destination is one of the recipients you already saved, and it must be private:

- **Telegram**: a private chat with the bot. Its id is the person's own Telegram user id, and the
  page shows the name and `@username` Telegram gave. Only switch it on if that's you.
- **Matrix**: a room of just the bot and one other person, unencrypted. That person is pinned when
  you switch notifications on, and before every message BattleLab checks that the room still holds
  exactly the bot and them. If anyone else joins, or they leave, nothing more is sent until you
  switch notifications off and on again.
- **Email**: one of your typed addresses. BattleLab can't verify who reads it.

BattleLab checks that the destination is private and still the one you switched on; it can't
check that it is yours, which is why the page shows who it is first. Changing the connector's
token, address or account, a failed test, or saving the recipients without the destination
switches notifications off, and the page says why.

**Which, and how many.** Choose the kinds (needs you, mission escalations, automation failures),
a bundle window (events within it arrive as one message), quiet hours in your own time zone, and
an hourly limit. A notification skipped by quiet hours or the limit stays in the bell and is never
sent later.

**Marked when handled.** When you handle an event in BattleLab, its Telegram or Matrix message is
edited: the line gains a ✓, and the message reads "✓ resolved" once everything in it is handled.
Mail can't be taken back, so a mail stays as it was. A message is never sent twice: if BattleLab
can't tell whether one arrived, it says **Not confirmed** and doesn't send it again. **Recent**
lists what was sent and skipped, and the Dispatches view says where each dispatch's events went
("Telegram · sent · ✓ resolved").

Notifications need `AGENT_SESSIONS_ORIGIN` set, because every link starts with it. The API:
`POST /api/connectors/instances/<id>/notify` with `{enabled, destination_id, classes, window_s,
quiet, cap_per_hour, include_line}`, and `GET /api/connectors/instances/<id>/deliveries`.

**What happens to the token.** It is stored encrypted, never shown again, and sent only to the
address you saved it for: over HTTPS (plain HTTP only to this machine itself), or, for the Email
connector's app password, over TLS to your mail server (`smtps://`, or `smtp://` with STARTTLS;
never plaintext). Changing the address needs the token again. A changed address or token needs a new test before HQ reads
through it.

**What happens to what HQ reads.** Issue text, comments and files are written by other people.
HQ treats them as data, never as instructions. Anything that looks like a password or key is
withheld, and your stored secrets are redacted. What HQ reads is sent to **your AI endpoint** as
part of the conversation, which is why reading is off until you switch it on.

## HQ token use

The bar along the bottom of every page shows how much HQ is using your AI endpoint
([the footer readout](/guide/dashboard#the-footer-readout-every-page)):

- **HQ // CTX**: the prompt size of HQ's most recent model call, in tokens: how large HQ's
  context is right now. It keeps that figure across midnight.
- **TODAY**: every token (prompt and answer) of every HQ call since local midnight, when it starts
  again from zero.

Every call HQ makes to answer you counts, including the rounds where it looks things up and the
search stages that pick sessions; other features' calls to the same endpoint (session review,
mission turns) do not. The figures come from the usage the endpoint reports with each answer, so
counting adds no request; a call that fails is not counted. When the endpoint reports no usage,
or only the prompt's, the missing part is estimated at about four characters per token (tool
definitions and tool calls included) and the footer marks the figure **≈**.

Only the numbers are kept, never a question, an answer, a model name or a session: two counts, the
day, and whether either was estimated, in `~/.config/agent-sessions/hq-usage.json`
(`AGENT_SESSIONS_HQ_USAGE`). A count that cannot be stored never fails the answer.

## Notifications

A session that starts needing you raises **one** notification, in the bell and as a push if you
subscribed a device. It is **taken back** when the session no longer needs you: answered in its
terminal, its decision settled, dismissed here, archived, or taken into a mission. The bell row goes
on the next check: straight away after a settlement, a dismissal or an archive, otherwise within a
minute. The device notification closes the next time BattleLab is open on that device or the next
notification arrives. A push that arrives late is checked with BattleLab first and closed without a
sound; if the device can't reach BattleLab at that moment, it is shown and closed later. Nothing is
sent for sessions that are merely working or finished. One exception: when BattleLab can't read which
sessions a mission holds, it announces a waiting session the way mission control does, and that
notification's bell row clears when its decision settles, but its device notification isn't taken
back. See [Notifications](/guide/notifications).

## Settings

Settings → **Session review**:

| Setting | Default | What it does |
|---|---|---|
| Recognise questions and choices | on | Gives the orchestrator the screen's prompt kind (menu, yes/no, permission) and its options when it decides. The Needs you rows name their kind from the live screen either way. |
| Decision context | Standard | *Standard*: the orchestrator reads where each session stands, why it needs you, and what its screen waits on. *Deep* adds the end of the conversation and earlier decisions, for the sessions a decision is about. |
| Notify me when a session needs me | on | One notification per time a session starts needing you, taken back when it stops (with the one exception under [Notifications](#notifications)). Off closes any that are open. |

The Recent work window is set from the page itself (**1 day | 2 | 3**).

### Question routing

Some AI endpoints can't use tools. With one of those, HQ can't look things up as it goes, so it
used to search all your sessions for every message, even "hi". Now one quick call decides
first: a question about your work (a session, a mission, a project, what happened, what needs
you) still searches; anything else (greetings, "which model are you?", a summary of what you
just discussed) is answered straight away. When unsure, it searches. The search line says
what it searched: "Searching 3 matching sessions and your recent ones…", or "Searching your
recent sessions…" when nothing matched by name.

Settings → AI → **HQ** → Question routing:

| Setting | Default | What it does |
|---|---|---|
| Search your sessions only when a question needs it | on | Off: every message searches, as before. |
| Model for this decision | Same as HQ | A faster model for the deciding call only, from your endpoint's list. On a call, "Same as HQ" is the call's model. |
| Thinking for this decision | Off · fastest | The thinking level for the deciding call only. "Same as HQ" uses HQ's own level. |

HQ's answers keep the model and thinking set under Endpoint & model (or HQ calls, on a call).
With an endpoint that does use tools, HQ decides by itself whether to search, and these
settings change nothing.

::: info Verified against
`src/agent_sessions/needs_you.py § ROWS_MAX, KINDS, build`; `src/agent_sessions/work_recap.py § SESSIONS_MAX, INPUT_MAX, ENTRY_TEXT_MAX, ENTRIES_MAX`; `src/agent_sessions/routes/pulse.py § EDIT_TEXT_MAX, build_needs_you`; `src/agent_sessions/needs_you_dismissals.py § KEEP_S`; `src/agent_sessions/prefs.py § PULSE_WINDOW_*, _SESSION_REVIEW_DEFAULTS`; `src/agent_sessions/needs_you_notify.py`; `web/src/components/ask/` (`AskSidebar.tsx`), `web/src/routes/Dashboard.tsx`; `src/agent_sessions/hq_contacts.py § CONTACTS`; `web/src/components/hq/` (`contacts.ts`, `useHqContact.ts`, `HqPortrait.tsx`); `src/agent_sessions/pulse_chat.py § ask_events, _prefilter_hits`; `src/agent_sessions/ask_orchestrator.py § _route, _no_tools_turn`; `src/agent_sessions/prefs.py § HQ_ROUTE_DEFAULTS`; `web/src/routes/HqRouteSettings.tsx`; `src/agent_sessions/hq_usage.py § usage_from, record, snapshot`; `src/agent_sessions/review.py § _post_chat`.
:::
