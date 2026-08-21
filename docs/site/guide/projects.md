# Projects

Sessions accumulate. A month in, the sidebar is a hundred rows across a dozen unrelated codebases,
and "which of these was the API refactor" stops being answerable. Projects are the fix.

## Projects are entities, not folder names

A project is a real record — `{id, name, color, folders, archived, session_count}` — managed
through `GET/POST/PATCH/DELETE /api/projects`. It owns a *set* of folders, so one project can span
several checkouts, which is what actually happens with worktrees and per-issue clones.

Assignment is a sidecar write: `PATCH /api/sessions/{sid}/metadata` with `{project_id}`, or `null`
to clear it. As always, nothing is written to the engine's own store.

Each project carries a colour, and sessions can override it individually, so a glance at the
sidebar tells you which world a row belongs to.

## AI auto-sort

Assigning every session by hand is exactly the chore that stops people from using a system like
this. Auto-sort does it: a background pass reads unassigned sessions and proposes a project for
each, using the same OpenAI-compatible endpoint configuration as [AI review](/guide/ai-review).

It is deliberately conservative:

- A **confidence floor** (`confidence_min`) — a proposal below it is discarded rather than guessed.
- A **cap per pass** (`max_per_pass`) so a bad configuration cannot relabel your whole sidebar in
  one sweep.
- **Call spacing** of 1 second between model calls, so a local model is not stampeded.
- Near-misses are retained (up to 8) for inspection rather than silently dropped.

Auto-sort only ever touches sessions with **no** project. It does not second-guess an assignment
you made.

## Visibility: three settings that are easy to confuse

These do genuinely different things, and the difference matters:

| Setting | Effect |
|---|---|
| **`projects_hidden`** | A *launch-location* control. The folder drops out of the new-session picker; its **sessions stay in the sidebar**. |
| **`folder_exclusions`** | Blocks the folder outright — its sessions are not listed, and resume is blocked. |
| **`projects_mode`** | `all` (default) or `included` — the only genuinely default-deny mode: with `included`, only explicitly listed projects appear. |

The first one surprises people, so it is worth restating: hiding a project hides where you *start*
work, not the work itself. If you want a folder gone from the listing entirely, that is
`folder_exclusions`.

## Creating a folder from the app

The new-session picker can create a project folder, but only inside a base directory you have
allowed via `AGENT_SESSIONS_PROJECT_ROOTS` (an `os.pathsep`-separated list). If it is empty or
unset the feature is **off** and the endpoint is disabled entirely — not merely hidden in the UI.
When it is set, the target path is `realpath`-resolved and must land strictly under a listed root,
so neither a symlink nor a `..` can escape it.

::: info Verified against
Commit `218cf3a` — `docs/reference.md § Sessions & projects`; `src/agent_sessions/projects.py`; `src/agent_sessions/autosort.py § NEAR_MISS_CAP, CALL_SPACING_S`; `src/agent_sessions/prefs.py § PROJECT_MODES`.
:::
