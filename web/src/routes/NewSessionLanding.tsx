import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import { owningProjectId } from "../lib/projectTree";
import type { Folder, ProjectEntity } from "../types/api";
import styles from "./NewSessionLanding.module.css";

/** Landing at "/" — no session selected. Pick an engine + project and start a new
 *  session: we mint a client-side id, then navigate to /s/:engine/:id carrying the
 *  fresh-launch params (cwd + bypass) so the terminal opens it with ?new=1. */
export function NewSessionLanding() {
  const config = useConfig();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Folder[]>([]);
  const [engineChoice, setEngineChoice] = useState("");
  const [cwdChoice, setCwdChoice] = useState("");
  const [bypass, setBypass] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const engines = config?.new_session_engines ?? [];
  // Preferred start dir (#335 Phase 2): honor it ONLY when it's still a pickable project (a stale
  // value silently falls back). `savedDefault` tracks a just-saved value so the button reflects it
  // without a config refetch.
  const [savedDefault, setSavedDefault] = useState<string | null>(null);
  const effectiveDefault = savedDefault ?? config?.default_project ?? "";
  const validDefault = projects.some((p) => p.cwd === effectiveDefault) ? effectiveDefault : "";
  // Effective selection: the user's explicit choice, else the preferred default, else the first
  // option. Derived (not effect-set) so there are no defaulting cascades.
  const engine = engineChoice || engines[0] || "";
  const cwd = cwdChoice || validDefault || projects[0]?.cwd || "";
  const isDefault = cwd !== "" && cwd === effectiveDefault;

  // Project entity assignment (#361 Phase 3). `projectChoice === null` means the user
  // hasn't touched the select, so it FOLLOWS the folder selection: the default is the
  // entity whose adopted folder owns the selected cwd (same boundary rule as the server
  // resolver), which is also what folder resolution would yield with no stamp at all.
  const [entities, setEntities] = useState<ProjectEntity[]>([]);
  const [projectChoice, setProjectChoice] = useState<string | null>(null);
  const owningId = owningProjectId(cwd, entities);
  const projectSel = projectChoice ?? owningId;

  // Inline "+ New project…" (#361): name-only — standalone entity, NO folder adoption
  // (adoption lives in Settings → Projects, where the conflict rules are surfaced).
  const [showNewProject, setShowNewProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [projectError, setProjectError] = useState<string | null>(null);

  const createProject = async () => {
    const name = newProjectName.trim();
    if (!name || creatingProject) return;
    setCreatingProject(true);
    setProjectError(null);
    try {
      const created = await api.createProject({ name });
      setEntities((prev) => [...prev, { ...created, session_count: 0 }]);
      setProjectChoice(created.id); // an explicit choice — folder-follow stops
      setShowNewProject(false);
      setNewProjectName("");
    } catch {
      setProjectError("Couldn’t create that project.");
    } finally {
      setCreatingProject(false);
    }
  };

  const setAsDefault = () => {
    if (!cwd || isDefault) return;
    setSavedDefault(cwd);
    api.setPrefs({ default_project: cwd }).catch(() => setSavedDefault(null));
  };

  // Scoped create-folder (#335 Phase 3). Only offered when the server reports configured roots.
  const roots = config?.project_roots ?? [];
  const [showNewFolder, setShowNewFolder] = useState(false);
  const [newRoot, setNewRoot] = useState("");
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [folderError, setFolderError] = useState<string | null>(null);

  const createFolder = async () => {
    const root = newRoot || roots[0] || "";
    const name = newName.trim();
    if (!root || !name || creating) return;
    setCreating(true);
    setFolderError(null);
    try {
      const { cwd: created } = await api.mkdir(root, name);
      // A brand-new empty dir isn't "pickable" yet (no sessions), so add it locally + select it;
      // it becomes pickable server-side once a session runs there.
      setProjects((prev) =>
        prev.some((p) => p.cwd === created) ? prev : [...prev, { cwd: created, label: created }],
      );
      setCwdChoice(created);
      setShowNewFolder(false);
      setNewName("");
    } catch {
      setFolderError("Couldn’t create that folder — check the name.");
    } finally {
      setCreating(false);
    }
  };

  useEffect(() => {
    let alive = true;
    api
      // visible: the picker mirrors the curated sidebar (#335) — excluded/hidden dirs don't
      // resurface here; Settings still fetches the unfiltered list for curation.
      .folders({ visible: true })
      .then((r) => {
        if (alive) setProjects(r.folders);
      })
      .catch(() => {
        if (alive) setError("Couldn’t load projects.");
      });
    api
      // Default (non-archived) entity list for the project select (#361). Fail-soft: with
      // no entities the select simply doesn't render — starting a session never blocks
      // on the projects store.
      .projectEntities()
      .then((r) => {
        if (alive) setEntities(r.projects);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const canStart = Boolean(engine && cwd);

  const start = () => {
    if (!canStart) return;
    const id = mintNewSessionId(engine);
    navigate(`/s/${engine}/${id}`, { state: { fresh: { cwd, bypass } } });
    // Stamp the project ONLY when it's an explicit choice folder-resolution would not
    // already produce (#361): the owning entity is redundant metadata, "" never stamps.
    // Best-effort AFTER navigation — `id` is exactly the session key the terminal opens
    // (for opencode the `new-<uuid>` placeholder; the sidecar follows the placeholder
    // alias on reconcile, so metadata stamped on it lands on the real `ses_…` row).
    if (projectSel && projectSel !== owningId) {
      api.setSessionProject(`${engine}:${id}`, projectSel).catch(() => {});
    }
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
        {error && <p className={styles.error}>{error}</p>}

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

        <label className={styles.field}>
          {/* Launch location (#361): folders are WHERE a session runs; the project
              select below is WHAT it belongs to. */}
          <span>Folder</span>
          <select
            value={cwd}
            onChange={(e) => setCwdChoice(e.target.value)}
            disabled={projects.length === 0}
          >
            {projects.length === 0 ? (
              <option value="">no projects found</option>
            ) : (
              projects.map((p) => (
                <option key={p.cwd} value={p.cwd}>
                  {p.label}
                </option>
              ))
            )}
          </select>
        </label>

        {projects.length > 0 && cwd && (
          <button
            type="button"
            className={styles.setDefault}
            onClick={setAsDefault}
            disabled={isDefault}
            aria-label={
              isDefault
                ? "This is your default project"
                : "Set the selected project as the default for new sessions"
            }
          >
            {isDefault ? "✓ Default project" : "Set as default project"}
          </button>
        )}

        {roots.length > 0 &&
          (!showNewFolder ? (
            <button
              type="button"
              className={styles.setDefault}
              onClick={() => setShowNewFolder(true)}
            >
              + New folder
            </button>
          ) : (
            <div className={styles.newFolder}>
              {roots.length > 1 && (
                <select
                  value={newRoot || roots[0]}
                  onChange={(e) => setNewRoot(e.target.value)}
                  aria-label="Base directory"
                >
                  {roots.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              )}
              <input
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="folder name"
                aria-label="New folder name"
              />
              <button
                type="button"
                className={styles.newFolderBtn}
                onClick={createFolder}
                disabled={!newName.trim() || creating}
              >
                {creating ? "Creating…" : "Create"}
              </button>
              <button
                type="button"
                className={styles.newFolderBtn}
                onClick={() => {
                  setShowNewFolder(false);
                  setFolderError(null);
                }}
              >
                Cancel
              </button>
            </div>
          ))}
        {folderError && <p className={styles.error}>{folderError}</p>}

        {/* Project entity select (#361): only rendered once entities exist — with zero
            entities folder grouping is the only behaviour and the extra control would
            just be noise. Untouched, it follows the folder selection (see projectSel). */}
        {entities.length > 0 && (
          <label className={styles.field}>
            <span>Project</span>
            <select
              aria-label="Assign to project"
              value={projectSel}
              onChange={(e) => setProjectChoice(e.target.value)}
            >
              <option value="">none (group by folder)</option>
              {entities.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {!showNewProject ? (
          <button
            type="button"
            className={styles.setDefault}
            onClick={() => setShowNewProject(true)}
          >
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
              onClick={() => void createProject()}
              disabled={!newProjectName.trim() || creatingProject}
            >
              {creatingProject ? "Creating…" : "Create"}
            </button>
            <button
              type="button"
              className={styles.newFolderBtn}
              onClick={() => {
                setShowNewProject(false);
                setProjectError(null);
              }}
            >
              Cancel
            </button>
          </div>
        )}
        {projectError && <p className={styles.error}>{projectError}</p>}

        <label className={styles.checkbox}>
          <input type="checkbox" checked={bypass} onChange={(e) => setBypass(e.target.checked)} />
          <span>Skip permission prompts</span>
        </label>

        <button type="button" className={`${styles.start} shine`} disabled={!canStart} onClick={start}>
          Start session
        </button>
        <p className={styles.hint}>…or open an existing session from the list.</p>
      </div>
    </div>
  );
}
