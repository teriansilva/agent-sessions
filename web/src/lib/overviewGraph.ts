// Pure transform: a flat session list → React Flow nodes/edges for the Session Overview.
// Kept independent of @xyflow/react at runtime (type-only import) so the grouping, hierarchy,
// and layout are unit-testable without mounting the canvas. Node styling lives in the node
// components; here we group by resolved project ref (#361 Phase 4: entity groups merge their
// member cwds; folder fallbacks stay keyed by cwd), derive the folder hierarchy (#148), place
// a layered tree, and classify chips.

import type { Edge, Node } from "@xyflow/react";
import type { Session } from "../types/api";

/** A session counts as "active" if its last activity is within this window. */
export const ACTIVE_WINDOW_S = 15 * 60;

// Layout geometry (px). Deterministic so snapshots/tests are stable.
const CHIP_W = 176;
const CHIP_H = 46;
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
  /** Resolved ref kind (#361): "project" groups label by entity name, never the path. */
  kind: "project" | "folder";
  /** Expand/collapse toggle key (#361 Phase 4): the cwd for folder groups (so pre-Phase-4
   *  prefs survive) or `project:<id>` for entity groups. The node id is `group:<groupKey>`. */
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
}

/** A session's cluster key (#361 Phase 4): the entity ref for project members (merging their
 *  sessions across cwds), the launch cwd for the folder fallback. */
const groupKeyOf = (s: Session): string =>
  s.project.kind === "project" ? `project:${s.project.id}` : s.cwd;

/** Toggle keys offered to "Expand all" — the same visibility predicate as `buildOverview`
 *  (#361): project-resolved rows stay on the map when their cwd is hidden, so their
 *  clusters must be expandable too. */
export function expandableKeys(sessions: Session[], dropped: Set<string>): string[] {
  return [
    ...new Set(
      sessions
        .filter((s) => s.project.kind === "project" || !dropped.has(s.cwd))
        .map(groupKeyOf),
    ),
  ];
}

/** Build the project hierarchy graph. Clusters (one per resolved ref: per entity for
 *  project members, per cwd for the folder fallback) are linked parent→child by folder
 *  nesting (#148, folder groups only — entity groups are always roots) and laid out as a
 *  layered tidy tree: depth = nesting level → row; siblings spread left→right with parents
 *  centered over their children. Group nodes precede their session-chip children (React
 *  Flow requirement). */
export function buildOverview(sessions: Session[], opts: BuildOptions = {}): OverviewGraph {
  const nowS = opts.nowS ?? Date.now() / 1000;
  const expanded = opts.expanded ?? new Set<string>();
  const excluded = opts.excluded ?? new Set<string>();
  const names = opts.names ?? {};
  // cwd visibility prefs apply to FOLDER-grouped sessions only (#361): a session
  // resolved to a project entity stays on the map even when its launch folder is
  // hidden / not allowlisted — mirroring the server's sidebar/facet rule.
  const visible = (opts.includeArchived ? sessions : sessions.filter((s) => !s.archived)).filter(
    (s) => s.project.kind === "project" || !excluded.has(s.cwd),
  );

  const groups = new Map<
    string,
    {
      project: string;
      kind: "project" | "folder";
      items: Session[];
      maxMtime: number;
      cwds: Set<string>;
      color?: string;
    }
  >();
  for (const s of visible) {
    // #361 Phase 4: cluster by the resolved ref — an entity group merges its sessions
    // across every member cwd (adopted folders AND folderless explicit assignments);
    // the folder fallback stays keyed by cwd, byte-compatible with pre-Phase-4 ids.
    const key = groupKeyOf(s);
    const g = groups.get(key) ?? {
      project: s.project.name,
      kind: s.project.kind,
      items: [],
      maxMtime: 0,
      cwds: new Set<string>(),
      color: undefined,
    };
    g.items.push(s);
    g.cwds.add(s.cwd);
    g.maxMtime = Math.max(g.maxMtime, s.last_mtime || 0);
    if (s.project.color) g.color = s.project.color;
    groups.set(key, g);
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
    const cwd = g.kind === "folder" ? key : [...g.cwds].sort()[0];
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
          extent: "parent",
          position: {
            x: PAD + (i % cols) * (CHIP_W + GAP),
            y: HEADER_H + PAD + Math.floor(i / cols) * (CHIP_H + GAP),
          },
          data: {
            session: s,
            active: nowS - (s.last_mtime || 0) < ACTIVE_WINDOW_S,
            working: !!s.working,
            selected: s.id === opts.activeId,
          } satisfies SessionNodeData,
          draggable: false,
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
