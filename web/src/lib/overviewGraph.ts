// Pure transform: a flat session list → React Flow nodes/edges for the Session Overview.
// Kept independent of @xyflow/react at runtime (type-only import) so the grouping, hierarchy,
// and layout are unit-testable without mounting the canvas. Node styling lives in the node
// components; here we group by resolved project ref (#361 Phase 4: entity groups merge their
// member cwds; folder fallbacks stay keyed by cwd), derive the folder hierarchy (#148), place
// a layered tree, and classify chips.

import type { Edge, Node } from "@xyflow/react";
import type { Session } from "../types/api";
import { displayProjectName, engineColor, engineName, projectColor } from "./format";

/** A session counts as "active" if its last activity is within this window. */
export const ACTIVE_WINDOW_S = 15 * 60;

/** How the map clusters sessions (#424 Phase 2). `project` (default) = resolved entity, else
 *  folder fallback (the pre-#424 behaviour); `folder` = pure cwd tree, ignoring entities;
 *  `agent` = one cluster per engine. Device-local selection (see OverviewPrefs). */
export type GroupBy = "folder" | "project" | "agent";

// Layout geometry (px). Deterministic so snapshots/tests are stable. Chips grew at #424
// Phase 4 to reach list-row parity (title + AI summary + project/folder/time meta line).
const CHIP_W = 240;
const CHIP_H = 80;
const GAP = 8;
const PAD = 12;
const HEADER_H = 44; // fits a custom name + a path subtitle line (#148)
const MAX_COLS = 3;
/** A collapsed cluster shows only its header at a fixed compact width (#144). */
const COLLAPSED_W = 300;
/** Horizontal gap between sibling subtrees; vertical gap between depth levels (#148). */
const SIBLING_GAP = 28;
const ROW_GAP = 64;

export interface ProjectGroupData extends Record<string, unknown> {
  project: string;
  /** Resolved cluster kind: "project" groups label by entity name; "folder" by cwd/path;
   *  "agent" by engine name (#424 Phase 2). Never the path for project/agent groups. */
  kind: "project" | "folder" | "agent";
  /** Expand/collapse toggle key (#361 Phase 4): the cwd for folder groups (so pre-Phase-4
   *  prefs survive), `project:<id>` for entity groups, or `agent:<engine>` for engine groups
   *  (#424 Phase 2). The node id is `group:<groupKey>`. */
  groupKey: string;
  /** Representative cwd: the only one for a folder group; the lexicographically first member
   *  cwd for an entity group (display falls back to a folder count when cwdCount > 1). */
  cwd: string;
  /** Distinct member cwds — entity groups merge sessions across all their folders (#361). */
  cwdCount: number;
  count: number;
  collapsed: boolean;
  /** Custom per-cwd display name (#148); falls back to the path when unset. Folder groups
   *  only — an entity group is always labelled by its entity name. */
  name?: string;
  /** Entity color (#361) — rendered as a tinted top border + dot on the group node. */
  color?: string;
  /** Folders-layout only (#445): the project this folder belongs to — the user project all
   *  its sessions resolve to (adopted folder), else the synthetic "Default" catch-all. Drawn
   *  as a small owning-project badge so folders read as a sub-property of a project. */
  owner?: { name: string; color?: string };
}
export interface SessionNodeData extends Record<string, unknown> {
  session: Session;
  active: boolean;
  /** Agent is currently emitting output (#156). Stronger signal than ``active`` — comes
   *  from the server's per-key last_output_at, browser-attached-only in v1. */
  working: boolean;
  /** This chip is the currently-open session (route = identity) — highlighted in sync with
   *  the sidebar list's active row (#149). */
  selected: boolean;
  /** Launch-folder display label (#424 Phase 4): the per-cwd custom name (#148) or shortened
   *  path — precomputed here so the chip stays presentational, matching the list row. */
  folderLabel: string;
}

export interface OverviewGraph {
  nodes: Node[];
  edges: Edge[];
}

function colsFor(count: number): number {
  return Math.min(MAX_COLS, Math.max(1, Math.ceil(Math.sqrt(count))));
}

function groupSize(count: number): { w: number; h: number; cols: number } {
  const cols = colsFor(count);
  const rows = Math.ceil(count / cols);
  return {
    w: PAD * 2 + cols * CHIP_W + (cols - 1) * GAP,
    h: HEADER_H + PAD * 2 + rows * CHIP_H + (rows - 1) * GAP,
    cols,
  };
}

// Tree resolution (parent / children / depth) is shared with the Settings → Session
// overview card via `./projectTree` (#174). The local copy here used to be the only one
// and is gone — both surfaces now agree on the same hierarchy by construction.
import { buildProjectTree } from "./projectTree";

