# Dashboard

The **Dashboard** (`/dashboard`, first in the top bar's section nav) is BattleLab's home and its
start page: `/` opens it, and so does the installed app. It shows what your agents are doing right
now, what is running and what needs you, on one page. Its heading reads **Welcome, Commander.**
Beside it are **New**, the page's primary action (a menu with **New session**, **New mission** and
**New project**), and your [primary HQ contact](#your-hq-contact).

Apart from those two, it adds no new actions. Every number is a count of something you can open, and every tile reads on
its own schedule, so one slow or failing read never blanks the rest.

## The strip across the top

Four numbers, each a link to the tile or section it counts:

| Number | Counts |
|---|---|
| **Active agents** | Sessions with a live agent, and how many of them are working, broken down per agent. |
| **Missions** | Missions that are active or waiting for your review. |
| **Needs you** | Sessions no mission holds that are waiting on you, the same list as [Needs you](#needs-you-and-recent-work) below. |
| **Plan quota — lowest left** | The agent plan with the least quota left, and when it resets. Only agents whose plan quota the tile lists count here; see *All agents* in [Settings](/guide/settings). |

A number that could not be read shows **—** and *couldn't read*, never a guess. A count that might be
wrong is worse than no count.

## Tiles

- **Running sessions**: every session with a live agent, working ones marked. The list is loaded on
  demand; the count is always shown.
- **Missions**: active and in-review missions, each linking to its console. With none running, it
  offers to start one.
- **Quota left**: per agent, the plan window nearest its limit and when it resets. Agents that
  report only token counts show *used of limit*. Under each, a **forecast** from the agent's recent
  pace (the readings of the last day): *runs out in ~9h … before the reset* when that pace outruns
  the quota, otherwise where it lands at the reset. It needs a few readings (about half an hour)
  before it says anything. Only quotas that were actually read are listed: an agent with no quota
  and no limit set, one whose quota could not be read for over an hour, and one whose vendor refuses
  the account are left off. They all still appear under *All agents* in
  [Settings](/guide/settings), which is where you set a limit.

The overview cards and the Running sessions, Missions and Quota panels stay visible. Session
history has one **All sessions** link below Needs you, rather than another list of the same sessions.

Sessions refresh every 30 seconds, missions every minute and quota every five minutes. A refresh that
fails keeps the last figures and says so, with a **Retry**.

## Needs you and recent work

The **Needs you** list with its actions (approve, reply, dismiss) is always visible. **Recent work**
is collapsed beneath it: open it to read the recap and choose its time window. The recap is loaded
only when opened. Closing it retires its in-flight read; opening it again shows its last successful
snapshot immediately and refreshes it. A failed read says so; close and reopen to try again.
How the list and recap behave is on the [HQ](/guide/hq) page.

## Your HQ contact

The card beside New shows your primary [HQ contact](/guide/hq#hq-contacts): the looping portrait,
the contact's name and *Primary HQ contact*, with **Open HQ →**. Clicking it opens the
[HQ](/guide/hq) sidebar, the same one the speech-bubble icon beside the notification bell opens
from any page. With **Random**, it is today's contact. On a desktop the card is exactly as wide as
the Plan quota number below it.

With the contact **Off**, the same place holds a placeholder: a silhouette, *No HQ contact* and
*Choose who briefs you*, with **Open HQ →** and **Select contact**, which opens the gallery in
[Settings](/guide/settings).

## Starting work

**New** opens a menu:

| Item | Opens |
|---|---|
| **New session** | The new-session page (`/new`). |
| **New mission** | The new-mission form in [Missions](/guide/missions) (`/mission`). |
| **New project** | The New project wizard (`/projects/new`), which returns to the Dashboard when it finishes. |

## On a phone

The Dashboard is one column: the heading and New, your HQ contact as a full-width row under
them, then the overview cards, then Needs you, the collapsed Recent work
control and All sessions link, followed by Running sessions, Missions and Quota.

## What it does not do

- It never starts, stops or changes anything by itself. Opening the page only reads.
- Its last successful reads are retained in memory while you navigate within the app. They are
  never saved to browser storage and do not survive a reload or logout.
- It is not a replacement for [Mission control](/guide/missions), which is still where mission work
  is steered.
