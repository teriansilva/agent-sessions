# Templates

A template is an instruction you send more than once: named text, the reference images that belong
with it, and `{{field}}` slots you fill in when you use it.

It is a **message, not a prompt**. A template goes into a live session through the composer, as
user input — exactly like something you typed. It never becomes a system prompt and never enters
the [prompt catalog](/reference/), and nothing in BattleLab acts on a template's content by itself.

## The library

The **Templates** icon in the top bar opens `/templates`: a gallery of every template you have
saved.

- **Sorted by last use** — the template you most recently *sent* comes first, then the most
  recently edited. Inserting a template into a composer without sending it does not change the
  order.
- **Search** matches a template's name, description, tags and text.
- **Tag chips** filter the gallery to one tag. **All** shows everything, and each chip carries its
  count.

Each card shows the first image, the name, the description and the tags, with four actions:
**Use**, **Edit**, **Duplicate** (saves a copy named `<name> (copy)` and opens it in the editor) and
**Delete** (asks first).

## Writing one

**New template** opens the editor; **Edit** opens an existing one.

| Part | What it is |
|---|---|
| **Name** | Required. One line. |
| **Description** | One line, shown on the card. |
| **Tags** | Lowercase letters, digits, `-` and `_`, starting with a letter or digit. |
| **Instructions** | The text that is sent. Required. `{{field}}` marks a slot you fill when you use it. |
| **Fields** | One row per slot, filled at send time in this order: a **name** (a lowercase letter, then letters, digits or `_`), a **label**, a **default** and whether it is **required**. |
| **Images** | Reference pictures, sent as file paths after the text. |

Substitution is a literal replace of `{{name}}` for the fields you declared — there is no expression
language and no escaping. A `{{token}}` that names no declared field is sent as written, and the
editor warns you about it.

Images are uploaded through the same route as a file pasted into the composer, into BattleLab's
uploads folder (`~/.agent-sessions/uploads`, a `0700` folder of `0600` files). A template stores
only the path. If that file is deleted later, the template still works as text; only its thumbnail
is missing.

### Variables: values you define once

Some values appear in many templates: a staging host, a repository URL, the command that runs your
tests. Instead of copying them into each template's field defaults, define them once under the
**Variables** tab of `/templates` (`/templates?tab=variables`) and use them from any template.

To use a variable, add a field with the **same name** and set its **Source** to **Library**. A
library field has no default of its own: the editor shows the variable's value instead, and the
preview uses it. **Edit** a variable, and every template that uses it sends the new value from its
next send. The picker reads the library when it opens, so a picker that is already open keeps the
values it opened with; close and reopen it to pick up an edit made meanwhile.

A value may span several lines, for example `cd repo` and `npm test` on separate lines. Enter adds a
line; **Ctrl+Enter** (**⌘+Enter** on a Mac) saves, and Escape cancels an edit.

- **In the picker**, a library field starts at the variable's value, marked **from library**. You
  can change it for that one send; the variable itself does not change.
- **A missing variable blocks the send.** If a template uses a library field whose variable does
  not exist, the picker says which one, and both **Send** and **Insert** stay disabled until you add
  it. The `{{token}}` is never sent as written, and the slot is never sent empty.
- **There is no rename.** A variable's name is what templates match on. To change a name, add the
  new variable, switch the templates to it, and delete the old one.
- **A variable in use cannot be deleted.** The delete is refused and the templates that still use
  it are listed, so no template is silently left without its value. Change those fields' source
  back to **This template** first.

Variable names follow the field-name rule (a lowercase letter, then letters, digits or `_`, up to
32 characters). A value is up to 2,000 characters, with the same control-character rule as the
instructions. The library holds up to 100 variables, in its own file
`~/.config/agent-sessions/template-variables.json` beside `templates.json` (override:
`AGENT_SESSIONS_TEMPLATE_VARS`), written atomically at mode `0600`, with the same damaged-file and
newer-version protections as the template library below.

### Secret fields: passwords and tokens

A field whose **Kind** is **Secret** holds a password, token or other credential. The value is
never written into the template and never shown again after you enter it. BattleLab never sends a
stored secret to your browser. The one exception is what the agent itself prints: after a send,
the value can appear on the session's screen and in its history.

- **Stored secret.** Under **Variables**, **New secret** stores a value encrypted. A secret is at
  least 8 characters, and cannot start or end with a space. It appears as `••••••••` with **Replace**, which starts empty: you type a new value,
  since the old one is never shown. Use it from a field with **Source: Library** and **Kind:
  Secret** of the same name.
- **Typed once.** A secret field with **Source: This template** is asked for in the picker, in a
  password box, each time you send. It is not stored anywhere.