export interface BuildOptions {
  /** Epoch seconds used to classify active/idle. Defaults to now (injectable for tests). */
  nowS?: number;
  /** Clustering mode (#424 Phase 2). Defaults to `project` (the pre-#424 behaviour). */
  groupBy?: GroupBy;
  /** Include archived sessions (default: hidden). */
  includeArchived?: boolean;
  /** Toggle keys (`groupKey`) of expanded clusters: cwds for folder groups, `project:<id>`
   *  for entity groups (#361 Phase 4). Anything not here is collapsed (header only) — the
   *  overview defaults to collapsed (#144). */
  expanded?: Set<string>;
  /** Cwds hidden from the map entirely (#144). */
  excluded?: Set<string>;
  /** Engine-qualified id ("engine:uuid") of the open session → its chip is marked selected. */
  activeId?: string;
  /** Per-cwd custom display names (#148). */
  names?: Record<string, string>;
  /** Make session chips draggable and drop their parent-clamp so one can be dragged onto
   *  another cluster to reassign it (#424 Phase 5). Enabled in Projects layout only — the
   *  canvas wires the drop → `api.setSessionProject`. Default off. */
  draggableSessions?: boolean;
  /** Non-archived project entities (#447). In `project` mode, an entity with no visible session
   *  still renders as an EMPTY cluster (count 0) so it's a drag-and-drop target — you can drag a
   *  session into a freshly-created project. Ignored in `folder`/`agent` mode. */
  projects?: ProjectEntityRef[];
}

/** Minimal project-entity shape the overview needs to draw an empty cluster (#447). */
export interface ProjectEntityRef {
  id: string;
  name: string;
  color?: string;
}

/** The synthetic "Default" project (#445) — folders are a sub-property of projects, so an
 *  unadopted session (resolved `kind:"folder"`) clusters under Default in `project` mode rather
 *  than as a standalone folder node. Mirrors the server's `projects.DEFAULT_PROJECT_ID`. */
export const DEFAULT_PROJECT_ID = "__default__";
export const DEFAULT_PROJECT_NAME = "Default";
const DEFAULT_GROUP_KEY = `project:${DEFAULT_PROJECT_ID}`;

/** A session's cluster for the active mode (#424 Phase 2 / #445):
 *  - `project` (default): the entity ref for project members (merging their sessions across
 *    cwds), the synthetic **Default** project for the unadopted folder fallback (#445) — so the
 *    Projects layout is projects→sessions only, never standalone folder nodes;
 *  - `folder`: always the launch cwd — entities are ignored, so a project's sessions split
 *    back out by folder (each folder node carries its owning-project badge, #445);
 *  - `agent`: one cluster per engine. */
interface Cluster {
  key: string;
  kind: "project" | "folder" | "agent";
  label: string;
  color?: string;
}
const clusterOf = (s: Session, groupBy: GroupBy): Cluster => {
  if (groupBy === "agent") {
    return { key: `agent:${s.engine}`, kind: "agent", label: engineName(s.engine), color: engineColor(s.engine) };
  }
  if (groupBy === "folder") {
    // Pure cwd tree — the path is the label fallback; entity name/color are intentionally
    // dropped. The folder still gets its own stable accent from the cwd hash (#285).
    return { key: s.cwd, kind: "folder", label: s.cwd, color: projectColor(s.cwd) };
  }
  // Entity groups tint by the explicit entity color, else the stable id hash (#285). The
  // synthetic Default catch-all stays neutral — it is many folders, not one project.
  return s.project.kind === "project"
    ? {
        key: `project:${s.project.id}`,
        kind: "project",
        label: s.project.name,
        color: s.project.color || projectColor(s.project.id),
      }
    : { key: DEFAULT_GROUP_KEY, kind: "project", label: DEFAULT_PROJECT_NAME };
};

/** A cwd hidden by prefs still keeps its sessions on the map ONLY in `project` mode when they
 *  resolve to an entity (mirrors the server's sidebar/facet rule, #361). In `folder`/`agent`
 *  mode every cluster is cwd- or engine-keyed, so a hidden cwd hides its sessions outright. */
const keepsHiddenCwd = (s: Session, groupBy: GroupBy): boolean =>
  groupBy === "project" && s.project.kind === "project";

/** The owning project for a folder node (#445, Folders layout): the single user project all the
 *  folder's sessions resolve to (an adopted folder), else the synthetic Default. A folder with
 *  any unadopted (`kind:"folder"`) session, or sessions split across >1 project, reads as
 *  Default — the folder itself belongs to Default; a hand-reassigned session doesn't relabel it. */
