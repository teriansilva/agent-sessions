// Pure transform: a flat session list → React Flow nodes/edges for the Session Overview
// (#139). Kept independent of @xyflow/react at runtime (type-only import) so the layout
// + grouping logic is unit-testable without mounting the canvas. The visual styling of a
// node (engine color, dots) lives in the node components; here we only group, place, and
// classify.

import type { Edge, Node } from "@xyflow/react";
import type { Session } from "../types/api";

/** A session counts as "active" if its last activity is within this window. */
export const ACTIVE_WINDOW_S = 15 * 60;

// Layout geometry (px). Deterministic so snapshots/tests are stable.
const CHIP_W = 176;
const CHIP_H = 46;
const GAP = 8;
const PAD = 12;
const HEADER_H = 34;
const GROUP_GAP = 28;
/** Wrap clusters to a new row once a row passes this width. */
const MAX_ROW_W = 1240;
/** Chip columns per cluster: a roughly-square grid, capped so wide clusters stay readable. */
const MAX_COLS = 3;
/** A collapsed cluster shows only its header at a fixed compact width (#144). */
const COLLAPSED_W = 300;

export interface ProjectGroupData extends Record<string, unknown> {
  project: string;
  cwd: string;
  count: number;
  collapsed: boolean;
}
export interface SessionNodeData extends Record<string, unknown> {
  session: Session;
  active: boolean;
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

export interface BuildOptions {
  /** Epoch seconds used to classify active/idle. Defaults to now (injectable for tests). */
  nowS?: number;
  /** Include archived sessions (default: hidden). */
  includeArchived?: boolean;
  /** Cwds whose cluster is expanded. Anything not here is collapsed (header only) — the
   *  overview defaults to collapsed (#144). */
  expanded?: Set<string>;
  /** Cwds hidden from the map entirely (#144). */
  excluded?: Set<string>;
}

/** Build the project-cluster graph. Group nodes are emitted before their children (React
 *  Flow requires a parent to precede its `parentId` children). Sessions are grouped by
 *  `cwd`; clusters are packed left→right, wrapping at MAX_ROW_W. Ordering is fully
 *  deterministic (most-recent cluster first; within a cluster sticky→recent→id). */
export function buildOverview(sessions: Session[], opts: BuildOptions = {}): OverviewGraph {
  const nowS = opts.nowS ?? Date.now() / 1000;
  const expanded = opts.expanded ?? new Set<string>();
  const excluded = opts.excluded ?? new Set<string>();
  const visible = (opts.includeArchived ? sessions : sessions.filter((s) => !s.archived)).filter(
    (s) => !excluded.has(s.cwd),
  );

  // Group by cwd, preserving each group's display label + max mtime for ordering.
  const groups = new Map<string, { project: string; items: Session[]; maxMtime: number }>();
  for (const s of visible) {
    const g = groups.get(s.cwd) ?? { project: s.project || s.cwd, items: [], maxMtime: 0 };
    g.items.push(s);
    g.maxMtime = Math.max(g.maxMtime, s.last_mtime || 0);
    groups.set(s.cwd, g);
  }

  // Clusters: most recently active first, then cwd for a stable tiebreak.
  const ordered = [...groups.entries()].sort(
    (a, b) => b[1].maxMtime - a[1].maxMtime || a[0].localeCompare(b[0]),
  );

  const nodes: Node[] = [];
  // Row-packing cursor.
  let x = 0;
  let y = 0;
  let rowH = 0;

  for (const [cwd, g] of ordered) {
    const isExpanded = expanded.has(cwd);
    const { w, h, cols } = isExpanded
      ? groupSize(g.items.length)
      : { w: COLLAPSED_W, h: HEADER_H, cols: 1 };
    if (x > 0 && x + w > MAX_ROW_W) {
      // Wrap to the next row.
      x = 0;
      y += rowH + GROUP_GAP;
      rowH = 0;
    }
    const groupId = `group:${cwd}`;
    nodes.push({
      id: groupId,
      type: "projectGroup",
      position: { x, y },
      data: {
        project: g.project,
        cwd,
        count: g.items.length,
        collapsed: !isExpanded,
      } satisfies ProjectGroupData,
      style: { width: w, height: h },
      draggable: false,
      selectable: false,
    });

    // Collapsed clusters render header-only — no child chips (keeps the map compact, #144).
    if (isExpanded) {
      // Sticky first, then most-recent, then id — deterministic chip order.
      const items = [...g.items].sort(
        (a, b) =>
          Number(b.sticky) - Number(a.sticky) ||
          (b.last_mtime || 0) - (a.last_mtime || 0) ||
          a.id.localeCompare(b.id),
      );
      items.forEach((s, i) => {
        const col = i % cols;
        const row = Math.floor(i / cols);
        nodes.push({
          id: s.id,
          type: "session",
          parentId: groupId,
          extent: "parent",
          position: {
            x: PAD + col * (CHIP_W + GAP),
            y: HEADER_H + PAD + row * (CHIP_H + GAP),
          },
          data: { session: s, active: nowS - (s.last_mtime || 0) < ACTIVE_WINDOW_S } satisfies SessionNodeData,
          draggable: false,
        });
      });
    }

    x += w + GROUP_GAP;
    rowH = Math.max(rowH, h);
  }

  return { nodes, edges: [] };
}
