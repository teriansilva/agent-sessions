# Dashboard

The **Dashboard** (`/dashboard`, first in the top bar's section nav) is BattleLab's home: what your
agents are doing right now, what is running and what needs you, on one page. Two buttons sit beside
its title: **Ask**, which opens the [Ask](/guide/ask) sidebar, and **New**, a menu with **New
session**, **New mission** and **New project**.

Apart from those two buttons, it adds no new actions. Every number is a count of something you can open, and every tile reads on
its own schedule, so one slow or failing read never blanks the rest.

## The strip across the top

Four numbers, each a link to the tile or section it counts:

| Number | Counts |
|---|---|
| **Active agents** | Sessions with a live agent, and how many of them are working, broken down per agent. |
| **Missions** | Missions that are active or waiting for your review. |
| **Needs you** | Sessions no mission holds that are waiting on you, the same list as [Needs you](#needs-you-and-ask) below. |
| **Plan quota — lowest left** | The agent plan with the least quota left, and when it resets. Only agents whose plan quota the tile lists count here; see *Agents & usage* in [Settings](/guide/settings). |

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
  the account are left off. They all still appear under *Agents & usage* in
  [Settings](/guide/settings), which is where you set a limit.
- **Most recent sessions**: your latest sessions, each tagged *Needs you*, *In flight*, *Recently
  active* or *Idle*, with a link to the full list.

Sessions refresh every 30 seconds, missions every minute and quota every five minutes. A refresh that
fails keeps the last figures and says so, with a **Retry**.

## Needs you and recent work

Your recent work and the **Needs you** list with its actions (approve, reply, dismiss) sit among the
tiles; how each behaves is on the [Ask](/guide/ask) page.

## Asking

Ask is not part of the Dashboard. It is a sidebar you can open from any page: the **Ask** button here
opens it, and so does the speech-bubble icon beside the notification bell. See [Ask](/guide/ask).

## Starting work

**New** opens a menu:

| Item | Opens |
|---|---|
| **New session** | The new-session page (`/`). |
| **New mission** | The new-mission form in [Missions](/guide/missions) (`/mission`). |
| **New project** | The New project wizard (`/projects/new`), which returns to the Dashboard when it finishes. |

## On a phone

The Dashboard is one column: the strip first, then Needs you and the other tiles.

## What it does not do

- It never starts, stops or changes anything by itself. Opening the page only reads.
- It keeps no copy of your data: close it and nothing is retained.
- It is not a replacement for [Mission control](/guide/missions), which is still where mission work
  is steered.
