/** The two read-only panes, split from the loader so this file exports components only
 *  (`react-refresh/only-export-components`). The loader is `useMissionDetail.ts`. */
import type { MissionContext, MissionEvent, MissionObjective } from "../../types/api";

import { MissionContextPanel } from "./MissionContext";
import { MissionObjectives } from "./MissionObjectives";
import { MissionTimeline } from "./MissionTimeline";
import styles from "./mission.module.css";

export function ObjectivesPane({
  objectives,
  context,
  loading,
}: {
  objectives: MissionObjective[];
  context: MissionContext | null;
  loading: boolean;
}) {
  return (
    <>
      <div className={styles.section}>Objectives</div>
      <MissionObjectives objectives={objectives} />
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
