/** Project → General (#2143): identity (name, colour, default project), folders (default folder,
 *  adopted folders, adopt one more) and archive / delete. The same calls Settings → Projects' row
 *  made (#361, #448, #615, #1187); the controls are NIGHTJAR's (`Field`, `Switch`, plates). */
import { Archive, ArchiveRestore, Plus, Star, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useConfigRefresh } from "../../app/config";
import { FolderPickerModal } from "../../components/FolderPickerModal";
import { ProjectColorPicker } from "../../components/ProjectColorPicker";
import { Field, Switch } from "../../components/hud/Controls";
import { Callsign, Panel } from "../../components/hud/Panel";
import { ConfirmDialog } from "../../components/templates/ConfirmDialog";
import action from "../../components/ui/actionButton.module.css";
import { api, ApiError } from "../../lib/api";
import { shortCwd } from "../../lib/format";
import { PROJECTS_PATH } from "../../lib/routes";
import type { Folder, ProjectArchiveReport, ProjectEntity } from "../../types/api";
import { BulkReport } from "./ProjectBits";
import styles from "./projects.module.css";

function errMessage(e: unknown, fallback: string): string {
  // Folder-adoption conflicts (409) carry the server's detail (which project owns the folder):
  // shown verbatim instead of a generic line.
  return e instanceof ApiError && e.message ? e.message : fallback;
}

type Patch = { name?: string; color?: string; folders?: string[]; default_folder?: string };

