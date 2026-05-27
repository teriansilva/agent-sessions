import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  type Node,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ChevronsDownUp, ChevronsUpDown } from "lucide-react";
import { useMemo } from "react";
import { engineColor } from "../../lib/format";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { buildOverview, type SessionNodeData } from "../../lib/overviewGraph";
import type { Session } from "../../types/api";
import { OverviewActions } from "./overviewActions";
import { ProjectGroupNode } from "./ProjectGroupNode";
import { SessionNode } from "./SessionNode";
import "./overview.css";

// Stable identity (module scope) so React Flow doesn't re-register node types each render.
const nodeTypes = { projectGroup: ProjectGroupNode, session: SessionNode };

const miniMapColor = (n: Node): string =>
  n.type === "session" ? engineColor((n.data as SessionNodeData).session.engine) : "var(--border)";

/** The project-cluster canvas, shared by the fullscreen /overview route and the squeezed
 *  sidebar view. `compact` drops the minimap for the narrow sidebar column. Clusters collapse
 *  by default; their open/closed state + excluded projects are persisted per-user (#144). */
export function OverviewCanvas({
  sessions,
  includeArchived = false,
  partial = false,
  compact = false,
}: {
  sessions: Session[];
  includeArchived?: boolean;
  partial?: boolean;
  compact?: boolean;
}) {
  const { expanded, excluded, toggle, expandAll, collapseAll } = useOverviewPrefs();

  const { nodes, edges } = useMemo(
    () => buildOverview(sessions, { includeArchived, expanded, excluded }),
    [sessions, includeArchived, expanded, excluded],
  );
  // Cwds available to expand (non-excluded) — drives "Expand all".
  const allCwds = useMemo(
    () => [...new Set(sessions.filter((s) => !excluded.has(s.cwd)).map((s) => s.cwd))],
    [sessions, excluded],
  );

  if (!nodes.length) {
    return <div className="tr-overview tr-ov-state">No sessions to map yet.</div>;
  }

  return (
    <OverviewActions.Provider value={{ toggle }}>
      <div className="tr-overview" style={{ position: "relative" }}>
        {partial && <div className="tr-ov-partial">Showing the most recent sessions</div>}
        <div className="tr-ov-toolbar">
          <button type="button" onClick={() => expandAll(allCwds)} title="Expand all projects">
            <ChevronsUpDown size={14} /> Expand all
          </button>
          <button type="button" onClick={collapseAll} title="Collapse all projects">
            <ChevronsDownUp size={14} /> Collapse all
          </button>
        </div>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.18 }}
          minZoom={0.2}
          maxZoom={1.5}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--border)" />
          <Controls showInteractive={false} position={compact ? "bottom-right" : "bottom-left"} />
          {!compact && (
            <MiniMap pannable zoomable nodeColor={miniMapColor} maskColor="rgba(0,0,0,0.45)" />
          )}
        </ReactFlow>
      </div>
    </OverviewActions.Provider>
  );
}
