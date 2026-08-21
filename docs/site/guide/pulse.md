# Pulse

Pulse answers one question: **what needs me right now?**

With a dozen sessions across several projects, the sidebar tells you what exists but not what
matters. Pulse ranks recent work by what is waiting on you, what is in flight, and what has gone
idle, and gives you one click back into any of it.

## Depth: the setting that decides what it costs

Pulse has three depths, and they differ in how much model work they do — not in how much they see.

| Depth | Model calls | What you get |
|---|---|---|
| **`fast`** (default) | **none** | Ranking and flags computed purely from the per-session AI-review summaries already on the sidecar. Free. |
| **`medium`** | **one** per scan | Adds the top "state of your work" banner. |
| **`slow`** | one, plus a bounded per-session pass | Adds a one-line "state + next step" for individual sessions. |

`fast` is genuinely free — it does no LLM work at all, it re-uses summaries
[AI review](/guide/ai-review) already produced. If you have not configured an AI endpoint, that is
the depth to stay on, and it is the default for exactly that reason.

At `medium` or `slow`, if the endpoint is unavailable Pulse **degrades to `fast` curation** with no
banner and an explicit `synthesis_skipped` marker, rather than failing or blocking the page.

The `slow` per-session pass is bounded and serialized: at most **12 sessions** per scan, with a
1-second spacing between calls. That cap is a deliberate ceiling on what one Pulse scan can cost
you against a metered endpoint or a slow local model.

## Window and caching

`window_days` controls how far back Pulse looks — default **3**, allowed 1–30. Anything active
within the last 24 hours counts as recent.

Results are cached, so opening Pulse is instant; the scan runs behind that. A background loop can
refresh it on an interval (`auto_enabled`, `interval_minutes`).

## Output limits

The banner is capped at 700 characters and each session line at 160, with a digest of at most 40
entries feeding the banner call. These are why Pulse stays scannable instead of becoming a wall of
model prose.

## Asking Pulse a question

Pulse also backs an ask-style chat over your recent work. The quality of the answer is dominated by
the retrieval step that selects which sessions are in scope — the AI recap of each session is part
of that haystack, which is what lets a question about work you did on Tuesday find the right
session at all.

::: info Verified against
Commit `218cf3a` — `src/agent_sessions/pulse.py § SCAN_DEPTHS, DEFAULT_DEPTH, WINDOW_DAYS_*, SLOW_SESSION_CAP, SYNTH_CALL_SPACING_S, BANNER_MAX, SESSION_LINE_MAX, RECENT_ACTIVE_S`; `src/agent_sessions/pulse_chat.py`.
:::
