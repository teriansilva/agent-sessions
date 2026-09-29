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

## Creating a project

**New project** opens a short wizard at `/projects/new`. It is on the dashboard, under the project
picker in **New session** ("+ New project…"), and in **Settings → Projects**. The session list is
hidden while it is open. It asks, one step at a time:

1. **Name**: any name. If an active project already has it, the wizard warns you but lets you
   keep it.
2. **Folder**: either a **new folder** (a parent, `~` by default, and a folder name, which is
   suggested from the project name) or an **existing folder** you browse to. A new-folder name
   that is already there is labelled *existing folder, reused*: the project uses it as it is. A
   folder that overlaps one another project owns is flagged here, because a folder belongs to one
   project.
3. **Colour**: one of the presets, or none, with a preview of the session-list row and the map
   cluster. The wizard preselects the first preset no other project uses.
4. **Review**: everything on one page, each with an Edit link, plus *Make this my default
   project*.

Nothing is written until you press **Create project**. Then the new folder is made (only if it is
absent, and only under your home directory), the project is created, and the default is set if you
asked for it. If the server refuses (for example, the folder already belongs to another project),
you stay on Review with its reason and a link to the step that fixes it. A folder that was made
before the refusal is left where it is and named. Trying again is safe.

When it is done you can start a session in the new project, plan a mission in it, see it on the
map, or go back to where you started. From New session, you return with the new project and its
folder selected, and your agent and permission choices kept. **Cancel** puts New session back
exactly as you left it. If you have typed anything, leaving asks first.

## The legacy folder-creation endpoint

`POST /api/folders/mkdir` creates a folder only inside a base directory you have allowed via
`AGENT_SESSIONS_PROJECT_ROOTS` (an `os.pathsep`-separated list). If it is empty or unset the
endpoint is **off** entirely, not merely hidden in the UI. When it is set, the target path is
`realpath`-resolved and must land strictly under a listed root, so neither a symlink nor a `..` can
escape it. The app itself no longer calls it: the New project wizard and the folder picker use
`POST /api/fs/mkdir`, which is bounded to your home directory the same way.

::: info Verified against
Commit `218cf3a` — `docs/reference.md § Sessions & projects`; `src/agent_sessions/projects.py`; `src/agent_sessions/fsbrowse.py § make_dir`; `web/src/routes/NewProject.tsx` (#1187); `src/agent_sessions/autosort.py § NEAR_MISS_CAP, CALL_SPACING_S`; `src/agent_sessions/prefs.py § PROJECT_MODES`.
:::
