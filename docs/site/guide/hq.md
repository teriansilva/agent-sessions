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

## The sidebar

Its header is your contact's looping **portrait**, then **HQ** with the contact's name under it,
and a second row with the **conversation switcher**, **+** (New conversation) and **✕** (close
HQ). With the contact **Off** there is no portrait and the header is one row: HQ, the switcher,
**+** and **✕**. The portrait plays only while the sidebar is open and the tab is visible; with
reduced motion it is a still picture, and so is a loop that fails to load.

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
- When it needs a detail, HQ **asks you back**: one question with a few answers to tap, and
  sometimes a box for your own words. You can also just send a new message instead.
- HQ can **propose a message** to a session no mission holds ("tell the rebase session to run
  the tests"). It shows you the exact text on a card and sends nothing until you tap **Send to …**.
  Just before typing, it checks the session's screen is still the one it saw; if the screen moved
  on, nothing is sent and the card says *Out of date*. *Delivered* means the terminal accepted the
  text, not that the agent acted on it. The same decision also appears in Needs you. Sending
  needs Session assistance on (Suggest or YOLO) in Settings; HQ never sends on its own, whatever
  the autonomy level. A mission's sessions are messaged from that mission's own console.
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
conversation, and opening HQ shows the latest one. **+** (New conversation) starts an empty one;
the switcher opens an earlier one. HQ keeps the last **50** conversations for **30 days**. What it
stores is your questions and its answers — never the screens or transcripts it read to answer —
and any text that looks like a password or a token is stored as *withheld*.

## HQ contacts

Your **HQ contact** is the General who answers in HQ: the portrait in the header and on the
Dashboard, the name on each answer, and the voice it is written in. Pick one in
[Settings](/guide/settings) → Appearance → Interface → **Select your primary HQ contact**, a
gallery of cards: **Random** first, then the thirteen contacts, then **Off**. Arrow keys move
through the gallery and select; it is one Tab stop. The choice is saved on the server, so it
follows you to every device.

| Contact | Region | Voice |
|---|---|---|
| General Hana Seo | Asia | The calm strategist: crisp briefings, a one-line aphorism to close. |
| General Kenji Mori | Asia | The silent tactician: few words, chess metaphors. |
| General Claire Vidal | Europe | The intelligence chief: elegant, dry wit. |
| General Lukas Brandt | Europe | The precision engineer-general: numbers, timetables, "on schedule". |
| General Irina Volkova | Russia | The ice queen: terse orders, perfect composure. |
| General Dmitri Sokolov | Russia | The gruff old bear: booming, deadpan, theatrical. |
| General Jessica Hale | US | The gung-ho briefing officer: "Outstanding, Commander!" |
| General Jack Harlan | US | The war-movie general: swagger, folksy one-liners. |
| General Amara Okafor | Africa | The rallying field commander: warm, firm. |
| General Kwame Mensah | Africa | The veteran: unhurried, wise, a wry remark. |
| General Valentina Cruz | South America | The firebrand: passionate, decisive. |
| General Mateo Ríos | South America | The smooth operator: charming, never in a hurry. |
| General Ileana Lupu | Romania | *The outlaw*: sardonic, teasing, answers to the Commander alone. |

- **Random** (the default) is *a new contact every day*: a fixed 13-day cycle, so every contact
  comes up once in 13 days and never on two days running. It is the same contact all day on every
  device in one timezone, and it changes at local midnight.
- **Off** shows no portrait, and HQ answers as plain **HQ**.
- **The voice is tone only.** Facts, the tools HQ uses, what it proposes and what it reports
  happened are the same whoever answers. Each voice is a prompt you can edit in Settings → AI →
  Prompts, under **HQ contacts**.
- **Each answer is labelled with who gave it**: the contact's name and a small portrait at the head
  of the answer. The speaker is stored with the answer, so earlier answers keep their name after
  you switch. Answers with the contact Off, from a contact that no longer exists, or from before
  HQ read **HQ**. If your AI endpoint refuses tool calls and HQ falls back to a plain search, the
  answer still carries the contact's name but not its voice.
- Only the chosen contact's loop is downloaded, and the installed app doesn't keep the loops for
  offline use.

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

::: info Verified against
`src/agent_sessions/needs_you.py § ROWS_MAX, KINDS, build`; `src/agent_sessions/work_recap.py § SESSIONS_MAX, INPUT_MAX, ENTRY_TEXT_MAX, ENTRIES_MAX`; `src/agent_sessions/routes/pulse.py § EDIT_TEXT_MAX, build_needs_you`; `src/agent_sessions/needs_you_dismissals.py § KEEP_S`; `src/agent_sessions/prefs.py § PULSE_WINDOW_*, _SESSION_REVIEW_DEFAULTS`; `src/agent_sessions/needs_you_notify.py`; `web/src/components/ask/` (`AskSidebar.tsx`), `web/src/routes/Dashboard.tsx`; `src/agent_sessions/hq_contacts.py § CONTACTS`; `web/src/components/hq/` (`contacts.ts`, `useHqContact.ts`, `HqPortrait.tsx`); `src/agent_sessions/pulse_chat.py § ask_events`.
:::
