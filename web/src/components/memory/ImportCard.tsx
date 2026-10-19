import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import { relTime } from "../../lib/format";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import type { ImportPreview, ImportResult, ImportSource } from "../../types/memory";
import { heldFor, labelsOf, MAX_SOURCES, message, plural, resultLine } from "./importResult";
import buttons from "../ui/actionButton.module.css";
import styles from "./importCard.module.css";

function sourceMeta(s: ImportSource): string {
  const bits = [plural(s.files, "note")];
  if (s.over_agent_size) bits.push(`${s.over_agent_size} longer than an agent note (fine up to 32 KiB)`);
  if (s.over_cap) bits.push(`${s.over_cap} over 32 KiB, refused`);
  if (s.beyond_max_files) bits.push(`${s.beyond_max_files} beyond the first 1000, not read`);
  return bits.join(" · ");
}

interface Props {
  pid: string;
  projectName: string;
  /** The page is mid-action: importing waits. */
  disabled?: boolean;
  /** The import landed: refresh what the page shows for THIS project. */
  onImported: (pid: string) => Promise<void>;
}

/** Existing memory (#1471): what agents already keep for this project's folders, imported on the
 *  operator's say-so. Read only — the agent's own files are never changed. Mount it with
 *  `key={pid}`: everything here belongs to one project. */
