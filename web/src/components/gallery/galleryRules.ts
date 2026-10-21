/** Pure rules behind Library's gallery views (#1440): which local ID to suggest, when a local copy
 *  shows "Update available", and where an old `/library/gallery` link lands. No I/O here; the
 *  server stays the authority on every write (NOREPLACE, acknowledgement, signed admission). */
import type { GalleryEntry, GalleryIndex, GalleryKind, GalleryOrigin } from "../../types/gallery";

/** A local playbook or skill ID (`schema.PLAYBOOK_ID_RE`). */
export const LOCAL_ID = /^[a-z][a-z0-9-]{1,47}$/;
const MAX_ID = 48;

/** `base` if it is a valid ID nobody holds, else the first free `<base>-2`, `<base>-3`, … The base
 *  is trimmed so every candidate stays within the 48-character limit. */
export function freeId(base: string, taken: ReadonlySet<string>): string {
  let stem = base.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/-+/g, "-");
  stem = stem.replace(/^[^a-z]+/, "").replace(/-+$/, "");
  if (stem.length < 2) stem = `copy${stem ? `-${stem}` : ""}`;
  stem = stem.slice(0, MAX_ID).replace(/-+$/, "");
  if (!taken.has(stem)) return stem;
  for (let n = 2; ; n++) {
    const suffix = `-${n}`;
    const candidate = `${stem.slice(0, MAX_ID - suffix.length).replace(/-+$/, "")}${suffix}`;
    if (!taken.has(candidate)) return candidate;
  }
}

/** The suggested ID for an updated copy: `<id>-v1-3-0`, or the next free one. */
export function updateId(id: string, version: string, taken: ReadonlySet<string>): string {
  return freeId(`${id}-v${version.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`, taken);
}

type Semver = { core: number[]; pre: string[] };
function parse(version: string): Semver | null {
  const m = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(version);
  if (!m) return null;
  return { core: [m[1], m[2], m[3]].map(Number), pre: m[4] ? m[4].split(".") : [] };
}
/** Semantic-version precedence: <0, 0 or >0. Unparseable versions compare as equal (never newer). */
export function compareVersions(a: string, b: string): number {
  const x = parse(a);
  const y = parse(b);
  if (!x || !y) return 0;
  for (let i = 0; i < 3; i++) if (x.core[i] !== y.core[i]) return x.core[i] - y.core[i];
  if (!x.pre.length || !y.pre.length) return y.pre.length - x.pre.length;
  for (let i = 0; i < Math.max(x.pre.length, y.pre.length); i++) {
    const p = x.pre[i];
    const q = y.pre[i];
    if (p === undefined) return -1;
    if (q === undefined) return 1;
    if (p === q) continue;
    const pn = /^\d+$/.test(p);
    const qn = /^\d+$/.test(q);
    if (pn && qn) return Number(p) - Number(q);
    if (pn !== qn) return pn ? -1 : 1;
    return p < q ? -1 : 1;
  }
  return 0;
}

export type UpdateStatus =
  | { state: "available"; entry: GalleryEntry }
  | { state: "current" }
  | { state: "unreachable" }
  | { state: "stale" }
  | { state: "withdrawn" }
  | { state: "damaged" };

/** "Update available" holds only when the stored listing is an accepted, unexpired publication
 *  with an available entry of the same upstream kind + id whose version is newer than the copy's
 *  imported base. Every other case is one plain, non-importable state. */
export function updateStatus(
  origin: GalleryOrigin | null | undefined,
  catalog: GalleryIndex | null | undefined,
): UpdateStatus | null {
  if (!origin) return null;
  if (origin.state !== "verified" || !origin.kind || !origin.id || !origin.version)
    return { state: "damaged" };
  if (!catalog || catalog.state === "empty" || catalog.state === "unavailable")
    return { state: "unreachable" };
  if (catalog.state === "stale") return { state: "stale" };
  const same = catalog.entries.filter(
    (e) => e.content.kind === origin.kind && e.content.id === origin.id,
  );
  const newer = same
    .filter((e) => e.available && compareVersions(e.content.version, origin.version!) > 0)
    .sort((a, b) => compareVersions(b.content.version, a.content.version));
  if (newer.length) return { state: "available", entry: newer[0] };
  if (same.length && !same.some((e) => e.available)) return { state: "withdrawn" };
  return { state: "current" };
}

/** What an old `/library/gallery[/<digest>][?kind=&id=]` link asks for, before any data is read.
 *  Only the exact kinds `playbook` and `skill` are destinations; anything else is `invalid`. */
export type LegacyLink =
  | { type: "browse" }
  | { type: "entry"; digest: string }
  | { type: "update"; kind: GalleryKind; id: string }
  | { type: "missing-local"; kind: GalleryKind }
  | { type: "invalid" };

export function classifyLegacy(digest: string | undefined, search: URLSearchParams): LegacyLink {
  const kind = search.get("kind");
  const id = search.get("id");
  if (kind !== null || id !== null) {
    if (kind !== "playbook" && kind !== "skill") return { type: "invalid" };
    if (id === null || !LOCAL_ID.test(id)) return { type: "missing-local", kind };
    return { type: "update", kind, id };
  }
  if (digest === undefined || digest === "") return { type: "browse" };
  return /^[0-9a-f]{64}$/.test(digest) ? { type: "entry", digest } : { type: "invalid" };
}
