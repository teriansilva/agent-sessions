import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useConfig } from "../app/config";
import { FolderPickerModal } from "../components/FolderPickerModal";
import { api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import { owningProjectId } from "../lib/projectTree";
import { shortCwd } from "../lib/format";
import type { ProjectEntity } from "../types/api";
import styles from "./NewSessionLanding.module.css";

/** Landing at "/" — no session selected. Pick an engine + project + folder and start a new
 *  session (#448): a project owns a DEFAULT launch folder, so choosing a project prefills the
 *  folder; the folder is overridable for this one session via a ~/-rooted picker. We mint a
 *  client-side id, navigate to /s/:engine/:id carrying the fresh-launch params (cwd + bypass). */
export function NewSessionLanding() {
  const config = useConfig();
  const navigate = useNavigate();
  const [engineChoice, setEngineChoice] = useState("");
  const [bypass, setBypass] = useState(true);

  const engines = config?.new_session_engines ?? [];
  const engine = engineChoice || engines[0] || "";

  // Project entities own the default launch folder (#448). projectChoice === null = untouched
  // (use the default selection); "" = no project; else an entity id.
  const [entities, setEntities] = useState<ProjectEntity[]>([]);
  const [projectChoice, setProjectChoice] = useState<string | null>(null);
  // The operator's starred project (#615 Phase 2). `entities` is already archived-filtered, so an
  // id naming an archived — or since-deleted — project simply isn't found, and we fall back to the
  // first project rather than pre-selecting nothing. Before #615 there was no pref at all: the
  // preselection was `entities[0]`, i.e. whichever project sorted first by name.
  const starredId = config?.default_project_id ?? "";
  const starredExists = entities.some((p) => p.id === starredId);
  const defaultProjectId = (starredExists ? starredId : entities[0]?.id) ?? "";
  const projectSel = projectChoice ?? defaultProjectId;
  const selectedProject = entities.find((p) => p.id === projectSel);

  // Folder: the project's default unless overridden for this session via the picker.
  const [cwdOverride, setCwdOverride] = useState<string | null>(null);
  // `||`, not `??`, between the project's folder and the legacy pref: a folderless project stores
  // "" (#448 back-compat), and "" is a *missing* folder, not a chosen one — fall through to the
  // legacy cwd rather than opening the picker on nothing (#615 Phase 2 edge case). `cwdOverride`
  // keeps `??`: an explicit "" from the picker is a real choice.
  const projectCwd = selectedProject?.default_folder || config?.default_project || "";
  const cwd = cwdOverride ?? projectCwd;
  const isProjectDefault = !!selectedProject && cwd === selectedProject.default_folder && cwd !== "";

  // One folder picker serves two flows: overriding this session's folder, or choosing the
  // default folder for a "+ New project". `null` = closed.
  const [picker, setPicker] = useState<null | "cwd" | "newproject">(null);
  // Captured at open time (not read from a ref during render) so focus returns to the trigger.
  const [pickerReturn, setPickerReturn] = useState<HTMLElement | null>(null);
  const openPicker = (mode: "cwd" | "newproject", e: { currentTarget: HTMLElement }) => {
    setPickerReturn(e.currentTarget);
    setPicker(mode);
  };

  // Inline "+ New project" (#448): name + a REQUIRED default folder (the picker).
  const [showNewProject, setShowNewProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectFolder, setNewProjectFolder] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [projectError, setProjectError] = useState<string | null>(null);

  const refreshEntities = () =>
    api
      .projectEntities()
      .then((r) => setEntities(r.projects.filter((p) => !p.archived)))
      .catch(() => {});

  useEffect(() => {
    refreshEntities();
  }, []);

  const createProject = async () => {
    const name = newProjectName.trim();
    if (!name || !newProjectFolder || creatingProject) return;
    setCreatingProject(true);
    setProjectError(null);
    try {
      const created = await api.createProject({ name, default_folder: newProjectFolder });
      await refreshEntities();
      setProjectChoice(created.id); // select it; folder follows its default
      setCwdOverride(null);
      setShowNewProject(false);
      setNewProjectName("");
      setNewProjectFolder("");
    } catch {
      setProjectError("Couldn’t create that project.");
    } finally {
      setCreatingProject(false);
    }
  };

  const canStart = Boolean(engine && cwd);

  const start = () => {
    if (!canStart) return;
    const id = mintNewSessionId(engine);
    navigate(`/s/${engine}/${id}`, { state: { fresh: { cwd, bypass } } });
    // Stamp the project only when it's an explicit choice folder-resolution wouldn't already
    // produce (#361): a redundant owning entity isn't stamped; "" never stamps.
    const owningId = owningProjectId(cwd, entities);
    if (projectSel && projectSel !== owningId) {
      api.setSessionProject(`${engine}:${id}`, projectSel).catch(() => {});
    }
  };

  const onPick = (path: string) => {
    if (picker === "cwd") setCwdOverride(path);
    else if (picker === "newproject") setNewProjectFolder(path);
    setPicker(null);
  };

  return (
    <div className={styles.landing}>
      <div className={styles.card}>
        <div className={styles.brandHero}>
          <div className={styles.wordmark}>
            Battle<b>Lab</b>
          </div>
          <p className={styles.tagline}>Command &amp; Code</p>
        </div>
        <h1>Start a new session</h1>

        {engines.length > 1 && (
          <label className={styles.field}>
            <span>Agent</span>
            <select value={engine} onChange={(e) => setEngineChoice(e.target.value)}>
              {engines.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
        )}

        {/* Project FIRST (#448): it owns the default launch folder below. */}
        {entities.length > 0 && (
          <label className={styles.field}>
            <span>Project</span>
            <select
              aria-label="Project"
              value={projectSel}
              onChange={(e) => {
                setProjectChoice(e.target.value);
                setCwdOverride(null); // folder follows the newly-selected project's default
              }}
            >
              <option value="">no project</option>
              {entities.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {!showNewProject ? (
          <button type="button" className={styles.setDefault} onClick={() => setShowNewProject(true)}>
            + New project…
          </button>
        ) : (
          <div className={styles.newFolder}>
            <input
              type="text"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              placeholder="project name"
              aria-label="New project name"
            />
            <button
              type="button"
              className={styles.newFolderBtn}
              onClick={(e) => openPicker("newproject", e)}
            >
              {newProjectFolder ? `📁 ${shortCwd(newProjectFolder)}` : "Default folder *…"}
            </button>
            <button
              type="button"
              className={styles.newFolderBtn}
              onClick={() => void createProject()}
              disabled={!newProjectName.trim() || !newProjectFolder || creatingProject}
            >
              {creatingProject ? "Creating…" : "Create"}
            </button>
            <button
              type="button"
              className={styles.newFolderBtn}
              onClick={() => {
                setShowNewProject(false);
                setNewProjectName("");
                setNewProjectFolder("");
                setProjectError(null);
              }}
            >
              Cancel
            </button>
          </div>
        )}
        {projectError && <p className={styles.error}>{projectError}</p>}

        {/* Folder BELOW the project (#448): prefilled from the project's default, overridable. */}
        <label className={styles.field}>
          <span>Folder</span>
          <div className={styles.folderRow}>
            <input
              type="text"
              readOnly
              aria-label="Launch folder"
              className={styles.folderPath}
              value={cwd || "no folder selected"}
            />
            <button
              type="button"
              className={styles.newFolderBtn}
              onClick={(e) => openPicker("cwd", e)}
            >
              Choose folder…
            </button>
          </div>
        </label>
        {isProjectDefault ? (
          <p className={styles.hint}>✓ default folder for “{selectedProject?.name}” — change it just for this session</p>
        ) : selectedProject && !selectedProject.default_folder && !cwdOverride ? (
          <p className={styles.error}>“{selectedProject.name}” has no default folder — choose one for this session.</p>
        ) : null}

        <label className={styles.checkbox}>
          <input type="checkbox" checked={bypass} onChange={(e) => setBypass(e.target.checked)} />
          <span>Skip permission prompts</span>
        </label>

        <button type="button" className={`${styles.start} shine`} disabled={!canStart} onClick={start}>
          Start session
        </button>
        <p className={styles.hint}>…or open an existing session from the list.</p>
      </div>

      {picker && (
        <FolderPickerModal
          initialPath={picker === "cwd" ? cwd || undefined : newProjectFolder || undefined}
          title={picker === "newproject" ? "Choose the project's default folder" : "Choose a folder"}
          onPick={onPick}
          onCancel={() => setPicker(null)}
          returnFocusTo={pickerReturn}
        />
      )}
    </div>
  );
}
