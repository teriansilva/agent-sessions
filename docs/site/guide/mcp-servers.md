# MCP servers

An MCP server gives agents extra tools: a tracker's issues, a file store, a search index. Register
a third-party MCP server once in **Library → MCP servers** (`/library/mcp`), check that it works,
and turn it on per project. Every agent session BattleLab starts in an enabled project gets its
tools when it starts: terminal sessions, unattended dispatches and API sessions alike. Agents see
a server's tools under its name, for example `tracker` · `list_issues`.

Credentials never live in the server's record. They are **Variables** secrets, write-only, and a
project uses either the shared value or its own.

## The list

Each row is one server, and the whole row opens its page. A row shows where the server comes
from, how many tools its last passing check found, which projects have it, and its state:

| State | Means |
|---|---|
| **Ready** (green) | Every enabled project has credentials and a passing check. |
| **Needs credentials in N projects** (amber) | An enabled project has neither its own value nor the shared one. It doesn't get the server. |
| **Needs a check** (amber) | A credential, a setting or the version changed since the last check. Nothing is delivered until a check passes again. |
| **Check failing** (red) | A project's check actually failed. Only a real failure is red. |
| **Disabled** | No new session gets its tools. Its settings and project choices are kept. |
| **Not enabled in any project** | Registered, but no project has it yet. |

If the list can't be read, the page says so in red with **Retry**. It never shows "No MCP servers
yet" for a list it couldn't read.

## Add a server

**Add MCP server** opens five steps: Source · Review · Settings · Check · Enable. **Finish later**
leaves a registered server where it is; you can continue on its page.

