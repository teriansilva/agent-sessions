import { type NodeProps } from "@xyflow/react";
import { type CSSProperties } from "react";
import { useNavigate } from "react-router-dom";
import { engineBadge, engineColor, relTime } from "../../lib/format";
import type { SessionNodeData } from "../../lib/overviewGraph";

/** A session chip inside a project cluster. Engine-colored; filled dot = active,
 *  hollow = idle; archived dimmed. Click opens the session (URL = identity). */
export function SessionNode({ data }: NodeProps) {
  const { session, active, selected } = data as SessionNodeData;
  const navigate = useNavigate();
  const color = engineColor(session.engine);
  const title = session.title || session.first_user_message || session.short_uuid;
  const open = () => navigate(`/s/${encodeURIComponent(session.engine)}/${encodeURIComponent(session.uuid)}`);

  return (
    <button
      type="button"
      // `nodrag nopan`: let the click reach this button instead of React Flow treating the
      // pointerdown as a pan/drag and swallowing it (#149).
      className={`tr-ov-chip nodrag nopan${session.archived ? " archived" : ""}${selected ? " selected" : ""}`}
      style={{ "--eng": color } as CSSProperties}
      onClick={open}
      title={`${title}\n${session.cwd}`}
      aria-label={`Open ${title}`}
      aria-current={selected ? "true" : undefined}
    >
      <span className={`tr-ov-dot ${active ? "active" : "idle"}`} aria-hidden="true" />
      <span className="tr-ov-meta">
        <span className="tr-ov-ttl">{title}</span>
        <span className="tr-ov-sub">{relTime(session.last_mtime)}</span>
      </span>
      <span className="tr-ov-eng" aria-hidden="true">
        {engineBadge(session.engine)}
      </span>
    </button>
  );
}
