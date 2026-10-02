import type { Node } from "@xyflow/react";
import type { Point, Size } from "./workspace";

/** Projecting a map node into the window overlay's coordinate space (#208).
 *
 *  Type-only import of `Node`, so this stays as testable as `overviewGraph.ts` — the runtime
 *  dependency is the two functions the caller passes in. */

/** Fallback chip geometry (overviewGraph's CHIP_W/CHIP_H), used only until React Flow has
 *  measured a node. Never load-bearing: a one-frame-stale tether corrects itself. */
const FALLBACK: Size = { w: 240, h: 80 };

/** Walk the parent chain — a session chip's `position` is relative to its cluster node. */
export function absolutePosition(
  node: Node,
  getNode: (id: string) => Node | undefined,
): Point {
  let cur: Node | undefined = node;
  let x = 0;
  let y = 0;
  // Bounded by the graph's depth; the guard is only against a malformed cycle.
  for (let i = 0; cur && i < 32; i++) {
    x += cur.position.x;
    y += cur.position.y;
    cur = cur.parentId ? getNode(cur.parentId) : undefined;
  }
  return { x, y };
}

export function nodeSize(node: Node): Size {
  return {
    w: node.measured?.width ?? (node.style?.width as number) ?? FALLBACK.w,
    h: node.measured?.height ?? (node.style?.height as number) ?? FALLBACK.h,
  };
}

/** Where a node's tether leaves it, in **overlay-local** coordinates.
 *
 *  `flowToScreenPosition` returns viewport-absolute screen coordinates, and the overlay sits
 *  inside the app shell (sidebar, header, map toolbar) — so its origin is never the viewport's.
 *  Subtracting the overlay origin is what makes a tether land on its chip at any shell offset
 *  rather than only when the map happens to start at 0,0. */
export function anchorPointOf(
  node: Node,
  getNode: (id: string) => Node | undefined,
  flowToScreenPosition: (p: Point) => Point,
  origin: Point,
): Point {
  const pos = absolutePosition(node, getNode);
  const size = nodeSize(node);
  const screen = flowToScreenPosition({
    x: pos.x + size.w,
    y: pos.y + size.h / 2,
  });
  return { x: screen.x - origin.x, y: screen.y - origin.y };
}
