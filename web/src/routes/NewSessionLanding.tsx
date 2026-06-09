import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import type { Project } from "../types/api";
import styles from "./NewSessionLanding.module.css";

/** Landing at "/" — no session selected. Pick an engine + project and start a new
 *  session: we mint a client-side id, then navigate to /s/:engine/:id carrying the
 *  fresh-launch params (cwd + bypass) so the terminal opens it with ?new=1. */
export function NewSessionLanding() {
  const config = useConfig();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
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
      .projects()
      .then((r) => {
        if (alive) setProjects(r.projects);
      })
      .catch(() => {
        if (alive) setError("Couldn’t load projects.");
      });
    return () => {
      alive = false;
    };
  }, []);

  const canStart = Boolean(engine && cwd);

  const start = () => {
    if (!canStart) return;
    const id = mintNewSessionId(engine);
    navigate(`/s/${engine}/${id}`, { state: { fresh: { cwd, bypass } } });
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
          <span>Project</span>
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
