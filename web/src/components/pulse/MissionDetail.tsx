/** The two read-only panes, split from the loader so this file exports components only
 *  (`react-refresh/only-export-components`). The loader is `useMissionDetail.ts`. */
import type {
  MissionContext,
  MissionEvent,
  MissionObjective,
  MissionSupervisor,
} from "../../types/api";

import { MissionContextPanel } from "./MissionContext";
import { MissionObjectives, type ObjectiveOp } from "./MissionObjectives";
import { MissionSupervisorBoard } from "./MissionSupervisorBoard";
import { MissionTimeline } from "./MissionTimeline";
import styles from "./mission.module.css";

export function ObjectivesPane({
  objectives,
  objectivesState,
  context,
  loading,
  supervisor,
  onOps,
  onStandDown,
  onDetach,
  onMembershipChanged,
  busy,
}: {
  objectives: MissionObjective[];
  /** From the mission row: `pending` while the producer is still running (#883). The empty state
   *  renders it, because "we have not been told yet" and "there are none" are different claims. */
  objectivesState?: string | null;
  context: MissionContext | null;
  loading: boolean;
  /** Forwarded to the per-session controls: the server refused one because the mission no longer
   *  holds its session, so the roster has to be re-read. */
  onMembershipChanged?: () => void;
  /** Absent when `assess()` could not run — the board says so rather than rendering a clean
   *  slate, which would read as "nothing to follow up". */
  supervisor?: MissionSupervisor;
  /** Both absent ⇒ read-only, which is what an archived or closed mission gets (#889). */
  onOps?: (ops: ObjectiveOp[]) => Promise<boolean>;
  onStandDown?: (key: string, episode: number) => void;
  onDetach?: (sessionKey: string) => void;
  busy?: boolean;
}) {
  return (
    <>
      <div className={styles.section}>Objectives</div>
      <MissionObjectives
        objectives={objectives}
        objectivesState={objectivesState}
        onOps={onOps}
      />
      <div className={styles.section}>Follow-through</div>
      <MissionSupervisorBoard
        supervisor={supervisor}
        onStandDown={onStandDown}
        busy={busy}
      />
      <div className={styles.section}>Context</div>
      <MissionContextPanel
        context={context}
        loading={loading}
        onDetach={onDetach}
        onMembershipChanged={onMembershipChanged}
        busy={busy}
      />
    </>
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
