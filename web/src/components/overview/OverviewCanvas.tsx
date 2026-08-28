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
import {
  Bot,
  Boxes,
  ChevronsDownUp,
  ChevronsUpDown,
  FolderTree,
  Plus,
  SquareDashedBottom,
} from "lucide-react";
import {
  type FormEvent,
  type MouseEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { api, ApiError } from "../../lib/api";
import { engineColor } from "../../lib/format";
import {
  buildOverview,
  clusterKeyFor,
  DEFAULT_PROJECT_ID,
  expandableKeys,
  type GroupBy,
  type ProjectGroupData,
  type SessionNodeData,
} from "../../lib/overviewGraph";
import { useIsMobile } from "../../lib/useIsMobile";
import type { ProjectRef, Session } from "../../types/api";
import { anchorPointOf } from "./nodeAnchor";
import { OverviewActionsCtx } from "./overviewActions";
import { ProjectGroupNode } from "./ProjectGroupNode";
import { SessionNode } from "./SessionNode";
import { useWorkspace } from "./useWorkspace";
import { WindowLayer } from "./WindowLayer";
import { canHostWindow, WINDOW_CAP } from "./workspace";
import "./overview.css";

// Stable identity (module scope) so React Flow doesn't re-register node types each render.
const nodeTypes = { projectGroup: ProjectGroupNode, session: SessionNode };

// The map layout selector (#424 Phase 2) — one explicit grouping at a time.
const GROUP_MODES: { key: GroupBy; label: string; Icon: typeof FolderTree }[] =
  [
    { key: "folder", label: "Folders", Icon: FolderTree },
    { key: "project", label: "Projects", Icon: Boxes },
    { key: "agent", label: "Agents", Icon: Bot },
  ];

const miniMapColor = (n: Node): string =>
  n.type === "session"
    ? engineColor((n.data as SessionNodeData).session.engine)
    : "var(--border)";

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
  const {
    expanded,
    excluded,
    projectsMode,
    includedProjects,
    projectNames,
    groupBy,
    setGroupBy,
    toggle,
    expandAll,
    collapseAll,
  } = useOverviewPrefs();
  const navigate = useNavigate();
  const rf = useReactFlow();
  // Drag-to-reassign is live in Projects layout only — folder/agent clusters aren't user-assignable.
  const draggable = groupBy === "project";

  // ---- Window workspace (#208) -------------------------------------------------------------
  // Desktop, fullscreen map only: `compact` is the squeezed embed, and the ≤800px breakpoint is
  // the shell's own (useIsMobile) rather than a second predicate that could drift from it.
  // Where the workspace is off, a chip click navigates exactly as it always has.
  const isMobile = useIsMobile();
  const windowsOn = !compact && !isMobile;
  const ws = useWorkspace();
  // The actions are stable `useCallback`s; naming them here keeps them out of the dependency
  // arrays as `ws.*` (which changes identity whenever a window moves).
  const { open: openWindow, close: closeWindow, syncTitles } = ws;
  const wrapRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const layerRef = useRef<HTMLDivElement>(null);
  // The overlay box: screen origin (the shell offset every projection subtracts) + size, with
  // the top inset below the floating toolbar so a window's chrome can never hide under it.
  const [box, setBox] = useState({ x: 0, y: 0, w: 0, h: 0, top: 0 });
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      const bar = barRef.current?.getBoundingClientRect();
      const top = bar ? Math.max(0, bar.bottom - r.top + 8) : 0;
      const next = {
        x: r.left,
        y: r.top + top,
        w: Math.round(r.width),
        h: Math.max(0, Math.round(r.height - top)),
        top: Math.round(top),
      };
      // Same-value guard: a ResizeObserver fires on layout, and re-setting an identical box
      // would re-render (and re-project) on every one of them.
      setBox((cur) =>
        cur.x === next.x &&
        cur.y === next.y &&
        cur.w === next.w &&
        cur.h === next.h &&
        cur.top === next.top
          ? cur
          : next,
      );
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (barRef.current) ro.observe(barRef.current);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);


  // Non-archived project entities (#447) → empty ones still render as drag-target clusters in
  // Projects mode. Fetched here (sessions alone can't surface a 0-session project) and refreshed
  // after create/reassign so a just-made project appears immediately.
  const [projects, setProjects] = useState<
    { id: string; name: string; color?: string }[]
  >([]);
  const fetchProjects = useCallback(() => {
    api
      .projectEntities()
      .then((r) =>
        setProjects(
          r.projects
            .filter((p) => !p.archived)
            .map((p) => ({
              id: p.id,
              name: p.name,
              color: p.color || undefined,
            })),
        ),
      )
      .catch(() => {});
  }, []);
  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  // The set of session cwds the map must DROP, resolved for the active mode (#335). `all` mode
  // drops the denylist (unchanged); `included` mode drops everything NOT in the allowlist. The map
  // takes an exclusion set, so we compute the mode-appropriate one here — keeping it in lockstep
  // with the server-filtered sidebar/facets.
  const dropped = useMemo(() => {
    if (projectsMode !== "included") return excluded;
    return new Set(
      sessions.map((s) => s.cwd).filter((cwd) => !includedProjects.has(cwd)),
    );
  }, [projectsMode, excluded, includedProjects, sessions]);

  // Optimistic reassignment overlay (#424 Phase 5): a drop applies `project` locally at once so
  // the chip jumps to its new cluster, then the server write is awaited. Authoritative session
  // data (a refetch landing) clears the overlay; a failed write rolls its entry back.
  const [overrides, setOverrides] = useState<Map<string, ProjectRef>>(
    new Map(),
  );
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
    return m
      ? `${decodeURIComponent(m[1])}:${decodeURIComponent(m[2])}`
      : undefined;
  }, [pathname]);

  const bounds = useMemo(() => ({ w: box.w, h: box.h }), [box.w, box.h]);
  // OPENING needs a map area that can hold a window at its floor: ≤800px is mobile, but 801px
  // with the sidebar expanded leaves ~460px of map, which is below MIN_SIZE and would hand the
  // agent a column count the floor exists to prevent. There the chip navigates, as it always did.
  const canOpenWindow = windowsOn && canHostWindow(bounds);
  // MOUNTING is a different question, deliberately: an open window must survive the map getting
  // small (or emptying out entirely) — unmounting the layer would close its socket and kill the
  // session's live view over a layout change.
  //
  // Note what this does NOT depend on: `windowsOn`, and therefore not on the mobile breakpoint.
  // Gating the mount on it meant dragging a desktop window from 801px to 800px closed every
  // open socket — the same defect as the empty map, at a different boundary, and contradicting
  // the sentence above. The breakpoint decides whether a window can be OPENED; on a real phone
  // `windows` is therefore always empty and this layer never mounts, which is the mobile
  // contract. The only way to be under it with windows open is to have crossed it in a
  // resizable browser, and a resize is not a reason to kill a live session.
  const layerOn = !compact && (canOpenWindow || ws.windows.length > 0);

  // Renames (the sidebar's, or an AI title landing) reach an OPEN window's chrome: the live
  // index goes to the workspace, which keeps each window's own title current. Resolving
  // live-or-captured at render time instead would make a window REVERT to its old name the
  // moment its session left the map.
  const titles = useMemo(
    () =>
      new Map(
        effectiveSessions.map((s) => [s.id, s.title || s.short_uuid] as const),
      ),
    [effectiveSessions],
  );
  useEffect(() => {
    syncTitles(titles);
  }, [titles, syncTitles]);

  // Chips whose session is open as a window (#208) — the map marks them.
  const openIds = useMemo(
    () => new Set(ws.windows.map((w) => w.key)),
    [ws.windows],
  );

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
        projects,
        openIds,
      }),
    [
      effectiveSessions,
      groupBy,
      includeArchived,
      expanded,
      dropped,
      activeId,
      projectNames,
      draggable,
      projects,
      openIds,
    ],
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
    () => expandableKeys(sessions, dropped, groupBy, projects),
    [sessions, dropped, groupBy, projects],
  );

  const [dragErr, setDragErr] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  // A chip dropped onto a project cluster writes an explicit project_id via the metadata seam
  // (#424). Dropping onto the synthetic Default cluster (#445) CLEARS the assignment instead —
  // the server rejects `project_id="__default__"` (only ""/null mean "unassign/default"), so the
  // session reverts to its folder/owning resolution. Optimistic first, rolled back on failure.
  const reassign = useCallback(
    async (sid: string, cwd: string, target: Node) => {
      const data = target.data as ProjectGroupData;
      const pid = data.groupKey.replace(/^project:/, "");
      const toDefault = pid === DEFAULT_PROJECT_ID;
      const ref: ProjectRef = toDefault
        ? { kind: "folder", id: cwd, name: cwd } // cleared → folder fallback until the refetch lands
        : { kind: "project", id: pid, name: data.project, color: data.color };
      setOverrides((prev) => new Map(prev).set(sid, ref));
      setDragErr(null);
      try {
        await api.setSessionProject(sid, toDefault ? "" : pid);
        onRefetch?.(); // authoritative re-pull; the [sessions] effect clears the overlay
        fetchProjects(); // a project may have just emptied/filled (#447)
      } catch (ex) {
        setOverrides((prev) => {
          const next = new Map(prev);
          next.delete(sid);
          return next;
        });
        setDragErr(
          ex instanceof ApiError && ex.message
            ? ex.message
            : "Couldn’t move the session.",
        );
      }
    },
    [onRefetch, fetchProjects],
  );

  const onNodeDragStart = useCallback(() => setDragging(true), []);
  const onNodeDragStop = useCallback(
    (_e: MouseEvent, node: Node) => {
      setDragging(false);
      if (draggable && node.type === "session") {
        const target = rf
          .getIntersectingNodes(node)
          .find(
            (n) =>
              n.type === "projectGroup" &&
              (n.data as ProjectGroupData).kind === "project",
          );
        if (target) {
          const pid = (target.data as ProjectGroupData).groupKey.replace(
            /^project:/,
            "",
          );
          const session = (node.data as SessionNodeData).session;
          const cur = session.project;
          // Dropping onto Default = clear assignment; a folder-fallback session is already in
          // Default, so that's a no-op. Onto a user project = assign, unless already there.
          const alreadyThere =
            pid === DEFAULT_PROJECT_ID
              ? cur.kind === "folder"
              : cur.kind === "project" && cur.id === pid;
          if (!alreadyThere) {
            void reassign(node.id, session.cwd, target);
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
        // #208 decision 1: on the fullscreen desktop map a chip OPENS A WINDOW; the window's
        // ⤢ is the way to the full-screen route. The chip only has one press gesture left
        // (press+move is already drag-to-reassign), so the workspace takes it rather than
        // crowding a 240px chip with a second target. Everywhere else — the squeezed embed,
        // mobile — the click navigates exactly as it always did.
        if (canOpenWindow) {
          openWindow(
            s,
            anchorPointOf(node, rf.getNode, rf.flowToScreenPosition, {
              x: box.x,
              y: box.y,
            }),
            bounds,
          );
          return;
        }
        navigate(
          `/s/${encodeURIComponent(s.engine)}/${encodeURIComponent(s.uuid)}`,
        );
      } else if (node.type === "projectGroup") {
        toggle((node.data as ProjectGroupData).groupKey);
      }
    },
    [navigate, toggle, canOpenWindow, openWindow, rf, box.x, box.y, bounds],
  );

  // Which map node a window's tether may attach to, best first: its own chip, else the cluster
  // that chip collapses into. Neither on the map (filtered, archived, another layout) → the
  // tether simply isn't drawn, and the window stays open and usable.
  const anchorCandidates = useCallback(
    (key: string) => {
      const s = effectiveSessions.find((x) => x.id === key);
      return s ? [key, `group:${clusterKeyFor(s, groupBy)}`] : [key];
    },
    [effectiveSessions, groupBy],
  );

  // ⤢ — hand this session to the full-screen route. The window closes with it; leaving
  // /overview unmounts the whole workspace, so the route's socket is the only one left.
  // Synced in an effect, never during render (the repo's `jiggleRef` idiom) — see Terminal.tsx.
  const windowsRef = useRef(ws.windows);
  useEffect(() => {
    windowsRef.current = ws.windows;
  });
  const onFullScreen = useCallback(
    (key: string) => {
      // Resolved through a ref so this handler stays stable across window state changes — an
      // unstable one would re-render every mounted pane on every drag frame.
      const w = windowsRef.current.find((x) => x.key === key);
      if (!w) return;
      closeWindow(key);
      navigate(`/s/${encodeURIComponent(w.engine)}/${encodeURIComponent(w.id)}`);
    },
    [navigate, closeWindow],
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
      fetchProjects(); // the new (empty) project should appear as a cluster at once (#447)
    } catch (ex) {
      setCreateErr(
        ex instanceof ApiError && ex.message
          ? ex.message
          : "Couldn’t create the project.",
      );
    } finally {
      setCreateBusy(false);
    }
  };

  // An empty map is a STATE, not a different component — while windows are open. Returning a
  // bare message here (as this did) unmounted the window layer with it, which closed every
  // window's socket and killed the live view because the last chip got filtered off the map.
  // #208's contract is the opposite: a map filter never closes a window. With no windows open,
  // the bare message is exactly what it always was.
  const emptyMap = !nodes.length;
  if (emptyMap && !layerOn) {
    return (
      <div className="tr-overview tr-ov-state">No sessions to map yet.</div>
    );
  }

  return (
    <OverviewActionsCtx.Provider value={actions}>
      <div
        ref={wrapRef}
        className={`tr-overview${dragging ? " tr-overview--dragging" : ""}`}
        style={{ position: "relative" }}
      >
        {partial && (
          <div className="tr-ov-partial">Showing the most recent sessions</div>
        )}
        <div className="tr-ov-toolbar" ref={barRef}>
          <div
            className="tr-ov-groupby"
            role="radiogroup"
            aria-label="Group sessions by"
          >
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
            <button
              type="button"
              onClick={() => setNaming(true)}
              title="Create a project entity"
            >
              <Plus size={14} /> New project
            </button>
          )}
          <button
            type="button"
            onClick={() => expandAll(allKeys)}
            title="Expand all projects"
          >
            <ChevronsUpDown size={14} /> Expand all
          </button>
          <button
            type="button"
            onClick={collapseAll}
            title="Collapse all projects"
          >
            <ChevronsDownUp size={14} /> Collapse all
          </button>
          {layerOn && (
            <>
              <span
                className="tr-ov-wins"
                data-window-readout
                title={`${ws.windows.length} of ${WINDOW_CAP} session windows open`}
              >
                <SquareDashedBottom size={14} aria-hidden="true" /> Windows{" "}
                <b>{ws.windows.length}</b>/{WINDOW_CAP}
              </span>
              {ws.windows.length > 0 && (
                <button
                  type="button"
                  onClick={ws.closeAll}
                  title="Close every open session window"
                  data-window-close-all
                >
                  Close all
                </button>
              )}
            </>
          )}
        </div>
        {(createErr || dragErr) && (
          <div className="tr-ov-toolbar-err">{createErr || dragErr}</div>
        )}
        {draggable && (
          <div className="tr-ov-hint" aria-hidden="true">
            Drag a session onto a project to move it
          </div>
        )}
        {emptyMap && (
          <div className="tr-ov-state tr-ov-state--overlay">
            No sessions to map yet — the open windows below stay live.
          </div>
        )}
        {!emptyMap && (
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
          <Background
            variant={BackgroundVariant.Dots}
            gap={22}
            size={1}
            color="var(--border)"
          />
          <Controls
            showInteractive={false}
            position={compact ? "bottom-right" : "bottom-left"}
          />
          {!compact && (
            <MiniMap
              pannable
              zoomable
              nodeColor={miniMapColor}
              maskColor="rgba(0,0,0,0.45)"
            />
          )}
        </ReactFlow>
        )}
        {/* Outside <ReactFlow>, deliberately: inside it the terminal would inherit the zoom
            transform, which is what makes xterm blurry and breaks its fit/selection/input
            maths. Only the tether is re-projected as the map moves (#208). */}
        {layerOn && (
          <WindowLayer
            layerRef={layerRef}
            windows={ws.windows}
            focusedKey={ws.focusedKey}
            flashKey={ws.flashKey}
            notice={ws.notice}
            bounds={bounds}
            originX={box.x}
            originY={box.y}
            topInset={box.top}
            anchorCandidates={anchorCandidates}
            onFocus={ws.focus}
            onClose={ws.close}
            onFullScreen={onFullScreen}
            onRect={ws.setRect}
            onRole={ws.setRole}
          />
        )}
      </div>
    </OverviewActionsCtx.Provider>
  );
}
