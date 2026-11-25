/** One mission plan in the Library (#1924) on the shared Library card (#2144): its facts and its
 *  provenance. A tap anywhere opens it; Export is the card's own control. Archive / Unarchive live
 *  in the detail's ⋯. Archiving hides a plan from new use and never deletes it. */
import { Workflow } from "lucide-react";
import { missionPlanPath } from "../../lib/routes";
import type { MissionPlanRow } from "../../types/missionPlans";
import { ImportedMark } from "../share/ShareDialogs";
import { CardExport, LibraryCard } from "../library/LibraryCard";
import lib from "../library/library.module.css";
import { countsText } from "./planRules";

export function MissionPlanCard({ plan }: { plan: MissionPlanRow }) {
  const fromFile = plan.origin_kind === "file";
  return (
    <LibraryCard
      testId={`mission-plan-card-${plan.id}`}
      to={missionPlanPath(plan.id)}
      title={plan.title}
      kind="Plan"
      version={String(plan.version)}
      badges={plan.archived ? <span className={lib.grade}>Archived</span> : undefined}
      source={fromFile ? "imported" : "mission"}
      summary={plan.summary || "No summary."}
      meta={countsText(plan.counts)}
      provenance={
        fromFile ? (
          <ImportedMark />
        ) : (
          <>
            <Workflow size={14} aria-hidden />
            Saved from a mission
          </>
        )
      }
      actions={
        plan.archived ? undefined : <CardExport kind="mission-plan" id={plan.id} title={plan.title} />
      }
    />
  );
}
