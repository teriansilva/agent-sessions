import { type NodeProps } from "@xyflow/react";
import { type CSSProperties } from "react";
import { engineBadge, engineColor, relTime, shortCwd } from "../../lib/format";
import type { SessionNodeData } from "../../lib/overviewGraph";
import { HudFrame } from "../hud/HudFrame";

/** A session chip inside a project cluster — at information parity with the sidebar list row
 *  (#424 Phase 4): a working/idle LED, the title, an intervention "!" badge, the AI summary,
 *  the engine badge, the project + folder, and the relative time. Engine-coloured left border;
 *  archived dimmed; `selected` highlights the open session. Presentational — the click is
 *  handled by the canvas's React Flow `onNodeClick` (opens the session); in Projects layout the
 *  chip is also draggable to reassign it (#424 Phase 5), so it carries `nopan` (no canvas pan on
 *  press) but NOT `nodrag` — React Flow tells a click from a drag by the movement threshold. */
export function SessionNode({ data }: NodeProps) {
  const { session, active, working, selected, folderLabel } = data as SessionNodeData;
  const color = engineColor(session.engine);
  const title = session.title || session.first_user_message || session.short_uuid;
  const intervention = !!session.intervention_required && !session.review_excluded;
  const summary = session.review_excluded ? "Excluded from AI review" : session.ai_summary;
  const folder = folderLabel ?? shortCwd(session.cwd);

  return (
    <div
      className={`tr-ov-chip nopan${session.archived ? " archived" : ""}${selected ? " selected" : ""}`}
      style={{ "--eng": color } as CSSProperties}
      title={`${title}\n${session.cwd}`}
      aria-label={`Open ${title}`}
      aria-current={selected ? "true" : undefined}
    >
      <HudFrame />
      <span className="tr-ov-chip-head">
        <span
          className={`tr-ov-dot ${working ? "working" : active ? "active" : "idle"}`}
          aria-label={working ? "agent working" : undefined}
          role={working ? "status" : undefined}
        />
        <span className="tr-ov-ttl">{title}</span>
        {intervention && (
          <span
            className="tr-ov-alert"
            role="img"
            aria-label={`intervention required: ${session.intervention_reason || "see session"}`}
            title={session.intervention_reason || "Intervention required"}
          >
            !
          </span>
        )}
        <span className="tr-ov-eng" aria-hidden="true">
          {engineBadge(session.engine)}
        </span>
      </span>
      {summary && (
        <span className={`tr-ov-summary${session.review_excluded ? " excluded" : ""}`}>
          {summary}
        </span>
      )}
      <span className="tr-ov-chip-foot">
        {session.project.kind === "project" && (
          <>
            <span className="tr-ov-chip-proj">
              {session.project.color && (
                <span
                  className="tr-ov-proj-dot"
                  style={{ background: session.project.color }}
                  aria-hidden="true"
                />
              )}
              {session.project.name}
            </span>
            <span className="tr-ov-foot-sep" aria-hidden="true">
              ·
            </span>
          </>
        )}
        <span className="tr-ov-chip-folder">
          <span className="tr-ov-folder-mark" aria-hidden="true">
            {"▸ "}
          </span>
          {folder}
        </span>
        <span className="tr-ov-foot-sep" aria-hidden="true">
          ·
        </span>
        <span className="tr-ov-time">{relTime(session.last_mtime)}</span>
      </span>
    </div>
  );
}
