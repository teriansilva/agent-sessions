# Files & git

Every session pane carries a file browser and a git view, both rooted at **that session's own
working directory**. They dock beside the terminal on a desktop and open as a full-screen sheet on
a phone.

Both panels can change things now — upload and edit files, stage, commit, push — and both are built
around one fact: **the agent in the terminal writes to the same tree you are looking at.** Every
change you make from a panel is bound to what you were shown, and is refused rather than guessed
when that has moved.

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

## Uploading files

**Upload into this folder**, in the FILES panel's header, offers **Files…** and **Folder…**; a
folder keeps its structure. You can also drop files or a folder straight onto a directory in the
tree — the upload lands in the directory under the cursor, not wherever the panel happens to be. A
browser that cannot pick a folder (iOS Safari has no folder picker) shows **Folder…** disabled with
the reason instead of quietly flattening it.

A drop is checked against the limits below before a byte moves. A file that breaks a per-file rule
gets its own row with the reason — **TOO LARGE**, for example — and the rest still go; a drop over
the file-count or total-size limit is refused as a whole, with the limit named. The upload queue
then shows every file's own state: **QUEUED**, **SENDING**, **DONE**, or the reason it did not land.

| Bound | Value |
|---|---|
| One file | 25 MiB |
| One drop | 500 files, 250 MiB in total |
| Folder depth | 32 levels |
| One name | 255 bytes |
| An idle drop | expires after 15 minutes without activity |
| Drops in flight | 8 per signed-in session, 64 across the server |

Uploaded files are created with mode `0644`, narrowed by the service's umask — never executable,
whatever the file was on your machine. The bytes are counted as they arrive and written straight to
the destination, and a refused or failed upload leaves nothing behind.

### When the name is taken

Nothing is overwritten without asking. The dialog names the file and offers:

- **Skip** — leave the existing file alone. Esc does the same.
- **Keep both** — save the upload under a free name, such as `report (2).pdf`.
- **Replace** — put the upload in place of the existing file.

With more files still to come, **Apply to the remaining files** answers once for all of them.

**Replace** checks twice. The existing file is fingerprinted — its content included — when you
choose, and again just before the new bytes take its name; if the agent changed it in between, it
is **not** replaced. A file over 64 MiB cannot be fingerprinted cheaply enough to be safe, so
replacing one is refused: replace it in the session instead.

### What an upload refuses

| Refused | Why |
|---|---|
| Anything inside git metadata — a `.git` folder, a repository's git directory wherever it lives, or a folder shaped like one | `hooks/` and `config` are code the agent's next git command runs |
| A file named `HEAD`, or `objects`/`refs` into a folder that already holds one | Either would turn an ordinary folder into a git repository |
| A symlink anywhere on the path | An upload never follows one |
| The editor's recovery store | The editor's save path acts on what is kept there |
| An absolute path, `..`, a backslash or a control character in a name | |
| A full disk | Reported as such; the partial file is removed |

If this platform cannot support the panel's containment checks, the panel is disabled and says why,
rather than falling back to weaker ones.

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

