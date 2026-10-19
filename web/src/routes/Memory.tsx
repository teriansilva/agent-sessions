import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowLeft, Copy, Download, Search } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { engineName, useEngineRoster } from "../app/engineRoster";
import { relTime, sessionPathFromKey } from "../lib/format";
import { useIsMobile } from "../lib/useIsMobile";
import { useConfig } from "../app/config";
import type { MemoryNote, MemoryProject, MemorySession, MemoryView, NoteEdit, NoteType } from "../types/memory";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { ImportCard } from "../components/memory/ImportCard";
import { linkedFrom, TYPE_ORDER, TYPE_TAG } from "../components/memory/memoryGraph";
import { MEMORY_GUIDE_URL } from "../lib/links";
import buttons from "../components/ui/actionButton.module.css";
import styles from "./Memory.module.css";

const MemoryGraph = lazy(() => import("../components/memory/MemoryGraph"));
/** How often an open page takes in new notes — the same period as the server's own loop. */
const REFRESH_MS = 30_000;

type Mode = "graph" | "list" | "trays";
// A confirmation belongs to the project it was opened for: it carries that id, acts on it, and is
// dropped the moment another project is selected (browser history included).
type Confirm = { pid: string; returnTo: HTMLElement } & (
  | { kind: "clear" }
  | { kind: "delete"; note: MemoryNote }
  | { kind: "drop"; id: number; filename: string }
);
type Draft = { slug: string; name: string; description: string; type: NoteType; body: string };

