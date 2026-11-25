/** Which built chunks stay OUT of the PWA precache (#2114).
 *
 *  The service worker precaches every `.js` the build emits, so an install downloads all of it.
 *  Mermaid is about 3.4 MB of JavaScript across ~100 chunks that only a Markdown preview with a
 *  diagram ever loads. Its chunks are written under `assets/lazy/` and the precache ignores that
 *  folder, so they load on first use instead.
 *
 *  A chunk goes there only when EVERY module in it belongs to a package reachable from a
 *  lazy-only root (`mermaid`) and from no other dependency in `package.json`. d3, for instance, is
 *  shared with the map (`@xyflow/react`), so a chunk holding it stays precached — the app needs it
 *  offline whether or not a diagram is ever drawn.
 *
 *  Pure: `vite.config.ts` hands in the parsed lockfile; `pwaLazyChunks.test.ts` pins the rules. */

export const LAZY_CHUNK_DIR = "assets/lazy";
export const LAZY_ROOTS = ["mermaid"] as const;

interface LockPackage {
  dependencies?: Record<string, string>;
  optionalDependencies?: Record<string, string>;
  peerDependencies?: Record<string, string>;
  dev?: boolean;
}
export interface Lockfile {
  packages: Record<string, LockPackage>;
}

/** The lockfile key a dependency `name` resolves to from package key `from` — node's lookup:
 *  the nearest `node_modules/<name>` walking up from `from`. */
function resolveKey(lock: Lockfile, from: string, name: string): string | null {
  let base = from;
  for (;;) {
    const key = base ? `${base}/node_modules/${name}` : `node_modules/${name}`;
    if (lock.packages[key]) return key;
    if (!base) return null;
    const i = base.lastIndexOf("/node_modules/");
    base = i < 0 ? "" : base.slice(0, i);
  }
}

function reach(lock: Lockfile, roots: string[]): Set<string> {
  const seen = new Set<string>();
  const stack = [...roots];
  while (stack.length) {
    const key = stack.pop()!;
    if (seen.has(key)) continue;
    seen.add(key);
    const pkg = lock.packages[key];
    const deps = { ...pkg?.dependencies, ...pkg?.optionalDependencies, ...pkg?.peerDependencies };
    for (const name of Object.keys(deps)) {
      const k = resolveKey(lock, key, name);
      if (k) stack.push(k);
    }
  }
  return seen;
}

/** Lockfile keys (`node_modules/khroma`, …) only the lazy roots need. */
export function lazyOnlyPackages(lock: Lockfile, roots: readonly string[] = LAZY_ROOTS): Set<string> {
  const app = lock.packages[""] ?? {};
  const direct = Object.keys(app.dependencies ?? {});
  const lazyKeys = roots.map((r) => resolveKey(lock, "", r)).filter((k): k is string => !!k);
  const otherKeys = direct
    .filter((d) => !roots.includes(d))
    .map((d) => resolveKey(lock, "", d))
    .filter((k): k is string => !!k);
  const lazy = reach(lock, lazyKeys);
  for (const k of reach(lock, otherKeys)) lazy.delete(k);
  return lazy;
}

/** The lockfile key of the package a bundled module id lives in, or null for app code. */
export function packageKeyOf(moduleId: string): string | null {
  const id = moduleId.replace(/\\/g, "/").replace(/^\0/, "").replace(/\?.*$/, "");
  const at = id.lastIndexOf("/node_modules/");
  if (at < 0) return null;
  // Keep every nested node_modules segment: `a/node_modules/b` is its own package.
  const first = id.indexOf("node_modules/");
  const rest = id.slice(at + "/node_modules/".length).split("/");
  const name = rest[0].startsWith("@") ? `${rest[0]}/${rest[1]}` : rest[0];
  return `${id.slice(first, at + "/node_modules/".length)}${name}`;
}

/** Whether a chunk made of `moduleIds` is lazy-only (and so stays out of the precache). */
export function isLazyOnlyChunk(moduleIds: readonly string[], lazy: ReadonlySet<string>): boolean {
  if (!moduleIds.length) return false;
  return moduleIds.every((id) => {
    const key = packageKeyOf(id);
    return key !== null && lazy.has(key);
  });
}
