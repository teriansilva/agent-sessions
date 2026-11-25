import { type Node } from "@xyflow/react";
import type { RemoteShare } from "../../lib/federation";

/** The data of a `remote` map node (#2055): a `RemoteShare`, never a local session. */
export interface RemoteNodeData extends Record<string, unknown> {
  remote: RemoteShare;
}

export const REMOTE_NODE_TYPE = "remote";
/** The unit chip's box (overview.css `.tr-ov-chip`, `CHIP_W` / `CHIP_H` in overviewGraph.ts). */
const CHIP_W = 240;
const CHIP_H = 96;
const GAP = 16;
const PER_ROW = 4;

/** The remote nodes for a laid-out map: a row under everything local, in list order. They are
 *  appended to what React Flow renders and are never among `buildOverview`'s inputs or outputs,
 *  so the local graph (and everything derived from it) does not know they exist. */
export function remoteMapNodes(local: readonly Node[], remote: readonly RemoteShare[]): Node[] {
  if (!remote.length) return [];
  const top = local.filter((n) => !n.parentId);
  const left = top.length ? Math.min(...top.map((n) => n.position.x)) : 0;
  const floor = top.length
    ? Math.max(
        ...top.map((n) => n.position.y + (typeof n.style?.height === "number" ? n.style.height : CHIP_H)),
      ) + 3 * GAP
    : 0;
  return remote.map((r, i) => ({
    id: `remote:${r.peer_id}/${r.grant_id}`,
    type: REMOTE_NODE_TYPE,
    position: {
      x: left + (i % PER_ROW) * (CHIP_W + GAP),
      y: floor + Math.floor(i / PER_ROW) * (CHIP_H + GAP),
    },
    data: { remote: r } satisfies RemoteNodeData,
    draggable: false,
    selectable: false,
    connectable: false,
    deletable: false,
  }));
}