export function ProjectGeneral({
  entity,
  starred,
  onChanged,
}: {
  entity: ProjectEntity;
  starred: boolean;
  onChanged: () => Promise<unknown>;
}) {
  const navigate = useNavigate();
  const refreshConfig = useConfigRefresh();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState(entity.name);
  const [syncedName, setSyncedName] = useState(entity.name);
  if (entity.name !== syncedName) {
    setSyncedName(entity.name);
    setName(entity.name);
  }
  // Default project (#615 Phase 2): optimistic with rollback, then a config refresh — New session
  // reads it from the shared config. Switching it off clears the preference (the first project is
  // then used, as before the pref existed).
  const [isDefault, setIsDefault] = useState(starred);
  const [syncedStarred, setSyncedStarred] = useState(starred);
  if (starred !== syncedStarred) {
    setSyncedStarred(starred);
    setIsDefault(starred);
  }
  const [folders, setFolders] = useState<Folder[]>([]);
  const [others, setOthers] = useState<ProjectEntity[]>([]);
  const [folderSel, setFolderSel] = useState("");
  const [picking, setPicking] = useState<HTMLElement | null>(null);
  const [report, setReport] = useState<ProjectArchiveReport | null>(null);
  const [confirming, setConfirming] = useState<HTMLElement | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let alive = true;
    // Unfiltered: curation offers every discovered dir. A folder belongs to at most one project
    // (server-enforced 409), so only folders adopted NOWHERE — archived projects included — are
    // offered.
    api
      .folders()
      .then((d) => alive && setFolders(d.folders))
      .catch(() => {});
    api
      .projectEntities({ includeArchived: true })
      .then((d) => alive && setOthers(d.projects))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [entity.folders]);
  const adopted = new Set([...others.flatMap((e) => e.folders), ...entity.folders]);
  const adoptable = folders.map((f) => f.cwd).filter((c) => !adopted.has(c));

  const patch = async (body: Patch) => {
    setBusy(true);
    setError(null);
    try {
      await api.patchProject(entity.id, body);
      await onChanged();
      return true;
    } catch (e) {
      setError(errMessage(e, "Couldn’t update the project."));
      return false;
    } finally {
      setBusy(false);
    }
  };
  const saveName = async () => {
    const next = name.trim();
    if (!next || next === entity.name) {
      setName(entity.name);
      return;
    }
    await patch({ name: next });
  };
  const chooseDefault = (on: boolean) => {
    const prev = isDefault;
    setIsDefault(on);
    setError(null);
    api
      .setPrefs({ default_project_id: on ? entity.id : "" })
      .then(() => refreshConfig())
      .catch(() => {
        setIsDefault(prev);
        setError("Couldn’t save the default project.");
      });
  };
  const runBulk = async (archive: boolean) => {
    setError(null);
    setBusy(true);
    try {
      setReport(archive ? await api.archiveProject(entity.id) : await api.unarchiveProject(entity.id));
      await onChanged();
    } catch (e) {
      setError(errMessage(e, "Couldn’t update the project."));
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    setError(null);
    setDeleting(true);
    try {
      await api.deleteProject(entity.id);
      navigate(PROJECTS_PATH, { replace: true });
    } catch (e) {
      setError(errMessage(e, "Couldn’t delete the project."));
      setDeleting(false);
      setConfirming(null);
    }
  };
  const nameDirty = name.trim() !== entity.name && name.trim() !== "";

  return (
    <div className={styles.body}>
      {error && (
        <p className="nj-error" role="alert" data-testid="project-error">
          {error}
        </p>
      )}
      {report && (
        <BulkReport report={report} onRetry={() => void runBulk(report.archived)} onDismiss={() => setReport(null)} />
      )}
      <div className={styles.cols}>
        <Panel tone="quiet" className={styles.panel} aria-labelledby="identity-h">
          <Callsign as="h2" id="identity-h" title="Identity" />
          <form
            className={styles.inline}
            onSubmit={(e) => {
              e.preventDefault();
              void saveName();
            }}
          >
            <Field
              label="Name"
              value={name}
              disabled={busy}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setName(entity.name);
              }}
              aria-label={`Project name for ${entity.name}`}
            />
            {nameDirty && (
              <button type="submit" className={action.ghost} disabled={busy}>
                Save name
              </button>
            )}
          </form>
          <div className={styles.fieldBlock}>
            <span className="nj-label" id="colour-label">
              Colour
            </span>
            <ProjectColorPicker
              label={`Color for ${entity.name}`}
              value={entity.color}
              disabled={busy}
              onChange={(color) => void patch({ color })}
            />
            <p className="nj-hint">Marks the project’s sessions in the sidebar, the filter and the map.</p>
          </div>
          <div className={styles.sep} />
          <div className={styles.setRow}>
            <div>
              <p className={styles.setTitle}>
                <Star size={14} aria-hidden="true" className={styles.star} /> Default project
              </p>
              <p className={styles.text}>
                Pre-selected on New session; its default folder becomes the start directory. With none
                set, the first project is used.
              </p>
            </div>
            <Switch
              checked={isDefault}
              onChange={chooseDefault}
              label="Default project"
              disabled={busy || entity.archived}
              testId="project-default-switch"
            />
          </div>
        </Panel>

        <Panel tone="quiet" className={styles.panel} aria-labelledby="folders-h">
          <Callsign
            as="h2"
            id="folders-h"
            title="Folders"
            value={`${entity.folders.length} adopted`}
          />
          <div className={styles.fieldBlock}>
            <span className="nj-label">Default folder</span>
            <div className={styles.inline}>
              {entity.default_folder ? (
                <span className={styles.pathBox} title={entity.default_folder}>
                  {shortCwd(entity.default_folder)}
                </span>
              ) : (
                <span className={`${styles.pathBox} ${styles.warn}`}>No default folder — set one</span>
              )}
              <button
                type="button"
                className={action.ghost}
                disabled={busy}
                onClick={(e) => setPicking(e.currentTarget)}
                aria-label={`${entity.default_folder ? "Change" : "Set"} default folder for ${entity.name}`}
              >
                {entity.default_folder ? "Choose…" : "Set folder…"}
              </button>
            </div>
            <p className="nj-hint">Where new sessions in this project start. Always one of the project’s folders.</p>
          </div>
          <div className={styles.fieldBlock}>
            <span className="nj-label">Folders</span>
            {entity.folders.length === 0 ? (
              <p className={styles.quiet}>No folders adopted yet.</p>
            ) : (
              <ul className={styles.folderChips} aria-label={`Folders of ${entity.name}`}>
                {entity.folders.map((f) => (
                  <li key={f} className="nj-chip" title={f}>
                    {f === entity.default_folder && (
                      <Star size={12} fill="currentColor" className={styles.star} aria-label="default folder" />
                    )}
                    {shortCwd(f)}
                    <button
                      type="button"
                      className={styles.chipX}
                      aria-label={`Release folder ${f} from ${entity.name}`}
                      disabled={busy}
                      onClick={() => void patch({ folders: entity.folders.filter((x) => x !== f) })}
                    >
                      <X size={12} aria-hidden="true" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {adoptable.length > 0 && (
            <div className={styles.inline}>
              <Field
                as="select"
                label="Add folder"
                mono
                aria-label={`Adopt a folder into ${entity.name}`}
                value={folderSel}
                disabled={busy}
                onChange={(e) => setFolderSel(e.target.value)}
              >
                <option value="">Choose a folder…</option>
                {adoptable.map((c) => (
                  <option key={c} value={c}>
                    {shortCwd(c)}
                  </option>
                ))}
              </Field>
              <button
                type="button"
                className={`${action.ghost} ${styles.alignEnd}`}
                disabled={busy || !folderSel}
                onClick={() =>
                  void patch({ folders: [...entity.folders, folderSel] }).then((ok) => ok && setFolderSel(""))
                }
              >
                <Plus size={14} aria-hidden="true" /> Add
              </button>
            </div>
          )}
          <p className={styles.text}>
            A folder belongs to one project; only folders no project has adopted are offered. Releasing
            a folder never moves session files: its sessions fall back to folder grouping.
          </p>
        </Panel>
      </div>

      <Panel tone="quiet" className={styles.panel} aria-labelledby="danger-h">
        <Callsign as="h2" id="danger-h" title="Archive or delete" />
        <div className={styles.setRow}>
          <div>
            <p className={styles.setTitle}>{entity.archived ? "Unarchive project" : "Archive project"}</p>
            <p className={styles.text}>
              {entity.archived
                ? `Brings ${entity.name} and its sessions back.`
                : `Archives ${entity.name} and its ${entity.session_count} session${entity.session_count === 1 ? "" : "s"}. Session files stay on disk; Unarchive brings both back.`}
            </p>
          </div>
          <button
            type="button"
            className={action.ghost}
            disabled={busy}
            aria-label={`${entity.archived ? "Unarchive" : "Archive"} project ${entity.name}`}
            onClick={() => void runBulk(!entity.archived)}
          >
            {entity.archived ? <ArchiveRestore size={14} aria-hidden="true" /> : <Archive size={14} aria-hidden="true" />}
            {entity.archived ? "Unarchive project" : "Archive project"}
          </button>
        </div>
        <div className={styles.sep} />
        <div className={styles.setRow}>
          <div>
            <p className={styles.setTitle}>Delete project</p>
            <p className={styles.text}>Its sessions revert to folder grouping — session files are never touched. Asks first.</p>
          </div>
          <button
            type="button"
            className={action.danger}
            disabled={busy}
            aria-label={`Delete project ${entity.name}`}
            onClick={(e) => setConfirming(e.currentTarget)}
          >
            <Trash2 size={14} aria-hidden="true" /> Delete project
          </button>
        </div>
      </Panel>

      {picking && (
        <FolderPickerModal
          initialPath={entity.default_folder || undefined}
          title={`Default folder for “${entity.name}”`}
          onPick={(path) => {
            setPicking(null);
            void patch({ default_folder: path });
          }}
          onCancel={() => setPicking(null)}
          returnFocusTo={picking}
        />
      )}
      {/* THE confirm dialog (#1531), never `window.confirm`. */}
      {confirming && (
        <ConfirmDialog
          tag="Delete project"
          title={`Delete project “${entity.name}”?`}
          confirmLabel="Delete project"
          danger
          busy={deleting}
          onCancel={() => setConfirming(null)}
          onConfirm={() => void remove()}
          returnFocusTo={confirming}
          fallbackFocusSelector="#project-h"
        >
          <p>Its sessions revert to folder grouping — session files are never touched.</p>
        </ConfirmDialog>
      )}
    </div>
  );
}
