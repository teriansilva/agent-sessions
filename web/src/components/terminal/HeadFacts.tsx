import { Crosshair } from "lucide-react";
import type { CSSProperties } from "react";
import { engineBadge, engineName, projectColor, relTime, shortCwd } from "../../lib/format";
import type { TermStatus } from "../../lib/termSocket";
import type { Session } from "../../types/api";
import styles from "./Terminal.module.css";

/** The LED class + label for the reusable .hud-led primitive (#211 4c, re-cut in #744). Distinct
 *  from statusText (the transient corner overlay) — this one is always present so the surface
 *  that renders it always declares its link state. #744: the label is the LED's accessible name
 *  + tooltip, never printed beside the dot. MOVED HERE from Terminal.tsx (#1109): both the pane's
 *  own bar and a window's chrome render the same run, so the resolver lives beside the run. */
function headStatus(s: TermStatus): { label: string; led: string } {
  switch (s.kind) {
    case "connected":
      return { label: "LIVE", led: "up" };
    case "connecting":
      return { label: "CONNECTING", led: "" };
    case "reconnecting":
      return { label: "RECONNECTING", led: "" };
    case "rejected":
      return { label: "OFFLINE", led: "down" };
  }
}

/** The pane-head facts run (#1109): ONE component for every surface that shows it.
 *
 *  Rendered by `<Terminal>`'s own `panelHead` (the full-screen pane, unchanged) and by a map
 *  window's chrome — two hand-copied runs is exactly how the two surfaces would drift, so the
 *  LED / engine badge / custom tag / mission tag / project · relative-time sequence and its
 *  collapse ladder live here and nowhere else.
 *
 *  The collapse ladder itself stays in `Terminal.module.css`, keyed on a container named
 *  `panelhead`: the pane's `.panelHead` and a window's chrome bar both declare that container,
 *  so one set of rules governs both bars (the chrome adds its own earlier-yielding rules for the
 *  tag, scoped to `[data-window-head]`).
 *
 *  `showTag` is the one asymmetry, and it is deliberate: the full-screen pane keeps its bar
 *  EXACTLY as shipped (#1109 out of scope) — the tag chip rides only where a caller asks for it
 *  (the window chrome, whose bar has no sidebar beside it and therefore carries every fact). */
export function HeadFacts({
  engine,
  status,
  row,
  showTag = false,
}: {
  engine: string;
  /** THIS browser's attachment state — the pane's socket, not the row's activity. The row's own
   *  status is a different signal (the sidebar dot / the session brief resolve it separately). */
  status: TermStatus;
  /** The row for THIS session (#232, re-sourced in #867). May be undefined while nothing has
   *  resolved yet — the run degrades to LED + engine badge and never throws (a header that
   *  cannot name its project must not take the session down with it). */
  row: Session | undefined;
  /** Render the session's custom tag (#551). Windows pass true; the full-screen pane does not. */
  showTag?: boolean;
}) {
  const head = headStatus(status);
  // Header meta run (#744): the same facts the sidebar row carries — project and how stale the
  // session is. A folder ref's `name` is the FULL cwd by server contract (projects.resolve), so
  // clients shorten it themselves; an adopted project keeps its entity name + colour dot.
  // `?? ""` is not defensive clutter: the run is fed by a LOOKUP as well as the sidebar's list
  // (#867), so a row can arrive from a source this component does not control.
  // `shortCwd(undefined)` throws, and a throw here is caught by the route's error boundary and
  // replaces the WHOLE session — live terminal included — with "we couldn't load this part of
  // the app". A header that cannot name its project must degrade to a blank chip, never take
  // the session down with it.
  const projectLabel = row
    ? row.project.kind === "project"
      ? (row.project.name ?? "")
      : shortCwd(row.project.name ?? row.cwd ?? "")
    : "";
  // The chip's tooltip is the FULL launch folder, not a repeat of the label it sits on (#867).
  // The visible text is either an entity name or a shortened cwd, so "which folder is this
  // actually in" had no answer anywhere in the pane — you had to open the file panel to find
  // out. An adopted project keeps its name on the first line and gains the folder under it.
  const projectTitle = row
    ? row.project.kind === "project"
      ? `${row.project.name ?? ""}\n${row.cwd ?? ""}`
      : (row.cwd ?? "")
    : "";
  const projectStyle =
    row?.project.kind === "project"
      ? ({
          "--proj": row.project.color || projectColor(row.project.id),
        } as CSSProperties)
      : undefined;

  return (
    <>
      <span
        className={`${styles.headLed} hud-led ${head.led}`}
        role="img"
        aria-label={`status: ${head.label.toLowerCase()}`}
        title={head.label}
        data-head-led={head.label.toLowerCase()}
      />
      <span className={styles.headEng} title={engineName(engine)}>
        {engineBadge(engine)}
      </span>
      {showTag && row?.tag ? (
        <span
          className={styles.headTag}
          title={`Tag: ${row.tag}`}
          data-head-tag=""
        >
          {row.tag}
        </span>
      ) : null}
      {row?.mission ? (
        <span
          className={styles.headMission}
          title={`Mission: ${row.mission.title} (${row.mission.state})`}
          data-testid="head-mission-tag"
        >
          <Crosshair size={9} aria-hidden="true" />
          <span className={styles.headMissionText}>{row.mission.title}</span>
        </span>
      ) : null}
      {row && (
        <span className={styles.headMeta}>
          {projectLabel && (
            <span
              className={styles.headProject}
              style={projectStyle}
              title={projectTitle}
            >
              {row.project.kind === "project" && (
                <span
                  className={styles.headProjectDot}
                  aria-hidden="true"
                />
              )}
              {projectLabel}
            </span>
          )}
          {/* Drops first as the bar narrows (see .headUpdated) — it carries the separator
              with it, so the project never trails a dangling "·". */}
          <span className={styles.headUpdated}>
            {projectLabel ? " · " : ""}
            {relTime(row.last_mtime)}
          </span>
        </span>
      )}
    </>
  );
}
