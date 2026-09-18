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
  useStoreApi,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  Bot,
  Boxes,
  ChevronsDownUp,
  ChevronsUpDown,
  FolderTree,
  Minus,
  Plus,
  RotateCcw,
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
import { flushSync } from "react-dom";
import { useLocation, useNavigate } from "react-router-dom";
import { useConfig } from "../../app/config";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { isNewSessionPlaceholder } from "../../app/sessionsStore";
import { useWorkspaceCtx } from "../../app/workspaceWindows";
import { api, ApiError } from "../../lib/api";
import { engineColor } from "../../lib/format";
import {
  buildOverview,
  clusterKeyFor,
  DEFAULT_PROJECT_ID,
  expandableKeys,
  type GroupBy,
  sessionOnMap,
  type ProjectGroupData,
  type SessionNodeData,
} from "../../lib/overviewGraph";
import { useIsMobile } from "../../lib/useIsMobile";
import type { ProjectRef, Session } from "../../types/api";
import type { MenuAnchor } from "../sidebar/RowMenu";
import { MapSessionMenu } from "./MapSessionMenu";
import {
  applyPins,
  loadPins,
  savePins,
  withoutLayout,
  withPin,
} from "./mapLayout";
import { restoreFocus, windowsForSession } from "./mapMenu";
import { anchorPointOf } from "./nodeAnchor";
import { OverviewActionsCtx } from "./overviewActions";
import { ProjectGroupNode } from "./ProjectGroupNode";
import { SessionNode } from "./SessionNode";
import type { WindowSeed } from "./useWorkspace";
import { WindowLayer } from "./WindowLayer";
import {
  canHostWindow,
  WINDOW_CAP_MAX,
  WINDOW_CAP_MIN,
} from "./workspace";
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

/** Below this zoom a chip cannot hold a 44-screen-px ⋯ target beside a chip-body target (#968):
 *  44px / 0.55 = 80 flow px, the chip's full height. The CSS hides the touch ⋯ under it. */
const KEBAB_TOUCH_MIN_ZOOM = 44 / 80;

const LAYOUTS: readonly GroupBy[] = ["folder", "project", "agent"];

/** A session row → the seed a window opens from (#936). One mapping, shared by the chip click
 *  and the drained requests, so the map cannot open a window under a different identity than the
 *  sidebar asked for. */
