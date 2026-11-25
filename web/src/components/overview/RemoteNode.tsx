import { type NodeProps } from "@xyflow/react";
import { remoteRowState } from "../../lib/federation";
import { PeerBadge, ScreenTag } from "../federation/PeerBadge";
import type { RemoteNodeData } from "./remoteNodes";

/** A session another instance shares, on the map (#2055).
 *
 *  Its own node type, never a `session` node: it carries a `RemoteShare`, not a local session, so
 *  nothing the map does with local units (the ⋯ menu, drag-to-reassign, grouping, tags, windows,
 *  tethers, the counts in the strip) can reach it. It is drawn with the peer badge's dashed line
 *  as its outline and the badge where a local unit shows its engine; it has no LED (this
 *  instance does not know whether that agent is working) and no ⋯. A click opens the read-only
 *  pane's route (the canvas's `onNodeClick`); it never opens a map window, so nothing of a peer
 *  is ever mounted in the window layer. */
export function RemoteNode({ data }: NodeProps) {
  const { remote } = data as RemoteNodeData;
  const row = remoteRowState(remote);
  return (
    <div
      className={`tr-ov-chip tr-ov-chip--remote nopan nodrag${row.offline ? " offline" : ""}`}
      aria-label={row.label}
      title={`${row.title}\nShared by ${row.peer}`}
      data-testid="remote-node"
      data-kind={row.screen ? "terminal" : "api"}
    >
      <span className="tr-ov-chip-head tr-ov-chip-head--remote">
        <PeerBadge name={row.peer} />
        {/* A shared terminal (#2060) is a screen to watch: its tag says so where an API session
            says "view only". */}
        {row.screen ? <ScreenTag /> : <span className="tr-ov-callsign">view only</span>}
      </span>
      <span className="tr-ov-ttl">{row.title}</span>
      <span className="tr-ov-chip-foot">
        <span className="tr-ov-unit-state">{row.offline ? "Offline" : "Shared"}</span>
        {!row.offline && (
          <>
            <span className="tr-ov-foot-sep" aria-hidden="true">
              ·
            </span>
            <span className="tr-ov-time">{row.until}</span>
          </>
        )}
      </span>
    </div>
  );
}
