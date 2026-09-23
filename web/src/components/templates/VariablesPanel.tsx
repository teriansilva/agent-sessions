import { useState, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { Check, Pencil, Plus, Trash2, X } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import { errMessage, FIELD_NAME_RE, cp } from "../../routes/templatesLib";
import type { TemplateRef, TemplateVariable, TemplateVariableLimits } from "../../types/api";
import { ConfirmDialog } from "./ConfirmDialog";
import page from "../../routes/Templates.module.css";
import styles from "./VariablesPanel.module.css";

/** VARIABLES — the templates' variables library (#1090, Phase 1).
 *
 *  A variable is a value defined once and used by every template field whose SOURCE is
 *  "Library" and whose name matches. Editing it here changes every such template at its next
 *  send. The name is the identity: there is no rename — delete and recreate — and a delete is
 *  refused (409, the dependants listed) while any template still uses it, so a template is
 *  never silently left pointing at nothing.
 *
 *  Edits and deletes are fenced by `updated_at`, the same optimistic-concurrency rule as a
 *  template: a stale write is a 409 the panel folds back in by reloading, never an overwrite.
 *  The fence is the revision the edit STARTED from, held in the edit itself — never the row's
 *  current `updated_at`, which a refresh (after any other create/save here) may already have
 *  moved to someone else's write (Hermes on #1095). The server re-checks every rule; the
 *  client's checks only keep the buttons honest.
 *
 *  Values may span lines (the server accepts them), so every value control is a textarea:
 *  Enter is a newline, Ctrl/⌘+Enter saves, Escape cancels. While a write is in flight every
 *  control is frozen and a second submit is ignored, so nothing typed after Save can be
 *  dropped under a "Saved" message. */

/** An edit in progress: the draft, and the revision it was started from (the save fence). A
 *  secret's edit starts EMPTY — its value is never in the browser — and replaces it whole. */
interface Editing {
  name: string;
  value: string;
  base: number;
  secret: boolean;
}

/** What NEW VARIABLE / NEW SECRET opened (#1090 Phase 2), or `null`. */
export type CreatingKind = "text" | "secret" | null;

/** A 409 that IS an edit conflict carries the record as stored now (`current`). Every other 409
 *  (the template library could not be read in full, so a delete is refused; a newer store) is
 *  not "changed elsewhere", and saying so would send the operator round a retry loop with the
 *  real reason thrown away — its `detail` is shown instead (#1095 review). */
const isConflict = (e: unknown): e is ApiError =>
  e instanceof ApiError &&
  e.status === 409 &&
  typeof e.record === "object" &&
  e.record !== null &&
  "current" in e.record;

const submitKey = (e: KeyboardEvent<HTMLTextAreaElement>) =>
  e.key === "Enter" && (e.ctrlKey || e.metaKey);

/** Rows for a value textarea: its own line count, 1–6. */
const rowsFor = (v: string) => Math.min(6, Math.max(1, v.split("\n").length));

export function VariablesPanel({
  variables,
  limits,
  creating,
  onCreatingChange,
  reload,
}: {
  variables: TemplateVariable[];
  limits: TemplateVariableLimits;
  /** The header's NEW VARIABLE / NEW SECRET opens the create row; the panel closes it. */
  creating: CreatingKind;
  onCreatingChange: (open: CreatingKind) => void;
  /** Re-read the library; resolves to whether it succeeded (a caller never claims "reloaded"
   *  over a failed reload — the gallery's rule, Hermes on #907). */
  reload: () => Promise<boolean>;
}) {
  const [draftName, setDraftName] = useState("");
  const [draftValue, setDraftValue] = useState("");
  const [editing, setEditing] = useState<Editing | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [inUse, setInUse] = useState<{ name: string; dependants: TemplateRef[] } | null>(null);
  const [note, setNote] = useState("");
  const [pendingDelete, setPendingDelete] = useState<{
    v: TemplateVariable;
    returnTo: HTMLElement | null;
  } | null>(null);

  const say = (msg: string) => {
    setError("");
    setInUse(null);
    setNote(msg);
  };
  const fail = (msg: string) => {
    setNote("");
    setInUse(null);
    setError(msg);
  };

  const nameProblem =
    draftName && !FIELD_NAME_RE.test(draftName)
      ? "a-z, 0-9, _ · starts with a letter · max 32"
      : variables.some((v) => v.name === draftName)
        ? "a variable with this name exists"
        : "";
  const creatingSecret = creating === "secret";
  const secretMin = limits.secret_min ?? 8;
  const canCreate =
    !busy &&
    FIELD_NAME_RE.test(draftName) &&
    !nameProblem &&
    draftValue.trim() !== "" &&
    cp(draftValue) <= limits.value_max &&
    (!creatingSecret || (draftValue.length >= secretMin && draftValue === draftValue.trim())) &&
    variables.length < limits.variables_max;

  const closeCreate = () => {
    setDraftName("");
    setDraftValue("");
    onCreatingChange(null);
  };

  const create = async () => {
    if (!canCreate || busy) return;
    setBusy(true);
    try {
      const rec = await api.createTemplateVariable({
        name: draftName,
        value: draftValue,
        kind: creatingSecret ? "secret" : "text",
      });
      closeCreate();
      const ok = await reload();
      say(
        ok
          ? rec.kind === "secret"
            ? `Stored the secret {{${rec.name}}}. It will not be shown again — use it from a field set to Library and Secret.`
            : `Added {{${rec.name}}}. Set a template field's source to Library to use it.`
          : `Added {{${rec.name}}}, but the list could not be refreshed.`,
      );
    } catch (e) {
      fail(errMessage(e, "Could not add the variable"));
    } finally {
      setBusy(false);
    }
  };

  const save = async (v: TemplateVariable) => {
    // `busy` also covers Ctrl+Enter, which does not go through the (disabled) Save button.
    if (!editing || busy) return;
    setBusy(true);
    try {
      await api.updateTemplateVariable(v.name, editing.value, editing.base);
      setEditing(null);
      const ok = await reload();
      const n = v.used_by.length;
      say(
        ok
          ? `Saved {{${v.name}}}${n ? ` — ${n} ${n === 1 ? "template uses" : "templates use"} it from the next send` : ""}.`
          : `Saved {{${v.name}}}, but the list could not be refreshed.`,
      );
    } catch (e) {
      if (isConflict(e) || (e instanceof ApiError && e.status === 404)) {
        setEditing(null);
        const ok = await reload();
        fail(
          `{{${v.name}}} ${e.status === 404 ? "was deleted" : "changed"} elsewhere — ` +
            (ok ? "reloaded, nothing saved." : "and reloading failed; nothing saved."),
        );
      } else {
        fail(errMessage(e, "Could not save the variable"));
      }
    } finally {
      setBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    const { v } = pendingDelete;
    setBusy(true);
    try {
      await api.deleteTemplateVariable(v.name, v.updated_at);
      setPendingDelete(null);
      const ok = await reload();
      say(
        ok
          ? `Deleted {{${v.name}}}.`
          : `Deleted {{${v.name}}}, but the list could not be refreshed.`,
      );
    } catch (e) {
      setPendingDelete(null);
      const deps =
        e instanceof ApiError && e.status === 409
          ? (e.record as { dependants?: TemplateRef[] } | undefined)?.dependants
          : undefined;
      if (deps && deps.length) {
        // Mockup E: name the templates, so the operator can go and switch them.
        setNote("");
        setError("");
        setInUse({ name: v.name, dependants: deps });
        void reload();
      } else if (isConflict(e) || (e instanceof ApiError && e.status === 404)) {
        const ok = await reload();
        fail(
          `{{${v.name}}} ${e.status === 404 ? "was already deleted" : "changed"} elsewhere — ` +
            (ok ? "reloaded, nothing deleted." : "and reloading failed; nothing deleted."),
        );
      } else {
        fail(errMessage(e, "Could not delete the variable"));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Variables">
      {note && <p className={page.note}>{note}</p>}
      {error && (
        <p className={page.err} role="alert">
          {error}
        </p>
      )}
      {inUse && (
        <p className={page.err} role="alert">
          Can&apos;t delete <code>{`{{${inUse.name}}}`}</code> —{" "}
          {inUse.dependants.length === 1
            ? "1 template still uses"
            : `${inUse.dependants.length} templates still use`}{" "}
          it:{" "}
          {inUse.dependants.map((d, i) => (
            <span key={d.id}>
              {i > 0 && ", "}
              <Link to={`/templates/${encodeURIComponent(d.id)}`}>{d.name}</Link>
            </span>
          ))}
          . Change those fields to &ldquo;This template&rdquo; first.
        </p>
      )}

      {creating && (
        <form
          className={styles.create}
          aria-label={creatingSecret ? "New secret" : "New variable"}
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <label className={styles.cell}>
            <span className={styles.lbl}>Name</span>
            <input
              className={`${page.input} ${styles.mono}`}
              value={draftName}
              readOnly={busy}
              onChange={(e) => setDraftName(e.target.value.trim().toLowerCase())}
              placeholder="staging_host"
              autoComplete="off"
              autoFocus
              aria-invalid={nameProblem !== ""}
            />
            {nameProblem && <span className={styles.rowErr}>{nameProblem}</span>}
          </label>
          {creatingSecret ? (
            <label className={`${styles.cell} ${styles.grow}`}>
              <span className={styles.lbl}>
                🔒 Secret · at least {secretMin} characters · never shown again
              </span>
              <input
                type="password"
                className={`${page.input} ${styles.mono}`}
                value={draftValue}
                readOnly={busy}
                onChange={(e) => setDraftValue(e.target.value)}
                placeholder="stored encrypted"
                autoComplete="new-password"
                spellCheck={false}
              />
              {draftValue !== "" && draftValue.length < secretMin && (
                <span className={styles.rowErr}>at least {secretMin} characters</span>
              )}
            </label>
          ) : (
            <label className={`${styles.cell} ${styles.grow}`}>
              <span className={styles.lbl}>
                Value · {cp(draftValue)} / {limits.value_max}
              </span>
              <textarea
                className={`${page.input} ${styles.mono} ${styles.valueArea}`}
                value={draftValue}
                rows={rowsFor(draftValue)}
                readOnly={busy}
                onChange={(e) => setDraftValue(e.target.value)}
                onKeyDown={(e) => {
                  if (submitKey(e)) {
                    e.preventDefault();
                    void create();
                  }
                }}
                placeholder="staging.acme.test"
                autoComplete="off"
                spellCheck={false}
              />
            </label>
          )}
          <div className={styles.createActs}>
            <button type="submit" className={page.cta} disabled={!canCreate}>
              <Check size={13} aria-hidden="true" />
              Add
            </button>
            <button type="button" className={page.ghost} onClick={closeCreate}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {variables.length === 0 && !creating && (
        <div className={page.empty}>
          <span className={page.emptyGlyph} aria-hidden="true">
            {"{{ }}"}
          </span>
          <p className={page.emptyTitle}>No variables yet</p>
          <p className={page.emptyHint}>
            Define a value once — a host, a repo URL, a test command — and use it in any template as{" "}
            <code>{"{{name}}"}</code>: add a field with that name and set its source to Library.
            Change it here, and every template that uses it changes with it.
          </p>
          <div className={page.emptyActs}>
            <button type="button" className={page.cta} onClick={() => onCreatingChange("text")}>
              <Plus size={14} aria-hidden="true" />
              New variable
            </button>
            <button type="button" className={page.ghost} onClick={() => onCreatingChange("secret")}>
              🔒 New secret
            </button>
          </div>
        </div>
      )}

      {variables.length > 0 && (
        <ul className={styles.list} aria-label="Variables">
          <li className={styles.headRow} aria-hidden="true">
            <span>Name</span>
            <span>Value</span>
            <span>Used by</span>
            <span />
          </li>
          {variables.map((v) => {
            const on = editing?.name === v.name;
            return (
              <li key={v.name} className={styles.row} data-variable={v.name}>
                <span className={styles.name}>
                  {`{{${v.name}}}`}
                  {v.kind === "secret" && <span className={styles.kindChip}>🔒 secret</span>}
                </span>
                {on && editing.secret ? (
                  <input
                    type="password"
                    className={`${page.input} ${styles.mono}`}
                    value={editing.value}
                    readOnly={busy}
                    onChange={(e) => setEditing({ ...editing, value: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void save(v);
                      } else if (e.key === "Escape" && !busy) {
                        setEditing(null);
                      }
                    }}
                    aria-label={`New value of ${v.name}`}
                    placeholder={`at least ${secretMin} characters`}
                    autoComplete="new-password"
                    spellCheck={false}
                    autoFocus
                  />
                ) : on ? (
                  <textarea
                    className={`${page.input} ${styles.mono} ${styles.valueArea}`}
                    value={editing.value}
                    rows={rowsFor(editing.value)}
                    readOnly={busy}
                    onChange={(e) => setEditing({ ...editing, value: e.target.value })}
                    onKeyDown={(e) => {
                      if (submitKey(e)) {
                        e.preventDefault();
                        void save(v);
                      } else if (e.key === "Escape" && !busy) {
                        setEditing(null);
                      }
                    }}
                    aria-label={`Value of ${v.name}`}
                    autoComplete="off"
                    spellCheck={false}
                    autoFocus
                  />
                ) : v.kind === "secret" ? (
                  v.needs_reentry ? (
                    <span className={styles.reentry}>
                      ⚠ needs re-entry · the stored value can no longer be decrypted
                    </span>
                  ) : (
                    <span className={styles.masked}>
                      ••••••••<span className={styles.maskHint}> set · never shown again</span>
                    </span>
                  )
                ) : (
                  <span className={styles.value} title={v.value}>
                    {v.value}
                  </span>
                )}
                <span className={styles.used}>
                  {v.used_by.length === 0 ? (
                    "not used yet"
                  ) : (
                    <span title={v.used_by.map((d) => d.name).join(", ")}>
                      {v.used_by.length} {v.used_by.length === 1 ? "template" : "templates"}
                    </span>
                  )}
                </span>
                <span className={styles.acts}>
                  {on ? (
                    <>
                      <button
                        type="button"
                        className={`${page.act} ${page.actUse}`}
                        disabled={
                          busy ||
                          !editing.value.trim() ||
                          cp(editing.value) > limits.value_max ||
                          (editing.secret && editing.value.length < secretMin)
                        }
                        onClick={() => void save(v)}
                        aria-label={`Save ${v.name}`}
                      >
                        <Check size={13} aria-hidden="true" />
                        Save
                      </button>
                      <button
                        type="button"
                        className={page.act}
                        disabled={busy}
                        onClick={() => setEditing(null)}
                        aria-label={`Cancel editing ${v.name}`}
                      >
                        <X size={13} aria-hidden="true" />
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        className={page.act}
                        disabled={busy}
                        onClick={() =>
                          setEditing({
                            name: v.name,
                            value: v.kind === "secret" ? "" : (v.value ?? ""),
                            base: v.updated_at,
                            secret: v.kind === "secret",
                          })
                        }
                        aria-label={
                          v.kind === "secret"
                            ? `${v.needs_reentry ? "Re-enter" : "Replace"} ${v.name}`
                            : `Edit ${v.name}`
                        }
                      >
                        <Pencil size={13} aria-hidden="true" />
                        {v.kind === "secret" ? (v.needs_reentry ? "Re-enter" : "Replace") : "Edit"}
                      </button>
                      <button
                        type="button"
                        className={`${page.act} ${page.actDanger}`}
                        disabled={busy}
                        onClick={(e) => setPendingDelete({ v, returnTo: e.currentTarget })}
                        aria-label={`Delete ${v.name}`}
                      >
                        <Trash2 size={13} aria-hidden="true" />
                        Delete
                      </button>
                    </>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {pendingDelete && (
        <ConfirmDialog
          tag="Delete variable"
          title={`{{${pendingDelete.v.name}}}`}
          confirmLabel="Delete"
          danger
          busy={busy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void confirmDelete()}
          returnFocusTo={pendingDelete.returnTo}
        >
          <p>
            {pendingDelete.v.used_by.length
              ? `${pendingDelete.v.used_by.length} ${pendingDelete.v.used_by.length === 1 ? "template uses" : "templates use"} this variable, so the delete will be refused until they no longer do.`
              : "No template uses this variable."}{" "}
            There is no rename: to change a name, add the new one and delete this one.
          </p>
        </ConfirmDialog>
      )}
    </section>
  );
}
