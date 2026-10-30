import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useBlocker, useLocation, useNavigate } from "react-router-dom";
import { FileKey, LockKeyhole, Plus, Search } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { VARIABLES_PATH } from "../lib/routes";
import type { CollectionInput, SharedVariable, SharedVariableInput, SharedVariableUpdate, SharedVariablesResponse, VariableCollection } from "../types/variables";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { VariableForm } from "../components/variables/VariableForm";
import { draftFor, isNoopUpdate, isSecret, variablePayload, type VariableDraft } from "../components/variables/variableDraft";
import { formatSize } from "../components/variables/secretFile";
import { SecretChannels } from "../components/variables/SecretChannels";
import buttons from "../components/ui/actionButton.module.css";
import styles from "./Variables.module.css";

type Editor =
  | { kind: "variable"; original?: SharedVariable; draft: VariableDraft; snapshot: string }
  | { kind: "collection"; original?: VariableCollection; draft: CollectionInput; snapshot: string };
type DeleteTarget = { kind: "variable"; row: SharedVariable; returnTo: HTMLElement } | { kind: "collection"; row: VariableCollection; returnTo: HTMLElement };
const message = (e: unknown) => e instanceof Error ? e.message : "The request failed. Try again.";
function prefilledEditor(state: unknown): Editor | null {
  const prefill = (state as { variablePrefill?: { name: string; value: string; secret: boolean } } | null)?.variablePrefill;
  if (!prefill) return null;
  const draft = { ...draftFor(undefined, prefill.secret ? "secret" : "text"), name: prefill.name, value: prefill.secret ? "" : prefill.value };
  return { kind: "variable", draft, snapshot: JSON.stringify(draftFor(undefined, draft.kind)) };
}

