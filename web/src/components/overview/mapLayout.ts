/** Where the operator put the map's clusters (#968).
 *
 *  The tidy-tree layout in `overviewGraph.ts` decides where every cluster STARTS; a cluster the
 *  operator dragged is PINNED at the flow position it was dropped on, per layout, on this device.
 *  Pure apart from the two storage calls, so the rules are testable without React Flow.
 *
 *  `localStorage`, like the grouping mode and the window workspace (`windowStore.ts`): a layout of
 *  clusters is device-shaped, and a server pref would be an `/api/prefs` allowlist change for a
 *  value no other device can use.
 *
 *  READ-LENIENT, WRITE-SWALLOWING — the same asymmetry `windowStore` uses. The operator can edit
 *  storage, so a read never throws and never returns a coordinate the map cannot render; one bad
 *  entry drops itself, never the layout. A pin for a cluster that is not on the map right now is
 *  IGNORED, not deleted: hiding a project, or archiving its last session, must not lose its spot.
 */
import type { Node } from "@xyflow/react";
import type { GroupBy, ProjectGroupData } from "../../lib/overviewGraph";

/** Beside `tr-overview-groupby` and `tr-overview-workspace`, so the map's keys read as one family. */
export const POSITIONS_KEY = "tr-overview-cluster-positions";
/** Per layout. Pins are never pruned for being off the map, so something must bound them. */
export const MAX_PINS_PER_LAYOUT = 500;
/** Far beyond any real canvas; what it rules out is a hand-edited 1e300 that no pan can reach. */
const COORD_LIMIT = 1_000_000;
const LAYOUTS: readonly GroupBy[] = ["folder", "project", "agent"];

export interface Pin {
  x: number;
  y: number;
}
/** Group key (`overviewGraph`'s `groupKey`) → the flow position the cluster was dropped at. */
export type Pins = Record<string, Pin>;
export type LayoutPins = Record<GroupBy, Pins>;

export const emptyPins = (): LayoutPins => ({ folder: {}, project: {}, agent: {} });

const coord = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) && Math.abs(v) <= COORD_LIMIT
    ? Math.floor(v + 0.5)
    : null;

function decodeLayout(raw: unknown): Pins {
  const out: Pins = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  let n = 0;
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (n >= MAX_PINS_PER_LAYOUT) break;
    if (!key || key.length > 1024) continue;
    if (!value || typeof value !== "object") continue;
    const x = coord((value as Record<string, unknown>).x);
    const y = coord((value as Record<string, unknown>).y);
    if (x === null || y === null) continue;
    out[key] = { x, y };
    n++;
  }
  return out;
}

/** A stored payload → pins for every layout. Never throws. */
export function decodePins(raw: string | null): LayoutPins {
  const out = emptyPins();
  if (!raw) return out;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return out;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return out;
  for (const layout of LAYOUTS) {
    out[layout] = decodeLayout((parsed as Record<string, unknown>)[layout]);
  }
  return out;
}

export function loadPins(): LayoutPins {
  try {
    return decodePins(localStorage.getItem(POSITIONS_KEY));
  } catch {
    return emptyPins();
  }
}

export function savePins(all: LayoutPins): void {
  try {
    // No pins anywhere REMOVES the key, so "never moved a cluster" and "reset everything" restore
    // identically and the storage inspector stays honest (the `saveWorkspace` rule).
    if (LAYOUTS.every((l) => Object.keys(all[l]).length === 0)) {
      localStorage.removeItem(POSITIONS_KEY);
    } else {
      localStorage.setItem(POSITIONS_KEY, JSON.stringify(all));
    }
  } catch {
    /* quota, private mode, storage disabled — never fatal */
  }
}

/** Pin one cluster. Re-pinning moves the key to the newest slot, and a layout at the bound drops
 *  its OLDEST pin — the one least likely to still describe where the operator wants anything. */
export function withPin(
  all: LayoutPins,
  layout: GroupBy,
  groupKey: string,
  pos: Pin,
): LayoutPins {
  const x = coord(pos.x);
  const y = coord(pos.y);
  if (x === null || y === null || !groupKey) return all;
  const next: Pins = { ...all[layout] };
  delete next[groupKey];
  const keys = Object.keys(next);
  for (let i = 0; i <= keys.length - MAX_PINS_PER_LAYOUT; i++) delete next[keys[i]];
  next[groupKey] = { x, y };
  return { ...all, [layout]: next };
}

/** Reset layout: this layout's pins, and only this layout's. */
export function withoutLayout(all: LayoutPins, layout: GroupBy): LayoutPins {
  return { ...all, [layout]: {} };
}

/** The computed graph with the pinned clusters moved to their pins.
 *
 *  Only cluster nodes move. A session chip's position is relative to its cluster (`parentId`), so
 *  it follows without being touched, and so do the folder-hierarchy edges. Returns the SAME array
 *  when nothing is pinned, so a caller's identity checks stay meaningful. */
export function applyPins(nodes: Node[], pins: Pins): Node[] {
  if (!Object.keys(pins).length) return nodes;
  let changed = false;
  const out = nodes.map((n) => {
    if (n.type !== "projectGroup") return n;
    const pin = pins[(n.data as ProjectGroupData).groupKey];
    if (!pin || (pin.x === n.position.x && pin.y === n.position.y)) return n;
    changed = true;
    return { ...n, position: { x: pin.x, y: pin.y } };
  });
  return changed ? out : nodes;
}