It can also act — fetch, pull, switch and manage branches, stage, discard, commit and push; each is
described under [Git operations](#git-operations) below. Each refuses rather than forces — a
diverged branch, a dirty tree, a missing upstream or an unresolved conflict is a stated refusal
with the next step, never a flag applied for you.

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

## Git operations

Every git write from the panel runs against the real repository, one at a time per repository, and
is bound to what the panel showed you: if the tree, the index or the branch has moved since, the
operation is refused and asks you to refresh, rather than acting on something you did not see. When
the panel can tell in advance that a control cannot run, it is disabled with the reason written on
it; otherwise the server refuses it — a pull on a tree with uncommitted changes, for example — and
the refusal shows in the tab as **Git // Refused** with the reason and the next step.

True of every operation:

- If the agent in the session is running git at that moment, the panel says **the repository is
  busy** and changes nothing.
- Network operations speak **https and ssh only**. A remote that is a folder on this machine, or that
  points back at this machine, is refused, and so is a repository that turns off TLS certificate
  verification. Network operations are bounded at 60 seconds, local ones at 30.
- Repository hooks do not run from the panel. A repository's own clean and smudge filters do, exactly
  as they would for the agent's own `git add` or checkout.

### Fetch

**FETCH** fetches the current branch's upstream remote, or the only remote. With several remotes and
no upstream it refuses rather than picking one. It updates only the remote-tracking branches — no
tags, and never a local branch.

### Pull — fast-forward only

**PULL** fetches the upstream and fast-forwards the current branch to it. It never merges and never
rebases, whatever the repository's `pull.rebase` says. It is refused when:

- HEAD is detached, or the branch has no upstream;
- there are unresolved conflicts;
- there are **any** uncommitted changes — commit or discard them first, so nothing is left sitting
  on a new base;
- the branch has diverged from its upstream — rebase or merge in the session, then pull again.

If the branch advances but the working tree cannot be brought up to match, the panel says both
things: the pull is done, and the working tree needs sorting out in the terminal.

### Switch, create and delete branches

The branch name at the top of the tab opens the branch menu:

- a **local branch** switches to it;
- a **remote-tracking** branch creates a local branch of the same name from it, and switches;
- **New branch…** creates a branch from the current one, and switches;
- **Delete branch…** lists the other local branches.

The menu is capped to the space under the branch name and the list scrolls inside that cap, so it
fits the screen at any number of branches — on a phone, in landscape, and while an on-screen
keyboard is open. **New branch…** and **Delete branch…** are pinned below the list rather than
sitting at the end of it, so they stay reachable however far the list runs.

A filter sits above the list. Typing narrows the local and the remote-tracking groups together, by
case-insensitive substring, and the count beside it reads `11 match // 40 total` — so a branch that
is filtered out is distinguishable from one that is not there at all. Enter moves focus to the
first match and never switches branch, since a switch touches the working tree and one keystroke
from a typed filter is too easy to fire by accident. Escape clears the filter first and closes the
menu on the second press. The delete list filters the same way.

Switching and creating are refused while there are uncommitted changes, because `git switch` would
silently carry them onto the other branch. If a change appears while a switch is running, the panel
goes back to the branch you were on.

Deleting is `git branch -d` only. The current branch cannot be deleted, and a branch that is not
fully merged is refused — there is no force option, which is why deleting does not ask for
confirmation.

### Stage and unstage

Rows in **Changes** and **Untracked** have **Stage**, rows in **Staged** have **Unstage**, and each
group has **Stage all** or **Unstage all** for exactly the rows it shows. Staging is whole files,
never hunks, and it stages the content the row showed — an edit that lands afterwards shows up as a
new unstaged change instead of riding in unseen. Unstaging puts the last commit's version back in
the index; on a branch with no commits yet, it removes the file from the index and leaves it in the
working tree. Both are refused while there are unresolved conflicts, and one request carries at most
500 paths.

### Discard — recoverable by object id

**Discard** appears only on rows in **Changes** — tracked files with uncommitted edits — and it is
the one git operation that asks first: the confirmation names the repository, the branch and the
session the panel is docked into.

Discard puts back the version the index holds, so for a file that is both staged and modified it
throws away only the unstaged edit. Before a file is replaced, its current contents are written to
git's object database, and the result lists their ids: recover any of them with
`git cat-file -p <id>`.

It is refused for an untracked file — git has no copy to put back, so "discard" would be a plain
delete — and when the file, or its staged version, changed after you were shown it.

### Commit

Type a message in the box at the foot of the tab and press **COMMIT**. It commits exactly the staged
set the panel showed; if anything was staged or unstaged since, it is refused, so a file the agent
staged cannot ride along unseen. The author is the git identity configured on the host — with none,
the commit is refused and asks you to set `user.name` and `user.email` in the session.

There is no amend, and no hook or signing program runs. The first commit on a new, empty branch
works. A commit is also refused when:

- nothing is staged, or there are unresolved conflicts;
- HEAD is detached — create a branch first, or the commit would not be reachable from one;
- the repository has more changes than the panel can list, so the commit would include files it
  never showed you — commit in the terminal instead;
- the branch moved while the commit was being written.

A message can be up to 16,384 characters.

### Push

**PUSH → *remote*** pushes the current branch to the branch of the same name on a remote the
**server** chooses, and the button names that remote before you press it. The panel asks
`GET /api/git/push-target`, which picks:

1. the branch's upstream remote;
2. otherwise the repository's `remote.pushDefault`;
3. otherwise the only remote.

`origin` is never assumed. With several remotes and nothing to choose between them, the tab lists
the candidates as buttons so you can pick one. A branch with no upstream gets one on its first push,
and the tab says so beforehand.

What you were shown is what gets pushed: the preflight pins the destination URL and the exact
commit, and if either has changed by the time you press — the agent committed again, or the remote's
push URL was repointed — nothing is sent and the panel asks you to refresh. There is no force push.

If the remote accepted the push but the local bookkeeping afterwards did not finish, the panel says
so plainly: the remote has the commit, and pushing again is safe but unnecessary.

::: info Verified against
Commit `8b1c66b` — `src/agent_sessions/files.py § FILES_MAX_ENTRIES, FILES_SCAN_BUDGET_MS, FILES_MAX_READ, capabilities`; `src/agent_sessions/fileedit.py § MAX_EDIT_BYTES, recovery_dir, resolve_pending, read_file, refuse_recovery_store`; `src/agent_sessions/filewrite.py § MAX_FILE_BYTES, MAX_BATCH_FILES, MAX_BATCH_BYTES, BATCH_IDLE_TTL_S, MAX_LIVE_BATCHES, MAX_LIVE_BATCHES_TOTAL, MAX_DEPTH, MAX_COMPONENT, FILE_MODE, _REPLACE_MAX, validate_relpath, refuse_git_metadata, open_destination`; `src/agent_sessions/gitpanel.py § GIT_MAX_ENTRIES, GIT_TIMEOUT_S, GIT_MAX_STDOUT, DIFF_MAX_*`; `src/agent_sessions/gitwrite.py § NET_TIMEOUT_S, LOCAL_TIMEOUT_S, GIT_ALLOW_PROTOCOL, MAX_PATHS, MAX_MESSAGE, git_fetch, git_pull, git_switch, git_branch_delete, git_stage, git_discard, git_commit, resolve_push, push_target, git_push`; `web/src/components/files/{UploadControl,UploadQueue,uploadPlan,GitTab,BranchMenu,gitOps}`.
:::
