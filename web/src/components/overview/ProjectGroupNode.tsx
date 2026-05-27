import { type NodeProps } from "@xyflow/react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { shortCwd } from "../../lib/format";
import type { ProjectGroupData } from "../../lib/overviewGraph";
import { useOverviewActions } from "./overviewActions";

/** A project cluster container. React Flow sizes it from the node `style`; the header is a
 *  toggle that collapses/expands the cluster (#144). Child session chips (when expanded) are
 *  positioned by React Flow. */
export function ProjectGroupNode({ data }: NodeProps) {
  const { project, cwd, count, collapsed } = data as ProjectGroupData;
  const { toggle } = useOverviewActions();
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  return (
    <div className={`tr-ov-group${collapsed ? " collapsed" : ""}`}>
      <button
        type="button"
        // `nodrag nopan`: the header is the collapse toggle — let its click through instead of
        // React Flow capturing the pointer for pan/drag (#149).
        className="tr-ov-group-head nodrag nopan"
        onClick={() => toggle(cwd)}
        aria-expanded={!collapsed}
        title={`${collapsed ? "Expand" : "Collapse"} ${cwd}`}
      >
        <Chevron size={14} className="tr-ov-chev" aria-hidden="true" />
        <span className="tr-ov-path">{shortCwd(cwd) || project}</span>
        <span className="tr-ov-count">
          {count} session{count === 1 ? "" : "s"}
        </span>
      </button>
    </div>
  );
}
