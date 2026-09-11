/** Mission detail components shared by the Context-first disclosures (#944).
 * Objective-specific follow-through stays on its objective; mission-level notices
 * have their own collapsible section. */
import type {
  MissionContext,
  MissionEvent,
  MissionObjective,
  MissionSupervisor,
} from "../../types/api";

import { MissionContextPanel } from "./MissionContext";
import { MissionObjectives, type ObjectiveOp } from "./MissionObjectives";
import { MissionTimeline } from "./MissionTimeline";

export function ObjectivesPane({
  objectives,
  objectivesState,
  objectivesFailed,
  showNotices,
  supervisor,
  onOps,
  onStandDown,
  busy,
}: {
  objectives: MissionObjective[];
  showNotices?: boolean;
  /** From the mission row: `pending` while the producer is still running (#883). The empty state
   *  renders it, because "we have not been told yet" and "there are none" are different claims. */
  objectivesState?: string | null;
  /** The objectives read failed — "we could not look", not "there are none" (#942 review 1). */
  objectivesFailed?: boolean;
  /** Absent when `assess()` could not run — the notices say so rather than rendering a clean
   *  list, which would read as "nothing to follow up". */
  supervisor?: MissionSupervisor;
  /** Both absent ⇒ read-only, which is what an archived or closed mission gets (#889). */
  onOps?: (ops: ObjectiveOp[]) => Promise<boolean>;
  onStandDown?: (key: string, episode: number) => void;
  busy?: boolean;
}) {
  return (
    <MissionObjectives
      showNotices={showNotices}
      objectives={objectives}
      objectivesState={objectivesState}
      objectivesFailed={objectivesFailed}
      onOps={onOps}
      supervisor={supervisor}
      onStandDown={onStandDown}
      busy={busy}
    />
  );
}

/** The mission's roster and working directory — its own tab since #942, where it used to be the
 *  third heading inside OBJECTIVES. */
export function ContextPane({
  context,
  loading,
  onDetach,
  onMembershipChanged,
  busy,
  spawn,
}: {
  context: MissionContext | null;
  loading: boolean;
  /** Forwarded to the per-session controls: the server refused one because the mission no longer
   *  holds its session, so the roster has to be re-read. */
  onMembershipChanged?: () => void;
  onDetach?: (sessionKey: string) => void;
  busy?: boolean;
  /** Passed straight through to the roster (#894). Absent ⇒ this mission cannot spawn. */
  spawn?: {
    engine: string;
    /** Where the sub-agent will run — shown to the operator and asserted back on START. */
    cwd: string;
    cap: number;
    live: number | null;
    onChanged: (opts?: { membershipChanged?: boolean }) => void;
    onNote: (msg: string) => void;
  };
}) {
  return (
    <MissionContextPanel
      context={context}
      loading={loading}
      onDetach={onDetach}
      onMembershipChanged={onMembershipChanged}
      busy={busy}
      spawn={spawn}
    />
  );
}

export function TimelinePane({
  events,
  cursor,
  loadingMore,
  onLoadMore,
}: {
  events: MissionEvent[];
  cursor: number | null;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  return (
    <MissionTimeline
      events={events}
      hasMore={cursor != null}
      loadingMore={loadingMore}
      onLoadMore={onLoadMore}
    />
  );
}