export function ImportCard({ pid, projectName, disabled = false, onImported }: Props) {
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [open, setOpen] = useState<HTMLElement | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // The import this card ran, shown with what is held back NOW (the preview reloads after it).
  const [ran, setRan] = useState<ImportResult | null>(null);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  // Every answer is applied only while the card still shows the project it was asked for, so a
  // late one never lands under another project — even if this instance is reused across projects.
  const shown = useRef(pid);
  useEffect(() => { shown.current = pid; }, [pid]);
  const mine = (asked: string) => live.current && shown.current === asked;
  const [shownPid, setShownPid] = useState(pid);
  if (pid !== shownPid) {
    setShownPid(pid);
    setPreview(null); setLoadError(null); setOpen(null); setRan(null); setError(null);
  }

  // The preview for THIS project; `reloads` asks again after an import. A superseded answer (the
  // project changed, or a newer read began) is dropped.
  const [reloads, setReloads] = useState(0);
  useEffect(() => {
    let stale = false;
    api.memoryImportPreview(pid)
      .then(next => { if (!stale && live.current) { setPreview(next); setLoadError(null); } })
      .catch(e => { if (!stale && live.current) setLoadError(e); });
    return () => { stale = true; };
  }, [pid, reloads]);

  const ok = preview?.sources.filter(s => s.state === "ok") ?? [];
  const unreadable = preview?.sources.filter(s => s.state !== "ok") ?? [];
  const found = ok.reduce((a, s) => a + s.files, 0);
  const selected = ok.filter(s => chosen.has(s.id));
  const count = selected.reduce((a, s) => a + s.files, 0);

  const openDialog = (from: HTMLElement) => {
    // A fresh choice each time: "this folder" is chosen, a subfolder's store is the operator's call.
    setChosen(new Set(ok.filter(s => s.relation === "this folder").slice(0, MAX_SOURCES).map(s => s.id)));
    setError(null);
    setOpen(from);
  };
  const toggle = (id: string) => setChosen(c => {
    const next = new Set(c);
    if (next.has(id)) next.delete(id);
    else if (next.size < MAX_SOURCES) next.add(id);
    return next;
  });
  const run = async () => {
    const asked = pid;
    setImporting(true); setError(null);
    try {
      const result = await api.importMemory(asked, selected.map(s => s.id));
      // The project it was about is refreshed whatever is shown now; the card only speaks for it.
      const refreshed = onImported(asked);
      if (mine(asked)) {
        setOpen(null);
        setRan(result);
        setReloads(n => n + 1);
      }
      await refreshed;
    } catch (e) {
      if (mine(asked)) setError(e);
    } finally {
      if (live.current) setImporting(false);
    }
  };

  const held = preview?.held ?? [];
  const last = preview?.last ?? null;
  return (
    <section className={styles.card} aria-label="Existing memory">
      <h2 className={styles.eyebrow}>Existing memory</h2>
      {preview === null && !loadError && <p className={styles.small}>Looking for existing agent memory…</p>}
      {loadError !== null && <p className={styles.warn} role="alert">Couldn't look for existing memory: {message(loadError)}</p>}
      {preview && (
        <>
          <p className={styles.muted}>
            {found > 0
              ? <>{labelsOf(ok)} keeps <b>{plural(found, "note")}</b> in {plural(ok.length, "place")} for this project's folders.</>
              : "No agent memory found for this project's folders."}
            {unreadable.length > 0 && ` ${plural(unreadable.length, "place")} couldn't be read.`}
          </p>
          {ran
            ? <p className={styles.result} role="status">{resultLine(ran, heldFor(ran, held))}</p>
            : last && <p className={styles.small}>Last import {relTime(last.at)} — {resultLine(last, heldFor(last, held))}</p>}
          {(ok.length > 0 || unreadable.length > 0) && (
            <button type="button" className={buttons.ghost} disabled={disabled || importing} onClick={e => openDialog(e.currentTarget)}>
              {last ? "Import again…" : "Import existing memory…"}
            </button>
          )}
          {held.length > 0 && (
            <details className={styles.held}>
              <summary>Held back from import · {held.length}</summary>
              <p className={styles.small}>Not taken in, and none of their text is kept here. Fix them in {labelsOf(ok)}'s memory, then import again.</p>
              <ul>
                {held.map(h => (
                  <li key={`${h.source}/${h.file}`}>
                    <code>{h.file}</code>
                    <span className={h.refused ? styles.danger : styles.warn}>{h.refused ? "refused: " : ""}{h.reason}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}
          <p className={styles.small}>Read only — BattleLab never changes an agent's own memory, and agents can keep writing to it.</p>
        </>
      )}
      {open && preview && (
        <ConfirmDialog
          tag="IMPORT"
          title={`Import existing memory — ${projectName}`}
          confirmLabel={count ? `Import ${plural(count, "note")}` : "Import"}
          confirmDisabled={selected.length === 0 || selected.length > MAX_SOURCES}
          busy={importing}
          returnFocusTo={open}
          onCancel={() => { setOpen(null); setError(null); }}
          onConfirm={run}
        >
          <p>Found for this project's folders. Importing copies notes into shared memory; the originals are read, never changed.</p>
          <fieldset className={styles.sources}>
            <legend className="sr-only">Where to import from</legend>
            {preview.sources.map(s => (
              <label key={s.id} className={styles.source} data-state={s.state}>
                <input
                  type="checkbox"
                  checked={chosen.has(s.id)}
                  disabled={s.state !== "ok" || importing || (!chosen.has(s.id) && chosen.size >= MAX_SOURCES)}
                  onChange={() => toggle(s.id)}
                />
                <span className={styles.sourceText}>
                  <span className={styles.sourceName}>{s.label} · {s.relation || "can't be imported"}</span>
                  <code className={styles.path}>{s.path}</code>
                  {s.state === "ok" ? <span className={styles.small}>{sourceMeta(s)}</span> : <span className={styles.warn}>{s.reason}</span>}
                </span>
              </label>
            ))}
          </fieldset>
          {ok.length > MAX_SOURCES && (
            <p className={styles.small} role="note">One import takes at most {MAX_SOURCES} places — import the rest in a second run.</p>
          )}
          <p className={styles.small}>
            Every note passes the same check as an agent's: anything that holds a stored secret or looks like a credential is held back — fix it in the agent's memory and import again. A note whose name is taken here is added beside it. Importing again takes in only what changed; notes you edited or deleted here stay as you left them.
          </p>
          {error !== null && <div className={styles.error} role="alert">{message(error)}</div>}
        </ConfirmDialog>
      )}
    </section>
  );
}
