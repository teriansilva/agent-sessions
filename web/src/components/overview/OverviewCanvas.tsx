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
import { type MouseEvent, useCallback, useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { engineColor } from "../../lib/format";
import {
  buildOverview,
  type ProjectGroupData,
  type SessionNodeData,
} from "../../lib/overviewGraph";
import type { Session } from "../../types/api";
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
  const { expanded, excluded, projectsMode, includedProjects, projectNames, toggle, expandAll, collapseAll } =
    useOverviewPrefs();
  const navigate = useNavigate();
  // The set of session cwds the map must DROP, resolved for the active mode (#335). `all` mode
  // drops the denylist (unchanged); `included` mode drops everything NOT in the allowlist. The map
  // takes an exclusion set, so we compute the mode-appropriate one here — keeping it in lockstep
  // with the server-filtered sidebar/facets.
  const dropped = useMemo(() => {
    if (projectsMode !== "included") return excluded;
    return new Set(sessions.map((s) => s.cwd).filter((cwd) => !includedProjects.has(cwd)));
  }, [projectsMode, excluded, includedProjects, sessions]);

  // All node interaction goes through React Flow's onNodeClick. This is required, not just
  // convenient: RF only sets pointer-events:all on a node when it's selectable/draggable OR
  // has a click handler — with selection+drag disabled and no handler, every node would be
  // pointer-events:none and the inner buttons would be dead (#149/#152). A click on a session
  // chip opens it; a click on a cluster header toggles collapse.
  const onNodeClick = useCallback(
    (_e: MouseEvent, node: Node) => {
      if (node.type === "session") {
        const s = (node.data as SessionNodeData).session;
        navigate(`/s/${encodeURIComponent(s.engine)}/${encodeURIComponent(s.uuid)}`);
      } else if (node.type === "projectGroup") {
        toggle((node.data as ProjectGroupData).cwd);
      }
    },
    [navigate, toggle],
  );

  // The currently-open session ("engine:uuid"), parsed from /s/:engine/:id — its chip is
  // highlighted, in sync with the sidebar list's active row (#149).
  const { pathname } = useLocation();
  const activeId = useMemo(() => {
    const m = /^\/s\/([^/]+)\/([^/]+)\/?$/.exec(pathname);
    return m ? `${decodeURIComponent(m[1])}:${decodeURIComponent(m[2])}` : undefined;
  }, [pathname]);

  const { nodes, edges } = useMemo(
    () =>
      buildOverview(sessions, {
        includeArchived,
        expanded,
        excluded: dropped,
        activeId,
        names: projectNames,
      }),
    [sessions, includeArchived, expanded, dropped, activeId, projectNames],
  );
  // Cwds available to expand (still visible) — drives "Expand all".
  const allCwds = useMemo(
    () => [...new Set(sessions.filter((s) => !dropped.has(s.cwd)).map((s) => s.cwd))],
    [sessions, dropped],
  );

  if (!nodes.length) {
    return <div className="tr-overview tr-ov-state">No sessions to map yet.</div>;
  }

  return (
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
        onNodeClick={onNodeClick}
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