const seedOf = (s: Session): WindowSeed => ({
  key: s.id,
  engine: s.engine,
  id: s.uuid,
  title: s.title || s.short_uuid,
});

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
  // The workspace lives in a provider above the router now (#936), so the records survive
  // leaving the map — this canvas is a consumer, not the owner. What it still owns is all the
  // GEOMETRY: measurement, projection, the tether, and the decision of whether a window may open
  // at all. Those are facts about a mounted, measured map, and nothing outside it can know them.
  const ws = useWorkspaceCtx();
  // The reconcile action alone: a stable callback (the workspace memoizes every action), so the
  // reconcile wrapper below doesn't have to ride the whole workspace object's identity.
  const { reconcile: wsReconcile } = ws;
  // The actions are stable `useCallback`s; naming them here keeps them out of the dependency
  // arrays as `ws.*` (which changes identity whenever a window moves).
  const {
    open: openWindow,
    syncTitles,
    setMapReady,
    restore: restoreWindows,
    detach,
    drain,
    clearRejected,
    setCap,
    close: closeWindow,
  } = ws;
  const wrapRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const layerRef = useRef<HTMLDivElement>(null);
  // The overlay box: screen origin (the shell offset every projection subtracts) + size, with
  // the top inset below the floating toolbar so a window's chrome can never hide under it.
  const [box, setBox] = useState({ x: 0, y: 0, w: 0, h: 0, top: 0 });
  // Has a measurement HAPPENED? Tracked separately from the dimensions it produced, because the
  // two are not the same question and conflating them hid a real case: the usable height
  // legitimately clamps to zero when the toolbar chrome consumes the whole map area, so
  // `box.h > 0` reads a completed measurement of an unusable box as "not measured yet" and
  // suppresses the cannot-host fallback forever (Hermes on #939, round 2). "Unmeasured" must
  // still mean unmeasured, though — the first effect pass genuinely sees zeroes, and treating
  // that as a refusal would bounce every arrival straight off the map.
  const [measured, setMeasured] = useState(false);
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
      setMeasured(true);
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

  // Touch targets after zoom (#968): the chip ⋯ is counter-scaled by the viewport zoom so its hit
  // square stays ≥44 SCREEN px, and hides below the zoom where it cannot sit beside a chip-body
  // target. Written straight onto the wrapper from a store subscription — a React subscription
  // would re-render the canvas on every zoom frame for a value only CSS reads. A data attribute,
  // not a class: React owns `className` here and would overwrite a class on its next change.
  const storeApi = useStoreApi();
  useEffect(() => {
    const apply = (zoom: number) => {
      const el = wrapRef.current;
      if (!el) return;
      el.style.setProperty("--ov-zoom", String(zoom));
      if (zoom < KEBAB_TOUCH_MIN_ZOOM) el.dataset.zoomFar = "";
      else delete el.dataset.zoomFar;
    };
    apply(storeApi.getState().transform[2]);
    return storeApi.subscribe((st) => apply(st.transform[2]));
  }, [storeApi]);


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


  // Publish "a mounted map can host a window" so the surfaces OUTSIDE the map — the sidebar row,
  // the pane's To-map chip, the new-session landing — can decide between opening a window and
  // navigating without re-deriving the breakpoint and the box they cannot see (#936). Cleared on
  // unmount, so leaving `/overview` puts every one of them back to plain navigation.
  useEffect(() => {
    // Only publish a MEASUREMENT once the box has actually been measured — the first effect pass
    // still sees zeroes, and recording that as "cannot host" would withhold the pane's To-map chip
    // on a map that is perfectly capable of hosting.
    if (measured) setMapReady(canOpenWindow);
    // `null`, not `false`: this is an unmount, not a measurement. `false` here would make every
    // navigation away from the map record it as unable to host one, which is what the pane reads
    // to decide whether to offer To map at all.
    return () => setMapReady(null);
  }, [canOpenWindow, measured, setMapReady]);

  // Unmounting the map takes every `<Terminal>` with it, which is exactly when a window's frozen
  // transport identity stops being load-bearing and starts being a hazard: a window launched under
  // a `new-<uuid>` placeholder would come back mounting the placeholder AND its `fresh` params,
  // and send `new=1` a second time. `detach` normalises each record onto the id its engine
  // actually minted. Mount-only, so it fires on the unmount and nowhere else.
  useEffect(() => detach, [detach]);

  // Apply the stored layout (#936, delivering #872) — once, on the first measured box that can
  // host a window. Deliberately NOT at provider construction: `canHostWindow` and the mobile
  // breakpoint are this canvas's to evaluate, and a desktop layout applied sight-unseen would
  // mount eight terminals on a phone. `restore` is idempotent, so StrictMode's double effect is
  // a no-op, and it also marks the workspace hydrated — which is what unblocks persistence, so
  // an empty stored layout still has to go through it.
  useEffect(() => {
    if (!canOpenWindow || ws.hydrated) return;
    restoreWindows(bounds);
  }, [canOpenWindow, ws.hydrated, bounds, restoreWindows]);

  // Drain the queue from outside the map. Resolving the anchor is why this cannot happen at the
  // call site: it needs the chip's projected position, which only a measured, mounted canvas has.
  //
  // The whole queue goes through ONE atomic transition, which returns an explicit
  // accepted/rejected outcome per request. That matters more than it looks: the two ways a
  // request can fail — the map cannot host a window at all, or the cap is already full — used to
  // be handled in two places, and the second one silently cleared the queue. A caller decided
  // this session should be on screen, and a fresh one carries the cwd and bypass the operator had
  // just chosen; neither may evaporate (Hermes on #939, rounds 1 + 2).
  const pending = ws.pending;
  useEffect(() => {
    if (!pending.length || !measured) return;
    const anchors = new Map(
      pending.map((req) => {
        const node = canOpenWindow ? rf.getNode(req.seed.key) : undefined;
        return [
          req.seed.key,
          node
            ? anchorPointOf(node, rf.getNode, rf.flowToScreenPosition, {
                x: box.x,
                y: box.y,
              })
            : null,
        ] as const;
      }),
    );
    drain(anchors, bounds, canOpenWindow);
  }, [pending, canOpenWindow, measured, drain, rf, box.x, box.y, bounds]);

  // A refused request is handed BACK to the route it came from — the full-screen pane, carrying
  // `fresh` so the launch still happens. Anything else loses work the operator has already done.
  //
  // Only the first can be handed back; there is one screen. In practice there is never more than
  // one, because every caller queues in response to a single press and navigates here at once.
  const rejected = ws.rejected;
  useEffect(() => {
    if (!rejected.length) return;
    const [first] = rejected;
    clearRejected();
    navigate(
      `/s/${encodeURIComponent(first.seed.engine)}/${encodeURIComponent(first.seed.id)}`,
      first.fresh ? { state: { fresh: first.fresh } } : undefined,
    );
  }, [rejected, clearRejected, navigate]);

  // Renames reach an OPEN window's chrome through the MAP's list: the live index goes to the
  // workspace, which keeps each window's own title current. The map's list updates on its own
  // revalidations (the menu's mutations, a reconcile-triggered refetch below, a route
  // re-entry) — a rename that only the sidebar's poll has seen so far lands with the map's
  // next one; the window never reverts either way. Resolving live-or-captured at render time
  // instead would make a window REVERT to its old name the moment its session left the map.
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
  //
  // Memoized on a STRING SIGNATURE of the open keys, never on `ws.windows` (#936). That array
  // gets a new identity on every `rect` dispatch, i.e. on every pointer move of a window drag,
  // and this set feeds `buildOverview` — so the old dependency rebuilt the entire node array
  // ~60 times a second while a window was being dragged. React Flow renders an unmeasured node
  // with `visibility: hidden`, and a fresh array discards the measurements it had, so the whole
  // map went BLANK for the length of the gesture and reappeared when it ended.
  //
  // The rule this encodes is the general one, not a patch for one dependency: **a gesture on
  // the overlay must not rebuild the map's node array.** The set of open sessions genuinely
  // does not change when one of them is dragged, so its identity must not either.
  //
  // `actionKey`, not `key`: after a converge the chip on the map carries the engine's real id.
  const openSig = ws.windows
    .map((w) => w.actionKey)
    .sort()
    .join("\u0000");
  const openIds = useMemo(
    () => new Set(openSig ? openSig.split("\u0000") : []),
    [openSig],
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
  //
  // Clusters the operator moved (#968) are PINNED, per layout, on this device (`mapLayout.ts`).
  // The pins live in a ref rather than among `buildOverview`'s inputs, and that is the #936 rule
  // applied to a drop: React Flow already holds the dropped position, so recording a pin must not
  // rebuild the node array — a fresh array re-measures every node and the map blanks while it
  // does. The ref is folded in here, whenever the graph genuinely rebuilds.
  const [initialPins] = useState(loadPins);
  const pinsRef = useRef(initialPins);
  // Which layouts have pins at all — Reset layout's visibility and nothing else. Its own small
  // state, so showing the control never touches the graph.
  const [pinnedLayouts, setPinnedLayouts] = useState<Set<GroupBy>>(
    () =>
      new Set(LAYOUTS.filter((l) => Object.keys(initialPins[l]).length > 0)),
  );
  const hasPins = pinnedLayouts.has(groupBy);
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState(
    applyPins(nodes, initialPins[groupBy]),
  );
  useEffect(() => {
    setRfNodes(applyPins(nodes, pinsRef.current[groupBy]));
  }, [nodes, groupBy, setRfNodes]);

  // Reset layout: this layout's clusters back to their computed slots — the one deliberate
  // rebuild the pins ever cause.
  const resetLayout = useCallback(() => {
    pinsRef.current = withoutLayout(pinsRef.current, groupBy);
    savePins(pinsRef.current);
    setPinnedLayouts((prev) => {
      const next = new Set(prev);
      next.delete(groupBy);
      return next;
    });
    setRfNodes(nodes);
  }, [groupBy, nodes, setRfNodes]);

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

  // The drop-target outline is a CHIP-drag signal: a cluster is never dropped onto anything.
  const onNodeDragStart = useCallback((_e: MouseEvent, node: Node) => {
    if (node.type === "session") setDragging(true);
  }, []);
  const onNodeDragStop = useCallback(
    (_e: MouseEvent, node: Node) => {
      setDragging(false);
      if (node.type === "projectGroup") {
        // A cluster drop (#968): pin it where it landed and return BEFORE the chip snap-back
        // below. Nothing is rebuilt — React Flow already shows the cluster there.
        pinsRef.current = withPin(
          pinsRef.current,
          groupBy,
          (node.data as ProjectGroupData).groupKey,
          node.position,
        );
        savePins(pinsRef.current);
        setPinnedLayouts((prev) =>
          prev.has(groupBy) ? prev : new Set(prev).add(groupBy),
        );
        return;
      }
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
      // No actionable target → snap the chip back to its computed slot (clusters keep their pins).
      setRfNodes(applyPins(nodes, pinsRef.current[groupBy]));
    },
    [draggable, rf, reassign, setRfNodes, nodes, groupBy],
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
            seedOf(s),
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
  //
  // Called with a window's `actionKey`, never its transport `key`: a window launched under a
  // `new-` placeholder has no chip until the engine reconciles, and the chip it then gets carries
  // the real id (#936).
  const anchorCandidates = useCallback(
    (key: string) => {
      const s = effectiveSessions.find((x) => x.id === key);
      return s ? [key, `group:${clusterKeyFor(s, groupBy)}`] : [key];
    },
    [effectiveSessions, groupBy],
  );

  // The route's refetch, ref-synced so the reconcile wrapper below stays stable across
  // refetch identity changes — the same idiom as `windowsRef` above (an unstable prop would
  // re-render every mounted pane each time the route re-creates its callback).
  const refetchRef = useRef(onRefetch);
  useEffect(() => {
    refetchRef.current = onRefetch;
  });
  // A window launched from this map adopting its engine-minted id (#127/#315) — reconcile as
  // today, then let the MAP's list learn the row (#1037). The map has no poll (#1007), so
  // every window surface that reads the list — the title sync, the chip the tether anchors
  // to, the ⋯ menu's isOnMap, the chip's open marker — would otherwise keep aiming at the
  // `new-` placeholder until the operator happened to leave and re-enter. One refetch,
  // bounded by the reconcile event itself (the {"t":"id"} frame is emitted once per launch;
  // reconnects of a converged placeholder resolve via the persisted alias and never re-fire
  // it). The server persists the alias and busts the scan cache BEFORE that frame
  // (main._reconcile_new_session), so this revalidation is guaranteed to re-walk WITH the
  // new row; on failure the retained-list hook keeps the previous map (#1007). The refetch
  // touches the LIST, never the socket — the window's transport identity and its live
  // connection are untouched.
  const onWindowReconcile = useCallback(
    (key: string, sid: string) => {
      // Defensive first: the server never converges to a placeholder, and adopting one would
      // both un-name the record and (below) never refetch — so a placeholder frame is a
      // complete no-op here. The helper takes the complete composite key as the frame carries
      // it (web/src/app/sessionsStore.ts).
      if (isNewSessionPlaceholder(sid)) return;
      wsReconcile(key, sid);
      refetchRef.current?.();
    },
    [wsReconcile],
  );

  // ⤢ — hand this session to the full-screen route.
  //
  // The window RECORD stays (#936). It used to be closed here, which was right while the
  // workspace died with the map anyway; now that it survives, closing would make ⤢ a one-way
  // door in a system that has a door back (the pane's "To map" chip), and the round trip would
  // silently lose the window's place. Nothing is double-mounted by keeping it: the router renders
  // one route at a time, so leaving `/overview` unmounts the layer and its socket regardless.
  //
  // The record is resolved through a ref so this handler stays stable across window state
  // changes — an unstable one would re-render every mounted pane on every drag frame. Synced in
  // an effect, never during render (the repo's `jiggleRef` idiom) — see Terminal.tsx.
  const windowsRef = useRef(ws.windows);
  useEffect(() => {
    windowsRef.current = ws.windows;
  });
  const onFullScreen = useCallback(
    (key: string) => {
      const w = windowsRef.current.find((x) => x.key === key);
      if (!w) return;
      // The ROUTE gets the id the server should act on — after a converge that is the engine's
      // real id, and navigating to the frozen placeholder would land on a session that does not
      // exist (#867).
      const [engine, ...rest] = w.actionKey.split(":");
      const id = rest.join(":");
      navigate(
        `/s/${encodeURIComponent(engine || w.engine)}/${encodeURIComponent(id || w.id)}`,
      );
    },
    [navigate],
  );

  // ---- The session menu (#968) --------------------------------------------------------------
  // ONE menu for the whole map — the sidebar row's (`useSessionMenu`) — opened from a chip's ⋯, a
  // right-click on a chip, or a window's chrome. The target captures the row it opened on, so a
  // dialog already open keeps naming its session after a refetch drops the chip; the POPOVER only
  // shows while the session is still on the map, so a menu never outlives its session.
  // The rows actually DRAWN in this layout — the graph's own predicate (#968 review), so a menu's
  // availability can never disagree with the chips: a hidden folder's session has no chip in
  // Folders layout, and its window's ⋯ must say so. Collapse does not count; see `sessionOnMap`.
  const mapRows = useMemo(
    () =>
      effectiveSessions.filter((s) =>
        sessionOnMap(s, { groupBy, includeArchived, excluded: dropped }),
      ),
    [effectiveSessions, groupBy, includeArchived, dropped],
  );
  const rowsRef = useRef(mapRows);
  useEffect(() => {
    rowsRef.current = mapRows;
  });
  const [menuTarget, setMenuTarget] = useState<{
    key: string;
    anchor: MenuAnchor | null;
    opener: HTMLElement | null;
    row: Session;
  } | null>(null);
  const openSessionMenu = useCallback(
    (key: string, anchor: MenuAnchor, opener: HTMLElement | null) => {
      const row = rowsRef.current.find((s) => s.id === key);
      if (row) setMenuTarget({ key, anchor, opener, row });
    },
    [],
  );
  const menuRow = menuTarget
    ? mapRows.find((s) => s.id === menuTarget.key)
    : undefined;
  const openerRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    openerRef.current = menuTarget?.opener ?? null;
  });
  const onMenuClose = useCallback((refocus: boolean) => {
    setMenuTarget((t) => (t ? { ...t, anchor: null } : t));
    if (refocus) restoreFocus(openerRef.current, wrapRef.current);
  }, []);
  const onMenuDone = useCallback(() => setMenuTarget(null), []);

  // Right-click a chip → the menu at the pointer. On Android a long-press fires the same event,
  // which is the touch path to the menu below the zoom where the ⋯ hides.
  const onNodeContextMenu = useCallback(
    (e: MouseEvent, node: Node) => {
      if (node.type !== "session") return;
      e.preventDefault();
      const opener =
        (e.currentTarget as HTMLElement | null)?.querySelector<HTMLElement>(
          "[data-chip-menu]",
        ) ?? null;
      openSessionMenu(
        node.id,
        { point: { x: e.clientX, y: e.clientY } },
        opener,
      );
    },
    [openSessionMenu],
  );

  // A window's ⋯ names its TRANSPORT key; the menu acts on the session the server knows, which is
  // the window's `actionKey` after a converge (#867).
  const openWindowMenu = useCallback(
    (wkey: string, anchor: MenuAnchor, opener: HTMLElement | null) => {
      const w = windowsRef.current.find((x) => x.key === wkey);
      if (w) openSessionMenu(w.actionKey, anchor, opener);
    },
    [openSessionMenu],
  );
  const isOnMap = useCallback(
    (key: string) => mapRows.some((s) => s.id === key),
    [mapRows],
  );

  // The menu's actions on the map: the same routes the sidebar calls, then a map refetch (the map
  // holds no in-place row patches). A failure shows the server's detail in the toolbar's error
  // slot. The sidebar list converges on its own poll.
  const aiConfigured = useConfig()?.ai_review?.configured ?? false;
  const [actionErr, setActionErr] = useState<string | null>(null);
  // Reviews in flight, per session (#968 review). Owned HERE rather than by a menu host, because the
  // host that started one can be gone before it settles — the menu closed, or another session's menu
  // replaced it — and a second Review now is a second paid model call whose result can land out of
  // order. The ref is the guard (synchronous, so two presses in one tick still send one request);
  // the state is what the menu renders from.
  const reviewingRef = useRef(new Set<string>());
  const [reviewingKeys, setReviewingKeys] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const mutate = useCallback(
    async (fn: () => Promise<unknown>, fallback: string) => {
      setActionErr(null);
      try {
        await fn();
        onRefetch?.();
      } catch (ex) {
        setActionErr(
          ex instanceof ApiError && ex.message ? ex.message : fallback,
        );
      }
    },
    [onRefetch],
  );
  const menuHandlers = useMemo(
    () => ({
      onRename: (id: string, title: string) =>
        mutate(() => api.rename(id, title), "Couldn’t rename the session."),
      onSetTag: (id: string, tag: string) =>
        mutate(() => api.setTag(id, tag), "Couldn’t set the tag."),
      onToggleFavorite: (id: string, value: boolean) =>
        mutate(
          () => (value ? api.favorite(id) : api.unfavorite(id)),
          "Couldn’t update the favorite.",
        ),
      onSetProject: (id: string, ref: ProjectRef | null) =>
        mutate(async () => {
          await api.setSessionProject(
            id,
            ref && ref.kind === "project" ? ref.id : null,
          );
          fetchProjects(); // a project may have just emptied/filled (#447)
        }, "Couldn’t move the session."),
      // Archive reaps the session's runtime (#523), and a live socket would try to relaunch into
      // an archived session (#631). So every window showing it closes FIRST — matched on
      // `actionKey`, so a converged placeholder window counts — and the close is committed
      // synchronously (`flushSync`) and given a task to tear its terminal down before the
      // request leaves. A refused archive does not reopen anything: the operator reopens from the
      // chip, which stays. Focus moves to the map now, because the ⋯ that would get it back is
      // about to disappear with its chip.
      onToggleArchive: async (id: string, currentlyArchived: boolean) => {
        if (!currentlyArchived) {
          wrapRef.current?.focus({ preventScroll: true });
          const doomed = windowsForSession(windowsRef.current, id);
          if (doomed.length) {
            flushSync(() => {
              for (const w of doomed) closeWindow(w.key);
            });
            await new Promise((r) => setTimeout(r, 0));
          }
        }
        await mutate(
          () => (currentlyArchived ? api.unarchive(id) : api.archive(id)),
          currentlyArchived
            ? "Couldn’t unarchive the session."
            : "Couldn’t archive the session.",
        );
      },
      // AI review, gated exactly as in the sidebar (#356): absent handlers hide the items.
      ...(aiConfigured
        ? {
            onReviewNow: async (id: string) => {
              if (reviewingRef.current.has(id)) return;
              reviewingRef.current.add(id);
              setReviewingKeys(new Set(reviewingRef.current));
              try {
                await mutate(() => api.reviewNow(id), "Review failed.");
              } finally {
                reviewingRef.current.delete(id);
                setReviewingKeys(new Set(reviewingRef.current));
              }
            },
            onToggleReviewExcluded: (id: string, excluded: boolean) =>
              mutate(
                () => api.reviewExclude(id, excluded),
                "Couldn’t update the AI review setting.",
              ),
            onToggleOrchestratorExcluded: (id: string, excluded: boolean) =>
              mutate(
                () => api.setOrchestratorExcluded(id, excluded),
                "Couldn’t update the mission control setting.",
              ),
          }
        : {}),
    }),
    [mutate, fetchProjects, closeWindow, aiConfigured],
  );

  // Group nodes reach the sessions refetch via context (#361 Phase 4) — see overviewActions — and
  // chips reach the session menu the same way (#968), keeping `buildOverview` function-free.
  const actions = useMemo(
    () => ({ refetchSessions: onRefetch ?? (() => {}), openSessionMenu }),
    [onRefetch, openSessionMenu],
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
        // Focus's fallback when the element that opened a menu is gone (#968).
        tabIndex={-1}
        data-overview-map=""
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
          {hasPins && (
            <button
              type="button"
              onClick={resetLayout}
              title="Put every cluster in this layout back where the map lays it out"
              data-reset-layout
            >
              <RotateCcw size={14} /> Reset layout
            </button>
          )}
          {layerOn && (
            <>
              {/* The readout IS the cap control (#936): the limit is configured where it is
                felt, not three clicks away in Settings. The stepper shares the readout's border
                so the two read as one control; a step at either clamp end dims rather than
                disappearing, so the range is legible without a tooltip. */}
              <span
                className="tr-ov-wins"
                data-window-readout
                title={`${ws.windows.length} of ${ws.cap} session windows open`}
              >
                <span className="tr-ov-wins-label">
                  <SquareDashedBottom size={14} aria-hidden="true" /> Windows{" "}
                  <b>{ws.windows.length}</b>/{ws.cap}
                </span>
                <span className="tr-ov-wins-step">
                  <button
                    type="button"
                    onClick={() => setCap(ws.cap - 1)}
                    disabled={ws.cap <= WINDOW_CAP_MIN}
                    aria-label="Fewer session windows allowed"
                    title={`Lower the window limit (minimum ${WINDOW_CAP_MIN}). Windows already open are never closed by this.`}
                    data-window-cap-down
                  >
                    <Minus size={12} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    onClick={() => setCap(ws.cap + 1)}
                    disabled={ws.cap >= WINDOW_CAP_MAX}
                    aria-label="More session windows allowed"
                    title={`Raise the window limit (maximum ${WINDOW_CAP_MAX}). Each window is a live terminal, so this costs memory on this device.`}
                    data-window-cap-up
                  >
                    <Plus size={12} aria-hidden="true" />
                  </button>
                </span>
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
        {(createErr || dragErr || actionErr) && (
          <div className="tr-ov-toolbar-err" role="alert" data-map-error>
            {createErr || dragErr || actionErr}
          </div>
        )}
        <div className="tr-ov-hint" aria-hidden="true">
          {draggable
            ? "Drag a session onto a project to move it · drag a cluster header to rearrange"
            : "Drag a cluster header to rearrange"}
        </div>
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
          onNodeContextMenu={onNodeContextMenu}
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
            onReconcile={onWindowReconcile}
            onMenu={openWindowMenu}
            isOnMap={isOnMap}
          />
        )}
        {menuTarget && (
          <MapSessionMenu
            // Keyed by session: switching the menu to another session mounts a FRESH host, so one
            // session's busy / reviewing / dialog state can never leak into another's menu
            // (#968 review). A review still in flight for the previous session stays guarded by
            // `reviewingKeys`, which the canvas owns for exactly this reason.
            key={menuTarget.key}
            session={menuRow ?? menuTarget.row}
            anchor={menuRow ? menuTarget.anchor : null}
            handlers={menuHandlers}
            onMenuClose={onMenuClose}
            onDone={onMenuDone}
            reviewInFlight={reviewingKeys.has(menuTarget.key)}
          />
        )}
      </div>
    </OverviewActionsCtx.Provider>
  );
}
