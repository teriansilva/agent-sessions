# Ask

**Ask** is the home for the sessions that are not in a mission. It starts on the
[Dashboard](/guide/dashboard) (`/dashboard`), which shows two things beside its tiles: **Recent
work**, a short chronological account of what you did, and **Needs you**, only the sessions that are
waiting on you. Docked at the bottom is the field itself, which answers questions about your work
from the transcripts and missions this install can already see. **Enter** sends; **Shift+Enter**
starts a new line.

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

## Once you've asked

Asking opens the conversation on its own page, **Ask** (`/ask`, also in the Dashboard's chevron
menu), with your question already running. Its only controls are the **back arrow** (to the
Dashboard) and **New conversation**.

- While it works, the answer shows what it is doing: *Searching 140 sessions…*, then *Checking
  against 3 transcripts…*, with the seconds so far.
- The first answer appears as soon as the session catalog has been searched. It is then checked
  against the transcripts of the sessions it names, and replaced by the confirmed answer.
- An answer that names a session needing you carries the same *Needs you* marker and ⓘ as the
  list, whatever the list's filter.

Answers are **transient**: leaving the page, or New conversation, discards the conversation and
stops a question that is still running.

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
`src/agent_sessions/needs_you.py § ROWS_MAX, KINDS, build`; `src/agent_sessions/work_recap.py § SESSIONS_MAX, INPUT_MAX, ENTRY_TEXT_MAX, ENTRIES_MAX`; `src/agent_sessions/routes/pulse.py § EDIT_TEXT_MAX, build_needs_you`; `src/agent_sessions/needs_you_dismissals.py § KEEP_S`; `src/agent_sessions/prefs.py § PULSE_WINDOW_*, _SESSION_REVIEW_DEFAULTS`; `src/agent_sessions/needs_you_notify.py`; `web/src/components/ask/`, `web/src/routes/Ask.tsx`, `web/src/routes/Dashboard.tsx`; `src/agent_sessions/pulse_chat.py § ask_events`.
:::
