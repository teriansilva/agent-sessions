# Secrets for agents

The variables library can hold credentials agents need at work: API tokens, passwords, private
keys, certificates such as an App Store Connect `.p8` key, or any other secret file. Every agent
BattleLab starts can ask for one. It never gets the content in a reply. It gets the **path of a
private copy** that BattleLab writes for that session alone and deletes when access ends.

Secrets are **off for agents by default**. A secret reaches an agent only when both of these hold:

- the secret's **Agent access** switch is on, and
- the agent's session belongs to a **project that binds the secret**.

## Store a secret

Open **Library → Variables** and choose **New secret**. Pick a type:

| Type | For | Notes |
|---|---|---|
| **Single line** | a token or password | Typed into a password field. |
| **Multi-line · PEM, key, JSON** | a PEM key, a certificate, a JSON service-account file | Leading and trailing blank lines are removed. |
| **File · upload** | any file, text or binary (`.p8`, `.p12`, `.json`, …) | Drop it on the page or choose it. Up to 256 KB. The file name is kept exactly as uploaded, because tools such as `altool` need it. |

Every value is **write-only**. It is stored encrypted and never shown or downloaded again; **Edit**
starts with an empty replacement field (or **Replace file…**), and leaving it alone keeps what is
stored. A secret file's row shows its name and size only.

## Give agents access

In the secret's editor:

1. Turn **Agent access** on.
2. Under **Projects that can use it**, tick the projects whose agents may ask for it.
3. **Save.**

A project that already has its own value under the same name keeps it; BattleLab refuses to bind
over it. A binding that a playbook deployment needs cannot be removed here.

Changes reach the next request at once:

- Turning **Agent access** off, or unticking a project, refuses every later request (from every
  project, or from that project) and deletes the copies BattleLab already placed.
- Replacing the value or the file deletes the existing copies; the next request gets a copy of the
  new one.
- Editing the label or help text, or ticking another project, leaves existing copies alone.

None of this can recall a copy an agent has already read, copied or sent anywhere.

A project you deleted or archived while it still bound the secret appears in the list as
**deleted or archived project**, so you can untick it.

## How an agent asks

The page's **How agents reach secrets** list shows, for each installed agent, which way it asks:

- **Secrets tool.** Claude Code, Codex, OpenCode and GitHub Copilot get a `battlelab_secrets` tool
  beside the memory tool, wired in when BattleLab starts them. `secret_list` names the secrets
  the session may use; `secret_path` returns the path of a private copy of one.
- **Helper command.** Every agent, including those without the tool (Gemini CLI, Antigravity,
  Kimi Code), can run the command in `$BATTLELAB_SECRETS_HELPER`:

  ```sh
  "$BATTLELAB_SECRETS_HELPER" list
  "$BATTLELAB_SECRETS_HELPER" path asc_api_key
  ```

Either way the agent gets a path such as
`/run/user/1000/agent-sessions/secrets/<id>/asc_api_key/AuthKey_7F3K2L9QXA.p8` and passes it to
the tool that needs it. A text secret's copy is a file named after the secret.

Like memory, secrets reach **new agent processes only**: a new or resumed terminal session, an
unattended dispatch, or a new API worker. Reconnecting to a running session changes nothing, so a
session started before you turned access on gets it after a restart. A session that is not in a
project gets no secrets.

## The access log

The editor of each secret shows the **access log**: the last 50 requests for it, granted and
refused (including requests from projects that don't bind it), with the project, the agent and a
link to the session that received a copy. It never
holds a value. If the log cannot be written, every request is refused: no copy is handed out
without its log entry.

## Where copies live

Copies are written to RAM-backed storage under your user's runtime directory
(`$XDG_RUNTIME_DIR`), readable by your user only, one directory per session launch. If no
RAM-backed storage is available, BattleLab hands out nothing. When the session ends, is archived,
or loses access, its copies are deleted. Anything else an agent writes into that directory is
deleted too, so keep work files elsewhere.

## What this does not protect against

- **Agents run as you.** An agent with permission bypass can read the copy and send it anywhere,
  and could read BattleLab's own files directly. Secrets for agents scope, log and revoke what
  BattleLab hands out; they are not a sandbox. Give access only to secrets an agent needs.
- **A binary file cannot be masked.** BattleLab masks text secrets in what it sends to a model
  (see [Prompt Templates](./templates.md#secret-fields-passwords-and-tokens)). If an agent prints
  a binary file, that output is not masked.

## If the key file is lost

Stored secrets are encrypted with `template-secrets.key`, next to the variables store. If that
file is lost or replaced, every stored secret and secret file shows **needs re-entry**, and agents
asking for one are refused. The two kinds then differ:

- A **text secret** that can't be decrypted refuses only the sends and templates that use it.
- A **secret file** that can't be decrypted pauses **every** model call: BattleLab can no longer
  mask that file's contents in anything it sends, so it sends nothing until you upload the file
  again or delete it.

Saving a new secret after the loss creates a new key; files stored under the old one keep refusing
until you upload them again. Keep `template-secrets.key` out of any sync or backup of that folder, and
keep its own backup somewhere safe.
