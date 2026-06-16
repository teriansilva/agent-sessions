import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  type Node,
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Bot, Boxes, ChevronsDownUp, ChevronsUpDown, FolderTree, Plus } from "lucide-react";
import {
  type FormEvent,
  type MouseEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { api, ApiError } from "../../lib/api";
import { engineColor } from "../../lib/format";
import {
  buildOverview,
  expandableKeys,
  type GroupBy,
  type ProjectGroupData,
  type SessionNodeData,
} from "../../lib/overviewGraph";
import type { ProjectRef, Session } from "../../types/api";
import { OverviewActionsCtx } from "./overviewActions";
import { ProjectGroupNode } from "./ProjectGroupNode";
import { SessionNode } from "./SessionNode";
import "./overview.css";

// Stable identity (module scope) so React Flow doesn't re-register node types each render.
const nodeTypes = { projectGroup: ProjectGroupNode, session: SessionNode };

// The map layout selector (#424 Phase 2) — one explicit grouping at a time.
const GROUP_MODES: { key: GroupBy; label: string; Icon: typeof FolderTree }[] = [
  { key: "folder", label: "Folders", Icon: FolderTree },
  { key: "project", label: "Projects", Icon: Boxes },
  { key: "agent", label: "Agents", Icon: Bot },
];

const miniMapColor = (n: Node): string =>
  n.type === "session" ? engineColor((n.data as SessionNodeData).session.engine) : "var(--border)";

type OverviewCanvasProps = {
  sessions: Session[];
  includeArchived?: boolean;
  partial?: boolean;
  compact?: boolean;
  onRefetch?: () => void;
};

/** The project-cluster canvas, shared by the fullscreen /overview route and the squeezed
 *  sidebar view. `compact` drops the minimap for the narrow sidebar column. Clusters collapse
 *  by default; their open/closed state + excluded projects are persisted per-user (#144).
 *  `onRefetch` re-pulls the session list after a mutation (create-project #361, reassign #424).
 *  Wrapped in a ReactFlowProvider so the drag-to-reassign handler can use `getIntersectingNodes`
 *  to find the project cluster a chip was dropped on (#424 Phase 5). */
export function OverviewCanvas(props: OverviewCanvasProps) {
  return (
    <ReactFlowProvider>
      <OverviewCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function OverviewCanvasInner({
  sessions,
  includeArchived = false,
  partial = false,
  compact = false,
  onRefetch,
}: OverviewCanvasProps) {
  const { expanded, excluded, projectsMode, includedProjects, projectNames, groupBy, setGroupBy, toggle, expandAll, collapseAll } =
    useOverviewPrefs();
  const navigate = useNavigate();
  const rf = useReactFlow();
  // Drag-to-reassign is live in Projects layout only — folder/agent clusters aren't user-assignable.
  const draggable = groupBy === "project";

  // The set of session cwds the map must DROP, resolved for the active mode (#335). `all` mode
  // drops the denylist (unchanged); `included` mode drops everything NOT in the allowlist. The map
  // takes an exclusion set, so we compute the mode-appropriate one here — keeping it in lockstep
  // with the server-filtered sidebar/facets.
  const dropped = useMemo(() => {
    if (projectsMode !== "included") return excluded;
    return new Set(sessions.map((s) => s.cwd).filter((cwd) => !includedProjects.has(cwd)));
  }, [projectsMode, excluded, includedProjects, sessions]);

  // Optimistic reassignment overlay (#424 Phase 5): a drop applies `project` locally at once so
  // the chip jumps to its new cluster, then the server write is awaited. Authoritative session
  // data (a refetch landing) clears the overlay; a failed write rolls its entry back.
  const [overrides, setOverrides] = useState<Map<string, ProjectRef>>(new Map());
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setOverrides(new Map());
  }, [sessions]);
  const effectiveSessions = useMemo(
    () =>
      overrides.size
        ? sessions.map((s) => {
            const ref = overrides.get(s.id);
            return ref ? { ...s, project: ref } : s;
          })
        : sessions,
    [sessions, overrides],
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
      buildOverview(effectiveSessions, {
        groupBy,
        includeArchived,
        expanded,
        excluded: dropped,
        activeId,
        names: projectNames,
        draggableSessions: draggable,
      }),
    [effectiveSessions, groupBy, includeArchived, expanded, dropped, activeId, projectNames, draggable],
  );

  // React Flow needs to own node positions to drag them, so mirror the derived graph into RF
  // state and re-sync whenever the layout is recomputed (mode/expand/reassign) — this also
  // snaps a dropped chip back to its computed slot.
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState(nodes);
  useEffect(() => {
    setRfNodes(nodes);
  }, [nodes, setRfNodes]);

  // Toggle keys available to expand (still visible) — drives "Expand all".
  const allKeys = useMemo(
    () => expandableKeys(sessions, dropped, groupBy),
    [sessions, dropped, groupBy],
  );

  const [dragErr, setDragErr] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  // A chip dropped onto a project cluster writes an explicit project_id via the metadata seam
  // (#424). Optimistic first, rolled back on failure.
  const reassign = useCallback(
    async (sid: string, target: Node) => {
      const data = target.data as ProjectGroupData;
      const pid = data.groupKey.replace(/^project:/, "");
      const ref: ProjectRef = { kind: "project", id: pid, name: data.project, color: data.color };
      setOverrides((prev) => new Map(prev).set(sid, ref));
      setDragErr(null);
      try {
        await api.setSessionProject(sid, pid);
        onRefetch?.(); // authoritative re-pull; the [sessions] effect clears the overlay
      } catch (ex) {
        setOverrides((prev) => {
          const next = new Map(prev);
          next.delete(sid);
          return next;
        });
        setDragErr(ex instanceof ApiError && ex.message ? ex.message : "Couldn’t move the session.");
      }
    },
    [onRefetch],
  );

  const onNodeDragStart = useCallback(() => setDragging(true), []);
  const onNodeDragStop = useCallback(
    (_e: MouseEvent, node: Node) => {
      setDragging(false);
      if (draggable && node.type === "session") {
        const target = rf
          .getIntersectingNodes(node)
          .find((n) => n.type === "projectGroup" && (n.data as ProjectGroupData).kind === "project");
        if (target) {
          const pid = (target.data as ProjectGroupData).groupKey.replace(/^project:/, "");
          const cur = (node.data as SessionNodeData).session.project;
          if (!(cur.kind === "project" && cur.id === pid)) {
            void reassign(node.id, target);
            return; // the optimistic overlay re-lays the chip into its new cluster
          }
        }
      }
      // No actionable target → snap the chip back to its computed slot.
      setRfNodes(nodes);
    },
    [draggable, rf, reassign, setRfNodes, nodes],
  );

  // All node interaction goes through React Flow's onNodeClick. This is required, not just
  // convenient: RF only sets pointer-events:all on a node when it's selectable/draggable OR
  // has a click handler — with selection disabled, a chip needs this to receive a click. A
  // click on a session chip opens it; a click on a cluster header toggles collapse (by groupKey).
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

  // Group nodes reach the sessions refetch via context (#361 Phase 4) — see overviewActions.
  const actions = useMemo(() => ({ refetchSessions: onRefetch ?? (() => {}) }), [onRefetch]);

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
      <div className={`tr-overview${dragging ? " tr-overview--dragging" : ""}`} style={{ position: "relative" }}>
        {partial && <div className="tr-ov-partial">Showing the most recent sessions</div>}
        <div className="tr-ov-toolbar">
          <div className="tr-ov-groupby" role="radiogroup" aria-label="Group sessions by">
            {GROUP_MODES.map(({ key, label, Icon }) => (
              <button
                key={key}
                type="button"
                role="radio"
                aria-checked={groupBy === key}
                aria-label={`Group by ${label.toLowerCase()}`}
                className={groupBy === key ? "on" : ""}
                onClick={() => setGroupBy(key)}
                title={`Group by ${label.toLowerCase()}`}
              >
                <Icon size={14} aria-hidden="true" />
                <span className="tr-ov-gb-label">{label}</span>
              </button>
            ))}
          </div>
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
        {(createErr || dragErr) && <div className="tr-ov-toolbar-err">{createErr || dragErr}</div>}
        {draggable && (
          <div className="tr-ov-hint" aria-hidden="true">
            Drag a session onto a project to move it
          </div>
        )}
        <ReactFlow
          nodes={rfNodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={onNodeClick}
          onNodesChange={onNodesChange}
          onNodeDragStart={onNodeDragStart}
          onNodeDragStop={onNodeDragStop}
          fitView
          fitViewOptions={{ padding: 0.18 }}
          minZoom={0.2}
          maxZoom={1.5}
          nodesDraggable={draggable}
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
