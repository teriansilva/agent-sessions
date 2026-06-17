import { Handle, type NodeProps, Position } from "@xyflow/react";
import { ChevronDown, ChevronRight, FolderInput } from "lucide-react";
import { type MouseEvent, useState } from "react";
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
  const { project, kind, cwd, cwdCount, count, collapsed, name, color, owner } =
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
  const spansFolders = (kind === "project" || kind === "agent") && (cwdCount ?? 1) > 1;
  const subtitle = spansFolders ? `${cwdCount} folders` : shortCwd(cwd);

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
        setErr(ex instanceof ApiError && ex.message ? ex.message : "Couldn’t create the project.");
      })
      .finally(() => setBusy(false));
  };

  return (
    <div
      className={`tr-ov-group tr-ov-group--${kind}${collapsed ? " collapsed" : ""}`}
      style={color ? { borderTop: `2px solid ${color}` } : undefined}
    >
      <Handle type="target" position={Position.Top} className="tr-ov-handle" isConnectable={false} />
      <div
        className="tr-ov-group-head nodrag nopan"
        aria-expanded={!collapsed}
        title={`${collapsed ? "Expand" : "Collapse"} ${kind === "folder" ? cwd : project}`}
      >
        <Chevron size={14} className="tr-ov-chev" aria-hidden="true" />
        {color && <span className="tr-ov-proj-dot" style={{ background: color }} aria-hidden="true" />}
        <span className="tr-ov-meta">
          {kind === "folder" && owner && (
            <span className="tr-ov-owner" title={`In project ${owner.name}`}>
              <span
                className="tr-ov-owner-dot"
                style={owner.color ? { background: owner.color } : undefined}
                aria-hidden="true"
              />
              {owner.name}
            </span>
          )}
          <span className="tr-ov-path">{label}</span>
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
          {count} session{count === 1 ? "" : "s"}
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
