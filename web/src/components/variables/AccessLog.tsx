import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { sessionPathFromKey } from "../../lib/format";
import { engineName } from "../../app/engineRoster";
import type { SecretAccessEntry } from "../../types/variables";
import styles from "../../routes/Variables.module.css";

const OUTCOME: Record<SecretAccessEntry["outcome"], string> = {
  materialized: "delivered",
  pending: "in progress",
  failed: "failed",
  refused: "refused",
  interrupted: "interrupted · copy removed",
};
const REASON: Record<string, string> = {
  access_off: "access off",
  not_bound: "not bound to the project",
  not_offered: "not offered",
  needs_reentry: "needs re-entry",
  storage_unavailable: "no private storage",
  audit_unavailable: "log unavailable",
  store_unavailable: "library unreadable",
  launch_ended: "session ended",
  launch_unconfirmed: "session not confirmed",
  project_changed: "session moved project",
  no_project: "not in a project",
};

function when(at: number): string {
  const d = new Date(at * 1000);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === today.toDateString() ? `Today ${time}` : `${d.toLocaleDateString()} ${time}`;
}

/** The newest agent requests for one secret (#1786): granted and refused, never a value. */
export function AccessLog({ vid, projectNames }: { vid: string; projectNames: Map<string, string> }) {
  const [entries, setEntries] = useState<SecretAccessEntry[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    api.variableAccessLog(vid).then(r => { if (live) setEntries(r.entries); }).catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [vid]);
  return <div className={styles.field}>
    <span>Access log · last 50 requests</span>
    {failed ? <div className={styles.error} role="alert">The access log could not be read. While it cannot be written, every agent request for a secret is refused, because no request is granted without its log entry. Reload to try again.</div>
      : entries === null ? <p className={styles.help}>Loading the access log…</p>
      : entries.length === 0 ? <p className={styles.logEmpty}>No agent has requested this secret yet.</p>
      : <table className={styles.log}>
        <thead><tr><th>When</th><th>Project</th><th className={styles.hideNarrow}>Agent</th><th>Session</th></tr></thead>
        <tbody>{entries.map((e, i) => {
          const href = e.outcome === "materialized" && e.session_key ? sessionPathFromKey(e.session_key) : null;
          const outcome = e.outcome === "refused" || e.outcome === "failed"
            ? `${OUTCOME[e.outcome]} · ${REASON[e.reason ?? ""] ?? e.reason ?? ""}`
            : OUTCOME[e.outcome];
          return <tr key={i}>
            <td>{when(e.at)}</td>
            <td className={styles.dim}>{e.project_id ? projectNames.get(e.project_id) ?? e.project_id : "—"}</td>
            <td className={styles.hideNarrow}>{e.engine ? engineName(e.engine) : "—"}</td>
            <td>{href
              ? <Link to={href}>open session ↗</Link>
              : <span className={styles.dim}>{outcome}</span>}</td>
          </tr>;
        })}</tbody>
      </table>}
  </div>;
}
