import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  type Node,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ChevronsDownUp, ChevronsUpDown, Plus } from "lucide-react";
import { type FormEvent, type MouseEvent, useCallback, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { api, ApiError } from "../../lib/api";
import { engineColor } from "../../lib/format";
import {
  buildOverview,
  expandableKeys,
  type ProjectGroupData,
  type SessionNodeData,
} from "../../lib/overviewGraph";
import type { Session } from "../../types/api";
import { OverviewActionsCtx } from "./overviewActions";
import { ProjectGroupNode } from "./ProjectGroupNode";
import { SessionNode } from "./SessionNode";
import "./overview.css";

// Stable identity (module scope) so React Flow doesn't re-register node types each render.
const nodeTypes = { projectGroup: ProjectGroupNode, session: SessionNode };

const miniMapColor = (n: Node): string =>
  n.type === "session" ? engineColor((n.data as SessionNodeData).session.engine) : "var(--border)";

/** The project-cluster canvas, shared by the fullscreen /overview route and the squeezed
 *  sidebar view. `compact` drops the minimap for the narrow sidebar column. Clusters collapse
 *  by default; their open/closed state + excluded projects are persisted per-user (#144).
 *  `onRefetch` re-pulls the session list after a create-project mutation (#361 Phase 4). */
export function OverviewCanvas({
  sessions,
  includeArchived = false,
  partial = false,
  compact = false,
  onRefetch,
}: {
  sessions: Session[];
  includeArchived?: boolean;
  partial?: boolean;
  compact?: boolean;
  onRefetch?: () => void;
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
  // chip opens it; a click on a cluster header toggles collapse (by groupKey, #361 Phase 4:
  // the cwd for folder groups, project:<id> for entity groups).
  const onNodeClick = useCallback(
    (_e: MouseEvent, node: Node) => {
      if (node.type === "session") {
        const s = (node.data as SessionNodeData).session;
        navigate(`/s/${encodeURIComponent(s.engine)}/${encodeURIComponent(s.uuid)}`);
      } else if (node.type === "projectGroup") {
        toggle((node.data as ProjectGroupData).groupKey);
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
  // Toggle keys available to expand (still visible) — drives "Expand all".
  const allKeys = useMemo(
    () => expandableKeys(sessions, dropped),
    [sessions, dropped],
  );

  // Group nodes reach the sessions refetch via context (#361 Phase 4) — see overviewActions.
  const actions = useMemo(
    () => ({ refetchSessions: onRefetch ?? (() => {}) }),
    [onRefetch],
  );

  // "+ New project" (#361 Phase 4): a standalone entity (no folders) from an inline name
  // input in the toolbar. A 409 (duplicate name) carries the server's detail string.
  const [naming, setNaming] = useState(false);
  const [newName, setNewName] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  const [createErr, setCreateErr] = useState<string | null>(null);
  const submitNewProject = async (e: FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name || createBusy) return;
    setCreateBusy(true);
    setCreateErr(null);
    try {
      await api.createProject({ name });
      setNaming(false);
      setNewName("");
      onRefetch?.();
    } catch (ex) {
      setCreateErr(ex instanceof ApiError && ex.message ? ex.message : "Couldn’t create the project.");
    } finally {
      setCreateBusy(false);
    }
  };

  if (!nodes.length) {
    return <div className="tr-overview tr-ov-state">No sessions to map yet.</div>;
  }

  return (
    <OverviewActionsCtx.Provider value={actions}>
      <div className="tr-overview" style={{ position: "relative" }}>
        {partial && <div className="tr-ov-partial">Showing the most recent sessions</div>}
        <div className="tr-ov-toolbar">
          {naming ? (
            <form className="tr-ov-newproj" onSubmit={submitNewProject}>
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Project name"
                aria-label="Project name"
                autoFocus
              />
              <button type="submit" disabled={!newName.trim() || createBusy}>
                Create
              </button>
              <button
                type="button"
                title="Cancel"
                onClick={() => {
                  setNaming(false);
                  setNewName("");
                  setCreateErr(null);
                }}
              >
                ✕
              </button>
            </form>
          ) : (
            <button type="button" onClick={() => setNaming(true)} title="Create a project entity">
              <Plus size={14} /> New project
            </button>
          )}
          <button type="button" onClick={() => expandAll(allKeys)} title="Expand all projects">
            <ChevronsUpDown size={14} /> Expand all
          </button>
          <button type="button" onClick={collapseAll} title="Collapse all projects">
            <ChevronsDownUp size={14} /> Collapse all
          </button>
        </div>
        {createErr && <div className="tr-ov-toolbar-err">{createErr}</div>}
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
    </OverviewActionsCtx.Provider>
  );
}
