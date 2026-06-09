/**
 * Shared parent/child tree resolver for project cwds (#174).
 *
 * The Overview map (`buildOverview`) and the Settings → Session overview card both render
 * the same hierarchy: cwds linked by **folder nesting**, boundary-aware (so `/a` parents
 * `/a/b` but never `/a-foo`), only by **nearest present ancestor** (absent intermediates
 * are skipped — no synthetic nodes). Extracting it here keeps that logic in one place,
 * independently testable, and ensures the two surfaces stay in sync.
 */

/** The nearest *present* project that is a path-BOUNDARY ancestor of `cwd` (longest match),
 *  or `undefined`. Boundary-aware so `/a/b` links to `/a` but `/a-foo` never does. */
export function nearestAncestor(cwd: string, present: Iterable<string>): string | undefined {
  let best: string | undefined;
  for (const c of present) {
    if (c === cwd) continue;
    const pfx = c.endsWith("/") ? c : `${c}/`;
    if (cwd.startsWith(pfx) && (best === undefined || c.length > best.length)) best = c;
  }
  return best;
}

export interface TreeNode {
  cwd: string;
  parent: string | undefined;
  children: string[];
  /** 0 for root, parent's depth + 1 otherwise. */
  depth: number;
}

/** Build the parent/children/depth map for `cwds` using `nearestAncestor`. The returned
 *  shape is keyed by cwd so callers can render either a flat list (sorted) or walk depth-
 *  first by following `children`. Roots are cwds with no present ancestor. */
export function buildProjectTree(cwds: Iterable<string>): Map<string, TreeNode> {
  const present = new Set(cwds);
  const nodes = new Map<string, TreeNode>();
  for (const cwd of present) {
    nodes.set(cwd, { cwd, parent: undefined, children: [], depth: 0 });
  }
  // Resolve parents first (independent of order).
  for (const cwd of present) {
    const p = nearestAncestor(cwd, present);
    nodes.get(cwd)!.parent = p;
    if (p !== undefined) nodes.get(p)!.children.push(cwd);
  }
  // Then compute depths via top-down walks from the roots.
  const roots = [...present].filter((c) => nodes.get(c)!.parent === undefined);
  const walk = (cwd: string, depth: number) => {
    const n = nodes.get(cwd)!;
    n.depth = depth;
    for (const ch of n.children) walk(ch, depth + 1);
  };
  for (const r of roots) walk(r, 0);
  return nodes;
}

/** Flatten the tree into a depth-first list. `sortChildren` is invoked on each parent's
 *  child list before recursion so callers can choose a stable order (alphabetical, recency,
 *  whatever) without owning the traversal. Roots are sorted the same way. */
export function flattenTree(
  nodes: Map<string, TreeNode>,
  sortChildren: (a: string, b: string) => number = (a, b) => a.localeCompare(b),
): TreeNode[] {
  const out: TreeNode[] = [];
  const roots = [...nodes.values()]
    .filter((n) => n.parent === undefined)
    .map((n) => n.cwd)
    .sort(sortChildren);
  const dfs = (cwd: string) => {
    const n = nodes.get(cwd)!;
    out.push(n);
    [...n.children].sort(sortChildren).forEach(dfs);
  };
  for (const r of roots) dfs(r);
  return out;
}
