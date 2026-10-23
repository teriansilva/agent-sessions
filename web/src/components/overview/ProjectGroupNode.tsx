import { Handle, type NodeProps, Position } from "@xyflow/react";
import { ChevronDown, ChevronRight, FolderInput } from "lucide-react";
import { type CSSProperties, type MouseEvent, useState } from "react";
import { api, ApiError } from "../../lib/api";
import { pathBase, shortCwd } from "../../lib/format";
import type { ProjectGroupData } from "../../lib/overviewGraph";
import { useOverviewActions } from "./overviewActions";

/** A project cluster container. React Flow sizes it from the node `style`; the header shows
 *  the collapse/expand chevron + display name + count. Clicking the header is handled by the
 *  canvas's React Flow `onNodeClick`, which toggles this cluster (#144/#149) — the one
 *  exception is the promote-to-project button (#361 Phase 4), which owns its click and
 *  stops propagation so it never doubles as a toggle. The (hidden) handles let the
 *  folder-hierarchy edges attach top/bottom (#148). When a custom name is set, the real
 *  path is shown as a subtitle to disambiguate. */
export function ProjectGroupNode({ data }: NodeProps) {
  const { project, kind, cwd, cwdCount, count, collapsed, name, color, owner, live } =
    data as ProjectGroupData;
  const { refetchSessions } = useOverviewActions();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  // #361/#424: a folder group keeps the custom-name/path display; a project-entity group is
  // labelled by its entity name and an agent group by its engine name (`project` carries the
  // label for both). A project/agent group spanning several folders (#361 Phase 4) shows the
  // folder count instead of a single (arbitrary) path.
  const label = kind === "folder" ? name || shortCwd(cwd) || project : project;
  const spansFolders =
    (kind === "project" || kind === "agent") && (cwdCount ?? 1) > 1;
  // An empty project cluster (#447) is a drop target — show a discoverable hint instead of a
  // (nonexistent) path subtitle, since clusters are collapsed (header-only) by default.
  const isEmptyProject = kind === "project" && count === 0;
  const subtitle = isEmptyProject
    ? "drag sessions here"
    : spansFolders
      ? `${cwdCount} folders`
      : shortCwd(cwd);

  // "Make this a project" (#361 Phase 4): promote a folder-fallback cluster into an
  // entity adopting this cwd. A 409 (folder already owned) carries the server's detail
  // string — surfaced inline in the header subtitle slot.
  const makeProject = (e: MouseEvent) => {
    e.stopPropagation(); // the surrounding header click toggles collapse — not this
    if (busy) return;
    setBusy(true);
    setErr(null);
    api
      .createProject({ name: pathBase(cwd), folders: [cwd] })
      .then(() => refetchSessions())
      .catch((ex: unknown) => {
        setErr(
          ex instanceof ApiError && ex.message
            ? ex.message
            : "Couldn’t create the project.",
        );
      })
      .finally(() => setBusy(false));
  };

  // The sector's callsign kind (#1600, the v5 board): SECTOR for a project, FOLDER, AGENT. It
  // sits INSIDE the armour panel, in the header, before the cluster's name.
  const sectorKind = kind === "folder" ? "Folder" : kind === "agent" ? "Agent" : "Sector";

  return (
    <div
      className={`tr-ov-group tr-ov-group--${kind}${collapsed ? " collapsed" : ""}${isEmptyProject ? " empty" : ""}${color ? " tinted" : ""}${live ? " is-live" : ""}`}
      style={color ? ({ "--proj": color } as CSSProperties) : undefined}
      data-sector-live={live ? "" : undefined}
    >
      {/* The armour panel's lit rail (#1600): brighter while an agent works in this sector. */}
      <span className="tr-ov-rail" aria-hidden="true" />
      <Handle
        type="target"
        position={Position.Top}
        className="tr-ov-handle"
        isConnectable={false}
      />
      {/* The header is the cluster's drag handle (#968, `dragHandle` in overviewGraph) as well as
          its collapse toggle — React Flow suppresses the click after a real drag, so the two
          gestures stay apart. `nopan` keeps a press here from panning the canvas instead. */}
      <div
        className="tr-ov-group-head nopan"
        aria-expanded={!collapsed}
        title={`${collapsed ? "Expand" : "Collapse"} ${kind === "folder" ? cwd : project}`}
      >
        <Chevron size={14} className="tr-ov-chev" aria-hidden="true" />
        <span className="nj-notch tr-ov-notch" aria-hidden="true" />
        {color && <span className="tr-ov-proj-dot" aria-hidden="true" />}
        {/* Two lines (#1600): a folder sector puts its kind + owning-project tag on line 1 and
            the path across line 2, so a narrow one-unit sector never squeezes the path to
            nothing; a project/agent sector reads "SECTOR // NAME" with the folder under it. */}
        <span className="tr-ov-meta">
          <span className="tr-ov-callline">
            <span className="tr-ov-kind" aria-hidden="true">
              {sectorKind} //
            </span>
            {kind === "folder" ? (
              owner && (
                <span
                  className="tr-ov-owner"
                  title={`In project ${owner.name}`}
                  style={
                    owner.color
                      ? ({ "--owner": owner.color } as CSSProperties)
                      : undefined
                  }
                >
                  <span className="tr-ov-owner-dot" aria-hidden="true" />
                  {owner.name}
                </span>
              )
            ) : (
              <span className="tr-ov-path">{label}</span>
            )}
          </span>
          {kind === "folder" && <span className="tr-ov-path">{label}</span>}
          {err ? (
            <span className="tr-ov-err" title={err}>
              {err}
            </span>
          ) : (
            (name || kind === "project" || kind === "agent") && (
              <span className="tr-ov-cwd">{subtitle}</span>
            )
          )}
        </span>
        <span className="tr-ov-count">
          {count}
          <span className="tr-ov-count-word"> session{count === 1 ? "" : "s"}</span>
        </span>
        {kind === "folder" && (
          <button
            type="button"
            className="tr-ov-mkproj nodrag nopan"
            onClick={makeProject}
            disabled={busy}
            title={`Make ${shortCwd(cwd)} a project`}
            aria-label={`Make ${shortCwd(cwd)} a project`}
          >
            <FolderInput size={13} aria-hidden="true" />
          </button>
        )}
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="tr-ov-handle"
        isConnectable={false}
      />
    </div>
  );
}
