import { type NodeProps } from "@xyflow/react";
import { type CSSProperties } from "react";
import { engineBadge, engineColor, relTime } from "../../lib/format";
import type { SessionNodeData } from "../../lib/overviewGraph";

/** A session chip inside a project cluster. Engine-colored; filled dot = active, hollow =
 *  idle; archived dimmed; `selected` highlights the open session. Presentational — the click
 *  is handled by the canvas's React Flow `onNodeClick` (opens the session). `nodrag nopan`
 *  stops a press from initiating a pan/drag. */
export function SessionNode({ data }: NodeProps) {
  const { session, active, working, selected } = data as SessionNodeData;
  const color = engineColor(session.engine);
  const title = session.title || session.first_user_message || session.short_uuid;

  return (
    <div
      className={`tr-ov-chip nodrag nopan${session.archived ? " archived" : ""}${selected ? " selected" : ""}`}
      style={{ "--eng": color } as CSSProperties}
      title={`${title}\n${session.cwd}`}
      aria-label={`Open ${title}`}
      aria-current={selected ? "true" : undefined}
    >
      <span
        className={`tr-ov-dot ${working ? "working" : active ? "active" : "idle"}`}
        aria-label={working ? "agent working" : undefined}
        role={working ? "status" : undefined}
      />
      <span className="tr-ov-meta">
        <span className="tr-ov-ttl">{title}</span>
        <span className="tr-ov-sub">{relTime(session.last_mtime)}</span>
      </span>
      <span className="tr-ov-eng" aria-hidden="true">
        {engineBadge(session.engine)}
      </span>
    </div>
  );
}