const ownerOf = (items: Session[]): { name: string; color?: string } => {
  let id: string | undefined;
  let name = "";
  let color: string | undefined;
  let conflict = false;
  let hasFolder = false;
  for (const s of items) {
    if (s.project.kind === "project") {
      if (id === undefined) {
        id = s.project.id;
        name = s.project.name;
        color = s.project.color || projectColor(s.project.id);
      } else if (id !== s.project.id) {
        conflict = true;
      }
    } else {
      hasFolder = true;
    }
  }
  return !hasFolder && !conflict && id !== undefined
    ? { name, ...(color ? { color } : {}) }
    : { name: DEFAULT_PROJECT_NAME };
};

/** Toggle keys offered to "Expand all" — the same visibility predicate + clustering as
 *  `buildOverview` for the active mode (#424 Phase 2). */
export function expandableKeys(
  sessions: Session[],
  dropped: Set<string>,
  groupBy: GroupBy = "project",
  projects: ProjectEntityRef[] = [],
): string[] {
  const keys = new Set(
    sessions
      .filter((s) => keepsHiddenCwd(s, groupBy) || !dropped.has(s.cwd))
      .map((s) => clusterOf(s, groupBy).key),
  );
  // Empty project clusters are expandable too (#447) so "Expand all" covers them.
  if (groupBy === "project") for (const p of projects) keys.add(`project:${p.id}`);
  return [...keys];
}

/** Build the project hierarchy graph. Clusters (one per resolved ref: per entity for
 *  project members, per cwd for the folder fallback) are linked parent→child by folder
 *  nesting (#148, folder groups only — entity groups are always roots) and laid out as a
 *  layered tidy tree: depth = nesting level → row; siblings spread left→right with parents
 *  centered over their children. Group nodes precede their session-chip children (React
 *  Flow requirement). */
