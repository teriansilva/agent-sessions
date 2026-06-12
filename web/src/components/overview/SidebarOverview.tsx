import { useOverviewSessions } from "../../hooks/useOverviewSessions";
import { OverviewCanvas } from "./OverviewCanvas";

/** The Session Overview squeezed into the sidebar column (compact: no minimap). Default
 *  export so it can be React.lazy-loaded — keeps @xyflow/react out of the main bundle until
 *  the user switches the sidebar to Map (#139). */
export default function SidebarOverview() {
  const { sessions, loading, error, partial, refetch } = useOverviewSessions();
  if (loading) return <div className="tr-overview tr-ov-state">Loading map…</div>;
  if (error) return <div className="tr-overview tr-ov-state">{error}</div>;
  return <OverviewCanvas sessions={sessions} partial={partial} onRefetch={refetch} compact />;
}
