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
  // Effective selection: the user's explicit choice, else the first available option.
  // Derived (not effect-set) so there are no defaulting cascades.
  const engine = engineChoice || engines[0] || "";
  const cwd = cwdChoice || projects[0]?.cwd || "";

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
