import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  type Node,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";
import { engineColor } from "../../lib/format";
import { buildOverview, type SessionNodeData } from "../../lib/overviewGraph";
import type { Session } from "../../types/api";
import { ProjectGroupNode } from "./ProjectGroupNode";
import { SessionNode } from "./SessionNode";
import "./overview.css";

// Stable identity (module scope) so React Flow doesn't re-register node types each render.
const nodeTypes = { projectGroup: ProjectGroupNode, session: SessionNode };

const miniMapColor = (n: Node): string =>
  n.type === "session" ? engineColor((n.data as SessionNodeData).session.engine) : "var(--border)";

/** The project-cluster canvas, shared by the fullscreen /overview route and the squeezed
 *  sidebar view. `compact` drops the minimap for the narrow sidebar column. */
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
  const { nodes, edges } = useMemo(
    () => buildOverview(sessions, { includeArchived }),
    [sessions, includeArchived],
  );

  if (!nodes.length) {
    return <div className="tr-overview tr-ov-state">No sessions to map yet.</div>;
  }

  return (
    <div className="tr-overview" style={{ position: "relative" }}>
      {partial && <div className="tr-ov-partial">Showing the most recent sessions</div>}
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
  );
}
