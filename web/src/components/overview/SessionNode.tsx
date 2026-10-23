import { type NodeProps } from "@xyflow/react";
import { MoreHorizontal } from "lucide-react";
import { type CSSProperties } from "react";
import {
  engineBadge,
  engineColor,
  engineName,
  projectColor,
  relTime,
  shortCwd,
} from "../../lib/format";
import type { SessionNodeData } from "../../lib/overviewGraph";
import { useOverviewActions } from "./overviewActions";
import { STATE_WORD, unitState } from "./unitState";
import { useEngineRoster } from "../../app/engineRoster";
import { originOf, useAutomationOrigins } from "../../app/automationOrigins";
import { OriginBadge } from "../automations/OriginBadge";

/** A unit card inside a sector (#1600, the v5 map board) — at information parity with the sidebar
 *  list row (#424 Phase 4). The callsign row: the state LED, the engine's colour bar and the
 *  engine's callsign (its badge, with the agent's name). Then the title, the AI summary, and the
 *  status line, which LEADS with the state in words (ENGAGED / AWAITING ORDERS / STANDBY /
 *  OFFLINE) before the project, folder and relative time. An intervention "!" stays on the
 *  callsign row and puts the card on the amber edge. Archived dimmed; `selected` highlights the
 *  open session. The chip body's click is
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
  // The unit's state in words (#1600), from the fields the LED already reads. `running` (a live
  // agent process, #1085) is absent on an older server; recent activity stands in for it there.
  const state = unitState(session.archived, intervention, working, session.running ?? active);

  return (
    <div
      className={`tr-ov-chip nopan${session.archived ? " archived" : ""}${selected ? " selected" : ""}${opened ? " opened" : ""}`}
      data-unit-state={state}
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
          className={`tr-ov-dot ${working ? "working nj-ambient" : active ? "active" : "idle"}`}
          aria-label={working ? "agent working" : undefined}
          role={working ? "status" : undefined}
        />
        <span className="tr-ov-engbar" aria-hidden="true" />
        <span className="tr-ov-eng" aria-hidden="true">
          {engineBadge(session.engine)}
        </span>
        {/* ENGINE-ID, as the window's bar reads it (#1600): two units of one engine never share it. */}
        <span className="tr-ov-callsign" aria-hidden="true">
          {engineName(session.engine)}
          {session.short_uuid ? `-${session.short_uuid}` : ""}
        </span>
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
      </span>
      <span className="tr-ov-ttl">{title}</span>
      {summary && (
        <span
          className={`tr-ov-summary${session.review_excluded ? " excluded" : ""}`}
        >
          {summary}
        </span>
      )}
      <span className="tr-ov-chip-foot">
        <span className="tr-ov-unit-state">{STATE_WORD[state]}</span>
        <span className="tr-ov-foot-sep" aria-hidden="true">
          ·
        </span>
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
