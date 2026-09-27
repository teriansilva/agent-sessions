# Dashboard

The **Dashboard** (`/dashboard`, first in the top bar's section nav) is BattleLab's home: what your
agents are doing right now, what is running, what needs you, and the Ask field, on one page. `/ask`
still works and opens the same page.

It adds no new actions. Every number is a count of something you can open, and every tile reads on
its own schedule, so one slow or failing read never blanks the rest.

## The strip across the top

Four numbers, each a link to the tile or section it counts:

| Number | Counts |
|---|---|
| **Active agents** | Sessions with a live agent, and how many of them are working, broken down per agent. |
| **Missions** | Missions that are active or waiting for your review. |
| **Needs you** | Sessions no mission holds that are waiting on you, the same list as [Needs you](#needs-you-and-ask) below. |
| **Plan quota — lowest left** | The agent plan with the least quota left, and when it resets. Only agents that report a real plan quota count here; see *Agents & usage* in [Settings](/guide/settings). |

A number that could not be read shows **—** and *couldn't read*, never a guess. A count that might be
wrong is worse than no count.

## Tiles

- **Running sessions**: every session with a live agent, working ones marked. The list is loaded on
  demand; the count is always shown.
- **Missions**: active and in-review missions, each linking to its console. With none running, it
  offers to start one.
- **Quota left**: per agent, the plan windows it reports and when each resets. Agents that report
  only token counts show *used of limit* (or *no limit set*); an agent nobody measured says *not
  measured*, never 0 %.
- **Most recent sessions**: your latest sessions, each tagged *Needs you*, *In flight*, *Recently
  active* or *Idle*, with a link to the full list.

Sessions refresh every 30 seconds, missions every minute and quota every five minutes. A refresh that
fails keeps the last figures and says so, with a **Retry**.

## Needs you and Ask

The middle of the page is [Ask](/guide/ask): your recent work, the **Needs you** list with its
actions (approve, reply, dismiss), and the Ask field. The Dashboard only arranges them around the
tiles; how each behaves is on the Ask page.

## On a phone

The strip and Needs you stay on screen. Missions, Quota and Recent work sit behind **More ▾**, so the
page is one column you can act from with a thumb.

## What it does not do

- It never starts, stops or changes anything by itself. Opening the page only reads.
- It keeps no copy of your data: close it and nothing is retained.
- It is not a replacement for [Mission control](/guide/missions), which is still where mission work
  is steered.