1. **Source.** Choose where it comes from:
   - **Remote server (URL)**: someone else runs it, and sessions connect over HTTPS. Only
     `https://` addresses are accepted. Put credentials in a setting, never in the URL.
   - **npm package** or **PyPI package**: give the package name and an **exact version**
     (`1.4.2`). Ranges and `latest` are refused. A PyPI package with more than one command can
     name the one to start.
   - **Repository link**: paste a public repository's link, such as
     `https://github.com/owner/repo` (the repository itself: a `/tree/` or `/blob/` link is
     refused). BattleLab reads the repository at its current commit and installs **the npm or
     PyPI release it publishes**, or, for a Go repository, **builds its Go module from that
     commit**. See [From a repository link](#from-a-repository-link) and
     [Go repositories](#go-repositories) below.

   The name is how agents see the tools. It is suggested from the source; change it if you like.
   If you also registered a server of the same name in your CLI's own config, remove that one.
2. **Review.** What will run, before anything is downloaded. A remote server shows its URL and
   host; there is nothing to install. A package is resolved into a lock first, and the page shows
   the registry, the integrity hash, how many dependencies are pinned, the install scripts the
   package declares (they are **never run**), the command that will start it, and its license.
   Nothing is fetched until you press **Install**, and Install installs exactly the reviewed lock.
3. **Settings.** The values the server needs. Each row is either:
   - **Plain**: a value stored with the server, such as a region or a base folder.
   - **Credential**: a Variables secret. Choose **New credential** to type a value. It is stored
     in Variables as a secret, write-only, and never shown again. Or choose **Existing variable**
     to name a secret you already have.

   **Delivered as** says how the server receives each row: as an **environment variable** for a
   package, or as an **HTTP header** for a remote server. A header credential can have a prefix,
   such as `Bearer `, put in front of the value. A package can also take extra command-line
   arguments, one per line; they are passed literally, never through a shell.
4. **Check.** BattleLab starts the server (or connects to it), completes the MCP handshake and
   lists its tools, using the shared credentials. Each step shows a light and what it found. A
   failure names the step that stopped it and offers **Edit credentials** and **Run check again**.
5. **Enable.** Choose **All projects** (this includes projects you add later) or **Selected**. For
   each project the page shows which credentials it would use:
   - a project with **no value of its own** uses the shared one. Enable binds the shared secret to
     the project in Variables. That binding doesn't give agents the secret: agents can read a
     secret only if you turn on its **Agent access** (see [Secrets for agents](./secrets.md)).
   - a project with **its own value** whose check hasn't passed yet is checked when you press
     Enable. If that check fails, the project stays off and the row says why.
   - with no shared value and none of its own, the row says **Add credentials first** and links to
     the server's Projects tab.

### From a repository link

With **Repository link**, **Review** first reads the link. The page lists what it has read so far:
the default branch's commit, the manifest at the root, the published release, and whether the
release's repository record names the link. **Stop reading** ends it. The repository is read,
but no server is registered and no package is downloaded while it is, so the footer offers
**Cancel**.

The rules are fixed, and BattleLab never guesses:

- The root holds **exactly one** package manifest: `package.json` (npm), a `pyproject.toml` with
  a `[project]` table (PyPI), or a `go.mod` (Go, see [Go repositories](#go-repositories)). A
  repository with more than one is refused, and so is one with none.
- The manifest names the package and states a **fixed version**. A missing version, or a
  `pyproject.toml` whose version is `dynamic`, is refused.
- That exact version must be **published** on npm or PyPI (PyPI: with a wheel). An unpublished
  repository is refused: BattleLab doesn't build packages from source.
- The release's own repository record (npm's `repository`, PyPI's Source, Repository or Homepage
  link) must name the same host, owner and repository as your link. Anything else is refused.
  This catches a typo or a package with a borrowed name. It doesn't prove the release was built
  from that commit.

A refusal says why in words and registers nothing. **Try another link** goes back to Source.

When the link resolves, Review shows the repository and commit, the package it declares, the
registry record that names it, and *"Installs the published release; not built from this
commit."* Then come the release's facts, as for any package. **Install** registers the server and
installs exactly that lock. From there, Settings, Check and Enable are the same as for an npm or
PyPI package.

The server keeps where it came from (`github.com/owner/repo @ 3f2a9c1`). Its page shows it under
**Settings → Source**, for display only. Saving a different source (another version, say) removes
it, because it no longer describes what is installed.

### Go repositories

A repository whose root holds a `go.mod` is a **Go module**, and BattleLab **builds** it from the
commit it read. A Go build runs none of the repository's own code: Go has no install scripts,
`go:generate` never runs on a build, and cgo is off, so no C compiler runs either.

While the link is read, the rows are **Commit**, **Manifest**, **Module**, **Commit match** and
**Review**:

- **Module** asks Go's public module proxy (`proxy.golang.org`) for the module at exactly that
  commit. The first Go link also downloads **BattleLab's own Go toolchain** (about 70 MB, checked
  against the sha256 go.dev lists for it); the row says *"Downloading the Go toolchain (first time
  only)"*. On a slow connection that takes a few minutes, once. BattleLab never uses a Go installed
  on your machine.
- **Commit match** proves the code: the proxy's record for that version must name the **same
  commit** as your link. A mirror (a Codeberg copy of a module that lives elsewhere, say) is
  accepted only at the identical commit, and Review names the module's own home. A repository whose
  `go.mod` names someone else's module never matches.
- **Review** downloads the module and every module in its build, each one checked against Go's
  checksum database (`sum.golang.org`), and finds the one command it builds. Nothing is built yet.

Review then shows the repository and commit, the line *"Built from this commit with the Go
toolchain. No install scripts run; every download is verified against sum.golang.org."*, and the
Go module: its path, the version (the tag at that commit, or a pseudo-version when no tag points at
it), its home and *"same commit as the link"* for a mirror, the command, the Go version it needs,
the module hash (`h1:…`), how many modules are in its build, and the toolchain. **Install** builds
exactly that version and hash with exactly that toolchain; a build takes a few minutes the first
time and seconds after that.

What a Go repository can't be:

- **cgo**: a module that needs a C compiler is refused when it is built.
- **`replace` or `exclude` in its `go.mod`**: refused at Review, because `go install` refuses them
  too, and BattleLab builds only what `go install` builds.
- **Not on the public proxy**: a private module, or one the proxy doesn't serve, is refused. Go
  never fetches from the repository itself here.
- **Several commands**: a module whose root isn't a command and that has more than one under
  `cmd/` is refused; BattleLab builds exactly one command per server and never picks one for you.
- **A newer Go than the newest stable release**: refused.

Go builds run one at a time. A second one waits for the first (*"another Go build is still
running"* after 20 minutes), and each step stops after 10 minutes.

In **HQ**, *"add the MCP server at https://github.com/owner/repo"* drafts the same card with the
same facts and the same sentence, directly above **Register**. HQ only reads a link that you
typed in your message, never one it found in a page or a tool result. If the card sits until its
reading expires (after a few hours, or after BattleLab restarts), Register says so and HQ reads
the link again.

## The server's page

- **Overview**: the tools from the newest passing check, the source, and whether the current
  version is installed.
- **Projects**: one row per project with its credentials (**Shared**, **Own** or **Missing**),
  its last check and when, and its **Enabled** switch. **Use own credentials** stores a value for
  that project only; it is used instead of the shared one and is write-only like any secret.
  **Use shared** binds the shared value to a project that has none. **Run check** checks that
  project's values. A project with no credentials can't be switched on.
- **Settings**: the shared settings and credentials, the arguments, the probe call, and the source.
  Changing the version of a package means reviewing and installing again. A Go module's version
  can't be typed in; it changes only through **Update from source**.
- **Activity**: every check, newest first, with its steps.

Each project's page (HQ → Projects → **Memory & equipment**) lists the servers chosen for that
project, read-only, with the same credential source and check state as this tab. Its links lead
back here; nothing is edited there.

The **⋯** menu disables, enables again, updates, or removes the server. **Remove** deletes the
server's registration: its settings, project choices and check history. Its credentials stay in
Variables, and its installed files stay on disk.

### Update from source

A server added from a repository link (npm, PyPI or Go) has **Update from source**, in the **⋯**
menu and under **Settings → Source**. It reads the server's own link again, at its default branch's
current head, with the same rows as adding it. **Stop reading** or **Cancel** leaves everything as
it was, and the server keeps working meanwhile.

- If the link's head is what is installed, the page says *"Already up to date at v3.3.0
  (9c3fa38)"*. Nothing is built, nothing changes, and the server's check still counts.
- Otherwise **Review** shows what changes, old → new: the commit, the version, and the module hash
  (Go) or the release's integrity (npm, PyPI), with the same facts as adding it.
- **Install** replaces the server's source and where it came from in one step, then installs (or
  builds) exactly what Review showed. You land on the **Projects** tab: *"Updated to v3.4.0 — run
  the checks; new sessions don't get this server until a check passes."* Settings, credentials and
  project choices are kept.

If the server changed after its link was read (someone saved it, or another update ran first),
Install changes nothing and says so; **Update again** reads the link against what the server is
now. If the reading expired (after a few hours, or after BattleLab restarts), the page reads the
link again. If the install fails after the update was applied, the server names the new version but
no new session gets it until **Install again** succeeds and a check passes; running sessions keep
what they started with.

An npm or PyPI server added by name has no link to read: change its version under **Settings →
Source** as before. Typing a version for a server added from a link drops its repository line.

### Checks go stale

A check is good only for the exact values it ran with. When a credential, a setting, the arguments
or the version change, the project's check shows **Credentials changed — check again** in amber.
Its switch stays on, but **nothing is delivered** to new sessions in that project until a check
passes again. **Run all checks** at the top of the page runs the shared check, then each project's
check that hasn't passed.

### Probe call

A check can also call one tool to prove the server answers. You can pick it on the Settings tab
once a check has listed the server's tools. BattleLab calls it only if the server marks the tool
read-only (`readOnlyHint`), unless you tick **This call may change data**. Pick a harmless read,
such as listing a folder.

## Limits

- **Third-party code runs as your user.** A package server runs inside every session that gets it,
  with your permissions. A remote server receives your sessions' tool calls and the credentials
  you set for it. Only add servers you trust. Review is there to show what will run, not to vouch
  for it.
- **Running sessions keep what they started with.** A server is added when a session starts, never
  to a running one. Enabling, disabling, removing or changing a server affects the next session
  start; a server process already running keeps the values it started with until the session
  exits.
- **Not a sandbox.** Delivering a credential to a server is separate from giving it to agents, and
  agents can't list or fetch it through the secrets tools. But agents run as you, so this keeps
  credentials out of their hands by default, not by isolation.