A template with any secret field is **sent by the server**: BattleLab fills in the secrets, then
clears the prompt line, pastes the message and presses Enter in the session, spaced like a normal
send. So such a template can be **sent** from a session's composer but never **inserted** into the
composer or a mission brief, because either would put the secret in a text box. It also cannot be
sent into a session that has not started yet, because there is no session id to send to.

What you see instead of the value:

- the picker and editor preview show `[secret: name]`;
- your sent history keeps the same masked text, so **Restore** gives you the mask, not the secret;
- the send fails with a clear reason, and nothing is typed, if a stored secret is missing or needs
  re-entry.
- one template send at a time per session: a second one sent while the first is still being typed
  is refused, so two messages can never merge into one prompt. The picker stays open until the
  send has finished.

**What the protection covers, and what it does not.**

- The file `~/.config/agent-sessions/template-variables.json` holds only ciphertext. The key is
  a separate file, `template-secrets.key` (0600, next to it). A copy of the store without the key
  file decrypts nothing. **Leave `template-secrets.key` out of any sync or backup** of that folder
  if you want the encryption to protect that copy.
- The key is not your login's secret key, so rotating that key does not affect stored secrets. If
  the key file is lost or replaced, every stored secret shows **needs re-entry**; enter it again.
- **The agent receives the plain value**, because that is the point. It may repeat it in its
  output, a commit, a log or a pull request, and it is on that session's screen (the terminal and
  its scroll-back history) and in its transcript. Agents run as your user and can read the key
  file.
- A **Quick** handoff copies recent turns into a new session; a secret in those turns is replaced
  with `[secret]` first, in the preview and in what the new agent receives.
- **Before anything goes to your AI endpoint** (reviews, recaps, Ask, missions), BattleLab
  replaces every stored secret, and every value typed once in the last 24 hours, with `[secret]`.
  This covers the plain text, JSON- and URL-encoded copies, and copies split by terminal colour
  codes. It **cannot** catch a secret the agent's screen wrapped across two lines, or one the
  agent re-encoded (base64, hex).
- If BattleLab cannot read the file of stored secrets in full, it **does not call the AI
  endpoint** (the review or recap reports an error) rather than risk sending one unredacted. It
  refuses even if it read the file successfully a moment before, because a secret may have been
  added since.
- A value typed once, and a stored value you **replace or delete** (or that stops decrypting),
  keeps being redacted for 24 hours from when BattleLab last held it, and only until the app
  restarts. After that it is no longer known, so a transcript that still contains it is sent as it
  is. If you rotate a leaked credential, it is safest to also archive the sessions it was sent to.

### Suggested templates: what should you write?

The **Suggested** tab asks your AI endpoint to help you write templates, in two ways.

