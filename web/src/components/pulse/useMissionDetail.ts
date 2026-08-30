/** One mission's data — thread events, objectives, context, timeline (#878).
 *
 * **This component is keyed on the mission id by its parent, so switching missions REMOUNTS it.**
 * That is the whole fencing strategy, and it is deliberate: the alternative is resetting five
 * pieces of state in an effect and checking a captured id inside every late `.then`, which is
 * both more code and the kind of check that is correct until someone adds a sixth fetch and
 * forgets it. A remount cannot forget. A late response from the previous mission resolves into
 * an unmounted instance and updates nothing.
 *
 * The composer's transient turns deliberately do NOT live here — they belong to the console,
 * keyed by mission, so that switching away and back does not silently discard an answer the
 * operator asked for.
 */
import { useCallback, useEffect, useState } from "react";

import { api } from "../../lib/api";
import type {
  Mission,
  MissionContext,
  MissionEvent,
  MissionObjective,
} from "../../types/api";

const EVENTS_PAGE = 40;

export interface MissionDetailState {
  mission: Mission | null;
  events: MissionEvent[];
  objectives: MissionObjective[];
  context: MissionContext | null;
  cursor: number | null;
  loadingMore: boolean;
  loadOlder: () => void;
}

/** Loads one mission. Split from the panes so the panes stay pure and the loading lives in one
 *  place — the console renders the same data in two different layouts (stops vs the persistent
 *  column) and must not fetch it twice. */
export function useMissionDetail(missionId: string): MissionDetailState {
  const [mission, setMission] = useState<Mission | null>(null);
  const [events, setEvents] = useState<MissionEvent[]>([]);
  const [objectives, setObjectives] = useState<MissionObjective[]>([]);
  const [context, setContext] = useState<MissionContext | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .mission(missionId, { eventsLimit: EVENTS_PAGE })
      .then((m) => {
        if (!live) return;
        setMission(m);
        setEvents(m.events ?? []);
        setCursor(m.events_next_seq ?? null);
      })
      .catch(() => undefined);
    api
      .missionObjectives(missionId)
      .then((r) => live && setObjectives(r.objectives))
      .catch(() => undefined);
    api
      .missionContext(missionId)
      .then((c) => live && setContext(c))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [missionId]);

  const loadOlder = useCallback(() => {
    if (cursor == null || loadingMore) return;
    setLoadingMore(true);
    // A CURSOR, never an offset: events arrive while the operator reads, and an offset window
    // shifts under them — duplicating rows or skipping them.
    api
      .mission(missionId, { eventsLimit: EVENTS_PAGE, before: cursor })
      .then((m) => {
        setEvents((prev) => [...prev, ...(m.events ?? [])]);
        setCursor(m.events_next_seq ?? null);
      })
      .catch(() => undefined)
      // Leaving what is already shown: a failed page is not a reason to drop the timeline.
      .finally(() => setLoadingMore(false));
  }, [missionId, cursor, loadingMore]);

  return { mission, events, objectives, context, cursor, loadingMore, loadOlder };
}
