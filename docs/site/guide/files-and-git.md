# Files & git

Every session pane carries a file browser and a git view, both rooted at **that session's own
working directory**. They dock beside the terminal on a desktop and open as a full-screen sheet on
a phone.

Both panels can change things now — edit a file, stage, commit, push — and both are built around
one fact: **the agent in the terminal writes to the same tree you are looking at.** Every change
you make from a panel is bound to what you were shown, and is refused rather than guessed when that
has moved.

## FILES

A browser over the session's working directory: navigate, open a file, read it, edit it.

Its limits exist because a working directory can be a monorepo:

| Bound | Value |
|---|---|
| Entries listed per directory | 2000 |
| Directory scan budget | 1500 ms |
| Maximum file read | 1 MiB |

Scanning is parallel but capped — 8 workers overall, 3 per root — so browsing a large tree cannot
starve the event loop that is also pumping your terminal.

## Editing a file

Open a text file and press **EDIT**. The viewer is a full code editor themed on BattleLab's own
colours: syntax highlighting, search (Ctrl/Cmd+F), undo, bracket matching. **SAVE** — or
Ctrl/Cmd+S — writes the file.

The viewer is a dialog, so **Tab moves focus** rather than inserting a tab; indent with
Ctrl/Cmd+] and outdent with Ctrl/Cmd+[. Closing the viewer, pressing Esc or navigating away with
unsaved edits asks before discarding them.

### When a file stays read-only

The viewer shows the file and says why it cannot be edited. A file is editable only when every one
of these holds:

| Condition | Why |
|---|---|
| At most 1 MiB | Only the first 1 MiB is loaded; saving that would cut the file short |
| Valid UTF-8 | A byte that is not UTF-8 cannot survive the round trip unchanged |
| One line-ending style | LF or CRLF is kept exactly as it was, which needs one to keep; a BOM is kept too |
| One name | A hard-linked file would split from its other names — including the copy a refused save keeps in the recovery store; delete that copy to edit the file again |
| Owned by you | |
| Not git metadata | `.git/config` and hooks are code the agent's next git command runs, and a `.git` file tells git where a repository is |
| Not a kept previous version | Those open read-only (see below) |
| On a filesystem that supports file leases, the same one as the recovery store | How a save knows nobody else has the file open |

### When a save is refused

Nothing is written, and your edits stay in the editor.

- **Changed on disk** — the file changed after you opened it, usually because the agent wrote it.
  Switch between **MINE** and **ON DISK** to compare, then **RELOAD FROM DISK**, or **OVERWRITE
  WITH MINE** once you have looked at the version on disk.
- **Open in another process** — another program has the file open. BattleLab asks the kernel
  (with a file lease) instead of guessing, so this includes a reader such as `tail -f`. Close it
  and save again.
- **Opened during the save** — another program opened the file while it was being replaced. The
  original is put back before the save reports.
- **Permissions cannot be kept** — a save keeps the file's mode, group and access list (ACL). If
  the group or ACL cannot be carried over, the save is refused before anything changes.

### Previous versions

Each save keeps the version it replaced in a recovery store — `~/.agent-sessions/edit-recovery`,
or wherever `AGENT_SESSIONS_EDIT_RECOVERY` points — and the viewer links to it after the save.

Kept versions are **never removed automatically** — they stay until you delete them. Removing one
safely would need proof that no program is about to write into it, and no check can give that for a
write that starts a moment later. So the recovery store grows with every save, and cleaning it up is
yours. A save that is refused or interrupted puts the original back at its name **and** keeps it in
the store under a second name, so the file opens read-only (two names) until you delete the kept
copy. Removing that name automatically could lose what a program still writing into the file adds.
Uploads cannot write into the recovery store.

::: warning Two limits, stated plainly
- A save survives a crash **or a power loss** between any two of its steps: the original is
  either at its name or in the recovery store, and is put back on the next start. That holds as
  far as the filesystem honours `fsync`.
- A program that looked the file up just before a save but only opened it after the save
  finished writes into the kept previous version, not into the new file. Those bytes are
  recoverable from the recovery store; they are not merged.
:::

## GIT

The GIT tab shows the branch name, ahead/behind counts, and the working tree's changed paths
grouped into **conflicts**, **staged**, **changes** and **untracked**. Selecting a path shows either
a unified diff with both old and new line-number gutters, or the file's current contents when a
diff does not apply.

It can also act: fetch, fast-forward pull, switch, create and delete branches, stage and unstage
whole files, discard a change to a tracked file, commit what is staged, and push. Each refuses
rather than forces — a diverged branch, a dirty tree, a missing upstream or an unresolved conflict
is a stated refusal with the next step, never a flag applied for you.

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
`src/agent_sessions/files.py § FILES_MAX_ENTRIES, FILES_SCAN_BUDGET_MS, FILES_MAX_READ`; `src/agent_sessions/fileedit.py § MAX_EDIT_BYTES, recovery_dir, resolve_pending, read_file`; `src/agent_sessions/gitpanel.py § GIT_MAX_ENTRIES, GIT_TIMEOUT_S, GIT_MAX_STDOUT, DIFF_MAX_*`; `src/agent_sessions/gitwrite.py`.
:::
