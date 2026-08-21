# Files & git

Every session pane carries a file browser and a git view, both rooted at **that session's own
working directory**. They dock beside the terminal on a desktop and open as a full-screen sheet on
a phone.

Both are **read-only**. There is no stage, commit, discard or checkout. That is deliberate: the
agent in the terminal is the thing that changes the repository, and a second mutation path would
race it. These panels answer "what did it just do", not "let me fix it myself".

## FILES

A plain browser over the session's working directory: navigate, open a file, read it.

Its limits exist because a working directory can be a monorepo:

| Bound | Value |
|---|---|
| Entries listed per directory | 2000 |
| Directory scan budget | 1500 ms |
| Maximum file read | 1 MiB |

Scanning is parallel but capped — 8 workers overall, 3 per root — so browsing a large tree cannot
starve the event loop that is also pumping your terminal.

## GIT

The GIT tab shows the branch name, ahead/behind counts, and the working tree's changed paths
grouped into **conflicts**, **staged**, **changes** and **untracked**. Selecting a path shows either
a unified diff with both old and new line-number gutters, or the file's current contents when a
diff does not apply.

The status list is capped at about **5000 entries** and reports itself as truncated beyond that —
it tells you it is incomplete rather than quietly showing a prefix. Every git invocation is bounded
the same way: a 10-second timeout and a 4 MiB stdout budget enforced *while reading*, not after, so
a pathological repository cannot hang or balloon the request.

Diff rendering has its own ceilings — 2 MiB / 50,000 lines of source considered, 20,000 lines or
512 KiB rendered — and a total cell budget beyond which a diff is reported as too large rather than
attempted.

::: tip Why the caps are documented
These are the numbers you hit on a real repository, and a truncated view that does not say it is
truncated is worse than no view. Each of these limits announces itself in the UI.
:::

::: info Verified against
Commit `218cf3a` — `src/agent_sessions/files.py § FILES_MAX_ENTRIES, FILES_SCAN_BUDGET_MS, FILES_MAX_READ`; `src/agent_sessions/gitpanel.py § GIT_MAX_ENTRIES, GIT_TIMEOUT_S, GIT_MAX_STDOUT, DIFF_MAX_*`.
:::
