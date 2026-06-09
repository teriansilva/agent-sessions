import { Handle, type NodeProps, Position } from "@xyflow/react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { shortCwd } from "../../lib/format";
import type { ProjectGroupData } from "../../lib/overviewGraph";

/** A project cluster container. React Flow sizes it from the node `style`; the header shows
 *  the collapse/expand chevron + display name + count. Presentational — clicking the header
 *  is handled by the canvas's React Flow `onNodeClick`, which toggles this cluster
 *  (#144/#149). The (hidden) handles let the folder-hierarchy edges attach top/bottom (#148).
 *  When a custom name is set, the real path is shown as a subtitle to disambiguate. */
export function ProjectGroupNode({ data }: NodeProps) {
  const { project, cwd, count, collapsed, name } = data as ProjectGroupData;
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  const label = name || shortCwd(cwd) || project;
  return (
    <div className={`tr-ov-group${collapsed ? " collapsed" : ""}`}>
      <Handle type="target" position={Position.Top} className="tr-ov-handle" isConnectable={false} />
      <div
        className="tr-ov-group-head nodrag nopan"
        aria-expanded={!collapsed}
        title={`${collapsed ? "Expand" : "Collapse"} ${cwd}`}
      >
        <Chevron size={14} className="tr-ov-chev" aria-hidden="true" />
        <span className="tr-ov-meta">
          <span className="tr-ov-path">{label}</span>
          {name && <span className="tr-ov-cwd">{shortCwd(cwd)}</span>}
        </span>
        <span className="tr-ov-count">
          {count} session{count === 1 ? "" : "s"}
        </span>
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
