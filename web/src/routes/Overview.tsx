import { RotateCcw } from "lucide-react";
import { Loader } from "../components/hud/Loader";
import { OverviewCanvas } from "../components/overview/OverviewCanvas";
import action from "../components/ui/actionButton.module.css";
import { useOverviewSessions } from "../hooks/useOverviewSessions";

/** Fullscreen Session Overview (#139) — the project-cluster map. Default export so it can be
 *  React.lazy-loaded from the route (keeps @xyflow/react out of the main bundle).
 *
 *  Its two blocking states are NIGHTJAR's (#1531 Phase 9): the first read is Acquiring over the
 *  tactical grid, and a failed first read is an error with Retry. Only a read with nothing to show
 *  blocks — a revalidation runs behind the retained map (#1007), so neither state ever replaces a
 *  painted map. */
export default function Overview() {
  const { sessions, loading, error, partial, refetch } = useOverviewSessions();
  if (loading)
    return (
      <div className="tr-overview tr-ov-state" data-map-state="loading">
        <Loader kind="acquire" label="Loading session map…" className="tr-ov-loader" />
      </div>
    );
  if (error)
    return (
      <div className="tr-overview tr-ov-state" data-map-state="error">
        <p className="tr-ov-state-error" role="alert">
          {error}
        </p>
        <button type="button" className={`${action.ghost} ${action.compact}`} onClick={refetch}>
          <RotateCcw size={14} aria-hidden="true" /> Retry
        </button>
      </div>
    );
  return (
    <OverviewCanvas sessions={sessions} partial={partial} onRefetch={refetch} />
  );
}
