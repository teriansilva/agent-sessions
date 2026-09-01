/** The two read-only panes, split from the loader so this file exports components only
 *  (`react-refresh/only-export-components`). The loader is `useMissionDetail.ts`. */
import type {
  MissionContext,
  MissionEvent,
  MissionObjective,
  MissionSupervisor,
} from "../../types/api";

import { MissionContextPanel } from "./MissionContext";
import { MissionObjectives } from "./MissionObjectives";
import { MissionSupervisorBoard } from "./MissionSupervisorBoard";
import { MissionTimeline } from "./MissionTimeline";
import styles from "./mission.module.css";

export function ObjectivesPane({
  objectives,
  context,
  loading,
  supervisor,
}: {
  objectives: MissionObjective[];
  context: MissionContext | null;
  loading: boolean;
  /** Absent when `assess()` could not run — the board says so rather than rendering a clean
   *  slate, which would read as "nothing to follow up". */
  supervisor?: MissionSupervisor;
}) {
  return (
    <>
      <div className={styles.section}>Objectives</div>
      <MissionObjectives objectives={objectives} />
      <div className={styles.section}>Follow-through</div>
      <MissionSupervisorBoard supervisor={supervisor} />
      <div className={styles.section}>Context</div>
      <MissionContextPanel context={context} loading={loading} />
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
