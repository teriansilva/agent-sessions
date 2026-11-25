import { RotateCcw } from "lucide-react";
import { useEffect } from "react";
import { useMapWindows } from "../app/workspaceWindows";
import { NewSessionModal } from "../components/overview/NewSessionModal";
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
  // New session over the map (#1648): opened from the sidebar, rendered here so it lives exactly
  // as long as the map route does. Leaving the route (a detour, a full-screen start) closes it.
  const commands = useMapWindows();
  const newSession = commands?.newSession ?? null;
  const closeNewSession = commands?.closeNewSession;
  useEffect(() => () => closeNewSession?.(), [closeNewSession]);
  const modal = newSession && closeNewSession && (
    <NewSessionModal onClose={closeNewSession} returnFocusTo={newSession.trigger} />
  );
  if (loading)
    return (
      <div className="tr-overview tr-ov-state" data-map-state="loading">
        <Loader kind="acquire" label="Loading session map…" className="tr-ov-loader" />
        {modal}
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
        {modal}
      </div>
    );
  return (
    <>
      <OverviewCanvas sessions={sessions} partial={partial} onRefetch={refetch} />
      {modal}
    </>
  );
}
