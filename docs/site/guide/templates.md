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
