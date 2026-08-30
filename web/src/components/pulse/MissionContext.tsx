/** Folder, git summary, session roster and JUMP IN (#878).
 *
 * `GET /api/missions/{id}/context` takes **no client path** — the server reads the mission's own
 * cwd — so there is nothing here that can be pointed somewhere it should not go. The git read
 * fails CLOSED and names only the exception KIND, never the path, and this component renders that
 * as a degraded state rather than as an empty panel.
 */
import { Link } from "react-router-dom";

import type { MissionContext as MissionContextData } from "../../types/api";

import styles from "./mission.module.css";

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded. A bare `replace(":", "/")` is wrong
 *  for any id needing escaping, and silently so. */
function sessionRoute(key: string): string {
  const i = key.indexOf(":");
  const engine = i < 0 ? key : key.slice(0, i);
  const uuid = i < 0 ? "" : key.slice(i + 1);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
}

/** One line summarising the working tree. Each count is checked rather than assumed, so a
 *  server-side shape change degrades to a shorter line instead of rendering `undefined`. */
function gitLine(g: MissionContextData["git"]): string {
  if (!g) return "";
  const parts: string[] = [];
  const entries = Array.isArray(g.entries) ? g.entries : [];
  if (entries.length) parts.push(`${entries.length} changed`);
  const staged = entries.filter((e) => e.kind === "staged").length;
  if (staged) parts.push(`${staged} staged`);
  if (typeof g.ahead === "number" && g.ahead > 0) parts.push(`ahead ${g.ahead}`);
  if (typeof g.behind === "number" && g.behind > 0) parts.push(`behind ${g.behind}`);
  if (!parts.length) parts.push("clean");
  return parts.join(" · ");
}

export function MissionContextPanel({
  context,
  loading,
}: {
  context: MissionContextData | null;
  loading?: boolean;
}) {
  if (loading && !context) {
    return (
      <div className={styles.empty} data-testid="context-loading">
        Reading the mission's folder…
      </div>
    );
  }
  if (!context) return null;

  const sessions = context.sessions ?? [];
  const first = sessions[0]?.session_key;
  const branch = context.git?.branch;

  return (
    <div data-testid="mission-context">
      {context.cwd ? (
        <div className={styles.cwd}>{context.cwd}</div>
      ) : (
        <div className={styles.empty}>No folder yet — this mission is still a draft.</div>
      )}

      {context.git_error ? (
        // Fails CLOSED and says so. The server deliberately sends only the exception kind, so
        // this cannot leak a path even if the operator screenshots it.
        <div className={styles.notice} role="status" data-testid="context-git-error">
          <div className={styles.noticeLead}>Git could not be read.</div>
          <div>{context.git_error} — the working tree is not shown.</div>
        </div>
      ) : context.git ? (
        <div className={styles.gitLine}>
          {branch ? `${branch} · ` : ""}
          {gitLine(context.git)}
        </div>
      ) : null}

      {sessions.length ? (
        <div className={styles.roster}>
          {sessions.map((s) => s.session_key).join("  ·  ")}
        </div>
      ) : (
        <div className={styles.roster}>No sessions adopted yet.</div>
      )}

      {first ? (
        // A real route, not a handler — the operator can middle-click it, and it survives the
        // console being unmounted.
        <Link
          className={styles.jumpIn}
          to={sessionRoute(first)}
          data-testid="jump-in"
        >
          JUMP IN
        </Link>
      ) : null}
    </div>
  );
}
