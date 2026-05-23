import { useParams } from "react-router-dom";

/** Session view at "/s/:engine/:id" — the URL is the single source of truth for
 *  which session is open (deep-linkable, refresh-safe). The real terminal pane
 *  (xterm + touch + delta-resume) lands in Phase 4; this proves the route + params. */
export function SessionView() {
  const { engine, id } = useParams<{ engine: string; id: string }>();
  return (
    <div className="session-view">
      <div className="session-id">
        {engine}:{id}
      </div>
      <div className="placeholder">terminal pane — Phase 4</div>
    </div>
  );
}
