import { type NodeProps } from "@xyflow/react";
import { MoreHorizontal } from "lucide-react";
import { type CSSProperties } from "react";
import {
  engineBadge,
  engineColor,
  projectColor,
  relTime,
  shortCwd,
} from "../../lib/format";
import type { SessionNodeData } from "../../lib/overviewGraph";
import { HudFrame } from "../hud/HudFrame";
import { useOverviewActions } from "./overviewActions";
import { useEngineRoster } from "../../app/engineRoster";
import { originOf, useAutomationOrigins } from "../../app/automationOrigins";
import { OriginBadge } from "../automations/OriginBadge";

/** A session chip inside a project cluster — at information parity with the sidebar list row
 *  (#424 Phase 4): a working/idle LED, the title, an intervention "!" badge, the AI summary,
 *  the engine badge, the project + folder, and the relative time. Framed by the HudFrame corner
 *  brackets only (#476) — no engine left rail; engine identity reads off the coloured dot/handles
 *  + badge. Archived dimmed; `selected` highlights the open session. The chip body's click is
 *  handled by the canvas's React Flow `onNodeClick` (opens the session); in Projects layout the
 *  chip is also draggable to reassign it (#424 Phase 5), so it carries `nopan` (no canvas pan on
 *  press) but NOT `nodrag` — React Flow tells a click from a drag by the movement threshold.
 *
 *  The ⋯ (#968) is the one control on the chip that is not the chip: it opens the sidebar row's
 *  session menu, and it must never open a window or start a drag — hence `nodrag` and a stopped
 *  click. Right-click anywhere on the chip opens the same menu (the canvas's `onNodeContextMenu`). */
export function SessionNode({ data }: NodeProps) {
  // Re-render when the engine roster lands or changes (#853 P4): this renders agent names,
  // badges or colours, which come from the roster, not from a client-side list.
  useEngineRoster();
  const { session, active, working, selected, folderLabel, opened } =
    data as SessionNodeData;
  const { openSessionMenu } = useOverviewActions();
  // Started by an automation (#1201): the sidebar's badge, from the same origins map.
  const origin = originOf(useAutomationOrigins(), session.id);
  const color = engineColor(session.engine);
  // #284: the server already resolves the meaningful display title (manual rename → AI
  // title → meaningful first message, else ""). Never fall back to the RAW first message
  // here, or a stray "a" / "." would leak as the chip name — drop straight to the short id.
  const title = session.title || session.short_uuid;
  const intervention =
    !!session.intervention_required && !session.review_excluded;
  const summary = session.review_excluded
    ? "Excluded from AI review"
    : session.ai_summary;
  const folder = folderLabel ?? shortCwd(session.cwd);

  return (
    <div
      className={`tr-ov-chip nopan${session.archived ? " archived" : ""}${selected ? " selected" : ""}${opened ? " opened" : ""}`}
      style={
        {
          "--eng": color,
          // Project accent for the foot dot (#285): explicit entity color, else the id hash.
          "--proj": session.project.color || projectColor(session.project.id),
        } as CSSProperties
      }
      title={`${title}\n${session.cwd}`}
      aria-label={`Open ${title}`}
      aria-current={selected ? "true" : undefined}
    >
      <HudFrame />
      {/* #208: this session is open as a workspace window — the marker plus the tether are what
          keep a floating panel legible as "this node, opened". */}
      {opened && (
        <span className="tr-ov-opened" aria-label="open as a window" role="img">
          ▣
        </span>
      )}
      <button
        type="button"
        className="tr-ov-kebab nodrag nopan"
        aria-label={`Session actions: ${title}`}
        title="Session actions"
        aria-haspopup="menu"
        data-chip-menu
        onClick={(e) => {
          // The chip's own click opens a window; this press is the menu's alone.
          e.stopPropagation();
          openSessionMenu(session.id, { element: e.currentTarget }, e.currentTarget);
        }}
      >
        <span className="tr-ov-kebab-glyph" aria-hidden="true">
          <MoreHorizontal size={14} />
        </span>
      </button>
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
        <span
          className={`tr-ov-summary${session.review_excluded ? " excluded" : ""}`}
        >
          {summary}
        </span>
      )}
      <span className="tr-ov-chip-foot">
        {origin ? <OriginBadge origin={origin} /> : null}
        {session.project.kind === "project" && (
          <>
            <span className="tr-ov-chip-proj">
              <span className="tr-ov-proj-dot" aria-hidden="true" />
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
