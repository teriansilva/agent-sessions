import { OverviewCanvas } from "../components/overview/OverviewCanvas";
import { useOverviewSessions } from "../hooks/useOverviewSessions";

/** Fullscreen Session Overview (#139) — the project-cluster map. Default export so it can be
 *  React.lazy-loaded from the route (keeps @xyflow/react out of the main bundle). */
export default function Overview() {
  const { sessions, loading, error, partial, refetch } = useOverviewSessions();
  if (loading) return <div className="tr-overview tr-ov-state">Loading session map…</div>;
  if (error) return <div className="tr-overview tr-ov-state">{error}</div>;
  return <OverviewCanvas sessions={sessions} partial={partial} onRefetch={refetch} />;
}
