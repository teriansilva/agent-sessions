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
     PyPI release it publishes**. Nothing is built from the repository's code. See
     [From a repository link](#from-a-repository-link) below.

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

- The root holds **exactly one** package manifest: `package.json` (npm) or a `pyproject.toml` with
  a `[project]` table (PyPI). A repository with both is refused, and so is one with neither.
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
  Changing the version of a package means reviewing and installing again.
- **Activity**: every check, newest first, with its steps.

Each project's page (HQ → Projects → **Memory & equipment**) lists the servers chosen for that
project, read-only, with the same credential source and check state as this tab. Its links lead
back here; nothing is edited there.

The **⋯** menu disables, enables again, or removes the server. **Remove** deletes its settings,
install and check history. Its credentials stay in Variables.

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