export function buildOverview(sessions: Session[], opts: BuildOptions = {}): OverviewGraph {
  const nowS = opts.nowS ?? Date.now() / 1000;
  const groupBy = opts.groupBy ?? "project";
  const expanded = opts.expanded ?? new Set<string>();
  const excluded = opts.excluded ?? new Set<string>();
  const names = opts.names ?? {};
  const draggableSessions = opts.draggableSessions ?? false;
  // cwd visibility prefs apply per `keepsHiddenCwd`: an entity-resolved session in `project`
  // mode survives a hidden cwd (server sidebar/facet parity, #361); in `folder`/`agent` mode
  // a hidden cwd hides its sessions outright (#424 Phase 2).
  const visible = (opts.includeArchived ? sessions : sessions.filter((s) => !s.archived)).filter(
    (s) => keepsHiddenCwd(s, groupBy) || !excluded.has(s.cwd),
  );

  const groups = new Map<
    string,
    {
      project: string;
      kind: "project" | "folder" | "agent";
      items: Session[];
      maxMtime: number;
      cwds: Set<string>;
      color?: string;
    }
  >();
  for (const s of visible) {
    // Cluster by the active mode (#424 Phase 2): resolved ref (project), cwd (folder), or
    // engine (agent). In `project` mode an entity group merges its sessions across every
    // member cwd; the folder fallback stays keyed by cwd, byte-compatible with #361 ids.
    const c = clusterOf(s, groupBy);
    const g = groups.get(c.key) ?? {
      project: c.label,
      kind: c.kind,
      items: [],
      maxMtime: 0,
      cwds: new Set<string>(),
      color: undefined,
    };
    g.items.push(s);
    g.cwds.add(s.cwd);
    g.maxMtime = Math.max(g.maxMtime, s.last_mtime || 0);
    if (c.color) g.color = c.color;
    groups.set(c.key, g);
  }

  // Empty project clusters (#447): in `project` mode, every non-archived entity that produced no
  // session group still renders as an EMPTY cluster (count 0, maxMtime 0 → sorts last) so it's a
  // drag-and-drop target. `maxMtime: 0` keeps empties after active clusters in the recency order.
  if (groupBy === "project") {
    for (const p of opts.projects ?? []) {
      const key = `project:${p.id}`;
      if (!groups.has(key)) {
        groups.set(key, {
          project: p.name,
          kind: "project",
          items: [],
          maxMtime: 0,
          cwds: new Set<string>(),
          color: p.color || projectColor(p.id),
        });
      }
    }
  }

  // Hierarchy (parent/children/depth) — extracted to `./projectTree` so the Settings card
  // can render the same tree (#174). Folder-nesting edges apply among FOLDER groups only
  // (their keys are cwds); entity groups are roots laid out alongside (#361 Phase 4).
  const present = new Set(groups.keys());
  const folderKeys = [...present].filter((k) => groups.get(k)!.kind === "folder");
  const tree = buildProjectTree(folderKeys);
  const parent = new Map<string, string | undefined>(
    [...tree.values()].map((n) => [n.cwd, n.parent]),
  );
  const children = new Map<string, string[]>(
    [...tree.values()].map((n) => [n.cwd, n.children]),
  );
  const depthOf = (key: string): number => tree.get(key)?.depth ?? 0;
  const sizeOf = (key: string) =>
    expanded.has(key) ? groupSize(groups.get(key)!.items.length) : { w: COLLAPSED_W, h: HEADER_H };

  // Row Y by depth (each row as tall as its tallest cluster).
  const maxDepth = present.size ? Math.max(...[...present].map(depthOf)) : 0;
  const rowH: number[] = [];
  for (const key of present) {
    const d = depthOf(key);
    rowH[d] = Math.max(rowH[d] ?? 0, sizeOf(key).h);
  }
  const rowY: number[] = [];
  let acc = 0;
  for (let d = 0; d <= maxDepth; d++) {
    rowY[d] = acc;
    acc += (rowH[d] ?? HEADER_H) + ROW_GAP;
  }

  // Deterministic ordering: most-recent first, then key. Roots + each child list.
  const byRecent = (a: string, b: string) =>
    groups.get(b)!.maxMtime - groups.get(a)!.maxMtime || a.localeCompare(b);
  const roots = [...present].filter((k) => !parent.get(k)).sort(byRecent);
  for (const ks of children.values()) ks.sort(byRecent);

  // Tidy-tree layout: leaves consume the x-cursor; a parent centers over its children.
  const pos = new Map<string, { x: number; y: number }>();
  let cursor = 0;
  const place = (key: string): number => {
    const { w } = sizeOf(key);
    const kids = children.get(key) ?? [];
    let cx: number;
    if (kids.length === 0) {
      cx = cursor + w / 2;
      cursor += w + SIBLING_GAP;
    } else {
      const cs = kids.map(place);
      cx = (cs[0] + cs[cs.length - 1]) / 2;
    }
    pos.set(key, { x: cx - w / 2, y: rowY[depthOf(key)] });
    return cx;
  };
  for (const r of roots) place(r);

  const nodes: Node[] = [];
  const edges: Edge[] = [];
  // DFS emit: each group node immediately followed by its chips; parent before children.
  const emit = (key: string) => {
    const g = groups.get(key)!;
    const isExpanded = expanded.has(key);
    const { w, h } = isExpanded ? groupSize(g.items.length) : { w: COLLAPSED_W, h: HEADER_H };
    const cols = isExpanded ? groupSize(g.items.length).cols : 1;
    const groupId = `group:${key}`;
    // Representative cwd: a folder group's key IS its cwd; an entity group shows its
    // first (sorted) member cwd, or just the folder count when it spans several.
    const cwd = g.kind === "folder" ? key : ([...g.cwds].sort()[0] ?? "");
    nodes.push({
      id: groupId,
      type: "projectGroup",
      position: pos.get(key)!,
      data: {
        project: g.project,
        kind: g.kind,
        groupKey: key,
        cwd,
        cwdCount: g.cwds.size,
        count: g.items.length,
        collapsed: !isExpanded,
        name: g.kind === "folder" ? names[key] : undefined,
        color: g.color,
        // Folders layout (#445): tag each folder node with its owning project (the user project
        // all its sessions resolve to, else Default) so folders read as a sub-property of a
        // project. Only meaningful for folder-kind groups in `folder` mode.
        owner: groupBy === "folder" && g.kind === "folder" ? ownerOf(g.items) : undefined,
      } satisfies ProjectGroupData,
      style: { width: w, height: h },
      draggable: false,
      selectable: false,
    });
    if (isExpanded) {
      const items = [...g.items].sort(
        (a, b) =>
          Number(b.sticky) - Number(a.sticky) ||
          (b.last_mtime || 0) - (a.last_mtime || 0) ||
          a.id.localeCompare(b.id),
      );
      items.forEach((s, i) => {
        nodes.push({
          id: s.id,
          type: "session",
          parentId: groupId,
          // Parent-clamped when static; the clamp is dropped when chips are draggable so one
          // can be dragged out onto another cluster to reassign it (#424 Phase 5).
          ...(draggableSessions ? {} : { extent: "parent" as const }),
          position: {
            x: PAD + (i % cols) * (CHIP_W + GAP),
            y: HEADER_H + PAD + Math.floor(i / cols) * (CHIP_H + GAP),
          },
          data: {
            session: s,
            active: nowS - (s.last_mtime || 0) < ACTIVE_WINDOW_S,
            working: !!s.working,
            selected: s.id === opts.activeId,
            folderLabel: displayProjectName(s.cwd, names),
          } satisfies SessionNodeData,
          draggable: draggableSessions,
        });
      });
    }
    for (const kid of children.get(key) ?? []) {
      edges.push({
        id: `e:${key}->${kid}`,
        source: groupId,
        target: `group:${kid}`,
        type: "smoothstep",
        selectable: false,
        focusable: false,
        deletable: false,
      });
      emit(kid);
    }
  };
  for (const r of roots) emit(r);

  return { nodes, edges };
}