/** Notes are at most a few KiB, so "0.0 MB" says nothing: bytes, then KiB. */
const noteSize = (n: number) => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KiB`);
const message = (e: unknown) => (e instanceof Error ? e.message : "The request failed. Try again.");

function Writer({ session }: { session: MemorySession }) {
  const name = session.engine ? engineName(session.engine) : "unknown agent";
  const to = session.key ? sessionPathFromKey(session.key) : null;
  const label = session.kind === "import"
    ? `${name} · imported`  // taken in from the agent's own memory (#1471): no session wrote it here
    : `${name} · ${session.kind || "agent"}${session.resolved ? "" : " · session unresolved"}`;
  return to ? <Link to={to} className={styles.writer}>{label}</Link> : <span className={styles.writer}>{label}</span>;
}

/** Library → Memory (#1441): each project's shared notebook — what agent sessions left for each
 *  other, who wrote it, and the trays for what was not taken in. Attribution is a label, and
 *  BattleLab cannot see who READ a note, so nothing here claims it. */
export default function Memory() {
  useEngineRoster();
  const mobile = useIsMobile();
  const [params, setParams] = useSearchParams();
  const [projects, setProjects] = useState<MemoryProject[] | null>(null);
  // One slot per project: a response always lands in ITS project's slot, so a late answer for one
  // project can never overwrite another's — whatever the order in which they settle.
  const [views, setViews] = useState<Record<string, MemoryView>>({});
  const [error, setError] = useState<unknown>(null);
  const [status, setStatusFor] = useState<{ pid: string | null; text: string }>({ pid: null, text: "" });
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<Mode>(mobile ? "list" : "graph");
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  // The draft a Save in flight submitted: its editor is locked until the Save settles; any other
  // editor stays usable.
  const [saving, setSaving] = useState<Draft | null>(null);
  const [release, setRelease] = useState<{ id: number; text: string } | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [typed, setTyped] = useState("");
  const [typeFilter, setTypeFilter] = useState<NoteType | "">("");
  const [engineFilter, setEngineFilter] = useState("");
  const live = useRef(true);
  const loadGeneration = useRef(new Map<string, number>());
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);

  const pid = params.get("project") ?? projects?.find(p => p.enabled)?.id ?? projects?.[0]?.id ?? null;
  const view = pid ? views[pid] ?? null : null;
  // A message belongs to the project it is about; it is shown only while that project is selected.
  const setStatus = (text: string, about: string | null = pid) => setStatusFor({ pid: about, text });
  const statusText = status.pid === null || status.pid === pid ? status.text : "";
  // Trays are their own tab only on a phone; on a desktop they sit under the graph or list.
  const shown: Mode = !mobile && mode === "trays" ? "graph" : mode;
  // A different project starts clean: reset during render (not in an effect) when the id changes.
  const [shownPid, setShownPid] = useState(pid);
  if (pid !== shownPid) {
    setShownPid(pid);
    setSelected(null); setDraft(null); setRelease(null); setConfirm(null); setTyped(""); setEngineFilter("");
  }

  const loadProjects = useCallback(async () => {
    const next = await api.memoryProjects();
    if (live.current) setProjects(next.projects);
  }, []);
  const loadView = useCallback(async (id: string) => {
    const generation = (loadGeneration.current.get(id) ?? 0) + 1;
    loadGeneration.current.set(id, generation);
    const next = await api.memory(id);
    // The newest load of THIS project wins; other projects' slots are never touched.
    if (live.current && loadGeneration.current.get(id) === generation) setViews(v => ({ ...v, [id]: next }));
  }, []);
  useEffect(() => { loadProjects().catch(e => live.current && setError(e)); }, [loadProjects]);
  useEffect(() => {
    if (pid) loadView(pid).catch(e => live.current && setError(e));
  }, [pid, loadView]);
  // Take in what agents left since the last pass, so the page is never 30 s stale. The pass is a
  // CSRF-guarded POST, so it waits for the config read that primes the token — fired at mount it
  // was refused 403 before the token existed. A refused pass (e.g. the secret check is
  // unavailable, 503) keeps the inbox files and says so; the view stays what it was.
  // An open page keeps taking in what agents leave: the same pass every REFRESH_MS while it is
  // visible (the server's own loop runs on the same period).
  const ready = useConfig() !== null;
  useEffect(() => {
    if (!pid || !ready) return;
    let stale = false;
    const tick = () => {
      if (document.visibilityState === "hidden") return;
      api.ingestMemory(pid)
        .then(() => { if (!stale) return loadView(pid); })
        .catch(e => { if (!stale && live.current) setStatusFor({ pid, text: e instanceof ApiError ? e.message : "Couldn't take in new notes." }); });
    };
    tick();
    const timer = window.setInterval(tick, REFRESH_MS);
    return () => { stale = true; window.clearInterval(timer); };
  }, [pid, ready, loadView]);

  const perform = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(null); setStatus("");
    try { await action(); } catch (e) { if (live.current) setError(e); }
    finally { if (live.current) setBusy(false); }
  };
  // A follow-up refreshes the project the action was ABOUT, never whichever is selected by then.
  const refresh = async (id: string) => { await loadView(id); await loadProjects(); };

  const notes = useMemo(() => view?.notes ?? [], [view]);
  const engines = useMemo(() => [...new Set(notes.map(n => n.session.engine).filter(Boolean))].sort(), [notes]);
  // A filter for an agent this project has no notes from is no filter: the select could not show it.
  const agent = engines.includes(engineFilter) ? engineFilter : "";
  const visible = useMemo(() => {
    const q = query.toLowerCase();
    return notes.filter(n =>
      (!typeFilter || n.type === typeFilter)
      && (!agent || n.session.engine === agent)
      && `${n.name} ${n.description} ${n.body} ${n.type}`.toLowerCase().includes(q));
  }, [notes, query, typeFilter, agent]);
  const current = notes.find(n => n.slug === selected) ?? null;
  const project = projects?.find(p => p.id === pid) ?? null;
  const trays = (view?.pending.length ?? 0) + (view?.refused.length ?? 0) + (view?.quarantine.length ?? 0);

  const toggle = () => perform(async () => {
    if (!pid || !view) return;
    const id = pid;
    await api.setMemoryEnabled(id, !view.enabled);
    await refresh(id);
    setStatus(view.enabled ? "Memory is off. New sessions won't get it; running ones keep their paths." : "Memory is on. Sessions already running join after a restart.", id);
  });
  const copy = async () => {
    if (!view) return;
    try { await navigator.clipboard.writeText(view.instructions); setStatus("Copied — paste it into the project's CLAUDE.md, AGENTS.md or GEMINI.md."); }
    catch { setStatus("Copy failed — select the paragraph and copy it by hand."); }
  };
  const save = () => perform(async () => {
    if (!pid || !draft) return;
    const id = pid;
    const submitted = draft;
    setSaving(submitted);
    const original = notes.find(n => n.slug === draft.slug);
    const edit: NoteEdit = {};
    for (const k of ["name", "description", "type", "body"] as const) if (original?.[k] !== draft[k]) (edit as Record<string, string>)[k] = draft[k];
    try {
      if (Object.keys(edit).length) await api.editMemoryNote(id, submitted.slug, edit);
    } finally {
      if (live.current) setSaving(null);
    }
    // Close only the editor this Save came from: by the time it settles, another note's (or another
    // project's) editor may be open with edits of its own.
    setDraft(d => (d === submitted ? null : d)); await refresh(id); setStatus(`Saved “${submitted.name}”.`, id);
  });
  const doConfirm = () => perform(async () => {
    if (!confirm || confirm.pid !== pid) return; // never act on a project other than the dialog's
    const id = confirm.pid;
    if (confirm.kind === "clear") { await api.clearMemory(id); setSelected(null); setStatus("Memory cleared.", id); }
    if (confirm.kind === "delete") { await api.deleteMemoryNote(id, confirm.note.slug); setSelected(null); setStatus(`Deleted “${confirm.note.name}”.`, id); }
    if (confirm.kind === "drop") { await api.dropQuarantined(id, confirm.id); setStatus(`Dropped ${confirm.filename}.`, id); }
    setConfirm(null); setTyped(""); await refresh(id);
  });
  const takeIn = () => perform(async () => {
    if (!pid || !release) return;
    const id = pid;
    const { slug } = await api.releaseQuarantined(id, release.id, release.text);
    setRelease(null); await refresh(id); setSelected(slug); setStatus("Taken in as a note.", id);
  });

  const errorView = error ? <div className={styles.error} role="alert">{message(error)}</div> : null;


  // The selected project's bar: what it is, whether it's on, and what can be done to it — over the
  // graph, so the switch and Clear are next to the notes they act on, not in the project rail.
  const projectBar = view && project && (
    <div className={styles.projectBar} aria-label={`Project ${project.name}`} role="group">
      <div className={styles.projectMain}>
        <div className={styles.projectTitle}>
          {!mobile && <>
            <span className={styles.swatch} style={{ background: project.color || "var(--engine-slate)" }} aria-hidden="true" />
            <h2>{project.name}</h2>
            <span className={styles.meta}>{notes.length} / {view.limits.notes} notes</span>
          </>}
          <button type="button" role="switch" aria-checked={view.enabled} aria-label={`Shared memory for ${project.name}`} className={styles.switch} disabled={busy} onClick={toggle}>
            <span className={styles.knob} aria-hidden="true" />
            <span className={styles.switchLabel}>{view.enabled ? "ON" : "OFF"}</span>
          </button>
        </div>
        <span className={styles.hint}>{view.enabled ? "Sessions already running join after a restart." : "Off by default. Turning it on reaches new sessions only."}</span>
      </div>
      <div className={styles.barActions}>
        {!mobile && <div className={styles.seg} role="group" aria-label="View">
          <button type="button" aria-pressed={shown === "graph"} onClick={() => setMode("graph")}>Graph</button>
          <button type="button" aria-pressed={shown === "list"} onClick={() => setMode("list")}>List</button>
        </div>}
        <a className={buttons.ghost} href={api.memoryExportUrl(view.project.id)} download><Download size={14} aria-hidden="true" /> Export</a>
        <button type="button" className={`${buttons.ghost} ${styles.dangerButton}`} disabled={busy} onClick={e => pid && setConfirm({ kind: "clear", pid, returnTo: e.currentTarget })}>Clear…</button>
      </div>
    </div>
  );

  // Existing memory (#1471): one card per project — `key` drops everything it held when the
  // project changes, so a late answer for one project never shows under another.
  const importCard = view && pid && (
    <ImportCard key={pid} pid={pid} projectName={view.project.name} disabled={busy} onImported={refresh} />
  );

  const instructions = view && (
    <section className={styles.instructions} aria-label="Agent instructions">
      <h2 className={styles.eyebrow}>Agent instructions</h2>
      <p className={styles.muted}>Paste once into the project's <code>CLAUDE.md</code> / <code>AGENTS.md</code> / <code>GEMINI.md</code>. BattleLab never edits your files.</p>
      <pre className={styles.paragraph}>{view.instructions}</pre>
      <button type="button" className={buttons.ghost} onClick={copy}><Copy size={14} aria-hidden="true" /> Copy paragraph</button>
      <p className={styles.small}>Reaches new agent processes only — reconnecting doesn't update a running agent, and turning memory off doesn't revoke access from processes already running.</p>
      <a className={styles.guideLink} href={MEMORY_GUIDE_URL} target="_blank" rel="noopener noreferrer">Which agents can reach it</a>
    </section>
  );

  const emptyText = query || typeFilter || agent ? "No notes match." : "No notes yet. Agents add them by writing into their inbox.";
  const list = (
    <section className={styles.list} aria-label="Notes">
      <label className={styles.search}><Search size={16} aria-hidden="true" /><input aria-label="Search notes" placeholder="Search notes" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <div className={styles.filters}>
        <select aria-label="Filter by type" value={typeFilter} onChange={e => setTypeFilter(e.target.value as NoteType | "")}>
          <option value="">All types</option>
          {TYPE_ORDER.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select aria-label="Filter by agent" value={agent} onChange={e => setEngineFilter(e.target.value)}>
          <option value="">All agents</option>
          {engines.map(e => <option key={e} value={e}>{engineName(e)}</option>)}
        </select>
      </div>
      {visible.map(n => (
        <button key={n.slug} type="button" className={styles.row} aria-pressed={n.slug === selected} onClick={() => { setSelected(n.slug); setDraft(null); }}>
          <span className={styles.tag} data-type={n.type}>{TYPE_TAG[n.type]}</span>
          <span className={styles.rowText}>
            <span className={styles.rowName}>{n.name}</span>
            <span className={styles.meta}>{n.session.engine ? engineName(n.session.engine) : "unknown"} · {n.session.kind === "import" ? "imported" : `${n.session.kind || "agent"}${n.session.resolved ? "" : " · unresolved"}`} · {relTime(n.updated)}</span>
          </span>
        </button>
      ))}
      {visible.length === 0 && <p className={styles.empty}>{emptyText}</p>}
    </section>
  );

  const traysView = view && (
    <section className={styles.trays} aria-label="Trays">
      <div className={styles.tray}>
        <h2 className={styles.eyebrow}>Pending · {view.pending.length}</h2>
        {view.pending.map((p, i) => <div key={`p${i}`} className={styles.trayItem}><Writer session={p.session} /><code>{p.filename}</code><span className={styles.meta}>{noteSize(p.size)}{view.enabled ? "" : " · memory off"}</span></div>)}
        {view.pending.length === 0 && <p className={styles.small}>Nothing waiting.</p>}
      </div>
      <div className={styles.tray}>
        <h2 className={styles.eyebrow}>Refused · {view.refused.length}</h2>
        {view.refused.map((r, i) => <div key={`r${i}`} className={styles.trayItem}><Writer session={r.session} /><code>{r.filename}</code><span className={styles.danger}>{r.reason}</span></div>)}
        {view.refused.length === 0 && <p className={styles.small}>Nothing refused.</p>}
      </div>
      <div className={styles.tray}>
        <h2 className={styles.eyebrow}>Quarantine · {view.quarantine.length}</h2>
        <p className={styles.small}>Held back because it looks like a credential. Never shown to agents.</p>
        {view.quarantine.map(q => (
          <div key={q.id} className={styles.quarantined} aria-label={`Quarantined ${q.filename}`}>
            <div className={styles.trayItem}><Writer session={q.session} /><code>{q.filename}</code><span className={styles.warn}>{q.reason}</span></div>
            {release?.id === q.id ? (
              <form onSubmit={e => { e.preventDefault(); void takeIn(); }}>
                <label className={styles.field}><span>Write the note again without the withheld value</span><textarea rows={6} value={release.text} placeholder="# Title&#10;&#10;What the note should say" onChange={e => setRelease({ id: q.id, text: e.target.value })} /></label>
                <div className={styles.actions}><button type="submit" className={buttons.primary} disabled={busy || !release.text.trim()}>Take in</button><button type="button" className={buttons.ghost} onClick={() => setRelease(null)}>Cancel</button></div>
              </form>
            ) : (
              <>
                <pre className={styles.masked}>{q.masked}</pre>
                <div className={styles.actions}>
                  <button type="button" className={buttons.ghost} disabled={busy} onClick={() => setRelease({ id: q.id, text: "" })}>Rewrite</button>
                  <button type="button" className={`${buttons.ghost} ${styles.dangerButton}`} disabled={busy} onClick={e => pid && setConfirm({ kind: "drop", pid, id: q.id, filename: q.filename, returnTo: e.currentTarget })}>Delete</button>
                </div>
              </>
            )}
          </div>
        ))}
      </div>
    </section>
  );

  const inspector = current && (
    <aside className={styles.inspector} aria-label={`Note: ${current.name}`}>
      {mobile && <button type="button" className={buttons.ghost} onClick={() => { setSelected(null); setDraft(null); }}><ArrowLeft size={14} aria-hidden="true" /> Back to notes</button>}
      {draft ? (
        <form aria-label="Edit note" onSubmit={e => { e.preventDefault(); void save(); }}>
          <label className={styles.field}><span>Name</span><input disabled={saving === draft} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
          <label className={styles.field}><span>Type</span><select disabled={saving === draft} value={draft.type} onChange={e => setDraft({ ...draft, type: e.target.value as NoteType })}>{TYPE_ORDER.map(t => <option key={t} value={t}>{t}</option>)}</select></label>
          <label className={styles.field}><span>Description</span><input disabled={saving === draft} value={draft.description} onChange={e => setDraft({ ...draft, description: e.target.value })} /></label>
          <label className={styles.field}><span>Body</span><textarea disabled={saving === draft} rows={8} value={draft.body} onChange={e => setDraft({ ...draft, body: e.target.value })} /></label>
          <div className={styles.actions}><button type="submit" className={buttons.primary} disabled={busy}>{busy ? "Saving…" : "Save"}</button><button type="button" className={buttons.ghost} disabled={busy} onClick={() => setDraft(null)}>Cancel</button></div>
        </form>
      ) : (
        <>
          <div className={styles.inspectorHead}><span className={styles.tag} data-type={current.type}>{current.type.toUpperCase()}</span><code className={styles.meta}>notes/{current.slug}.md</code></div>
          <h2 className={styles.noteTitle}>{current.name}</h2>
          <dl className={styles.kv}>
            <dt>Written by</dt><dd><Writer session={current.session} /></dd>
            <dt>When</dt><dd>{new Date(current.updated * 1000).toLocaleString()}</dd>
            <dt>Size</dt><dd>{noteSize(current.size)} / {noteSize(view?.limits.note_bytes ?? 4096)}</dd>
          </dl>
          <pre className={styles.body}>{current.body}</pre>
          <h3 className={styles.eyebrow}>Links to</h3>
          {current.links.length ? current.links.map(s => <button key={s} type="button" className={styles.linkButton} onClick={() => setSelected(s)}>→ {notes.find(n => n.slug === s)?.name ?? s}</button>) : <p className={styles.small}>none</p>}
          <h3 className={styles.eyebrow}>Linked from</h3>
          {linkedFrom(notes, current.slug).map(n => <button key={n.slug} type="button" className={styles.linkButton} onClick={() => setSelected(n.slug)}>← {n.name}</button>)}
          {linkedFrom(notes, current.slug).length === 0 && <p className={styles.small}>none</p>}
          <div className={styles.actions}>
            <button type="button" className={buttons.ghost} onClick={() => setDraft({ slug: current.slug, name: current.name, description: current.description, type: current.type, body: current.body })}>Edit</button>
            <button type="button" className={`${buttons.ghost} ${styles.dangerButton}`} disabled={busy} onClick={e => pid && setConfirm({ kind: "delete", pid, note: current, returnTo: e.currentTarget })}>Delete</button>
          </div>
        </>
      )}
    </aside>
  );

  const offState = view && !view.enabled && notes.length === 0 && (
    <section className={styles.off} aria-label="Memory is off">
      <h2>Shared memory is off for this project</h2>
      <p className={styles.muted}>Turn it on and every new session in this project — terminal or API, any agent — gets two environment variables: <code>BATTLELAB_MEMORY</code> (the notes to read) and <code>BATTLELAB_MEMORY_INBOX</code> (where it leaves new ones). Agents use them once your project's instruction file says so.</p>
      <div className={styles.actions}><button type="button" className={buttons.primary} disabled={busy} onClick={toggle}>Turn memory on</button></div>
      <p className={styles.small}>Notes stay outside your repo, in BattleLab's config folder. Anything that looks like a credential is held back for you to review. Any agent in the project can read what another wrote — it's a shared notebook, so only turn it on where you want that.</p>
    </section>
  );

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div><div className={styles.eyebrow}>Library / Shared memory</div><h1>Memory</h1></div>
      </header>
      {statusText && <p className={styles.status} role="status">{statusText}</p>}
      {view && view.withheld.notes > 0 && (
        <p className={styles.status} role="note">
          {view.withheld.notes} {view.withheld.notes === 1 ? "note is" : "notes are"} not shown because {view.withheld.reason}.
        </p>
      )}
      {!confirm && errorView}
      {projects === null && !error && <p className={styles.empty}>Loading memory…</p>}
      {projects?.length === 0 && <p className={styles.empty}>No projects yet. Memory belongs to a project — create one from New session or Settings → Projects, then turn its memory on here.</p>}
      {projects && projects.length > 0 && (
        <div className={styles.layout} data-mobile={mobile}>
          <nav className={styles.rail} aria-label="Projects">
            {mobile ? (
              <label className={styles.field}><span>Project</span>
                <select value={pid ?? ""} disabled={busy} onChange={e => setParams({ project: e.target.value })}>
                  {projects.map(p => <option key={p.id} value={p.id}>{p.name}{p.enabled ? ` · ${p.notes}` : " · off"}</option>)}
                </select>
              </label>
            ) : <>
              <h2 className={styles.eyebrow}>Projects</h2>
              {projects.map(p => (
              <button key={p.id} type="button" className={styles.project} aria-pressed={p.id === pid} disabled={busy} onClick={() => setParams({ project: p.id })}>
                <span className={styles.swatch} style={{ background: p.color || "var(--engine-slate)" }} aria-hidden="true" />
                <span className={styles.projectName}>{p.name}</span>
                <span className={styles.count}>{p.enabled ? p.notes : "—"}</span>
                <span className={styles.onoff} data-on={p.enabled}>{p.enabled ? "ON" : "OFF"}</span>
              </button>
              ))}
            </>}
          </nav>
          <main className={styles.main}>
            {!view && !error && <p className={styles.empty}>Loading notes…</p>}
            {projectBar}
            {view && (offState ? (
              <>
                {offState}
                {/* The desktop side column carries the paragraph; on a phone the off state must, too —
                    it's what the operator pastes before turning memory on. */}
                {mobile && importCard}
                {mobile && instructions}
                {/* Turning memory off keeps what was held: it must stay inspectable and droppable. */}
                {trays > 0 && traysView}
              </>
            ) : (
              <>
                {mobile && !current && (
                  <div className={styles.tabs} role="tablist" aria-label="Memory view">
                    {(["list", "graph", "trays"] as const).map(m => (
                      <button key={m} type="button" role="tab" aria-selected={shown === m} onClick={() => setMode(m)}>
                        {m === "list" ? `List · ${notes.length}` : m === "trays" ? `Trays · ${trays}` : "Graph"}
                      </button>
                    ))}
                  </div>
                )}
                {mobile && current ? inspector : (
                  <>
                    {shown === "graph" && visible.length === 0 && <div className={styles.graphEmpty}><p>{emptyText}</p></div>}
                    {shown === "graph" && visible.length > 0 && (
                      <div className={styles.graph}>
                        <Suspense fallback={<p className={styles.empty}>Loading graph…</p>}>
                          <MemoryGraph notes={visible} selected={selected} onSelect={s => { setSelected(s); setDraft(null); }} />
                        </Suspense>
                      </div>
                    )}
                    {shown === "list" && list}
                    {(shown === "trays" || !mobile) && traysView}
                    {mobile && shown === "trays" && importCard}
                    {mobile && shown === "trays" && instructions}
                  </>
                )}
              </>
            ))}
          </main>
          {!mobile && view && (
            <div className={styles.side}>
              {!offState && (inspector ?? <p className={styles.placeholder}>Select a note to see who wrote it and what it links to.</p>)}
              {importCard}
              {instructions}
            </div>
          )}
        </div>
      )}
      {confirm && (
        <ConfirmDialog
          tag={confirm.kind === "clear" ? "CLEAR MEMORY" : confirm.kind === "delete" ? "DELETE NOTE" : "DELETE QUARANTINED"}
          title={confirm.kind === "clear" ? `Clear memory — ${view?.project.name ?? ""}?` : confirm.kind === "delete" ? `Delete “${confirm.note.name}”?` : `Delete ${confirm.filename}?`}
          confirmLabel={confirm.kind === "clear" ? "Clear memory" : "Delete"}
          danger
          busy={busy}
          returnFocusTo={confirm.returnTo}
          confirmDisabled={confirm.kind === "clear" && typed.trim() !== (view?.project.name ?? "")}
          onCancel={() => { setConfirm(null); setTyped(""); setError(null); }}
          onConfirm={doConfirm}
        >
          {confirm.kind === "clear" && view && <>
            <p>Deletes all {view.notes.length} notes, drops {view.pending.length} pending inbox files and {view.quarantine.length} quarantined items. Running sessions keep their paths; nothing old comes back. This can't be undone — <a href={api.memoryExportUrl(view.project.id)} download>export first</a> if you want a copy.</p>
            <label className={styles.field}><span>Type <b>{view.project.name}</b> to confirm</span><input value={typed} onChange={e => setTyped(e.target.value)} autoComplete="off" /></label>
          </>}
          {confirm.kind === "delete" && <p>The note is removed from memory. A retry of the same file never brings it back.</p>}
          {confirm.kind === "drop" && <p>The held-back file is discarded and never taken in.</p>}
          {errorView}
        </ConfirmDialog>
      )}
    </div>
  );
}