/** One shared library. Form values, including write-only secret input, stay in memory only. */
export default function Variables() {
  const location = useLocation();
  const navigate = useNavigate();
  const [library, setLibrary] = useState<SharedVariablesResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [note, setNote] = useState("");
  const [selected, setSelected] = useState("all");
  const [query, setQuery] = useState("");
  const [editor, setEditor] = useState<Editor | null>(() => prefilledEditor(location.state));
  const [busy, setBusy] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [deleting, setDeleting] = useState<DeleteTarget | null>(null);
  const live = useRef(true);
  // A chosen secret file stays in memory only, outside the draft, until Save uploads it (#1786).
  const fileRef = useRef<File | null>(null);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const busyRef = useRef(false);
  const loadGeneration = useRef(0);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const dirty = editor !== null && JSON.stringify(editor.draft) !== editor.snapshot;
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (dirty || busyRef.current) && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  // A save may happen below the fold on a phone; bring its refusal into view and focus.
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);
  useEffect(() => {
    if (!dirty && !busy) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, busy]);

  const load = useCallback(() => {
    const generation = ++loadGeneration.current;
    return Promise.resolve().then(() => api.variables()).then(next => {
      if (live.current && generation === loadGeneration.current) setLibrary(next);
      return next;
    }).catch((e: unknown) => {
      if (live.current && generation === loadGeneration.current) setError(e);
      return null;
    });
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    api.projectPickerEntities().then(r => { if (live.current) setProjects(r.projects.map(p => ({ id: p.id, name: p.name }))); }).catch(() => {});
  }, []);
  // Suggestions pass only a non-secret value. Consume the state once; never persist typed secrets.
  useEffect(() => {
    if (location.state) navigate(VARIABLES_PATH, { replace: true, state: null });
  }, [location.state, navigate]);

  const perform = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(null); setNote("");
    try { await action(); } catch (e) { if (live.current) setError(e); }
    finally { busyRef.current = false; if (live.current) setBusy(false); }
  };
  const closeEditor = () => { fileRef.current = null; setEditor(null); setError(null); setDiscard(false); requestAnimationFrame(() => titleRef.current?.focus()); };
  const editVariable = (original?: SharedVariable, kind: "text" | "secret" | "secret_file" = "text") => {
    fileRef.current = null;
    const draft = draftFor(original, kind);
    setEditor({ kind: "variable", original, draft, snapshot: JSON.stringify(draft) }); setError(null); setNote("");
  };
  const editCollection = (original?: VariableCollection) => {
    const draft = original ? { name: original.name, variable_ids: [...original.variable_ids] } : { name: "", variable_ids: [] };
    setEditor({ kind: "collection", original, draft, snapshot: JSON.stringify(draft) }); setError(null); setNote("");
  };
  const save = () => perform(async () => {
    if (!editor) return;
    if (editor.kind === "variable") {
      const { draft, original } = editor;
      // The file is taken with the draft it belongs to: nothing chosen after Save reaches this save.
      const file = draft.kind === "secret_file" ? fileRef.current : null;
      const payload = variablePayload(draft, original, library?.limits.secret_min);
      let row = original?.id
        ? isSecret(draft) && isNoopUpdate(payload as SharedVariableUpdate, original) ? original : await api.updateVariable(original.id, payload as SharedVariableUpdate, original.updated_at)
        : await api.createVariable(payload as SharedVariableInput);
      // A secret's later steps chain on the newest revision. A step that fails leaves the editor
      // on the saved row, so Save again continues as an edit instead of creating it twice.
      try {
        const id = row.id;
        if (isSecret(draft) && id) {
          if (file) { row = await api.putVariableFile(id, file, draft.fileName, row.updated_at); if (fileRef.current === file) fileRef.current = null; }
          if (!original && draft.agentAccess) row = await api.updateVariable(id, { agent_access: true }, row.updated_at);
          if (JSON.stringify(draft.projects) !== JSON.stringify([...(original?.projects ?? [])].sort())) row = await api.setVariableProjects(id, draft.projects, row.updated_at);
        }
      } catch (e) {
        // A file already stored is no longer "ready to store".
        const stored = !!file && fileRef.current !== file;
        if (live.current) setEditor(current => current && current.kind === "variable" ? { ...current, original: row, draft: stored ? { ...current.draft, fileName: "", fileSize: 0 } : current.draft } : current);
        throw e;
      }
      if (!live.current) return;
      setLibrary(old => old && { ...old, variables: [...old.variables.filter(v => v.id !== row.id), row] });
      setSelected("all"); setNote(`Saved “${row.name}”.`);
    } else {
      const row = editor.original
        ? await api.updateVariableCollection(editor.original.id, editor.draft, editor.original.updated_at)
        : await api.createVariableCollection(editor.draft);
      if (!live.current) return;
      setLibrary(old => old && { ...old, collections: [...old.collections.filter(c => c.id !== row.id), row] });
      setSelected(row.id); setNote(`Saved collection “${row.name}”.`);
    }
    closeEditor(); await load();
  });
  const reloadEditor = () => perform(async () => {
    const next = await load();
    if (!next || !live.current || !editor?.original) return;
    const row = editor.kind === "variable" ? next.variables.find(v => v.id === editor.original?.id) : next.collections.find(c => c.id === editor.original?.id);
    if (!row) { setError(new Error("This item was deleted elsewhere. Your draft is still here; cancel to return to the library.")); return; }
    if (editor.kind === "variable") editVariable(row as SharedVariable);
    else editCollection(row as VariableCollection);
    setNote("Loaded the current saved version. Your unsaved draft was discarded.");
  });
  const remove = () => perform(async () => {
    if (!deleting || !deleting.row.id) return;
    if (deleting.kind === "variable") await api.deleteVariable(deleting.row.id, deleting.row.updated_at);
    else await api.deleteVariableCollection(deleting.row.id, deleting.row.updated_at);
    if (!live.current) return;
    setLibrary(old => old && (deleting.kind === "variable"
      ? { ...old, variables: old.variables.filter(v => v.id !== deleting.row.id), collections: old.collections.map(c => ({ ...c, variable_ids: c.variable_ids.filter(id => id !== deleting.row.id) })) }
      : { ...old, collections: old.collections.filter(c => c.id !== deleting.row.id) }));
    if (deleting.kind === "collection") setSelected("all");
    setNote(`Deleted ${deleting.kind} “${deleting.row.name}”.`); setDeleting(null); closeEditor(); await load();
  });

  const collections = library?.collections ?? [];
  const variables = library?.variables ?? [];
  const collection = collections.find(c => c.id === selected);
  const collected = new Set(collections.flatMap(c => c.variable_ids));
  const visible = variables.filter(v => (!collection || v.id !== null && collection.variable_ids.includes(v.id)) && (selected !== "uncollected" || !v.id || !collected.has(v.id)) && `${v.name} ${v.definition.label ?? ""} ${v.definition.help ?? ""}`.toLowerCase().includes(query.toLowerCase()));
  const locked = busy || editor !== null;
  const needsMigration = library?.migration_required ?? false;
  const conflict = error instanceof ApiError && error.status === 409 && !!(error.record as { current?: unknown } | undefined)?.current;
  const dependencies = error instanceof ApiError ? error.record as { dependants?: { id: string; name: string }[]; projects?: string[] } | undefined : undefined;
  const errorView = error ? <div ref={errorRef} className={styles.error} role="alert" tabIndex={-1}>
    {message(error)}
    {dependencies?.dependants?.map(t => <p key={t.id}>Template: <Link to={`/templates/${encodeURIComponent(t.id)}`}>{t.name}</Link></p>)}
    {dependencies?.projects?.map(id => <p key={id}>Project binding: {id}</p>)}
    {conflict && editor && <p><button type="button" className={buttons.ghost} disabled={busy} onClick={reloadEditor}>Discard draft & reload current</button></p>}
  </div> : null;

  return <div className={styles.page}>
    <header className={styles.head}>
      <div><div className={`nj-kicker ${styles.eyebrow}`}><span className="nj-notch" aria-hidden="true" />Library // <b>Shared inputs</b></div><h1 ref={titleRef} tabIndex={-1}>Variables & collections</h1></div>
      <div className={styles.actions}>
        <button className={buttons.ghost} disabled={locked || !library || needsMigration || variables.length >= library.limits.variables_max} onClick={() => editVariable(undefined, "secret")}><LockKeyhole size={14} aria-hidden="true" /> New secret</button>
        <button className={buttons.primary} disabled={locked || !library || needsMigration || variables.length >= library.limits.variables_max} onClick={() => editVariable()}><Plus size={14} aria-hidden="true" /> New variable</button>
      </div>
    </header>
    <p className={styles.notice}>Reusable definitions and optional global values. Collections organize variables; they do not grant access to secrets or run work. Secrets with <b>Agent access</b> can be requested by agents in the projects that bind them, through the BattleLab secrets tool.</p>
    <SecretChannels />
    {note && <p className={styles.status} role="status">{note}</p>}
    {!deleting && errorView}
    {!library && !error && <p className={styles.empty}>Loading variables…</p>}
    {!library && error !== null && <button className={buttons.ghost} disabled={busy} onClick={() => perform(async () => { await load(); })}>Retry</button>}
    {needsMigration && <div className={styles.notice}><p>Enable the shared library to organize existing variables into collections. Your values are kept, and a backup is retained.</p><button className={buttons.primary} disabled={busy} onClick={() => perform(async () => { await api.enableSharedVariables(); await load(); })}>Enable shared variables</button></div>}
    {library && <div className={styles.layout}>
      <aside className={styles.collections} aria-label="Variable collections">
        <h2>Collections</h2>
        <div className={styles.collectionList}>
          {[{ id: "all", name: "All variables", count: variables.length }, ...collections.map(c => ({ id: c.id, name: c.name, count: c.variable_ids.length })), { id: "uncollected", name: "Uncollected", count: variables.filter(v => !v.id || !collected.has(v.id)).length }].map(c => <button key={c.id} className={styles.collection} aria-pressed={selected === c.id} disabled={locked} onClick={() => setSelected(c.id)}><span>{c.name}</span><span className={styles.count}>{c.count}</span></button>)}
        </div>
        <button className={`${buttons.ghost} ${styles.newCollection}`} disabled={locked || needsMigration || collections.length >= library.limits.collections_max} onClick={() => editCollection()}><Plus size={14} aria-hidden="true" /> New collection</button>
      </aside>
      <section className={styles.content} aria-label={editor ? "Edit library item" : "Variables"}>
        {editor ? <form
          aria-label={`${editor.original ? "Edit" : "New"} ${editor.kind === "collection" ? "collection" : isSecret(editor.draft) ? "secret" : "variable"}`}
          onSubmit={e => { e.preventDefault(); void save(); }}
          onKeyDown={e => {
            if (e.nativeEvent.isComposing) return;
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              if (!busy && !needsMigration) e.currentTarget.requestSubmit();
            } else if (e.key === "Escape" && !busy && (e.target as HTMLElement).tagName !== "SELECT") {
              e.preventDefault();
              // The newly mounted confirmation listens on document: this same Escape
              // must not bubble up and immediately dismiss the dialog it just opened.
              e.stopPropagation();
              if (dirty) setDiscard(true); else closeEditor();
            }
          }}>
          <div className={styles.toolbar}><h2>{editor.original ? "Edit" : "New"} {editor.kind === "collection" ? "collection" : isSecret(editor.draft) ? "secret" : "variable"}</h2></div>
          {editor.kind === "variable" ? <VariableForm draft={editor.draft} editing={!!editor.original} original={editor.original} projects={projects} onFile={f => { fileRef.current = f; }} disabled={busy || needsMigration} onChange={draft => setEditor({ ...editor, draft })} /> : <fieldset disabled={busy || needsMigration} className={styles.fields}>
            <label className={styles.field}><span>Collection name</span><input autoFocus value={editor.draft.name} onChange={e => setEditor({ ...editor, draft: { ...editor.draft, name: e.target.value } })} /></label>
            <p className={styles.help}>Choose existing variables. A variable can belong to several collections.</p>
            {variables.length === 0 && <p className={styles.empty}>Create variables first, or save an empty collection.</p>}
            {variables.filter(v => v.id !== null).map(v => <label key={v.id} className={styles.check}><input type="checkbox" checked={editor.draft.variable_ids.includes(v.id!)} onChange={e => setEditor({ ...editor, draft: { ...editor.draft, variable_ids: e.target.checked ? [...editor.draft.variable_ids, v.id!] : editor.draft.variable_ids.filter(id => id !== v.id) } })} /> {v.name}{v.kind !== "text" ? " · secret" : ""}</label>)}
          </fieldset>}
          <div className={styles.actions}>
            <button type="submit" className={buttons.primary} disabled={busy || needsMigration}>{busy ? "Saving…" : "Save"}</button>
            <button type="button" className={buttons.ghost} disabled={busy} onClick={() => dirty ? setDiscard(true) : closeEditor()}>Cancel</button>
            {editor.original && <button type="button" className={buttons.ghost} disabled={busy || needsMigration} onClick={e => setDeleting(editor.kind === "variable" ? { kind: "variable", row: editor.original!, returnTo: e.currentTarget } : { kind: "collection", row: editor.original!, returnTo: e.currentTarget })}>Delete {editor.kind}</button>}
          </div>
        </form> : <>
          <div className={styles.toolbar}><h2>{collection?.name ?? (selected === "uncollected" ? "Uncollected" : "All variables")}</h2>{collection && <button className={buttons.ghost} disabled={busy} onClick={() => editCollection(collection)}>Edit collection</button>}</div>
          <label className={styles.search}><Search size={16} aria-hidden="true" /><input aria-label="Search variables" placeholder="Search names and descriptions" value={query} onChange={e => setQuery(e.target.value)} /></label>
          {visible.map(v => <article className={styles.row} key={v.id ?? v.name} aria-label={v.name}>
            <div><div className={styles.name}>{v.kind === "secret" && <LockKeyhole size={14} aria-hidden="true" />}{v.kind === "secret_file" && <FileKey size={14} aria-hidden="true" />}<span>{v.name}</span>{v.agent_access && <span className={styles.chip} title="Agents in the projects that bind it can request it">Agents</span>}</div><div className={styles.kind}>{v.kind === "secret" ? "Secret" : v.kind === "secret_file" ? <>Secret file{v.file ? <> · <span className={styles.fileCase}>{v.file.name}</span></> : null}</> : v.definition.value_type}{v.definition.label && v.definition.label !== v.name ? ` · ${v.definition.label}` : ""}</div></div>
            <div className={styles.value}>{v.needs_reentry ? "Needs re-entry" : !v.set ? "No global value" : v.kind === "secret_file" && v.file ? `Stored · hidden · ${formatSize(v.file.size)}` : v.kind !== "text" ? "Stored · hidden" : String(v.value)}</div>
            <button className={buttons.ghost} disabled={busy || !v.id || needsMigration} aria-label={`Edit ${v.name}`} onClick={() => editVariable(v)}>Edit</button>
          </article>)}
          {visible.length === 0 && <p className={styles.empty}>{query ? "No variables match this search." : collection ? "This collection is empty. Edit it to choose variables from the library." : "No variables here yet. Create a variable or a secret to get started."}</p>}
        </>}
      </section>
    </div>}
    {(discard || blocker.state === "blocked") && <ConfirmDialog tag="UNSAVED CHANGES" title="Discard your changes?" confirmLabel="Discard changes" busy={busy} returnFocusTo={document.activeElement as HTMLElement} onCancel={() => { setDiscard(false); if (blocker.state === "blocked") blocker.reset(); }} onConfirm={() => { closeEditor(); if (blocker.state === "blocked") blocker.proceed(); }}><p>Your unsaved draft will be lost.</p></ConfirmDialog>}
    {deleting && <ConfirmDialog tag={`DELETE ${deleting.kind.toUpperCase()}`} title={`Delete “${deleting.row.name}”?`} confirmLabel={`Delete ${deleting.kind}`} danger busy={busy} returnFocusTo={deleting.returnTo} onCancel={() => { setDeleting(null); setError(null); }} onConfirm={remove}><p>{deleting.kind === "collection" ? "Its variables and stored values stay in the library." : "This removes the definition and stored value. Existing uses must be unbound first."}</p>{errorView}</ConfirmDialog>}
  </div>;
}