**Write me a template for…** — describe the template you want ("reviewing a pull request against
our guidelines") and press **Write it**. The AI drafts one, and it opens in the editor as a new
template, with `{{fields}}` for the parts that change and your library variables where they fit.
Only your request and the names of your templates and variables are sent — no messages, no values.
A request that looks like it contains a password or token is refused before anything is sent: use a
`{{placeholder}}` instead. Nothing is saved until you save it in the editor.

**Analyse my messages** reads what you typed to your agents over the last 30 days, newest first,
and proposes:

- **templates** for instructions you keep retyping with small changes, with `{{fields}}` for the
  parts that change;
- **variables** for values you keep pasting, such as a host, a URL or a command.

It runs **only when you press Analyse**; nothing runs in the background. Every suggestion is a
draft:

- **Open in editor** starts a new template with the draft filled in.
- **Add to library** opens the new-variable form filled in.
- **Dismiss** hides a suggestion for good, even if a later analysis proposes it again.

Nothing is saved until you save it yourself.

What is sent, and what is not:

- Only the messages **you** typed in the last 30 days, never the agents' replies. That is the
  date of each message: an old session you resumed today contributes only what you typed in it
  recently. Only from sessions inside your project
  folders (roots and exclusions), and not from sessions you archived in BattleLab, whatever the
  engine. At most 60 sessions and 400
  distinct messages; identical messages are sent once, with how often you sent them.
- Your stored secrets are removed before anything leaves BattleLab. If they cannot be read,
  nothing is sent.
- A message that looks like it holds a credential is **left out entirely** — not redacted, not
  sent. The same goes for a template description (the template's name is still sent). It is left
  out if it shows any of:
  - a `password=`, `PGPASSWORD=` or `token:` style setting;
  - a password inside a URL, or in `curl -u user:pass`;
  - `mysql -p…`, `sshpass -p …` or `--password …`;
  - an `Authorization` header, a bearer token or a private key block;
  - a long random-looking string;
  - "the password is …".

  The analysis says how many messages were left out. This is a pattern match, not a guarantee: a
  password written in prose with nothing around it ("log in with admin / hunter2") is not
  recognised, so keep real credentials in stored secrets.
- Text an agent wrote is never sent, even when it arrives as your turn. That covers Claude's
  "continued from a previous conversation" summary and a handoff's seed.
- A suggested variable that looks like a credential arrives **without its value**. You type it
  into the new-secret form yourself.
- The model's reply is checked like anything you could have typed. A suggestion that is not valid
  as a template or variable, that duplicates one you already have, or that contains one of your
  stored secrets or anything shaped like a credential, is left out. Only the checked
  suggestions are kept, in `~/.config/agent-sessions/template-suggestions.json` (0600), never the
  raw reply.

It needs the AI endpoint from **Settings → AI**. You can change the instruction it uses under
**Settings → AI → Prompts** ("Template suggestions").

### The preview is exactly what the agent receives

**What the agent receives** shows the instructions with each field's default filled in, followed by
the image paths — the same assembly a send uses, so what the preview shows is what gets pasted. A
send is one bracketed paste followed by Enter, the same path as a message you type.

### Saving

Save is refused, and the editor tells you why, when a rule is broken: a missing name or text,
anything over a limit, a malformed tag or field name, or a **control character** in any text — any
C0 control other than tab and line feed, DEL, or a C1 control. Carriage returns are not refused:
CRLF and a lone CR become LF before the check. An escape character inside a paste can end the paste
early and turn the rest into key presses, so it is rejected and named rather than silently stripped.

If the template was changed somewhere else after you opened it — another tab, another device — the
save is refused with **Changed elsewhere**: **Reload theirs**, or **Overwrite** with yours. Leaving
the editor with unsaved changes asks first (**Keep editing** / **Discard and leave**).

## Using one in a session

The composer's **Use a template** control opens the picker:

1. Search for a template and select it.
2. Fill its fields. Defaults are filled in for you, and required fields are marked.
3. Check **What will be sent**.
4. **Send** delivers it now, as one message, recorded in sent history like any other. It stays
   disabled until every required field has a value. **Insert into composer** puts the text and
   images into the composer instead, so you can edit it and press Send yourself.

**Use** on a gallery card does the same from the other end: pick a session, and its pane opens with
the picker already on that template. The list offers the non-archived sessions the sidebar has
loaded, working sessions first.

### Save as template

A message worth keeping can become a template without retyping it:

- **Save as template** in the composer, whenever it holds text or an attachment;
- **Save as template** on any entry in the composer's **Sent messages** history.

Both open a new template with the text and images filled in; add a name and save. A session that has
not started yet has no id to keep your draft under, so the composer says so instead of leaving and
losing it.

## Using one in a mission brief

On the new-mission page, **TEMPLATE** opens the same picker with one difference: a mission has no
session to send into yet, so it offers **Insert into mission brief** only. The template is appended
to the brief, and nothing is sent until you press START. See [Mission control](/guide/missions).

## Storage and limits

The library lives in its own file, `~/.config/agent-sessions/templates.json` beside `prefs.json`
(override: `AGENT_SESSIONS_TEMPLATES`), written atomically at mode `0600`. It is loaded only by the
gallery and the pickers, never at app start, and every templates response is sent `no-store`.

| Bound | Value |
|---|---|
| Templates in the library | 200 |
| Name | 120 characters |
| Description | 300 characters |
| Tags per template | 8, each up to 24 characters |
| Instructions | 100,000 characters |
| Fields per template | 12 |
| Field name · label · default | 32 · 60 · 500 characters |
| Images per template | 8 — `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` |
| One image upload | 25 MB |

Two protections are worth knowing about:

- **A damaged file is never overwritten in place.** A file that cannot be parsed, or that contains a
  record that fails validation, reads as the records that can be trusted. The first successful save
  after that keeps the damaged file as `templates.json.corrupt-<timestamp>` before writing the new
  one.
- **A newer file is left alone.** If `templates.json` was written by a newer BattleLab, this version
  shows an empty library and refuses every save, rather than rewriting records it does not
  understand.

::: info Verified against
Commit `8b1c66b` — `src/agent_sessions/templates.py § TEMPLATES_MAX, NAME_MAX, DESCRIPTION_MAX, TAGS_MAX, TAG_RE, BODY_MAX, FIELDS_MAX, FIELD_NAME_RE, LABEL_MAX, DEFAULT_MAX, IMAGES_MAX, IMAGE_SUFFIXES, store_path, _write, _read, _quarantine, _sorted`; `src/agent_sessions/routes/templates.py`; `src/agent_sessions/routes/upload.py § DIR_MODE, FILE_MODE, IMAGE_TYPES, upload_context`; `web/src/routes/Templates.tsx`, `web/src/routes/TemplateEditor.tsx`, `web/src/components/templates/`, `web/src/components/terminal/Compose.tsx`, `web/src/components/pulse/Composer.tsx`, `web/src/lib/templateMessage.ts`.
:::
