import { type NodeProps } from "@xyflow/react";
import { shortCwd } from "../../lib/format";
import type { ProjectGroupData } from "../../lib/overviewGraph";

/** A project cluster container. React Flow sizes it from the node `style`; we render the
 *  header (project path + session count). Child session chips are positioned by React Flow. */
export function ProjectGroupNode({ data }: NodeProps) {
  const { project, cwd, count } = data as ProjectGroupData;
  return (
    <div className="tr-ov-group">
      <div className="tr-ov-group-head">
        <span className="tr-ov-folder" aria-hidden="true" />
        <span className="tr-ov-path" title={cwd}>
          {shortCwd(cwd) || project}
        </span>
        <span className="tr-ov-count">
          {count} session{count === 1 ? "" : "s"}
        </span>
      </div>
    </div>
  );
}
