import {
  Archive,
  ArchiveRestore,
  Check,
  Palette,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { shortCwd } from "../lib/format";
import type { Folder, ProjectArchiveReport, ProjectEntity } from "../types/api";
import settings from "./Settings.module.css";
import styles from "./ProjectsManager.module.css";

/** Preset entity colors (#361): a deliberately small set — the color is a glanceable
 *  sidebar marker, not a theming surface. "Clear" maps to the store's `color: ""`. */
const COLOR_PRESETS = ["#ffb000", "#5fd7ff", "#7ee787", "#c792ea", "#ff7a7a", "#e8e8e8"];

/** "1 archived · 2 already archived · 1 failed" from the bulk report's counts —
 *  the keys mirror the direction, so the same formatter serves both endpoints. */
function formatCounts(counts: Record<string, number>): string {
  return Object.entries(counts)
    .map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`)
    .join(" · ");
}

function errMessage(e: unknown, fallback: string): string {
  // Folder-adoption conflicts (409) carry the server's detail string (which project
  // owns the folder) — show it verbatim instead of a generic failure line.
  return e instanceof ApiError && e.message ? e.message : fallback;
}

interface RowProps {
  entity: ProjectEntity;
  /** Folders not adopted by ANY project — the only legal adoption targets (409 otherwise). */
  adoptable: string[];
  onChanged: () => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (entity: ProjectEntity) => Promise<void>;
}

function EntityRow({ entity, adoptable, onChanged, onArchive, onDelete }: RowProps) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(entity.name);
  const [showColors, setShowColors] = useState(false);
  const [folderSel, setFolderSel] = useState("");
  const [busy, setBusy] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);

  const patch = async (body: { name?: string; color?: string; folders?: string[] }) => {
    setBusy(true);
    setRowError(null);
    try {
      await api.patchProject(entity.id, body);
      await onChanged();
      return true;
    } catch (e) {
      setRowError(errMessage(e, "Couldn’t update the project."));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const commitRename = async () => {
    const name = draft.trim();
    if (!name || name === entity.name) {
      setRenaming(false);
      return;
    }
    if (await patch({ name })) setRenaming(false);
  };

  const addFolder = async () => {
    if (!folderSel) return;
    if (await patch({ folders: [...entity.folders, folderSel] })) setFolderSel("");
  };

  return (
    <li className={styles.row}>
      <div className={styles.rowHead}>
        <span
          className={entity.color ? styles.dot : `${styles.dot} ${styles.dotEmpty}`}
          style={entity.color ? { background: entity.color } : undefined}
          aria-hidden="true"
        />
        {renaming ? (
          <form
            className={styles.editForm}
            onSubmit={(e) => {
              e.preventDefault();
              void commitRename();
            }}
          >
            <input
              ref={(el) => el?.focus()}
              className={styles.editInput}
              aria-label={`Project name for ${entity.name}`}
              value={draft}
              disabled={busy}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  setDraft(entity.name);
                  setRenaming(false);
                }
              }}
            />
            <button
              type="submit"
              className={styles.iconBtn}
              aria-label={`Save name for ${entity.name}`}
              disabled={busy}
            >
              <Check size={14} />
            </button>
            <button
              type="button"
              className={styles.iconBtn}
              aria-label={`Cancel renaming ${entity.name}`}
              disabled={busy}
              onClick={() => {
                setDraft(entity.name);
                setRenaming(false);
              }}
            >
              <X size={14} />
            </button>
          </form>
        ) : (
          <span className={styles.name}>{entity.name}</span>
        )}
        <span className={styles.count}>
          {entity.session_count} session{entity.session_count === 1 ? "" : "s"}
        </span>
        <span className={styles.actions}>
          <button
            type="button"
            className={styles.iconBtn}
            aria-label={`Rename project ${entity.name}`}
            title="Rename"
            disabled={busy}
            onClick={() => {
              setDraft(entity.name);
              setRenaming(true);
            }}
          >
            <Pencil size={14} />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            aria-label={`Set color for ${entity.name}`}
            title="Color"
            disabled={busy}
            onClick={() => setShowColors((v) => !v)}
          >
            <Palette size={14} />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            aria-label={`Archive project ${entity.name}`}
            title="Archive"
            disabled={busy}
            onClick={() => void onArchive(entity.id)}
          >
            <Archive size={14} />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            aria-label={`Delete project ${entity.name}`}
            title="Delete"
            disabled={busy}
            onClick={() => void onDelete(entity)}
          >
            <Trash2 size={14} />
          </button>
        </span>
      </div>

      {showColors && (
        <div className={styles.swatches} role="group" aria-label={`Color for ${entity.name}`}>
          {COLOR_PRESETS.map((c) => (
            <button
              key={c}
              type="button"
              className={styles.swatch}
              style={{ background: c }}
              aria-label={`Color ${c}`}
              disabled={busy}
              onClick={() => void patch({ color: c }).then((ok) => ok && setShowColors(false))}
            />
          ))}
          <button
            type="button"
            className={styles.smallBtn}
            disabled={busy}
            onClick={() => void patch({ color: "" }).then((ok) => ok && setShowColors(false))}
          >
            Clear
          </button>
        </div>
      )}

      <div className={styles.folderRow}>
        {entity.folders.map((f) => (
          <span key={f} className={styles.chip} title={f}>
            {shortCwd(f)}
            <button
              type="button"
              className={styles.chipX}
              aria-label={`Release folder ${f} from ${entity.name}`}
              disabled={busy}
              onClick={() =>
                void patch({ folders: entity.folders.filter((x) => x !== f) })
              }
            >
              <X size={11} />
            </button>
          </span>
        ))}
        {adoptable.length > 0 && (
          <span className={styles.adopt}>
            <select
              aria-label={`Adopt a folder into ${entity.name}`}
              value={folderSel}
              disabled={busy}
              onChange={(e) => setFolderSel(e.target.value)}
            >
              <option value="">add folder…</option>
              {adoptable.map((c) => (
                <option key={c} value={c}>
                  {shortCwd(c)}
                </option>
              ))}
            </select>
            <button
              type="button"
              className={styles.smallBtn}
              disabled={busy || !folderSel}
              onClick={() => void addFolder()}
            >
              Add
            </button>
          </span>
        )}
      </div>

      {rowError && <p className={settings.err}>{rowError}</p>}
    </li>
  );
}

/** Settings → Projects manager (#361 Phase 3): CRUD over project entities, folder
 *  adoption, and the bulk archive/unarchive flows with their per-member report.
 *  Every mutation refetches the entity list — the server is the source of truth
 *  (session_count and folder conflicts both resolve there). */
export function ProjectsManagerCard() {
  const [entities, setEntities] = useState<ProjectEntity[] | null>(null);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [report, setReport] = useState<ProjectArchiveReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newFolder, setNewFolder] = useState("");
  const [creating, setCreating] = useState(false);

  const refresh = useCallback(
    // includeArchived: this manager is the one surface that must show archived
    // entities (the Unarchive path lives here).
    () =>
      api.projectEntities({ includeArchived: true }).then((d) => {
        setEntities(d.projects);
      }),
    [],
  );

  useEffect(() => {
    refresh().catch(() => {
      setEntities([]);
      setError("Couldn’t load projects.");
    });
    api
      // Unfiltered folder list, like the Session overview card: curation surfaces
      // must offer every discovered dir, not just the visible ones.
      .folders()
      .then((d) => setFolders(d.folders))
      .catch(() => {});
  }, [refresh]);

  const active = (entities ?? []).filter((e) => !e.archived);
  const archived = (entities ?? []).filter((e) => e.archived);
  // A folder belongs to at most one project (server-enforced 409) — offer only
  // folders adopted NOWHERE, archived projects included.
  const adopted = new Set((entities ?? []).flatMap((e) => e.folders));
  const adoptable = folders.map((f) => f.cwd).filter((c) => !adopted.has(c));

  const runBulk = async (id: string, archiveMembers: boolean) => {
    setError(null);
    try {
      const r = archiveMembers ? await api.archiveProject(id) : await api.unarchiveProject(id);
      setReport(r);
      await refresh();
    } catch (e) {
      setError(errMessage(e, "Couldn’t update the project."));
    }
  };

  const remove = async (entity: ProjectEntity) => {
    // window.confirm over a custom dialog — destructive but cheap to re-do (the
    // entity is just metadata), so a native confirm is proportionate.
    const ok = window.confirm(
      `Delete project “${entity.name}”? Its sessions revert to folder grouping — ` +
        "session files are never touched.",
    );
    if (!ok) return;
    setError(null);
    try {
      await api.deleteProject(entity.id);
      await refresh();
    } catch (e) {
      setError(errMessage(e, "Couldn’t delete the project."));
    }
  };

  const create = async () => {
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    setError(null);
    try {
      await api.createProject({ name, folders: newFolder ? [newFolder] : [] });
      setNewName("");
      setNewFolder("");
      await refresh();
    } catch (e) {
      setError(errMessage(e, "Couldn’t create the project."));
    } finally {
      setCreating(false);
    }
  };

  const failed = (report?.sessions ?? []).filter((s) => s.result === "failed");

  return (
    <section className={settings.section} aria-labelledby="projects-manager-h">
      <h2 id="projects-manager-h">Projects</h2>
      <p className={settings.hint}>
        Group sessions across folders: a project adopts launch folders and can be assigned
        per session. Projects are metadata — assigning or archiving never moves session
        files.
      </p>

      {error && <p className={settings.err}>{error}</p>}

      {entities === null ? (
        <p className={settings.hint}>Loading projects…</p>
      ) : active.length === 0 ? (
        <p className={settings.hint}>No projects yet — create one below.</p>
      ) : (
        <ul className={styles.list} aria-label="Projects">
          {active.map((e) => (
            <EntityRow
              key={e.id}
              entity={e}
              adoptable={adoptable}
              onChanged={refresh}
              onArchive={(id) => runBulk(id, true)}
              onDelete={remove}
            />
          ))}
        </ul>
      )}

      {report && (
        <div className={styles.report} role="status">
          <p className={styles.reportLine}>
            {report.archived ? "Archive" : "Unarchive"}: {formatCounts(report.counts)}
          </p>
          {failed.length > 0 && (
            <>
              <ul className={styles.failedList} aria-label="Failed sessions">
                {failed.map((s) => (
                  <li key={s.id}>
                    <code>{s.id}</code> — {s.reason || "failed"}
                  </li>
                ))}
              </ul>
              {/* Blind re-call of the SAME endpoint: it's idempotent, so the done set
                  reports already_* and only the failed members are retried. */}
              <button
                type="button"
                className={styles.smallBtn}
                onClick={() => void runBulk(report.id, report.archived)}
              >
                Retry
              </button>
            </>
          )}
          <button type="button" className={styles.smallBtn} onClick={() => setReport(null)}>
            Dismiss
          </button>
        </div>
      )}

      <div className={styles.newRow}>
        <input
          type="text"
          className={styles.newInput}
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="project name"
          aria-label="New project name"
        />
        {adoptable.length > 0 && (
          <select
            aria-label="Folder to adopt"
            value={newFolder}
            onChange={(e) => setNewFolder(e.target.value)}
          >
            <option value="">no folder</option>
            {adoptable.map((c) => (
              <option key={c} value={c}>
                {shortCwd(c)}
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          className={styles.smallBtn}
          disabled={!newName.trim() || creating}
          onClick={() => void create()}
        >
          {creating ? "Creating…" : "Create"}
        </button>
      </div>

      {archived.length > 0 && (
        <details className={styles.archivedBox}>
          <summary>Archived ({archived.length})</summary>
          <ul className={styles.archivedList} aria-label="Archived projects">
            {archived.map((e) => (
              <li key={e.id} className={styles.archivedRow}>
                <span className={styles.name}>{e.name}</span>
                <button
                  type="button"
                  className={styles.smallBtn}
                  aria-label={`Unarchive project ${e.name}`}
                  onClick={() => void runBulk(e.id, false)}
                >
                  <ArchiveRestore size={13} /> Unarchive
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
