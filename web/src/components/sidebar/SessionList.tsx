import { Link } from "react-router-dom";
import type { Session } from "../../types/api";
import { useSessions } from "../../hooks/useSessions";
import styles from "./SessionList.module.css";

function relTime(epoch: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function shortCwd(cwd: string): string {
  return cwd.replace(/^\/home\/[^/]+\//, "~/");
}

function Row({ s }: { s: Session }) {
  const badge = s.engine === "opencode" ? "oc" : s.engine === "codex" ? "cx" : "cc";
  return (
    <li>
      <Link to={`/s/${s.engine}/${s.uuid}`} className={styles.row}>
        <span className={`${styles.badge} ${styles[s.engine] ?? ""}`}>{badge}</span>
        <div className={styles.body}>
          <div className={styles.title}>{s.title || "(untitled)"}</div>
          <div className={styles.meta}>
            {shortCwd(s.cwd)} · {relTime(s.last_mtime)}
          </div>
        </div>
      </Link>
    </li>
  );
}

/** Phase-0 read-only proof view: typed API → render. Interaction comes later. */
export function SessionList() {
  const { sessions, total, loading, error } = useSessions();

  if (error) return <div className={styles.empty}>{error}</div>;
  if (loading) return <div className={styles.empty}>Loading…</div>;
  if (sessions.length === 0) return <div className={styles.empty}>No sessions yet.</div>;

  return (
    <ul className={styles.list} aria-label={`${total} sessions`}>
      {sessions.map((s) => (
        <Row key={s.id} s={s} />
      ))}
    </ul>
  );
}
