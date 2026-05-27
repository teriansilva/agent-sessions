import { type NodeProps } from "@xyflow/react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { shortCwd } from "../../lib/format";
import type { ProjectGroupData } from "../../lib/overviewGraph";

/** A project cluster container. React Flow sizes it from the node `style`; the header shows
 *  the collapse/expand chevron + project + count. Presentational — clicking the header is
 *  handled by the canvas's React Flow `onNodeClick`, which toggles this cluster (#144/#149).
 *  `nodrag nopan` stops a press from initiating a pan/drag. */
export function ProjectGroupNode({ data }: NodeProps) {
  const { project, cwd, count, collapsed } = data as ProjectGroupData;
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  return (
    <div className={`tr-ov-group${collapsed ? " collapsed" : ""}`}>
      <div
        className="tr-ov-group-head nodrag nopan"
        aria-expanded={!collapsed}
        title={`${collapsed ? "Expand" : "Collapse"} ${cwd}`}
      >
        <Chevron size={14} className="tr-ov-chev" aria-hidden="true" />
        <span className="tr-ov-path">{shortCwd(cwd) || project}</span>
        <span className="tr-ov-count">
          {count} session{count === 1 ? "" : "s"}
        </span>
      </div>
    </div>
  );
}
